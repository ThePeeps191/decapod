"""Reflection: turn each cycle's outcome into memory, agenda progress and lessons."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from decapod import prompts
from decapod.agents import Roster
from decapod.caucus.context import WorkingContext
from decapod.caucus.schemas import Digest, Reflection
from decapod.domain import (
    Decision,
    GoalStatus,
    LLMError,
    MemoryKind,
    ProviderUnavailable,
    RiskTier,
    TaskStatus,
    ToolResult,
)
from decapod.llm import LLM, Message
from decapod.memory import AgendaError, AgendaStore, MemoryStore
from decapod.storage import EventLog
from decapod.util.text import one_line, truncate
from decapod.util.timeutil import now

log = logging.getLogger(__name__)

OutcomeStatus = Literal["executed", "failed", "approval_requested", "denied", "waited"]


@dataclass
class Outcome:
    status: OutcomeStatus
    detail: str
    result: ToolResult | None = None
    approval_id: int | None = None

    @property
    def ok(self) -> bool:
        return self.status in ("executed", "waited", "approval_requested")

    def describe(self, max_chars: int = 2500) -> str:
        if self.result is not None:
            body = (
                self.result.output
                if self.result.ok
                else f"ERROR: {self.result.error}\n" + self.result.output
            )
            return f"{self.status}: {truncate(body, max_chars)}"
        return f"{self.status}: {self.detail}"


class Reflector:
    def __init__(
        self, llm: LLM, roster: Roster, memory: MemoryStore, agenda: AgendaStore, events: EventLog
    ) -> None:
        self.llm = llm
        self.roster = roster
        self.memory = memory
        self.agenda = agenda
        self.events = events

    async def reflect(
        self, ctx: WorkingContext, decision: Decision, outcome: Outcome
    ) -> Reflection | None:
        goal_id = decision.chosen.goal_id if decision.chosen else None
        if decision.is_wait or outcome.status in ("waited", "approval_requested", "denied"):
            # Nothing happened in the world: record the episode cheaply, without a model call.
            summary = self._plain_summary(decision, outcome)
            self.memory.add(
                MemoryKind.EPISODIC,
                summary,
                tags=["cycle", outcome.status],
                importance=0.3 if outcome.status == "waited" else 0.5,
                goal_id=goal_id,
                cycle_id=ctx.cycle_id,
            )
            return None
        reflection: Reflection | None = None
        if self.roster.enabled("reflector"):
            reflection = await self._ask(ctx, decision, outcome)
        if reflection is None:
            self.memory.add(
                MemoryKind.EPISODIC,
                self._plain_summary(decision, outcome),
                tags=["cycle", outcome.status],
                importance=0.5,
                goal_id=goal_id,
                cycle_id=ctx.cycle_id,
            )
            return None
        self._apply(ctx, decision, outcome, reflection)
        return reflection

    @staticmethod
    def _plain_summary(decision: Decision, outcome: Outcome) -> str:
        if decision.is_wait:
            mins = f" {decision.wait_minutes:g} min" if decision.wait_minutes else ""
            return f"Waited{mins}: {one_line(decision.rationale, 240)}"
        return (
            f"{decision.describe()} → {outcome.status}: "
            f"{one_line(outcome.result.summary() if outcome.result else outcome.detail, 300)}"
        )

    async def _ask(
        self, ctx: WorkingContext, decision: Decision, outcome: Outcome
    ) -> Reflection | None:
        spec = self.roster.get("reflector")
        focus = ctx.focus_text
        prompt = prompts.render(
            "reflect",
            charter_text=ctx.charter.render(),
            cycle_id=ctx.cycle_id,
            focus=focus,
            decision=decision.describe()
            + (
                f" (goal G{decision.chosen.goal_id})"
                if decision.chosen and decision.chosen.goal_id
                else ""
            )
            + (
                f" (task T{decision.chosen.task_id})"
                if decision.chosen and decision.chosen.task_id
                else ""
            ),
            rationale=one_line(decision.rationale, 500),
            dissent="; ".join(decision.dissent),
            outcome=outcome.describe(),
        )
        system = prompts.render(
            "system",
            role_title=spec.title,
            brief=spec.brief,
            charter=ctx.charter,
            charter_text=ctx.charter.render(),
        )
        try:
            return await self.llm.structured(
                [Message.system(system), Message.user(prompt)],
                Reflection,
                tier=spec.tier,
                purpose="reflect",
                cycle_id=ctx.cycle_id,
                temperature=spec.temperature,
            )
        except ProviderUnavailable:
            raise
        except LLMError as exc:
            self.events.append(
                "reflect.error",
                one_line(str(exc), 200),
                {"error": str(exc)},
                actor="reflector",
                cycle_id=ctx.cycle_id,
            )
            return None

    def _apply(
        self, ctx: WorkingContext, decision: Decision, outcome: Outcome, reflection: Reflection
    ) -> None:
        cycle_id = ctx.cycle_id
        goal_id = decision.chosen.goal_id if decision.chosen else None
        tier = decision.chosen.risk_tier if decision.chosen else RiskTier.INTERNAL
        importance = 0.65 if not outcome.ok else 0.5
        if tier >= RiskTier.EXTERNAL_WRITE:
            importance = 0.75
        self.memory.add(
            MemoryKind.EPISODIC,
            reflection.summary or self._plain_summary(decision, outcome),
            tags=["cycle", outcome.status],
            importance=importance,
            goal_id=goal_id,
            cycle_id=cycle_id,
        )

        if reflection.task_update is not None:
            update = reflection.task_update
            task = self.agenda.get_task(update.task_id)
            if task is not None and task.status.value != update.status:
                try:
                    self.agenda.update_task(
                        update.task_id, TaskStatus(update.status), update.note, cycle_id=cycle_id
                    )
                except AgendaError as exc:
                    log.info("reflection task update ignored: %s", exc)
        if reflection.goal_update is not None:
            update_g = reflection.goal_update
            goal = self.agenda.get_goal(update_g.goal_id)
            if goal is not None:
                try:
                    self.agenda.update_goal(
                        update_g.goal_id,
                        status=GoalStatus(update_g.status),
                        progress_note=update_g.progress_note or None,
                        cycle_id=cycle_id,
                    )
                except AgendaError as exc:
                    log.info("reflection goal update ignored: %s", exc)
        for fact in reflection.facts:
            if fact.content.strip():
                self.memory.add(
                    MemoryKind.SEMANTIC,
                    fact.content,
                    tags=fact.tags,
                    confidence=fact.confidence,
                    importance=0.6,
                    goal_id=goal_id,
                    cycle_id=cycle_id,
                )
        for lesson in reflection.lessons:
            text = lesson.strip()
            if not text:
                continue
            similar = self.memory.search(text, kinds=[MemoryKind.REFLECTIVE], k=1, touch=False)
            if similar and _similar(similar[0].content, text):
                self.memory.adjust_usefulness(similar[0].id, 0.1)
                continue
            self.memory.add(
                MemoryKind.REFLECTIVE,
                text,
                tags=["lesson"],
                importance=0.7,
                usefulness=0.5,
                cycle_id=cycle_id,
            )
        for used in ctx.lessons:  # lessons in context during a success gain credibility
            self.memory.adjust_usefulness(used.id, 0.05 if outcome.ok else -0.05)
        if reflection.dissent_vindicated is not None and decision.dissent:
            self.events.append(
                "reflect.dissent",
                "dissent was " + ("right" if reflection.dissent_vindicated else "wrong"),
                {"vindicated": reflection.dissent_vindicated, "dissent": decision.dissent},
                actor="reflector",
                cycle_id=cycle_id,
            )
            if reflection.dissent_vindicated:
                self.memory.add(
                    MemoryKind.REFLECTIVE,
                    "The caucus overrode an objection that proved right: "
                    + one_line("; ".join(decision.dissent), 300),
                    tags=["lesson", "dissent"],
                    importance=0.7,
                    usefulness=0.6,
                    cycle_id=cycle_id,
                )

    async def compact(self, older_than: timedelta, cycle_id: str | None = None) -> int:
        """Merge old episodic memories into daily digests (cheap FREE-tier model)."""
        groups = self.memory.compaction_groups(now() - older_than)
        spec = self.roster.get("compactor")
        merged = 0
        for day, items in groups.items():
            listing = "\n".join(f"- {one_line(m.content, 300)}" for m in items[:80])
            try:
                result = await self.llm.structured(
                    [Message.user(prompts.render("compact", day=day, items=listing))],
                    Digest,
                    tier=spec.tier,
                    purpose="memory.compact",
                    cycle_id=cycle_id,
                    temperature=spec.temperature,
                )
                text = result.digest
            except ProviderUnavailable:
                raise
            except LLMError:
                text = f"{len(items)} episodes on {day}: " + " | ".join(
                    one_line(m.content, 80) for m in items[:12]
                )
            digest = self.memory.add(
                MemoryKind.EPISODIC,
                f"[digest {day}] {text}",
                tags=["digest"],
                importance=max(m.importance for m in items),
                cycle_id=cycle_id,
                created_at=items[-1].created_at,
            )
            self.memory.supersede([m.id for m in items], digest.id)
            merged += len(items)
        if merged:
            self.events.append(
                "memory.compacted",
                f"merged {merged} episodes into {len(groups)} digest(s)",
                {"days": list(groups)},
                actor="memory",
                cycle_id=cycle_id,
            )
        return merged


def _similar(a: str, b: str) -> bool:
    wa = set(a.lower().split())
    wb = set(b.lower().split())
    if not wa or not wb:
        return False
    return len(wa & wb) / len(wa | wb) >= 0.6
