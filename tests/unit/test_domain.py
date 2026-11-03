import pytest
from pydantic import ValidationError

from decapod.domain import (
    ActionSpec,
    Cost,
    Critique,
    Decision,
    Level,
    Proposal,
    RiskTier,
    Tier,
    ToolResult,
)


def test_tier_parse_and_aliases():
    assert Tier.parse("fast") is Tier.FAST
    assert Tier.parse("FLASH") is Tier.FAST
    assert Tier.parse("pro") is Tier.STANDARD
    assert Tier.parse("MAX") is Tier.STRONG
    assert Tier.parse("Mythos") is Tier.FRONTIER
    assert Tier.parse("multimodal") is Tier.VISION
    with pytest.raises(ValueError):
        Tier.parse("ultra")


def test_level_parse():
    assert Level.parse("L2") is Level.L2
    assert Level.parse(3) is Level.L3
    assert Level.parse("1") is Level.L1
    assert Level.L0.label == "reflex"


def test_action_spec_validation():
    with pytest.raises(ValidationError):
        ActionSpec(kind="tool")
    wait = ActionSpec(kind="wait", tool="x", args={"a": 1}, wait_minutes=5)
    assert wait.tool is None and wait.args == {}
    assert wait.describe() == "wait 5m"
    with pytest.raises(ValidationError):
        ActionSpec.wait(-1)


def test_action_fingerprint_is_order_independent():
    a = ActionSpec(tool="web.fetch", args={"url": "x", "max": 1})
    b = ActionSpec(tool="web.fetch", args={"max": 1, "url": "x"})
    assert a.fingerprint() == b.fingerprint()
    assert ActionSpec.wait().fingerprint() == "wait"


def test_cost_addition():
    total = Cost(usd=0.1, tokens_in=10, llm_requests=1) + Cost(usd=0.2, tokens_out=5, tool_calls=1)
    assert total.usd == pytest.approx(0.3)
    assert total.tokens == 15
    assert total.llm_requests == 1 and total.tool_calls == 1


def test_clamping():
    p = Proposal(id="P1", author_role="x", action=ActionSpec.wait(), confidence=7)
    assert p.confidence == 1.0
    c = Critique(proposal_id="P1", author_role="critic", verdict="oppose", score=99)
    assert c.score == 5


def test_decision_wait_semantics():
    d = Decision(cycle_id="c1", level=Level.L0, wait_minutes=10)
    assert d.is_wait
    assert d.action.kind == "wait" and d.action.wait_minutes == 10
    p = Proposal(id="P1", author_role="planner", action=ActionSpec(tool="clock.now"))
    d2 = Decision(cycle_id="c1", level=Level.L1, chosen=p)
    assert not d2.is_wait and d2.describe().startswith("clock.now")


def test_tool_result_summary():
    assert ToolResult.success("done").summary() == "done"
    assert ToolResult.failure("boom").summary() == "error: boom"


def test_risk_labels():
    assert RiskTier.EXTERNAL_WRITE.label == "external-write"
    assert RiskTier(4) is RiskTier.IRREVERSIBLE
