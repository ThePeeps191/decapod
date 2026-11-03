import pytest

from decapod.logs import EventBus
from decapod.storage import Database, EventLog
from decapod.storage.db import _split_sql


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "t.db")
    yield database
    database.close()


def test_migrations_idempotent(tmp_path):
    path = tmp_path / "x.db"
    Database(path).close()
    with Database(path) as again:
        assert again.scalar("SELECT value FROM meta WHERE key='schema_version'") == "1"
        assert again.has_fts


def test_state_roundtrip(db):
    assert db.get_state("missing", 5) == 5
    db.set_state("k", {"a": [1, 2]})
    db.set_states({"x": 1, "y": "two"})
    assert db.get_state("k") == {"a": [1, 2]}
    assert db.all_state()["y"] == "two"


def test_transaction_rollback(db):
    with pytest.raises(RuntimeError), db.transaction():
        db.set_state("inside", 1)
        raise RuntimeError("boom")
    assert db.get_state("inside") is None
    with db.transaction(), db.transaction():  # nested joins outer
        db.set_state("nested", 1)
    assert db.get_state("nested") == 1


def test_eventlog_append_tail_and_bus(db):
    bus = EventBus()
    seen = []
    bus.subscribe(seen.append, "cycle.*")
    log = EventLog(db, bus, redact=lambda s: s.replace("hunter2", "[REDACTED]"))
    e1 = log.append("cycle.start", "start", {"n": 1}, cycle_id="c1")
    log.append("tool.result", "password hunter2", {"secret": "hunter2"}, cycle_id="c1")
    log.append("cycle.end", "end", cycle_id="c2")
    assert [e.kind for e in seen] == ["cycle.start", "cycle.end"]
    events = log.tail(after_id=e1.id)
    assert [e.kind for e in events] == ["tool.result", "cycle.end"]
    assert events[0].payload == {"secret": "[REDACTED]"}
    assert "[REDACTED]" in events[0].summary
    assert [e.kind for e in log.tail(kinds=["cycle.*"])] == ["cycle.start", "cycle.end"]
    assert len(log.for_cycle("c1")) == 2
    assert [e.kind for e in log.recent(2)] == ["tool.result", "cycle.end"]
    assert log.get(e1.id).payload == {"n": 1}


def test_eventlog_truncates_huge_payloads(db):
    log = EventLog(db)
    event = log.append("big", payload={"blob": "x" * 300_000})
    assert event.payload["truncated"] is True


def test_readonly_connection_sees_writes(tmp_path):
    path = tmp_path / "rw.db"
    writer = Database(path)
    EventLog(writer).append("hello")
    reader = Database(path, readonly=True)
    assert reader.scalar("SELECT COUNT(*) FROM events") == 1
    assert reader.has_fts
    reader.close()
    writer.close()


def test_split_sql_keeps_triggers():
    script = "CREATE TABLE a(x);\nCREATE TRIGGER t AFTER INSERT ON a BEGIN\n  SELECT 1;\nEND;\n"
    parts = _split_sql(script)
    assert len(parts) == 2
    assert parts[1].strip().endswith("END;")


def test_bus_queue_and_broken_subscriber():
    bus = EventBus()
    q = bus.queue("x*", maxsize=1)

    def broken(_):
        raise ValueError("nope")

    bus.subscribe(broken)
    from decapod.domain import Event

    bus.publish(Event(kind="x1"))
    bus.publish(Event(kind="x2"))  # drops oldest when full
    assert q.get_nowait().kind == "x2"
