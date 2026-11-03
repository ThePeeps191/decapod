"""Perception: everything new since the last cycle, turned into percepts."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from email.utils import parseaddr
from typing import TYPE_CHECKING, Any

from decapod.domain import ApprovalStatus, Percept
from decapod.tools.builtin.email import _imap_fetch, remember_contact
from decapod.tools.builtin.web import BlockedAddress, fetch, parse_feed
from decapod.util.text import one_line
from decapod.util.timeutil import format_duration, from_iso, now, to_iso

if TYPE_CHECKING:
    from decapod.runtime.instance import Instance
    from decapod.tools import ToolContext

log = logging.getLogger(__name__)
SENSOR_TIMEOUT_S = 60.0


def _due(instance: Instance, sensor: str, interval: timedelta) -> bool:
    last = instance.db.get_state(f"sensor:{sensor}:last")
    if last and now() - from_iso(last) < interval:
        return False
    instance.db.set_state(f"sensor:{sensor}:last", to_iso(now()))
    return True


def _seen(instance: Instance, sensor: str, item: str) -> bool:
    row = instance.db.query_one(
        "SELECT 1 FROM sensor_seen WHERE sensor = ? AND item = ?", (sensor, item)
    )
    if row is not None:
        return True
    instance.db.execute(
        "INSERT OR IGNORE INTO sensor_seen(sensor, item, ts) VALUES (?, ?, ?)",
        (sensor, item, to_iso(now())),
    )
    return False


def operator_notes(instance: Instance) -> list[Percept]:
    rows = instance.db.query("SELECT id, text FROM operator_notes WHERE delivered = 0 ORDER BY id")
    percepts = []
    for row in rows:
        percepts.append(
            Percept(
                source="operator",
                kind="note",
                importance=0.9,
                summary=f"Message from your operator: {one_line(row['text'], 600)}",
                data={"note_id": row["id"]},
            )
        )
        instance.db.execute("UPDATE operator_notes SET delivered = 1 WHERE id = ?", (row["id"],))
    return percepts


def approval_percepts(instance: Instance) -> list[Percept]:
    percepts: list[Percept] = []
    instance.approvals.expire_due()
    reported = []
    for approval in instance.approvals.unreported():
        action = approval.action.describe()
        if approval.status is ApprovalStatus.EXECUTED:
            ok = (approval.result or {}).get("ok")
            output = one_line(
                str(
                    (approval.result or {}).get("output")
                    or (approval.result or {}).get("error")
                    or ""
                ),
                300,
            )
            summary = (
                f"Operator approved #{approval.id} {action}; it was executed "
                f"({'ok' if ok else 'failed'}): {output}"
            )
            importance = 0.7
        elif approval.status is ApprovalStatus.DENIED:
            note = f" Note: {approval.note}" if approval.note else ""
            summary = f"Operator DENIED #{approval.id} {action}.{note} Do not retry it as is."
            importance = 0.85
        else:
            summary = f"Approval #{approval.id} {action} expired without an answer."
            importance = 0.6
        percepts.append(
            Percept(
                source="approval",
                kind=f"approval_{approval.status.value}",
                summary=summary,
                importance=importance,
                data={"approval_id": approval.id},
            )
        )
        reported.append(approval.id)
    instance.approvals.mark_reported(reported)
    pending = instance.approvals.pending()
    if pending:
        listing = "; ".join(f"#{a.id} {a.action.describe()}" for a in pending[:5])
        percepts.append(
            Percept(
                source="approval",
                kind="approval_pending",
                importance=0.3,
                summary=f"{len(pending)} action(s) await operator approval: "
                f"{listing}. Do not propose them again.",
            )
        )
    return percepts


def budget_percepts(instance: Instance) -> list[Percept]:
    percepts = []
    for status in instance.budget.warnings():
        resets = (
            f"; resets in {format_duration(status.resets_at - now())}" if status.resets_at else ""
        )
        percepts.append(
            Percept(
                source="budget",
                kind="budget_warning",
                importance=0.6,
                summary=(
                    f"{status.window} {status.metric} budget at {status.fraction:.0%} "
                    f"({status.used:g}/{status.limit:g}){resets}. Prefer cheap actions."
                ),
            )
        )
    return percepts


def crash_percepts(instance: Instance) -> list[Percept]:
    notice = instance.db.get_state("pending_crash_notice")
    if not notice:
        return []
    instance.db.set_state("pending_crash_notice", None)
    return [
        Percept(
            source="system",
            kind="crash_recovery",
            importance=0.7,
            summary=(
                f"The process stopped unexpectedly during cycle {notice['cycle_id']} "
                f"(phase: {notice.get('phase')}). Check whether that cycle's action "
                "completed before repeating it."
            ),
        )
    ]


def denial_percepts(instance: Instance) -> list[Percept]:
    streak = instance.policy.consecutive_denials
    if streak >= 2:
        return [
            Percept(
                source="policy",
                kind="denial_streak",
                importance=0.8,
                summary=(
                    f"{streak} consecutive actions were denied by policy. Stop "
                    "proposing out-of-bounds actions; the instance will be stopped "
                    "if this continues."
                ),
            )
        ]
    return []


async def feed_percepts(instance: Instance, ctx: ToolContext) -> list[Percept]:
    feeds = instance.settings.tools.feeds
    if not feeds or not instance.policy.scope_granted("web.read"):
        return []
    if not _due(instance, "feeds", timedelta(minutes=30)):
        return []
    percepts: list[Percept] = []
    for feed in feeds:
        try:
            response = await fetch(ctx, feed.url, accept="application/rss+xml, application/xml")
            entries = parse_feed(response.text) if response.status_code < 400 else []
        except (BlockedAddress, Exception) as exc:  # sensors never break a cycle
            log.info("feed %s failed: %s", feed.url, exc)
            continue
        fresh = [e for e in entries[:30] if not _seen(instance, f"feed:{feed.url}", e["id"])]
        for entry in fresh[:5]:
            percepts.append(
                Percept(
                    source=f"sensor:feed:{feed.name or feed.url}",
                    kind="feed_item",
                    importance=0.4,
                    untrusted=True,
                    summary=f"New item: {one_line(entry['title'], 160)} ({entry['link']})",
                    data=entry,
                )
            )
    return percepts


async def email_percepts(instance: Instance, ctx: ToolContext) -> list[Percept]:
    if instance.registry.get("email.list") is None or not instance.policy.scope_granted(
        "email.read"
    ):
        return []
    if not _due(instance, "email", instance.settings.tools.email.poll_interval):
        return []
    try:
        items: list[dict[str, Any]] = await asyncio.to_thread(_imap_fetch, ctx, "UNSEEN", 20)
    except Exception as exc:
        log.info("email sensor failed: %s", exc)
        return []
    percepts = []
    for item in items:
        if _seen(instance, "email", item["message_id"] or item["uid"]):
            continue
        sender = parseaddr(item["from"])[1]
        remember_contact(ctx, sender)
        percepts.append(
            Percept(
                source="sensor:email",
                kind="inbox",
                importance=0.7,
                untrusted=True,
                summary=(
                    f"New email uid {item['uid']} from {one_line(item['from'], 80)}: "
                    f"{one_line(item['subject'], 120)}"
                ),
                data={"uid": item["uid"], "from": sender},
            )
        )
    return percepts


async def perceive(
    instance: Instance, ctx: ToolContext, extra: list[Percept] | None = None
) -> list[Percept]:
    percepts: list[Percept] = list(extra or [])
    percepts += crash_percepts(instance)
    percepts += operator_notes(instance)
    percepts += approval_percepts(instance)
    percepts += budget_percepts(instance)
    percepts += denial_percepts(instance)
    # Sensors are best-effort and bounded: a slow feed or mail server cannot hold up a cycle
    # (or a stop) for long.
    for sensor in (feed_percepts, email_percepts):
        try:
            percepts += await asyncio.wait_for(sensor(instance, ctx), timeout=SENSOR_TIMEOUT_S)
        except TimeoutError:
            log.info("sensor %s timed out", sensor.__name__)
    return percepts
