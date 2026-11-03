"""Aggregate many agents' 1–5 scores into one ranking (weighted Borda count)."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class RankTable:
    #: proposal id -> ranker role -> score (1..5)
    scores: dict[str, dict[str, int]] = field(default_factory=lambda: defaultdict(dict))
    flagged: dict[str, str] = field(default_factory=dict)  # proposal -> charter violation

    def add(self, ranker: str, proposal_id: str, score: int) -> None:
        self.scores[proposal_id][ranker] = max(1, min(5, int(score)))

    def flag(self, proposal_id: str, reason: str) -> None:
        self.flagged.setdefault(proposal_id, reason)


def borda(
    table: RankTable,
    proposal_ids: list[str],
    weights: dict[str, float] | None = None,
    *,
    flag_penalty: float = 0.25,
) -> list[tuple[str, float]]:
    """Weighted Borda points per proposal, best first.

    Each ranker contributes, for each proposal it scored, the fraction of the *other*
    proposals it scored that this one beats (ties count half). Rankers that scored a single
    proposal contribute their normalized score instead. Proposals flagged for a charter
    violation are multiplied by ``flag_penalty``. Ties are broken by mean score, then id.
    """
    weights = weights or {}
    by_ranker: dict[str, dict[str, int]] = defaultdict(dict)
    for pid in proposal_ids:
        for ranker, score in table.scores.get(pid, {}).items():
            by_ranker[ranker][pid] = score

    points: dict[str, float] = dict.fromkeys(proposal_ids, 0.0)
    for ranker, scored in by_ranker.items():
        weight = weights.get(ranker, 1.0)
        n = len(scored)
        for pid, score in scored.items():
            if n == 1:
                points[pid] += weight * (score - 1) / 4
                continue
            beats = sum(
                1.0 if score > other else 0.5 if score == other else 0.0
                for opid, other in scored.items()
                if opid != pid
            )
            points[pid] += weight * beats / (n - 1)

    for pid, _reason in table.flagged.items():
        if pid in points:
            points[pid] *= flag_penalty

    def mean(pid: str) -> float:
        values = list(table.scores.get(pid, {}).values())
        return sum(values) / len(values) if values else 0.0

    return sorted(points.items(), key=lambda kv: (-kv[1], -mean(kv[0]), _id_key(kv[0])))


def _id_key(pid: str) -> tuple[int, str]:
    digits = "".join(ch for ch in pid if ch.isdigit())
    return (int(digits) if digits else 0, pid)


def render_table(table: RankTable, ranking: list[tuple[str, float]]) -> str:
    lines = []
    for pid, pts in ranking:
        scores = table.scores.get(pid, {})
        detail = ", ".join(f"{r}={s}" for r, s in sorted(scores.items()))
        flag = f"  ⚑ charter: {table.flagged[pid]}" if pid in table.flagged else ""
        lines.append(f"{pid}: {pts:.2f} points ({detail or 'unscored'}){flag}")
    return "\n".join(lines)
