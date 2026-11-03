"""Tool registry and executor."""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import time
from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from decapod.config import Secrets, Settings
from decapod.control import ToolCheck
from decapod.domain import ActionSpec, Cost, RiskTier, ToolResult
from decapod.tools.base import Tool, ToolArgs, ToolContext
from decapod.util.text import json_dumps, one_line, truncate
from decapod.util.timeutil import now, to_iso

log = logging.getLogger(__name__)

#: Hard upper bound for any single tool run, on top of tool-specific timeouts.
TOOL_TIMEOUT_S = 600


class ToolRegistry:
    def __init__(self, settings: Settings, secrets: Secrets) -> None:
        self.settings = settings
        self.secrets = secrets
        self._tools: dict[str, Tool] = {}
        self.unavailable: dict[str, str] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name {tool.name!r}")
        if any(fnmatch.fnmatchcase(tool.name, p) for p in self.settings.tools.disabled):
            self.unavailable[tool.name] = "disabled in config"
            return
        reason = tool.unavailable_reason(self.settings, self.secrets)
        if reason:
            self.unavailable[tool.name] = reason
            return
        self._tools[tool.name] = tool

    def get(self, name: str | None) -> Tool | None:
        return self._tools.get(name or "")

    def tools(self) -> list[Tool]:
        return sorted(self._tools.values(), key=lambda t: (int(t.tier), t.name))

    def names(self) -> list[str]:
        return [t.name for t in self.tools()]

    def parse_args(self, tool: Tool, args: dict[str, Any]) -> tuple[ToolArgs | None, str | None]:
        try:
            return tool.Args.model_validate(args), None
        except ValidationError as exc:
            parts = []
            for err in exc.errors()[:5]:
                loc = ".".join(str(p) for p in err["loc"]) or "(args)"
                parts.append(f"{loc}: {err['msg']}")
            return None, "; ".join(parts)

    def check(self, action: ActionSpec, ctx: ToolContext) -> tuple[Tool | None, ToolCheck | None]:
        """Build the policy engine's view of an action."""
        if action.kind == "wait":
            return None, None
        tool = self.get(action.tool)
        if tool is None:
            return None, None
        parsed, error = self.parse_args(tool, action.args)
        scopes: list[str] = []
        if parsed is not None:
            scopes = tool.scopes(parsed)
            if error is None:
                try:
                    error = tool.precheck(parsed, ctx)
                except Exception as exc:
                    error = f"precheck failed: {exc}"
        else:
            scopes = list(tool.required_scopes)
        return tool, ToolCheck(
            name=tool.name,
            tier=tool.tier,
            side_effect=tool.side_effect,
            scopes=scopes,
            args_error=error,
            outbound=tool.outbound,
        )

    def risk_of(self, action: ActionSpec) -> RiskTier:
        if action.kind == "wait":
            return RiskTier.INTERNAL
        tool = self.get(action.tool)
        return tool.tier if tool else RiskTier.IRREVERSIBLE

    def catalog(
        self,
        max_tier: RiskTier = RiskTier.IRREVERSIBLE,
        granted: Callable[[str], bool] | None = None,
    ) -> str:
        """Tool list for prompts, grouped by risk tier.

        Tools whose fixed scopes the charter does not grant are left out, so agents do not
        waste proposals on actions the policy engine would deny anyway.
        """
        lines: list[str] = []
        current: RiskTier | None = None
        for tool in self.tools():
            if tool.tier > max_tier:
                continue
            if granted is not None and not all(granted(s) for s in tool.required_scopes):
                continue
            if tool.tier is not current:
                current = tool.tier
                mode = self.settings.policy.mode_for(tool.tier)
                lines.append(f"[tier {int(tool.tier)} · {tool.tier.label} · policy: {mode}]")
            lines.append(f"- {tool.name}({tool.signature()})")
            lines.append(f"    {tool.description}")
        return "\n".join(lines) or "(no tools available)"


class ToolExecutor:
    """Runs an already-approved action. The policy engine has been consulted before this."""

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    async def execute(
        self, action: ActionSpec, ctx: ToolContext, *, idempotency_key: str | None = None
    ) -> ToolResult:
        tool = self.registry.get(action.tool)
        if tool is None:
            return ToolResult.failure(f"unknown tool {action.tool!r}")
        parsed, error = self.registry.parse_args(tool, action.args)
        if parsed is None:
            return ToolResult.failure(f"invalid arguments: {error}")

        if idempotency_key:
            existing = ctx.db.query_one(
                "SELECT ok, output, error FROM tool_calls WHERE idempotency_key = ?",
                (idempotency_key,),
            )
            if existing is not None:
                if existing["ok"] is None:
                    # The process died while this exact call was running. Its side effects
                    # may or may not have happened, so never silently run it again.
                    return ToolResult.failure(
                        "an earlier attempt of this exact call was interrupted; its outcome is "
                        "unknown, so it was not re-run. Verify before retrying."
                    )
                return ToolResult(
                    ok=bool(existing["ok"]),
                    output=existing["output"] or "",
                    error=existing["error"],
                    data={"replayed": True},
                )

        if tool.outbound:
            leaked = _find_secret(json_dumps(action.args), ctx.secrets)
            if leaked:
                return ToolResult.failure("refused: arguments contain a configured secret value")

        started = time.monotonic()
        call_id = ctx.db.insert(
            "INSERT INTO tool_calls(ts, cycle_id, tool, args, risk_tier, idempotency_key) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                to_iso(now()),
                ctx.cycle_id,
                tool.name,
                json_dumps(action.args),
                int(tool.tier),
                idempotency_key,
            ),
        )
        ctx.events.append(
            "tool.start",
            action.describe(),
            {"tool": tool.name, "args": action.args, "tier": int(tool.tier), "call_id": call_id},
            actor="tools",
            cycle_id=ctx.cycle_id,
        )
        try:
            result = await asyncio.wait_for(tool.run(parsed, ctx), timeout=TOOL_TIMEOUT_S)
        except TimeoutError:
            result = ToolResult.failure(f"tool timed out after {TOOL_TIMEOUT_S}s")
        except asyncio.CancelledError:
            ctx.db.execute(
                "UPDATE tool_calls SET ok = 0, error = ? WHERE id = ?", ("cancelled", call_id)
            )
            raise
        except Exception as exc:
            log.exception("tool %s crashed", tool.name)
            result = ToolResult.failure(f"{type(exc).__name__}: {exc}")

        limit = self.registry.settings.tools.output_max_chars
        result = result.model_copy(
            update={
                "output": ctx.secrets.redact(truncate(result.output, limit)),
                "error": ctx.secrets.redact(result.error) if result.error else None,
            }
        )
        duration = int((time.monotonic() - started) * 1000)
        ctx.db.execute(
            "UPDATE tool_calls SET ok = ?, output = ?, error = ?, duration_ms = ? WHERE id = ?",
            (1 if result.ok else 0, result.output, result.error, duration, call_id),
        )
        ctx.budget.record(
            "tool",
            Cost(tool_calls=1, outbound=1 if (tool.outbound and result.ok) else 0),
            ctx.cycle_id,
        )
        ctx.events.append(
            "tool.result",
            f"{tool.name}: {result.summary(160)}",
            {
                "tool": tool.name,
                "ok": result.ok,
                "output": truncate(result.output, 4000),
                "error": result.error,
                "duration_ms": duration,
                "call_id": call_id,
                "data": result.data,
            },
            actor="tools",
            cycle_id=ctx.cycle_id,
        )
        return result


def _find_secret(text: str, secrets: Secrets) -> bool:
    return any(value in text for value in secrets.values_for_scrubbing())


def describe_result(result: ToolResult, max_chars: int = 3000) -> str:
    if result.ok:
        return truncate(result.output or "(no output)", max_chars)
    return one_line(f"FAILED: {result.error}", max_chars)
