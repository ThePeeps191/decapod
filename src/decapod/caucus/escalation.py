"""Deciding how hard to think: rule-based triage, optional LLM triage, loop detection."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from decapod.caucus.context import WorkingContext
from decapod.config.settings import CaucusSettings
from decapod.domain import Level

#: Percept kinds that are routine bookkeeping and do not by themselves need discussion.
ROUTINE_PERCEPTS = {"tool_result", "budget_ok", "resumed"}


@dataclass
class TriageDecision:
    level: Level
    reasons: list[str]


def rule_level(ctx: WorkingContext, settings: CaucusSettings) -> TriageDecision:
    reasons: list[str] = []
    level = Level.L0
    if not ctx.has_active_goals:
        level = max(level, Level.L2)
        reasons.append("no active goals")
    if settings.review_every and ctx.seq > 0 and ctx.seq % settings.review_every == 0:
        level = max(level, Level.L2)
        reasons.append(f"periodic agenda review (every {settings.review_every} cycles)")
    if ctx.loop_warning:
        level = max(level, Level.L2)
        reasons.append("possible loop detected")
    important = [p for p in ctx.percepts if p.importance >= 0.8]
    if important:
        level = max(level, Level.L2)
        reasons.append(f"{len(important)} important percept(s)")
    notable = [p for p in ctx.percepts if p.kind not in ROUTINE_PERCEPTS]
    if notable:
        level = max(level, Level.L1)
        reasons.append(f"{len(notable)} new percept(s)")
    if ctx.last_failed:
        level = max(level, Level.L1)
        reasons.append("the last action failed")
    if ctx.focus_task is None and ctx.has_active_goals:
        level = max(level, Level.L1)
        reasons.append("focused goal has no ready task")
    if not reasons:
        reasons.append("routine continuation of the current task")
    return TriageDecision(clamp(level, settings), reasons)


def clamp(level: Level, settings: CaucusSettings) -> Level:
    low, high = settings.min_level, settings.max_level
    low = min(low, high)
    return Level(max(int(low), min(int(high), int(level))))


TRIAGE_LEVELS = {
    "routine": Level.L0,
    "notable": Level.L1,
    "significant": Level.L2,
    "grave": Level.L3,
}


def detect_loop(history: list[tuple[str, bool]], window: int, threshold: int) -> str | None:
    """``history`` is (action fingerprint, ok) for recent cycles, newest last."""
    recent = [h for h in history[-window:] if h[0] != "wait"]
    if not recent:
        return None
    counts = Counter(fp for fp, _ in recent)
    fingerprint, count = counts.most_common(1)[0]
    if count >= threshold:
        failures = sum(1 for fp, ok in recent if fp == fingerprint and not ok)
        tool = fingerprint.split(":", 1)[0]
        detail = f", failing {failures} time(s)" if failures else ""
        return (
            f"The same action ({tool} with identical arguments) was chosen {count} times in "
            f"the last {len(recent)} non-wait cycles{detail}. Do something different: "
            "re-plan, try another approach, mark the task failed, or abandon the goal."
        )
    failures_by_tool = Counter(fp.split(":", 1)[0] for fp, ok in recent if not ok)
    if failures_by_tool:
        tool, fails = failures_by_tool.most_common(1)[0]
        if fails >= threshold:
            return (
                f"{tool} failed {fails} times recently. Stop retrying it blindly; diagnose "
                "the cause or change approach."
            )
    return None
