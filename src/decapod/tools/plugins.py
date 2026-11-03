"""Third-party tools registered through the ``decapod.tools`` entry point group."""

from __future__ import annotations

from collections.abc import Callable
from importlib.metadata import entry_points

from decapod.tools.base import Tool


def plugin_tools(on_error: Callable[[str, Exception], None] | None = None) -> list[Tool]:
    """Load plugin tools. Each entry point may name a Tool subclass, a Tool instance, or a
    callable returning a list of tools. A broken plugin is reported and skipped."""
    found: list[Tool] = []
    for entry in entry_points(group="decapod.tools"):
        try:
            obj = entry.load()
            if isinstance(obj, type) and issubclass(obj, Tool):
                found.append(obj())
            elif isinstance(obj, Tool):
                found.append(obj)
            elif callable(obj):
                found.extend(t for t in obj() if isinstance(t, Tool))
        except Exception as exc:
            if on_error is not None:
                on_error(entry.name, exc)
    return found
