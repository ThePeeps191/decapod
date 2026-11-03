"""The charter: the operator-authored, read-only constitution of an instance.

Decapod reads the charter every cycle and may *propose* amendments, but there is no
tool that can write it. Its SHA-256 is logged each cycle so tampering is visible.
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from decapod.domain import ConfigError


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Identity(_Section):
    name: str = "Decapod"
    mission: str
    voice: str = "clear, honest and concise"
    description: str = ""


class Values(_Section):
    ranked: list[str] = Field(default_factory=list)


class Scopes(_Section):
    #: Glob patterns of scopes the instance may ever use, e.g. "web.read", "drive.*".
    granted: list[str] = Field(default_factory=lambda: ["web.read", "drive.*", "notes.*"])


class Prohibitions(_Section):
    #: Natural-language rules shown to every agent and checked by the Risk Officer.
    rules: list[str] = Field(default_factory=list)
    #: Regular expressions; any external-write action whose arguments match is denied.
    patterns: list[str] = Field(default_factory=list)

    @field_validator("patterns")
    @classmethod
    def _compile(cls, value: list[str]) -> list[str]:
        for pattern in value:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid prohibition pattern {pattern!r}: {exc}") from None
        return value


class Outbound(_Section):
    #: Appended to every outbound message by the tool layer (not by the prompt).
    disclosure: str = (
        "\n\n—\nThis message was written and sent by {name}, an autonomous AI agent "
        "(https://github.com/ThePeeps191/decapod)."
    )
    #: "reply_only": may only message people who contacted it first.
    contact_policy: Literal["reply_only", "any"] = "reply_only"


class SeedGoal(_Section):
    title: str
    description: str = ""
    priority: float = 0.6


class Goals(_Section):
    seed: list[SeedGoal] = Field(default_factory=list)


class Charter(_Section):
    identity: Identity
    values: Values = Field(default_factory=Values)
    scopes: Scopes = Field(default_factory=Scopes)
    prohibitions: Prohibitions = Field(default_factory=Prohibitions)
    outbound: Outbound = Field(default_factory=Outbound)
    goals: Goals = Field(default_factory=Goals)

    sha256: str = ""
    source: str = ""

    @property
    def name(self) -> str:
        return self.identity.name

    def clauses(self) -> dict[str, str]:
        """Addressable clauses that goals cite via ``charter_refs``."""
        out = {"mission": self.identity.mission}
        for i, value in enumerate(self.values.ranked, 1):
            out[f"value.{i}"] = value
        for i, rule in enumerate(self.prohibitions.rules, 1):
            out[f"prohibition.{i}"] = rule
        return out

    def disclosure_text(self) -> str:
        return self.outbound.disclosure.replace("{name}", self.identity.name)

    def compiled_patterns(self) -> list[re.Pattern[str]]:
        return [re.compile(p, re.IGNORECASE) for p in self.prohibitions.patterns]

    def render(self) -> str:
        """Plain-text rendering used in prompts."""
        lines = [f"Name: {self.identity.name}", f"Mission: {self.identity.mission}"]
        if self.identity.description:
            lines.append(f"About: {self.identity.description}")
        lines.append(f"Voice: {self.identity.voice}")
        if self.values.ranked:
            lines.append("Values (highest priority first):")
            lines += [f"  value.{i}: {v}" for i, v in enumerate(self.values.ranked, 1)]
        if self.prohibitions.rules:
            lines.append("Hard prohibitions (never do these):")
            lines += [f"  prohibition.{i}: {r}" for i, r in enumerate(self.prohibitions.rules, 1)]
        lines.append("Granted scopes: " + ", ".join(self.scopes.granted or ["(none)"]))
        lines.append(f"Contact policy: {self.outbound.contact_policy}")
        return "\n".join(lines)


def parse_charter(text: str, source: str = "<charter>") -> Charter:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{source}: invalid TOML: {exc}") from None
    try:
        charter = Charter.model_validate(data)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise ConfigError(f"{source}: {details}") from None
    charter.sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    charter.source = source
    return charter


def load_charter(path: Path) -> Charter:
    if not path.exists():
        raise ConfigError(f"charter not found: {path} (run `decapod init`)")
    return parse_charter(path.read_text(encoding="utf-8"), str(path))
