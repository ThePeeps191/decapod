"""Enumerations shared by every Decapod subsystem."""

from __future__ import annotations

from enum import IntEnum, StrEnum


class Tier(StrEnum):
    """Model tiers. Each maps to a concrete model in config (or a provider preset).

    * FLASH – fast and cheap: specialists, triage, memory compaction.
    * PRO   – capable: managers, reflection, the executive's normal decisions.
    * MAX   – the strongest model you have: the executive on grave (L3) decisions.
    """

    FLASH = "FLASH"
    PRO = "PRO"
    MAX = "MAX"

    @classmethod
    def parse(cls, value: str | Tier) -> Tier:
        """Parse a tier name (case-insensitive), accepting common synonyms."""
        if isinstance(value, Tier):
            return value
        key = str(value).strip().upper()
        key = TIER_ALIASES.get(key, key)
        try:
            return cls(key)
        except ValueError:
            valid = ", ".join(t.value for t in cls)
            raise ValueError(f"unknown model tier {value!r} (valid: {valid})") from None


#: Synonyms accepted in config files.
TIER_ALIASES: dict[str, str] = {
    "FAST": "FLASH",
    "CHEAP": "FLASH",
    "SMALL": "FLASH",
    "STANDARD": "PRO",
    "SMART": "PRO",
    "STRONG": "MAX",
    "BEST": "MAX",
    "FRONTIER": "MAX",
}


class RiskTier(IntEnum):
    """How much damage a tool can do. Enforced by the policy engine, never by prompts."""

    INTERNAL = 0  # think, agenda, memory, read own drive
    READ = 1  # read-only access to the outside world
    LOCAL_WRITE = 2  # write inside the sandboxed drive, run sandboxed code
    EXTERNAL_WRITE = 3  # send messages, post publicly, open PRs
    IRREVERSIBLE = 4  # deploys, deletions of external data, spending money

    @property
    def label(self) -> str:
        return _RISK_LABELS[self]


_RISK_LABELS = {
    RiskTier.INTERNAL: "internal",
    RiskTier.READ: "read",
    RiskTier.LOCAL_WRITE: "local-write",
    RiskTier.EXTERNAL_WRITE: "external-write",
    RiskTier.IRREVERSIBLE: "irreversible",
}


class SideEffect(StrEnum):
    NONE = "none"
    LOCAL = "local"
    EXTERNAL_READ = "external_read"
    EXTERNAL_WRITE = "external_write"
    IRREVERSIBLE = "irreversible"


class Level(IntEnum):
    """Caucus escalation levels: how hard Decapod thinks before acting."""

    L0 = 0  # reflex: planner + quick confirmation
    L1 = 1  # quick: a few proposers, one critic, executive
    L2 = 2  # full: complete six-round protocol
    L3 = 3  # grave: full protocol, two critique rounds, frontier executive

    @property
    def label(self) -> str:
        return ("reflex", "quick", "full", "grave")[self.value]

    @classmethod
    def parse(cls, value: str | int | Level) -> Level:
        if isinstance(value, Level):
            return value
        if isinstance(value, int):
            return cls(value)
        text = str(value).strip().upper()
        if text.startswith("L"):
            text = text[1:]
        return cls(int(text))


class GoalStatus(StrEnum):
    PROPOSED = "proposed"
    ACTIVE = "active"
    BLOCKED = "blocked"
    DONE = "done"
    ABANDONED = "abandoned"

    @property
    def is_open(self) -> bool:
        return self in (GoalStatus.PROPOSED, GoalStatus.ACTIVE, GoalStatus.BLOCKED)


class TaskStatus(StrEnum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"

    @property
    def is_open(self) -> bool:
        return self in (TaskStatus.TODO, TaskStatus.IN_PROGRESS)


class CycleStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    ABORTED = "aborted"  # process died mid-cycle; detected on restart
    FAILED = "failed"  # an exception ended the cycle
    HALTED = "halted"  # stopped by the control layer


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    EXECUTED = "executed"


class RunState(StrEnum):
    """Lifecycle state of the daemon as seen by the operator."""

    STARTING = "starting"
    RUNNING = "running"
    SLEEPING = "sleeping"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    KILLED = "killed"


class MemoryKind(StrEnum):
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    REFLECTIVE = "reflective"


class ControlCommand(StrEnum):
    PAUSE = "pause"
    RESUME = "resume"
    STOP = "stop"
    KILL = "kill"
    WAKE = "wake"
