"""Wiring: build every subsystem of one instance from its configuration."""

from __future__ import annotations

import contextlib
import logging
from typing import Any

import httpx

from decapod.agents import Roster
from decapod.caucus import Admission, Caucus
from decapod.config import InstanceConfig
from decapod.config.providers import resolve_routes
from decapod.control import Approvals, Budget, Controller, PolicyEngine
from decapod.domain import ActionSpec, ConfigError, CycleStatus, GoalStatus, RiskTier, RunState
from decapod.executive import Reflector
from decapod.llm import LLM, LLMProvider, build_provider
from decapod.logs import EventBus
from decapod.memory import AgendaStore, MemoryStore
from decapod.storage import Database, EventLog
from decapod.tools import (
    MCPManager,
    ProcessRegistry,
    Tool,
    ToolContext,
    ToolExecutor,
    ToolRegistry,
    builtin_tools,
)
from decapod.tools.plugins import plugin_tools
from decapod.util.timeutil import now, to_iso

log = logging.getLogger(__name__)


class Instance:
    def __init__(
        self,
        config: InstanceConfig,
        *,
        provider: LLMProvider | None = None,
        http: httpx.AsyncClient | None = None,
        extra_tools: list[Tool] | None = None,
        include_builtin_tools: bool = True,
        control_poll: float = 0.5,
    ) -> None:
        self.config = config
        self.paths = config.paths
        self.settings = config.settings
        self.secrets = config.secrets
        self.paths.ensure()

        self.db = Database(self.paths.db)
        self.bus = EventBus()
        self.events = EventLog(self.db, self.bus, redact=self.secrets.redact)
        self.budget = Budget(self.db, self.settings.budget)
        self.policy = PolicyEngine(self.settings, config.charter, self.budget, self.db)
        self.approvals = Approvals(self.db, self.events, self.settings.policy.approval_ttl)
        self.controller = Controller(
            self.db, self.events, self.paths.stop_file, poll_interval=control_poll
        )
        self.agenda = AgendaStore(self.db, self.events)
        self.memory = MemoryStore(self.db, self.settings.memory, self.events)
        self.routing = resolve_routes(self.settings.llm, self.secrets)
        if provider is None:
            if not self.routing.ok:
                self.db.close()
                raise ConfigError("model configuration: " + "; ".join(self.routing.problems))
            provider = build_provider(self.settings.llm, self.secrets, self.routing)
        elif not self.routing.ok:  # injected provider (tests, evals): any route names will do
            self.routing = resolve_routes(self.settings.llm, self.secrets, placeholder=True)
        self.llm = LLM(
            self.settings.llm,
            provider,
            gate=self.budget,
            routing=self.routing,
            recorder=self.events,
        )
        self._owns_http = http is None
        self.http = http or httpx.AsyncClient(follow_redirects=False)
        self.procs = ProcessRegistry()
        self.registry = ToolRegistry(self.settings, self.secrets)
        self.executor = ToolExecutor(self.registry)
        self.mcp = MCPManager(self.settings.mcp.servers)
        self.roster = Roster(self.settings.caucus)
        self.caucus = Caucus(
            self.llm,
            self.roster,
            self.settings.caucus,
            self.events,
            self.admit,
            checkpoint=self.controller.checkpoint,
        )
        self.reflector = Reflector(self.llm, self.roster, self.memory, self.agenda, self.events)
        self._extra_tools = extra_tools or []
        self._include_builtin = include_builtin_tools
        self._started = False

    @property
    def charter(self) -> Any:
        return self.config.charter

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if self._started:
            return
        tools: list[Tool] = builtin_tools() if self._include_builtin else []
        tools += self._extra_tools
        if self._include_builtin:
            tools += self._plugin_tools()
        if self.mcp.configs:
            tools += await self.mcp.start()
            for name, error in self.mcp.errors.items():
                self.events.append(
                    "mcp.error",
                    f"MCP server {name} failed: {error}",
                    {"server": name, "error": error},
                    actor="tools",
                )
        for tool in tools:
            try:
                self.registry.register(tool)
            except ValueError as exc:  # a plugin/MCP name colliding with an existing tool
                self.registry.unavailable[f"{tool.name} (duplicate)"] = str(exc)
                self.events.append("tools.duplicate", str(exc), {"tool": tool.name}, actor="tools")
        self.controller.on_kill(self.procs.kill_all)
        self.controller.on_kill(self.mcp.kill_all)
        self.recover()
        self.seed_goals()
        self._started = True

    def _plugin_tools(self) -> list[Tool]:
        def report(name: str, exc: Exception) -> None:
            self.events.append(
                "tools.plugin_error",
                f"plugin {name} failed: {exc}",
                {"plugin": name, "error": str(exc)},
                actor="tools",
            )

        return plugin_tools(report)

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self.mcp.close()
        with contextlib.suppress(Exception):
            await self.llm.close()
        if self._owns_http:
            with contextlib.suppress(Exception):
                await self.http.aclose()
        self.db.close()

    def recover(self) -> int:
        """Mark cycles left 'running' by a crash as aborted."""
        rows = self.db.query(
            "SELECT id, phase FROM cycles WHERE status = ?", (CycleStatus.RUNNING.value,)
        )
        for row in rows:
            self.db.execute(
                "UPDATE cycles SET status = ?, ended_at = ?, error = ? WHERE id = ?",
                (
                    CycleStatus.ABORTED.value,
                    to_iso(now()),
                    f"process ended during phase {row['phase'] or 'unknown'}",
                    row["id"],
                ),
            )
            self.events.append(
                "cycle.aborted",
                f"{row['id']} aborted during {row['phase']}",
                {"cycle_id": row["id"], "phase": row["phase"]},
                cycle_id=row["id"],
            )
            self.db.set_state(
                "pending_crash_notice", {"cycle_id": row["id"], "phase": row["phase"]}
            )
        return len(rows)

    def seed_goals(self) -> int:
        """On first start, add the charter's seed goals (if any) to an empty agenda."""
        if self.db.get_state("seeded") or self.agenda.goals():
            self.db.set_state("seeded", True)
            return 0
        count = 0
        for seed in self.charter.goals.seed:
            self.agenda.add_goal(
                seed.title,
                seed.description,
                priority=seed.priority,
                status=GoalStatus.ACTIVE,
                charter_refs=["mission"],
                rationale="seed goal from the charter",
            )
            count += 1
        self.db.set_state("seeded", True)
        return count

    # ------------------------------------------------------------------ helpers

    def tool_context(self, cycle_id: str | None) -> ToolContext:
        return ToolContext(
            paths=self.paths,
            settings=self.settings,
            charter=self.charter,
            secrets=self.secrets,
            db=self.db,
            events=self.events,
            agenda=self.agenda,
            memory=self.memory,
            budget=self.budget,
            http=self.http,
            procs=self.procs,
            llm=self.llm if self.settings.llm.embeddings_model else None,
            cycle_id=cycle_id,
        )

    def admit(self, action: ActionSpec) -> Admission:
        """Would this action be allowed (or approvable)? Used to filter proposals early."""
        if action.kind == "wait":
            return Admission(True, "wait", RiskTier.INTERNAL)
        ctx = self.tool_context(None)
        tool, check = self.registry.check(action, ctx)
        if tool is None or check is None:
            available = ", ".join(self.registry.names()[:40])
            return Admission(
                False,
                f"unknown tool {action.tool!r} (available: {available})",
                RiskTier.IRREVERSIBLE,
            )
        state = RunState.RUNNING
        verdict = self.policy.review(action, check, cycle_id=None, run_state=state, count=False)
        if verdict.outcome == "deny" and not verdict.transient:
            return Admission(False, verdict.reason, check.tier)
        return Admission(True, verdict.reason, check.tier)

    def reload_charter_if_changed(self) -> bool:
        previous = self.charter.sha256
        try:
            charter = self.config.reload_charter()
        except Exception as exc:  # keep running on the last good charter
            self.events.append(
                "charter.error",
                f"charter reload failed: {exc}",
                {"error": str(exc)},
                actor="control",
            )
            return False
        if charter.sha256 != previous:
            self.policy.update_charter(charter)
            self.events.append(
                "charter.changed",
                f"charter updated ({charter.sha256[:12]})",
                {"sha256": charter.sha256, "previous": previous},
                actor="operator",
            )
            return True
        return False
