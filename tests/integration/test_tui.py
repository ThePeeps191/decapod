import pytest

from decapod.control import Approvals
from decapod.daemon import Daemon
from decapod.domain import ActionSpec
from decapod.evals.brain import scripted_brain
from decapod.runtime import Instance
from decapod.storage import Database
from decapod.tui import DecapodTUI
from tests.helpers import make_config


@pytest.fixture
async def populated(tmp_path):
    config = make_config(tmp_path / "inst")
    instance = Instance(config, provider=scripted_brain(), control_poll=0.02)
    await Daemon(config, instance=instance, max_cycles=3).run()
    db = Database(config.paths.db)
    Approvals(db).enqueue(
        ActionSpec(tool="email.send", args={"to": "a@b.c", "body": "x"}),
        {"rationale": "say hi", "dissent": ["critic: unsolicited"]},
        "requires approval",
        "c00003",
    )
    db.close()
    return config


async def test_tui_shows_state_and_controls(populated):
    app = DecapodTUI(populated.paths, refresh_seconds=0.2)
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        status = str(app.query_one("#status").render())
        assert "STOPPED" in status
        assert app.query_one("#decisions").row_count == 3
        await pilot.press("3")
        await pilot.pause()
        assert app.selected_cycle == "c00003"
        await pilot.press("5")
        await pilot.pause()
        assert app.selected_approval == 1
        await pilot.click("#approve")
        await pilot.pause()
        await pilot.press("p")
        await pilot.press("k")
        await pilot.pause()
        await pilot.click("#confirm")
        await pilot.pause()
        await pilot.press("4")
        app.query_one("#memory-query").value = "briefing"
        await pilot.press("enter")
        await pilot.pause()
    db = Database(populated.paths.db)
    commands = [r["command"] for r in db.query("SELECT command FROM control_commands ORDER BY id")]
    assert "kill" in commands and "pause" in commands and "wake" in commands
    assert db.scalar("SELECT status FROM approvals WHERE id = 1") == "approved"
    db.close()


async def test_tui_note_flow(populated):
    app = DecapodTUI(populated.paths, refresh_seconds=0.5)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("n")
        await pilot.pause()
        await pilot.press(*"focus on kelp")
        await pilot.press("enter")
        await pilot.pause()
    db = Database(populated.paths.db)
    assert db.scalar("SELECT text FROM operator_notes") == "focus on kelp"
    db.close()
