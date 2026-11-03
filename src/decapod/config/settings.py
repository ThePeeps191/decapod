"""Instance settings: packaged defaults < ``decapod.toml`` < ``DECAPOD_*`` environment."""

from __future__ import annotations

import os
import tomllib
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from decapod.domain import ConfigError, Level, RiskTier, Tier
from decapod.util.timeutil import parse_duration

PolicyMode = Literal["auto", "approval", "deny"]


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


def _duration(value: Any) -> timedelta:
    try:
        return parse_duration(value)
    except ValueError as exc:
        raise ValueError(str(exc)) from None


class InstanceSettings(_Section):
    name: str = "decapod"
    charter: str = "charter.toml"


class PriceSettings(_Section):
    input: float = 0.0  # USD per million input tokens
    output: float = 0.0  # USD per million output tokens


class ProviderSettings(_Section):
    """An OpenAI-compatible endpoint. Built-in presets fill in anything left unset."""

    base_url: str | None = None
    api_key_env: str | None = None
    #: Set for local servers (Ollama, LM Studio, vLLM) that need no key.
    requires_key: bool | None = None
    extra_headers: dict[str, str] = Field(default_factory=dict)
    #: Default model per tier for this provider (overrides the preset's defaults).
    models: dict[Tier, str] = Field(default_factory=dict)

    @field_validator("models", mode="before")
    @classmethod
    def _parse_models(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {Tier.parse(k): v for k, v in value.items()}
        return value


class LLMSettings(_Section):
    #: Provider used for tiers that do not name one. None = the first provider (in preset
    #: order) whose API key is present in the environment / .env files.
    default_provider: str | None = None
    #: Extra providers or overrides of the built-in presets, keyed by name.
    providers: dict[str, ProviderSettings] = Field(default_factory=dict)
    #: Model per tier: "provider:model" or just "model" (uses the default provider).
    #: Tiers left out use the default provider's preset model for that tier.
    tiers: dict[Tier, str] = Field(default_factory=dict)
    #: If a tier fails after retries, try these tiers next (in order).
    fallbacks: dict[Tier, list[Tier]] = Field(
        default_factory=lambda: {
            Tier.MAX: [Tier.PRO],
            Tier.PRO: [Tier.FLASH],
        }
    )
    timeout: timedelta = timedelta(seconds=120)
    max_retries: int = Field(default=3, ge=0, le=10)
    max_concurrency: int = Field(default=4, ge=1, le=64)
    min_request_interval: timedelta = timedelta(0)
    #: "schema" = JSON-schema response_format, "object" = JSON mode, "none" = prompt only.
    json_mode: Literal["schema", "object", "none"] = "object"
    repair_attempts: int = Field(default=2, ge=0, le=5)
    circuit_breaker_failures: int = Field(default=5, ge=1)
    circuit_breaker_cooldown: timedelta = timedelta(minutes=5)
    #: USD per million tokens, keyed by "provider:model" or "model".
    pricing: dict[str, PriceSettings] = Field(default_factory=dict)
    default_price: PriceSettings = Field(default_factory=PriceSettings)
    #: "provider:model" or "model" for embeddings; None = keyword search only.
    embeddings_model: str | None = None

    _v_timeout = field_validator(
        "timeout", "min_request_interval", "circuit_breaker_cooldown", mode="before"
    )(_duration)

    @field_validator("tiers", mode="before")
    @classmethod
    def _parse_tiers(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {Tier.parse(k): v for k, v in value.items()}
        return value

    @field_validator("fallbacks", mode="before")
    @classmethod
    def _parse_fallbacks(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {Tier.parse(k): [Tier.parse(t) for t in v] for k, v in value.items()}
        return value

    def price_for(self, provider: str, model: str) -> PriceSettings:
        return self.pricing.get(f"{provider}:{model}") or self.pricing.get(
            model, self.default_price
        )


class ScheduleSettings(_Section):
    min_interval: timedelta = timedelta(seconds=30)
    max_interval: timedelta = timedelta(hours=2)
    idle_interval: timedelta = timedelta(minutes=10)
    error_backoff: timedelta = timedelta(minutes=1)

    _v = field_validator(
        "min_interval", "max_interval", "idle_interval", "error_backoff", mode="before"
    )(_duration)


class BudgetLimits(_Section):
    usd: float | None = None
    tokens: int | None = None
    llm_requests: int | None = None
    tool_calls: int | None = None
    outbound: int | None = None

    def items(self) -> list[tuple[str, float]]:
        out: list[tuple[str, float]] = []
        for key in ("usd", "tokens", "llm_requests", "tool_calls", "outbound"):
            value = getattr(self, key)
            if value is not None:
                out.append((key, float(value)))
        return out


class BudgetSettings(_Section):
    cycle: BudgetLimits = Field(
        default_factory=lambda: BudgetLimits(llm_requests=40, tokens=400_000, tool_calls=5)
    )
    hourly: BudgetLimits = Field(default_factory=lambda: BudgetLimits(llm_requests=300))
    daily: BudgetLimits = Field(
        default_factory=lambda: BudgetLimits(usd=2.0, llm_requests=2_000, outbound=10)
    )
    lifetime: BudgetLimits = Field(default_factory=lambda: BudgetLimits(usd=50.0))
    warn_fraction: float = Field(default=0.8, gt=0, le=1)


class RateLimit(_Section):
    per_hour: int = Field(ge=0)


def _default_modes() -> dict[RiskTier, PolicyMode]:
    return {
        RiskTier.INTERNAL: "auto",
        RiskTier.READ: "auto",
        RiskTier.LOCAL_WRITE: "auto",
        RiskTier.EXTERNAL_WRITE: "approval",
        RiskTier.IRREVERSIBLE: "approval",
    }


class PolicySettings(_Section):
    modes: dict[RiskTier, PolicyMode] = Field(default_factory=_default_modes)
    #: Per-scope overrides, e.g. {"github:me/notes.write" = "auto"}. Glob patterns allowed.
    scope_modes: dict[str, PolicyMode] = Field(default_factory=dict)
    rate_limits: dict[str, RateLimit] = Field(
        default_factory=lambda: {
            "web.fetch": RateLimit(per_hour=120),
            "web.search": RateLimit(per_hour=60),
        }
    )
    approval_ttl: timedelta = timedelta(hours=24)
    #: Trip the kill switch after this many consecutive policy denials (0 = never).
    consecutive_denials_kill: int = Field(default=8, ge=0)

    _v = field_validator("approval_ttl", mode="before")(_duration)

    @field_validator("modes", mode="before")
    @classmethod
    def _parse_modes(cls, value: Any) -> Any:
        if isinstance(value, dict):
            out: dict[RiskTier, Any] = {}
            for key, mode in value.items():
                text = str(key).lower().removeprefix("tier")
                out[RiskTier(int(text))] = mode
            return out
        return value

    def mode_for(self, tier: RiskTier) -> PolicyMode:
        return self.modes.get(tier, "approval" if tier >= RiskTier.EXTERNAL_WRITE else "auto")


class CaucusSettings(_Section):
    max_level: Level = Level.L3
    min_level: Level = Level.L0
    #: Force a full agenda review (L2) every N cycles.
    review_every: int = Field(default=12, ge=0)
    #: Use a cheap LLM triage call in addition to rules.
    llm_triage: bool = True
    #: Minimum level for deliberations that end in an action of a given risk tier.
    escalate_on_tier: dict[RiskTier, Level] = Field(
        default_factory=lambda: {
            RiskTier.LOCAL_WRITE: Level.L1,
            RiskTier.EXTERNAL_WRITE: Level.L3,
            RiskTier.IRREVERSIBLE: Level.L3,
        }
    )
    #: Disable roles by name, e.g. ["opportunist"].
    disabled_roles: list[str] = Field(default_factory=list)
    #: Override a role's model tier, e.g. {critic = "STANDARD"}.
    role_tiers: dict[str, Tier] = Field(default_factory=dict)
    #: Ranking weight per role (default 1.0).
    role_weights: dict[str, float] = Field(default_factory=dict)
    max_proposals_per_role: int = Field(default=2, ge=1, le=5)
    loop_window: int = Field(default=6, ge=2)
    loop_threshold: int = Field(default=3, ge=2)

    @field_validator("max_level", "min_level", mode="before")
    @classmethod
    def _lvl(cls, value: Any) -> Any:
        return Level.parse(value)

    @field_validator("escalate_on_tier", mode="before")
    @classmethod
    def _esc(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                RiskTier(int(str(k).lower().removeprefix("tier"))): Level.parse(v)
                for k, v in value.items()
            }
        return value

    @field_validator("role_tiers", mode="before")
    @classmethod
    def _tiers(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {k: Tier.parse(v) for k, v in value.items()}
        return value


class MemorySettings(_Section):
    relevance_weight: float = 1.0
    recency_weight: float = 0.6
    importance_weight: float = 0.5
    goal_weight: float = 0.4
    recency_half_life: timedelta = timedelta(days=3)
    retrieve_k: int = Field(default=8, ge=0, le=50)
    lessons_k: int = Field(default=5, ge=0, le=20)
    compact_every: int = Field(default=50, ge=0)
    compact_after: timedelta = timedelta(days=2)
    context_tokens: int = Field(default=6_000, ge=500)

    _v = field_validator("recency_half_life", "compact_after", mode="before")(_duration)


class WebSettings(_Section):
    search_provider: Literal["duckduckgo", "searxng", "brave"] = "duckduckgo"
    searxng_url: str | None = None
    brave_api_key_env: str = "BRAVE_API_KEY"
    user_agent: str = "Decapod/1.0 (autonomous AI agent; +https://github.com/ThePeeps191/decapod)"
    max_bytes: int = 2_000_000
    timeout: timedelta = timedelta(seconds=20)
    allow_private_addresses: bool = False

    _v = field_validator("timeout", mode="before")(_duration)


class CodeSettings(_Section):
    timeout: timedelta = timedelta(seconds=30)
    memory_mb: int = 512
    isolation: Literal["auto", "bwrap", "none"] = "auto"
    allow_network: bool = False

    _v = field_validator("timeout", mode="before")(_duration)


class EmailSettings(_Section):
    enabled: bool = False
    address: str = ""
    imap_host: str = ""
    imap_port: int = 993
    smtp_host: str = ""
    smtp_port: int = 465
    username_env: str = "DECAPOD_EMAIL_USERNAME"
    password_env: str = "DECAPOD_EMAIL_PASSWORD"
    mailbox: str = "INBOX"
    poll_interval: timedelta = timedelta(minutes=5)

    _v = field_validator("poll_interval", mode="before")(_duration)


class GitHubSettings(_Section):
    enabled: bool = False
    token_env: str = "GITHUB_TOKEN"
    api_url: str = "https://api.github.com"


class MCPServerSettings(_Section):
    name: str
    command: list[str]
    env: dict[str, str] = Field(default_factory=dict)
    default_tier: RiskTier = RiskTier.EXTERNAL_WRITE
    tool_tiers: dict[str, RiskTier] = Field(default_factory=dict)
    enabled: bool = True
    timeout: timedelta = timedelta(seconds=60)

    _v = field_validator("timeout", mode="before")(_duration)


class FeedSettings(_Section):
    url: str
    name: str = ""


class ToolSettings(_Section):
    #: Tool names (globs) to disable entirely.
    disabled: list[str] = Field(default_factory=list)
    output_max_chars: int = Field(default=12_000, ge=500)
    web: WebSettings = Field(default_factory=WebSettings)
    code: CodeSettings = Field(default_factory=CodeSettings)
    email: EmailSettings = Field(default_factory=EmailSettings)
    github: GitHubSettings = Field(default_factory=GitHubSettings)
    feeds: list[FeedSettings] = Field(default_factory=list)
    drive_max_file_bytes: int = 5_000_000


class MCPSettings(_Section):
    servers: list[MCPServerSettings] = Field(default_factory=list)


class Settings(_Section):
    instance: InstanceSettings = Field(default_factory=InstanceSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    schedule: ScheduleSettings = Field(default_factory=ScheduleSettings)
    budget: BudgetSettings = Field(default_factory=BudgetSettings)
    policy: PolicySettings = Field(default_factory=PolicySettings)
    caucus: CaucusSettings = Field(default_factory=CaucusSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    tools: ToolSettings = Field(default_factory=ToolSettings)
    mcp: MCPSettings = Field(default_factory=MCPSettings)


# --------------------------------------------------------------------------- loading

ENV_PREFIX = "DECAPOD_"
_RESERVED_ENV = {
    "DECAPOD_HOME",
    "DECAPOD_INSTANCE",
    "DECAPOD_EMAIL_USERNAME",
    "DECAPOD_EMAIL_PASSWORD",
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _coerce_env_value(raw: str) -> Any:
    try:
        return tomllib.loads(f"v = {raw}")["v"]
    except tomllib.TOMLDecodeError:
        return raw


def env_overrides(environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Map ``DECAPOD_LLM__BASE_URL=...`` style variables onto nested settings."""
    environ = dict(os.environ if environ is None else environ)
    out: dict[str, Any] = {}
    for key, raw in environ.items():
        if not key.startswith(ENV_PREFIX) or key in _RESERVED_ENV or "__" not in key:
            continue
        path = [p.lower() for p in key[len(ENV_PREFIX) :].split("__") if p]
        if not path:
            continue
        node = out
        for part in path[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                break
        else:
            node[path[-1]] = _coerce_env_value(raw)
    return out


def load_settings(path: Path | None, environ: dict[str, str] | None = None) -> Settings:
    data: dict[str, Any] = {}
    if path is not None and path.exists():
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{path}: invalid TOML: {exc}") from None
    data = _deep_merge(data, env_overrides(environ))
    try:
        return Settings.model_validate(data)
    except ValidationError as exc:
        where = str(path) if path else "settings"
        raise ConfigError(f"{where}: {_format_validation(exc)}") from None


def _format_validation(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"])
        parts.append(f"{loc}: {err['msg']}")
    return "; ".join(parts)
