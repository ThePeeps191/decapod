"""Context budgeting: assemble prompt sections that always fit a token budget."""

from __future__ import annotations

from dataclasses import dataclass

from decapod.util.text import estimate_tokens, truncate


@dataclass
class Section:
    title: str
    body: str
    priority: int = 5  # lower = filled first
    max_tokens: int | None = None

    def render(self, body: str) -> str:
        return f"## {self.title}\n{body.strip() or '(none)'}"


def assemble(sections: list[Section], token_budget: int) -> str:
    """Fill sections by priority, truncating predictably; keep declaration order in output.

    Context overflow is never an error: low-priority sections shrink or disappear first.
    """
    remaining = max(0, token_budget)
    bodies: dict[int, str] = {}
    for index in sorted(range(len(sections)), key=lambda i: sections[i].priority):
        section = sections[index]
        header_cost = estimate_tokens(f"## {section.title}\n") + 1
        if remaining <= header_cost:
            continue
        cap = remaining - header_cost
        if section.max_tokens is not None:
            cap = min(cap, section.max_tokens)
        body = section.body.strip()
        if estimate_tokens(body) > cap:
            body = truncate(body, cap * 4)
        bodies[index] = body
        remaining -= header_cost + estimate_tokens(body)
    return "\n\n".join(sections[i].render(bodies[i]) for i in range(len(sections)) if i in bodies)
