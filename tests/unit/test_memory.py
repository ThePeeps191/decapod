from datetime import timedelta

import pytest

from decapod.config.settings import MemorySettings
from decapod.domain import GoalStatus, MemoryKind, TaskStatus
from decapod.llm import hash_embedding
from decapod.memory import AgendaError, AgendaStore, MemoryStore, Section, TaskDraft, assemble
from decapod.memory.store import fts_query
from decapod.util.text import estimate_tokens
from decapod.util.timeutil import now

# --------------------------------------------------------------------------- agenda


def test_goal_crud(db, events):
    agenda = AgendaStore(db, events)
    goal = agenda.add_goal(
        "Write digest",
        "weekly",
        success_criteria=["published"],
        priority=2.0,
        charter_refs=["mission"],
    )
    assert goal.priority == 1.0 and goal.ref == "G1"
    updated = agenda.update_goal(goal.id, status="done", progress_note="shipped")
    assert updated.status is GoalStatus.DONE and updated.progress_note == "shipped"
    with pytest.raises(AgendaError):
        agenda.update_goal(goal.id, bogus=1)
    with pytest.raises(AgendaError):
        agenda.add_goal("  ")
    with pytest.raises(AgendaError):
        agenda.add_goal("child", parent_id=99)
    assert agenda.goals(open_only=True) == []
    assert "agenda.goal_added" in [e.kind for e in events.recent(5)]


def test_plan_dag_and_focus(db):
    agenda = AgendaStore(db)
    goal = agenda.add_goal("G", priority=0.9)
    low = agenda.add_goal("Low", priority=0.1)
    tasks = agenda.set_plan(
        goal.id,
        [
            TaskDraft(title="research"),
            TaskDraft(title="outline"),
            TaskDraft(title="write", depends_on=[1, 2]),
        ],
    )
    assert [t.title for t in agenda.ready_tasks(goal.id)] == ["research", "outline"]
    g, t = agenda.focus()
    assert g.id == goal.id and t.title == "research"
    agenda.update_task(tasks[0].id, TaskStatus.DONE)
    agenda.update_task(tasks[1].id, TaskStatus.IN_PROGRESS, "drafting")
    g, t = agenda.focus()
    assert t.id == tasks[1].id  # in-progress tasks come first
    agenda.update_task(tasks[1].id, "done")
    assert [x.title for x in agenda.ready_tasks(goal.id)] == ["write"]
    assert agenda.progress(goal.id) == (2, 3)
    text = agenda.render()
    assert "G1" in text and "(ready)" in text and "after T1, T2" in text
    assert low.ref in text


def test_replan_supersedes_open_tasks(db):
    agenda = AgendaStore(db)
    goal = agenda.add_goal("G")
    first = agenda.set_plan(goal.id, [TaskDraft(title="a"), TaskDraft(title="b")])
    agenda.update_task(first[0].id, "done")
    second = agenda.set_plan(goal.id, [TaskDraft(title="c")])
    assert second[0].plan_version == 2
    assert agenda.get_task(first[1].id).status is TaskStatus.SKIPPED
    assert agenda.get_task(first[0].id).status is TaskStatus.DONE
    assert [t.title for t in agenda.current_plan(goal.id)] == ["c"]


@pytest.mark.parametrize(
    "drafts",
    [
        [],
        [TaskDraft(title="a", depends_on=[1])],
        [TaskDraft(title="a", depends_on=[2]), TaskDraft(title="b", depends_on=[1])],
        [TaskDraft(title="a", depends_on=[5])],
    ],
)
def test_bad_plans(db, drafts):
    agenda = AgendaStore(db)
    goal = agenda.add_goal("G")
    with pytest.raises(AgendaError):
        agenda.set_plan(goal.id, drafts)


def test_task_attempts_and_add_task(db):
    agenda = AgendaStore(db)
    goal = agenda.add_goal("G")
    t = agenda.add_task(goal.id, "solo")
    t2 = agenda.add_task(goal.id, "after", depends_on=[t.id])
    assert t2.depends_on == [t.id]
    with pytest.raises(AgendaError):
        agenda.add_task(goal.id, "bad", depends_on=[999])
    agenda.update_task(t.id, "in_progress")
    agenda.update_task(t.id, "failed", "timeout")
    assert agenda.get_task(t.id).attempts == 2
    with pytest.raises(AgendaError):
        agenda.update_task(999, "done")


def test_empty_agenda_render(db):
    assert "empty" in AgendaStore(db).render()
    assert AgendaStore(db).focus() == (None, None)


# --------------------------------------------------------------------------- memory


@pytest.fixture
def store(db, events):
    return MemoryStore(db, MemorySettings(), events)


def test_fts_query():
    assert fts_query("The quick brown fox and the fox") == '"quick" OR "brown" OR "fox"'
    assert fts_query("the a of") is None


def test_add_and_search(store):
    store.add(MemoryKind.SEMANTIC, "Tide pools host anemones and sea stars", tags=["ecology"])
    store.add(
        MemoryKind.SEMANTIC,
        "The API rate-limits at 60 requests per minute",
        tags=["api"],
        importance=0.9,
    )
    store.add(MemoryKind.EPISODIC, "Fetched three articles about anemones", goal_id=1)
    hits = store.search("anemones", k=2)
    assert len(hits) == 2
    assert all("anemone" in h.content for h in hits)
    goal_hits = store.search("anemones", k=1, goal_id=1)
    assert goal_hits[0].goal_id == 1
    sem_only = store.search("anemones", kinds=[MemoryKind.SEMANTIC], k=5)
    assert all(m.kind is MemoryKind.SEMANTIC for m in sem_only)
    with pytest.raises(ValueError):
        store.add(MemoryKind.SEMANTIC, "   ")
    assert store.search("x", k=0) == []


def test_search_with_embeddings(store):
    store.add(
        MemoryKind.SEMANTIC,
        "zebra stripes pattern",
        embedding=hash_embedding("zebra stripes pattern"),
    )
    store.add(MemoryKind.SEMANTIC, "unrelated content", embedding=hash_embedding("unrelated"))
    hits = store.search("qqq", k=1, query_embedding=hash_embedding("zebra stripes"))
    assert hits[0].content == "zebra stripes pattern"


def test_lessons_prefer_useful(store):
    a = store.add(MemoryKind.REFLECTIVE, "Split big tasks into small steps", usefulness=0.1)
    b = store.add(MemoryKind.REFLECTIVE, "Split work before starting big tasks", usefulness=0.9)
    lessons = store.lessons("big tasks", k=1)
    assert lessons[0].id == b.id
    store.adjust_usefulness(a.id, 5)
    assert store.get(a.id).usefulness == 1.0


def test_supersede_and_compaction_groups(store):
    old = now() - timedelta(days=5)
    m1 = store.add(MemoryKind.EPISODIC, "did thing one", created_at=old)
    m2 = store.add(MemoryKind.EPISODIC, "did thing two", created_at=old)
    store.add(MemoryKind.EPISODIC, "recent thing")
    groups = store.compaction_groups(now() - timedelta(days=2))
    assert list(groups.values())[0] == [store.get(m1.id), store.get(m2.id)] or len(groups) == 1
    digest = store.add(MemoryKind.EPISODIC, "digest", tags=["digest"])
    store.supersede([m1.id, m2.id], digest.id)
    assert store.compaction_groups(now()) == {}
    assert all(m.id not in (m1.id, m2.id) for m in store.recent(limit=10))
    assert store.count(MemoryKind.EPISODIC) == 4


def test_like_fallback_without_fts(store):
    store.db.has_fts = False
    store.add(MemoryKind.SEMANTIC, "kelp forests are productive")
    assert store.search("kelp", k=1)[0].content.startswith("kelp")


# --------------------------------------------------------------------------- context


def test_assemble_respects_budget_and_priority():
    sections = [
        Section("Low", "x" * 4000, priority=9),
        Section("High", "important " * 50, priority=1),
        Section("Capped", "y" * 4000, priority=2, max_tokens=50),
    ]
    text = assemble(sections, 400)
    assert estimate_tokens(text) <= 420
    # output keeps declaration order; the low-priority section is the one that got truncated
    assert text.index("## Low") < text.index("## High") < text.index("## Capped")
    assert "truncated" in text.split("## High")[0]
    assert "## High" in text and "## Capped" in text
    assert assemble(sections, 0) == ""
    assert "(none)" in assemble([Section("Empty", "")], 100)
