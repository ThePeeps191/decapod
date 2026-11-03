from pathlib import Path

from decapod.config import InstanceConfig

CHARTER = """
[identity]
name = "Testpod"
mission = "Research tidal ecology and publish short briefings."

[scopes]
granted = ["web.read", "drive.*", "notes.*"]

[prohibitions]
rules = ["Never impersonate a human."]
"""

CONFIG = """
[schedule]
min_interval = "0s"
idle_interval = "0s"
error_backoff = "0s"

[caucus]
llm_triage = false
"""


def make_config(root: Path, charter: str = CHARTER, config: str = CONFIG) -> InstanceConfig:
    root.mkdir(parents=True, exist_ok=True)
    (root / "charter.toml").write_text(charter, encoding="utf-8")
    (root / "decapod.toml").write_text(config, encoding="utf-8")
    return InstanceConfig.load(root)
