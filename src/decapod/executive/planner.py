"""Planning heuristics: cheap, deterministic hints that steer the Planner specialist."""

from __future__ import annotations

from decapod.domain import TaskStatus
from decapod.memory import AgendaStore


def planner_notes(agenda: AgendaStore, *, max_notes: int = 6) -> list[str]:
    notes: list[str] = []
    for goal in agenda.active_goals():
        plan = agenda.current_plan(goal.id)
        if not plan:
            notes.append(f"{goal.ref} has no plan yet: propose agenda.plan for it.")
            continue
        if all(not t.status.is_open for t in plan):
            done = sum(1 for t in plan if t.status is TaskStatus.DONE)
            notes.append(
                f"Every task of {goal.ref} is closed ({done}/{len(plan)} done): check "
                "the success criteria and mark the goal done, or re-plan."
            )
            continue
        for task in plan:
            if task.status is TaskStatus.FAILED or task.attempts >= 3:
                notes.append(
                    f"{task.ref} ({goal.ref}) has failed or stalled after "
                    f"{task.attempts} attempt(s): re-plan around it or skip it."
                )
        if plan and not agenda.ready_tasks(goal.id):
            notes.append(
                f"{goal.ref} has open tasks but none is ready (blocked dependencies): re-plan."
            )
    return notes[:max_notes]
