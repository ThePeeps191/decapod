"""Console rendering shared by CLI commands."""

from __future__ import annotations

from datetime import datetime

from rich.console import Console
from rich.markup import escape

from decapod.domain import Event

console = Console(highlight=False, soft_wrap=True)
err = Console(stderr=True, highlight=False, soft_wrap=True)

KIND_STYLES = (
    ("cycle.error", "bold red"),
    ("cycle.", "bold cyan"),
    ("caucus.decision", "bold green"),
    ("caucus.inadmissible", "yellow"),
    ("caucus.", "magenta"),
    ("tool.result", "green"),
    ("tool.", "blue"),
    ("policy.", "yellow"),
    ("approval.", "bold yellow"),
    ("control.", "bold red"),
    ("budget.", "bold red"),
    ("llm.error", "red"),
    ("llm.", "dim"),
    ("memory.", "dim cyan"),
    ("agenda.", "cyan"),
    ("percept", "white"),
    ("daemon.", "bold white"),
)


def style_for(kind: str) -> str:
    for prefix, style in KIND_STYLES:
        if kind.startswith(prefix):
            return style
    return "white"


def format_event(event: Event, *, show_cycle: bool = True) -> str:
    ts = event.ts.astimezone().strftime("%H:%M:%S")
    style = style_for(event.kind)
    cycle = f"[dim]{event.cycle_id or '      '}[/] " if show_cycle else ""
    return (
        f"[dim]{ts}[/] {cycle}[{style}]{event.kind:<22}[/] "
        f"[dim]{escape(event.actor):<18}[/] {escape(event.summary)}"
    )


def print_event(event: Event, *, verbose: bool = False) -> None:
    if not verbose and event.kind in (
        "llm.call",
        "memory.episodic",
        "percept",
        "cycle.context",
        "daemon.sleep",
    ):
        return
    console.print(format_event(event))


def ago(timestamp: float | None) -> str:
    if not timestamp:
        return "never"
    delta = datetime.now().timestamp() - float(timestamp)
    if delta < 0:
        return f"in {_span(-delta)}"
    return f"{_span(delta)} ago"


def _span(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    if seconds < 86400:
        return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"
    return f"{seconds // 86400}d {seconds % 86400 // 3600:02d}h"
