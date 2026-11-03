"""Regression tests for defects found in review (one test per defect)."""

import sys
from datetime import UTC, datetime

import pytest

from decapod.agents import Roster
from decapod.caucus import Admission, Caucus
from decapod.caucus.schemas import ExecutiveChoice, ProposalDraft
from decapod.config import Secrets
from decapod.config.settings import CaucusSettings, LLMSettings, RateLimit
from decapod.control import Budget, PolicyEngine, ToolCheck
from decapod.domain import ActionSpec, Level, RiskTier, SideEffect, ToolResult
from decapod.llm import LLM, ScriptedProvider
from decapod.memory import AgendaError, AgendaStore, TaskDraft
from decapod.tools import ToolExecutor, ToolRegistry
from decapod.tools.base import Tool, ToolArgs
from decapod.tools.builtin.github import WriteFile, safe_path
from tests.unit.test_caucus import make_ctx


async def no_sleep(_):
    return None


def test_github_paths_cannot_escape_the_repository():
    assert safe_path("docs/My File.md") == "docs/My%20File.md"
    for bad in [
        "../../victim/prod/contents/x",
        "a/../b",
        "x?ref=evil",
        "x#y",
        "%2e%2e/x",
        "",
        "./x",
    ]:
        with pytest.raises(ValueError):
            safe_path(bad)
    tool = WriteFile()
    args = tool.Args.model_validate(
        {"repo": "me/notes", "path": "a.md", "content": "x", "message": "add a"}
    )
    assert tool.scopes(args) == ["github:me/notes:write"]
    with pytest.raises(ValueError):
        tool.Args.model_validate(
            {"repo": "me/notes", "path": "../../x/y", "content": "x", "message": "m"}
        )


def test_github_scope_glob_cannot_match_another_repo(settings, charter, db):
    policy = PolicyEngine(settings, charter, Budget(db, settings.budget), db)
    assert policy.scope_granted("github:me/notes:write")  # conftest grants github:me/notes:*
    assert not policy.scope_granted("github:me/notes.evil:write")


async def test_reflex_decline_with_max_level_l0_waits(charter, events):
    cs = CaucusSettings(llm_triage=False, max_level="L0")
    provider = ScriptedProvider({"caucus.propose.*": {"proposals": []}})
    caucus = Caucus(
        LLM(LLMSettings(), provider, sleep=no_sleep),
        Roster(cs),
        cs,
        events,
        lambda a: Admission(True, "", RiskTier.INTERNAL),
    )
    decision = await caucus.deliberate(make_ctx(charter), Level.L0)
    assert decision.is_wait and len(provider.calls) == 1


def test_negative_waits_are_clamped():
    assert ExecutiveChoice(choice="wait", wait_minutes=-5).wait_minutes == 0
    assert ProposalDraft(tool="wait", wait_minutes=-1).wait_minutes == 0


async def test_escalation_is_rechecked_after_re_deliberation(charter, events):
    tiers = {"drive.write": RiskTier.LOCAL_WRITE, "email.send": RiskTier.EXTERNAL_WRITE}

    def admit(action):
        if action.kind == "wait":
            return Admission(True, "", RiskTier.INTERNAL)
        return Admission(True, "", tiers[action.tool])

    write = {"tool": "drive.write", "args": {"path": "a", "content": "b"}}
    send = {"tool": "email.send", "args": {"to": "a@b.c"}}
    # Each run proposes [write, send] (merged across proposers): L1 → P1/P2, L2 → P3/P4,
    # L3 → P5/P6. L1 picks the tier-2 write (needs L2); L2 then picks the tier-3 send,
    # which needs L3 and must trigger a second escalation.
    provider = ScriptedProvider(
        {
            "caucus.propose.*": {"proposals": [write, send]},
            "caucus.critique*": {"critiques": []},
            "caucus.revise.*": {"revisions": []},
            "caucus.rank.*": {"scores": []},
            "caucus.report.*": {"top": []},
            "caucus.decide": [{"choice": "P1"}, {"choice": "P4"}, {"choice": "P6"}],
        }
    )
    cs = CaucusSettings(llm_triage=False, escalate_on_tier={"tier2": "L2", "tier3": "L3"})
    caucus = Caucus(LLM(LLMSettings(), provider, sleep=no_sleep), Roster(cs), cs, events, admit)
    decision = await caucus.deliberate(make_ctx(charter), Level.L1)
    assert decision.level is Level.L3
    assert decision.chosen.action.tool == "email.send"


def test_prohibition_patterns_see_raw_values(settings, charter, db):
    charter.prohibitions.patterns = [r"wire\s+transfer", r"^EXACT$"]
    policy = PolicyEngine(settings, charter, Budget(db, settings.budget), db)
    send = ToolCheck(
        name="email.send",
        tier=RiskTier.EXTERNAL_WRITE,
        side_effect=SideEffect.EXTERNAL_WRITE,
        scopes=["email.send"],
    )
    for body in ["please wire\ntransfer it", "EXACT"]:
        verdict = policy.review(
            ActionSpec(tool="email.send", args={"body": body}), send, cycle_id=None
        )
        assert verdict.outcome == "deny" and "prohibition" in verdict.reason


def test_glob_rate_limit_is_shared(settings, charter, db):
    settings.policy.rate_limits = {"web.*": RateLimit(per_hour=1)}
    policy = PolicyEngine(settings, charter, Budget(db, settings.budget), db)
    db.insert(
        "INSERT INTO tool_calls(ts, tool, args, risk_tier) VALUES (?, 'web.fetch', '{}', 1)",
        (datetime.now(UTC).isoformat(),),
    )
    search = ToolCheck(
        name="web.search",
        tier=RiskTier.READ,
        side_effect=SideEffect.EXTERNAL_READ,
        scopes=["web.read"],
    )
    verdict = policy.review(
        ActionSpec(tool="web.search", args={"query": "x"}), search, cycle_id=None
    )
    assert verdict.outcome == "deny" and verdict.transient


async def test_secrets_never_leave_through_read_tools(settings, make_ctx):
    class QueryArgs(ToolArgs):
        query: str

    class Search(Tool):
        name = "test.search"
        description = "search"
        Args = QueryArgs
        tier = RiskTier.READ
        side_effect = SideEffect.EXTERNAL_READ

        async def run(self, args, ctx):
            raise AssertionError("must not run")

    registry = ToolRegistry(settings, Secrets({}))
    registry.register(Search())
    ctx = make_ctx(secrets={"MY_TOKEN": 'tok"with\\quote12'})
    result = await ToolExecutor(registry).execute(
        ActionSpec(tool="test.search", args={"query": 'look up tok"with\\quote12'}), ctx
    )
    assert not result.ok and "secret" in result.error


def test_plan_rejects_forward_dependencies(db):
    agenda = AgendaStore(db)
    goal = agenda.add_goal("G")
    with pytest.raises(AgendaError, match="earlier steps"):
        agenda.set_plan(goal.id, [TaskDraft(title="a", depends_on=[2]), TaskDraft(title="b")])


async def test_mcp_duplicate_names_are_suffixed(tmp_path):
    from decapod.config.settings import MCPServerSettings
    from decapod.tools import MCPManager

    script = tmp_path / "srv.py"
    script.write_text(
        "import json, sys\n"
        "tools = [{'name': 'get.item'}, {'name': 'get_item'}]\n"
        "for line in sys.stdin:\n"
        "    m = json.loads(line)\n"
        "    if 'id' not in m: continue\n"
        "    r = {'protocolVersion': '1', 'capabilities': {}} if m['method'] == 'initialize' "
        "else {'tools': tools}\n"
        "    print(json.dumps({'jsonrpc': '2.0', 'id': m['id'], 'result': r}), flush=True)\n"
    )
    manager = MCPManager([MCPServerSettings(name="srv", command=[sys.executable, str(script)])])
    try:
        names = sorted(t.name for t in await manager.start())
    finally:
        await manager.close()
    assert names == ["mcp.srv.get_item", "mcp.srv.get_item_2"]


def test_tool_result_unused():
    assert ToolResult.success("x").ok
