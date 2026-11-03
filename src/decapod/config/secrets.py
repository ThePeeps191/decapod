"""Secret lookup. Secrets never enter LLM context; tools fetch them by name."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from dotenv import dotenv_values


class Secrets:
    """Read-only view over the process environment overlaid with the instance ``.env``."""

    def __init__(self, values: Mapping[str, str] | None = None) -> None:
        self._values: dict[str, str] = dict(values or {})

    @classmethod
    def load(cls, env_file: Path | None) -> Secrets:
        merged: dict[str, str] = {k: v for k, v in os.environ.items() if v is not None}
        if env_file is not None and env_file.exists():
            for key, value in dotenv_values(env_file).items():
                if value is not None and key not in os.environ:
                    merged[key] = value
        return cls(merged)

    def get(self, name: str, default: str | None = None) -> str | None:
        value = self._values.get(name)
        return value if value else default

    def require(self, name: str) -> str:
        value = self.get(name)
        if not value:
            raise KeyError(f"secret {name!r} is not set (add it to the instance .env)")
        return value

    def values_for_scrubbing(self, min_length: int = 8) -> list[str]:
        """Secret-looking values to redact from outbound text and logs."""
        markers = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASS", "AUTH")
        return sorted(
            {
                v
                for k, v in self._values.items()
                if any(m in k.upper() for m in markers) and len(v) >= min_length
            },
            key=len,
            reverse=True,
        )

    def redact(self, text: str) -> str:
        for value in self.values_for_scrubbing():
            if value in text:
                text = text.replace(value, "[REDACTED]")
        return text
