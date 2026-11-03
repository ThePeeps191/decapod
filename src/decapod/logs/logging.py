"""Program logging (for debugging Decapod itself; agent behaviour lives in events)."""

from __future__ import annotations

import json
import logging
import logging.handlers
from pathlib import Path

from decapod.util.timeutil import now, to_iso


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {
            "ts": to_iso(now()),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False)


def setup_logging(
    log_dir: Path | None = None, *, level: str = "INFO", console: bool = True
) -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())
    for handler in list(root.handlers):
        if getattr(handler, "_decapod", False):
            root.removeHandler(handler)
    if console:
        from rich.logging import RichHandler

        rich_handler = RichHandler(rich_tracebacks=True, show_path=False, markup=False)
        rich_handler.setLevel(level.upper())
        rich_handler._decapod = True  # type: ignore[attr-defined]
        root.addHandler(rich_handler)
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / "decapod.jsonl", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(JsonFormatter())
        file_handler._decapod = True  # type: ignore[attr-defined]
        root.addHandler(file_handler)
    for noisy in ("httpx", "httpcore", "openai", "markdown_it", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
