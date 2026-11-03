"""Thin SQLite wrapper: WAL mode, migrations, explicit transactions."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from decapod.storage.schema import FTS_SCHEMA, MIGRATIONS
from decapod.util.text import json_dumps
from decapod.util.timeutil import now, to_iso

Params = Sequence[Any] | dict[str, Any]


class Database:
    """One SQLite connection per process component.

    The daemon owns the writer; the TUI and CLI open their own connections. WAL mode
    lets readers proceed while the daemon writes.
    """

    def __init__(self, path: str | Path, *, readonly: bool = False) -> None:
        self.path = str(path)
        self.readonly = readonly
        if readonly:
            uri = f"file:{Path(self.path).as_posix()}?mode=ro"
            self.conn = sqlite3.connect(
                uri, uri=True, isolation_level=None, check_same_thread=False, timeout=10
            )
        else:
            if self.path != ":memory:":
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(
                self.path, isolation_level=None, check_same_thread=False, timeout=10
            )
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._depth = 0
        self.has_fts = False
        self.conn.execute("PRAGMA busy_timeout = 10000")
        if not readonly:
            self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.execute("PRAGMA synchronous = NORMAL")
            self.conn.execute("PRAGMA foreign_keys = ON")
            self.migrate()
        else:
            self.has_fts = self._table_exists("memories_fts")

    # ------------------------------------------------------------------ lifecycle

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def migrate(self) -> None:
        with self._lock:
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            row = self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            current = int(row[0]) if row else 0
            for version, sql in MIGRATIONS:
                if version <= current:
                    continue
                with self.transaction():
                    for statement in _split_sql(sql):
                        self.conn.execute(statement)
                    self.conn.execute(
                        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (str(version),),
                    )
            self.has_fts = self._ensure_fts()

    def _ensure_fts(self) -> bool:
        if self._table_exists("memories_fts"):
            return True
        try:
            with self.transaction():
                for statement in _split_sql(FTS_SCHEMA):
                    self.conn.execute(statement)
                self.conn.execute("INSERT INTO memories_fts(memories_fts) VALUES ('rebuild')")
        except sqlite3.OperationalError:
            return False  # SQLite built without FTS5: memory falls back to LIKE search
        return True

    def _table_exists(self, name: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (name,)).fetchone()
        return row is not None

    # ------------------------------------------------------------------ queries

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Re-entrant transaction (nested calls join the outer transaction)."""
        with self._lock:
            if self._depth == 0:
                self.conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield self.conn
            except BaseException:
                self._depth -= 1
                if self._depth == 0:
                    self.conn.execute("ROLLBACK")
                raise
            else:
                self._depth -= 1
                if self._depth == 0:
                    self.conn.execute("COMMIT")

    def execute(self, sql: str, params: Params = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, params)

    def insert(self, sql: str, params: Params = ()) -> int:
        with self._lock:
            cur = self.conn.execute(sql, params)
            return int(cur.lastrowid or 0)

    def query(self, sql: str, params: Params = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: Params = ()) -> sqlite3.Row | None:
        with self._lock:
            row: sqlite3.Row | None = self.conn.execute(sql, params).fetchone()
            return row

    def scalar(self, sql: str, params: Params = (), default: Any = None) -> Any:
        row = self.query_one(sql, params)
        if row is None or row[0] is None:
            return default
        return row[0]

    # ------------------------------------------------------------------ key/value state

    def get_state(self, key: str, default: Any = None) -> Any:
        row = self.query_one("SELECT value FROM state WHERE key = ?", (key,))
        if row is None:
            return default
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            return default

    def set_state(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO state(key, value, updated_at) VALUES(?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, json_dumps(value), to_iso(now())),
        )

    def set_states(self, values: dict[str, Any]) -> None:
        with self.transaction():
            for key, value in values.items():
                self.set_state(key, value)

    def all_state(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for row in self.query("SELECT key, value FROM state"):
            try:
                out[row["key"]] = json.loads(row["value"])
            except json.JSONDecodeError:
                continue
        return out


def _split_sql(script: str) -> list[str]:
    """Split a schema script into statements, keeping trigger bodies intact."""
    statements: list[str] = []
    buf: list[str] = []
    in_trigger = False
    for line in script.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        buf.append(line)
        upper = stripped.upper()
        if upper.startswith("CREATE TRIGGER"):
            in_trigger = True
        if in_trigger:
            if upper == "END;":
                statements.append("\n".join(buf))
                buf = []
                in_trigger = False
        elif stripped.endswith(";"):
            statements.append("\n".join(buf))
            buf = []
    if buf:
        statements.append("\n".join(buf))
    return statements


def loads(value: str | None, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default
