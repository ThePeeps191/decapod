"""Long-term memory: episodic, semantic and reflective stores with scored retrieval."""

from __future__ import annotations

import math
import re
from array import array
from datetime import datetime
from typing import Any

from decapod.config.settings import MemorySettings
from decapod.domain import Memory, MemoryKind
from decapod.storage import Database, EventLog
from decapod.util.text import one_line
from decapod.util.timeutil import from_iso, now, to_iso

_WORD_RE = re.compile(r"[A-Za-z0-9_]{2,}")
_STOPWORDS = frozenset(
    """
    the and for with that this from have has had was were are you your our their them they
    what when where which who how why not but can could should would will into onto about
    than then there here also just very more most some such only own same over under again
    its it's is be been being do does did of to in on at by or an as if so no
    """.split()
)


def fts_query(text: str, max_terms: int = 16) -> str | None:
    words: list[str] = []
    for word in _WORD_RE.findall(text.lower()):
        if word in _STOPWORDS or word in words:
            continue
        words.append(word)
        if len(words) >= max_terms:
            break
    if not words:
        return None
    return " OR ".join(f'"{w}"' for w in words)


def pack(vector: list[float]) -> bytes:
    return array("f", vector).tobytes()


def unpack(blob: bytes | None) -> list[float] | None:
    if not blob:
        return None
    values = array("f")
    values.frombytes(blob)
    return list(values)


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _memory(row: Any) -> Memory:
    tags = [t for t in (row["tags"] or "").split(",") if t]
    return Memory(
        id=row["id"],
        kind=MemoryKind(row["kind"]),
        content=row["content"],
        tags=tags,
        importance=row["importance"],
        confidence=row["confidence"],
        usefulness=row["usefulness"],
        goal_id=row["goal_id"],
        cycle_id=row["cycle_id"],
        source_event_id=row["source_event_id"],
        created_at=from_iso(row["created_at"]),
        superseded_by=row["superseded_by"],
    )


class MemoryStore:
    def __init__(
        self, db: Database, settings: MemorySettings, events: EventLog | None = None
    ) -> None:
        self.db = db
        self.settings = settings
        self.events = events

    # ------------------------------------------------------------------ writes

    def add(
        self,
        kind: MemoryKind,
        content: str,
        *,
        tags: list[str] | None = None,
        importance: float = 0.5,
        confidence: float = 1.0,
        usefulness: float = 0.5,
        goal_id: int | None = None,
        cycle_id: str | None = None,
        source_event_id: int | None = None,
        embedding: list[float] | None = None,
        created_at: datetime | None = None,
    ) -> Memory:
        content = content.strip()
        if not content:
            raise ValueError("memory content must not be empty")
        clean_tags = sorted(
            {t.strip().lower().replace(",", " ") for t in (tags or []) if t.strip()}
        )
        memory_id = self.db.insert(
            "INSERT INTO memories(kind, content, tags, importance, confidence, usefulness, goal_id,"
            " cycle_id, source_event_id, created_at, embedding) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                kind.value,
                content,
                ",".join(clean_tags),
                _clamp(importance),
                _clamp(confidence),
                _clamp(usefulness),
                goal_id,
                cycle_id,
                source_event_id,
                to_iso(created_at or now()),
                pack(embedding) if embedding else None,
            ),
        )
        memory = self.get(memory_id)
        assert memory is not None
        if self.events is not None:
            self.events.append(
                f"memory.{kind.value}",
                one_line(content, 160),
                {"memory_id": memory_id, "tags": clean_tags, "importance": memory.importance},
                actor="memory",
                cycle_id=cycle_id,
            )
        return memory

    def get(self, memory_id: int) -> Memory | None:
        row = self.db.query_one("SELECT * FROM memories WHERE id = ?", (memory_id,))
        return _memory(row) if row else None

    def supersede(self, ids: list[int], by_id: int) -> None:
        for memory_id in ids:
            self.db.execute(
                "UPDATE memories SET superseded_by = ? WHERE id = ?", (by_id, memory_id)
            )

    def adjust_usefulness(self, memory_id: int, delta: float) -> None:
        self.db.execute(
            "UPDATE memories SET usefulness = MAX(0.0, MIN(1.0, usefulness + ?)) WHERE id = ?",
            (delta, memory_id),
        )

    def mark_access(self, ids: list[int]) -> None:
        ts = to_iso(now())
        for memory_id in ids:
            self.db.execute(
                "UPDATE memories SET last_access_at = ?, access_count = access_count + 1 "
                "WHERE id = ?",
                (ts, memory_id),
            )

    # ------------------------------------------------------------------ reads

    def count(self, kind: MemoryKind | None = None) -> int:
        if kind is None:
            return int(self.db.scalar("SELECT COUNT(*) FROM memories", (), 0))
        return int(self.db.scalar("SELECT COUNT(*) FROM memories WHERE kind = ?", (kind.value,), 0))

    def recent(self, kind: MemoryKind | None = None, limit: int = 10) -> list[Memory]:
        sql = "SELECT * FROM memories WHERE superseded_by IS NULL"
        params: list[Any] = []
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind.value)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [_memory(r) for r in self.db.query(sql, params)]

    def search(
        self,
        query: str,
        *,
        kinds: list[MemoryKind] | None = None,
        k: int | None = None,
        goal_id: int | None = None,
        query_embedding: list[float] | None = None,
        touch: bool = True,
    ) -> list[Memory]:
        """Score = relevance·α + recency·β + importance·γ + goal match·δ."""
        k = self.settings.retrieve_k if k is None else k
        if k <= 0:
            return []
        kind_values = [kd.value for kd in (kinds or list(MemoryKind))]
        placeholders = ",".join("?" for _ in kind_values)
        relevance: dict[int, float] = {}
        rows: dict[int, Any] = {}

        match = fts_query(query)
        if match:
            if self.db.has_fts:
                found = self.db.query(
                    f"SELECT m.* FROM memories_fts f JOIN memories m ON m.id = f.rowid "
                    f"WHERE memories_fts MATCH ? AND m.superseded_by IS NULL "
                    f"AND m.kind IN ({placeholders}) ORDER BY bm25(memories_fts) LIMIT 60",
                    [match, *kind_values],
                )
            else:
                terms = [t.strip('"') for t in match.split(" OR ")][:6]
                likes = " OR ".join("lower(m.content) LIKE ?" for _ in terms)
                found = self.db.query(
                    f"SELECT m.* FROM memories m WHERE ({likes}) AND m.superseded_by IS NULL "
                    f"AND m.kind IN ({placeholders}) ORDER BY m.id DESC LIMIT 60",
                    [*[f"%{t}%" for t in terms], *kind_values],
                )
            total = len(found)
            for i, row in enumerate(found):
                relevance[row["id"]] = 1.0 - (i / max(total, 1)) * 0.7
                rows[row["id"]] = row

        if query_embedding is not None:
            candidates = self.db.query(
                f"SELECT * FROM memories WHERE embedding IS NOT NULL AND superseded_by IS NULL "
                f"AND kind IN ({placeholders}) ORDER BY id DESC LIMIT 2000",
                kind_values,
            )
            for row in candidates:
                vec = unpack(row["embedding"])
                if vec is None:
                    continue
                sim = max(0.0, cosine(query_embedding, vec))
                relevance[row["id"]] = max(relevance.get(row["id"], 0.0), sim)
                rows.setdefault(row["id"], row)

        for row in self.db.query(
            f"SELECT * FROM memories WHERE superseded_by IS NULL AND kind IN ({placeholders}) "
            f"ORDER BY id DESC LIMIT 15",
            kind_values,
        ):
            rows.setdefault(row["id"], row)

        current = now()
        half_life = max(self.settings.recency_half_life.total_seconds(), 1.0)
        scored: list[Memory] = []
        for memory_id, row in rows.items():
            memory = _memory(row)
            age = max(0.0, (current - memory.created_at).total_seconds())
            recency = 0.5 ** (age / half_life)
            goal_match = 1.0 if goal_id is not None and memory.goal_id == goal_id else 0.0
            memory.score = (
                self.settings.relevance_weight * relevance.get(memory_id, 0.0)
                + self.settings.recency_weight * recency
                + self.settings.importance_weight * memory.importance
                + self.settings.goal_weight * goal_match
            )
            scored.append(memory)
        scored.sort(key=lambda m: m.score, reverse=True)
        top = scored[:k]
        if touch and top:
            self.mark_access([m.id for m in top])
        return top

    def lessons(self, query: str, k: int | None = None) -> list[Memory]:
        """Reflective lessons, boosted by how useful they proved to be."""
        k = self.settings.lessons_k if k is None else k
        found = self.search(query, kinds=[MemoryKind.REFLECTIVE], k=max(k * 3, k), touch=False)
        for memory in found:
            memory.score += memory.usefulness
        found.sort(key=lambda m: m.score, reverse=True)
        top = found[:k]
        if top:
            self.mark_access([m.id for m in top])
        return top

    def compaction_groups(self, older_than: datetime) -> dict[str, list[Memory]]:
        """Episodic memories older than ``older_than`` grouped by day, ready to digest."""
        rows = self.db.query(
            "SELECT * FROM memories WHERE kind = ? AND superseded_by IS NULL AND created_at < ? "
            "AND tags NOT LIKE '%digest%' ORDER BY id",
            (MemoryKind.EPISODIC.value, to_iso(older_than)),
        )
        groups: dict[str, list[Memory]] = {}
        for row in rows:
            memory = _memory(row)
            groups.setdefault(memory.created_at.date().isoformat(), []).append(memory)
        return {day: items for day, items in groups.items() if len(items) >= 2}


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))
