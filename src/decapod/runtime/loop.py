"""The cognitive loop. One cycle = perceive → triage → deliberate → act → reflect."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Literal

from decapod.caucus import WorkingContext, detect_loop
from decapod.domain import (
    ActionSpec,
    BudgetExceeded,
    CycleStatus,
    Decision,
    Halted,
    MemoryKind,
    Percept,
    ProviderUnavailable,
    ToolResult,
)
from decapod.executive import Outcome, planner_notes
from decapod.runtime.instance import Instance
from decapod.runtime.perceive import perceive
from decapod.storage import loads
from decapod.util.text import json_dumps, one_line
from decapod.util.timeutil import format_duration, now, to_iso

log = logging.getLogger(__name__)

CycleKind = Literal["completed", "budget", "provider_down", "error", "halted", "skipped"]


@dataclass
class CycleResult:
    cycle_id: str | None
    kind: CycleKind
    decision: Decision | None = None
    outcome: Outcome | None = None
    retry_after: float | None = None  # seconds, for budget/provider waits
    error: str | None = None
    lifetime_exhausted: bool = False
    extra: dict[str, object] = field(default_factory=dict)


class Runtime:
    def __init__(self, instance: Instance) -> None:
        self.instance = instance

    # ------------------------------------------------------------------ cycle

    async def run_cycle(self) -> CycleResult:
        inst = self.instance
        exhausted = inst.budget.exhausted()
        if exhausted is not None:
            return self._budget_result(None, exhausted)

        seq = int(inst.db.scalar("SELECT COALESCE(MAX(seq), 0) + 1 FROM cycles", (), 1))
        cycle_id = f"c{seq:05d}"
        inst.reload_charter_if_changed()
        inst.db.insert(
            "INSERT INTO cycles(seq, id, started_at, status, phase, charter_sha) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                seq,
                cycle_id,
                to_iso(now()),
                CycleStatus.RUNNING.value,
                "perceive",
                inst.charter.sha256,
            ),
        )
        inst.db.set_states({"current_cycle": cycle_id, "phase": "perceive"})
        inst.events.append(
            "cycle.start",
            f"cycle {cycle_id}",
            {"seq": seq, "charter_sha": inst.charter.sha256},
            cycle_id=cycle_id,
        )
        try:
            result = await self._cycle(seq, cycle_id)
        except Halted as exc:
            self._finish(cycle_id, CycleStatus.HALTED, error=exc.reason)
            raise
        except asyncio.CancelledError:
            self._finish(cycle_id, CycleStatus.HALTED, error="cancelled (kill)")
            raise
        except BudgetExceeded as exc:
            self._finish(cycle_id, CycleStatus.COMPLETED, error=str(exc))
            return self._budget_result(cycle_id, exc)
        except ProviderUnavailable as exc:
            self._finish(cycle_id, CycleStatus.FAILED, error=str(exc))
            inst.events.append(
                "cycle.error",
                f"model provider unavailable: {exc}",
                {"error": str(exc)},
                cycle_id=cycle_id,
            )
            return CycleResult(
                cycle_id,
                "provider_down",
                error=str(exc),
                retry_after=max(inst.llm.breaker.remaining(), 30.0),
            )
        except Exception as exc:
            log.exception("cycle %s failed", cycle_id)
            self._finish(cycle_id, CycleStatus.FAILED, error=f"{type(exc).__name__}: {exc}")
            inst.events.append(
                "cycle.error",
                f"{type(exc).__name__}: {one_line(str(exc), 200)}",
                {"error": str(exc), "type": type(exc).__name__},
                cycle_id=cycle_id,
            )
            return CycleResult(cycle_id, "error", error=str(exc))
        return result

    async def _cycle(self, seq: int, cycle_id: str) -> CycleResult:
        inst = self.instance
        ctl = inst.controller

        await self._phase(cycle_id, "perceive")
        await self.execute_approved(cycle_id)
        tool_ctx = inst.tool_context(cycle_id)
        percepts = await perceive(inst, tool_ctx)
        for p in percepts:
            inst.events.append(
                "percept", p.summary, p.model_dump(mode="json"), actor=p.source, cycle_id=cycle_id
            )

        context = await self.build_context(seq, cycle_id, percepts)
        inst.events.append(
            "cycle.context",
            f"focus: {one_line(context.focus_text, 160)}",
            {"context": context.to_payload()},
            cycle_id=cycle_id,
        )

        await self._phase(cycle_id, "triage")
        triage = await inst.caucus.triage(context)
        inst.db.execute("UPDATE cycles SET level = ? WHERE id = ?", (int(triage.level), cycle_id))

        await self._phase(cycle_id, "deliberate")
        decision = await inst.caucus.deliberate(context, triage.level, triage.reasons)
        inst.db.execute(
            "UPDATE cycles SET level = ?, decision = ? WHERE id = ?",
            (int(decision.level), json_dumps(decision.model_dump(mode="json")), cycle_id),
        )

        await self._phase(cycle_id, "act")
        outcome = await self.act(cycle_id, decision)
        # Persist the outcome now: if the cycle is stopped or fails during reflection, the
        # record must still say the action happened, so it is not repeated blindly.
        inst.db.execute(
            "UPDATE cycles SET result = ? WHERE id = ?",
            (json_dumps(_outcome_record(outcome)), cycle_id),
        )

        await self._phase(cycle_id, "reflect")
        await inst.reflector.reflect(context, decision, outcome)

        compact_every = inst.settings.memory.compact_every
        if compact_every and seq % compact_every == 0:
            await inst.reflector.compact(inst.settings.memory.compact_after, cycle_id)

        self._finish(cycle_id, CycleStatus.COMPLETED, decision=decision, outcome=outcome)
        await ctl.checkpoint("idle")
        return CycleResult(cycle_id, "completed", decision=decision, outcome=outcome)

    async def _phase(self, cycle_id: str, phase: str) -> None:
        self.instance.db.execute("UPDATE cycles SET phase = ? WHERE id = ?", (phase, cycle_id))
        await self.instance.controller.checkpoint(phase)

    def _finish(
        self,
        cycle_id: str,
        status: CycleStatus,
        *,
        decision: Decision | None = None,
        outcome: Outcome | None = None,
        error: str | None = None,
    ) -> None:
        inst = self.instance
        cost = inst.budget.spent_in_cycle(cycle_id)
        result: dict[str, object] | None = None
        if outcome is not None:
            result = _outcome_record(outcome)
        else:  # keep an outcome that was persisted right after acting
            row = inst.db.query_one("SELECT result FROM cycles WHERE id = ?", (cycle_id,))
            result = loads(row["result"], None) if row else None
        inst.db.execute(
            "UPDATE cycles SET status = ?, ended_at = ?, result = ?, cost = ?, error = ?, "
            "phase = ? WHERE id = ?",
            (
                status.value,
                to_iso(now()),
                json_dumps(result) if result else None,
                json_dumps(cost.model_dump()),
                error,
                "done",
                cycle_id,
            ),
        )
        summary = f"{cycle_id} {status.value}"
        if decision is not None:
            summary += f": {decision.describe()}"
        if outcome is not None:
            summary += f" → {outcome.status}"
        if error:
            summary += f" ({one_line(error, 120)})"
        inst.events.append(
            "cycle.end",
            summary,
            {"status": status.value, "cost": cost.model_dump(), "error": error, "outcome": result},
            cycle_id=cycle_id,
        )
        inst.db.set_states(
            {
                "current_cycle": None,
                "phase": "idle",
                "last_cycle": cycle_id,
                "last_cycle_status": status.value,
            }
        )

    def _budget_result(self, cycle_id: str | None, exc: BudgetExceeded) -> CycleResult:
        inst = self.instance
        inst.events.append(
            "budget.exhausted",
            str(exc),
            {
                "window": exc.window,
                "metric": exc.metric,
                "used": exc.used,
                "limit": exc.limit,
                "resets_at": to_iso(exc.resets_at) if exc.resets_at else None,
            },
            actor="control",
            cycle_id=cycle_id,
        )
        retry = None
        if exc.resets_at is not None:
            retry = max(1.0, (exc.resets_at - now()).total_seconds())
        return CycleResult(
            cycle_id,
            "budget",
            error=str(exc),
            retry_after=retry,
            lifetime_exhausted=exc.window == "lifetime",
        )

    # ------------------------------------------------------------------ context

    async def build_context(
        self, seq: int, cycle_id: str, percepts: list[Percept]
    ) -> WorkingContext:
        inst = self.instance
        goal, task = inst.agenda.focus()
        query = (
            " ".join(
                filter(
                    None,
                    [
                        goal.title if goal else "",
                        task.title if task else "",
                        task.description if task else "",
                        *[p.summary for p in percepts[:5]],
                    ],
                )
            )
            or inst.charter.identity.mission
        )
        embedding = None
        if inst.settings.llm.embeddings_model:
            vectors = await inst.llm.embed([query])
            embedding = vectors[0] if vectors else None
        memories = inst.memory.search(
            query,
            kinds=[MemoryKind.EPISODIC, MemoryKind.SEMANTIC],
            goal_id=goal.id if goal else None,
            query_embedding=embedding,
        )
        lessons = inst.memory.lessons(query)
        history = self.history(inst.settings.caucus.loop_window)
        loop_warning = detect_loop(
            history, inst.settings.caucus.loop_window, inst.settings.caucus.loop_threshold
        )
        if loop_warning:
            inst.events.append(
                "caucus.loop_warning", loop_warning, {}, actor="historian", cycle_id=cycle_id
            )
        last = inst.db.query_one(
            "SELECT result FROM cycles WHERE status = ? ORDER BY seq DESC LIMIT 1",
            (CycleStatus.COMPLETED.value,),
        )
        last_result = loads(last["result"], {}) if last else {}
        last_failed = bool(last_result) and last_result.get("status") in ("failed", "denied")
        return WorkingContext(
            cycle_id=cycle_id,
            seq=seq,
            now=now(),
            charter=inst.charter,
            agenda_text=inst.agenda.render(),
            focus_goal=goal,
            focus_task=task,
            percepts=percepts,
            recent_text=self.recent_text(),
            memories=memories,
            lessons=lessons,
            budget_text=self.budget_text(),
            planner_notes=planner_notes(inst.agenda),
            catalog=inst.registry.catalog(granted=inst.policy.scope_granted),
            loop_warning=loop_warning,
            last_failed=last_failed,
            has_active_goals=bool(inst.agenda.active_goals()),
            token_budget=inst.settings.memory.context_tokens,
        )

    def history(self, limit: int) -> list[tuple[str, bool]]:
        rows = self.instance.db.query(
            "SELECT decision, result FROM cycles WHERE status = ? AND decision IS NOT NULL "
            "ORDER BY seq DESC LIMIT ?",
            (CycleStatus.COMPLETED.value, limit),
        )
        out: list[tuple[str, bool]] = []
        for row in reversed(rows):
            decision = loads(row["decision"], {})
            chosen = decision.get("chosen")
            action = ActionSpec.model_validate(chosen["action"]) if chosen else ActionSpec.wait()
            result = loads(row["result"], {}) or {}
            out.append((action.fingerprint(), bool(result.get("ok", True))))
        return out

    def recent_text(self, limit: int = 6) -> str:
        rows = self.instance.db.query(
            "SELECT id, ended_at, status, level, decision, result, error FROM cycles "
            "WHERE status != ? ORDER BY seq DESC LIMIT ?",
            (CycleStatus.RUNNING.value, limit),
        )
        if not rows:
            return "This is your first cycle."
        lines = []
        for row in reversed(rows):
            decision = loads(row["decision"], {}) or {}
            chosen = decision.get("chosen")
            if chosen:
                act = ActionSpec.model_validate(chosen["action"]).describe()
            elif decision:
                act = "wait"
            else:
                act = "(no decision)"
            result = loads(row["result"], {}) or {}
            detail = result.get("detail") or ""
            if result.get("result"):
                r = result["result"]
                detail = (r.get("output") if r.get("ok") else f"error: {r.get('error')}") or detail
            level = f"L{row['level']}" if row["level"] is not None else "-"
            status = result.get("status") or row["status"]
            if row["error"] and not result:
                detail = row["error"]
            lines.append(f"- {row['id']} [{level}] {act} → {status}: {one_line(str(detail), 220)}")
        return "\n".join(lines)

    def budget_text(self) -> str:
        parts = []
        for s in self.instance.budget.status():
            resets = f", resets in {format_duration(s.resets_at - now())}" if s.resets_at else ""
            parts.append(
                f"{s.window} {s.metric}: {s.used:g}/{s.limit:g} ({s.fraction:.0%}{resets})"
            )
        return "\n".join(parts) or "no limits configured"

    # ------------------------------------------------------------------ acting

    async def act(self, cycle_id: str, decision: Decision) -> Outcome:
        inst = self.instance
        if decision.is_wait:
            mins = decision.wait_minutes
            return Outcome(
                "waited",
                f"waiting{f' {mins:g} min' if mins else ''}: {one_line(decision.rationale, 200)}",
            )
        action = decision.action
        ctx = inst.tool_context(cycle_id)
        _tool, check = inst.registry.check(action, ctx)
        verdict = inst.policy.review(
            action, check, cycle_id=cycle_id, run_state=inst.controller.state
        )
        inst.events.append(
            "policy.verdict",
            f"{verdict.outcome}: {verdict.reason}",
            {
                "outcome": verdict.outcome,
                "reason": verdict.reason,
                "checks": verdict.checks,
                "action": action.model_dump(mode="json"),
            },
            actor="policy",
            cycle_id=cycle_id,
        )
        threshold = inst.settings.policy.consecutive_denials_kill
        if threshold and inst.policy.consecutive_denials >= threshold:
            inst.controller.trip(f"{inst.policy.consecutive_denials} consecutive policy denials")
        if verdict.outcome == "deny":
            return Outcome("denied", verdict.reason)
        if verdict.outcome == "approve":
            proposal = decision.chosen.model_dump(mode="json") if decision.chosen else {}
            approval = inst.approvals.enqueue(
                action,
                {**proposal, "rationale": decision.rationale, "dissent": decision.dissent},
                verdict.reason,
                cycle_id,
            )
            return Outcome(
                "approval_requested",
                f"queued as approval #{approval.id}: {verdict.reason}",
                approval_id=approval.id,
            )
        result = await inst.executor.execute(
            action, ctx, idempotency_key=f"{cycle_id}:{action.fingerprint()}"
        )
        return Outcome("executed" if result.ok else "failed", result.summary(300), result=result)

    async def execute_approved(self, cycle_id: str) -> int:
        """Run actions the operator approved since the last cycle (re-checked by policy).

        Their outcomes reach the caucus as percepts via ``approvals.unreported()``.
        """
        inst = self.instance
        executed = 0
        for approval in inst.approvals.ready_to_execute():
            ctx = inst.tool_context(cycle_id)
            _tool, check = inst.registry.check(approval.action, ctx)
            verdict = inst.policy.review(
                approval.action,
                check,
                cycle_id=cycle_id,
                run_state=inst.controller.state,
                preapproved=True,
            )
            inst.events.append(
                "policy.verdict",
                f"approved #{approval.id}: {verdict.outcome}: {verdict.reason}",
                {
                    "outcome": verdict.outcome,
                    "reason": verdict.reason,
                    "checks": verdict.checks,
                    "approval_id": approval.id,
                },
                actor="policy",
                cycle_id=cycle_id,
            )
            if verdict.outcome != "allow" and verdict.transient:
                # Rate limit, budget or shutdown: keep it approved and try again next cycle.
                break
            if verdict.outcome == "allow":
                result = await inst.executor.execute(
                    approval.action, ctx, idempotency_key=f"approval:{approval.id}"
                )
            else:
                result = ToolResult.failure(f"blocked at execution time: {verdict.reason}")
            inst.approvals.mark_executed(approval.id, result)
            executed += 1
            await inst.controller.checkpoint("perceive")
        return executed


def _outcome_record(outcome: Outcome) -> dict[str, object]:
    return {
        "status": outcome.status,
        "detail": outcome.detail,
        "ok": outcome.ok,
        "approval_id": outcome.approval_id,
        "result": outcome.result.model_dump(mode="json") if outcome.result else None,
    }
