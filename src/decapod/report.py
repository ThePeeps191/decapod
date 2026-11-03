"""Progress reports: what Decapod did over a period, built from the database (no model)."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from decapod.config.charter import Charter
from decapod.memory import AgendaStore
from decapod.storage import Database, loads
from decapod.util.text import one_line
from decapod.util.timeutil import format_duration, now, to_iso


@dataclass
class Report:
    since: datetime
    until: datetime
    markdown: str
    summary: str


def build_report(db: Database, charter: Charter, since: timedelta) -> Report:
    until = now()
    start = until - since
    lo = to_iso(start)

    cycles = db.query("SELECT status, level FROM cycles WHERE started_at >= ?", (lo,))
    by_status = Counter(r["status"] for r in cycles)
    by_level = Counter(f"L{r['level']}" for r in cycles if r["level"] is not None)
    spend = db.query_one(
        "SELECT COALESCE(SUM(usd),0) usd, COALESCE(SUM(tokens_in + tokens_out),0) tokens,"
        " COALESCE(SUM(llm_requests),0) calls, COALESCE(SUM(tool_calls),0) tools,"
        " COALESCE(SUM(outbound),0) outbound FROM ledger WHERE ts >= ?",
        (lo,),
    )
    assert spend is not None
    tools = db.query("SELECT tool, ok, output FROM tool_calls WHERE ts >= ? ORDER BY id", (lo,))
    tool_counts = Counter(r["tool"] for r in tools)
    failures = sum(1 for r in tools if r["ok"] == 0)
    published = [r["output"] for r in tools if r["tool"] == "notes.publish" and r["ok"] == 1]
    deployed = sum(1 for r in tools if r["tool"] == "site.deploy" and r["ok"] == 1)

    def events(kind: str) -> list[str]:
        return [
            r["summary"]
            for r in db.query(
                "SELECT summary FROM events WHERE kind = ? AND ts >= ? ORDER BY id", (kind, lo)
            )
        ]

    goals_added = events("agenda.goal_added")
    goals_done = [
        r["summary"]
        for r in db.query(
            "SELECT summary, payload FROM events WHERE kind = 'agenda.goal_updated' AND ts >= ? "
            "ORDER BY id",
            (lo,),
        )
        if (loads(r["payload"], {}) or {}).get("completed")
    ]
    approvals = Counter(
        r["status"] for r in db.query("SELECT status FROM approvals WHERE created_at >= ?", (lo,))
    )
    errors = events("cycle.error")
    lessons = [
        r["content"]
        for r in db.query(
            "SELECT content FROM memories WHERE kind = 'reflective' AND created_at >= ? "
            "ORDER BY id",
            (lo,),
        )
    ]
    notes = [
        r["text"]
        for r in db.query("SELECT text FROM operator_notes WHERE ts >= ? ORDER BY id", (lo,))
    ]
    last_decisions = db.query(
        "SELECT id, decision FROM cycles WHERE started_at >= ? AND decision IS NOT NULL "
        "ORDER BY seq DESC LIMIT 5",
        (lo,),
    )

    period = format_duration(since)
    lines = [
        f"# {charter.name}: progress report",
        "",
        f"*{start:%Y-%m-%d %H:%M} → {until:%Y-%m-%d %H:%M} UTC ({period})*",
        "",
        "## At a glance",
        "",
        f"- **Cycles:** {len(cycles)}"
        + (f" ({', '.join(f'{k} {v}' for k, v in sorted(by_status.items()))})" if cycles else ""),
        "- **Deliberation depth:** "
        + (", ".join(f"{k} ×{v}" for k, v in sorted(by_level.items())) or "–"),
        f"- **Model calls:** {spend['calls']} ({spend['tokens']:,} tokens, ${spend['usd']:.2f})",
        f"- **Tool calls:** {spend['tools']} ({failures} failed), outbound messages: "
        f"{spend['outbound']}",
        f"- **Goals:** {len(goals_added)} added, {len(goals_done)} completed",
        f"- **Notes published:** {len(published)}"
        + (f", site deployed {deployed}×" if deployed else ""),
        "- **Approvals:** "
        + (", ".join(f"{k} {v}" for k, v in sorted(approvals.items())) or "none"),
        "",
    ]
    if goals_done or goals_added:
        lines += ["## Goals", ""]
        lines += [f"- ✅ {one_line(s, 160)}" for s in goals_done]
        lines += [f"- ➕ {one_line(s, 160)}" for s in goals_added]
        lines.append("")
    if published:
        lines += ["## Published", ""]
        lines += [f"- {one_line(p, 160)}" for p in published]
        lines.append("")
    if tool_counts:
        lines += ["## Tools used", ""]
        lines += [f"- `{tool}` ×{n}" for tool, n in tool_counts.most_common(10)]
        lines.append("")
    if lessons:
        lines += ["## Lessons learned", ""]
        lines += [f"- {one_line(lesson, 240)}" for lesson in lessons[-10:]]
        lines.append("")
    if last_decisions:
        lines += ["## Latest decisions", ""]
        for row in last_decisions:
            decision = loads(row["decision"], {}) or {}
            chosen = decision.get("chosen")
            action = (chosen or {}).get("action", {})
            what = action.get("tool") or "wait"
            lines.append(f"- {row['id']}: `{what}`: {one_line(decision.get('rationale', ''), 160)}")
        lines.append("")
    if notes:
        lines += ["## Operator notes received", ""]
        lines += [f"- {one_line(n, 200)}" for n in notes]
        lines.append("")
    if errors:
        lines += ["## Errors", ""]
        lines += [f"- {one_line(e, 200)}" for e in errors[-5:]]
        lines.append("")
    lines += ["## Agenda now", "", "```", AgendaStore(db).render(), "```", ""]
    summary = (
        f"{len(cycles)} cycles, {len(goals_done)} goals completed, "
        f"{len(published)} notes published, ${spend['usd']:.2f} spent in the last "
        f"{period}"
    )
    return Report(start, until, "\n".join(lines), summary)


def save_report(report: Report, drive: Path) -> Path:
    folder = drive / "reports"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{report.until:%Y-%m-%d-%H%M}.md"
    path.write_text(report.markdown, encoding="utf-8")
    return path
