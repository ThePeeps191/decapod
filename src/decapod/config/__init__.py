"""Configuration: settings, charter, instance paths and secrets."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from decapod.config.charter import Charter, load_charter, parse_charter
from decapod.config.paths import InstancePaths, default_root
from decapod.config.secrets import Secrets
from decapod.config.settings import (
    BudgetLimits,
    BudgetSettings,
    MCPServerSettings,
    Settings,
    load_settings,
)


@dataclass
class InstanceConfig:
    """Everything needed to run one instance, loaded from disk."""

    paths: InstancePaths
    settings: Settings
    charter: Charter
    secrets: Secrets

    @classmethod
    def load(cls, root: str | Path | None = None, instance: str | None = None) -> InstanceConfig:
        paths = InstancePaths.resolve(root, instance)
        settings = load_settings(paths.config)
        charter = load_charter(paths.charter(settings.instance.charter))
        secrets = Secrets.load(paths.env)
        return cls(paths=paths, settings=settings, charter=charter, secrets=secrets)

    def reload_charter(self) -> Charter:
        self.charter = load_charter(self.paths.charter(self.settings.instance.charter))
        return self.charter


__all__ = [
    "BudgetLimits",
    "BudgetSettings",
    "Charter",
    "InstanceConfig",
    "InstancePaths",
    "MCPServerSettings",
    "Secrets",
    "Settings",
    "default_root",
    "load_charter",
    "load_settings",
    "parse_charter",
]
