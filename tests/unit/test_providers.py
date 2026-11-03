import pytest

from decapod.config import Secrets
from decapod.config.providers import PRESETS, Route, known_key_envs, resolve_routes
from decapod.config.settings import LLMSettings, ProviderSettings
from decapod.domain import Tier
from decapod.llm import LLM, MultiProvider, ProviderError, ProviderRequest, ScriptedProvider


def test_auto_detects_first_provider_with_a_key():
    routing = resolve_routes(
        LLMSettings(), Secrets({"OPENROUTER_API_KEY": "k", "DEEPSEEK_API_KEY": "k"})
    )
    assert routing.ok, routing.problems
    assert routing.default_provider == "openrouter"
    assert routing.routes[Tier.FLASH] == Route("openrouter", "deepseek/deepseek-v4-flash")
    assert set(routing.providers) == {"openrouter"}


def test_anthropic_preferred_and_mixed_routing():
    settings = LLMSettings(
        tiers={"MAX": "openrouter:anthropic/claude-fable-5", "FLASH": "tencent/hy3:free"}
    )
    routing = resolve_routes(
        settings, Secrets({"ANTHROPIC_API_KEY": "a", "OPENROUTER_API_KEY": "o"})
    )
    assert routing.default_provider == "anthropic"
    assert routing.routes[Tier.PRO].provider == "anthropic"
    assert routing.routes[Tier.MAX] == Route("openrouter", "anthropic/claude-fable-5")
    # a colon inside a model name is not a provider prefix
    assert routing.routes[Tier.FLASH] == Route("anthropic", "tencent/hy3:free")
    assert set(routing.providers) == {"anthropic", "openrouter"}


def test_problems_are_reported():
    routing = resolve_routes(LLMSettings(), Secrets({}))
    assert not routing.ok and "no provider API key" in routing.problems[0]
    groq = resolve_routes(LLMSettings(), Secrets({"GROQ_API_KEY": "x"}))
    assert any("no model for tier FLASH" in p for p in groq.problems)
    missing_key = resolve_routes(LLMSettings(default_provider="deepseek"), Secrets({}))
    assert any("DEEPSEEK_API_KEY" in p for p in missing_key.problems)
    unknown = resolve_routes(LLMSettings(default_provider="nope"), Secrets({}))
    assert any("not a known provider" in p for p in unknown.problems)


def test_local_and_custom_providers():
    settings = LLMSettings(default_provider="ollama", tiers={t: "llama3" for t in Tier})
    routing = resolve_routes(settings, Secrets({}))
    assert routing.ok and routing.routes[Tier.FLASH] == Route("ollama", "llama3")
    custom = LLMSettings(
        providers={
            "corp": ProviderSettings(
                base_url="https://llm.corp/v1", models={"FLASH": "m1", "PRO": "m2", "MAX": "m3"}
            )
        }
    )
    routing = resolve_routes(custom, Secrets({"CORP_API_KEY": "k", "ANTHROPIC_API_KEY": "a"}))
    assert routing.default_provider == "corp"  # explicitly configured providers win
    assert routing.providers["corp"].api_key_env == "CORP_API_KEY"


def test_placeholder_routing_for_tests():
    routing = resolve_routes(LLMSettings(embeddings_model="emb"), None, placeholder=True)
    assert routing.routes[Tier.PRO] == Route("default", "pro")
    assert routing.embeddings == Route("default", "emb")


def test_presets_and_key_list():
    assert "ANTHROPIC_API_KEY" in known_key_envs()
    assert not PRESETS["ollama"].requires_key
    assert all(p.base_url.startswith("http") for p in PRESETS.values())


async def test_multiprovider_dispatch_and_missing_key():
    routing = resolve_routes(LLMSettings(), Secrets({"DEEPSEEK_API_KEY": "k"}))
    multi = MultiProvider(routing.providers, Secrets({}))
    with pytest.raises(ProviderError, match="no API key"):
        await multi.complete(
            ProviderRequest(model="m", tier=Tier.FLASH, messages=[], provider="deepseek")
        )
    with pytest.raises(ProviderError, match="unknown provider"):
        await multi.complete(
            ProviderRequest(model="m", tier=Tier.FLASH, messages=[], provider="ghost")
        )
    await multi.close()


async def test_llm_sends_provider_and_model():
    routing = resolve_routes(LLMSettings(), Secrets({"DEEPSEEK_API_KEY": "k"}))
    provider = ScriptedProvider({"x": "ok"})

    async def no_sleep(_):
        return None

    llm = LLM(LLMSettings(), provider, routing=routing, sleep=no_sleep)
    await llm.chat([], tier=Tier.PRO, purpose="x")
    assert (provider.calls[0].provider, provider.calls[0].model) == ("deepseek", "deepseek-chat")
    assert str(llm.route(Tier.FLASH)) == "deepseek:deepseek-chat"
