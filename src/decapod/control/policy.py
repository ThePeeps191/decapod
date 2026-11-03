"""The policy engine: the gate between a Decision and the outside world.

Pure Python. It does not care how convinced the model was; it only checks facts:
run state, charter scopes, prohibition patterns, rate limits, budgets and risk-tier
policy. Every verdict is logged with the list of checks that ran.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Literal

from decapod.config.charter import Charter
from decapod.config.settings import PolicyMode, Settings
from decapod.control.budget import Budget
from decapod.domain import ActionSpec, BudgetExceeded, Cost, RiskTier, RunState, SideEffect
from decapod.storage import Database
from decapod.util.text import json_dumps
from decapod.util.timeutil import now, to_iso

Outcome = Literal["allow", "approve", "deny"]
_MODE_RANK: dict[PolicyMode, int] = {"auto": 0, "approval": 1, "deny": 2}


@dataclass(frozen=True)
class ToolCheck:
    """What the policy engine needs to know about a tool invocation."""

    name: str
    tier: RiskTier
    side_effect: SideEffect
    scopes: list[str]
    args_error: str | None = None
    outbound: bool = False


@dataclass
class Verdict:
    outcome: Outcome
    reason: str
    checks: list[str] = field(default_factory=list)
    transient: bool = False  # e.g. rate limit or budget: may succeed later

    @property
    def allowed(self) -> bool:
        return self.outcome == "allow"


class PolicyEngine:
    def __init__(self, settings: Settings, charter: Charter, budget: Budget, db: Database) -> None:
        self.settings = settings
        self.charter = charter
        self.budget = budget
        self.db = db
        self.consecutive_denials = 0

    def update_charter(self, charter: Charter) -> None:
        self.charter = charter

    def scope_granted(self, scope: str) -> bool:
        return any(fnmatch.fnmatchcase(scope, pattern) for pattern in self.charter.scopes.granted)

    def tool_disabled(self, name: str) -> bool:
        return any(fnmatch.fnmatchcase(name, p) for p in self.settings.tools.disabled)

    def mode_for(self, check: ToolCheck) -> PolicyMode:
        """Tier mode, overridden by the most restrictive matching scope override."""
        overrides = [
            mode
            for scope in check.scopes
            for pattern, mode in self.settings.policy.scope_modes.items()
            if fnmatch.fnmatchcase(scope, pattern)
        ]
        if overrides:
            return max(overrides, key=lambda m: _MODE_RANK[m])
        return self.settings.policy.mode_for(check.tier)

    def review(
        self,
        action: ActionSpec,
        tool: ToolCheck | None,
        *,
        cycle_id: str | None,
        run_state: RunState = RunState.RUNNING,
        preapproved: bool = False,
    ) -> Verdict:
        verdict = self._review(action, tool, cycle_id, run_state, preapproved)
        if verdict.outcome == "deny" and not verdict.transient:
            self.consecutive_denials += 1
        elif verdict.outcome == "allow":
            self.consecutive_denials = 0
        return verdict

    def _review(
        self,
        action: ActionSpec,
        tool: ToolCheck | None,
        cycle_id: str | None,
        run_state: RunState,
        preapproved: bool,
    ) -> Verdict:
        checks: list[str] = []

        def deny(reason: str, *, transient: bool = False) -> Verdict:
            return Verdict("deny", reason, checks, transient=transient)

        checks.append("run_state")
        if run_state in (RunState.STOPPING, RunState.STOPPED, RunState.KILLED):
            return deny(f"instance is {run_state.value}", transient=True)
        if action.kind == "wait":
            return Verdict("allow", "waiting is always allowed", checks)

        checks.append("tool_known")
        if tool is None:
            return deny(f"unknown tool {action.tool!r}")
        checks.append("tool_enabled")
        if self.tool_disabled(tool.name):
            return deny(f"tool {tool.name!r} is disabled by the operator")
        checks.append("arguments")
        if tool.args_error:
            return deny(f"invalid arguments for {tool.name}: {tool.args_error}")

        checks.append("charter_scopes")
        missing = [s for s in tool.scopes if not self.scope_granted(s)]
        if missing:
            return deny(f"charter does not grant scope(s): {', '.join(missing)}")

        if tool.side_effect in (SideEffect.EXTERNAL_WRITE, SideEffect.IRREVERSIBLE) or (
            tool.tier >= RiskTier.EXTERNAL_WRITE
        ):
            checks.append("charter_prohibitions")
            serialized = json_dumps(action.args)
            for pattern in self.charter.compiled_patterns():
                if pattern.search(serialized):
                    return deny(f"arguments match charter prohibition pattern {pattern.pattern!r}")

        checks.append("rate_limit")
        for tool_glob, limit in self.settings.policy.rate_limits.items():
            if fnmatch.fnmatchcase(tool.name, tool_glob):
                since = to_iso(now() - timedelta(hours=1))
                used = self.db.scalar(
                    "SELECT COUNT(*) FROM tool_calls WHERE tool = ? AND ts >= ?",
                    (tool.name, since),
                    0,
                )
                if used >= limit.per_hour:
                    return deny(
                        f"rate limit for {tool.name}: {used}/{limit.per_hour} per hour",
                        transient=True,
                    )

        checks.append("budget")
        try:
            self.budget.authorize(Cost(tool_calls=1, outbound=1 if tool.outbound else 0), cycle_id)
        except BudgetExceeded as exc:
            return deny(str(exc), transient=True)

        checks.append("risk_mode")
        mode = self.mode_for(tool)
        if mode == "deny":
            return deny(f"{tool.tier.label} actions are set to 'deny' by the operator")
        if mode == "approval" and not preapproved:
            return Verdict(
                "approve",
                f"{tool.tier.label} action (tier {int(tool.tier)}) requires operator approval",
                checks,
            )
        return Verdict(
            "allow", "all checks passed" + (" (operator-approved)" if preapproved else ""), checks
        )
