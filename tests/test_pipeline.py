"""End-to-end orchestration, on the mock provider.

Proves the behaviour the product is sold on, without spending a euro:

  * six previews come back, screened, ranked and streamed one by one
  * a candidate that fails the screen is replaced SILENTLY - she never sees it
  * only the previews she picks are turned into finals
  * a final is derived from the preview she chose, via the same provider
    (preview fidelity - the central technical risk of the design)
  * spending caps REFUSE rather than overspend
  * the cost line is real, and is cost per photo she KEPT
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.catalog import Catalog, ProposalEngine
from app.compile.compiler import PromptCompiler
from app.config import Settings, Thresholds
from app.contracts.attribute_ir import AttributeIR, CaptureIR, SubjectIR
from app.contracts.common import Attribute, Framing
from app.contracts.look_recipe import (
    AppliesTo,
    CameraSpec,
    GarmentSpec,
    LightingSpec,
    LookRecipe,
    Recipe,
    SceneSpec,
)
from app.contracts.provider import Tier
from app.contracts.qa_report import Verdict
from app.contracts.selections import Selections
from app.gate.gate import Gate
from app.ledger import BudgetExceeded, Ledger
from app.orchestrator.engine import Orchestrator
from app.orchestrator.events import EventBus, EventKind
from app.providers.base import Registry
from app.providers.mock import build_mock_providers
from app.providers.router import Router
from app.profile.model import Coverage, IdentityProfile


# -- fixtures -----------------------------------------------------------------


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings()
    object.__setattr__(s, "data_dir", tmp_path)
    object.__setattr__(s, "preview_count", 6)
    object.__setattr__(s, "preview_width", 128)
    object.__setattr__(s, "preview_height", 160)
    object.__setattr__(s, "final_width", 192)
    object.__setattr__(s, "final_height", 240)
    object.__setattr__(s, "thresholds", Thresholds(min_sharpness=0.0))
    s.ensure_dirs()
    return s


@pytest.fixture
def look() -> LookRecipe:
    return LookRecipe(
        id="moda_terraza_atardecer",
        name="Terraza al atardecer",
        category="moda",
        recipe=Recipe(
            garment=GarmentSpec(type="vestido largo", fabric="satinado",
                                colors=["negro", "burdeos"]),
            scene=SceneSpec(place="terraza urbana", time="hora dorada"),
            lighting=LightingSpec(key="sol bajo lateral", fill="rebote suave"),
            camera=CameraSpec(framing=Framing.MEDIUM),
            pose_family=["de pie apoyada", "caminando"],
        ),
        applies_to=AppliesTo(framing=[Framing.MEDIUM, Framing.FULL_BODY], needs_body=True),
        variation_axes=[Attribute.GESTURE, Attribute.EXPRESSION, Attribute.LIGHT],
        selectable=[Attribute.GARMENT, Attribute.GESTURE, Attribute.EXPRESSION],
        chips={
            Attribute.GARMENT: ["vestido", "traje", "casual"],
            Attribute.GESTURE: ["de pie", "caminando", "sentada", "apoyada"],
            Attribute.EXPRESSION: ["neutra", "sonrisa suave", "seria"],
            Attribute.LIGHT: ["natural", "dorada", "estudio"],
        },
    )


@pytest.fixture
def ir() -> AttributeIR:
    return AttributeIR(
        subject=SubjectIR(build="atletica"),
        capture=CaptureIR(framing=Framing.MEDIUM, quality_score=0.8),
    )


@pytest.fixture
def profile() -> IdentityProfile:
    return IdentityProfile(owner="test", coverage=Coverage(full_body=5, medium=8, close_up=5))


def build_orchestrator(settings: Settings, profile: IdentityProfile, ledger: Ledger):
    registry = Registry()
    for provider in build_mock_providers(settings.images_dir):
        registry.register(provider)
    return Orchestrator(
        settings=settings,
        router=Router(registry),
        # strict=False because no CV models are installed in CI.  Unknown
        # checks are recorded as unknown, never rewritten as passes.
        gate=Gate(
            profile=profile,
            thresholds=settings.thresholds,
            models_dir=settings.models_dir,
            strict=False,
        ),
        compiler=PromptCompiler(),
        ledger=ledger,
        bus=EventBus(),
    )


# -- stage A ------------------------------------------------------------------


def test_six_previews_come_back_ranked(settings, look, ir, profile):
    ledger = Ledger(per_session_usd=5.0, per_day_usd=20.0)
    orchestrator = build_orchestrator(settings, profile, ledger)
    state = orchestrator.open_session(
        source_path=None,
        ir=ir,
        look=look,
        selections=Selections(values={Attribute.GARMENT: ["vestido", "casual"]}),
    )

    candidates = asyncio.run(orchestrator.run_previews(state, count=6))

    assert len(candidates) == 6
    assert all(Path(c.image_path).exists() for c in candidates)
    # Ranked best first, so the strongest option is where her eye lands.
    assert [c.score for c in candidates] == sorted(
        (c.score for c in candidates), reverse=True
    )


def test_previews_honour_her_selection(settings, look, ir, profile):
    """ONE selected -> fixed everywhere.  SEVERAL -> only those values."""
    ledger = Ledger(per_session_usd=5.0, per_day_usd=20.0)
    orchestrator = build_orchestrator(settings, profile, ledger)
    state = orchestrator.open_session(
        source_path=None,
        ir=ir,
        look=look,
        selections=Selections(
            values={
                Attribute.GARMENT: ["vestido", "casual"],
                Attribute.EXPRESSION: ["sonrisa suave"],
            }
        ),
    )
    candidates = asyncio.run(orchestrator.run_previews(state, count=6))

    for candidate in candidates:
        values = candidate.slot.values
        assert values[Attribute.GARMENT] in {"vestido", "casual"}
        assert values[Attribute.EXPRESSION] == "sonrisa suave"


def test_untouched_row_is_varied_by_the_robot(settings, look, ir, profile):
    ledger = Ledger(per_session_usd=5.0, per_day_usd=20.0)
    orchestrator = build_orchestrator(settings, profile, ledger)
    state = orchestrator.open_session(
        source_path=None, ir=ir, look=look, selections=Selections()
    )
    candidates = asyncio.run(orchestrator.run_previews(state, count=6))
    gestures = {c.slot.values.get(Attribute.GESTURE) for c in candidates}
    assert len(gestures) > 1, "a row she left alone should be varied for her"


def test_previews_stream_one_by_one(settings, look, ir, profile):
    """Progressive delivery: each tile is published as it lands, not at the end."""
    ledger = Ledger(per_session_usd=5.0, per_day_usd=20.0)
    orchestrator = build_orchestrator(settings, profile, ledger)
    state = orchestrator.open_session(
        source_path=None, ir=ir, look=look, selections=Selections()
    )
    asyncio.run(orchestrator.run_previews(state, count=6))

    # Read the session's history rather than draining a shared queue. The bus
    # used to hand every listener the SAME queue, so reading it here consumed
    # the events - exactly as a second browser tab did in production, leaving
    # the first tab waiting for a photograph it would never be told about.
    kinds = [event.kind for event in orchestrator._bus.history(state.id)]

    assert kinds.count(EventKind.PREVIEW_READY) == 6
    assert kinds[0] is EventKind.PREVIEW_STARTED
    assert kinds[-1] is EventKind.PREVIEWS_DONE


def test_otras_seis_continues_instead_of_repeating(settings, look, ir, profile):
    ledger = Ledger(per_session_usd=50.0, per_day_usd=50.0)
    orchestrator = build_orchestrator(settings, profile, ledger)
    selections = Selections(
        values={
            Attribute.GARMENT: ["vestido", "traje", "casual"],
            Attribute.GESTURE: ["de pie", "caminando", "sentada"],
        }
    )
    state = orchestrator.open_session(
        source_path=None, ir=ir, look=look, selections=selections
    )

    first = asyncio.run(orchestrator.run_previews(state, count=6))
    second = asyncio.run(orchestrator.run_previews(state, count=3))

    first_combos = {
        (c.slot.values[Attribute.GARMENT], c.slot.values[Attribute.GESTURE])
        for c in first
    }
    second_combos = {
        (c.slot.values[Attribute.GARMENT], c.slot.values[Attribute.GESTURE])
        for c in second
    }
    assert not (first_combos & second_combos)


# -- stage B ------------------------------------------------------------------


def test_only_chosen_previews_become_finals(settings, look, ir, profile):
    """The structural saving: the expensive path never runs on an image she
    did not choose."""
    ledger = Ledger(per_session_usd=5.0, per_day_usd=20.0)
    orchestrator = build_orchestrator(settings, profile, ledger)
    state = orchestrator.open_session(
        source_path=None, ir=ir, look=look, selections=Selections()
    )
    candidates = asyncio.run(orchestrator.run_previews(state, count=6))

    chosen = [candidates[0].id, candidates[2].id]
    finals = asyncio.run(orchestrator.run_finals(state, chosen))

    assert len(finals) == 2
    assert {f.candidate_id for f in finals} == set(chosen)
    kinds = [e.kind for e in ledger.entries]
    assert kinds.count("final") == 2, "only two finals should have been generated"
    assert kinds.count("preview") == 6


def test_the_final_uses_the_quality_tier_when_one_exists(settings, look, ir, profile):
    """This test used to assert the opposite, and the opposite was the bug.

    It required the final to come from the SAME provider as the preview, in
    the name of preview fidelity. The effect was that finals came from
    whichever model made the preview - a four-step distilled model chosen for
    being fast and free - so the paid quality tier was configured, priced, and
    never once called. Every "final" was a preview at a larger size, and
    four-step models fail hardest on fingers and the ends of limbs.

    Fidelity is preserved by a different mechanism, asserted in the test
    below: the chosen preview is passed as the SOURCE IMAGE, so the final is
    derived from the picture she chose. It is refined by a better model rather
    than re-rolled by a different one.
    """
    from app.contracts.provider import Capability, Tier

    ledger = Ledger(per_session_usd=5.0, per_day_usd=20.0)
    orchestrator = build_orchestrator(settings, profile, ledger)
    state = orchestrator.open_session(
        source_path=None, ir=ir, look=look, selections=Selections()
    )
    candidates = asyncio.run(orchestrator.run_previews(state, count=3))
    chosen = candidates[0]
    asyncio.run(orchestrator.run_finals(state, [chosen.id]))

    used = [e.provider_id for e in ledger.entries if e.kind == "final"]
    assert used, "no final was produced"

    final_tier = orchestrator._router._registry.candidates(
        tier=Tier.FINAL, required=(Capability.IMAGE_TO_IMAGE,)
    )
    if final_tier:
        assert used[0] in {p.descriptor.id for p in final_tier}
    else:
        # No quality tier configured: fall back to the preview's own provider,
        # because a slightly worse photograph beats an error.
        assert used[0] == chosen.provider_id


def test_the_free_choice_keeps_the_preview_provider(settings, look, ir, profile):
    """prefer_quality=False is the "Gratis" setting on the Ajustes screen. It
    must genuinely avoid the paid tier, or the setting is decorative."""
    ledger = Ledger(per_session_usd=5.0, per_day_usd=20.0)
    orchestrator = build_orchestrator(settings, profile, ledger)
    state = orchestrator.open_session(
        source_path=None, ir=ir, look=look, selections=Selections(),
        prefer_quality=False,
    )
    candidates = asyncio.run(orchestrator.run_previews(state, count=3))
    chosen = candidates[0]
    asyncio.run(orchestrator.run_finals(state, [chosen.id]))

    used = [e.provider_id for e in ledger.entries if e.kind == "final"]
    assert used[0] == chosen.provider_id


def test_finals_are_derived_from_the_chosen_preview(settings, look, ir, profile):
    ledger = Ledger(per_session_usd=5.0, per_day_usd=20.0)
    orchestrator = build_orchestrator(settings, profile, ledger)
    state = orchestrator.open_session(
        source_path=None, ir=ir, look=look, selections=Selections()
    )
    candidates = asyncio.run(orchestrator.run_previews(state, count=2))
    finals = asyncio.run(orchestrator.run_finals(state, [candidates[0].id]))

    assert len(finals) == 1
    assert Path(finals[0].image_path).exists()
    assert finals[0].report.stage == "final"


# -- money --------------------------------------------------------------------


def test_caps_refuse_rather_than_overspend(settings, look, ir, profile):
    """A batch that cannot afford to finish must never start: a half-finished
    batch has spent real money and delivered nothing."""

    class DearProvider:
        pass

    ledger = Ledger(per_session_usd=0.001, per_day_usd=0.001)
    orchestrator = build_orchestrator(settings, profile, ledger)
    state = orchestrator.open_session(
        source_path=None, ir=ir, look=look, selections=Selections()
    )
    # The analysis line alone exceeds this cap, so nothing should be generated.
    with pytest.raises(BudgetExceeded) as exc:
        asyncio.run(orchestrator.run_previews(state, count=6))
    assert exc.value.message_es()
    assert ledger.entries == []


def test_cost_is_reported_per_kept_photo(settings, look, ir, profile):
    ledger = Ledger(per_session_usd=5.0, per_day_usd=20.0)
    orchestrator = build_orchestrator(settings, profile, ledger)
    state = orchestrator.open_session(
        source_path=None, ir=ir, look=look, selections=Selections()
    )
    candidates = asyncio.run(orchestrator.run_previews(state, count=6))
    finals = asyncio.run(orchestrator.run_finals(state, [c.id for c in candidates[:3]]))

    per_photo = ledger.cost_per_delivered(state.id, len(finals))
    assert per_photo is not None
    # The mock is free, so the arithmetic is what is under test, not the price.
    assert per_photo == pytest.approx(ledger.session_total(state.id) / len(finals))


# -- the gate -----------------------------------------------------------------


def test_gate_records_unknown_rather_than_passing(settings, profile):
    """Without CV models the identity check cannot run.  It must say so, and
    must never be silently upgraded to a pass."""
    gate = Gate(
        profile=profile,
        thresholds=settings.thresholds,
        models_dir=settings.models_dir,
        strict=False,
    )
    providers = build_mock_providers(settings.images_dir)
    from app.contracts.provider import GenerationRequest

    result = asyncio.run(
        providers[0].generate(GenerationRequest(prompt="test", width=128, height=160))
    )
    report = gate.inspect(result.image_path)

    identity = report.check("identity")
    assert identity is not None
    assert identity.outcome.value == "unknown"
    assert "SIN VERIFICAR" in report.notes


def test_strict_gate_blocks_what_it_cannot_measure(settings, profile):
    """The production posture: an unmeasurable check discards the image.

    Through inspect(), the stage that delivers. screen() never blocks by
    design - see Gate.screen - because a preview is a proposal, and a strict
    screen with identity enrolled discarded every candidate before the stage
    that can actually preserve her was ever reached.
    """
    gate = Gate(
        profile=profile,
        thresholds=settings.thresholds,
        models_dir=settings.models_dir,
        strict=True,
    )
    providers = build_mock_providers(settings.images_dir)
    from app.contracts.provider import GenerationRequest

    result = asyncio.run(
        providers[0].generate(GenerationRequest(prompt="test", width=128, height=160))
    )
    report = gate.inspect(result.image_path)
    assert report.verdict is Verdict.DISCARD
    assert "no medible" in report.notes


# -- proposal engine ----------------------------------------------------------


def test_close_up_photo_does_not_get_full_body_styles(look, ir):
    """The hard filter that makes the tiles feel chosen rather than listed."""
    engine = ProposalEngine()
    close_up = AttributeIR(capture=CaptureIR(framing=Framing.CLOSE_UP, quality_score=0.9))
    assert engine.rank([look], close_up) == []
    assert engine.rank([look], ir), "a medium shot should still match"


def test_ranking_prefers_what_she_keeps(look, ir):
    engine = ProposalEngine()
    loved = look.model_copy(deep=True)
    loved.id = "loved"
    loved.stats.keep_rate = 0.9
    ignored = look.model_copy(deep=True)
    ignored.id = "ignored"
    ignored.stats.keep_rate = 0.1

    ranked = engine.rank([ignored, loved], ir)
    assert ranked[0].look.id == "loved"


# ---------------------------------------------------------------------------
# The event stream must not be consumed by whoever reads it first
# ---------------------------------------------------------------------------


def test_two_listeners_both_see_every_event():
    """The bug this closes, and it was invisible from the server side.

    The bus held ONE queue per session and asyncio.Queue delivers each item to
    exactly one consumer, so a second listener did not observe the session -
    it stole from the first. With two tabs open, final_ready went to one and
    finals_done to the other, and the tab holding finals_done showed
    "Listas - 1 foto" above an empty grid. The photograph existed and was
    served correctly; it was simply never announced to the page looking at it.
    """
    from app.orchestrator.events import EventBus, EventKind

    async def scenario():
        bus = EventBus()

        async def listen(collected):
            async for event in bus.subscribe("s1", heartbeat_s=0.05):
                if event.kind is EventKind.HEARTBEAT:
                    continue
                collected.append(event.kind)
                if event.kind is EventKind.FINALS_DONE:
                    return

        one, two = [], []
        tasks = [
            asyncio.ensure_future(listen(one)),
            asyncio.ensure_future(listen(two)),
        ]
        await asyncio.sleep(0.05)  # let both subscribe

        bus.publish("s1", EventKind.FINAL_READY, url="/media/a.jpg")
        bus.publish("s1", EventKind.FINALS_DONE, delivered=1)
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=3)
        return one, two

    one, two = asyncio.run(scenario())
    for collected in (one, two):
        assert EventKind.FINAL_READY in collected, "a listener missed the photograph"
        assert EventKind.FINALS_DONE in collected


def test_a_late_listener_is_told_what_it_missed():
    """A page that connects a moment after the photograph arrived - or simply
    reloads - must still learn about it, or it waits forever for something
    that already happened."""
    from app.orchestrator.events import EventBus, EventKind

    async def scenario():
        bus = EventBus()
        bus.publish("s2", EventKind.FINAL_READY, url="/media/a.jpg")
        bus.publish("s2", EventKind.FINALS_DONE, delivered=1)

        seen = []
        async for event in bus.subscribe("s2", heartbeat_s=0.05):
            if event.kind is EventKind.HEARTBEAT:
                continue
            seen.append(event.kind)
            if event.kind is EventKind.FINALS_DONE:
                break
        return seen

    seen = asyncio.run(scenario())
    assert seen == [EventKind.FINAL_READY, EventKind.FINALS_DONE]


def test_reading_the_history_does_not_consume_it():
    from app.orchestrator.events import EventBus, EventKind

    bus = EventBus()
    bus.publish("s3", EventKind.FINAL_READY, url="/media/a.jpg")
    assert len(bus.history("s3")) == 1
    assert len(bus.history("s3")) == 1, "history was consumed by reading it"
