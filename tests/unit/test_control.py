import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from decapod.config.settings import BudgetLimits, BudgetSettings, RateLimit
from decapod.control import Approvals, Budget, Controller, PolicyEngine, ToolCheck, issue_command
from decapod.domain import (
    ActionSpec,
    ApprovalStatus,
    BudgetExceeded,
    Cost,
    Halted,
    RiskTier,
    RunState,
    SideEffect,
    ToolResult,
)
from decapod.util.timeutil import to_iso

# --------------------------------------------------------------------------- budget


def make_budget(db, **windows):
    settings = BudgetSettings(
        cycle=windows.get("cycle", BudgetLimits()),
        hourly=windows.get("hourly", BudgetLimits()),
        daily=windows.get("daily", BudgetLimits()),
        lifetime=windows.get("lifetime", BudgetLimits()),
    )
    return Budget(db, settings)


def test_budget_blocks_when_exhausted(db):
    budget = make_budget(db, daily=BudgetLimits(llm_requests=2))
    llm = Cost(llm_requests=1)
    budget.authorize(llm, "c1")
    budget.record("llm", llm, "c1")
    budget.authorize(llm, "c1")
    budget.record("llm", llm, "c1")
    with pytest.raises(BudgetExceeded) as info:
        budget.authorize(llm, "c1")
    assert info.value.window == "daily" and info.value.resets_at is not None
    # tool calls are not blocked by an LLM limit
    budget.authorize(Cost(tool_calls=1), "c1")


def test_budget_usd_blocks_llm_after_limit_reached(db):
    budget = make_budget(db, lifetime=BudgetLimits(usd=1.0))
    budget.record("llm", Cost(usd=1.0, llm_requests=1), None)
    with pytest.raises(BudgetExceeded) as info:
        budget.authorize(Cost(llm_requests=1), None)
    assert info.value.is_lifetime
    assert budget.exhausted() is not None


def test_budget_cycle_window_and_status(db):
    budget = make_budget(db, cycle=BudgetLimits(tool_calls=1), daily=BudgetLimits(tokens=100))
    budget.record("tool", Cost(tool_calls=1), "c1")
    with pytest.raises(BudgetExceeded):
        budget.authorize(Cost(tool_calls=1), "c1")
    budget.authorize(Cost(tool_calls=1), "c2")  # a new cycle starts fresh
    budget.record("llm", Cost(tokens_in=80, llm_requests=1), "c2")
    warn = budget.warnings()
    assert any(w.metric == "tokens" and w.fraction == pytest.approx(0.8) for w in warn)
    statuses = budget.status("c1")
    assert {s.window for s in statuses} == {"cycle", "daily"}
    assert budget.spent_in_cycle("c2").tokens == 80


def test_budget_hourly_window_ignores_old_entries(db):
    budget = make_budget(db, hourly=BudgetLimits(llm_requests=1))
    old = datetime.now(UTC) - timedelta(hours=3)
    db.insert("INSERT INTO ledger(ts, kind, llm_requests) VALUES (?, 'llm', 5)", (to_iso(old),))
    budget.authorize(Cost(llm_requests=1), None)


def test_budget_record_noop_for_zero(db):
    budget = make_budget(db)
    budget.record("llm", Cost(), None)
    assert db.scalar("SELECT COUNT(*) FROM ledger") == 0


# --------------------------------------------------------------------------- policy


def tool(name="web.fetch", tier=RiskTier.READ, side=SideEffect.EXTERNAL_READ, scopes=None, **kw):
    return ToolCheck(
        name=name,
        tier=tier,
        side_effect=side,
        scopes=scopes if scopes is not None else ["web.read"],
        **kw,
    )


@pytest.fixture
def policy(settings, charter, db):
    return PolicyEngine(settings, charter, make_budget(db, daily=BudgetLimits(outbound=1)), db)


ACT = ActionSpec(tool="web.fetch", args={"url": "https://example.com"})


@pytest.mark.parametrize(
    ("check", "action", "outcome", "needle"),
    [
        (tool(), ACT, "allow", "all checks"),
        (None, ACT, "deny", "unknown tool"),
        (tool(args_error="url: missing"), ACT, "deny", "invalid arguments"),
        (tool(scopes=["shell.exec"]), ACT, "deny", "does not grant"),
        (
            tool(
                name="email.send",
                tier=RiskTier.EXTERNAL_WRITE,
                side=SideEffect.EXTERNAL_WRITE,
                scopes=["email.send"],
                outbound=True,
            ),
            ActionSpec(tool="email.send", args={"body": "hi"}),
            "approve",
            "approval",
        ),
        (
            tool(
                name="email.send",
                tier=RiskTier.EXTERNAL_WRITE,
                side=SideEffect.EXTERNAL_WRITE,
                scopes=["email.send"],
            ),
            ActionSpec(tool="email.send", args={"body": "my API_KEY is x"}),
            "deny",
            "prohibition",
        ),
        (
            tool(
                name="github.comment",
                tier=RiskTier.EXTERNAL_WRITE,
                side=SideEffect.EXTERNAL_WRITE,
                scopes=["github:someone/else.write"],
            ),
            ActionSpec(tool="github.comment", args={}),
            "deny",
            "does not grant",
        ),
        (
            tool(
                name="drive.write",
                tier=RiskTier.LOCAL_WRITE,
                side=SideEffect.LOCAL,
                scopes=["drive.write"],
            ),
            ActionSpec(tool="drive.write", args={"path": "a", "content": "api_key"}),
            "allow",
            "",
        ),
        (tool(), ActionSpec.wait(5), "allow", "waiting"),
    ],
)
def test_policy_table(policy, check, action, outcome, needle):
    verdict = policy.review(action, check, cycle_id="c1")
    assert verdict.outcome == outcome, verdict.reason
    assert needle in verdict.reason


def test_policy_halted_denies_everything(policy):
    verdict = policy.review(ACT, tool(), cycle_id=None, run_state=RunState.KILLED)
    assert verdict.outcome == "deny" and verdict.transient


def test_policy_preapproved_and_modes(policy, settings):
    send = tool(
        name="email.send",
        tier=RiskTier.EXTERNAL_WRITE,
        side=SideEffect.EXTERNAL_WRITE,
        scopes=["email.send"],
    )
    action = ActionSpec(tool="email.send", args={"body": "hello"})
    assert policy.review(action, send, cycle_id=None, preapproved=True).allowed
    settings.policy.scope_modes = {"email.*": "auto"}
    assert policy.review(action, send, cycle_id=None).allowed
    settings.policy.scope_modes = {"email.*": "auto", "email.send": "deny"}
    assert policy.review(action, send, cycle_id=None).outcome == "deny"
    settings.policy.scope_modes = {}
    settings.policy.modes[RiskTier.READ] = "approval"
    assert policy.review(ACT, tool(), cycle_id=None).outcome == "approve"


def test_policy_disabled_tool(policy, settings):
    settings.tools.disabled = ["web.*"]
    assert "disabled" in policy.review(ACT, tool(), cycle_id=None).reason


def test_policy_rate_limit(policy, settings, db):
    settings.policy.rate_limits = {"web.fetch": RateLimit(per_hour=2)}
    for _ in range(2):
        db.insert(
            "INSERT INTO tool_calls(ts, tool, args, risk_tier) VALUES (?, 'web.fetch', '{}', 1)",
            (to_iso(datetime.now(UTC)),),
        )
    verdict = policy.review(ACT, tool(), cycle_id=None)
    assert verdict.outcome == "deny" and verdict.transient and "rate limit" in verdict.reason


def test_policy_outbound_budget(policy, db):
    send = tool(
        name="email.send",
        tier=RiskTier.EXTERNAL_WRITE,
        side=SideEffect.EXTERNAL_WRITE,
        scopes=["email.send"],
        outbound=True,
    )
    action = ActionSpec(tool="email.send", args={"body": "x"})
    policy.budget.record("tool", Cost(tool_calls=1, outbound=1), None)
    verdict = policy.review(action, send, cycle_id=None, preapproved=True)
    assert verdict.outcome == "deny" and "outbound" in verdict.reason


def test_policy_consecutive_denials(policy):
    for _ in range(3):
        policy.review(ACT, None, cycle_id=None)
    assert policy.consecutive_denials == 3
    policy.review(ACT, tool(), cycle_id=None, run_state=RunState.KILLED)  # transient: no change
    assert policy.consecutive_denials == 3
    policy.review(ACT, tool(), cycle_id=None)
    assert policy.consecutive_denials == 0


# --------------------------------------------------------------------------- approvals


def test_approval_lifecycle(db, events):
    approvals = Approvals(db, events, ttl=timedelta(hours=1))
    action = ActionSpec(tool="email.send", args={"to": "a@b.c", "body": "hi"})
    a = approvals.enqueue(action, {"id": "P1"}, "needs approval", "c1")
    assert approvals.pending()[0].id == a.id
    approved = approvals.resolve(a.id, True, note="ok", edited_args={"to": "a@b.c", "body": "hey"})
    assert approved.status is ApprovalStatus.APPROVED and approved.action.args["body"] == "hey"
    with pytest.raises(ValueError, match="already"):
        approvals.resolve(a.id, False)
    assert [x.id for x in approvals.ready_to_execute()] == [a.id]
    approvals.mark_executed(a.id, ToolResult.success("sent"))
    assert approvals.unreported()[0].status is ApprovalStatus.EXECUTED
    approvals.mark_reported([a.id])
    assert approvals.unreported() == []
    kinds = [e.kind for e in events.recent(10)]
    assert "approval.requested" in kinds and "approval.approved" in kinds
    with pytest.raises(KeyError):
        approvals.resolve(999, True)


def test_approval_expiry(db, events):
    approvals = Approvals(db, events, ttl=timedelta(seconds=-1))
    a = approvals.enqueue(ActionSpec(tool="x"), {}, "r", None)
    with pytest.raises(ValueError, match="expired"):
        approvals.resolve(a.id, True)
    assert approvals.get(a.id).status is ApprovalStatus.EXPIRED
    b = approvals.enqueue(ActionSpec(tool="x"), {}, "r", None)
    assert [x.id for x in approvals.expire_due()] == [b.id]
    assert len(approvals.unreported()) == 2


# --------------------------------------------------------------------------- controller


async def test_controller_commands(db, events, tmp_path):
    ctl = Controller(db, events, tmp_path / "STOP", poll_interval=0.01)
    ctl.set_state(RunState.RUNNING)
    issue_command(db, "pause")
    ctl.poll()
    assert ctl.state is RunState.PAUSED

    async def resume_later():
        await asyncio.sleep(0.05)
        issue_command(db, "resume")

    task = asyncio.create_task(resume_later())
    await ctl.checkpoint("deliberate")  # blocks until resumed
    await task
    assert ctl.state is RunState.RUNNING
    assert db.get_state("phase") == "deliberate"

    issue_command(db, "stop", {"reason": "maintenance"})
    with pytest.raises(Halted) as info:
        await ctl.checkpoint()
    assert not info.value.kill and "maintenance" in info.value.reason
    ctl.set_state(RunState.RUNNING)  # cannot un-stop
    assert ctl.state is RunState.STOPPING


async def test_controller_kill_runs_hooks(db, events):
    ctl = Controller(db, events)
    ctl.set_state(RunState.RUNNING)
    killed = []
    ctl.on_kill(lambda: killed.append(True))
    ctl.on_kill(lambda: 1 / 0)  # a failing hook must not block others
    ctl.kill("test")
    assert killed == [True]
    with pytest.raises(Halted) as info:
        await ctl.checkpoint()
    assert info.value.kill
    ctl.set_state(RunState.RUNNING)
    assert ctl.state is RunState.KILLED


async def test_controller_stop_file_and_sleep(db, events, tmp_path):
    stop = tmp_path / "STOP"
    ctl = Controller(db, events, stop, poll_interval=0.01)
    ctl.set_state(RunState.SLEEPING)
    assert await ctl.sleep(0.02) is False
    issue_command(db, "wake")
    assert await ctl.sleep(5) is True
    stop.write_text("")
    assert "stop-file" in ctl.poll()
    assert ctl.halted


def test_unknown_command_rejected(db, events):
    db.insert("INSERT INTO control_commands(ts, command) VALUES ('x', 'explode')")
    ctl = Controller(db, events)
    assert ctl.poll() == []
    assert db.scalar("SELECT status FROM control_commands") == "rejected"
    with pytest.raises(ValueError):
        issue_command(db, "explode")


async def test_pause_is_not_overridden_by_scheduler_states(db, events):
    ctl = Controller(db, events, poll_interval=0.01)
    ctl.set_state(RunState.RUNNING)
    ctl.pause("operator")
    ctl.set_state(RunState.RUNNING, "cycle starting")
    ctl.set_state(RunState.SLEEPING, "keeping momentum")
    assert ctl.state is RunState.PAUSED
    ctl.resume()
    assert ctl.state is RunState.RUNNING
