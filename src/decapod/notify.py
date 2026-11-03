"""Push notifications: ntfy, Slack, Discord or any JSON webhook.

The daemon subscribes a Notifier to its event bus. Delivery is best-effort and never
blocks or breaks the agent; failures are logged.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from decapod.config import Secrets
from decapod.config.settings import NotifySettings
from decapod.domain import Event
from decapod.util.text import one_line

log = logging.getLogger(__name__)
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
UNTHROTTLED = ("approval.requested",)


@dataclass(frozen=True)
class Message:
    title: str
    body: str
    kind: str
    priority: str = "default"  # ntfy priorities: min, low, default, high, urgent


def expand(target: str, secrets: Secrets) -> str:
    return _VAR_RE.sub(lambda m: secrets.get(m.group(1), "") or "", target)


def describe(event: Event, instance: str) -> Message:
    payload = event.payload
    kind = event.kind
    if kind == "approval.requested":
        return Message(
            f"{instance}: approval needed",
            f"#{payload.get('approval_id')} {event.summary}\n"
            f"{payload.get('reason', '')}\nRun `decapod approvals` or open the TUI.",
            kind,
            "high",
        )
    if kind == "daemon.stop":
        state = payload.get("state", "stopped")
        return Message(
            f"{instance}: {state}",
            one_line(str(payload.get("reason") or event.summary), 300),
            kind,
            "high" if state == "killed" else "default",
        )
    if kind in ("budget.exhausted", "control.trip", "cycle.error"):
        return Message(
            f"{instance}: {kind.replace('.', ' ')}", one_line(event.summary, 400), kind, "high"
        )
    if kind == "report.created":
        return Message(f"{instance}: progress report", event.summary, kind, "low")
    return Message(f"{instance}: {kind}", one_line(event.summary, 400), kind)


def request_for(target: str, message: Message) -> tuple[str, dict[str, Any]]:
    """(url, httpx request kwargs) for a target URL."""
    if target.startswith("ntfy:"):
        target = "https://ntfy.sh/" + target[5:].lstrip("/")
    host = (urlparse(target).hostname or "").lower()
    if "ntfy" in host:
        return target, {
            "content": message.body.encode("utf-8"),
            "headers": {
                "Title": message.title.encode("utf-8").decode("latin-1", "ignore"),
                "Priority": message.priority,
                "Tags": "crab",
            },
        }
    if host == "hooks.slack.com":
        return target, {"json": {"text": f"*{message.title}*\n{message.body}"}}
    if host in ("discord.com", "discordapp.com") and "/api/webhooks/" in target:
        return target, {"json": {"content": f"**{message.title}**\n{message.body}"[:1900]}}
    return target, {
        "json": {
            "title": message.title,
            "message": message.body,
            "kind": message.kind,
            "priority": message.priority,
        }
    }


class Notifier:
    def __init__(
        self,
        settings: NotifySettings,
        secrets: Secrets,
        http: httpx.AsyncClient,
        instance: str,
        clock: Any = time.monotonic,
    ) -> None:
        self.settings = settings
        self.secrets = secrets
        self.http = http
        self.instance = instance
        self.clock = clock
        self._last: dict[str, float] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self.sent: list[tuple[str, Message]] = []
        self.errors: list[str] = []

    @property
    def enabled(self) -> bool:
        return bool(self.settings.targets)

    def wants(self, event: Event) -> bool:
        if not any(fnmatch.fnmatchcase(event.kind, p) for p in self.settings.events):
            return False
        if event.kind in UNTHROTTLED:
            return True
        current = self.clock()
        last = self._last.get(event.kind)
        if last is not None and current - last < self.settings.min_interval.total_seconds():
            return False
        self._last[event.kind] = current
        return True

    def handle(self, event: Event) -> None:
        """Event-bus subscriber: schedule delivery without blocking the agent."""
        if not self.enabled or not self.wants(event):
            return
        try:
            task = asyncio.get_running_loop().create_task(self.send(describe(event, self.instance)))
        except RuntimeError:  # no running loop (e.g. a synchronous CLI context)
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def send(self, message: Message) -> list[str]:
        errors: list[str] = []
        for raw in self.settings.targets:
            target = expand(raw, self.secrets)
            try:
                url, kwargs = request_for(target, message)
                response = await self.http.post(url, timeout=15, **kwargs)
                if response.status_code >= 400:
                    errors.append(f"{_safe(url)}: HTTP {response.status_code}")
                else:
                    self.sent.append((_safe(url), message))
            except (httpx.HTTPError, ValueError) as exc:
                errors.append(f"{_safe(target)}: {exc}")
        for error in errors:
            log.warning("notification failed: %s", error)
        self.errors.extend(errors)
        return errors

    async def drain(self, wait_s: float = 10.0) -> None:
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=wait_s)


def _safe(url: str) -> str:
    """Hide webhook secrets (path tokens) when logging."""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.hostname}/…" if parsed.hostname else "target"
