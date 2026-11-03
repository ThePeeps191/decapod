from datetime import UTC, datetime

import pytest

from decapod import prompts
from decapod.agents import Roster
from decapod.caucus import (
    Admission,
    Caucus,
    RankTable,
    WorkingContext,
    borda,
    detect_loop,
    render_table,
    rule_level,
)
from decapod.config.settings import CaucusSettings, LLMSettings
from decapod.domain import (
    Goal,
    Level,
    Percept,
    ProviderUnavailable,
    RiskTier,
    Task,
    Tier,
)
from decapod.executive import planner_notes
from decapod.llm import LLM, ProviderError, ScriptedProvider
from decapod.memory import AgendaStore, TaskDraft


async def no_sleep(_):
    return None


TIERS = {
    "web.fetch": RiskTier.READ,
    "drive.write": RiskTier.LOCAL_WRITE,
    "email.send": RiskTier.EXTERNAL_WRITE,
    "agenda.plan": RiskTier.INTERNAL,
}


def admit(action):
    if action.kind == "wait":
        return Admission(True, "wait", RiskTier.INTERNAL)
    if action.tool not in TIERS:
        return Admission(False, f"unknown tool {action.tool}", RiskTier.IRREVERSIBLE)
    return Admission(True, "ok", TIERS[action.tool])


def make_ctx(charter, **kw):
    goal = Goal(id=1, title="Write digest")
    task = Task(id=1, goal_id=1, title="Research sources")
    defaults = dict(
        cycle_id="c7",
        seq=7,
        now=datetime.now(UTC),
        charter=charter,
        agenda_text="G1 Write digest",
        focus_goal=goal,
        focus_task=task,
        catalog="- web.fetch(url: str)",
        has_active_goals=True,
    )
    defaults.update(kw)
    return WorkingContext(**defaults)


def make_caucus(provider, events, **settings):
    cs = CaucusSettings(llm_triage=False, **settings)
    llm = LLM(LLMSettings(), provider, sleep=no_sleep)
    return Caucus(llm, Roster(cs), cs, events, admit)


FETCH = {
    "tool": "web.fetch",
    "args": {"url": "https://a.example"},
    "goal_id": 1,
    "task_id": 1,
    "rationale": "need sources",
    "expected_outcome": "a page",
    "confidence": 0.7,
}
WRITE = {
    "tool": "drive.write",
    "args": {"path": "d.md", "content": "x"},
    "goal_id": 1,
    "rationale": "draft",
    "expected_outcome": "file",
    "confidence": 0.6,
}
SEND = {
    "tool": "email.send",
    "args": {"to": "a@b.c", "subject": "s", "body": "b"},
    "rationale": "announce",
    "expected_outcome": "sent",
    "confidence": 0.9,
}


# --------------------------------------------------------------------------- ranking


def test_borda_basic_and_ties():
    table = RankTable()
    for ranker, scores in {
        "a": {"P1": 5, "P2": 3, "P3": 1},
        "b": {"P1": 4, "P2": 4, "P3": 2},
    }.items():
        for pid, score in scores.items():
            table.add(ranker, pid, score)
    ranking = borda(table, ["P1", "P2", "P3"])
    assert [pid for pid, _ in ranking] == ["P1", "P2", "P3"]
    assert ranking[0][1] == pytest.approx(1.0 + 0.75)


def test_borda_weights_flags_and_single():
    table = RankTable()
    table.add("x", "P1", 5)
    table.add("x", "P2", 1)
    table.add("y", "P2", 5)
    table.add("y", "P1", 1)
    assert borda(table, ["P1", "P2"], {"y": 2.0})[0][0] == "P2"
    table.flag("P2", "prohibition.1")
    assert borda(table, ["P1", "P2"], {"y": 2.0})[0][0] == "P1"
    solo = RankTable()
    solo.add("z", "P9", 5)
    assert borda(solo, ["P9", "P10"]) == [("P9", 1.0), ("P10", 0.0)]
    assert "charter" in render_table(table, borda(table, ["P1", "P2"]))


# --------------------------------------------------------------------------- escalation


def test_rule_level(charter):
    s = CaucusSettings(review_every=10)
    assert rule_level(make_ctx(charter), s).level is Level.L0
    assert rule_level(make_ctx(charter, has_active_goals=False), s).level is Level.L2
    assert rule_level(make_ctx(charter, seq=20), s).level is Level.L2
    assert rule_level(make_ctx(charter, last_failed=True), s).level is Level.L1
    assert rule_level(make_ctx(charter, focus_task=None), s).level is Level.L1
    p = Percept(source="operator", kind="note", summary="hi", importance=0.9)
    assert rule_level(make_ctx(charter, percepts=[p]), s).level is Level.L2
    capped = CaucusSettings(max_level="L1")
    assert rule_level(make_ctx(charter, has_active_goals=False), capped).level is Level.L1
    floor = CaucusSettings(min_level="L2")
    assert rule_level(make_ctx(charter), floor).level is Level.L2


def test_detect_loop():
    same = [("web.fetch:{x}", False)] * 3
    assert "same action" in detect_loop(same, 6, 3)
    assert detect_loop([("a:1", True), ("b:2", True), ("wait", True)], 6, 3) is None
    fails = [("web.fetch:1", False), ("web.fetch:2", False), ("web.fetch:3", False)]
    assert "failed 3 times" in detect_loop(fails, 6, 3)
    assert detect_loop([], 6, 3) is None


# --------------------------------------------------------------------------- protocol


async def test_l0_reflex_path(charter, events):
    provider = ScriptedProvider(
        {
            "caucus.propose.planner": {"proposals": [FETCH]},
            "caucus.confirm": {"approve": True, "reason": "routine"},
        }
    )
    caucus = make_caucus(provider, events)
    decision = await caucus.deliberate(make_ctx(charter), Level.L0)
    assert decision.level is Level.L0 and decision.chosen.action.tool == "web.fetch"
    assert provider.purposes() == ["caucus.propose.planner", "caucus.confirm"]
    assert events.recent(1)[0].kind == "caucus.decision"


async def test_l0_declined_falls_back_to_l1(charter, events):
    provider = ScriptedProvider(
        {
            "caucus.propose.*": {"proposals": [FETCH]},
            "caucus.confirm": {"approve": False, "reason": "odd"},
            "caucus.critique.critic": {
                "critiques": [{"proposal_id": "P2", "verdict": "support", "score": 4}]
            },
            "caucus.decide": {"choice": "P2", "rationale": "fine"},
        }
    )
    decision = await make_caucus(provider, events).deliberate(make_ctx(charter), Level.L0)
    assert decision.level is Level.L1
    assert decision.chosen.author_role == "strategist+planner+operator"  # merged duplicates


async def test_l1_with_inadmissible_and_critique(charter, events):
    provider = ScriptedProvider(
        {
            "caucus.propose.strategist": {"proposals": [{"tool": "shell.exec", "args": {}}]},
            "caucus.propose.planner": {"proposals": [FETCH]},
            "caucus.propose.operator": {"proposals": [WRITE]},
            "caucus.critique.critic": {
                "critiques": [
                    {
                        "proposal_id": "p2",
                        "verdict": "oppose",
                        "concerns": ["premature"],
                        "score": 1,
                    },
                    {"proposal_id": "P3", "verdict": "support", "score": 5},
                    {"proposal_id": "P99", "verdict": "support", "score": 5},
                ]
            },
            "caucus.decide": {"choice": "P2", "rationale": "need info first"},
        }
    )
    decision = await make_caucus(provider, events).deliberate(make_ctx(charter), Level.L1)
    kinds = [e.kind for e in events.recent(30)]
    assert "caucus.inadmissible" in kinds
    assert decision.chosen.id == "P2"
    assert decision.ranking == ["P3", "P2"]
    assert decision.dissent == ["critic: premature"]


async def test_l2_full_protocol(charter, events):
    provider = ScriptedProvider(
        {
            "caucus.propose.planner": {"proposals": [FETCH]},
            "caucus.propose.operator": {"proposals": [WRITE]},
            "caucus.propose.*": {"proposals": []},
            "caucus.critique.*": {
                "critiques": [
                    {"proposal_id": "P1", "verdict": "support", "score": 4},
                    {
                        "proposal_id": "P2",
                        "verdict": "amend",
                        "concerns": ["too early"],
                        "score": 2,
                    },
                ]
            },
            "caucus.revise.operator": {
                "revisions": [{"proposal_id": "P2", "action": "withdraw", "note": "fair"}]
            },
            "caucus.rank.*": {"scores": [{"proposal_id": "P1", "score": 5}]},
            "caucus.report.*": {
                "top": ["P1"],
                "consensus": "fetch first",
                "key_risks": [],
                "dissent": ["one voice wanted to write now"],
            },
            "caucus.decide": {"choice": "P1", "rationale": "consensus"},
        }
    )
    decision = await make_caucus(provider, events).deliberate(make_ctx(charter), Level.L2)
    assert decision.level is Level.L2 and decision.chosen.id == "P1"
    purposes = provider.purposes()
    assert "caucus.revise.operator" in purposes
    assert sum(p.startswith("caucus.report.") for p in purposes) == 3
    assert any("chamber" in d for d in decision.dissent)
    assert "caucus.revision" in [e.kind for e in events.recent(40)]


async def test_external_write_escalates_to_l3(charter, events):
    provider = ScriptedProvider(
        {
            "caucus.propose.*": {"proposals": [SEND]},
            "caucus.critique*": {
                "critiques": [
                    {
                        "proposal_id": "P1",
                        "verdict": "oppose",
                        "concerns": ["unsolicited"],
                        "charter_violation": "prohibition.1",
                        "score": 1,
                    }
                ]
            },
            "caucus.revise.*": {"revisions": [{"proposal_id": "P1", "action": "keep"}]},
            "caucus.rank.*": {"scores": []},
            "caucus.report.*": {"top": [], "consensus": "risky"},
            "caucus.decide": [
                {"choice": "P1", "rationale": "go"},
                {
                    "choice": "wait",
                    "wait_minutes": 30,
                    "wake_on": ["inbox", "x"],
                    "rationale": "too risky",
                },
            ],
        }
    )
    decision = await make_caucus(provider, events).deliberate(make_ctx(charter), Level.L1)
    assert decision.level is Level.L3
    assert decision.is_wait and decision.wait_minutes == 30 and decision.wake_on == ["inbox"]
    decide_calls = [c for c in provider.calls if c.purpose == "caucus.decide"]
    assert decide_calls[-1].tier is Tier.MAX
    assert "caucus.critique2.risk_officer" in provider.purposes()
    assert "caucus.escalate" in [e.kind for e in events.recent(80)]


async def test_no_proposals_means_wait(charter, events):
    provider = ScriptedProvider({"caucus.propose.*": {"proposals": []}})
    decision = await make_caucus(provider, events).deliberate(make_ctx(charter), Level.L1)
    assert decision.is_wait and "No admissible" in decision.rationale


async def test_agent_errors_are_tolerated(charter, events):
    provider = ScriptedProvider(
        {
            "caucus.propose.planner": {"proposals": [FETCH]},
            "caucus.propose.*": "garbage that is not json",
            "caucus.critique.*": ProviderError("bad", retryable=False, status=500),
            "caucus.decide": {"choice": "P42", "rationale": "?"},
        }
    )
    decision = await make_caucus(provider, events).deliberate(make_ctx(charter), Level.L1)
    assert decision.chosen.action.tool == "web.fetch"
    assert "unknown option" in decision.rationale
    assert "caucus.agent_error" in [e.kind for e in events.recent(40)]


async def test_provider_unavailable_propagates(charter, events):
    provider = ScriptedProvider({"*": ProviderError("down", retryable=False, status=500)})
    cs = CaucusSettings(llm_triage=False)
    llm = LLM(LLMSettings(circuit_breaker_failures=1), provider, sleep=no_sleep)
    caucus = Caucus(llm, Roster(cs), cs, events, admit)
    with pytest.raises(ProviderUnavailable):
        await caucus.deliberate(make_ctx(charter), Level.L1)


async def test_llm_triage_can_raise_level(charter, events):
    provider = ScriptedProvider({"caucus.triage": {"level": "significant", "reason": "news"}})
    cs = CaucusSettings(llm_triage=True)
    caucus = Caucus(LLM(LLMSettings(), provider, sleep=no_sleep), Roster(cs), cs, events, admit)
    p = Percept(source="sensor:feeds", kind="feed_item", summary="new paper", importance=0.4)
    result = await caucus.triage(make_ctx(charter, percepts=[p]))
    assert result.level is Level.L2 and any("triage" in r for r in result.reasons)


def test_roster_settings():
    roster = Roster(
        CaucusSettings(
            disabled_roles=["strategist", "planner", "operator"],
            role_tiers={"critic": "STANDARD"},
            role_weights={"critic": 2},
        )
    )
    assert [s.role for s in roster.proposers(Level.L1)] == ["planner"]
    assert roster.get("critic").tier is Tier.PRO and roster.get("critic").weight == 2
    assert roster.executive_tier(Level.L3) is Tier.MAX
    assert len(roster.managers()) == 3


def test_all_templates_render(charter):
    ctx = make_ctx(charter)
    text = ctx.render(("agenda", "percepts", "recent", "memories", "lessons", "budget"))
    assert "Focus" in text
    common = dict(
        situation="S",
        role_title="R",
        catalog="C",
        max_proposals=2,
        focus_hint="",
        proposals="P",
        own="O",
        own_ids="",
        debate="D",
        ranking="R",
        chamber="c",
        members="m",
        shortlist="s",
        reports="r",
        lessons="",
        proposal="p",
        charter_text="t",
        cycle_id="c1",
        focus="f",
        decision="d",
        rationale="r",
        dissent="",
        outcome="o",
        day="2026-01-01",
        items="i",
        brief="b",
        charter=charter,
        now="now",
        context="ctx",
    )
    for name in prompts.templates():
        assert prompts.render(name, **common)
        assert len(prompts.template_hash(name)) == 12


def test_planner_notes(db):
    agenda = AgendaStore(db)
    goal = agenda.add_goal("No plan")
    assert "no plan" in planner_notes(agenda)[0]
    tasks = agenda.set_plan(goal.id, [TaskDraft(title="a")])
    agenda.update_task(tasks[0].id, "done")
    assert "closed" in planner_notes(agenda)[0]
    agenda.set_plan(goal.id, [TaskDraft(title="b")])
    t = agenda.current_plan(goal.id)[0]
    for _ in range(3):
        agenda.update_task(t.id, "in_progress")
        agenda.update_task(t.id, "todo")
    assert "stalled" in planner_notes(agenda)[0]
