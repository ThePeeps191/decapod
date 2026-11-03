"""The working context: what every agent may know about the current cycle."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from decapod.config.charter import Charter
from decapod.domain import Goal, Memory, Percept, Task
from decapod.memory.context import Section, assemble
from decapod.util.text import one_line


@dataclass
class WorkingContext:
    cycle_id: str
    seq: int
    now: datetime
    charter: Charter
    agenda_text: str = ""
    focus_goal: Goal | None = None
    focus_task: Task | None = None
    percepts: list[Percept] = field(default_factory=list)
    recent_text: str = ""
    memories: list[Memory] = field(default_factory=list)
    lessons: list[Memory] = field(default_factory=list)
    budget_text: str = ""
    planner_notes: list[str] = field(default_factory=list)
    catalog: str = ""
    loop_warning: str | None = None
    last_failed: bool = False
    has_active_goals: bool = False
    token_budget: int = 6000

    @property
    def focus_text(self) -> str:
        if self.focus_goal is None:
            return "No active goal. The agenda needs attention."
        text = f"{self.focus_goal.ref}: {self.focus_goal.title}"
        if self.focus_task is not None:
            text += (
                f"\nNext task {self.focus_task.ref} [{self.focus_task.status.value}]: "
                f"{self.focus_task.title}"
            )
            if self.focus_task.description:
                text += f"\n  {one_line(self.focus_task.description, 300)}"
            if self.focus_task.attempts:
                text += f"\n  attempts so far: {self.focus_task.attempts}"
        else:
            text += "\nThis goal has no ready task (it may need a plan)."
        return text

    def percepts_text(self) -> str:
        if not self.percepts:
            return "Nothing new since the last cycle."
        lines = []
        for p in sorted(self.percepts, key=lambda p: -p.importance):
            tag = " [untrusted]" if p.untrusted else ""
            lines.append(f"- ({p.source}/{p.kind}{tag}) {p.summary}")
        return "\n".join(lines)

    def memories_text(self) -> str:
        if not self.memories:
            return ""
        return "\n".join(
            f"- M{m.id} [{m.kind.value} {m.created_at:%Y-%m-%d}] {one_line(m.content, 280)}"
            for m in self.memories
        )

    def lessons_text(self) -> str:
        if not self.lessons:
            return ""
        return "\n".join(f"- L{m.id}: {one_line(m.content, 240)}" for m in self.lessons)

    def render(self, sees: tuple[str, ...], token_budget: int | None = None) -> str:
        """Render the sections an agent is allowed to see, within a token budget."""
        budget = token_budget or self.token_budget
        sections: list[Section] = [Section("Focus", self.focus_text, priority=0)]
        if self.loop_warning:
            sections.append(Section("Warning", self.loop_warning, priority=0))
        available = {
            "percepts": Section(
                "New since last cycle", self.percepts_text(), priority=1, max_tokens=1500
            ),
            "agenda": Section("Agenda", self.agenda_text, priority=2, max_tokens=1500),
            "planner_notes": Section(
                "Planning notes",
                "\n".join(f"- {n}" for n in self.planner_notes),
                priority=2,
                max_tokens=400,
            ),
            "recent": Section("Recent cycles", self.recent_text, priority=3, max_tokens=1200),
            "lessons": Section("Lessons", self.lessons_text(), priority=3, max_tokens=500),
            "memories": Section(
                "Relevant memories", self.memories_text(), priority=4, max_tokens=1200
            ),
            "budget": Section("Budget", self.budget_text, priority=5, max_tokens=300),
        }
        for name in sees:
            section = available.get(name)
            if section is not None and (section.body.strip() or name in ("agenda", "recent")):
                sections.append(section)
        return assemble(sections, budget)
