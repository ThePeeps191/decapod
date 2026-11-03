"""Pydantic models that flow between subsystems.

These are deliberately plain data: no behaviour beyond validation and small helpers.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from decapod.domain.enums import (
    ApprovalStatus,
    GoalStatus,
    Level,
    MemoryKind,
    RiskTier,
    TaskStatus,
)
from decapod.util.timeutil import now


class Model(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


# --------------------------------------------------------------------------- actions


class ActionSpec(Model):
    """What Decapod wants to do: run one tool, or wait."""

    kind: Literal["tool", "wait"] = "tool"
    tool: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    wait_minutes: float | None = None
    wake_on: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> ActionSpec:
        if self.kind == "tool":
            if not self.tool:
                raise ValueError("a tool action needs a 'tool' name")
        else:
            self.tool = None
            self.args = {}
            if self.wait_minutes is not None and self.wait_minutes < 0:
                raise ValueError("wait_minutes must be >= 0")
        return self

    @classmethod
    def wait(cls, minutes: float | None = None, wake_on: list[str] | None = None) -> ActionSpec:
        return cls(kind="wait", wait_minutes=minutes, wake_on=wake_on or [])

    def describe(self) -> str:
        if self.kind == "wait":
            mins = f" {self.wait_minutes:g}m" if self.wait_minutes is not None else ""
            return f"wait{mins}"
        from decapod.util.text import json_dumps, one_line

        return f"{self.tool}({one_line(json_dumps(self.args), 120)})"

    def fingerprint(self) -> str:
        """Stable identity for loop detection."""
        from decapod.util.text import json_dumps

        if self.kind == "wait":
            return "wait"
        return f"{self.tool}:{json_dumps(dict(sorted(self.args.items())))}"


class Cost(Model):
    usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    llm_requests: int = 0
    tool_calls: int = 0
    outbound: int = 0

    @property
    def tokens(self) -> int:
        return self.tokens_in + self.tokens_out

    def __add__(self, other: Cost) -> Cost:
        return Cost(
            usd=self.usd + other.usd,
            tokens_in=self.tokens_in + other.tokens_in,
            tokens_out=self.tokens_out + other.tokens_out,
            llm_requests=self.llm_requests + other.llm_requests,
            tool_calls=self.tool_calls + other.tool_calls,
            outbound=self.outbound + other.outbound,
        )


class ToolResult(Model):
    ok: bool
    output: str = ""
    data: dict[str, Any] | None = None
    error: str | None = None

    @classmethod
    def success(cls, output: str, data: dict[str, Any] | None = None) -> ToolResult:
        return cls(ok=True, output=output, data=data)

    @classmethod
    def failure(cls, error: str, output: str = "") -> ToolResult:
        return cls(ok=False, output=output, error=error)

    def summary(self, max_chars: int = 200) -> str:
        from decapod.util.text import one_line

        if self.ok:
            return one_line(self.output or "ok", max_chars)
        return one_line(f"error: {self.error}", max_chars)


# --------------------------------------------------------------------------- agenda


class Goal(Model):
    id: int
    title: str
    description: str = ""
    success_criteria: list[str] = Field(default_factory=list)
    priority: float = 0.5
    status: GoalStatus = GoalStatus.ACTIVE
    parent_id: int | None = None
    rationale: str = ""
    charter_refs: list[str] = Field(default_factory=list)
    progress_note: str = ""
    #: Recurring goals reopen this many seconds after each completion.
    every_seconds: float | None = None
    #: Optional deadline.
    due_at: datetime | None = None
    #: When a completed recurring goal reopens.
    next_due: datetime | None = None
    completions: int = 0
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)

    @property
    def recurring(self) -> bool:
        return bool(self.every_seconds)

    @field_validator("priority")
    @classmethod
    def _clamp(cls, value: float) -> float:
        return max(0.0, min(1.0, float(value)))

    @property
    def ref(self) -> str:
        return f"G{self.id}"


class Task(Model):
    id: int
    goal_id: int
    title: str
    description: str = ""
    status: TaskStatus = TaskStatus.TODO
    depends_on: list[int] = Field(default_factory=list)
    order: int = 0
    attempts: int = 0
    note: str = ""
    plan_version: int = 1
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)

    @property
    def ref(self) -> str:
        return f"T{self.id}"


# --------------------------------------------------------------------------- caucus


class Proposal(Model):
    id: str  # "P1", "P2", … unique within a deliberation
    author_role: str
    action: ActionSpec
    goal_id: int | None = None
    task_id: int | None = None
    rationale: str = ""
    expected_outcome: str = ""
    confidence: float = 0.5
    risk_tier: RiskTier = RiskTier.INTERNAL
    withdrawn: bool = False
    revision: int = 0

    @field_validator("confidence")
    @classmethod
    def _clamp(cls, value: float) -> float:
        return max(0.0, min(1.0, float(value)))


class Critique(Model):
    proposal_id: str
    author_role: str
    verdict: Literal["support", "amend", "oppose"]
    concerns: list[str] = Field(default_factory=list)
    amendment: str | None = None
    charter_violation: str | None = None
    score: int = 3

    @field_validator("score")
    @classmethod
    def _clamp(cls, value: int) -> int:
        return max(1, min(5, int(value)))


class ChamberReport(Model):
    chamber: str
    top: list[str] = Field(default_factory=list)
    consensus: str = ""
    key_risks: list[str] = Field(default_factory=list)
    dissent: list[str] = Field(default_factory=list)


class Decision(Model):
    cycle_id: str
    level: Level
    chosen: Proposal | None = None
    rationale: str = ""
    dissent: list[str] = Field(default_factory=list)
    ranking: list[str] = Field(default_factory=list)
    wait_minutes: float | None = None
    wake_on: list[str] = Field(default_factory=list)

    @property
    def is_wait(self) -> bool:
        return self.chosen is None or self.chosen.action.kind == "wait"

    @property
    def action(self) -> ActionSpec:
        if self.chosen is None:
            return ActionSpec.wait(self.wait_minutes, self.wake_on)
        return self.chosen.action

    def describe(self) -> str:
        return self.action.describe()


# --------------------------------------------------------------------------- runtime


class Percept(Model):
    source: str  # "control", "approval", "tool", "budget", "sensor:email", "operator", ...
    kind: str
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)
    importance: float = 0.5
    untrusted: bool = False  # content came from the outside world


class Event(Model):
    id: int = 0
    ts: datetime = Field(default_factory=now)
    cycle_id: str | None = None
    kind: str
    actor: str = "system"
    summary: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


class Memory(Model):
    id: int = 0
    kind: MemoryKind
    content: str
    tags: list[str] = Field(default_factory=list)
    importance: float = 0.5
    confidence: float = 1.0
    usefulness: float = 0.5
    goal_id: int | None = None
    cycle_id: str | None = None
    source_event_id: int | None = None
    created_at: datetime = Field(default_factory=now)
    superseded_by: int | None = None
    score: float = 0.0  # filled in by retrieval


class Approval(Model):
    id: int
    created_at: datetime
    expires_at: datetime
    cycle_id: str | None
    action: ActionSpec
    proposal: dict[str, Any] = Field(default_factory=dict)
    reason: str
    status: ApprovalStatus = ApprovalStatus.PENDING
    resolved_at: datetime | None = None
    resolved_by: str | None = None
    note: str = ""
    result: dict[str, Any] | None = None
