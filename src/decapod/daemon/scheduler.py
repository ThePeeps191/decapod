"""Adaptive cadence: how long to sleep after each cycle."""

from __future__ import annotations

from dataclasses import dataclass

from decapod.config.settings import ScheduleSettings
from decapod.runtime.loop import CycleResult


@dataclass
class SleepPlan:
    seconds: float
    reason: str
    wake_on: tuple[str, ...] = ("operator", "approval")


class Scheduler:
    def __init__(self, settings: ScheduleSettings) -> None:
        self.settings = settings
        self.consecutive_errors = 0

    def _clamp(self, seconds: float) -> float:
        low = self.settings.min_interval.total_seconds()
        high = self.settings.max_interval.total_seconds()
        return max(low, min(high, seconds))

    def plan(self, result: CycleResult) -> SleepPlan:
        s = self.settings
        if result.kind in ("error", "provider_down"):
            self.consecutive_errors += 1
            base = s.error_backoff.total_seconds() * (2 ** min(self.consecutive_errors - 1, 8))
            if result.retry_after:
                base = max(base, result.retry_after)
            return SleepPlan(
                min(base, s.max_interval.total_seconds()),
                f"backing off after {result.kind} ({self.consecutive_errors} in a row)",
            )
        self.consecutive_errors = 0
        if result.kind == "budget":
            wait = result.retry_after or s.max_interval.total_seconds()
            # Sleep in bounded chunks so config changes and commands are picked up.
            return SleepPlan(
                min(wait, s.max_interval.total_seconds()),
                "budget exhausted; waiting for the window to reset",
            )
        if result.kind == "skipped":
            return SleepPlan(s.idle_interval.total_seconds(), "nothing to do")
        decision = result.decision
        outcome = result.outcome
        if decision is not None and decision.is_wait:
            wake = ("operator", "approval", *decision.wake_on)
            if decision.wait_minutes is not None:
                return SleepPlan(
                    self._clamp(decision.wait_minutes * 60),
                    f"chose to wait {decision.wait_minutes:g} min",
                    tuple(dict.fromkeys(wake)),
                )
            return SleepPlan(
                self._clamp(s.idle_interval.total_seconds()),
                "chose to wait",
                tuple(dict.fromkeys(wake)),
            )
        if outcome is not None and outcome.status in ("failed", "denied"):
            return SleepPlan(
                self._clamp(s.error_backoff.total_seconds()), f"last action {outcome.status}"
            )
        return SleepPlan(s.min_interval.total_seconds(), "keeping momentum")
