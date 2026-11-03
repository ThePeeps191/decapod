from datetime import UTC, datetime, timedelta

import pytest

from decapod.domain import GoalStatus, TaskStatus
from decapod.memory import AgendaError, AgendaStore, TaskDraft
from decapod.memory.agenda import schedule_text
from decapod.util.timeutil import now, parse_when, to_iso


def test_parse_when():
    ref = datetime(2026, 1, 1, tzinfo=UTC)
    assert parse_when("3d", ref) == ref + timedelta(days=3)
    assert parse_when("2026-12-01") == datetime(2026, 12, 1, tzinfo=UTC)
    assert parse_when("2026-12-01T09:30+02:00").hour == 9
    with pytest.raises(ValueError):
        parse_when("next tuesday")


def test_recurring_goal_lifecycle(db, events):
    agenda = AgendaStore(db, events)
    goal = agenda.add_goal("Weekly digest", every=timedelta(days=7))
    assert goal.recurring and "recurs every 7d" in schedule_text(goal)
    agenda.set_plan(goal.id, [TaskDraft(title="research"), TaskDraft(title="write")])
    done = agenda.update_goal(goal.id, status="done")
    assert done.status is GoalStatus.DONE and done.completions == 1
    assert done.next_due is not None and done.next_due > now() + timedelta(days=6)
    assert agenda.reopen_due() == []  # not due yet
    db.execute(
        "UPDATE goals SET next_due = ? WHERE id = ?",
        (to_iso(now() - timedelta(minutes=1)), goal.id),
    )
    reopened = agenda.reopen_due()
    assert [g.id for g in reopened] == [goal.id]
    again = agenda.get_goal(goal.id)
    assert again.status is GoalStatus.ACTIVE and again.next_due is None
    assert "occurrence 2" in again.progress_note
    assert all(t.status is TaskStatus.SKIPPED for t in agenda.current_plan(goal.id))
    assert "agenda.goal_reopened" in [e.kind for e in events.recent(5)]


def test_recurrence_minimum_and_schedule_updates(db):
    agenda = AgendaStore(db)
    with pytest.raises(AgendaError):
        agenda.add_goal("Too often", every=timedelta(minutes=5))
    goal = agenda.add_goal("One-off", due_at=now() - timedelta(hours=1))
    assert [g.id for g in agenda.overdue()] == [goal.id]
    assert "OVERDUE" in schedule_text(goal)
    updated = agenda.update_goal(goal.id, every=timedelta(days=1), due_at=now() + timedelta(days=2))
    assert updated.recurring and "due in" in schedule_text(updated)
    cleared = agenda.update_goal(goal.id, clear_schedule=True)
    assert not cleared.recurring and cleared.due_at is None
    assert "schedule:" not in agenda.render()


async def test_schedule_tools_and_percepts(tmp_path):
    from decapod.domain import ActionSpec
    from decapod.evals.brain import scripted_brain
    from decapod.runtime import Instance
    from decapod.runtime.perceive import schedule_percepts
    from tests.helpers import make_config

    instance = Instance(make_config(tmp_path / "i"), provider=scripted_brain())
    await instance.start()
    ctx = instance.tool_context("c1")
    executor = instance.executor
    res = await executor.execute(
        ActionSpec(
            tool="agenda.add_goal",
            args={"title": "Daily notes", "charter_refs": ["mission"], "every": "1d", "due": "2h"},
        ),
        ctx,
    )
    assert res.ok, res.error
    _, check = instance.registry.check(
        ActionSpec(
            tool="agenda.add_goal",
            args={"title": "Spam", "charter_refs": ["mission"], "every": "10m"},
        ),
        ctx,
    )
    assert "at most once an hour" in check.args_error
    goal_id = res.data["goal_id"]
    instance.db.execute(
        "UPDATE goals SET due_at = ? WHERE id = ?", (to_iso(now() - timedelta(minutes=5)), goal_id)
    )
    first = schedule_percepts(instance)
    assert [p.kind for p in first] == ["goal_overdue"]
    assert schedule_percepts(instance) == []  # reported once
    res = await executor.execute(
        ActionSpec(tool="agenda.update_goal", args={"goal_id": goal_id, "status": "done"}), ctx
    )
    assert "next occurrence" in res.output
    instance.db.execute(
        "UPDATE goals SET next_due = ? WHERE id = ?",
        (to_iso(now() - timedelta(seconds=1)), goal_id),
    )
    assert [p.kind for p in schedule_percepts(instance)] == ["goal_due"]
    await instance.close()


def test_charter_seed_every():
    from decapod.config import parse_charter
    from decapod.domain import ConfigError

    charter = parse_charter('[identity]\nmission="m"\n[goals]\nseed=[{title="t", every="7d"}]\n')
    assert charter.goals.seed[0].every == "7d"
    with pytest.raises(ConfigError):
        parse_charter('[identity]\nmission="m"\n[goals]\nseed=[{title="t", every="1m"}]\n')
