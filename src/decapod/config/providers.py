"""Model providers: built-in presets for OpenAI-compatible endpoints and tier routing.

Preset default models are a convenience; providers rename models over time, so override
any of them in ``decapod.toml`` (``[llm.tiers]``) if a provider rejects a name.

Decapod talks to any OpenAI-compatible chat-completions API. Put one or more API keys in a
``.env`` file (the instance's, or ``~/.config/decapod/.env``) and Decapod picks a default
provider automatically; route individual tiers elsewhere with ``"provider:model"``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from decapod.config.secrets import Secrets
from decapod.config.settings import LLMSettings, ProviderSettings
from decapod.domain import Tier


@dataclass(frozen=True)
class ProviderPreset:
    name: str
    label: str
    base_url: str
    api_key_env: str | None
    models: dict[Tier, str] = field(default_factory=dict)
    requires_key: bool = True


def _tiers(flash: str, pro: str, max_: str) -> dict[Tier, str]:
    return {Tier.FLASH: flash, Tier.PRO: pro, Tier.MAX: max_}


PRESETS: dict[str, ProviderPreset] = {
    p.name: p
    for p in (
        ProviderPreset(
            "anthropic",
            "Anthropic (Claude)",
            "https://api.anthropic.com/v1/",
            "ANTHROPIC_API_KEY",
            _tiers("claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"),
        ),
        ProviderPreset(
            "openai",
            "OpenAI",
            "https://api.openai.com/v1",
            "OPENAI_API_KEY",
            _tiers("gpt-5-mini", "gpt-5", "gpt-5"),
        ),
        ProviderPreset(
            "openrouter",
            "OpenRouter",
            "https://openrouter.ai/api/v1",
            "OPENROUTER_API_KEY",
            _tiers(
                "deepseek/deepseek-v4-flash",
                "deepseek/deepseek-v4-pro",
                "anthropic/claude-opus-5-5",
            ),
        ),
        ProviderPreset(
            "deepseek",
            "DeepSeek",
            "https://api.deepseek.com/v1",
            "DEEPSEEK_API_KEY",
            _tiers("deepseek-chat", "deepseek-chat", "deepseek-reasoner"),
        ),
        ProviderPreset(
            "gemini",
            "Google Gemini",
            "https://generativelanguage.googleapis.com/v1beta/openai/",
            "GEMINI_API_KEY",
        ),
        ProviderPreset("mistral", "Mistral", "https://api.mistral.ai/v1", "MISTRAL_API_KEY"),
        ProviderPreset("groq", "Groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY"),
        ProviderPreset(
            "together", "Together AI", "https://api.together.xyz/v1", "TOGETHER_API_KEY"
        ),
        ProviderPreset("xai", "xAI", "https://api.x.ai/v1", "XAI_API_KEY"),
        ProviderPreset(
            "fireworks",
            "Fireworks AI",
            "https://api.fireworks.ai/inference/v1",
            "FIREWORKS_API_KEY",
        ),
        ProviderPreset(
            "ollama", "Ollama (local)", "http://localhost:11434/v1", None, requires_key=False
        ),
        ProviderPreset(
            "lmstudio", "LM Studio (local)", "http://localhost:1234/v1", None, requires_key=False
        ),
    )
}

#: Providers tried, in order, when no default provider is configured.
AUTO_ORDER = (
    "anthropic",
    "openai",
    "openrouter",
    "deepseek",
    "gemini",
    "mistral",
    "groq",
    "together",
    "xai",
    "fireworks",
)


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    base_url: str
    api_key_env: str | None
    requires_key: bool
    extra_headers: dict[str, str]
    models: dict[Tier, str]


@dataclass(frozen=True)
class Route:
    provider: str
    model: str

    def __str__(self) -> str:
        return f"{self.provider}:{self.model}"


@dataclass
class Routing:
    default_provider: str | None
    routes: dict[Tier, Route]
    providers: dict[str, ProviderConfig]
    problems: list[str]
    embeddings: Route | None = None

    @property
    def ok(self) -> bool:
        return not self.problems


def provider_configs(settings: LLMSettings) -> dict[str, ProviderConfig]:
    """Presets merged with (and extended by) the instance's ``[llm.providers.*]``."""
    out: dict[str, ProviderConfig] = {}
    names = list(PRESETS) + [n for n in settings.providers if n not in PRESETS]
    for name in names:
        preset = PRESETS.get(name)
        override = settings.providers.get(name, ProviderSettings())
        base_url = override.base_url or (preset.base_url if preset else None)
        if not base_url:
            continue  # a custom provider must at least name its endpoint
        requires_key = override.requires_key
        if requires_key is None:
            requires_key = preset.requires_key if preset else True
        key_env = override.api_key_env or (preset.api_key_env if preset else None)
        if key_env is None and requires_key:
            key_env = f"{name.upper().replace('-', '_')}_API_KEY"
        out[name] = ProviderConfig(
            name=name,
            base_url=base_url,
            api_key_env=key_env,
            requires_key=requires_key,
            extra_headers=dict(override.extra_headers),
            models={**(preset.models if preset else {}), **override.models},
        )
    return out


def has_key(config: ProviderConfig, secrets: Secrets) -> bool:
    if not config.requires_key:
        return True
    return bool(config.api_key_env and secrets.get(config.api_key_env))


def detect_default(
    settings: LLMSettings, secrets: Secrets, configs: dict[str, ProviderConfig]
) -> str | None:
    if settings.default_provider:
        return settings.default_provider
    custom = [n for n in settings.providers if n not in PRESETS]
    for name in [*custom, *AUTO_ORDER]:
        config = configs.get(name)
        if config is not None and config.requires_key and has_key(config, secrets):
            return name
    return None


def _parse(
    spec: str, default: str | None, configs: dict[str, ProviderConfig]
) -> tuple[str | None, str]:
    prefix, sep, rest = spec.partition(":")
    if sep and prefix in configs and rest:
        return prefix, rest
    return default, spec


def resolve_routes(
    settings: LLMSettings, secrets: Secrets | None, *, placeholder: bool = False
) -> Routing:
    """Work out which provider and model serves each tier.

    With ``placeholder=True`` (tests and offline tools) tiers that cannot be resolved get
    a ``default:<tier>`` route instead of a problem report.
    """
    secrets = secrets or Secrets({})
    configs = provider_configs(settings)
    default = detect_default(settings, secrets, configs)
    problems: list[str] = []
    if settings.default_provider and settings.default_provider not in configs:
        problems.append(
            f"default_provider {settings.default_provider!r} is not a known "
            f"provider (known: {', '.join(configs)})"
        )
    routes: dict[Tier, Route] = {}
    for tier in Tier:
        spec = settings.tiers.get(tier)
        if spec:
            provider, model = _parse(spec, default, configs)
        else:
            provider = default
            model = configs[default].models.get(tier, "") if default in configs else ""
        if provider is None or provider not in configs or not model:
            if placeholder:
                routes[tier] = Route(provider or "default", model or tier.value.lower())
                continue
            if provider is None:
                problems.append(f"no model for tier {tier.value}: no provider API key found")
            elif provider not in configs:
                problems.append(f"tier {tier.value}: unknown provider {provider!r}")
            else:
                problems.append(
                    f"no model for tier {tier.value} on provider {provider!r}; set "
                    f'[llm.tiers] {tier.value} = "{provider}:<model>"'
                )
            continue
        routes[tier] = Route(provider, model)
    used = {r.provider for r in routes.values()}
    for name in sorted(used):
        config = configs.get(name)
        if config is not None and not has_key(config, secrets) and not placeholder:
            problems.append(f"provider {name!r} needs {config.api_key_env} (set it in a .env)")
    embeddings: Route | None = None
    if settings.embeddings_model:
        provider, model = _parse(settings.embeddings_model, default, configs)
        if provider is None and placeholder:
            provider = "default"
        if provider is not None:
            embeddings = Route(provider, model)
            used.add(provider)
    if not routes and not problems:
        problems.append("no model tiers could be resolved")
    return Routing(
        default_provider=default,
        routes=routes,
        providers={n: c for n, c in configs.items() if n in used},
        problems=list(dict.fromkeys(problems)),
        embeddings=embeddings,
    )


def known_key_envs() -> list[str]:
    return [p.api_key_env for p in PRESETS.values() if p.api_key_env]
