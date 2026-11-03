"""Tier-0 internal tools: agenda, memory, self-status, clock and charter amendments."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import Field

from decapod.domain import GoalStatus, MemoryKind, TaskStatus, ToolResult
from decapod.memory import AgendaError, TaskDraft
from decapod.memory.agenda import check_recurrence, schedule_text
from decapod.tools.base import NoArgs, Tool, ToolArgs, ToolContext
from decapod.util.text import one_line
from decapod.util.timeutil import format_duration, now, parse_duration, parse_when, to_iso


class AddGoalArgs(ToolArgs):
    title: str = Field(min_length=3, max_length=200)
    description: str = ""
    success_criteria: list[str] = Field(default_factory=list, description="checkable outcomes")
    priority: float = Field(default=0.5, ge=0, le=1)
    charter_refs: list[str] = Field(
        default_factory=list, description="charter clauses, e.g. 'mission', 'value.1'"
    )
    rationale: str = ""
    parent_id: int | None = None
    every: str | None = Field(
        default=None, description="make it recurring, e.g. '7d' (at most hourly)"
    )
    due: str | None = Field(default=None, description="deadline: '2026-12-01' or '3d'")


def _schedule(every: str | None, due: str | None) -> tuple[timedelta | None, datetime | None]:
    interval = parse_duration(every) if every else None
    deadline = parse_when(due) if due else None
    return interval, deadline


class AddGoal(Tool):
    name = "agenda.add_goal"
    description = "Add a new goal to your agenda. Goals must serve the charter (cite clauses)."
    Args = AddGoalArgs

    def precheck(self, args: AddGoalArgs, ctx: ToolContext) -> str | None:
        clauses = ctx.charter.clauses()
        bad = [ref for ref in args.charter_refs if ref not in clauses]
        if bad:
            return f"unknown charter clause(s): {', '.join(bad)} (valid: {', '.join(clauses)})"
        if not args.charter_refs:
            return "charter_refs must cite at least one charter clause (e.g. 'mission')"
        open_titles = {g.title.lower() for g in ctx.agenda.goals(open_only=True)}
        if args.title.strip().lower() in open_titles:
            return "an open goal with this title already exists"
        try:
            interval, _ = _schedule(args.every, args.due)
        except ValueError as exc:
            return str(exc)
        if interval is not None:
            try:
                check_recurrence(interval)
            except AgendaError as exc:
                return str(exc)
        return None

    async def run(self, args: AddGoalArgs, ctx: ToolContext) -> ToolResult:
        interval, deadline = _schedule(args.every, args.due)
        goal = ctx.agenda.add_goal(
            args.title,
            args.description,
            every=interval,
            due_at=deadline,
            success_criteria=args.success_criteria,
            priority=args.priority,
            rationale=args.rationale,
            charter_refs=args.charter_refs,
            parent_id=args.parent_id,
            cycle_id=ctx.cycle_id,
        )
        return ToolResult.success(f"added {goal.ref}: {goal.title}", {"goal_id": goal.id})


class UpdateGoalArgs(ToolArgs):
    goal_id: int
    status: Literal["proposed", "active", "blocked", "done", "abandoned"] | None = None
    priority: float | None = Field(default=None, ge=0, le=1)
    progress_note: str | None = None
    description: str | None = None
    every: str | None = Field(default=None, description="set recurrence, e.g. '7d'")
    due: str | None = Field(default=None, description="set a deadline: '2026-12-01' or '3d'")
    clear_schedule: bool = False


class UpdateGoal(Tool):
    name = "agenda.update_goal"
    description = "Change a goal's status, priority, description or progress note."
    Args = UpdateGoalArgs

    def precheck(self, args: UpdateGoalArgs, ctx: ToolContext) -> str | None:
        if ctx.agenda.get_goal(args.goal_id) is None:
            return f"goal G{args.goal_id} does not exist"
        try:
            interval, _ = _schedule(args.every, args.due)
        except ValueError as exc:
            return str(exc)
        if interval is not None:
            try:
                check_recurrence(interval)
            except AgendaError as exc:
                return str(exc)
        return None

    async def run(self, args: UpdateGoalArgs, ctx: ToolContext) -> ToolResult:
        goal = ctx.agenda.update_goal(
            args.goal_id,
            status=GoalStatus(args.status) if args.status else None,
            priority=args.priority,
            progress_note=args.progress_note,
            description=args.description,
            every=parse_duration(args.every) if args.every else None,
            due_at=parse_when(args.due) if args.due else None,
            clear_schedule=args.clear_schedule,
            cycle_id=ctx.cycle_id,
        )
        schedule = schedule_text(goal)
        return ToolResult.success(
            f"{goal.ref} is now {goal.status.value} (priority {goal.priority:.2f})"
            + (f"; {schedule}" if schedule else "")
        )


class PlanTask(ToolArgs):
    title: str = Field(min_length=2)
    description: str = ""
    depends_on: list[int] = Field(
        default_factory=list, description="1-based indexes of earlier steps in this plan"
    )


class PlanArgs(ToolArgs):
    goal_id: int
    tasks: list[PlanTask] = Field(min_length=1, max_length=20)


class Plan(Tool):
    name = "agenda.plan"
    description = (
        "Set (or replace) the task plan for a goal. Open tasks from the previous "
        "plan are marked skipped; finished ones are kept."
    )
    Args = PlanArgs

    def precheck(self, args: PlanArgs, ctx: ToolContext) -> str | None:
        if ctx.agenda.get_goal(args.goal_id) is None:
            return f"goal G{args.goal_id} does not exist"
        return None

    async def run(self, args: PlanArgs, ctx: ToolContext) -> ToolResult:
        try:
            tasks = ctx.agenda.set_plan(
                args.goal_id,
                [
                    TaskDraft(title=t.title, description=t.description, depends_on=t.depends_on)
                    for t in args.tasks
                ],
                cycle_id=ctx.cycle_id,
            )
        except AgendaError as exc:
            return ToolResult.failure(str(exc))
        listing = "; ".join(f"{t.ref} {t.title}" for t in tasks)
        return ToolResult.success(
            f"plan for G{args.goal_id}: {listing}", {"task_ids": [t.id for t in tasks]}
        )


class UpdateTaskArgs(ToolArgs):
    task_id: int
    status: Literal["todo", "in_progress", "done", "failed", "skipped"]
    note: str = ""


class UpdateTask(Tool):
    name = "agenda.update_task"
    description = "Mark a task todo/in_progress/done/failed/skipped with a short note."
    Args = UpdateTaskArgs

    def precheck(self, args: UpdateTaskArgs, ctx: ToolContext) -> str | None:
        if ctx.agenda.get_task(args.task_id) is None:
            return f"task T{args.task_id} does not exist"
        return None

    async def run(self, args: UpdateTaskArgs, ctx: ToolContext) -> ToolResult:
        task = ctx.agenda.update_task(
            args.task_id, TaskStatus(args.status), args.note, cycle_id=ctx.cycle_id
        )
        return ToolResult.success(f"{task.ref} → {task.status.value}")


class RememberArgs(ToolArgs):
    content: str = Field(min_length=3, max_length=4000)
    tags: list[str] = Field(default_factory=list)
    importance: float = Field(default=0.5, ge=0, le=1)
    confidence: float = Field(default=0.8, ge=0, le=1)
    goal_id: int | None = None


class Remember(Tool):
    name = "memory.remember"
    description = "Store a durable fact you learned (semantic memory), with tags and confidence."
    Args = RememberArgs

    async def run(self, args: RememberArgs, ctx: ToolContext) -> ToolResult:
        embedding = None
        if ctx.llm is not None:
            vectors = await ctx.llm.embed([args.content])
            embedding = vectors[0] if vectors else None
        memory = ctx.memory.add(
            MemoryKind.SEMANTIC,
            args.content,
            tags=args.tags,
            importance=args.importance,
            confidence=args.confidence,
            goal_id=args.goal_id,
            cycle_id=ctx.cycle_id,
            embedding=embedding,
        )
        return ToolResult.success(f"remembered M{memory.id}")


class RecallArgs(ToolArgs):
    query: str = Field(min_length=2)
    kind: Literal["episodic", "semantic", "reflective", "any"] = "any"
    k: int = Field(default=8, ge=1, le=25)


class Recall(Tool):
    name = "memory.search"
    description = "Search your long-term memory (past episodes, facts, lessons)."
    Args = RecallArgs

    async def run(self, args: RecallArgs, ctx: ToolContext) -> ToolResult:
        kinds = None if args.kind == "any" else [MemoryKind(args.kind)]
        embedding = None
        if ctx.llm is not None:
            vectors = await ctx.llm.embed([args.query])
            embedding = vectors[0] if vectors else None
        hits = ctx.memory.search(args.query, kinds=kinds, k=args.k, query_embedding=embedding)
        if not hits:
            return ToolResult.success("no matching memories")
        lines = [
            f"M{m.id} [{m.kind.value}, {m.created_at:%Y-%m-%d}] {one_line(m.content, 300)}"
            for m in hits
        ]
        return ToolResult.success("\n".join(lines), {"ids": [m.id for m in hits]})


class SelfStatus(Tool):
    name = "self.status"
    description = "Report your budget usage, agenda summary and memory counts."
    Args = NoArgs

    async def run(self, args: NoArgs, ctx: ToolContext) -> ToolResult:
        lines = ["Budget:"]
        for s in ctx.budget.status(ctx.cycle_id):
            resets = f", resets in {format_duration(s.resets_at - now())}" if s.resets_at else ""
            lines.append(
                f"  {s.window} {s.metric}: {s.used:g}/{s.limit:g} ({s.fraction:.0%}{resets})"
            )
        lines.append("Agenda:")
        lines.append(ctx.agenda.render())
        counts = {k.value: ctx.memory.count(k) for k in MemoryKind}
        lines.append("Memories: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
        return ToolResult.success("\n".join(lines))


class ClockNow(Tool):
    name = "clock.now"
    description = "Current date and time (UTC and the host's local time)."
    Args = NoArgs

    async def run(self, args: NoArgs, ctx: ToolContext) -> ToolResult:
        utc = datetime.now(UTC)
        local = utc.astimezone()
        return ToolResult.success(
            f"UTC {utc:%Y-%m-%d %H:%M:%S} · local {local:%Y-%m-%d %H:%M %Z} ({local:%A})"
        )


class AmendArgs(ToolArgs):
    clause: str = Field(default="", description="clause id to change, or empty for a new one")
    text: str = Field(min_length=5, max_length=2000)
    rationale: str = Field(min_length=5)


class ProposeAmendment(Tool):
    name = "charter.propose_amendment"
    description = (
        "Propose a change to your charter. You cannot edit the charter; the operator "
        "reviews proposals and decides."
    )
    Args = AmendArgs

    async def run(self, args: AmendArgs, ctx: ToolContext) -> ToolResult:
        amendment_id = ctx.db.insert(
            "INSERT INTO amendments(ts, cycle_id, clause, text, rationale) VALUES (?,?,?,?,?)",
            (to_iso(now()), ctx.cycle_id, args.clause, args.text, args.rationale),
        )
        ctx.events.append(
            "charter.amendment_proposed",
            one_line(args.text, 160),
            args.model_dump(),
            actor="agent",
            cycle_id=ctx.cycle_id,
        )
        return ToolResult.success(f"amendment #{amendment_id} recorded for operator review")


INTERNAL_TOOLS: list[type[Tool]] = [
    AddGoal,
    UpdateGoal,
    Plan,
    UpdateTask,
    Remember,
    Recall,
    SelfStatus,
    ClockNow,
    ProposeAmendment,
]
