"""The ``decapod`` command line."""

from __future__ import annotations

import argparse
import asyncio
import fnmatch
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Any

from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from decapod import __version__
from decapod.cli.output import ago, console, err, format_event, print_event
from decapod.config import InstanceConfig, InstancePaths
from decapod.domain import ConfigError, ControlCommand, DecapodError, Tier

Handler = Callable[[argparse.Namespace], int]


# --------------------------------------------------------------------------- helpers


def _paths(args: argparse.Namespace) -> InstancePaths:
    return InstancePaths.resolve(args.dir, args.instance)


def _require_instance(args: argparse.Namespace) -> InstancePaths:
    paths = _paths(args)
    if not paths.exists():
        raise ConfigError(
            f"no Decapod instance at {paths.root} (run `decapod init` first, or "
            "pass --dir / --instance)"
        )
    return paths


def _load(args: argparse.Namespace) -> InstanceConfig:
    paths = _require_instance(args)
    return InstanceConfig.load(paths.root)


def _db(args: argparse.Namespace) -> Any:
    from decapod.storage import Database

    return Database(_require_instance(args).db)


def _running_pid(paths: InstancePaths) -> int | None:
    from decapod.daemon import running_pid

    return running_pid(paths.pidfile)


def _wait_for_exit(pid: int, timeout: float) -> bool:
    from decapod.daemon import pid_alive

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.2)
    return not pid_alive(pid)


# --------------------------------------------------------------------------- init / doctor


def cmd_init(args: argparse.Namespace) -> int:
    from decapod.cli.setup import run_init

    models: dict[Tier, str] = {}
    for item in args.model or []:
        tier, _, model = item.partition("=")
        if not model:
            raise ConfigError(f"--model expects TIER=MODEL, got {item!r}")
        models[Tier.parse(tier)] = model
    return run_init(
        args.dir,
        args.instance,
        charter=args.charter,
        name=args.name,
        provider=args.provider,
        api_key=args.api_key,
        models=models,
        yes=args.yes,
        force=args.force,
        user_wide_key=args.user_key,
    )


def cmd_doctor(args: argparse.Namespace) -> int:
    from decapod.config.providers import resolve_routes
    from decapod.config.secrets import user_env_file
    from decapod.tools import ToolRegistry, builtin_tools
    from decapod.tools.sandbox import bwrap_path

    config = _load(args)
    ok = True

    def check(label: str, passed: bool, detail: str = "", *, warn: bool = False) -> None:
        nonlocal ok
        mark = "[green]✓[/]" if passed else ("[yellow]![/]" if warn else "[red]✗[/]")
        if not passed and not warn:
            ok = False
        console.print(f" {mark} {label}" + (f" [dim]— {escape(detail)}[/]" if detail else ""))

    console.print(f"[bold]Instance[/] {config.paths.root}")
    check("config", True, str(config.paths.config))
    check("charter", True, f"{config.charter.name}: {config.charter.identity.mission[:70]}")
    routing = resolve_routes(config.settings.llm, config.secrets)
    console.print("[bold]Models[/]")
    for tier in Tier:
        route = routing.routes.get(tier)
        check(tier.value, route is not None, str(route) if route else "not configured")
    for problem in routing.problems:
        check("routing", False, problem)
    if routing.problems:
        console.print(
            f"   [dim]API keys are read from the environment, {config.paths.env} "
            f"and {user_env_file()}[/]"
        )
    if routing.ok and not args.offline:
        ok &= asyncio.run(_ping_models(config))
    console.print("[bold]Tools[/]")
    registry = ToolRegistry(config.settings, config.secrets)
    for tool in builtin_tools():
        registry.register(tool)
    granted = [
        t.name
        for t in registry.tools()
        if all(
            any(fnmatch.fnmatchcase(s, g) for g in config.charter.scopes.granted)
            for s in t.required_scopes
        )
    ]
    check("available to this charter", bool(granted), ", ".join(granted))
    by_reason: dict[str, list[str]] = {}
    for name, reason in registry.unavailable.items():
        by_reason.setdefault(reason, []).append(name)
    for reason, names in by_reason.items():
        check(", ".join(names), False, reason, warn=True)
    check(
        "bubblewrap sandbox",
        bwrap_path() is not None,
        bwrap_path() or "not installed: code.run stays disabled unless isolation = 'none'",
        warn=True,
    )
    from decapod.daemon import running_pid

    pid = running_pid(config.paths.pidfile)
    check("daemon", True, f"running (pid {pid})" if pid else "not running")
    console.print("[green]All good.[/]" if ok else "[red]Some checks failed.[/]")
    return 0 if ok else 1


async def _ping_models(config: InstanceConfig) -> bool:
    from pydantic import BaseModel

    from decapod.config.providers import resolve_routes
    from decapod.llm import LLM, Message, build_provider

    class Ping(BaseModel):
        ok: bool

    routing = resolve_routes(config.settings.llm, config.secrets)
    settings = config.settings.llm.model_copy(update={"max_retries": 1, "fallbacks": {}})
    llm = LLM(settings, build_provider(settings, config.secrets, routing), routing=routing)
    all_ok = True
    tested: set[str] = set()
    try:
        for tier, route in routing.routes.items():
            if str(route) in tested:
                continue
            tested.add(str(route))
            started = time.monotonic()
            try:
                result = await llm.structured(
                    [Message.user("Health check. Set ok to true.")],
                    Ping,
                    tier=tier,
                    purpose="doctor.structured",
                )
                ms = (time.monotonic() - started) * 1000
                console.print(
                    f" [green]✓[/] {route} answered ({ms:.0f} ms, structured "
                    f"output {'ok' if result.ok else 'odd'})"
                )
            except DecapodError as exc:
                all_ok = False
                console.print(f" [red]✗[/] {route}: {escape(str(exc))[:300]}")
    finally:
        await llm.close()
    return all_ok


# --------------------------------------------------------------------------- run / start / stop


def cmd_run(args: argparse.Namespace) -> int:
    from decapod.daemon import AlreadyRunning, Daemon
    from decapod.domain import Event
    from decapod.logs import setup_logging

    config = _load(args)
    setup_logging(
        config.paths.logs, level="DEBUG" if args.debug else "WARNING", console=not args.quiet
    )
    provider = None
    if args.offline:
        from decapod.evals import scripted_brain

        provider = scripted_brain()

    def on_event(event: Event) -> None:
        if not args.quiet:
            print_event(event, verbose=args.verbose)

    if not args.quiet:
        console.print(
            f"[bold]Decapod[/] {__version__} · {config.charter.name} · "
            f"{config.paths.root}"
            + (" · [yellow]offline scripted brain[/]" if args.offline else "")
        )
        console.print("[dim]Ctrl+C stops after the current step; press it twice to kill.[/]")
    daemon = Daemon(config, provider=provider, max_cycles=args.cycles, on_event=on_event)
    try:
        final = asyncio.run(daemon.run())
    except AlreadyRunning as exc:
        err.print(f"[red]{exc}[/]")
        return 1
    except KeyboardInterrupt:
        return 130
    if not args.quiet:
        console.print(f"Daemon {final.value} after {daemon.cycles_run} cycle(s).")
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    paths = _require_instance(args)
    InstanceConfig.load(paths.root)  # fail fast on config errors
    pid = _running_pid(paths)
    if pid:
        console.print(f"Already running (pid {pid}).")
        return 0
    if paths.stop_file.exists():
        if not args.clear_stop:
            err.print(
                f"[red]A STOP file exists at {paths.stop_file}[/]; remove it or pass --clear-stop."
            )
            return 1
        paths.stop_file.unlink()
    paths.logs.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "-m", "decapod", "--dir", str(paths.root), "run", "--quiet"]
    if args.offline:
        command.append("--offline")
    log_file = paths.daemon_log.open("ab")
    kwargs: dict[str, Any] = {
        "stdout": log_file,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
        "cwd": str(paths.root),
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED | NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(command, **kwargs)
    log_file.close()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            err.print(
                f"[red]Daemon exited during startup (code {proc.returncode}).[/] See "
                f"{paths.daemon_log}"
            )
            return 1
        if _running_pid(paths) == proc.pid:
            console.print(f"[green]Started[/] Decapod (pid {proc.pid}). Logs: {paths.daemon_log}")
            console.print("Watch it with [bold]decapod tui[/]; stop it with [bold]decapod stop[/].")
            return 0
        time.sleep(0.2)
    err.print("[yellow]The daemon has not reported in yet;[/] check `decapod status`.")
    return 0


def _control(args: argparse.Namespace, command: ControlCommand, reason: str = "") -> int:
    from decapod.control import issue_command

    paths = _require_instance(args)
    pid = _running_pid(paths)
    db = _db(args)
    try:
        issue_command(db, command, {"reason": reason} if reason else None, by="cli")
    finally:
        db.close()
    if pid is None:
        console.print(
            f"[yellow]Decapod is not running;[/] the {command.value} command was "
            "queued for the next start."
        )
    else:
        console.print(f"Sent {command.value} to pid {pid}.")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    paths = _require_instance(args)
    pid = _running_pid(paths)
    if pid is None:
        console.print("Decapod is not running.")
        return 0
    _control(args, ControlCommand.STOP, args.reason or "")
    if args.no_wait:
        return 0
    console.print("Waiting for the current step to finish…")
    if _wait_for_exit(pid, args.timeout):
        console.print("[green]Stopped.[/]")
        return 0
    err.print(f"[yellow]Still running after {args.timeout:.0f}s;[/] use `decapod kill`.")
    return 1


def cmd_kill(args: argparse.Namespace) -> int:
    from decapod.daemon import terminate

    paths = _require_instance(args)
    pid = _running_pid(paths)
    if pid is None:
        console.print("Decapod is not running.")
        return 0
    _control(args, ControlCommand.KILL, args.reason or "")
    if _wait_for_exit(pid, 5):
        console.print("[green]Killed.[/]")
        return 0
    terminate(pid)
    if _wait_for_exit(pid, 10):
        console.print("[green]Killed (SIGTERM).[/]")
        return 0
    terminate(pid, force=True)
    _wait_for_exit(pid, 5)
    console.print("[green]Killed (SIGKILL).[/]")
    return 0


def cmd_pause(args: argparse.Namespace) -> int:
    return _control(args, ControlCommand.PAUSE, args.reason or "")


def cmd_resume(args: argparse.Namespace) -> int:
    return _control(args, ControlCommand.RESUME)


def cmd_wake(args: argparse.Namespace) -> int:
    return _control(args, ControlCommand.WAKE)


# --------------------------------------------------------------------------- inspection


def cmd_status(args: argparse.Namespace) -> int:
    from decapod.control import Approvals, Budget
    from decapod.memory import AgendaStore

    config = _load(args)
    db = _db(args)
    try:
        state = db.all_state()
        pid = _running_pid(config.paths)
        run_state = (
            state.get("run_state", "never started")
            if pid
            else ("stopped" if state.get("run_state") not in ("killed",) else "killed")
        )
        heartbeat = state.get("heartbeat")
        lines = [
            f"[bold]{config.charter.name}[/] · {config.paths.root}",
            f"state      [bold]{run_state}[/]" + (f" (pid {pid})" if pid else ""),
            f"heartbeat  {ago(heartbeat)}" if pid else "",
            f"phase      {state.get('phase') or '-'}  cycle {state.get('current_cycle') or '-'}"
            if pid
            else "",
            f"next wake  {ago(state.get('next_wake'))} ({state.get('sleep_reason')})"
            if pid and state.get("next_wake")
            else "",
            f"last cycle {state.get('last_cycle') or '-'} "
            f"({state.get('last_cycle_status') or '-'})",
            f"cycles     {db.scalar('SELECT COUNT(*) FROM cycles', (), 0)}",
        ]
        console.print(
            Panel("\n".join(line for line in lines if line), title="Decapod", expand=False)
        )
        budget = Budget(db, config.settings.budget)
        table = Table("window", "metric", "used", "limit", "", box=None, pad_edge=False)
        for s in budget.status():
            color = "red" if s.fraction >= 1 else "yellow" if s.fraction >= 0.8 else "green"
            table.add_row(
                s.window, s.metric, f"{s.used:g}", f"{s.limit:g}", f"[{color}]{s.fraction:.0%}[/]"
            )
        console.print(table)
        console.print("\n[bold]Agenda[/]")
        console.print(escape(AgendaStore(db).render()))
        pending = Approvals(db).pending()
        if pending:
            console.print(
                f"\n[bold yellow]{len(pending)} approval(s) pending[/] — `decapod approvals`"
            )
    finally:
        db.close()
    return 0


def cmd_log(args: argparse.Namespace) -> int:
    from decapod.storage import read_events, read_recent

    db = _db(args)
    kinds = args.kind or None
    try:
        if args.cycle:
            events = read_events(db, cycle_id=args.cycle, limit=100_000, kinds=kinds)
        else:
            events = read_recent(db, limit=args.lines, kinds=kinds)
        for event in events:
            print_event(event, verbose=args.verbose or bool(kinds))
        last = events[-1].id if events else int(db.scalar("SELECT MAX(id) FROM events", (), 0))
        while args.follow:
            time.sleep(0.5)
            for event in read_events(db, after_id=last, kinds=kinds):
                print_event(event, verbose=args.verbose or bool(kinds))
                last = event.id
    except KeyboardInterrupt:
        pass
    finally:
        db.close()
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    from decapod.storage import read_events

    db = _db(args)
    try:
        cycle = args.cycle or db.scalar("SELECT id FROM cycles ORDER BY seq DESC LIMIT 1")
        if not cycle:
            console.print("No cycles yet.")
            return 0
        events = read_events(db, cycle_id=cycle, limit=100_000)
        if not events:
            err.print(f"[red]No events for cycle {cycle}[/]")
            return 1
        console.print(f"[bold]Cycle {cycle}[/]")
        for event in events:
            if event.kind in ("llm.call", "cycle.context"):
                continue
            console.print(format_event(event, show_cycle=False))
            payload = event.payload
            if event.kind == "caucus.proposal":
                console.print(f"    [dim]rationale:[/] {escape(payload.get('rationale', ''))}")
            elif event.kind == "caucus.critique" and payload.get("concerns"):
                console.print(f"    [dim]concerns:[/] {escape('; '.join(payload['concerns']))}")
            elif event.kind == "caucus.decision":
                console.print(f"    [dim]rationale:[/] {escape(payload.get('rationale', ''))}")
                for dissent in payload.get("dissent", []):
                    console.print(f"    [yellow]dissent:[/] {escape(dissent)}")
            elif event.kind == "tool.result":
                text = (payload.get("output") or payload.get("error") or "")[:800]
                console.print(f"    [dim]{escape(text)}[/]")
    finally:
        db.close()
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    from decapod.runtime.replay import replay_cycle

    config = _load(args)
    report = asyncio.run(replay_cycle(config, args.cycle))
    console.print(f"[bold]Replay of {report.cycle_id}[/] (level {report.level.name})")
    console.print(f"  recorded: {escape(report.recorded)}")
    console.print(f"  replayed: {escape(report.replayed or '(none)')}")
    if report.charter_changed:
        console.print("  [yellow]the charter changed since this cycle ran[/]")
    if report.misses:
        console.print(f"  [yellow]unrecorded model calls:[/] {', '.join(report.misses)}")
    if report.error:
        console.print(f"  [red]error:[/] {escape(report.error)}")
    console.print(
        "[green]Deterministic: same decision.[/]" if report.matches else "[red]Diverged.[/]"
    )
    return 0 if report.matches else 1


# --------------------------------------------------------------------------- operator actions


def cmd_note(args: argparse.Namespace) -> int:
    from decapod.control import issue_command
    from decapod.util.timeutil import now, to_iso

    text = " ".join(args.text).strip()
    if not text:
        err.print("[red]Empty note.[/]")
        return 1
    db = _db(args)
    try:
        db.insert("INSERT INTO operator_notes(ts, text) VALUES (?, ?)", (to_iso(now()), text))
        issue_command(db, ControlCommand.WAKE, by="cli")
    finally:
        db.close()
    console.print(
        "Delivered at the next cycle (Decapod weighs it against its charter; it is not an order)."
    )
    return 0


def cmd_approvals(args: argparse.Namespace) -> int:
    from decapod.control import Approvals, issue_command
    from decapod.storage import EventLog

    db = _db(args)
    try:
        approvals = Approvals(db, EventLog(db))
        if args.action in (None, "list"):
            items = approvals.fetch() if args.all else approvals.pending()
            if not items:
                console.print("No pending approvals." if not args.all else "No approvals.")
                return 0
            for a in items:
                console.print(
                    Panel(
                        f"[bold]{escape(a.action.describe())}[/]\n"
                        f"reason: {escape(a.reason)}\n"
                        f"rationale: {escape(str(a.proposal.get('rationale', '')))}\n"
                        + "".join(
                            f"[yellow]dissent:[/] {escape(d)}\n"
                            for d in a.proposal.get("dissent", [])
                        )
                        + f"[dim]args: {escape(json.dumps(a.action.args, indent=1)[:1500])}[/]",
                        title=f"#{a.id} · {a.status.value} · cycle {a.cycle_id}",
                        subtitle=f"expires {a.expires_at.astimezone():%Y-%m-%d %H:%M}",
                        expand=False,
                    )
                )
            return 0
        if args.id is None:
            err.print("[red]Give the approval id.[/]")
            return 1
        edited = json.loads(args.args) if getattr(args, "args", None) else None
        result = approvals.resolve(
            args.id, args.action == "approve", by="cli", note=args.note or "", edited_args=edited
        )
        issue_command(db, ControlCommand.WAKE, by="cli")
        console.print(f"#{result.id} {result.status.value}.")
    except (KeyError, ValueError) as exc:
        err.print(f"[red]{escape(str(exc))}[/]")
        return 1
    finally:
        db.close()
    return 0


def cmd_amendments(args: argparse.Namespace) -> int:
    db = _db(args)
    try:
        rows = db.query("SELECT * FROM amendments ORDER BY id DESC LIMIT 50")
        if not rows:
            console.print("No charter amendments proposed.")
        for row in rows:
            console.print(
                Panel(
                    f"{escape(row['text'])}\n\n[dim]why:[/] {escape(row['rationale'])}",
                    title=f"#{row['id']} {row['clause'] or 'new clause'} · {row['ts'][:16]}",
                    expand=False,
                )
            )
        if rows:
            console.print(
                "Decapod cannot edit its charter. To accept a proposal, edit "
                "charter.toml yourself; it is reloaded at the next cycle."
            )
    finally:
        db.close()
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    from decapod.config.providers import resolve_routes
    from decapod.llm import LLM, Message, build_provider

    config = _load(args)
    routing = resolve_routes(config.settings.llm, config.secrets)
    if not routing.ok:
        raise ConfigError("; ".join(routing.problems))

    async def go() -> str:
        llm = LLM(
            config.settings.llm,
            build_provider(config.settings.llm, config.secrets, routing),
            routing=routing,
        )
        try:
            out = await llm.chat(
                [Message.user(" ".join(args.prompt))], tier=Tier.parse(args.tier), purpose="cli.ask"
            )
            return (
                f"[dim]{out.model} · {out.cost.tokens} tokens · {out.latency_ms} ms[/]\n"
                + escape(out.content)
            )
        finally:
            await llm.close()

    console.print(asyncio.run(go()))
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    from decapod.domain import Level
    from decapod.evals.scenarios import SCENARIOS, run_scenario

    names = args.scenario or sorted(SCENARIOS)
    unknown = [n for n in names if n not in SCENARIOS]
    if unknown:
        err.print(
            f"[red]Unknown scenario(s): {', '.join(unknown)}[/] "
            f"(available: {', '.join(sorted(SCENARIOS))})"
        )
        return 1
    real = None
    if args.real:
        config = _load(args)
        from decapod.config.providers import resolve_routes

        routing = resolve_routes(config.settings.llm, config.secrets)
        if not routing.ok:
            raise ConfigError("; ".join(routing.problems))
        real = (config.settings.llm, config.secrets)
    levels = [Level.L1, Level.L3] if args.compare_levels else [Level.parse(args.max_level)]
    table = Table(
        "scenario",
        "level",
        "result",
        "cycles",
        "LLM calls",
        "tokens",
        "USD",
        "details",
        title="Decapod evals" + (" (real model)" if args.real else " (scripted brain)"),
    )
    passed = 0
    total = 0
    for name in names:
        for level in levels:
            total += 1
            result = asyncio.run(run_scenario(SCENARIOS[name], real=real, max_level=level))
            passed += result.passed
            m = result.metrics
            table.add_row(
                name,
                level.name,
                "[green]pass[/]" if result.passed else "[red]fail[/]",
                str(m.cycles),
                str(m.llm_requests),
                f"{m.tokens:,}",
                f"{m.usd:.4f}",
                escape(result.details),
            )
    console.print(table)
    console.print(f"{passed}/{total} passed")
    return 0 if passed == total else 1


def cmd_charters(args: argparse.Namespace) -> int:
    from decapod.cli.setup import charter_summary, example_charters

    for name, text in example_charters().items():
        console.print(f"[cyan]{name:<11}[/] {escape(charter_summary(text))}")
    console.print("\nUse one with `decapod init --charter NAME`.")
    return 0


def cmd_tui(args: argparse.Namespace) -> int:
    from decapod.tui import run_tui

    paths = _require_instance(args)
    return run_tui(paths)


# --------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="decapod",
        description="Decapod: an autonomous, self-directed AI agent with hard safety walls.",
    )
    parser.add_argument("--version", action="version", version=f"decapod {__version__}")
    parser.add_argument(
        "--dir", help="instance directory (default: $DECAPOD_HOME or the platform data dir)"
    )
    parser.add_argument("--instance", "-i", help="instance name under the data dir")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    def add(name: str, handler: Handler, help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text, description=help_text)
        p.set_defaults(handler=handler)
        return p

    p = add("init", cmd_init, "create an instance (config, charter, API key)")
    p.add_argument("--charter", help="example name (see `decapod charters`) or a path")
    p.add_argument("--name", help="the agent's name")
    p.add_argument("--provider", help="default model provider, e.g. anthropic, openrouter")
    p.add_argument("--api-key", help="API key for --provider (stored in a .env file)")
    p.add_argument(
        "--model",
        action="append",
        metavar="TIER=MODEL",
        help="pin a tier's model, e.g. --model FLASH=claude-haiku-5-5",
    )
    p.add_argument(
        "--user-key",
        action="store_true",
        help="store the key in the user-wide .env instead of the instance's",
    )
    p.add_argument("--yes", "-y", action="store_true", help="non-interactive")
    p.add_argument("--force", action="store_true", help="overwrite config and charter")

    p = add("doctor", cmd_doctor, "check configuration, models, keys and tools")
    p.add_argument("--offline", action="store_true", help="skip live model calls")

    p = add("run", cmd_run, "run in the foreground, printing what happens")
    p.add_argument("--cycles", type=int, help="stop after N cycles")
    p.add_argument(
        "--offline",
        action="store_true",
        help="use the built-in scripted brain instead of a model (demo)",
    )
    p.add_argument("--verbose", "-v", action="store_true", help="show every event")
    p.add_argument("--quiet", "-q", action="store_true", help="print nothing")
    p.add_argument("--debug", action="store_true", help="debug logging")

    p = add("start", cmd_start, "start the daemon in the background")
    p.add_argument("--offline", action="store_true", help="use the scripted brain (demo)")
    p.add_argument("--clear-stop", action="store_true", help="remove a STOP file first")

    p = add("stop", cmd_stop, "stop after the current step")
    p.add_argument("--reason")
    p.add_argument("--timeout", type=float, default=120.0)
    p.add_argument("--no-wait", action="store_true")
    p = add("kill", cmd_kill, "kill immediately (interrupts model and tool calls)")
    p.add_argument("--reason")
    p = add("pause", cmd_pause, "pause at the next safe point")
    p.add_argument("--reason")
    add("resume", cmd_resume, "resume after a pause")
    add("wake", cmd_wake, "end the current sleep now")

    add("status", cmd_status, "state, budget and agenda")
    add("tui", cmd_tui, "open the terminal monitor")
    p = add("log", cmd_log, "print the event log")
    p.add_argument("-n", "--lines", type=int, default=40)
    p.add_argument("-f", "--follow", action="store_true")
    p.add_argument("--cycle")
    p.add_argument("--kind", action="append", help="filter by kind (prefix with *)")
    p.add_argument("--verbose", "-v", action="store_true")
    p = add("show", cmd_show, "show one cycle's deliberation (default: the latest)")
    p.add_argument("cycle", nargs="?")
    p = add("replay", cmd_replay, "re-run a cycle's deliberation from its recorded responses")
    p.add_argument("cycle")

    p = add("note", cmd_note, "send the agent a message (a percept, not an order)")
    p.add_argument("text", nargs="+")
    p = add("approvals", cmd_approvals, "list, approve or deny queued actions")
    p.add_argument("action", nargs="?", choices=["list", "approve", "deny"])
    p.add_argument("id", nargs="?", type=int)
    p.add_argument("--note")
    p.add_argument("--args", help="replacement arguments as JSON (approve only)")
    p.add_argument("--all", action="store_true", help="include resolved approvals")
    add("amendments", cmd_amendments, "charter amendments the agent proposed")

    p = add("ask", cmd_ask, "one-off prompt to a model tier (debugging)")
    p.add_argument("prompt", nargs="+")
    p.add_argument("--tier", default="FLASH")
    p = add("eval", cmd_eval, "run scenario evals")
    p.add_argument("scenario", nargs="*")
    p.add_argument(
        "--real",
        action="store_true",
        help="use this instance's models instead of the scripted brain",
    )
    p.add_argument("--max-level", default="L3")
    p.add_argument(
        "--compare-levels",
        action="store_true",
        help="run each scenario at L1 and L3 to compare cost and outcome",
    )
    add("charters", cmd_charters, "list bundled example charters")
    return parser


def main(argv: list[str] | None = None) -> int:
    if sys.platform == "win32":  # pragma: no cover
        os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    parser = build_parser()
    args = parser.parse_args(argv)
    handler: Handler | None = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 0
    try:
        return handler(args)
    except (ConfigError, DecapodError) as exc:
        err.print(f"[red]error:[/] {escape(str(exc))}")
        return 2
    except KeyboardInterrupt:
        return 130


__all__ = ["build_parser", "main"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
