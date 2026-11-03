"""Structured outputs requested from caucus agents."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _Out(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ProposalDraft(_Out):
    tool: str = Field(description="tool name from the catalog, or 'wait'")
    args: dict[str, Any] = Field(default_factory=dict)
    goal_id: int | None = None
    task_id: int | None = None
    rationale: str = ""
    expected_outcome: str = ""
    confidence: float = 0.5
    wait_minutes: float | None = None

    @field_validator("confidence")
    @classmethod
    def _clamp(cls, value: float) -> float:
        return max(0.0, min(1.0, float(value)))

    @field_validator("wait_minutes")
    @classmethod
    def _wait(cls, value: float | None) -> float | None:
        return None if value is None else max(0.0, float(value))

    @field_validator("args", mode="before")
    @classmethod
    def _args(cls, value: Any) -> Any:
        return value if isinstance(value, dict) else {}


class ProposalSet(_Out):
    proposals: list[ProposalDraft] = Field(default_factory=list, max_length=5)


class CritiqueDraft(_Out):
    proposal_id: str
    verdict: Literal["support", "amend", "oppose"]
    concerns: list[str] = Field(default_factory=list)
    amendment: str | None = None
    charter_violation: str | None = None
    score: int = 3

    @field_validator("score", mode="before")
    @classmethod
    def _score(cls, value: Any) -> int:
        try:
            return max(1, min(5, round(float(value))))
        except (TypeError, ValueError):
            return 3

    @field_validator("concerns", mode="before")
    @classmethod
    def _concerns(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [value] if value else []
        return value


class CritiqueSet(_Out):
    critiques: list[CritiqueDraft] = Field(default_factory=list)


class Revision(_Out):
    proposal_id: str
    action: Literal["keep", "revise", "withdraw"]
    revised: ProposalDraft | None = None
    note: str = ""


class RevisionSet(_Out):
    revisions: list[Revision] = Field(default_factory=list)


class Score(_Out):
    proposal_id: str
    score: int

    @field_validator("score", mode="before")
    @classmethod
    def _score(cls, value: Any) -> int:
        try:
            return max(1, min(5, round(float(value))))
        except (TypeError, ValueError):
            return 3


class RankSet(_Out):
    scores: list[Score] = Field(default_factory=list)


class ManagerReport(_Out):
    top: list[str] = Field(default_factory=list, max_length=5)
    consensus: str = ""
    key_risks: list[str] = Field(default_factory=list)
    dissent: list[str] = Field(default_factory=list)


class ExecutiveChoice(_Out):
    choice: str = Field(description="a proposal id such as 'P2', or 'wait'")
    rationale: str = ""
    wait_minutes: float | None = None
    wake_on: list[str] = Field(default_factory=list)

    @field_validator("wait_minutes")
    @classmethod
    def _wait(cls, value: float | None) -> float | None:
        return None if value is None else max(0.0, float(value))


class Confirmation(_Out):
    approve: bool
    reason: str = ""


class TriageResult(_Out):
    level: Literal["routine", "notable", "significant", "grave"]
    reason: str = ""


class TaskUpdate(_Out):
    task_id: int
    status: Literal["todo", "in_progress", "done", "failed", "skipped"]
    note: str = ""


class GoalUpdate(_Out):
    goal_id: int
    status: Literal["proposed", "active", "blocked", "done", "abandoned"]
    progress_note: str = ""


class Fact(_Out):
    content: str
    tags: list[str] = Field(default_factory=list)
    confidence: float = 0.8


class Reflection(_Out):
    summary: str
    task_update: TaskUpdate | None = None
    goal_update: GoalUpdate | None = None
    facts: list[Fact] = Field(default_factory=list, max_length=6)
    lessons: list[str] = Field(default_factory=list, max_length=3)
    dissent_vindicated: bool | None = None


class Digest(_Out):
    digest: str
