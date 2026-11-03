"""Re-run a recorded deliberation with the model responses that were recorded for it.

The working context is restored from the ``cycle.context`` event and every model call is
answered from ``llm_calls`` by (purpose, occurrence). If the code, prompts or config
changed in a way that alters the debate, the replay diverges and says where.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from decapod.agents import Roster
from decapod.caucus import Admission, Caucus, WorkingContext
from decapod.config import InstanceConfig
from decapod.config.providers import resolve_routes
from decapod.domain import ActionSpec, Decision, Level, LLMError
from decapod.llm import LLM, ReplayProvider
from decapod.logs import EventBus
from decapod.runtime.instance import Instance
from decapod.storage import Database, EventLog, loads


@dataclass
class ReplayReport:
    cycle_id: str
    level: Level
    recorded: str
    replayed: str | None
    matches: bool
    misses: list[str] = field(default_factory=list)
    charter_changed: bool = False
    error: str | None = None


async def _noop(_: str) -> None:
    return None


async def replay_cycle(config: InstanceConfig, cycle_id: str) -> ReplayReport:
    db = Database(config.paths.db)
    try:
        row = db.query_one("SELECT decision, level FROM cycles WHERE id = ?", (cycle_id,))
        if row is None:
            raise KeyError(f"no cycle {cycle_id!r}")
        recorded_decision = loads(row["decision"], None)
        if recorded_decision is None:
            raise ValueError(f"cycle {cycle_id} has no recorded decision")
        context_event = db.query_one(
            "SELECT payload FROM events WHERE cycle_id = ? AND kind = 'cycle.context' "
            "ORDER BY id LIMIT 1",
            (cycle_id,),
        )
        start_event = db.query_one(
            "SELECT payload FROM events WHERE cycle_id = ? AND kind = 'caucus.start' "
            "ORDER BY id LIMIT 1",
            (cycle_id,),
        )
        if context_event is None or start_event is None:
            raise ValueError(f"cycle {cycle_id} has no recorded context to replay")
        payload = loads(context_event["payload"], {}).get("context")
        if not payload:
            raise ValueError(f"cycle {cycle_id} predates replayable context recording")
        start = loads(start_event["payload"], {})
        rejected = set()
        for event in db.query(
            "SELECT payload FROM events WHERE cycle_id = ? AND kind = 'caucus.inadmissible'",
            (cycle_id,),
        ):
            proposal = loads(event["payload"], {}).get("proposal") or {}
            if proposal.get("action"):
                rejected.add(ActionSpec.model_validate(proposal["action"]).fingerprint())
        calls = [
            (r["purpose"], r["response"])
            for r in db.query(
                "SELECT purpose, response FROM llm_calls WHERE cycle_id = ? AND ok = 1 "
                "AND response IS NOT NULL ORDER BY id",
                (cycle_id,),
            )
        ]
    finally:
        db.close()

    recorded = Decision.model_validate(recorded_decision)
    level = Level(int(start.get("level", recorded.level)))
    context = WorkingContext.from_payload(payload, config.charter)
    provider = ReplayProvider(calls)
    memory_db = Database(":memory:")
    events = EventLog(memory_db, EventBus())
    instance = Instance(config, provider=provider)
    try:
        await instance.start()
        settings = config.settings.llm.model_copy(update={"max_retries": 0})
        llm = LLM(settings, provider, routing=resolve_routes(settings, None, placeholder=True))

        def admit(action: ActionSpec) -> Admission:
            # Admission depends on state at the time (e.g. existing goals), so use what was
            # recorded: everything was admitted except the proposals logged as inadmissible.
            tier = instance.registry.risk_of(action)
            if action.fingerprint() in rejected:
                return Admission(False, "inadmissible when recorded", tier)
            return Admission(True, "replay", tier)

        caucus = Caucus(
            llm,
            Roster(config.settings.caucus),
            config.settings.caucus,
            events,
            admit,
            checkpoint=_noop,
        )
        replayed: Decision | None = None
        error = None
        try:
            replayed = await caucus.deliberate(context, level, list(start.get("reasons", [])))
        except LLMError as exc:
            error = str(exc)
    finally:
        await instance.close()
        memory_db.close()
    replayed_text = replayed.describe() if replayed else None
    return ReplayReport(
        cycle_id=cycle_id,
        level=level,
        recorded=recorded.describe(),
        replayed=replayed_text,
        matches=replayed is not None
        and replayed.action.fingerprint() == recorded.action.fingerprint(),
        misses=sorted(set(provider.misses)),
        charter_changed=payload.get("charter_sha") != config.charter.sha256,
        error=error,
    )
