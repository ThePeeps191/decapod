"""The approval queue: actions waiting for an operator's yes/no."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from decapod.domain import ActionSpec, Approval, ApprovalStatus, ToolResult
from decapod.storage import Database, EventLog, loads
from decapod.util.text import json_dumps
from decapod.util.timeutil import from_iso, now, to_iso


def _row(row: Any) -> Approval:
    return Approval(
        id=row["id"],
        created_at=from_iso(row["created_at"]),
        expires_at=from_iso(row["expires_at"]),
        cycle_id=row["cycle_id"],
        action=ActionSpec.model_validate(loads(row["action"], {})),
        proposal=loads(row["proposal"], {}),
        reason=row["reason"],
        status=ApprovalStatus(row["status"]),
        resolved_at=from_iso(row["resolved_at"]) if row["resolved_at"] else None,
        resolved_by=row["resolved_by"],
        note=row["note"],
        result=loads(row["result"], None),
    )


class Approvals:
    def __init__(
        self, db: Database, events: EventLog | None = None, ttl: timedelta = timedelta(hours=24)
    ) -> None:
        self.db = db
        self.events = events
        self.ttl = ttl

    def _emit(
        self,
        kind: str,
        summary: str,
        payload: dict[str, Any],
        actor: str,
        cycle_id: str | None = None,
    ) -> None:
        if self.events is not None:
            self.events.append(kind, summary, payload, actor=actor, cycle_id=cycle_id)

    def enqueue(
        self, action: ActionSpec, proposal: dict[str, Any], reason: str, cycle_id: str | None
    ) -> Approval:
        created = now()
        approval_id = self.db.insert(
            "INSERT INTO approvals(created_at, expires_at, cycle_id, action, proposal, reason,"
            " status) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                to_iso(created),
                to_iso(created + self.ttl),
                cycle_id,
                json_dumps(action.model_dump(mode="json")),
                json_dumps(proposal),
                reason,
                ApprovalStatus.PENDING.value,
            ),
        )
        approval = self.get(approval_id)
        assert approval is not None
        self._emit(
            "approval.requested",
            f"#{approval_id} {action.describe()}",
            {
                "approval_id": approval_id,
                "reason": reason,
                "action": action.model_dump(mode="json"),
            },
            "policy",
            cycle_id,
        )
        return approval

    def get(self, approval_id: int) -> Approval | None:
        row = self.db.query_one("SELECT * FROM approvals WHERE id = ?", (approval_id,))
        return _row(row) if row else None

    def fetch(self, status: ApprovalStatus | None = None, limit: int = 100) -> list[Approval]:
        if status is None:
            rows = self.db.query("SELECT * FROM approvals ORDER BY id DESC LIMIT ?", (limit,))
        else:
            rows = self.db.query(
                "SELECT * FROM approvals WHERE status = ? ORDER BY id ASC LIMIT ?",
                (status.value, limit),
            )
        return [_row(r) for r in rows]

    def pending(self) -> list[Approval]:
        return self.fetch(ApprovalStatus.PENDING)

    def resolve(
        self,
        approval_id: int,
        approve: bool,
        *,
        by: str = "operator",
        note: str = "",
        edited_args: dict[str, Any] | None = None,
    ) -> Approval:
        status = ApprovalStatus.APPROVED if approve else ApprovalStatus.DENIED
        # One immediate transaction: concurrent resolvers (TUI + CLI) cannot both win.
        with self.db.transaction():
            current = self.get(approval_id)
            if current is None:
                raise KeyError(f"no approval #{approval_id}")
            if current.status is not ApprovalStatus.PENDING:
                raise ValueError(f"approval #{approval_id} is already {current.status.value}")
            expired = current.expires_at <= now()
            if expired:
                self._set_status(
                    approval_id,
                    ApprovalStatus.EXPIRED,
                    by="system",
                    note="expired before it was resolved",
                )
            else:
                if edited_args is not None and approve:
                    action = ActionSpec.model_validate(
                        {**current.action.model_dump(), "args": edited_args}
                    )
                    self.db.execute(
                        "UPDATE approvals SET action = ? WHERE id = ?",
                        (json_dumps(action.model_dump(mode="json")), approval_id),
                    )
                self._set_status(approval_id, status, by=by, note=note)
        if expired:
            raise ValueError(f"approval #{approval_id} has expired")
        updated = self.get(approval_id)
        assert updated is not None
        self._emit(
            f"approval.{status.value}",
            f"#{approval_id} {status.value} by {by}",
            {"approval_id": approval_id, "note": note, "edited": edited_args is not None},
            by,
            current.cycle_id,
        )
        return updated

    def _set_status(
        self, approval_id: int, status: ApprovalStatus, *, by: str, note: str = ""
    ) -> None:
        self.db.execute(
            "UPDATE approvals SET status = ?, resolved_at = ?, resolved_by = ?, note = ? "
            "WHERE id = ?",
            (status.value, to_iso(now()), by, note, approval_id),
        )

    def expire_due(self) -> list[Approval]:
        rows = self.db.query(
            "SELECT id FROM approvals WHERE status = ? AND expires_at <= ?",
            (ApprovalStatus.PENDING.value, to_iso(now())),
        )
        expired = []
        for row in rows:
            self._set_status(
                row["id"],
                ApprovalStatus.EXPIRED,
                by="system",
                note="no operator response before the deadline",
            )
            approval = self.get(row["id"])
            if approval:
                expired.append(approval)
                self._emit(
                    "approval.expired",
                    f"#{approval.id} expired",
                    {"approval_id": approval.id},
                    "system",
                    approval.cycle_id,
                )
        return expired

    def ready_to_execute(self) -> list[Approval]:
        return self.fetch(ApprovalStatus.APPROVED)

    def mark_executed(self, approval_id: int, result: ToolResult) -> None:
        self.db.execute(
            "UPDATE approvals SET status = ?, result = ? WHERE id = ?",
            (
                ApprovalStatus.EXECUTED.value,
                json_dumps(result.model_dump(mode="json")),
                approval_id,
            ),
        )

    def unreported(self) -> list[Approval]:
        rows = self.db.query(
            "SELECT * FROM approvals WHERE reported = 0 AND status IN (?, ?, ?) ORDER BY id",
            (
                ApprovalStatus.DENIED.value,
                ApprovalStatus.EXPIRED.value,
                ApprovalStatus.EXECUTED.value,
            ),
        )
        return [_row(r) for r in rows]

    def mark_reported(self, ids: list[int]) -> None:
        for approval_id in ids:
            self.db.execute("UPDATE approvals SET reported = 1 WHERE id = ?", (approval_id,))
