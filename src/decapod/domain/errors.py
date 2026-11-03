"""Exception hierarchy."""

from __future__ import annotations

from datetime import datetime


class DecapodError(Exception):
    """Base class for all Decapod errors."""


class ConfigError(DecapodError):
    """Invalid or missing configuration / charter."""


class Halted(DecapodError):
    """Raised at a checkpoint when the operator stopped or killed the instance."""

    def __init__(self, reason: str = "halted", *, kill: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.kill = kill


class BudgetExceeded(DecapodError):
    """A hard budget ceiling would be crossed."""

    def __init__(
        self,
        window: str,
        metric: str,
        used: float,
        limit: float,
        resets_at: datetime | None = None,
    ) -> None:
        self.window = window
        self.metric = metric
        self.used = used
        self.limit = limit
        self.resets_at = resets_at
        super().__init__(f"{window} budget for {metric} exhausted ({used:g}/{limit:g})")

    @property
    def is_lifetime(self) -> bool:
        return self.window == "lifetime"


class LLMError(DecapodError):
    """A model call failed after retries and fallbacks."""


class ProviderUnavailable(LLMError):
    """The circuit breaker is open: the provider has been failing repeatedly."""


class StructuredOutputError(LLMError):
    """The model did not return valid structured output after repair attempts."""
