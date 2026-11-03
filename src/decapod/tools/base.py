"""Tool interface. A tool is a typed capability with a declared risk tier and scopes."""

from __future__ import annotations

import os
import signal
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, ConfigDict

from decapod.domain import RiskTier, SideEffect, ToolResult

if TYPE_CHECKING:
    import asyncio

    import httpx

    from decapod.config import Charter, InstancePaths, Secrets, Settings
    from decapod.control import Budget
    from decapod.llm import LLM
    from decapod.memory import AgendaStore, MemoryStore
    from decapod.storage import Database, EventLog


class ToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoArgs(ToolArgs):
    pass


class ProcessRegistry:
    """Tracks child processes so the kill switch can terminate them immediately."""

    def __init__(self) -> None:
        self._procs: set[asyncio.subprocess.Process] = set()

    def add(self, proc: asyncio.subprocess.Process) -> None:
        self._procs.add(proc)

    def discard(self, proc: asyncio.subprocess.Process) -> None:
        self._procs.discard(proc)

    def kill_all(self) -> int:
        killed = 0
        for proc in list(self._procs):
            if proc.returncode is not None:
                continue
            try:
                if sys.platform != "win32":
                    os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.kill()
                killed += 1
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
        return killed

    def __len__(self) -> int:
        return sum(1 for p in self._procs if p.returncode is None)


@dataclass
class ToolContext:
    paths: InstancePaths
    settings: Settings
    charter: Charter
    secrets: Secrets
    db: Database
    events: EventLog
    agenda: AgendaStore
    memory: MemoryStore
    budget: Budget
    http: httpx.AsyncClient
    procs: ProcessRegistry = field(default_factory=ProcessRegistry)
    llm: LLM | None = None
    cycle_id: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)


class Tool(ABC):
    name: ClassVar[str]
    description: ClassVar[str]
    Args: ClassVar[type[ToolArgs]] = NoArgs
    tier: ClassVar[RiskTier] = RiskTier.INTERNAL
    side_effect: ClassVar[SideEffect] = SideEffect.NONE
    #: Scopes required regardless of arguments. Override ``scopes()`` for dynamic scopes.
    required_scopes: ClassVar[tuple[str, ...]] = ()
    #: Counts against the outbound-message budget and gets the disclosure footer.
    outbound: ClassVar[bool] = False

    def scopes(self, args: ToolArgs) -> list[str]:
        return list(self.required_scopes)

    def unavailable_reason(self, settings: Settings, secrets: Secrets) -> str | None:
        """Return why this tool cannot be used in this instance, or None if usable."""
        return None

    def precheck(self, args: Any, ctx: ToolContext) -> str | None:
        """Argument checks that need instance state. A message here becomes a policy denial."""
        return None

    @abstractmethod
    async def run(self, args: Any, ctx: ToolContext) -> ToolResult: ...

    def spec(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "tier": int(self.tier),
            "args": self.Args.model_json_schema(),
        }

    def signature(self) -> str:
        """Compact one-line argument signature for prompts."""
        schema = self.Args.model_json_schema()
        props: dict[str, Any] = schema.get("properties", {})
        required = set(schema.get("required", []))
        parts = []
        for key, prop in props.items():
            kind = _type_name(prop, schema)
            optional = "" if key in required else "?"
            desc = prop.get("description")
            parts.append(f"{key}{optional}: {kind}" + (f" ({desc})" if desc else ""))
        return ", ".join(parts)


def _type_name(prop: dict[str, Any], schema: dict[str, Any]) -> str:
    if "enum" in prop:
        return "|".join(repr(v) for v in prop["enum"])
    if "const" in prop:
        return repr(prop["const"])
    if "anyOf" in prop:
        return " | ".join(_type_name(p, schema) for p in prop["anyOf"])
    if "$ref" in prop:
        ref = prop["$ref"].rsplit("/", 1)[-1]
        target = schema.get("$defs", {}).get(ref, {})
        if "enum" in target:
            return "|".join(repr(v) for v in target["enum"])
        inner = target.get("properties", {})
        return "{" + ", ".join(f"{k}: {_type_name(v, schema)}" for k, v in inner.items()) + "}"
    kind = prop.get("type", "any")
    if kind == "array":
        return f"list[{_type_name(prop.get('items', {}), schema)}]"
    return {
        "integer": "int",
        "number": "float",
        "string": "str",
        "boolean": "bool",
        "object": "object",
        "null": "null",
    }.get(kind, str(kind))
