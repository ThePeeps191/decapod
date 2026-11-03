"""Hierarchical spending ceilings: per-cycle, hourly, daily and lifetime.

Every LLM request and tool call is *pre-authorized* against the remaining budget and
*settled* afterwards into the ``ledger`` table. Limits are enforced here, in plain
Python; no model output can raise them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from decapod.config.settings import BudgetLimits, BudgetSettings
from decapod.domain import BudgetExceeded, Cost
from decapod.storage import Database
from decapod.util.timeutil import now, to_iso

WINDOWS = ("cycle", "hourly", "daily", "lifetime")
METRICS = ("usd", "tokens", "llm_requests", "tool_calls", "outbound")

#: Which metrics a given kind of spend is checked against.
_LLM_METRICS = {"usd", "tokens", "llm_requests"}


@dataclass(frozen=True)
class WindowStatus:
    window: str
    metric: str
    used: float
    limit: float
    resets_at: datetime | None

    @property
    def fraction(self) -> float:
        if self.limit <= 0:
            return 1.0
        return self.used / self.limit

    @property
    def remaining(self) -> float:
        return max(0.0, self.limit - self.used)


def _metric(cost: Cost, metric: str) -> float:
    if metric == "tokens":
        return float(cost.tokens)
    return float(getattr(cost, metric))


class Budget:
    def __init__(
        self,
        db: Database,
        settings: BudgetSettings,
        clock: Callable[[], datetime] = now,
    ) -> None:
        self.db = db
        self.settings = settings
        self.clock = clock
        # In-flight spend that has been authorized but not settled yet: (cycle_id, cost).
        # Counting it closes the gap where concurrent calls all see the same headroom.
        self._reserved: dict[int, tuple[str | None, Cost]] = {}
        self._next_token = 0

    # ------------------------------------------------------------------ windows

    def _limits(self, window: str) -> BudgetLimits:
        return getattr(self.settings, window)

    def window_bounds(self, window: str) -> tuple[datetime | None, datetime | None]:
        """(start, resets_at) of a calendar window, in UTC. Uses the local day boundary."""
        current = self.clock()
        if window == "hourly":
            start = current.replace(minute=0, second=0, microsecond=0)
            return start, start + timedelta(hours=1)
        if window == "daily":
            local = current.astimezone()
            start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
            start = start_local.astimezone(current.tzinfo)
            return start, (start_local + timedelta(days=1)).astimezone(current.tzinfo)
        return None, None

    def usage(self, window: str, cycle_id: str | None = None) -> Cost:
        sql = (
            "SELECT COALESCE(SUM(usd),0), COALESCE(SUM(tokens_in),0), COALESCE(SUM(tokens_out),0),"
            " COALESCE(SUM(llm_requests),0), COALESCE(SUM(tool_calls),0),"
            " COALESCE(SUM(outbound),0) FROM ledger"
        )
        params: list[object] = []
        if window == "cycle":
            if cycle_id is None:
                return Cost()
            sql += " WHERE cycle_id = ?"
            params.append(cycle_id)
        elif window in ("hourly", "daily"):
            start, _ = self.window_bounds(window)
            assert start is not None
            sql += " WHERE ts >= ?"
            params.append(to_iso(start))
        row = self.db.query_one(sql, params)
        assert row is not None
        return Cost(
            usd=float(row[0]),
            tokens_in=int(row[1]),
            tokens_out=int(row[2]),
            llm_requests=int(row[3]),
            tool_calls=int(row[4]),
            outbound=int(row[5]),
        )

    # ------------------------------------------------------------------ gate

    def authorize(self, estimate: Cost, cycle_id: str | None) -> None:
        """Raise ``BudgetExceeded`` if spending ``estimate`` would cross any ceiling."""
        relevant: set[str] = set()
        if estimate.llm_requests:
            relevant |= _LLM_METRICS
        if estimate.tool_calls:
            relevant.add("tool_calls")
        if estimate.outbound:
            relevant.add("outbound")
        for window in WINDOWS:
            limits = self._limits(window).items()
            if not limits:
                continue
            if window == "cycle" and cycle_id is None:
                continue
            used = self.usage(window, cycle_id) + self._pending(window, cycle_id)
            for metric, limit in limits:
                if metric not in relevant:
                    continue
                spent = _metric(used, metric)
                extra = _metric(estimate, metric)
                over = spent + extra > limit if extra > 0 else spent >= limit
                if over:
                    _, resets = self.window_bounds(window)
                    raise BudgetExceeded(window, metric, spent, limit, resets)

    def _pending(self, window: str, cycle_id: str | None) -> Cost:
        total = Cost()
        for reserved_cycle, cost in self._reserved.values():
            if window != "cycle" or reserved_cycle == cycle_id:
                total = total + cost
        return total

    def reserve(self, estimate: Cost, cycle_id: str | None) -> int:
        """Authorize ``estimate`` and hold it until ``release()``; returns a token."""
        self.authorize(estimate, cycle_id)
        self._next_token += 1
        self._reserved[self._next_token] = (cycle_id, estimate)
        return self._next_token

    def release(self, token: int) -> None:
        self._reserved.pop(token, None)

    def record(self, kind: str, cost: Cost, cycle_id: str | None) -> None:
        if cost == Cost():
            return
        self.db.insert(
            "INSERT INTO ledger(ts, cycle_id, kind, usd, tokens_in, tokens_out, llm_requests,"
            " tool_calls, outbound) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                to_iso(self.clock()),
                cycle_id,
                kind,
                cost.usd,
                cost.tokens_in,
                cost.tokens_out,
                cost.llm_requests,
                cost.tool_calls,
                cost.outbound,
            ),
        )

    # ------------------------------------------------------------------ reporting

    def status(self, cycle_id: str | None = None) -> list[WindowStatus]:
        out: list[WindowStatus] = []
        for window in WINDOWS:
            limits = self._limits(window).items()
            if not limits or (window == "cycle" and cycle_id is None):
                continue
            used = self.usage(window, cycle_id)
            _, resets = self.window_bounds(window)
            for metric, limit in limits:
                out.append(WindowStatus(window, metric, _metric(used, metric), limit, resets))
        return out

    def warnings(self) -> list[WindowStatus]:
        return [s for s in self.status() if s.fraction >= self.settings.warn_fraction]

    def exhausted(self) -> BudgetExceeded | None:
        """The most severe exhausted non-cycle window, if any (lifetime first)."""
        for s in sorted(self.status(), key=lambda s: WINDOWS.index(s.window), reverse=True):
            if s.used >= s.limit and s.metric in _LLM_METRICS:
                return BudgetExceeded(s.window, s.metric, s.used, s.limit, s.resets_at)
        return None

    def spent_in_cycle(self, cycle_id: str) -> Cost:
        return self.usage("cycle", cycle_id)
