"""A deterministic scripted 'brain' that plays every caucus role.

It reads the rendered prompts (the same ones a real model sees) and answers with sensible,
schema-valid JSON. It powers offline demos (``decapod run --offline``), integration tests
and the scripted mode of ``decapod eval``. It is intentionally simple: it follows the
agenda, researches with the tools it is offered, writes a note and moves on.
"""

from __future__ import annotations

import json
import re
from typing import Any

from decapod.llm import ProviderRequest, ProviderResponse, ScriptedProvider

_GOAL_RE = re.compile(r"\bG(\d+): (.+)")
_TASK_RE = re.compile(r"Next task T(\d+) \[(\w+)\]: (.+)")
_PID_RE = re.compile(r"^(P\d+)\b", re.MULTILINE)
_MISSION_RE = re.compile(r"Mission: (.+)")


def _text(request: ProviderRequest) -> str:
    return "\n".join(m.content for m in request.messages)


def _focus(text: str) -> tuple[int | None, str, int | None, str]:
    section = text.split("## Focus", 1)[-1].split("## ", 1)[0]
    goal = _GOAL_RE.search(section)
    task = _TASK_RE.search(section)
    return (
        int(goal.group(1)) if goal else None,
        goal.group(2).strip() if goal else "",
        int(task.group(1)) if task else None,
        task.group(3).strip() if task else "",
    )


def _topic(text: str) -> str:
    mission = _MISSION_RE.search(text)
    words = re.findall(r"[A-Za-z]{4,}", mission.group(1) if mission else "the mission")
    return " ".join(words[:6]) or "the mission"


def _proposals(text: str, role: str) -> dict[str, Any]:
    if role not in ("planner", "operator", "strategist"):
        return {"proposals": []}
    goal_id, goal_title, task_id, task_title = _focus(text)
    if role == "strategist" and goal_id is not None:
        return {"proposals": []}  # the strategist only steps in when the agenda is empty
    catalog = text.split("# Available tools", 1)[-1]
    topic = _topic(text)
    if goal_id is None:
        return {
            "proposals": [
                {
                    "tool": "agenda.add_goal",
                    "args": {
                        "title": f"Publish a short briefing on {topic}",
                        "description": "Research the topic and publish a concise, sourced note.",
                        "success_criteria": ["a note is published in drive/published"],
                        "priority": 0.8,
                        "charter_refs": ["mission"],
                        "rationale": "The agenda is empty; this directly serves the mission.",
                    },
                    "rationale": "The agenda is empty.",
                    "expected_outcome": "a first goal",
                    "confidence": 0.8,
                }
            ]
        }
    if task_id is None and ("failed or stalled" in text or "none is ready" in text):
        return {
            "proposals": [
                {
                    "tool": "agenda.plan",
                    "args": {
                        "goal_id": goal_id,
                        "tasks": [{"title": "Write and publish the note from what you know"}],
                    },
                    "goal_id": goal_id,
                    "rationale": "Research keeps failing; re-plan around it.",
                    "expected_outcome": "a plan that avoids the failing step",
                    "confidence": 0.7,
                }
            ]
        }
    if task_id is None:
        return {
            "proposals": [
                {
                    "tool": "agenda.plan",
                    "args": {
                        "goal_id": goal_id,
                        "tasks": [
                            {
                                "title": "Research the topic",
                                "description": f"Find sources about {topic}.",
                            },
                            {"title": "Write and publish the note", "depends_on": [1]},
                        ],
                    },
                    "goal_id": goal_id,
                    "rationale": "The goal needs a plan.",
                    "expected_outcome": "a two-step plan",
                    "confidence": 0.8,
                }
            ]
        }
    lowered = task_title.lower()
    if "research" in lowered:
        if "web.search(" in catalog:
            args: dict[str, Any] = {"query": topic, "max_results": 5}
            tool = "web.search"
        else:
            tool, args = "memory.search", {"query": topic}
        return {
            "proposals": [
                {
                    "tool": tool,
                    "args": args,
                    "goal_id": goal_id,
                    "task_id": task_id,
                    "rationale": "Gather sources first.",
                    "expected_outcome": "a list of sources",
                    "confidence": 0.7,
                }
            ]
        }
    if ("publish" in lowered or "write" in lowered) and "notes.publish(" in catalog:
        return {
            "proposals": [
                {
                    "tool": "notes.publish",
                    "args": {
                        "title": goal_title or f"Briefing on {topic}",
                        "content": f"# {goal_title}\n\nA short briefing on {topic}, written "
                        "autonomously from the sources gathered earlier.",
                        "tags": ["briefing"],
                    },
                    "goal_id": goal_id,
                    "task_id": task_id,
                    "rationale": "Ship the note.",
                    "expected_outcome": "a published note",
                    "confidence": 0.75,
                }
            ]
        }
    return {
        "proposals": [
            {
                "tool": "wait",
                "wait_minutes": 30,
                "rationale": "Nothing to do.",
                "expected_outcome": "rest",
                "confidence": 0.5,
            }
        ]
    }


def _ids(text: str) -> list[str]:
    return list(
        dict.fromkeys(
            _PID_RE.findall(text.split("# Task", 1)[0].split("# Proposals under review", 1)[-1])
        )
    ) or list(dict.fromkeys(_PID_RE.findall(text)))


def _reflection(text: str) -> dict[str, Any]:
    outcome = text.split("Outcome:", 1)[-1].split("\n", 1)[0]
    decision = text.split("Decision:", 1)[-1].split("\n", 1)[0]
    task = re.search(r"\(task T(\d+)\)", decision)
    goal = re.search(r"\(goal G(\d+)\)", decision)
    ok = outcome.strip().startswith("executed")
    result: dict[str, Any] = {
        "summary": f"{decision.strip()} → {outcome.strip()[:160]}",
        "facts": [],
        "lessons": [],
        "dissent_vindicated": None,
    }
    if task:
        result["task_update"] = {
            "task_id": int(task.group(1)),
            "status": "done" if ok else "failed",
            "note": "completed" if ok else "the action failed",
        }
    if goal and ok and "notes.publish" in decision:
        result["goal_update"] = {
            "goal_id": int(goal.group(1)),
            "status": "done",
            "progress_note": "note published",
        }
        result["lessons"] = ["Publishing a short note early beats polishing forever."]
    return result


def respond(request: ProviderRequest) -> ProviderResponse | dict[str, Any] | str:
    text = _text(request)
    purpose = request.purpose
    if purpose.startswith("caucus.propose."):
        return _proposals(text, purpose.rsplit(".", 1)[-1])
    if purpose == "caucus.confirm":
        return {"approve": True, "reason": "routine continuation"}
    if purpose == "caucus.triage":
        return {"level": "routine", "reason": "nothing unusual"}
    if purpose.startswith(("caucus.critique", "caucus.critique2")):
        return {
            "critiques": [
                {"proposal_id": pid, "verdict": "support", "score": 4, "concerns": []}
                for pid in _ids(text)
            ]
        }
    if purpose.startswith("caucus.revise."):
        return {"revisions": []}
    if purpose.startswith("caucus.rank."):
        return {"scores": [{"proposal_id": pid, "score": 4} for pid in _ids(text)]}
    if purpose.startswith("caucus.report."):
        ids = _ids(text)
        return {
            "top": ids[:1],
            "consensus": "proceed with the plan",
            "key_risks": [],
            "dissent": [],
        }
    if purpose == "caucus.decide":
        shortlist = text.split("# Shortlist", 1)[-1]
        ids = _PID_RE.findall(shortlist)
        return {
            "choice": ids[0] if ids else "wait",
            "rationale": "best ranked option",
            "wait_minutes": 30,
        }
    if purpose == "reflect":
        return _reflection(text)
    if purpose == "memory.compact":
        return {"digest": "Routine work on the agenda."}
    if purpose == "doctor.ping":
        return "OK"
    if purpose == "doctor.structured":
        return {"ok": True}
    return json.dumps({"note": "unscripted purpose", "purpose": purpose})


def scripted_brain() -> ScriptedProvider:
    return ScriptedProvider(default=respond)
