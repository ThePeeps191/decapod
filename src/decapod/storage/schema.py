"""Database schema, applied as ordered migrations."""

from __future__ import annotations

MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    cycle_id  TEXT,
    kind      TEXT NOT NULL,
    actor     TEXT NOT NULL,
    summary   TEXT NOT NULL DEFAULT '',
    payload   TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS events_cycle ON events(cycle_id);
CREATE INDEX IF NOT EXISTS events_kind ON events(kind);

CREATE TABLE IF NOT EXISTS cycles (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    id           TEXT NOT NULL UNIQUE,
    started_at   TEXT NOT NULL,
    ended_at     TEXT,
    status       TEXT NOT NULL,
    level        INTEGER,
    phase        TEXT,
    decision     TEXT,
    result       TEXT,
    cost         TEXT,
    error        TEXT,
    charter_sha  TEXT
);

CREATE TABLE IF NOT EXISTS goals (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    title             TEXT NOT NULL,
    description       TEXT NOT NULL DEFAULT '',
    success_criteria  TEXT NOT NULL DEFAULT '[]',
    priority          REAL NOT NULL DEFAULT 0.5,
    status            TEXT NOT NULL,
    parent_id         INTEGER REFERENCES goals(id),
    rationale         TEXT NOT NULL DEFAULT '',
    charter_refs      TEXT NOT NULL DEFAULT '[]',
    progress_note     TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id       INTEGER NOT NULL REFERENCES goals(id),
    title         TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL,
    depends_on    TEXT NOT NULL DEFAULT '[]',
    ord           INTEGER NOT NULL DEFAULT 0,
    attempts      INTEGER NOT NULL DEFAULT 0,
    note          TEXT NOT NULL DEFAULT '',
    plan_version  INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tasks_goal ON tasks(goal_id);

CREATE TABLE IF NOT EXISTS memories (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    kind             TEXT NOT NULL,
    content          TEXT NOT NULL,
    tags             TEXT NOT NULL DEFAULT '',
    importance       REAL NOT NULL DEFAULT 0.5,
    confidence       REAL NOT NULL DEFAULT 1.0,
    usefulness       REAL NOT NULL DEFAULT 0.5,
    goal_id          INTEGER,
    cycle_id         TEXT,
    source_event_id  INTEGER,
    created_at       TEXT NOT NULL,
    last_access_at   TEXT,
    access_count     INTEGER NOT NULL DEFAULT 0,
    superseded_by    INTEGER,
    embedding        BLOB
);
CREATE INDEX IF NOT EXISTS memories_kind ON memories(kind, created_at);

CREATE TABLE IF NOT EXISTS ledger (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    cycle_id      TEXT,
    kind          TEXT NOT NULL,
    usd           REAL NOT NULL DEFAULT 0,
    tokens_in     INTEGER NOT NULL DEFAULT 0,
    tokens_out    INTEGER NOT NULL DEFAULT 0,
    llm_requests  INTEGER NOT NULL DEFAULT 0,
    tool_calls    INTEGER NOT NULL DEFAULT 0,
    outbound      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ledger_ts ON ledger(ts);
CREATE INDEX IF NOT EXISTS ledger_cycle ON ledger(cycle_id);

CREATE TABLE IF NOT EXISTS llm_calls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    cycle_id    TEXT,
    purpose     TEXT NOT NULL,
    tier        TEXT NOT NULL,
    model       TEXT NOT NULL,
    request     TEXT NOT NULL,
    response    TEXT,
    reasoning   TEXT,
    tokens_in   INTEGER NOT NULL DEFAULT 0,
    tokens_out  INTEGER NOT NULL DEFAULT 0,
    usd         REAL NOT NULL DEFAULT 0,
    latency_ms  INTEGER NOT NULL DEFAULT 0,
    ok          INTEGER NOT NULL,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS llm_calls_cycle ON llm_calls(cycle_id);

CREATE TABLE IF NOT EXISTS tool_calls (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts               TEXT NOT NULL,
    cycle_id         TEXT,
    tool             TEXT NOT NULL,
    args             TEXT NOT NULL,
    risk_tier        INTEGER NOT NULL,
    ok               INTEGER,
    output           TEXT,
    error            TEXT,
    duration_ms      INTEGER,
    idempotency_key  TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS tool_calls_tool_ts ON tool_calls(tool, ts);

CREATE TABLE IF NOT EXISTS approvals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    cycle_id     TEXT,
    action       TEXT NOT NULL,
    proposal     TEXT NOT NULL DEFAULT '{}',
    reason       TEXT NOT NULL,
    status       TEXT NOT NULL,
    resolved_at  TEXT,
    resolved_by  TEXT,
    note         TEXT NOT NULL DEFAULT '',
    result       TEXT,
    reported     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS approvals_status ON approvals(status);

CREATE TABLE IF NOT EXISTS control_commands (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    command       TEXT NOT NULL,
    args          TEXT NOT NULL DEFAULT '{}',
    issued_by     TEXT NOT NULL DEFAULT 'operator',
    status        TEXT NOT NULL DEFAULT 'pending',
    processed_at  TEXT,
    note          TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS operator_notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    text       TEXT NOT NULL,
    delivered  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS amendments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    cycle_id   TEXT,
    clause     TEXT NOT NULL DEFAULT '',
    text       TEXT NOT NULL,
    rationale  TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL DEFAULT 'proposed'
);

CREATE TABLE IF NOT EXISTS state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sensor_seen (
    sensor  TEXT NOT NULL,
    item    TEXT NOT NULL,
    ts      TEXT NOT NULL,
    PRIMARY KEY (sensor, item)
);
""",
    ),
]

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
    USING fts5(content, tags, content='memories', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content, tags) VALUES (new.id, new.content, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, tags)
        VALUES ('delete', old.id, old.content, old.tags);
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE OF content, tags ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, tags)
        VALUES ('delete', old.id, old.content, old.tags);
    INSERT INTO memories_fts(rowid, content, tags) VALUES (new.id, new.content, new.tags);
END;
"""
