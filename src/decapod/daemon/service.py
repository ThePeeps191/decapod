"""The daemon: runs cycles forever, sleeps adaptively, honors control commands."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
from collections.abc import Callable

from decapod.config import InstanceConfig
from decapod.control import Controller
from decapod.daemon.pidfile import PidFile
from decapod.daemon.scheduler import Scheduler, SleepPlan
from decapod.domain import Event, Halted, RunState
from decapod.llm import LLMProvider
from decapod.notify import Notifier
from decapod.report import build_report, save_report
from decapod.runtime import Instance, Runtime
from decapod.util.timeutil import from_iso, now, to_iso

log = logging.getLogger(__name__)
HEARTBEAT_S = 2.0
WAKE_CHECK_S = 1.0


class Daemon:
    def __init__(
        self,
        config: InstanceConfig,
        *,
        provider: LLMProvider | None = None,
        instance: Instance | None = None,
        max_cycles: int | None = None,
        on_event: Callable[[Event], None] | None = None,
        use_pidfile: bool = True,
        control_poll: float = 0.5,
    ) -> None:
        self.config = config
        self.instance = instance or Instance(config, provider=provider, control_poll=control_poll)
        self.runtime = Runtime(self.instance)
        self.scheduler = Scheduler(config.settings.schedule)
        self.max_cycles = max_cycles
        self.on_event = on_event
        self.use_pidfile = use_pidfile
        self._cycle_task: asyncio.Task[object] | None = None
        self.cycles_run = 0
        self.notifier: Notifier | None = None

    @property
    def controller(self) -> Controller:
        return self.instance.controller

    async def run(self) -> RunState:
        """Run until stopped, killed, out of lifetime budget or ``max_cycles`` reached."""
        inst = self.instance
        pidfile = PidFile(self.config.paths.pidfile) if self.use_pidfile else None
        if pidfile is not None:
            pidfile.acquire()
        if self.on_event is not None:
            inst.bus.subscribe(self.on_event)
        notifier = Notifier(
            self.config.settings.notify, inst.secrets, inst.http, self.config.charter.name
        )
        if notifier.enabled:
            inst.bus.subscribe(notifier.handle)
        self.notifier = notifier
        ctl = inst.controller
        restore_signals = self._install_signals()
        heartbeat = asyncio.create_task(self._heartbeat(), name="decapod-heartbeat")
        watcher = asyncio.create_task(self._watch(), name="decapod-control")
        ctl.on_kill(self._cancel_cycle)
        final = RunState.STOPPED
        try:
            await inst.start()
            inst.db.set_states({"pid": os.getpid(), "started_at": to_iso(now()), "next_wake": None})
            inst.events.append(
                "daemon.start",
                f"daemon started (pid {os.getpid()})",
                {
                    "pid": os.getpid(),
                    "tools": inst.registry.names(),
                    "unavailable_tools": inst.registry.unavailable,
                    "routes": {t.value: str(r) for t, r in inst.routing.routes.items()},
                },
                actor="daemon",
            )
            ctl.set_state(RunState.RUNNING, "daemon started")
            final = await self._loop()
        except Halted as exc:
            final = RunState.KILLED if exc.kill else RunState.STOPPED
        except asyncio.CancelledError:
            final = RunState.KILLED if ctl.state is RunState.KILLED else RunState.STOPPED
        finally:
            heartbeat.cancel()
            watcher.cancel()
            for task in (heartbeat, watcher):
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            restore_signals()
            if ctl.state is RunState.KILLED:
                final = RunState.KILLED
            inst.procs.kill_all()
            with contextlib.suppress(Exception):
                ctl.set_state(final, ctl.reason or "daemon exited")
                inst.db.set_states({"pid": None, "next_wake": None, "phase": "stopped"})
                inst.events.append(
                    "daemon.stop",
                    f"daemon {final.value}",
                    {"state": final.value, "reason": ctl.reason, "cycles": self.cycles_run},
                    actor="daemon",
                )
            with contextlib.suppress(Exception):
                await notifier.drain()  # deliver the stop notice before the client closes
            await inst.close()
            if pidfile is not None:
                pidfile.release()
        return final

    async def _loop(self) -> RunState:
        inst = self.instance
        ctl = inst.controller
        while True:
            await ctl.checkpoint("idle")
            ctl.set_state(RunState.RUNNING, "cycle starting")
            self._cycle_task = asyncio.create_task(self.runtime.run_cycle(), name="decapod-cycle")
            try:
                result = await self._cycle_task
            except asyncio.CancelledError:
                if ctl.state is RunState.KILLED:
                    raise Halted(ctl.reason or "killed", kill=True) from None
                raise
            finally:
                self._cycle_task = None
            self.cycles_run += 1
            self._maybe_report()
            if result.lifetime_exhausted:
                ctl.trip("lifetime budget exhausted")
                await ctl.checkpoint("idle")
            if self.max_cycles is not None and self.cycles_run >= self.max_cycles:
                ctl.stop(f"completed {self.cycles_run} cycle(s) as requested")
                await ctl.checkpoint("idle")
            plan = self.scheduler.plan(result)
            await self._sleep(plan)

    def _maybe_report(self) -> None:
        """Write a periodic progress report when one is due ([report] every)."""
        every = self.config.settings.report.every
        if every is None:
            return
        db = self.instance.db
        last = db.get_state("last_report_at")
        current = now()
        if last is None:
            db.set_state("last_report_at", to_iso(current))  # first report after one period
            return
        if current - from_iso(last) < every:
            return
        try:
            report = build_report(db, self.config.charter, every)
            path = save_report(report, self.config.paths.drive)
        except Exception as exc:  # a report must never take the agent down
            log.exception("report failed")
            self.instance.events.append("report.error", f"report failed: {exc}", {}, actor="daemon")
            return
        db.set_state("last_report_at", to_iso(current))
        rel = path.relative_to(self.config.paths.drive).as_posix()
        self.instance.events.append(
            "report.created", f"{report.summary} (drive/{rel})", {"path": rel}, actor="daemon"
        )

    async def _sleep(self, plan: SleepPlan) -> None:
        inst = self.instance
        ctl = inst.controller
        wake_at = now().timestamp() + plan.seconds
        inst.db.set_states({"next_wake": wake_at, "sleep_reason": plan.reason})
        ctl.set_state(RunState.SLEEPING, plan.reason)
        inst.events.append(
            "daemon.sleep",
            f"sleeping {plan.seconds:.0f}s: {plan.reason}",
            {"seconds": plan.seconds, "reason": plan.reason, "wake_on": list(plan.wake_on)},
            actor="daemon",
        )
        ctl.wake.clear()
        remaining = plan.seconds
        while remaining > 0:
            woke = await ctl.sleep(min(WAKE_CHECK_S, remaining))
            if ctl.state is RunState.PAUSED:
                await ctl.checkpoint("paused")
            if woke or ctl.halted:
                break
            reason = self._wake_reason(plan)
            if reason:
                inst.events.append(
                    "daemon.wake", f"woken early: {reason}", {"reason": reason}, actor="daemon"
                )
                break
            remaining = wake_at - now().timestamp()
        inst.db.set_states({"next_wake": None, "sleep_reason": None})

    def _wake_reason(self, plan: SleepPlan) -> str | None:
        db = self.instance.db
        if "operator" in plan.wake_on and db.scalar(
            "SELECT COUNT(*) FROM operator_notes WHERE delivered = 0", (), 0
        ):
            return "a message from the operator"
        if "approval" in plan.wake_on and db.scalar(
            "SELECT COUNT(*) FROM approvals WHERE status = 'approved' OR "
            "(reported = 0 AND status IN ('denied', 'expired', 'executed'))",
            (),
            0,
        ):
            return "an approval was resolved"
        return None

    async def _heartbeat(self) -> None:
        while True:
            with contextlib.suppress(Exception):
                self.instance.db.set_state("heartbeat", now().timestamp())
            await asyncio.sleep(HEARTBEAT_S)

    async def _watch(self) -> None:
        """Poll control commands while a cycle runs, so a kill lands mid-call."""
        ctl = self.instance.controller
        while True:
            await asyncio.sleep(ctl.poll_interval)
            with contextlib.suppress(Exception):
                ctl.poll()

    def _cancel_cycle(self) -> None:
        task = self._cycle_task
        if task is not None and not task.done():
            task.cancel()

    def _install_signals(self) -> Callable[[], None]:
        loop = asyncio.get_running_loop()
        ctl = self.instance.controller
        installed: list[int] = []

        def on_term() -> None:
            if ctl.state is RunState.STOPPING:
                ctl.kill("second termination signal")
            else:
                ctl.stop("termination signal received")

        if sys.platform != "win32":
            for sig in (signal.SIGTERM, signal.SIGINT):
                with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                    loop.add_signal_handler(sig, on_term)
                    installed.append(sig)

        def restore() -> None:
            for sig in installed:
                with contextlib.suppress(Exception):
                    loop.remove_signal_handler(sig)

        return restore
