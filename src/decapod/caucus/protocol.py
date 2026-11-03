"""The deliberation protocol: propose → critique → revise → rank → synthesize → decide.

One call to :meth:`Caucus.deliberate` produces exactly one :class:`Decision`. Depth scales
with the escalation level (see ``docs/PLAN.md`` §5.6). Every round is logged as events, so
the TUI can replay the debate and ``decapod replay`` can re-run it deterministically.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from decapod import prompts
from decapod.agents import AgentSpec, Roster
from decapod.caucus.context import WorkingContext
from decapod.caucus.escalation import TRIAGE_LEVELS, TriageDecision, clamp, rule_level
from decapod.caucus.ranking import RankTable, borda, render_table
from decapod.caucus.schemas import (
    Confirmation,
    CritiqueSet,
    ExecutiveChoice,
    ManagerReport,
    ProposalDraft,
    ProposalSet,
    RankSet,
    RevisionSet,
    TriageResult,
)
from decapod.config.settings import CaucusSettings
from decapod.domain import (
    ActionSpec,
    ChamberReport,
    Critique,
    Decision,
    Level,
    LLMError,
    Proposal,
    ProviderUnavailable,
    RiskTier,
    Tier,
)
from decapod.llm import LLM, Message
from decapod.storage import EventLog
from decapod.util.text import json_dumps, one_line

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)
SHORTLIST = 5


@dataclass(frozen=True)
class Admission:
    ok: bool
    reason: str
    tier: RiskTier


Admit = Callable[[ActionSpec], Admission]
Checkpoint = Callable[[str], Awaitable[None]]


async def _no_checkpoint(_: str) -> None:
    return None


async def _gather(*aws: Any) -> list[Any]:
    """Like asyncio.gather, but if one task fails the others are cancelled, so no model
    call outlives a halted, killed or over-budget cycle."""
    tasks = [asyncio.ensure_future(a) for a in aws]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


class Caucus:
    def __init__(
        self,
        llm: LLM,
        roster: Roster,
        settings: CaucusSettings,
        events: EventLog,
        admit: Admit,
        *,
        checkpoint: Checkpoint = _no_checkpoint,
    ) -> None:
        self.llm = llm
        self.roster = roster
        self.settings = settings
        self.events = events
        self.admit = admit
        self.checkpoint = checkpoint
        self._counter = 0

    # ------------------------------------------------------------------ helpers

    def _emit(
        self,
        ctx: WorkingContext,
        kind: str,
        summary: str,
        payload: dict[str, Any],
        actor: str = "caucus",
    ) -> None:
        self.events.append(kind, summary, payload, actor=actor, cycle_id=ctx.cycle_id)

    def _system(self, ctx: WorkingContext, spec: AgentSpec) -> str:
        return prompts.render(
            "system",
            role_title=spec.title,
            brief=spec.brief,
            charter=ctx.charter,
            charter_text=ctx.charter.render(),
        )

    def _situation(self, ctx: WorkingContext, spec: AgentSpec) -> str:
        return prompts.render(
            "situation",
            cycle_id=ctx.cycle_id,
            now=f"{ctx.now:%Y-%m-%d %H:%M} UTC",
            context=ctx.render(spec.sees),
        )

    async def _ask(
        self,
        ctx: WorkingContext,
        spec: AgentSpec,
        template: str,
        output: type[T],
        purpose: str,
        *,
        tier: Tier | None = None,
        **values: Any,
    ) -> T | None:
        user = prompts.render(
            template, situation=self._situation(ctx, spec), role_title=spec.title, **values
        )
        try:
            return await self.llm.structured(
                [Message.system(self._system(ctx, spec)), Message.user(user)],
                output,
                tier=tier or spec.tier,
                purpose=purpose,
                cycle_id=ctx.cycle_id,
                temperature=spec.temperature,
            )
        except ProviderUnavailable:
            raise
        except LLMError as exc:
            self._emit(
                ctx,
                "caucus.agent_error",
                f"{spec.role}: {one_line(str(exc), 160)}",
                {"role": spec.role, "purpose": purpose, "error": str(exc)},
            )
            return None

    def _next_id(self) -> str:
        self._counter += 1
        return f"P{self._counter}"

    @staticmethod
    def _draft_action(draft: ProposalDraft) -> ActionSpec | None:
        try:
            if draft.tool.strip().lower() in ("wait", "sleep", "none"):
                return ActionSpec.wait(draft.wait_minutes)
            return ActionSpec(tool=draft.tool.strip(), args=draft.args)
        except ValidationError:
            return None

    def _to_proposal(
        self,
        ctx: WorkingContext,
        role: str,
        draft: ProposalDraft,
        pid: str | None = None,
        revision: int = 0,
        action: ActionSpec | None = None,
    ) -> Proposal | None:
        action = action or self._draft_action(draft)
        if action is None:
            return None
        admission = self.admit(action)
        proposal = Proposal(
            id=pid or self._next_id(),
            author_role=role,
            action=action,
            goal_id=draft.goal_id,
            task_id=draft.task_id,
            rationale=draft.rationale,
            expected_outcome=draft.expected_outcome,
            confidence=draft.confidence,
            risk_tier=admission.tier,
            revision=revision,
        )
        if not admission.ok:
            self._emit(
                ctx,
                "caucus.inadmissible",
                f"{proposal.id} by {role}: {action.describe()} — {admission.reason}",
                {"proposal": proposal.model_dump(mode="json"), "reason": admission.reason},
            )
            return None
        return proposal

    @staticmethod
    def _describe(p: Proposal, *, anonymous: bool = True) -> str:
        who = "" if anonymous else f" by {p.author_role}"
        links = []
        if p.goal_id is not None:
            links.append(f"G{p.goal_id}")
        if p.task_id is not None:
            links.append(f"T{p.task_id}")
        link = f" for {'/'.join(links)}" if links else ""
        return (
            f"{p.id}{who}{link} · tier {int(p.risk_tier)} · confidence {p.confidence:.2f}\n"
            f"  action: {p.action.kind if p.action.kind == 'wait' else p.action.tool} "
            f"{json_dumps(p.action.args) if p.action.kind == 'tool' else ''}"
            f"{f'{p.action.wait_minutes:g} min' if p.action.wait_minutes else ''}\n"
            f"  rationale: {one_line(p.rationale, 400)}\n"
            f"  expected: {one_line(p.expected_outcome, 300)}"
        )

    # ------------------------------------------------------------------ triage

    async def triage(self, ctx: WorkingContext) -> TriageDecision:
        decision = rule_level(ctx, self.settings)
        if (
            self.settings.llm_triage
            and decision.level < Level.L2
            and self.roster.enabled("triage")
            and ctx.percepts
        ):
            result = await self._ask(
                ctx, self.roster.get("triage"), "triage", TriageResult, "caucus.triage"
            )
            if result is not None:
                llm_level = TRIAGE_LEVELS[result.level]
                if llm_level > decision.level:
                    decision = TriageDecision(
                        clamp(llm_level, self.settings),
                        [*decision.reasons, f"triage: {result.reason}"],
                    )
        return decision

    # ------------------------------------------------------------------ main entry

    async def deliberate(
        self, ctx: WorkingContext, level: Level, reasons: list[str] | None = None
    ) -> Decision:
        self._counter = 0
        decision = await self._run(ctx, level, reasons or [])
        # Re-deliberate while the chosen action's risk tier demands a deeper debate. The
        # level strictly increases each time (and is capped by max_level), so this ends.
        while decision.chosen is not None:
            required = self.settings.escalate_on_tier.get(decision.chosen.risk_tier, Level.L0)
            required = clamp(required, self.settings)
            if required <= decision.level:
                break
            self._emit(
                ctx,
                "caucus.escalate",
                f"{decision.describe()} is tier {int(decision.chosen.risk_tier)}: "
                f"re-deliberating at {required.name}",
                {
                    "from": decision.level.name,
                    "to": required.name,
                    "candidate": decision.chosen.model_dump(mode="json"),
                },
            )
            decision = await self._run(
                ctx,
                required,
                [
                    f"escalated: the {decision.level.name} caucus chose "
                    f"{decision.describe()} (tier {int(decision.chosen.risk_tier)})"
                ],
            )
        self._emit(
            ctx,
            "caucus.decision",
            f"[{decision.level.name}] {decision.describe()}",
            decision.model_dump(mode="json"),
            actor="executive",
        )
        return decision

    async def _run(self, ctx: WorkingContext, level: Level, reasons: list[str]) -> Decision:
        self._emit(
            ctx,
            "caucus.start",
            f"{level.name} ({level.label}): " + "; ".join(reasons),
            {
                "level": int(level),
                "reasons": reasons,
                "prompt_versions": {t: prompts.template_hash(t) for t in prompts.templates()},
            },
        )
        await self.checkpoint("deliberate")
        if level is Level.L0:
            decision = await self._reflex(ctx)
            if decision is not None:
                return decision
            fallback = clamp(Level.L1, self.settings)
            if fallback is Level.L0:  # max_level = L0: there is no deeper level to fall back to
                return Decision(
                    cycle_id=ctx.cycle_id,
                    level=Level.L0,
                    rationale="The reflex path declined and deliberation is capped at L0; waiting.",
                )
            return await self._run(ctx, fallback, ["reflex path declined; quick deliberation"])

        # Round 1: propose ------------------------------------------------------------
        proposals = await self._propose(ctx, self.roster.proposers(level), level)
        if not proposals:
            return Decision(
                cycle_id=ctx.cycle_id,
                level=level,
                rationale="No admissible proposals this cycle; waiting.",
                wait_minutes=None,
            )
        await self.checkpoint("deliberate")

        # Round 2: critique -------------------------------------------------------------
        table = RankTable()
        critiques = await self._critique(ctx, level, proposals, table, "critique")
        await self.checkpoint("deliberate")

        # Round 3: revise (full and grave) ---------------------------------------------
        if level >= Level.L2:
            proposals = await self._revise(ctx, proposals, critiques)
            if not proposals:
                return Decision(
                    cycle_id=ctx.cycle_id,
                    level=level,
                    rationale="All proposals were withdrawn after critique; waiting.",
                )
            if level is Level.L3:
                table = RankTable()
                critiques = await self._critique(ctx, level, proposals, table, "critique2")
            await self.checkpoint("deliberate")

        # Round 4: rank ---------------------------------------------------------------------
        if level >= Level.L2:
            await self._rank(ctx, proposals, critiques, table)
        ids = [p.id for p in proposals]
        weights = {s.role: s.weight for s in self.roster.all()}
        ranking = borda(table, ids, weights)
        self._emit(
            ctx,
            "caucus.rank",
            "ranking: " + ", ".join(f"{p}={s:.2f}" for p, s in ranking),
            {
                "ranking": ranking,
                "scores": {k: dict(v) for k, v in table.scores.items()},
                "flagged": table.flagged,
            },
        )

        # Round 5: synthesize ---------------------------------------------------------------
        reports: list[ChamberReport] = []
        if level >= Level.L2:
            reports = await self._synthesize(ctx, level, proposals, critiques, table, ranking)
            await self.checkpoint("deliberate")

        # Round 6: decide -------------------------------------------------------------------
        return await self._decide(ctx, level, proposals, critiques, table, ranking, reports)

    # ------------------------------------------------------------------ rounds

    async def _reflex(self, ctx: WorkingContext) -> Decision | None:
        planner = self.roster.get("planner")
        proposals = await self._propose(ctx, [planner], Level.L0, max_each=1)
        if not proposals:
            return None
        proposal = proposals[0]
        if proposal.risk_tier > RiskTier.READ and proposal.action.kind == "tool":
            # Writes are never reflexes, whatever the escalation settings say.
            self._emit(
                ctx,
                "caucus.reflex_declined",
                f"{proposal.id} is tier {int(proposal.risk_tier)}",
                {},
            )
            return None
        executive = self.roster.get("executive")
        confirm = await self._ask(
            ctx,
            executive,
            "confirm",
            Confirmation,
            "caucus.confirm",
            tier=self.roster.executive_tier(Level.L0),
            proposal=self._describe(proposal),
        )
        if confirm is None or not confirm.approve:
            self._emit(
                ctx,
                "caucus.reflex_declined",
                f"executive declined: {confirm.reason if confirm else 'no answer'}",
                {"proposal": proposal.id},
            )
            return None
        return Decision(
            cycle_id=ctx.cycle_id,
            level=Level.L0,
            chosen=proposal,
            rationale=confirm.reason or proposal.rationale,
            ranking=[proposal.id],
        )

    async def _propose(
        self, ctx: WorkingContext, specs: list[AgentSpec], level: Level, max_each: int | None = None
    ) -> list[Proposal]:
        limit = max_each or self.settings.max_proposals_per_role

        async def one(spec: AgentSpec) -> tuple[AgentSpec, ProposalSet | None]:
            hint = spec.focus_hint
            if spec.role == "planner" and ctx.planner_notes:
                hint = (hint + " " + " ".join(ctx.planner_notes[:3])).strip()
            result = await self._ask(
                ctx,
                spec,
                "propose",
                ProposalSet,
                f"caucus.propose.{spec.role}",
                catalog=ctx.catalog,
                max_proposals=limit,
                focus_hint=hint,
            )
            return spec, result

        results = await _gather(*(one(s) for s in specs))
        proposals: list[Proposal] = []
        seen: dict[str, Proposal] = {}
        for spec, result in results:
            if result is None:
                continue
            for draft in result.proposals[:limit]:
                action = self._draft_action(draft)
                if action is None:
                    continue
                existing = seen.get(action.fingerprint())
                if existing is not None:  # two specialists independently agree: merge
                    if spec.role not in existing.author_role.split("+"):
                        existing.author_role += f"+{spec.role}"
                        existing.confidence = max(existing.confidence, draft.confidence)
                    continue
                proposal = self._to_proposal(ctx, spec.role, draft, action=action)
                if proposal is None:
                    continue
                seen[action.fingerprint()] = proposal
                proposals.append(proposal)
        for p in proposals:
            self._emit(
                ctx,
                "caucus.proposal",
                f"{p.id} by {p.author_role}: {p.action.describe()}",
                p.model_dump(mode="json"),
                actor=f"specialist.{p.author_role.split('+')[0]}",
            )
        return proposals

    async def _critique(
        self,
        ctx: WorkingContext,
        level: Level,
        proposals: list[Proposal],
        table: RankTable,
        round_name: str,
    ) -> list[Critique]:
        specs = self.roster.critics(level)
        listing = "\n\n".join(self._describe(p) for p in proposals)
        valid = {p.id for p in proposals}

        async def one(spec: AgentSpec) -> tuple[AgentSpec, CritiqueSet | None]:
            return spec, await self._ask(
                ctx,
                spec,
                "critique",
                CritiqueSet,
                f"caucus.{round_name}.{spec.role}",
                proposals=listing,
                focus_hint=spec.focus_hint,
            )

        critiques: list[Critique] = []
        for spec, result in await _gather(*(one(s) for s in specs)):
            if result is None:
                continue
            for draft in result.critiques:
                pid = draft.proposal_id.strip().upper()
                if pid not in valid:
                    continue
                critique = Critique(
                    proposal_id=pid,
                    author_role=spec.role,
                    verdict=draft.verdict,
                    concerns=draft.concerns,
                    amendment=draft.amendment,
                    charter_violation=draft.charter_violation,
                    score=draft.score,
                )
                critiques.append(critique)
                table.add(spec.role, pid, critique.score)
                if critique.charter_violation:
                    table.flag(pid, f"{spec.role}: {critique.charter_violation}")
                self._emit(
                    ctx,
                    "caucus.critique",
                    f"{spec.role} {critique.verdict}s {pid} ({critique.score}/5)",
                    critique.model_dump(mode="json"),
                    actor=f"specialist.{spec.role}",
                )
        if not specs:
            for p in proposals:  # no critics enabled: fall back to self-reported confidence
                table.add("confidence", p.id, 1 + round(p.confidence * 4))
        return critiques

    async def _revise(
        self, ctx: WorkingContext, proposals: list[Proposal], critiques: list[Critique]
    ) -> list[Proposal]:
        contested = {c.proposal_id for c in critiques if c.verdict != "support"}
        by_author: dict[str, list[Proposal]] = {}
        for p in proposals:
            if p.id in contested:
                by_author.setdefault(p.author_role.split("+")[0], []).append(p)
        if not by_author:
            return proposals

        async def one(role: str, own: list[Proposal]) -> tuple[str, RevisionSet | None]:
            spec = self.roster.get(role)
            text = "\n\n".join(
                self._describe(p)
                + "\n  critiques:\n"
                + "\n".join(
                    f"   - {c.verdict} ({c.score}/5): {'; '.join(c.concerns) or '-'}"
                    + (f" | amendment: {c.amendment}" if c.amendment else "")
                    + (f" | charter: {c.charter_violation}" if c.charter_violation else "")
                    for c in critiques
                    if c.proposal_id == p.id
                )
                for p in own
            )
            return role, await self._ask(
                ctx,
                spec,
                "revise",
                RevisionSet,
                f"caucus.revise.{role}",
                own=text,
                catalog=ctx.catalog,
            )

        results = await _gather(*(one(r, own) for r, own in by_author.items()))
        updated = {p.id: p for p in proposals}
        for role, result in results:
            if result is None:
                continue
            own_ids = {p.id for p in by_author[role]}
            for revision in result.revisions:
                pid = revision.proposal_id.strip().upper()
                if pid not in own_ids or pid not in updated:
                    continue
                if revision.action == "withdraw":
                    updated.pop(pid)
                    self._emit(
                        ctx,
                        "caucus.revision",
                        f"{role} withdraws {pid}",
                        {"proposal_id": pid, "action": "withdraw", "note": revision.note},
                        actor=f"specialist.{role}",
                    )
                elif revision.action == "revise" and revision.revised is not None:
                    old = updated[pid]
                    new = self._to_proposal(
                        ctx, old.author_role, revision.revised, pid=pid, revision=old.revision + 1
                    )
                    if new is None:
                        continue  # inadmissible revision: keep the original
                    updated[pid] = new
                    self._emit(
                        ctx,
                        "caucus.revision",
                        f"{role} revises {pid}: {new.action.describe()}",
                        {
                            "proposal_id": pid,
                            "action": "revise",
                            "note": revision.note,
                            "proposal": new.model_dump(mode="json"),
                        },
                        actor=f"specialist.{role}",
                    )
        return [updated[p.id] for p in proposals if p.id in updated]

    async def _rank(
        self,
        ctx: WorkingContext,
        proposals: list[Proposal],
        critiques: list[Critique],
        table: RankTable,
    ) -> None:
        listing = self._debate_text(proposals, critiques)
        valid = {p.id for p in proposals}
        rankers = self.roster.proposers(Level.L2)

        async def one(spec: AgentSpec) -> tuple[AgentSpec, RankSet | None]:
            own = [p.id for p in proposals if spec.role in p.author_role.split("+")]
            if not valid - set(own):  # nothing left for this ranker to score
                return spec, None
            return spec, await self._ask(
                ctx,
                spec,
                "rank",
                RankSet,
                f"caucus.rank.{spec.role}",
                proposals=listing,
                own_ids=", ".join(own),
            )

        for spec, result in await _gather(*(one(s) for s in rankers)):
            if result is None:
                continue
            own = {p.id for p in proposals if spec.role in p.author_role.split("+")}
            for score in result.scores:
                pid = score.proposal_id.strip().upper()
                if pid in valid and pid not in own:
                    table.add(spec.role, pid, score.score)

    def _debate_text(self, proposals: list[Proposal], critiques: list[Critique]) -> str:
        parts = []
        for p in proposals:
            lines = [self._describe(p, anonymous=False)]
            for c in critiques:
                if c.proposal_id == p.id:
                    lines.append(
                        f"  · {c.author_role} {c.verdict}s ({c.score}/5): "
                        + one_line("; ".join(c.concerns) or "-", 300)
                        + (f" [amend: {one_line(c.amendment, 160)}]" if c.amendment else "")
                        + (f" [charter: {c.charter_violation}]" if c.charter_violation else "")
                    )
            parts.append("\n".join(lines))
        return "\n\n".join(parts)

    async def _synthesize(
        self,
        ctx: WorkingContext,
        level: Level,
        proposals: list[Proposal],
        critiques: list[Critique],
        table: RankTable,
        ranking: list[tuple[str, float]],
    ) -> list[ChamberReport]:
        debate = self._debate_text(proposals, critiques)
        ranking_text = render_table(table, ranking)
        members = {
            chamber: [
                s.role
                for s in [*self.roster.proposers(level), *self.roster.critics(level)]
                if s.chamber == chamber
            ]
            for chamber in ("strategy", "operations", "oversight")
        }
        valid = {p.id for p in proposals}

        async def one(spec: AgentSpec) -> tuple[AgentSpec, ManagerReport | None]:
            return spec, await self._ask(
                ctx,
                spec,
                "manager",
                ManagerReport,
                f"caucus.report.{spec.chamber}",
                debate=debate,
                ranking=ranking_text,
                chamber=spec.chamber,
                members=", ".join(members.get(spec.chamber, [])) or "(none)",
            )

        reports: list[ChamberReport] = []
        for spec, result in await _gather(*(one(s) for s in self.roster.managers())):
            if result is None:
                continue
            report = ChamberReport(
                chamber=spec.chamber,
                top=[t.strip().upper() for t in result.top if t.strip().upper() in valid],
                consensus=result.consensus,
                key_risks=result.key_risks,
                dissent=result.dissent,
            )
            reports.append(report)
            self._emit(
                ctx,
                "caucus.report",
                f"{spec.chamber}: top {', '.join(report.top) or '-'}",
                report.model_dump(mode="json"),
                actor=f"manager.{spec.chamber}",
            )
        return reports

    async def _decide(
        self,
        ctx: WorkingContext,
        level: Level,
        proposals: list[Proposal],
        critiques: list[Critique],
        table: RankTable,
        ranking: list[tuple[str, float]],
        reports: list[ChamberReport],
    ) -> Decision:
        by_id = {p.id: p for p in proposals}
        shortlist_ids = [pid for pid, _ in ranking[:SHORTLIST]]
        shortlist = "\n\n".join(
            self._describe(by_id[pid], anonymous=False)
            + f"\n  ranking points: {dict(ranking)[pid]:.2f}"
            + (f"\n  ⚑ charter concern: {table.flagged[pid]}" if pid in table.flagged else "")
            for pid in shortlist_ids
        )
        if reports:
            report_text = "\n\n".join(
                f"[{r.chamber}] top: {', '.join(r.top) or '-'}\n  consensus: {r.consensus}\n"
                f"  risks: {'; '.join(r.key_risks) or '-'}\n"
                f"  dissent: {'; '.join(r.dissent) or '-'}"
                for r in reports
            )
        else:
            relevant = [c for c in critiques if c.proposal_id in shortlist_ids]
            report_text = (
                "\n".join(
                    f"- {c.author_role} on {c.proposal_id}: {c.verdict} ({c.score}/5) "
                    f"{one_line('; '.join(c.concerns), 240)}"
                    for c in relevant
                )
                or "(no critiques)"
            )
        executive = self.roster.get("executive")
        choice = await self._ask(
            ctx,
            executive,
            "executive",
            ExecutiveChoice,
            "caucus.decide",
            tier=self.roster.executive_tier(level),
            shortlist=shortlist,
            reports=report_text,
            lessons=ctx.lessons_text(),
        )
        rationale_prefix = ""
        chosen: Proposal | None = None
        wait_minutes: float | None = None
        wake_on: list[str] = []
        if choice is None:
            chosen = by_id[shortlist_ids[0]] if shortlist_ids else None
            rationale_prefix = "(executive unavailable; defaulted to the top-ranked proposal) "
            rationale = chosen.rationale if chosen else "no proposals"
        else:
            rationale = choice.rationale
            pick = choice.choice.strip().upper()
            if pick in ("WAIT", "SLEEP", "NONE"):
                wait_minutes = choice.wait_minutes
                wake_on = [w for w in choice.wake_on if w in ("inbox", "approval", "operator")]
            elif pick in by_id:
                chosen = by_id[pick]
            elif shortlist_ids:
                chosen = by_id[shortlist_ids[0]]
                rationale_prefix = (
                    f"(executive chose unknown option {choice.choice!r}; "
                    "defaulted to the top-ranked proposal) "
                )
        dissent: list[str] = []
        if chosen is not None:
            for c in critiques:
                if c.proposal_id == chosen.id and c.verdict == "oppose":
                    dissent.append(f"{c.author_role}: {'; '.join(c.concerns) or 'opposed'}")
            if chosen.id in table.flagged:
                dissent.append(f"charter concern — {table.flagged[chosen.id]}")
        for report in reports:
            dissent.extend(f"{report.chamber} chamber: {d}" for d in report.dissent)
        if chosen is not None and chosen.action.kind == "wait":
            wait_minutes = chosen.action.wait_minutes
        return Decision(
            cycle_id=ctx.cycle_id,
            level=level,
            chosen=chosen,
            rationale=rationale_prefix + rationale,
            dissent=dissent,
            ranking=[pid for pid, _ in ranking],
            wait_minutes=wait_minutes,
            wake_on=wake_on,
        )
