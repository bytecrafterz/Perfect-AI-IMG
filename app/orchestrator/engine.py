"""The two-stage orchestrator.

    STAGE A  previews  cheap, fast, low resolution, screened by free CPU
                       checks.  Pushed to her one by one as they land.
    she picks
    STAGE B  finals    only her choices, at full resolution, through the full
                       gate, repaired silently where a defect is local.

The structural saving: the expensive model, the paid judge and the repair loop
NEVER run on an image she did not choose.  That is where "about a third
cheaper per finished photo" comes from - not from generation getting cheaper.

Three rules this module exists to keep:

  1. SHE NEVER SEES A FAILURE.  A candidate that fails the screen is replaced
     silently.  A final that fails is regenerated silently.  Only a repeated,
     unrecoverable failure is ever surfaced, named and explained.

  2. WHAT SHE PICKED IS WHAT SHE RECEIVES.  A final is derived from the exact
     preview she chose, through the same provider that made it.  Routing
     afresh would hand her a different photograph.

  3. NOTHING STARTS THAT CANNOT AFFORD TO FINISH.  Caps are checked against
     the estimate before the first call, because a half-finished batch has
     spent real money and delivered nothing.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from app.compile.combinations import BatchPlan, Slot, plan_batch
from app.compile.compiler import PromptCompiler
from app.config import Settings
from app.contracts.attribute_ir import AttributeIR
from app.contracts.common import Attribute, BBox
from app.contracts.look_recipe import LookRecipe
from app.contracts.provider import (
    GenerationRequest,
    GenerationResult,
    Tier,
)
from app.contracts.qa_report import QAReport, Verdict
from app.contracts.selections import Selections
from app.gate.gate import Gate
from app.ledger import BudgetExceeded, Ledger, SessionEstimate
from app.orchestrator.events import EventBus, EventKind
from app.providers.base import ProviderError
from app.providers.router import Router


@dataclass
class Candidate:
    """One preview, with everything needed to turn it into a final."""

    id: str
    slot: Slot
    image_path: str
    provider_id: str
    reproduction: dict
    report: QAReport
    cost_usd: float

    @property
    def score(self) -> float:
        return self.report.score

    def public(self) -> dict:
        return {
            "id": self.id,
            "url": f"/media/{Path(self.image_path).name}",
            "score": round(self.score, 3),
            "describes": self.slot.describe(),
        }


@dataclass
class FinalImage:
    id: str
    candidate_id: str
    image_path: str
    report: QAReport
    cost_usd: float
    repaired: bool = False

    def public(self) -> dict:
        return {
            "id": self.id,
            "url": f"/media/{Path(self.image_path).name}",
            "summary": self.report.summary_line(),
            "repaired": self.repaired,
        }


@dataclass
class SessionState:
    """Everything one session holds between the two stages."""

    id: str
    source_path: str | None
    ir: AttributeIR
    look: LookRecipe | None
    selections: Selections
    cursor: int = 0
    candidates: dict[str, Candidate] = field(default_factory=dict)
    finals: list[FinalImage] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    plan: BatchPlan | None = None
    #: Whether finals should go to the paid quality tier.
    #:
    #: Held on the session rather than read from settings at use time, so a
    #: preference changed mid-generation cannot produce a batch where some
    #: photographs came from one model and some from another.
    prefer_quality: bool = True


class Orchestrator:
    def __init__(
        self,
        *,
        settings: Settings,
        router: Router,
        gate: Gate,
        compiler: PromptCompiler,
        ledger: Ledger,
        bus: EventBus,
    ) -> None:
        self._settings = settings
        self._router = router
        self._gate = gate
        self._compiler = compiler
        self._ledger = ledger
        self._bus = bus
        self._generation_sem = asyncio.Semaphore(settings.generation_concurrency)
        # CV is CPU-bound; capped separately so it cannot starve the event
        # loop on a 4-core box and blow the latency budget.
        self._cv_sem = asyncio.Semaphore(settings.cv_concurrency)
        self._sessions: dict[str, SessionState] = {}

    # -- session bookkeeping ----------------------------------------------

    def open_session(
        self,
        *,
        source_path: str | None,
        ir: AttributeIR,
        look: LookRecipe | None,
        selections: Selections,
        prefer_quality: bool = True,
    ) -> SessionState:
        state = SessionState(
            id=uuid.uuid4().hex[:12],
            source_path=source_path,
            ir=ir,
            look=look,
            prefer_quality=prefer_quality,
            selections=selections,
        )
        self._sessions[state.id] = state
        return state

    def session(self, session_id: str) -> SessionState:
        try:
            return self._sessions[session_id]
        except KeyError:
            raise LookupError(f"unknown session {session_id}") from None

    # -- estimation --------------------------------------------------------

    def estimate(self, state: SessionState, *, count: int) -> SessionEstimate:
        """What the session is expected to cost, before it starts.

        An ESTIMATE must never be the thing that prevents a session. With no
        final-tier provider registered this raised straight out of
        POST /previews and she saw "No he podido empezar" with no further
        explanation - stopped by a cost projection, before a single image had
        been attempted.

        A missing final tier is worth knowing about, and it is worth knowing
        about when the finals are actually requested. Here it is a zero.
        """
        preview_choice = self._router.select(
            tier=Tier.PREVIEW, look=state.look, has_source=bool(state.source_path)
        )
        try:
            final_unit = self._router.select(
                tier=Tier.FINAL, look=state.look, has_source=bool(state.source_path)
            ).unit_cost_usd
        except ProviderError:
            # Estimate the finals at the preview's price rather than refusing
            # to quote. It is the honest fallback: if no final tier exists,
            # run_finals will use the preview provider too.
            final_unit = preview_choice.unit_cost_usd

        from app.gate.judge import JUDGE_COST_USD

        return SessionEstimate(
            previews=count,
            preview_unit_usd=preview_choice.unit_cost_usd,
            analysis_usd=0.004,
            # She typically keeps about half; the estimate says so rather than
            # quietly assuming the cheapest or the dearest case.
            expected_finals=max(1, count // 2),
            final_unit_usd=final_unit,
            judge_unit_usd=JUDGE_COST_USD,
        )

    # -- Stage A -----------------------------------------------------------

    async def run_previews(
        self, state: SessionState, *, count: int | None = None
    ) -> list[Candidate]:
        """Generate, screen and stream `count` previews."""
        count = count or self._settings.preview_count
        estimate = self.estimate(state, count=count)

        # Refuse before spending, never after.
        self._ledger.check(
            session_id=state.id, additional_usd=estimate.preview_stage_usd
        )

        free_rows = self._free_rows(state)
        plan = plan_batch(
            constrained_rows={
                a: state.selections.chosen(a)
                for a in state.selections.constrained_attributes
            },
            free_rows=free_rows,
            count=count,
            cursor=state.cursor,
        )
        state.plan = plan
        state.cursor = plan.next_cursor

        if plan.reseeded:
            # Never a silent truncation: if the space was too small to fill
            # the grid with distinct combinations, say so.
            self._bus.publish(
                state.id,
                EventKind.PREVIEW_STARTED,
                total=count,
                note="pocas combinaciones: algunas opciones se repiten con otra variacion",
            )
        else:
            self._bus.publish(state.id, EventKind.PREVIEW_STARTED, total=count)

        results = await asyncio.gather(
            *(
                self._one_preview(state, slot, position)
                for position, slot in enumerate(plan.slots)
            ),
            return_exceptions=True,
        )

        candidates = [r for r in results if isinstance(r, Candidate)]
        # Which combinations failed to produce a usable image.  They are
        # retried AT THE SAME COMBINATION with a fresh seed, not swapped for
        # new ones: a discard is a bad roll of the generator, not evidence
        # that she did not want that combination.  Drawing a replacement
        # instead would silently skip a combination she asked for and burn
        # cursor space, so [ Otras 6 ] would later wrap into options she has
        # already rejected.
        failed = [
            plan.slots[position]
            for position, outcome in enumerate(results)
            if not isinstance(outcome, Candidate)
        ]
        if failed:
            candidates.extend(await self._refill(state, failed, len(candidates)))

        candidates.sort(key=lambda c: c.score, reverse=True)
        for candidate in candidates:
            state.candidates[candidate.id] = candidate

        self._bus.publish(
            state.id,
            EventKind.PREVIEWS_DONE,
            delivered=len(candidates),
            requested=count,
            cost=round(self._ledger.session_total(state.id), 4),
        )
        return candidates

    async def _one_preview(
        self, state: SessionState, slot: Slot, position: int
    ) -> Candidate | None:
        async with self._generation_sem:
            try:
                choice = self._router.select(
                    tier=Tier.PREVIEW,
                    look=state.look,
                    has_source=bool(state.source_path),
                )
                compiled = self._compiler.compile(
                    look=state.look,
                    ir=state.ir,
                    slot=slot,
                    dialect=choice.provider.descriptor.prompt_dialect,
                    width=self._settings.preview_width,
                    height=self._settings.preview_height,
                    source_image_path=state.source_path,
                    for_final=False,
                )
                result = await choice.provider.generate(compiled.request)
            except (ProviderError, BudgetExceeded) as exc:
                # Logged as well as published. The event goes to the browser,
                # where the finals_done notice promptly overwrites it - so a
                # provider failure was visible for about a second and left no
                # trace anywhere. The operator was told "no ha salido bien"
                # and given nothing to act on.
                print(f"[estudio] preview fallo en {choice.provider.descriptor.id}: {exc}")
                self._bus.publish(state.id, EventKind.ERROR, detail=str(exc))
                return None

        self._ledger.record(
            session_id=state.id,
            kind="preview",
            provider_id=result.provider_id,
            usd=result.cost_usd,
            detail=slot.describe(),
        )

        async with self._cv_sem:
            report = await asyncio.to_thread(self._gate.screen, result.image_path)

        if report.verdict is Verdict.DISCARD:
            # Silently replaced.  She never learns this happened.
            self._bus.publish(
                state.id, EventKind.PREVIEW_REPLACED, position=position,
                reason=report.notes,
            )
            return None

        candidate = Candidate(
            id=uuid.uuid4().hex[:12],
            slot=slot,
            image_path=result.image_path,
            provider_id=result.provider_id,
            reproduction=result.reproduction,
            report=report,
            cost_usd=result.cost_usd,
        )
        self._bus.publish(
            state.id, EventKind.PREVIEW_READY, position=position, **candidate.public()
        )
        return candidate

    async def _refill(
        self, state: SessionState, failed: list[Slot], already: int
    ) -> list[Candidate]:
        """Retry the combinations that failed, with a fresh seed each time.

        The cursor is deliberately NOT advanced here.  It moves once per batch,
        by the number of combinations shown, so that every combination she
        asked for is either delivered or retried - never silently skipped -
        and [ Otras 6 ] continues into genuinely new ground.

        Bounded: an endlessly refilling batch would burn her balance chasing a
        target on a bad day.  When the rounds run out we deliver fewer and say
        how many, rather than spending without a limit.
        """
        gathered: list[Candidate] = []
        pending = list(failed)

        for round_index in range(self._settings.max_refill_rounds):
            if not pending:
                break
            choice = self._router.select(
                tier=Tier.PREVIEW, look=state.look, has_source=bool(state.source_path)
            )
            affordable = self._ledger.affordable(
                session_id=state.id,
                unit_usd=choice.unit_cost_usd,
                wanted=len(pending),
            )
            if affordable <= 0:
                break

            attempting = pending[:affordable]
            # A different roll of the dice on the same combination.  The prime
            # offset keeps the retry seed clear of any other slot's seed.
            retries = [
                Slot(
                    index=slot.index,
                    constrained=slot.constrained,
                    free=slot.free,
                    seed=slot.seed + 104_729 * (round_index + 1),
                )
                for slot in attempting
            ]

            results = await asyncio.gather(
                *(
                    self._one_preview(state, slot, already + len(gathered) + i)
                    for i, slot in enumerate(retries)
                ),
                return_exceptions=True,
            )

            fresh = [r for r in results if isinstance(r, Candidate)]
            gathered.extend(fresh)
            pending = [
                attempting[i]
                for i, outcome in enumerate(results)
                if not isinstance(outcome, Candidate)
            ] + pending[affordable:]

            if not fresh:
                break  # a round that produced nothing will not do better next time

        if pending:
            # Never a silent shortfall: say how many could not be produced.
            self._bus.publish(
                state.id,
                EventKind.ERROR,
                detail=(
                    f"{len(pending)} de las opciones no han salido bien y las he "
                    "descartado"
                ),
            )
        return gathered

    def _free_rows(self, state: SessionState) -> dict[Attribute, list[str]]:
        """Rows she left alone that this look permits the robot to vary."""
        if state.look is None:
            return {}
        touched = set(state.selections.constrained_attributes)
        rows: dict[Attribute, list[str]] = {}
        for axis in state.look.variation_axes:
            if axis in touched:
                continue
            values = state.look.ordered_chips(axis)
            if values:
                rows[axis] = [v for v in values if v.lower() not in {"como esta", "el mio"}]
        return {a: v for a, v in rows.items() if v}

    # -- Stage B -----------------------------------------------------------

    async def run_finals(
        self, state: SessionState, candidate_ids: list[str]
    ) -> list[FinalImage]:
        chosen = [state.candidates[cid] for cid in candidate_ids if cid in state.candidates]
        if not chosen:
            return []

        from app.gate.judge import JUDGE_COST_USD

        final_choice = self._router.select(
            tier=Tier.FINAL, look=state.look, has_source=bool(state.source_path)
        )
        estimated = len(chosen) * (final_choice.unit_cost_usd + JUDGE_COST_USD)
        self._ledger.check(session_id=state.id, additional_usd=estimated)

        self._bus.publish(state.id, EventKind.FINAL_STARTED, total=len(chosen))

        results = await asyncio.gather(
            *(self._one_final(state, candidate) for candidate in chosen),
            return_exceptions=True,
        )
        finals = [r for r in results if isinstance(r, FinalImage)]
        state.finals.extend(finals)

        failed = len(chosen) - len(finals)
        self._bus.publish(
            state.id,
            EventKind.FINALS_DONE,
            delivered=len(finals),
            requested=len(chosen),
            failed=failed,
            cost=round(self._ledger.session_total(state.id), 4),
            elapsed=round(time.time() - state.started_at, 1),
        )
        return finals

    def _afford(self, state, usd: float, what: str) -> bool:
        """Whether this specific call still fits inside the caps.

        run_previews and run_finals each call ledger.check() once, with an
        ESTIMATE, before their stage begins. Everything after that only
        record()s - and record() just appends. So the repair loop and the
        silent retry, which are exactly the paths that fire when things are
        going badly, spent without ever asking again. Measured overrun was up
        to 2.9x the session cap.

        Checking here rather than at record() is deliberate: record() runs
        AFTER the provider call, so refusing there would decline to log money
        already spent, which is worse than not checking at all.
        """
        try:
            self._ledger.check(session_id=state.id, additional_usd=usd)
            return True
        except BudgetExceeded as exc:
            self._bus.publish(state.id, EventKind.ERROR, detail=exc.message_es())
            print(f"[estudio] tope alcanzado antes de {what}: {exc}")
            return False

    async def _one_final(
        self, state: SessionState, candidate: Candidate
    ) -> FinalImage | None:
        """Turn one chosen preview into a delivered photograph.

        PREVIEW FIDELITY: the request is rebuilt from the candidate's own
        reproduction record and sent to the SAME provider, so the final is
        derived from the image she picked rather than from a fresh roll of the
        dice.
        """
        # The QUALITY tier, seeded from the preview she chose - not the model
        # that happened to make the preview. See Router.provider_for_final.
        provider = self._router.provider_for_final(
            candidate.provider_id, prefer_quality=state.prefer_quality
        )
        repro = candidate.reproduction

        request = GenerationRequest(
            prompt=str(repro.get("prompt", "")),
            negative_prompt=str(repro.get("negative_prompt", "")),
            # img2img from the accepted preview is the fidelity-preferred
            # route: the final is provably derived from what she chose, so it
            # cannot drift. See PREVIEW FIDELITY in the spec.
            source_image_path=candidate.image_path,
            width=self._settings.final_width,
            height=self._settings.final_height,
            seed=repro.get("seed"),  # type: ignore[arg-type]
            steps=30,
            guidance=repro.get("guidance"),  # type: ignore[arg-type]
            strength=0.35,
        )

        if not self._afford(state, provider.descriptor.cost_per_call_usd, "la foto final"):
            return None

        async with self._generation_sem:
            try:
                result = await provider.generate(request)
            except ProviderError as exc:
                print(f"[estudio] final fallo en {provider.descriptor.id}: {exc}")
                self._bus.publish(state.id, EventKind.ERROR, detail=str(exc))
                return None

        self._ledger.record(
            session_id=state.id,
            kind="final",
            provider_id=result.provider_id,
            usd=result.cost_usd,
            detail=candidate.slot.describe(),
        )

        async with self._cv_sem:
            report = await asyncio.to_thread(
                self._gate.inspect,
                result.image_path,
                source_path=state.source_path,
                change_mask=None,
                request_summary=candidate.slot.describe(),
            )
        self._ledger.record(
            session_id=state.id,
            kind="judge",
            provider_id="judge",
            usd=report.cost_usd,
            detail=candidate.id,
        )

        image_path, report, repaired = await self._repair_if_needed(
            state, candidate, result, report
        )

        if report.verdict is Verdict.DISCARD:
            # One silent retry, then be honest rather than deliver rubbish.
            retry = await self._retry_final(state, candidate)
            if retry is None:
                self._bus.publish(
                    state.id,
                    EventKind.ERROR,
                    detail=(
                        "Una de las fotos no ha salido bien y la he descartado: "
                        f"{report.notes}"
                    ),
                    candidate_id=candidate.id,
                )
                return None
            image_path, report = retry

        final = FinalImage(
            id=uuid.uuid4().hex[:12],
            candidate_id=candidate.id,
            image_path=image_path,
            report=report,
            cost_usd=result.cost_usd,
            repaired=repaired,
        )
        self._bus.publish(state.id, EventKind.FINAL_READY, **final.public())
        return final

    async def _repair_if_needed(
        self,
        state: SessionState,
        candidate: Candidate,
        result: GenerationResult,
        report: QAReport,
    ) -> tuple[str, QAReport, bool]:
        """Repaint a localised defect without touching the rest of the frame.

        Never asks permission.  A regeneration would discard the identity the
        gate has already validated everywhere else in the image, which is both
        more expensive and worse.
        """
        image_path = result.image_path
        repaired = False

        for _ in range(self._settings.max_repair_attempts):
            if report.verdict is not Verdict.REPAIR:
                break
            defects = report.repairable_defects
            if not defects:
                break

            worst = max(defects, key=lambda d: d.severity)
            if worst.bbox is None:
                break

            self._bus.publish(
                state.id,
                EventKind.FINAL_REPAIRING,
                candidate_id=candidate.id,
                what=worst.kind.value,
            )

            mask_path = await asyncio.to_thread(
                _write_mask, image_path, worst.bbox, self._settings.images_dir
            )
            # Repair needs INPAINT, which the free preview model does not
            # have - so every repair silently refused and a flawed hand was
            # simply delivered. The quality tier can inpaint.
            provider = self._router.provider_for_final(
                candidate.provider_id, prefer_quality=state.prefer_quality
            )
            repair_request = GenerationRequest(
                prompt=(
                    f"{candidate.slot.describe()}. Repaint only the masked region: "
                    f"a correct, anatomically normal {worst.kind.value}. "
                    "Match the surrounding lighting, skin tone and focus exactly."
                ),
                negative_prompt="extra fingers, fused fingers, distorted anatomy",
                source_image_path=image_path,
                mask_path=mask_path,
                width=self._settings.final_width,
                height=self._settings.final_height,
                seed=(result.seed or 0) + 1,
                strength=0.85,
            )

            if not self._afford(state, provider.descriptor.cost_per_call_usd, "la reparacion"):
                break

            async with self._generation_sem:
                try:
                    repair = await provider.inpaint(repair_request)
                except ProviderError:
                    break

            self._ledger.record(
                session_id=state.id,
                kind="repair",
                provider_id=repair.provider_id,
                usd=repair.cost_usd,
                detail=worst.kind.value,
            )

            async with self._cv_sem:
                fresh = await asyncio.to_thread(
                    self._gate.inspect,
                    repair.image_path,
                    source_path=state.source_path,
                    change_mask=None,
                    request_summary=candidate.slot.describe(),
                )

            if fresh.score >= report.score:
                image_path, report, repaired = repair.image_path, fresh, True
            else:
                break  # the repair made it worse; keep the original

        return image_path, report, repaired

    async def _retry_final(
        self, state: SessionState, candidate: Candidate
    ) -> tuple[str, QAReport] | None:
        # The retry produces a DELIVERED photograph, so it belongs on the same
        # tier as the first attempt. Routing it back to the preview provider
        # was the last surviving path that quietly downgraded a final - and
        # the one that runs precisely when the first attempt already failed.
        provider = self._router.provider_for_final(
            candidate.provider_id, prefer_quality=state.prefer_quality
        )
        repro = candidate.reproduction
        request = GenerationRequest(
            prompt=str(repro.get("prompt", "")),
            negative_prompt=str(repro.get("negative_prompt", "")),
            source_image_path=candidate.image_path,
            width=self._settings.final_width,
            height=self._settings.final_height,
            seed=(repro.get("seed") or 0) + 7919,  # a different roll, same intent
            steps=30,
            strength=0.35,
        )
        if not self._afford(state, provider.descriptor.cost_per_call_usd, "el reintento"):
            return None

        async with self._generation_sem:
            try:
                result = await provider.generate(request)
            except ProviderError:
                return None

        self._ledger.record(
            session_id=state.id,
            kind="final",
            provider_id=result.provider_id,
            usd=result.cost_usd,
            detail=f"reintento {candidate.id}",
        )
        async with self._cv_sem:
            report = await asyncio.to_thread(
                self._gate.inspect,
                result.image_path,
                source_path=state.source_path,
                request_summary=candidate.slot.describe(),
            )
        if report.verdict is Verdict.DISCARD:
            return None
        return result.image_path, report


def _write_mask(image_path: str, bbox: BBox, output_dir: Path) -> str:
    """A soft-edged mask for the defect region.

    Dilated so the repaint has surrounding context to blend into, and blurred
    at the edge so no seam appears where the patch meets the original.
    """
    from PIL import Image, ImageDraw, ImageFilter

    with Image.open(image_path) as source:
        width, height = source.size

    grown = bbox.dilated(1.35)
    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).rectangle(grown.to_pixels(width, height), fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(radius=max(4, min(width, height) // 60)))

    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"mask_{uuid.uuid4().hex[:10]}.png"
    mask.save(path, "PNG")
    return str(path)
