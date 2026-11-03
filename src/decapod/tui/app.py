"""The terminal monitor: watch Decapod think, steer it, stop it.

A separate process from the daemon. It reads the instance database (WAL mode allows
concurrent readers) and writes only control commands, operator notes and approval
decisions, which the daemon validates. If the TUI crashes, the agent is unaffected; the
kill switch also works without it (``decapod kill`` or a STOP file).
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from rich.markup import escape
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
    Tree,
)

from decapod.cli.output import ago, format_event
from decapod.config import InstanceConfig, InstancePaths
from decapod.control import Approvals, Budget, issue_command
from decapod.daemon import running_pid
from decapod.domain import ControlCommand, GoalStatus, MemoryKind, TaskStatus
from decapod.memory import AgendaStore, MemoryStore
from decapod.storage import Database, EventLog, loads, read_events, read_recent
from decapod.util.timeutil import now, to_iso

STATE_STYLE = {
    "running": "bold green",
    "sleeping": "cyan",
    "paused": "bold yellow",
    "stopping": "yellow",
    "stopped": "dim",
    "killed": "bold red",
    "starting": "green",
}
HIDDEN_FEED = {"llm.call", "cycle.context"}


def bar(fraction: float, width: int = 20) -> str:
    filled = max(0, min(width, round(fraction * width)))
    color = "red" if fraction >= 1 else "yellow" if fraction >= 0.8 else "green"
    return f"[{color}]{'█' * filled}[/][dim]{'░' * (width - filled)}[/]"


# --------------------------------------------------------------------------- modals


class ConfirmScreen(ModalScreen[bool]):
    DEFAULT_CSS = """
    ConfirmScreen { align: center middle; }
    #dialog { width: 64; height: auto; border: thick $error; background: $surface;
              padding: 1 2; }
    #dialog Horizontal { height: auto; margin-top: 1; align: right middle; }
    #dialog Button { margin-left: 2; }
    """

    def __init__(
        self, message: str, confirm_label: str = "Confirm", variant: str = "error"
    ) -> None:
        super().__init__()
        self.message = message
        self.confirm_label = confirm_label
        self.variant = variant

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.message)
            with Horizontal():
                yield Button("Cancel", id="cancel")
                yield Button(self.confirm_label, id="confirm", variant=self.variant)  # type: ignore[arg-type]

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")

    def key_escape(self) -> None:
        self.dismiss(False)


class PromptScreen(ModalScreen[str | None]):
    DEFAULT_CSS = """
    PromptScreen { align: center middle; }
    #dialog { width: 80; height: auto; border: thick $primary; background: $surface;
              padding: 1 2; }
    """

    def __init__(self, title: str, placeholder: str = "", value: str = "") -> None:
        super().__init__()
        self.title_text = title
        self.placeholder = placeholder
        self.value = value

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.title_text)
            yield Input(value=self.value, placeholder=self.placeholder, id="prompt-input")
            yield Label("[dim]Enter to send · Esc to cancel[/]")

    @on(Input.Submitted)
    def _submit(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def key_escape(self) -> None:
        self.dismiss(None)


# --------------------------------------------------------------------------- app


class DecapodTUI(App[None]):
    TITLE = "Decapod"
    CSS = """
    Screen { background: $background; }
    #dash { grid-size: 2; grid-columns: 1fr 1fr; grid-rows: auto 1fr; grid-gutter: 1; }
    .panel { border: round $primary; padding: 0 1; height: auto; }
    #decisions-panel { column-span: 2; height: 1fr; }
    #status { min-height: 9; }
    #feed-filter, #memory-query { margin-bottom: 1; }
    #cycle-list { width: 26; border: round $primary; }
    #cycle-detail { border: round $primary; }
    #agenda-tree { width: 1fr; border: round $primary; }
    #memory-side { width: 1fr; }
    #memory-results { border: round $primary; height: 1fr; }
    #approval-table { height: 8; border: round $primary; }
    #approval-detail { border: round $primary; height: 1fr; min-height: 8; }
    #approval-buttons, #control-buttons { height: auto; margin: 1 0; }
    #approval-buttons Button, #control-buttons Button { margin-right: 2; }
    #charter { border: round $primary; height: 12; }
    #banner { height: auto; background: $error; color: $text; padding: 0 1; display: none; }
    """
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("1", "tab('dashboard')", "Dashboard"),
        Binding("2", "tab('feed')", "Feed"),
        Binding("3", "tab('caucus')", "Caucus"),
        Binding("4", "tab('agenda')", "Agenda"),
        Binding("5", "tab('control')", "Control"),
        Binding("p", "toggle_pause", "Pause/Resume"),
        Binding("w", "wake", "Wake"),
        Binding("n", "note", "Note"),
        Binding("s", "stop", "Stop"),
        Binding("k", "kill", "KILL", priority=True),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, paths: InstancePaths, *, refresh_seconds: float = 1.0) -> None:
        super().__init__()
        self.paths = paths
        self.refresh_seconds = refresh_seconds
        self.db = Database(paths.db)
        self.events = EventLog(self.db)
        try:
            self.config: InstanceConfig | None = InstanceConfig.load(paths.root)
        except Exception:  # still useful as a monitor with a broken config
            self.config = None
        self.last_event_id = 0
        self.feed_filter = ""
        self.selected_approval: int | None = None
        self.selected_cycle: str | None = None

    # ------------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(id="banner")
        with TabbedContent(initial="dashboard"):
            with TabPane("Dashboard", id="dashboard"), Grid(id="dash"):
                yield Static(id="status", classes="panel")
                yield Static(id="budget", classes="panel")
                with Vertical(id="decisions-panel", classes="panel"):
                    yield Static("[bold]Focus[/]", id="focus")
                    yield DataTable(id="decisions", cursor_type="row", zebra_stripes=True)
            with TabPane("Live feed", id="feed"), Vertical():
                yield Input(
                    placeholder="filter by kind or text (e.g. caucus, tool, error)",
                    id="feed-filter",
                )
                yield RichLog(id="feed-log", markup=True, wrap=True, max_lines=5000)
            with TabPane("Caucus", id="caucus"), Horizontal():
                yield ListView(id="cycle-list")
                yield RichLog(id="cycle-detail", markup=True, wrap=True)
            with TabPane("Agenda & memory", id="agenda"), Horizontal():
                yield Tree("Agenda", id="agenda-tree")
                with Vertical(id="memory-side"):
                    yield Input(placeholder="search memory…", id="memory-query")
                    yield RichLog(id="memory-results", markup=True, wrap=True)
            with TabPane("Approvals & control", id="control"), Vertical():
                yield DataTable(id="approval-table", cursor_type="row")
                with Horizontal(id="approval-buttons"):
                    yield Button("Approve", id="approve", variant="success")
                    yield Button("Deny", id="deny", variant="warning")
                    yield Button("Edit args & approve", id="edit-approve")
                yield VerticalScroll(Static(id="approval-detail-text"), id="approval-detail")
                with Horizontal(id="control-buttons"):
                    yield Button("Pause", id="btn-pause")
                    yield Button("Resume", id="btn-resume", variant="success")
                    yield Button("Wake", id="btn-wake")
                    yield Button("Send note", id="btn-note", variant="primary")
                    yield Button("Stop", id="btn-stop", variant="warning")
                    yield Button("KILL", id="btn-kill", variant="error")
                yield VerticalScroll(Static(id="charter-text"), id="charter")
        yield Footer()

    def on_mount(self) -> None:
        name = self.config.charter.name if self.config else self.paths.root.name
        self.title = f"Decapod · {name}"
        self.sub_title = str(self.paths.root)
        decisions = self.query_one("#decisions", DataTable)
        decisions.add_columns("cycle", "level", "decision", "outcome", "cost")
        approvals = self.query_one("#approval-table", DataTable)
        approvals.add_columns("#", "action", "reason", "cycle", "expires")
        feed = self.query_one("#feed-log", RichLog)
        for event in read_recent(self.db, limit=300):
            self._feed(feed, event)
            self.last_event_id = event.id
        self.refresh_all()
        self.set_interval(self.refresh_seconds, self.refresh_all)

    def on_unmount(self) -> None:
        self.db.close()

    # ------------------------------------------------------------------ refresh

    def refresh_all(self) -> None:
        try:
            self._refresh_status()
            self._refresh_feed()
            self._refresh_decisions()
            self._refresh_cycles()
            self._refresh_agenda()
            self._refresh_approvals()
            self._refresh_charter()
        except Exception as exc:  # never crash the monitor on a transient read error
            self.notify(f"refresh failed: {exc}", severity="warning", timeout=3)

    def _state(self) -> tuple[str, int | None, dict[str, Any]]:
        state = self.db.all_state()
        pid = running_pid(self.paths.pidfile)
        run_state = str(state.get("run_state") or "never started")
        if pid is None and run_state not in ("killed", "never started"):
            run_state = "stopped"
        return run_state, pid, state

    def _refresh_status(self) -> None:
        run_state, pid, state = self._state()
        style = STATE_STYLE.get(run_state, "white")
        lines = [
            f"[bold]Status[/]  [{style}]{run_state.upper()}[/]" + (f"  pid {pid}" if pid else "")
        ]
        if state.get("run_state_reason"):
            lines.append(f"[dim]{escape(str(state['run_state_reason']))}[/]")
        if pid:
            lines.append(f"heartbeat  {ago(state.get('heartbeat'))}")
            lines.append(
                f"phase      {state.get('phase') or '-'}"
                f"   cycle {state.get('current_cycle') or '-'}"
            )
            if state.get("next_wake"):
                lines.append(
                    f"next wake  {ago(state.get('next_wake'))}  "
                    f"[dim]{escape(str(state.get('sleep_reason') or ''))}[/]"
                )
        lines.append(
            f"last cycle {state.get('last_cycle') or '-'} ({state.get('last_cycle_status') or '-'})"
        )
        cycles = self.db.scalar("SELECT COUNT(*) FROM cycles", (), 0)
        pending = self.db.scalar("SELECT COUNT(*) FROM approvals WHERE status = 'pending'", (), 0)
        lines.append(f"cycles     {cycles}")
        if pending:
            lines.append(f"[bold yellow]{pending} approval(s) waiting — press 5[/]")
        self.query_one("#status", Static).update("\n".join(lines))
        banner = self.query_one("#banner", Static)
        heartbeat = state.get("heartbeat")
        stale = pid is not None and heartbeat and now().timestamp() - float(heartbeat) > 30
        if stale:
            banner.update("⚠ The daemon's heartbeat is stale: it may be hung. Kill switch: k")
            banner.display = True
        elif self.config and any(
            m == "auto" for t, m in self.config.settings.policy.modes.items() if int(t) >= 3
        ):
            banner.update("⚠ External-write actions run WITHOUT approval (policy mode 'auto').")
            banner.display = True
        else:
            banner.display = False

        budget_lines = ["[bold]Budget[/]"]
        if self.config:
            for s in Budget(self.db, self.config.settings.budget).status():
                budget_lines.append(
                    f"{s.window:<9}{s.metric:<13}{bar(s.fraction, 16)} {s.used:g}/{s.limit:g}"
                )
        self.query_one("#budget", Static).update("\n".join(budget_lines))
        agenda = AgendaStore(self.db)
        goal, task = agenda.focus()
        focus = "[bold]Focus[/]  "
        if goal is None:
            focus += "[dim]no active goal[/]"
        else:
            done, total = agenda.progress(goal.id)
            focus += f"{goal.ref} {escape(goal.title)} [dim]({done}/{total} tasks)[/]"
            if task is not None:
                focus += f"\n        next: {task.ref} {escape(task.title)}"
        self.query_one("#focus", Static).update(focus)

    def _feed(self, log: RichLog, event: Any) -> None:
        if event.kind in HIDDEN_FEED:
            return
        if self.feed_filter:
            needle = self.feed_filter.lower()
            if needle not in event.kind.lower() and needle not in event.summary.lower():
                return
        log.write(Text.from_markup(format_event(event)))

    def _refresh_feed(self) -> None:
        log = self.query_one("#feed-log", RichLog)
        for event in read_events(self.db, after_id=self.last_event_id, limit=1000):
            self._feed(log, event)
            self.last_event_id = event.id

    def _refresh_decisions(self) -> None:
        table = self.query_one("#decisions", DataTable)
        rows = self.db.query(
            "SELECT id, level, status, decision, result, cost FROM cycles "
            "ORDER BY seq DESC LIMIT 25"
        )
        table.clear()
        for row in rows:
            decision = loads(row["decision"], {}) or {}
            chosen = decision.get("chosen")
            if chosen:
                action = chosen["action"]
                text = action.get("tool") or "wait"
                args = json.dumps(action.get("args", {}))[:60]
                described = f"{text} {args}" if action.get("kind") == "tool" else "wait"
            elif decision:
                described = "wait"
            else:
                described = f"({row['status']})"
            result = loads(row["result"], {}) or {}
            cost = loads(row["cost"], {}) or {}
            table.add_row(
                row["id"],
                f"L{row['level']}" if row["level"] is not None else "-",
                described,
                result.get("status", row["status"]),
                f"{cost.get('llm_requests', 0)} calls",
            )

    def _refresh_cycles(self) -> None:
        view = self.query_one("#cycle-list", ListView)
        ids = [r["id"] for r in self.db.query("SELECT id FROM cycles ORDER BY seq DESC LIMIT 60")]
        current = [getattr(item, "cycle_id", None) for item in view.children]
        if current != ids:
            view.clear()
            for cycle_id in ids:
                item = ListItem(Label(cycle_id))
                item.cycle_id = cycle_id  # type: ignore[attr-defined]
                view.append(item)
            if self.selected_cycle is None and ids:
                self.show_cycle(ids[0])

    def show_cycle(self, cycle_id: str) -> None:
        self.selected_cycle = cycle_id
        log = self.query_one("#cycle-detail", RichLog)
        log.clear()
        log.write(Text.from_markup(f"[bold]Cycle {cycle_id}[/]"))
        for event in read_events(self.db, cycle_id=cycle_id, limit=100_000):
            if event.kind in HIDDEN_FEED:
                continue
            log.write(Text.from_markup(format_event(event, show_cycle=False)))
            payload = event.payload
            detail = ""
            if event.kind == "caucus.proposal":
                detail = payload.get("rationale", "")
            elif event.kind == "caucus.critique":
                detail = "; ".join(payload.get("concerns", []))
            elif event.kind == "caucus.report":
                detail = payload.get("consensus", "")
            elif event.kind == "caucus.decision":
                detail = payload.get("rationale", "")
                for dissent in payload.get("dissent", []):
                    log.write(Text.from_markup(f"      [yellow]dissent:[/] {escape(dissent)}"))
            elif event.kind == "tool.result":
                detail = (payload.get("output") or payload.get("error") or "")[:600]
            if detail:
                log.write(Text.from_markup(f"      [dim]{escape(str(detail))}[/]"))

    def _refresh_agenda(self) -> None:
        tree = self.query_one("#agenda-tree", Tree)
        agenda = AgendaStore(self.db)
        goals = agenda.goals()
        signature = [(g.id, g.status.value, g.updated_at) for g in goals]
        signature += [
            (t.id, t.status.value, t.updated_at) for g in goals for t in agenda.current_plan(g.id)
        ]
        if getattr(tree, "_signature", None) == signature:
            return
        tree._signature = signature  # type: ignore[attr-defined]
        tree.clear()
        tree.root.expand()
        colors = {
            GoalStatus.ACTIVE: "green",
            GoalStatus.DONE: "dim",
            GoalStatus.BLOCKED: "yellow",
            GoalStatus.ABANDONED: "dim strike",
            GoalStatus.PROPOSED: "cyan",
        }
        task_colors = {
            TaskStatus.DONE: "dim",
            TaskStatus.FAILED: "red",
            TaskStatus.IN_PROGRESS: "bold green",
            TaskStatus.SKIPPED: "dim strike",
            TaskStatus.TODO: "white",
        }
        for goal in goals:
            done, total = agenda.progress(goal.id)
            node = tree.root.add(
                Text.from_markup(
                    f"[{colors[goal.status]}]{goal.ref} {escape(goal.title)}[/] "
                    f"[dim]{goal.status.value} · p{goal.priority:.2f} · {done}/{total}[/]"
                ),
                expand=goal.status.is_open,
            )
            for task in agenda.current_plan(goal.id):
                node.add_leaf(
                    Text.from_markup(
                        f"[{task_colors[task.status]}]{task.ref} {escape(task.title)}[/] "
                        f"[dim]{task.status.value}[/]"
                    )
                )

    def _refresh_approvals(self) -> None:
        table = self.query_one("#approval-table", DataTable)
        approvals = Approvals(self.db).pending()
        keys = [a.id for a in approvals]
        if getattr(table, "_keys", None) == keys:
            return
        table._keys = keys  # type: ignore[attr-defined]
        table.clear()
        for a in approvals:
            table.add_row(
                str(a.id),
                a.action.describe()[:70],
                a.reason[:50],
                a.cycle_id or "-",
                a.expires_at.astimezone().strftime("%m-%d %H:%M"),
                key=str(a.id),
            )
        if approvals and self.selected_approval not in keys:
            self.select_approval(approvals[0].id)
        elif not approvals:
            self.selected_approval = None
            self.query_one("#approval-detail-text", Static).update("[dim]No pending approvals.[/]")

    def select_approval(self, approval_id: int) -> None:
        self.selected_approval = approval_id
        approval = Approvals(self.db).get(approval_id)
        if approval is None:
            return
        proposal = approval.proposal
        text = [
            f"[bold]#{approval.id} {escape(approval.action.describe())}[/]",
            f"[dim]reason:[/] {escape(approval.reason)}",
            f"[dim]rationale:[/] {escape(str(proposal.get('rationale', '')))}",
            f"[dim]expected:[/] {escape(str(proposal.get('expected_outcome', '')))}",
        ]
        for dissent in proposal.get("dissent", []):
            text.append(f"[yellow]dissent:[/] {escape(dissent)}")
        text.append("[dim]arguments:[/]")
        text.append(escape(json.dumps(approval.action.args, indent=2)))
        self.query_one("#approval-detail-text", Static).update("\n".join(text))

    def _refresh_charter(self) -> None:
        if getattr(self, "_charter_done", False) or self.config is None:
            return
        self._charter_done = True
        amendments = self.db.query("SELECT * FROM amendments ORDER BY id DESC LIMIT 10")
        text = [
            "[bold]Charter[/] [dim](read-only; edit charter.toml to change it)[/]",
            escape(self.config.charter.render()),
        ]
        if amendments:
            text.append("\n[bold]Proposed amendments[/]")
            for row in amendments:
                text.append(
                    f"#{row['id']} {escape(row['text'])} [dim]— {escape(row['rationale'])}[/]"
                )
        self.query_one("#charter-text", Static).update("\n".join(text))

    # ------------------------------------------------------------------ events

    @on(Input.Changed, "#feed-filter")
    def _filter(self, event: Input.Changed) -> None:
        self.feed_filter = event.value.strip()
        log = self.query_one("#feed-log", RichLog)
        log.clear()
        for e in read_recent(self.db, limit=1000):
            self._feed(log, e)

    @on(Input.Submitted, "#memory-query")
    def _memory_search(self, event: Input.Submitted) -> None:
        log = self.query_one("#memory-results", RichLog)
        log.clear()
        if self.config is None or not event.value.strip():
            return
        store = MemoryStore(self.db, self.config.settings.memory)
        hits = store.search(event.value, k=20, touch=False)
        if not hits:
            log.write("No matching memories.")
        for m in hits:
            source = f" · from event #{m.source_event_id}" if m.source_event_id else ""
            cycle = f" · cycle {m.cycle_id}" if m.cycle_id else ""
            log.write(
                Text.from_markup(
                    f"[bold]M{m.id}[/] [cyan]{m.kind.value}[/] [dim]{m.created_at:%Y-%m-%d %H:%M}"
                    f"{cycle}{source} · score {m.score:.2f}[/]\n{escape(m.content)}\n"
                )
            )
        lessons = [m for m in hits if m.kind is MemoryKind.REFLECTIVE]
        if lessons:
            log.write(Text.from_markup(f"[dim]{len(lessons)} lesson(s) among the results[/]"))

    @on(ListView.Selected, "#cycle-list")
    def _cycle_selected(self, event: ListView.Selected) -> None:
        cycle_id = getattr(event.item, "cycle_id", None)
        if cycle_id:
            self.show_cycle(cycle_id)

    @on(DataTable.RowHighlighted, "#approval-table")
    def _approval_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is not None and event.row_key.value is not None:
            self.select_approval(int(event.row_key.value))

    @on(Button.Pressed)
    def _button(self, event: Button.Pressed) -> None:
        actions = {
            "approve": lambda: self.resolve_approval(True),
            "deny": lambda: self.resolve_approval(False),
            "edit-approve": self.edit_and_approve,
            "btn-pause": lambda: self.send(ControlCommand.PAUSE),
            "btn-resume": lambda: self.send(ControlCommand.RESUME),
            "btn-wake": self.action_wake,
            "btn-note": self.action_note,
            "btn-stop": self.action_stop,
            "btn-kill": self.action_kill,
        }
        handler = actions.get(event.button.id or "")
        if handler is not None:
            handler()

    # ------------------------------------------------------------------ actions

    def send(self, command: ControlCommand, reason: str = "") -> None:
        issue_command(self.db, command, {"reason": reason} if reason else None, by="tui")
        self.notify(f"{command.value} sent", timeout=2)

    def action_tab(self, tab: str) -> None:
        self.query_one(TabbedContent).active = tab

    def action_toggle_pause(self) -> None:
        run_state, _, _ = self._state()
        self.send(ControlCommand.RESUME if run_state == "paused" else ControlCommand.PAUSE)

    def action_wake(self) -> None:
        self.send(ControlCommand.WAKE)

    @work
    async def action_note(self) -> None:
        text = await self.push_screen_wait(
            PromptScreen(
                "Message to the agent (a percept weighed against its charter, not an order):",
                "e.g. Focus on the intertidal zone first.",
            )
        )
        if text:
            self.db.insert(
                "INSERT INTO operator_notes(ts, text) VALUES (?, ?)", (to_iso(now()), text)
            )
            self.send(ControlCommand.WAKE)
            self.notify("Note delivered at the next cycle.")

    @work
    async def action_stop(self) -> None:
        if await self.push_screen_wait(
            ConfirmScreen("Stop Decapod after its current step?", "Stop", "warning")
        ):
            self.send(ControlCommand.STOP, "stopped from the TUI")

    @work
    async def action_kill(self) -> None:
        if await self.push_screen_wait(
            ConfirmScreen(
                "KILL Decapod now? Model and tool calls in flight are interrupted.", "KILL"
            )
        ):
            self.send(ControlCommand.KILL, "killed from the TUI")

    def resolve_approval(
        self,
        approve: bool,
        edited: dict[str, Any] | None = None,
        approval_id: int | None = None,
    ) -> None:
        target = approval_id if approval_id is not None else self.selected_approval
        if target is None:
            self.notify("No approval selected.", severity="warning")
            return
        self.selected_approval = target
        try:
            Approvals(self.db, self.events).resolve(target, approve, by="tui", edited_args=edited)
        except (KeyError, ValueError) as exc:
            self.notify(str(exc), severity="error")
            return
        issue_command(self.db, ControlCommand.WAKE, by="tui")
        self.notify(f"#{self.selected_approval} {'approved' if approve else 'denied'}")
        self.selected_approval = None
        self.query_one("#approval-table", DataTable)._keys = None  # type: ignore[attr-defined]
        self._refresh_approvals()

    @work
    async def edit_and_approve(self) -> None:
        if self.selected_approval is None:
            self.notify("No approval selected.", severity="warning")
            return
        approval_id = self.selected_approval  # pinned: the list may change while editing
        approval = Approvals(self.db).get(approval_id)
        if approval is None:
            return
        text = await self.push_screen_wait(
            PromptScreen(
                "Edit the arguments (JSON), then Enter to approve:",
                "",
                json.dumps(approval.action.args),
            )
        )
        if text is None:
            return
        try:
            edited = json.loads(text)
            if not isinstance(edited, dict):
                raise ValueError("arguments must be a JSON object")
        except ValueError as exc:
            self.notify(f"Invalid JSON: {exc}", severity="error")
            return
        self.resolve_approval(True, edited, approval_id=approval_id)


def run_tui(paths: InstancePaths) -> int:
    DecapodTUI(paths).run()
    return 0


__all__ = ["ConfirmScreen", "DecapodTUI", "PromptScreen", "run_tui"]
