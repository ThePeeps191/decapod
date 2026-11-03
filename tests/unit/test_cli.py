import json
import sys

import pytest

from decapod.cli import main


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "inst"
    monkeypatch.setenv("DECAPOD_HOME", str(root))
    for key in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "DEEPSEEK_API_KEY",
        "GEMINI_API_KEY",
        "MISTRAL_API_KEY",
        "GROQ_API_KEY",
        "TOGETHER_API_KEY",
        "XAI_API_KEY",
        "FIREWORKS_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("decapod.config.secrets.user_env_file", lambda: tmp_path / "user.env")
    monkeypatch.setattr("decapod.cli.setup.user_env_file", lambda: tmp_path / "user.env")
    return root


def test_init_and_friends(home, capsys):
    assert (
        main(
            [
                "init",
                "--yes",
                "--charter",
                "writer",
                "--name",
                "Ink",
                "--provider",
                "deepseek",
                "--api-key",
                "sk-test-123456",
            ]
        )
        == 0
    )
    assert (home / "decapod.toml").exists()
    assert 'name = "Ink"' in (home / "charter.toml").read_text()
    assert "DEEPSEEK_API_KEY=sk-test-123456" in (home / ".env").read_text()
    assert 'default_provider = "deepseek"' in (home / "decapod.toml").read_text()
    assert main(["init", "--yes"]) == 1  # refuses to overwrite
    assert main(["doctor", "--offline"]) == 0
    out = capsys.readouterr().out
    assert "deepseek:deepseek-chat" in out
    assert main(["status"]) == 0
    assert main(["charters"]) == 0
    assert "researcher" in capsys.readouterr().out


def test_commands_without_instance(home, capsys):
    assert main(["status"]) == 2
    assert "decapod init" in capsys.readouterr().err
    assert main([]) == 0


def test_offline_run_note_and_approvals(home, capsys):
    assert main(["init", "--yes", "--charter", "minimal"]) == 0
    toml = home / "decapod.toml"
    toml.write_text(toml.read_text().replace('min_interval = "30s"', 'min_interval = "0s"'))
    assert main(["note", "hello", "there"]) == 0
    assert main(["run", "--offline", "--cycles", "2", "--quiet"]) == 0
    assert main(["log", "-n", "5"]) == 0
    assert main(["show"]) == 0
    assert "Cycle c00002" in capsys.readouterr().out
    assert main(["replay", "c00001"]) == 0
    assert main(["approvals"]) == 0
    assert "No pending" in capsys.readouterr().out
    assert main(["approvals", "approve", "99"]) == 1
    assert main(["amendments"]) == 0
    assert main(["pause"]) == 0  # queued while not running
    assert main(["stop"]) == 0


def test_init_model_flags_and_bad_input(home):
    assert (
        main(
            [
                "init",
                "--yes",
                "--provider",
                "groq",
                "--model",
                "FLASH=a",
                "--model",
                "pro=b",
                "--model",
                "max=c",
            ]
        )
        == 0
    )
    text = (home / "decapod.toml").read_text()
    assert 'FLASH = "a"' in text and 'MAX = "c"' in text
    assert main(["init", "--yes", "--force", "--charter", "nope"]) == 1
    assert main(["init", "--yes", "--force", "--provider", "nope"]) == 1
    assert main(["init", "--yes", "--force", "--model", "bad"]) == 2


def test_eval_command(home, capsys):
    assert main(["eval", "steady_progress"]) == 0
    assert "1/1 passed" in capsys.readouterr().out
    assert main(["eval", "unknown"]) == 1
    json.dumps({})


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups and signals")
def test_background_start_stop_kill(home, capsys):
    from decapod.config import InstancePaths
    from decapod.daemon import running_pid
    from decapod.storage import Database

    assert main(["init", "--yes", "--charter", "minimal"]) == 0
    paths = InstancePaths(home)
    assert main(["start", "--offline"]) == 0
    pid = running_pid(paths.pidfile)
    assert pid is not None
    assert main(["start", "--offline"]) == 0  # idempotent
    assert "Already running" in capsys.readouterr().out
    assert main(["status"]) == 0
    assert main(["pause", "--reason", "test"]) == 0
    assert main(["resume"]) == 0
    assert main(["stop", "--timeout", "60"]) == 0
    assert running_pid(paths.pidfile) is None
    db = Database(paths.db)
    assert db.get_state("run_state") == "stopped"
    db.close()

    assert main(["start", "--offline"]) == 0
    assert main(["kill"]) == 0
    assert running_pid(paths.pidfile) is None
    paths.stop_file.write_text("")
    assert main(["start", "--offline"]) == 1
    assert main(["start", "--offline", "--clear-stop"]) == 0
    assert main(["kill"]) == 0


def test_start_refuses_without_models(home, capsys):
    assert main(["init", "--yes", "--charter", "minimal"]) == 0
    assert main(["start"]) == 2
    assert "model configuration" in capsys.readouterr().err


def test_init_name_is_escaped(home):
    from decapod.config import InstanceConfig

    assert main(["init", "--yes", "--charter", "minimal", "--name", 'Bob "the" bot\\']) == 0
    config = InstanceConfig.load(home)
    assert config.charter.name == 'Bob "the" bot\\'
    assert config.settings.instance.name == 'Bob "the" bot\\'
