"""Where an instance keeps its files."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_data_dir

DEFAULT_INSTANCE = "default"


def default_root(instance: str | None = None) -> Path:
    """Resolve an instance directory.

    Order: ``DECAPOD_HOME`` → platform data dir / instances / ``instance``
    (``DECAPOD_INSTANCE`` or ``default``).
    """
    home = os.environ.get("DECAPOD_HOME")
    if home:
        return Path(home).expanduser().resolve()
    name = instance or os.environ.get("DECAPOD_INSTANCE") or DEFAULT_INSTANCE
    return Path(user_data_dir("decapod", appauthor=False)) / "instances" / name


@dataclass(frozen=True)
class InstancePaths:
    root: Path

    @classmethod
    def resolve(cls, root: str | Path | None = None, instance: str | None = None) -> InstancePaths:
        path = Path(root).expanduser().resolve() if root else default_root(instance)
        return cls(path)

    @property
    def config(self) -> Path:
        return self.root / "decapod.toml"

    @property
    def env(self) -> Path:
        return self.root / ".env"

    @property
    def db(self) -> Path:
        return self.root / "decapod.db"

    @property
    def drive(self) -> Path:
        return self.root / "drive"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def pidfile(self) -> Path:
        return self.root / "decapod.pid"

    @property
    def stop_file(self) -> Path:
        return self.root / "STOP"

    @property
    def daemon_log(self) -> Path:
        return self.logs / "daemon.log"

    def charter(self, name: str) -> Path:
        path = Path(name).expanduser()
        return path if path.is_absolute() else self.root / path

    def ensure(self) -> None:
        for directory in (self.root, self.drive, self.logs):
            directory.mkdir(parents=True, exist_ok=True)

    def exists(self) -> bool:
        return self.config.exists()
