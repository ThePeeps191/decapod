import asyncio

import httpx
import pytest

from decapod.control import issue_command
from decapod.daemon import Daemon
from decapod.domain import ActionSpec, GoalStatus, RunState
from decapod.evals.brain import scripted_brain
from decapod.runtime import Instance
from decapod.tools.builtin import web as web_mod
from tests.helpers import make_config

SEARCH_HTML = (
    '<div><a class="result__a" href="https://tides.example/">Tide pools</a>'
    '<a class="result__snippet">Tidal ecology basics</a></div>'
)


@pytest.fixture(autouse=True)
def offline_web(monkeypatch):
    async def ok(url, *, allow_private):
        return None

    monkeypatch.setattr(web_mod, "check_url", ok)


def make_instance(tmp_path, **kw):
    config = make_config(tmp_path / "inst")
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=SEARCH_HTML))
    )
    return config, Instance(config, provider=scripted_brain(), http=http, control_poll=0.02, **kw)


async def test_daemon_pursues_its_own_agenda(tmp_path):
    config, instance = make_instance(tmp_path)
    seen = []
    daemon = Daemon(config, instance=instance, max_cycles=6, on_event=seen.append)
    final = await daemon.run()
    assert final is RunState.STOPPED
    assert daemon.cycles_run == 6

    from decapod.storage import Database

    db = Database(config.paths.db)
    goals = db.query("SELECT title, status FROM goals")
    assert goals and goals[0]["status"] == GoalStatus.DONE.value, [dict(g) for g in goals]
    published = list((config.paths.drive / "published").glob("*.md"))
    assert any(p.name != "index.md" for p in published)
    tools = [r["tool"] for r in db.query("SELECT tool FROM tool_calls ORDER BY id")]
    assert tools[:4] == ["agenda.add_goal", "agenda.plan", "web.search", "notes.publish"]
    statuses = {r["status"] for r in db.query("SELECT status FROM cycles")}
    assert statuses == {"completed"}
    kinds = {e.kind for e in seen}
    assert {
        "daemon.start",
        "cycle.start",
        "caucus.decision",
        "tool.result",
        "cycle.end",
        "daemon.stop",
        "memory.episodic",
    } <= kinds
    assert db.get_state("run_state") == "stopped"
    assert not config.paths.pidfile.exists()
    lessons = db.scalar("SELECT COUNT(*) FROM memories WHERE kind = 'reflective'")
    assert lessons >= 1
    db.close()


async def test_kill_interrupts_a_running_cycle(tmp_path):
    config, instance = make_instance(tmp_path)
    brain = instance.llm.provider
    started = asyncio.Event()

    async def slow(request):
        started.set()
        await asyncio.sleep(30)
        raise AssertionError("should have been cancelled")

    original = brain.complete

    async def complete(request):
        if request.purpose.startswith("caucus.propose"):
            return await slow(request)
        return await original(request)

    brain.complete = complete
    daemon = Daemon(config, instance=instance)
    task = asyncio.create_task(daemon.run())
    await asyncio.wait_for(started.wait(), 10)
    issue_command(instance.db, "kill", {"reason": "test"})
    final = await asyncio.wait_for(task, 10)
    assert final is RunState.KILLED
    from decapod.storage import Database

    db = Database(config.paths.db)
    assert db.scalar("SELECT status FROM cycles") == "halted"
    assert db.get_state("run_state") == "killed"
    db.close()


async def test_stop_file_and_second_daemon(tmp_path):
    config, instance = make_instance(tmp_path)
    config.paths.stop_file.write_text("stop")
    final = await Daemon(config, instance=instance).run()
    assert final is RunState.STOPPED


async def test_approval_flow_end_to_end(tmp_path):
    """A tier-3 action is queued; once approved it executes at the next cycle."""
    from decapod.domain import RiskTier, SideEffect, ToolResult
    from decapod.tools.base import Tool, ToolArgs

    class AnnounceArgs(ToolArgs):
        text: str

    sent = []

    class Announce(Tool):
        name = "test.announce"
        description = "Announce something publicly."
        Args = AnnounceArgs
        tier = RiskTier.EXTERNAL_WRITE
        side_effect = SideEffect.EXTERNAL_WRITE
        required_scopes = ("notes.announce",)
        outbound = True

        async def run(self, args, ctx):
            sent.append(args.text)
            return ToolResult.success("announced")

    config, instance = make_instance(tmp_path, extra_tools=[Announce()])
    await instance.start()
    from decapod.runtime import Runtime

    runtime = Runtime(instance)
    from decapod.domain import Decision, Level, Proposal

    decision = Decision(
        cycle_id="c00001",
        level=Level.L3,
        chosen=Proposal(
            id="P1",
            author_role="operator",
            action=ActionSpec(tool="test.announce", args={"text": "hello"}),
            risk_tier=RiskTier.EXTERNAL_WRITE,
        ),
    )
    outcome = await runtime.act("c00001", decision)
    assert outcome.status == "approval_requested" and sent == []
    instance.approvals.resolve(outcome.approval_id, True, note="go ahead")
    assert await runtime.execute_approved("c00002") == 1
    assert sent == ["hello"]
    percepts = __import__("decapod.runtime.perceive", fromlist=["x"]).approval_percepts(instance)
    assert any("executed" in p.summary for p in percepts)
    await instance.close()
