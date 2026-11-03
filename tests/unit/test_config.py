from datetime import timedelta

import pytest

from decapod.config import InstancePaths, Secrets, load_settings, parse_charter
from decapod.config.settings import env_overrides
from decapod.domain import ConfigError, Level, RiskTier, Tier

CHARTER = """
[identity]
name = "Tidepool"
mission = "Research tidal ecology."

[values]
ranked = ["accuracy", "budget"]

[scopes]
granted = ["web.read", "drive.*"]

[prohibitions]
rules = ["Never impersonate a human."]
patterns = ["(?i)password"]
"""


def test_defaults_are_valid():
    settings = load_settings(None, environ={})
    assert settings.llm.model_for(Tier.FAST) == "deepseek/deepseek-v4-flash"
    assert settings.policy.mode_for(RiskTier.EXTERNAL_WRITE) == "approval"
    assert settings.policy.mode_for(RiskTier.READ) == "auto"
    assert settings.schedule.min_interval == timedelta(seconds=30)


def test_load_toml_with_durations_and_aliases(tmp_path):
    path = tmp_path / "decapod.toml"
    path.write_text(
        """
[llm]
timeout = "45s"
[llm.tiers]
FLASH = "my/fast-model"
[schedule]
min_interval = "1m"
[policy.modes]
tier3 = "auto"
[caucus]
max_level = "L2"
[caucus.escalate_on_tier]
tier2 = "L2"
""",
        encoding="utf-8",
    )
    s = load_settings(path, environ={})
    assert s.llm.tiers[Tier.FAST] == "my/fast-model"
    assert s.llm.timeout == timedelta(seconds=45)
    assert s.schedule.min_interval == timedelta(minutes=1)
    assert s.policy.mode_for(RiskTier.EXTERNAL_WRITE) == "auto"
    assert s.caucus.max_level is Level.L2
    assert s.caucus.escalate_on_tier[RiskTier.LOCAL_WRITE] is Level.L2


def test_env_overrides():
    env = {
        "DECAPOD_LLM__BASE_URL": "http://localhost:8000/v1",
        "DECAPOD_BUDGET__DAILY__USD": "1.5",
        "DECAPOD_HOME": "/ignored",
        "OTHER": "x",
    }
    assert env_overrides(env) == {
        "llm": {"base_url": "http://localhost:8000/v1"},
        "budget": {"daily": {"usd": 1.5}},
    }
    s = load_settings(None, environ=env)
    assert s.budget.daily.usd == 1.5


def test_invalid_settings_raise_config_error(tmp_path):
    path = tmp_path / "decapod.toml"
    path.write_text("[llm]\nmax_retries = 99\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="max_retries"):
        load_settings(path, environ={})
    path.write_text("not = [valid", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_settings(path, environ={})
    path.write_text("[nonsense]\na=1\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_settings(path, environ={})


def test_charter_parse():
    charter = parse_charter(CHARTER)
    assert charter.name == "Tidepool"
    assert len(charter.sha256) == 64
    assert charter.clauses()["value.2"] == "budget"
    assert charter.clauses()["prohibition.1"].startswith("Never")
    assert "Tidepool" in charter.disclosure_text()
    assert "Hard prohibitions" in charter.render()
    assert charter.compiled_patterns()[0].search("my PASSWORD is")


def test_charter_errors():
    with pytest.raises(ConfigError, match="mission"):
        parse_charter("[identity]\nname='x'\n")
    with pytest.raises(ConfigError, match="pattern"):
        parse_charter("[identity]\nmission='m'\n[prohibitions]\npatterns=['(']\n")


def test_paths(tmp_path, monkeypatch):
    paths = InstancePaths.resolve(tmp_path / "inst")
    paths.ensure()
    assert paths.drive.is_dir() and paths.logs.is_dir()
    assert paths.charter("charter.toml") == paths.root / "charter.toml"
    monkeypatch.setenv("DECAPOD_HOME", str(tmp_path / "home"))
    assert InstancePaths.resolve().root == (tmp_path / "home").resolve()


def test_secrets(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("MY_API_KEY=supersecretvalue\nSHORT_TOKEN=abc\n", encoding="utf-8")
    monkeypatch.delenv("MY_API_KEY", raising=False)
    secrets = Secrets.load(env)
    assert secrets.require("MY_API_KEY") == "supersecretvalue"
    assert secrets.get("MISSING") is None
    with pytest.raises(KeyError):
        secrets.require("MISSING")
    assert secrets.redact("key=supersecretvalue!") == "key=[REDACTED]!"
    assert secrets.redact("abc") == "abc"  # too short to be scrubbed
