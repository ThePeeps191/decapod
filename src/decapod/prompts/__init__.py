"""Prompt templates (Jinja2). Every template carries a version comment; the hash of each
rendered template set is recorded in events so decisions can be traced to prompt versions."""

from __future__ import annotations

import hashlib
from functools import cache, lru_cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

TEMPLATE_DIR = Path(__file__).parent / "templates"


@lru_cache(maxsize=1)
def environment() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        undefined=StrictUndefined,
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=False,
    )


def render(name: str, **values: Any) -> str:
    return environment().get_template(f"{name}.j2").render(**values).strip()


@cache
def template_hash(name: str) -> str:
    data = (TEMPLATE_DIR / f"{name}.j2").read_bytes()
    return hashlib.sha256(data).hexdigest()[:12]


def templates() -> list[str]:
    return sorted(p.stem for p in TEMPLATE_DIR.glob("*.j2"))


__all__ = ["TEMPLATE_DIR", "environment", "render", "template_hash", "templates"]
