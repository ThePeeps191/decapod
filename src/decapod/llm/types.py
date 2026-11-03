"""Request/response types for the LLM layer."""

from __future__ import annotations

from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from decapod.domain import Cost, Tier

Role = Literal["system", "user", "assistant"]


class Message(BaseModel):
    role: Role
    content: str

    @classmethod
    def system(cls, content: str) -> Message:
        return cls(role="system", content=content)

    @classmethod
    def user(cls, content: str) -> Message:
        return cls(role="user", content=content)

    @classmethod
    def assistant(cls, content: str) -> Message:
        return cls(role="assistant", content=content)


class ProviderRequest(BaseModel):
    """What a provider receives. ``purpose`` lets fake/replay providers route responses."""

    model: str
    tier: Tier
    messages: list[Message]
    temperature: float = 0.7
    max_tokens: int | None = None
    json_mode: Literal["schema", "object", "none"] = "none"
    json_schema: dict[str, Any] | None = None
    schema_name: str | None = None
    purpose: str = "chat"
    cycle_id: str | None = None


class ProviderResponse(BaseModel):
    content: str
    reasoning: str = ""
    tokens_in: int | None = None
    tokens_out: int | None = None
    model: str | None = None


class Completion(BaseModel):
    content: str
    reasoning: str = ""
    tier: Tier
    model: str
    purpose: str
    cost: Cost = Field(default_factory=Cost)
    latency_ms: int = 0
    attempts: int = 1


class ProviderError(Exception):
    """Raised by providers. ``retryable`` drives the retry loop."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = True,
        status: int | None = None,
        bad_response_format: bool = False,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status
        self.bad_response_format = bad_response_format


class LLMProvider(Protocol):
    async def complete(self, request: ProviderRequest) -> ProviderResponse: ...

    async def embed(self, model: str, texts: list[str]) -> list[list[float]]: ...

    async def close(self) -> None: ...


class SpendGate(Protocol):
    """Implemented by the control layer's budget. Kept here to avoid an upward import."""

    def authorize(self, estimate: Cost, cycle_id: str | None) -> None: ...

    def record(self, kind: str, cost: Cost, cycle_id: str | None) -> None: ...
