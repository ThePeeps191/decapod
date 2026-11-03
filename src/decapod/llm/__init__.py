"""Model access: providers, tiers, structured output and metering."""

from __future__ import annotations

from decapod.config.secrets import Secrets
from decapod.config.settings import LLMSettings
from decapod.llm.fake import ReplayProvider, ScriptedProvider, hash_embedding
from decapod.llm.provider import OpenAICompatibleProvider
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


def build_provider(settings: LLMSettings, secrets: Secrets) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        settings.base_url,
        secrets.get(settings.api_key_env),
        timeout=settings.timeout.total_seconds(),
        extra_headers=settings.extra_headers,
    )


__all__ = [
    "LLM",
    "CircuitBreaker",
    "Completion",
    "LLMProvider",
    "Message",
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
