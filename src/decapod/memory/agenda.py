"""The agenda: Decapod's own goals and their task plans."""

from __future__ import annotations

from typing import Any

import networkx as nx
from pydantic import BaseModel, Field

from decapod.domain import Goal, GoalStatus, Task, TaskStatus
from decapod.storage import Database, EventLog, loads
from decapod.util.text import json_dumps, one_line
from decapod.util.timeutil import from_iso, now, to_iso


class TaskDraft(BaseModel):
    title: str
    description: str = ""
    #: 1-based indexes of earlier tasks in the same plan that must finish first.
    depends_on: list[int] = Field(default_factory=list)


class AgendaError(ValueError):
    pass


def _goal(row: Any) -> Goal:
    return Goal(
        id=row["id"],
        title=row["title"],
        description=row["description"],
        success_criteria=loads(row["success_criteria"], []),
        priority=row["priority"],
        status=GoalStatus(row["status"]),
        parent_id=row["parent_id"],
        rationale=row["rationale"],
        charter_refs=loads(row["charter_refs"], []),
        progress_note=row["progress_note"],
        created_at=from_iso(row["created_at"]),
        updated_at=from_iso(row["updated_at"]),
    )


def _task(row: Any) -> Task:
    return Task(
        id=row["id"],
        goal_id=row["goal_id"],
        title=row["title"],
        description=row["description"],
        status=TaskStatus(row["status"]),
        depends_on=loads(row["depends_on"], []),
        order=row["ord"],
        attempts=row["attempts"],
        note=row["note"],
        plan_version=row["plan_version"],
        created_at=from_iso(row["created_at"]),
        updated_at=from_iso(row["updated_at"]),
    )


_GOAL_FIELDS = {
    "title",
    "description",
    "success_criteria",
    "priority",
    "status",
    "parent_id",
    "rationale",
    "charter_refs",
    "progress_note",
}


class AgendaStore:
    def __init__(self, db: Database, events: EventLog | None = None) -> None:
        self.db = db
        self.events = events

    def _emit(self, kind: str, summary: str, payload: dict[str, Any], cycle_id: str | None) -> None:
        if self.events is not None:
            self.events.append(kind, summary, payload, actor="agenda", cycle_id=cycle_id)

    # ------------------------------------------------------------------ goals

    def add_goal(
        self,
        title: str,
        description: str = "",
        *,
        success_criteria: list[str] | None = None,
        priority: float = 0.5,
        status: GoalStatus = GoalStatus.ACTIVE,
        parent_id: int | None = None,
        rationale: str = "",
        charter_refs: list[str] | None = None,
        cycle_id: str | None = None,
    ) -> Goal:
        title = title.strip()
        if not title:
            raise AgendaError("goal title must not be empty")
        if parent_id is not None and self.get_goal(parent_id) is None:
            raise AgendaError(f"parent goal G{parent_id} does not exist")
        ts = to_iso(now())
        goal_id = self.db.insert(
            "INSERT INTO goals(title, description, success_criteria, priority, status, parent_id,"
            " rationale, charter_refs, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                title,
                description,
                json_dumps(success_criteria or []),
                max(0.0, min(1.0, priority)),
                status.value,
                parent_id,
                rationale,
                json_dumps(charter_refs or []),
                ts,
                ts,
            ),
        )
        goal = self.get_goal(goal_id)
        assert goal is not None
        self._emit(
            "agenda.goal_added", f"{goal.ref} {goal.title}", goal.model_dump(mode="json"), cycle_id
        )
        return goal

    def get_goal(self, goal_id: int) -> Goal | None:
        row = self.db.query_one("SELECT * FROM goals WHERE id = ?", (goal_id,))
        return _goal(row) if row else None

    def require_goal(self, goal_id: int) -> Goal:
        goal = self.get_goal(goal_id)
        if goal is None:
            raise AgendaError(f"goal G{goal_id} does not exist")
        return goal

    def update_goal(self, goal_id: int, *, cycle_id: str | None = None, **fields: Any) -> Goal:
        self.require_goal(goal_id)
        unknown = set(fields) - _GOAL_FIELDS
        if unknown:
            raise AgendaError(f"unknown goal fields: {', '.join(sorted(unknown))}")
        updates: dict[str, Any] = {}
        for key, value in fields.items():
            if value is None and key != "parent_id":
                continue
            if key in ("success_criteria", "charter_refs"):
                value = json_dumps(list(value))
            elif key == "status":
                value = GoalStatus(value).value
            elif key == "priority":
                value = max(0.0, min(1.0, float(value)))
            updates[key] = value
        if updates:
            updates["updated_at"] = to_iso(now())
            assignments = ", ".join(f"{k} = ?" for k in updates)
            self.db.execute(
                f"UPDATE goals SET {assignments} WHERE id = ?", (*updates.values(), goal_id)
            )
        goal = self.require_goal(goal_id)
        if updates:
            self._emit(
                "agenda.goal_updated",
                f"{goal.ref} {goal.status.value}: {goal.title}",
                {"goal_id": goal_id, "changes": list(updates)},
                cycle_id,
            )
        return goal

    def goals(self, *, open_only: bool = False, status: GoalStatus | None = None) -> list[Goal]:
        sql = "SELECT * FROM goals"
        params: list[Any] = []
        if status is not None:
            sql += " WHERE status = ?"
            params.append(status.value)
        elif open_only:
            sql += " WHERE status IN (?, ?, ?)"
            params += [GoalStatus.PROPOSED.value, GoalStatus.ACTIVE.value, GoalStatus.BLOCKED.value]
        sql += " ORDER BY priority DESC, id ASC"
        return [_goal(r) for r in self.db.query(sql, params)]

    def active_goals(self) -> list[Goal]:
        return self.goals(status=GoalStatus.ACTIVE)

    # ------------------------------------------------------------------ tasks

    def get_task(self, task_id: int) -> Task | None:
        row = self.db.query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        return _task(row) if row else None

    def tasks(self, goal_id: int, *, include_closed: bool = True) -> list[Task]:
        rows = self.db.query(
            "SELECT * FROM tasks WHERE goal_id = ? ORDER BY plan_version, ord, id", (goal_id,)
        )
        out = [_task(r) for r in rows]
        if not include_closed:
            out = [t for t in out if t.status.is_open]
        return out

    def current_plan(self, goal_id: int) -> list[Task]:
        version = self.db.scalar(
            "SELECT MAX(plan_version) FROM tasks WHERE goal_id = ?", (goal_id,), 0
        )
        return [t for t in self.tasks(goal_id) if t.plan_version == version]

    def add_task(
        self,
        goal_id: int,
        title: str,
        description: str = "",
        *,
        depends_on: list[int] | None = None,
        cycle_id: str | None = None,
    ) -> Task:
        self.require_goal(goal_id)
        deps = list(depends_on or [])
        for dep in deps:
            dep_task = self.get_task(dep)
            if dep_task is None or dep_task.goal_id != goal_id:
                raise AgendaError(f"dependency T{dep} is not a task of G{goal_id}")
        version = self.db.scalar(
            "SELECT MAX(plan_version) FROM tasks WHERE goal_id = ?", (goal_id,), 1
        )
        order = self.db.scalar("SELECT MAX(ord) FROM tasks WHERE goal_id = ?", (goal_id,), 0) + 1
        ts = to_iso(now())
        task_id = self.db.insert(
            "INSERT INTO tasks(goal_id, title, description, status, depends_on, ord, plan_version,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                goal_id,
                title.strip(),
                description,
                TaskStatus.TODO.value,
                json_dumps(deps),
                order,
                version,
                ts,
                ts,
            ),
        )
        task = self.get_task(task_id)
        assert task is not None
        self._emit(
            "agenda.task_added",
            f"{task.ref} for G{goal_id}: {task.title}",
            task.model_dump(mode="json"),
            cycle_id,
        )
        return task

    def set_plan(
        self, goal_id: int, drafts: list[TaskDraft], *, cycle_id: str | None = None
    ) -> list[Task]:
        """Replace the open tasks of a goal with a new plan version (old ones are kept)."""
        self.require_goal(goal_id)
        if not drafts:
            raise AgendaError("a plan needs at least one task")
        graph: nx.DiGraph[int] = nx.DiGraph()
        for i, draft in enumerate(drafts, 1):
            graph.add_node(i)
            for dep in draft.depends_on:
                if dep < 1 or dep >= i:
                    raise AgendaError(
                        f"task {i} depends on invalid step {dep} (a step may only depend on "
                        "earlier steps)"
                    )
                graph.add_edge(dep, i)
        if not nx.is_directed_acyclic_graph(graph):
            raise AgendaError("plan dependencies contain a cycle")
        version = (
            self.db.scalar("SELECT MAX(plan_version) FROM tasks WHERE goal_id = ?", (goal_id,), 0)
            + 1
        )
        ts = to_iso(now())
        created: list[Task] = []
        with self.db.transaction():
            self.db.execute(
                "UPDATE tasks SET status = ?, note = ?, updated_at = ? "
                "WHERE goal_id = ? AND status IN (?, ?)",
                (
                    TaskStatus.SKIPPED.value,
                    f"superseded by plan v{version}",
                    ts,
                    goal_id,
                    TaskStatus.TODO.value,
                    TaskStatus.IN_PROGRESS.value,
                ),
            )
            ids: dict[int, int] = {}
            for i, draft in enumerate(drafts, 1):
                deps = [ids[d] for d in draft.depends_on]
                ids[i] = self.db.insert(
                    "INSERT INTO tasks(goal_id, title, description, status, depends_on, ord,"
                    " plan_version, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        goal_id,
                        draft.title.strip(),
                        draft.description,
                        TaskStatus.TODO.value,
                        json_dumps(deps),
                        i,
                        version,
                        ts,
                        ts,
                    ),
                )
            for task_id in ids.values():
                task = self.get_task(task_id)
                assert task is not None
                created.append(task)
        self._emit(
            "agenda.plan_set",
            f"G{goal_id} plan v{version}: {len(created)} tasks",
            {"goal_id": goal_id, "version": version, "tasks": [t.title for t in created]},
            cycle_id,
        )
        return created

    def update_task(
        self,
        task_id: int,
        status: TaskStatus | str | None = None,
        note: str = "",
        *,
        cycle_id: str | None = None,
    ) -> Task:
        task = self.get_task(task_id)
        if task is None:
            raise AgendaError(f"task T{task_id} does not exist")
        new_status = TaskStatus(status) if status is not None else task.status
        attempts = task.attempts + (
            1
            if new_status in (TaskStatus.IN_PROGRESS, TaskStatus.FAILED)
            and new_status is not task.status
            else 0
        )
        self.db.execute(
            "UPDATE tasks SET status = ?, note = ?, attempts = ?, updated_at = ? WHERE id = ?",
            (new_status.value, note or task.note, attempts, to_iso(now()), task_id),
        )
        updated = self.get_task(task_id)
        assert updated is not None
        self._emit(
            "agenda.task_updated",
            f"{updated.ref} → {updated.status.value}"
            + (f": {one_line(note, 100)}" if note else ""),
            {"task_id": task_id, "status": updated.status.value, "note": note},
            cycle_id,
        )
        return updated

    def ready_tasks(self, goal_id: int) -> list[Task]:
        plan = self.current_plan(goal_id)
        finished = {
            t.id for t in self.tasks(goal_id) if t.status in (TaskStatus.DONE, TaskStatus.SKIPPED)
        }
        return [t for t in plan if t.status.is_open and all(d in finished for d in t.depends_on)]

    def focus(self) -> tuple[Goal | None, Task | None]:
        """The highest-priority active goal and its next ready task."""
        for goal in self.active_goals():
            ready = self.ready_tasks(goal.id)
            in_progress = [t for t in ready if t.status is TaskStatus.IN_PROGRESS]
            if in_progress:
                return goal, in_progress[0]
            if ready:
                return goal, ready[0]
        goals = self.active_goals()
        return (goals[0] if goals else None), None

    def progress(self, goal_id: int) -> tuple[int, int]:
        plan = self.current_plan(goal_id)
        done = sum(1 for t in plan if t.status in (TaskStatus.DONE, TaskStatus.SKIPPED))
        return done, len(plan)

    # ------------------------------------------------------------------ rendering

    def render(self, max_goals: int = 6, max_tasks: int = 8) -> str:
        goals = self.goals(open_only=True)
        if not goals:
            return "(the agenda is empty: no open goals)"
        lines: list[str] = []
        for goal in goals[:max_goals]:
            done, total = self.progress(goal.id)
            lines.append(
                f"{goal.ref} [{goal.status.value}, priority {goal.priority:.2f}, "
                f"{done}/{total} tasks] {goal.title}"
            )
            if goal.description:
                lines.append(f"    {one_line(goal.description, 200)}")
            if goal.success_criteria:
                lines.append("    success: " + "; ".join(goal.success_criteria)[:240])
            if goal.progress_note:
                lines.append(f"    progress: {one_line(goal.progress_note, 200)}")
            plan = self.current_plan(goal.id)
            ready = {t.id for t in self.ready_tasks(goal.id)}
            for task in plan[:max_tasks]:
                flag = " (ready)" if task.id in ready and task.status is TaskStatus.TODO else ""
                deps = (
                    f" after {', '.join(f'T{d}' for d in task.depends_on)}"
                    if task.depends_on
                    else ""
                )
                note = f" — {one_line(task.note, 80)}" if task.note else ""
                lines.append(f"    {task.ref} [{task.status.value}]{flag}{deps} {task.title}{note}")
            if len(plan) > max_tasks:
                lines.append(f"    … {len(plan) - max_tasks} more tasks")
        if len(goals) > max_goals:
            lines.append(f"… {len(goals) - max_goals} more open goals")
        return "\n".join(lines)
