"""The append-only event log: the single source of truth for what Decapod did."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from decapod.domain import Event
from decapod.logs.bus import EventBus
from decapod.storage.db import Database, loads
from decapod.util.text import json_dumps, one_line
from decapod.util.timeutil import from_iso, now, to_iso

Redactor = Callable[[str], str]

#: Maximum size of a single serialized payload. Bigger payloads are truncated, not dropped.
MAX_PAYLOAD_CHARS = 200_000


class EventLog:
    def __init__(
        self, db: Database, bus: EventBus | None = None, redact: Redactor | None = None
    ) -> None:
        self.db = db
        self.bus = bus or EventBus()
        self._redact = redact or (lambda s: s)

    def redact(self, text: str) -> str:
        """Apply the secret redactor (used for anything persisted next to events)."""
        return self._redact(text)

    def append(
        self,
        kind: str,
        summary: str = "",
        payload: dict[str, Any] | None = None,
        *,
        actor: str = "system",
        cycle_id: str | None = None,
    ) -> Event:
        ts = now()
        body = self._redact(json_dumps(payload or {}))
        if len(body) > MAX_PAYLOAD_CHARS:
            body = json_dumps({"truncated": True, "preview": body[:MAX_PAYLOAD_CHARS]})
        summary = self._redact(one_line(summary, 300))
        event_id = self.db.insert(
            "INSERT INTO events(ts, cycle_id, kind, actor, summary, payload) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (to_iso(ts), cycle_id, kind, actor, summary, body),
        )
        event = Event(
            id=event_id,
            ts=ts,
            cycle_id=cycle_id,
            kind=kind,
            actor=actor,
            summary=summary,
            payload=loads(body, {}),
        )
        self.bus.publish(event)
        return event

    # ------------------------------------------------------------------ reading

    def tail(
        self,
        after_id: int = 0,
        limit: int = 500,
        kinds: Iterable[str] | None = None,
        cycle_id: str | None = None,
    ) -> list[Event]:
        return read_events(self.db, after_id=after_id, limit=limit, kinds=kinds, cycle_id=cycle_id)

    def recent(self, limit: int = 50, kinds: Iterable[str] | None = None) -> list[Event]:
        return read_recent(self.db, limit=limit, kinds=kinds)

    def for_cycle(self, cycle_id: str) -> list[Event]:
        return read_events(self.db, cycle_id=cycle_id, limit=100_000)

    def get(self, event_id: int) -> Event | None:
        row = self.db.query_one("SELECT * FROM events WHERE id = ?", (event_id,))
        return row_to_event(row) if row else None


def row_to_event(row: Any) -> Event:
    return Event(
        id=row["id"],
        ts=from_iso(row["ts"]),
        cycle_id=row["cycle_id"],
        kind=row["kind"],
        actor=row["actor"],
        summary=row["summary"],
        payload=loads(row["payload"], {}),
    )


def _kind_clause(kinds: Iterable[str] | None) -> tuple[str, list[Any]]:
    if not kinds:
        return "", []
    parts = []
    params: list[Any] = []
    for kind in kinds:
        if kind.endswith("*"):
            parts.append("kind LIKE ?")
            params.append(kind[:-1] + "%")
        else:
            parts.append("kind = ?")
            params.append(kind)
    return " AND (" + " OR ".join(parts) + ")", params


def read_events(
    db: Database,
    *,
    after_id: int = 0,
    limit: int = 500,
    kinds: Iterable[str] | None = None,
    cycle_id: str | None = None,
) -> list[Event]:
    sql = "SELECT * FROM events WHERE id > ?"
    params: list[Any] = [after_id]
    clause, extra = _kind_clause(kinds)
    sql += clause
    params += extra
    if cycle_id is not None:
        sql += " AND cycle_id = ?"
        params.append(cycle_id)
    sql += " ORDER BY id ASC LIMIT ?"
    params.append(limit)
    return [row_to_event(r) for r in db.query(sql, params)]


def read_recent(
    db: Database, *, limit: int = 50, kinds: Iterable[str] | None = None
) -> list[Event]:
    clause, params = _kind_clause(kinds)
    sql = f"SELECT * FROM events WHERE 1=1{clause} ORDER BY id DESC LIMIT ?"
    rows = db.query(sql, [*params, limit])
    return [row_to_event(r) for r in reversed(rows)]
