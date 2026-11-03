"""Model access: providers, tiers, structured output and metering."""

from __future__ import annotations

from decapod.config.providers import Routing, resolve_routes
from decapod.config.secrets import Secrets
from decapod.config.settings import LLMSettings
from decapod.llm.fake import ReplayProvider, ScriptedProvider, hash_embedding
from decapod.llm.provider import MultiProvider, OpenAICompatibleProvider
from decapod.llm.service import LLM, CircuitBreaker
from decapod.llm.types import (
    Completion,
    LLMProvider,
    Message,
    ProviderError,
    ProviderRequest,
    ProviderResponse,
    SpendGate,
)


def build_provider(
    settings: LLMSettings, secrets: Secrets, routing: Routing | None = None
) -> MultiProvider:
    routing = routing or resolve_routes(settings, secrets)
    return MultiProvider(routing.providers, secrets, timeout=settings.timeout.total_seconds())


__all__ = [
    "LLM",
    "CircuitBreaker",
    "Completion",
    "LLMProvider",
    "Message",
    "MultiProvider",
    "OpenAICompatibleProvider",
    "ProviderError",
    "ProviderRequest",
    "ProviderResponse",
    "ReplayProvider",
    "ScriptedProvider",
    "SpendGate",
    "build_provider",
    "hash_embedding",
]
