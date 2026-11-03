"""Run-state control: pause, resume, stop and kill.

Commands arrive through the ``control_commands`` table (written by the TUI/CLI), a
``STOP`` file in the instance directory, POSIX signals, or internal trips (lifetime
budget exhausted, too many policy denials). ``checkpoint()`` is awaited at every phase
boundary, so a stop takes effect at the next safe point and a kill takes effect now.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from decapod.domain import ControlCommand, Halted, RunState
from decapod.storage import Database, EventLog, loads
from decapod.util.text import json_dumps
from decapod.util.timeutil import now, to_iso

log = logging.getLogger(__name__)
HALTED_STATES = (RunState.STOPPING, RunState.STOPPED, RunState.KILLED)


def issue_command(
    db: Database,
    command: ControlCommand | str,
    args: dict[str, Any] | None = None,
    *,
    by: str = "operator",
) -> int:
    """Queue a control command for the daemon (used by the CLI and TUI)."""
    cmd = ControlCommand(command)
    return db.insert(
        "INSERT INTO control_commands(ts, command, args, issued_by) VALUES (?, ?, ?, ?)",
        (to_iso(now()), cmd.value, json_dumps(args or {}), by),
    )


class Controller:
    def __init__(
        self,
        db: Database,
        events: EventLog,
        stop_file: Path | None = None,
        *,
        poll_interval: float = 0.5,
    ) -> None:
        self.db = db
        self.events = events
        self.stop_file = stop_file
        self.poll_interval = poll_interval
        self.state = RunState.STARTING
        self.reason = ""
        self.wake = asyncio.Event()
        self._kill_hooks: list[Callable[[], object]] = []
        self._paused_before: RunState | None = None

    # ------------------------------------------------------------------ hooks

    def on_kill(self, hook: Callable[[], object]) -> Callable[[], None]:
        self._kill_hooks.append(hook)

        def remove() -> None:
            with contextlib.suppress(ValueError):
                self._kill_hooks.remove(hook)

        return remove

    # ------------------------------------------------------------------ state

    @property
    def halted(self) -> bool:
        return self.state in HALTED_STATES

    def set_state(self, state: RunState, reason: str = "") -> None:
        if self.state is RunState.KILLED and state is not RunState.KILLED:
            return  # kill is terminal
        if self.state in (RunState.STOPPING, RunState.STOPPED) and state in (
            RunState.RUNNING,
            RunState.SLEEPING,
            RunState.PAUSED,
        ):
            return  # a stop cannot be undone by the agent's own scheduling
        previous = self.state
        self.state = state
        self.reason = reason
        self.db.set_states({"run_state": state.value, "run_state_reason": reason})
        if previous is not state:
            self.events.append(
                "control.state",
                f"{previous.value} → {state.value}" + (f": {reason}" if reason else ""),
                {"from": previous.value, "to": state.value, "reason": reason},
                actor="control",
            )

    def pause(self, reason: str = "operator pause") -> None:
        if self.halted or self.state is RunState.PAUSED:
            return
        self._paused_before = self.state
        self.set_state(RunState.PAUSED, reason)

    def resume(self, reason: str = "operator resume") -> None:
        if self.state is not RunState.PAUSED:
            self.wake.set()
            return
        self.set_state(RunState.RUNNING, reason)
        self._paused_before = None
        self.wake.set()

    def stop(self, reason: str = "operator stop") -> None:
        if self.halted:
            return
        self.set_state(RunState.STOPPING, reason)
        self.wake.set()

    def kill(self, reason: str = "operator kill") -> None:
        if self.state is RunState.KILLED:
            return
        self.set_state(RunState.KILLED, reason)
        for hook in list(self._kill_hooks):
            try:
                hook()
            except Exception:
                log.exception("kill hook failed")
        self.wake.set()

    def trip(self, reason: str, *, kill: bool = False) -> None:
        """Internal safety trip (budget exhausted, denial streak, …)."""
        self.events.append("control.trip", reason, {"kill": kill}, actor="control")
        if kill:
            self.kill(reason)
        else:
            self.stop(reason)

    # ------------------------------------------------------------------ polling

    def poll(self) -> list[str]:
        """Apply pending commands and the STOP file. Returns the commands applied."""
        applied: list[str] = []
        if self.stop_file is not None and self.stop_file.exists() and not self.halted:
            self.stop(f"STOP file present at {self.stop_file}")
            applied.append("stop-file")
        rows = self.db.query(
            "SELECT id, command, args, issued_by FROM control_commands WHERE status = 'pending' "
            "ORDER BY id"
        )
        for row in rows:
            command = row["command"]
            by = row["issued_by"]
            args = loads(row["args"], {})
            note = args.get("reason", "") if isinstance(args, dict) else ""
            reason = f"{command} by {by}" + (f": {note}" if note else "")
            try:
                cmd = ControlCommand(command)
            except ValueError:
                self._finish(row["id"], "rejected", f"unknown command {command!r}")
                continue
            if cmd is ControlCommand.PAUSE:
                self.pause(reason)
            elif cmd is ControlCommand.RESUME:
                self.resume(reason)
            elif cmd is ControlCommand.STOP:
                self.stop(reason)
            elif cmd is ControlCommand.KILL:
                self.kill(reason)
            elif cmd is ControlCommand.WAKE:
                self.wake.set()
            self._finish(row["id"], "done")
            applied.append(cmd.value)
        return applied

    def _finish(self, command_id: int, status: str, note: str = "") -> None:
        self.db.execute(
            "UPDATE control_commands SET status = ?, processed_at = ?, note = ? WHERE id = ?",
            (status, to_iso(now()), note, command_id),
        )

    async def checkpoint(self, phase: str | None = None) -> None:
        """Honor control state. Blocks while paused; raises ``Halted`` when stopping/killed."""
        self.poll()
        if phase is not None:
            self.db.set_state("phase", phase)
        while self.state is RunState.PAUSED:
            await asyncio.sleep(self.poll_interval)
            self.poll()
        if self.state is RunState.KILLED:
            raise Halted(self.reason or "killed", kill=True)
        if self.state in (RunState.STOPPING, RunState.STOPPED):
            raise Halted(self.reason or "stopped")

    async def sleep(self, seconds: float) -> bool:
        """Sleep until timeout or wake/command. Returns True if woken early.

        The caller clears ``wake`` before a sleep period; this method never does, so a wake
        command processed by a background poll in between is not lost.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, seconds)
        while True:
            self.poll()
            if self.halted or self.wake.is_set():
                return True
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.wake.wait(), timeout=min(self.poll_interval, remaining))
