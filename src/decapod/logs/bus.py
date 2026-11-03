"""In-process event bus. Every subsystem publishes; the event log persists."""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import logging
from collections.abc import Callable

from decapod.domain import Event

Subscriber = Callable[[Event], None]
log = logging.getLogger(__name__)


class EventBus:
    def __init__(self) -> None:
        self._subscribers: list[tuple[str, Subscriber]] = []
        self._queues: list[tuple[str, asyncio.Queue[Event]]] = []

    def subscribe(self, callback: Subscriber, pattern: str = "*") -> Callable[[], None]:
        entry = (pattern, callback)
        self._subscribers.append(entry)

        def unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._subscribers.remove(entry)

        return unsubscribe

    def queue(self, pattern: str = "*", maxsize: int = 1000) -> asyncio.Queue[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)
        self._queues.append((pattern, q))
        return q

    def publish(self, event: Event) -> None:
        for pattern, callback in list(self._subscribers):
            if fnmatch.fnmatchcase(event.kind, pattern):
                try:
                    callback(event)
                except Exception:  # a broken subscriber must never break the agent
                    log.exception("event subscriber failed for %s", event.kind)
        for pattern, q in list(self._queues):
            if fnmatch.fnmatchcase(event.kind, pattern):
                if q.full():
                    with contextlib.suppress(asyncio.QueueEmpty):
                        q.get_nowait()
                q.put_nowait(event)
