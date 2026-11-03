"""Secret lookup. Secrets never enter LLM context; tools fetch them by name.

Sources, highest priority first: the process environment, the instance ``.env``, then the
user-wide ``.env`` (``~/.config/decapod/.env`` on Linux; see ``user_env_file()``). Put as
many provider API keys there as you like.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from dotenv import dotenv_values
from platformdirs import user_config_dir


def user_env_file() -> Path:
    return Path(user_config_dir("decapod", appauthor=False)) / ".env"


class Secrets:
    """Read-only view over the environment overlaid with ``.env`` files."""

    def __init__(self, values: Mapping[str, str] | None = None) -> None:
        self._values: dict[str, str] = dict(values or {})

    @classmethod
    def load(cls, env_file: Path | None, *, include_user: bool = True) -> Secrets:
        merged: dict[str, str] = {}
        files = [user_env_file()] if include_user else []
        if env_file is not None:
            files.append(env_file)
        for path in files:  # later files override earlier ones
            if path.exists():
                for key, value in dotenv_values(path).items():
                    if value:
                        merged[key] = value
        merged.update({k: v for k, v in os.environ.items() if v})
        return cls(merged)

    def get(self, name: str, default: str | None = None) -> str | None:
        value = self._values.get(name)
        return value if value else default

    def require(self, name: str) -> str:
        value = self.get(name)
        if not value:
            raise KeyError(f"secret {name!r} is not set (add it to a .env file)")
        return value

    def names(self) -> list[str]:
        return sorted(self._values)

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
