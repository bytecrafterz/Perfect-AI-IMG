"""The coverage policy - a switch, not a law.

While COVERAGE_POLICY is on, generated images must keep the subject covered:
closed neckline, covered shoulders and arms, covered midriff and back, legs to
the ankle, nothing sheer. It is on during the POC because testing is happening
under a restricted brief.

It is a SETTING because the restriction is temporary. The subject is expected
to get the full styling range she originally asked for, and switching that on
should be one line in .env - not an archaeology exercise across the compiler,
the judge and twenty catalog files.

So these tests check two things, and the second matters as much as the first:

  ON   the policy cannot be defeated by a look, a chip, or an edited IR
  OFF  nothing lingers - no stray negatives, no judge still failing images
       for a rule nobody is applying any more

The catalog deliberately keeps its authored recipes, exposure and all. The
compiler neutralises them while the policy is on; turning it off restores them
with nothing to undo.
"""

from __future__ import annotations

import pytest

from app.compile.combinations import Slot
from app.compile.compiler import COVERAGE_CLAUSE, PromptCompiler
from app.contracts.attribute_ir import AttributeIR
from app.contracts.common import ALWAYS_LOCKED, Attribute, Framing
from app.contracts.look_recipe import CameraSpec, GarmentSpec, LookRecipe, Recipe
from app.contracts.provider import PromptDialect

DIALECTS = list(PromptDialect)

EXPOSING = (
    "cleavage", "bare shoulders", "off-shoulder", "spaghetti straps",
    "strapless", "sleeveless", "bare back", "open back", "backless",
    "exposed midriff", "crop top", "bare legs", "mini skirt", "shorts",
    "bikini", "swimwear", "lingerie", "sheer",
)


def compile_one(
    *,
    enforce: bool,
    look: LookRecipe | None = None,
    ir: AttributeIR | None = None,
    slot: Slot | None = None,
    dialect: PromptDialect = PromptDialect.NATURAL_VERBOSE,
    for_final: bool = False,
):
    return PromptCompiler(enforce_coverage=enforce).compile(
        look=look,
        ir=ir or AttributeIR(),
        slot=slot or Slot(index=0, seed=1),
        dialect=dialect,
        width=512,
        height=640,
        for_final=for_final,
    )


def reckless_look() -> LookRecipe:
    """A recipe that asks for exactly what the policy forbids."""
    return LookRecipe(
        id="reckless",
        name="reckless",
        category="test",
        recipe=Recipe(
            garment=GarmentSpec(
                type="strapless mini dress",
                fabric="sheer",
                details="backless, spaghetti straps",
            ),
            camera=CameraSpec(framing=Framing.FULL_BODY),
        ),
    )


# -- ON: it holds -----------------------------------------------------------


@pytest.mark.parametrize("dialect", DIALECTS)
def test_every_dialect_states_the_requirement(dialect) -> None:
    prompt = compile_one(enforce=True, dialect=dialect).request.prompt.lower()
    for phrase in ("high closed collar", "legs completely covered", "midriff"):
        assert phrase in prompt, f"{dialect.value} dropped '{phrase}'"


@pytest.mark.parametrize("dialect", DIALECTS)
def test_every_dialect_negates_exposure(dialect) -> None:
    negative = compile_one(enforce=True, dialect=dialect).request.negative_prompt.lower()
    for term in EXPOSING:
        assert term in negative, f"{dialect.value} is missing negative '{term}'"


def test_it_survives_a_final_render() -> None:
    """A policy that holds on previews and lapses on finals is worse than
    none - finals are the deliverable."""
    prompt = compile_one(enforce=True, for_final=True).request.prompt
    assert COVERAGE_CLAUSE.split(":")[0] in prompt


def test_an_edited_ir_cannot_drop_it() -> None:
    """It does not come from ir.locks, so emptying those changes nothing."""
    vandalised = AttributeIR()
    vandalised.locks = []
    vandalised.mutable = list(Attribute)
    prompt = compile_one(enforce=True, ir=vandalised).request.prompt.lower()
    assert "high closed collar" in prompt


def test_a_look_asking_for_exposure_is_neutralised_not_obeyed() -> None:
    compiled = compile_one(enforce=True, look=reckless_look())
    assert "high closed collar" in compiled.request.prompt.lower()
    assert "backless" in compiled.request.negative_prompt.lower()
    assert "sheer" in compiled.request.negative_prompt.lower()


def test_a_chip_selection_cannot_override_it() -> None:
    slot = Slot(index=0, constrained={Attribute.GARMENT: "bikini"}, seed=1)
    compiled = compile_one(enforce=True, slot=slot)
    assert "high closed collar" in compiled.request.prompt.lower()
    assert "bikini" in compiled.request.negative_prompt.lower()


def test_identity_locks_are_not_displaced_by_coverage() -> None:
    """Coverage was added to the same clause list as identity. One silently
    replacing the other would be easy to miss."""
    prompt = compile_one(enforce=True).request.prompt.lower()
    assert "high closed collar" in prompt          # coverage
    assert "without slimming" in prompt              # anti-slimming
    assert "facial identity" in prompt               # identity
    assert set(ALWAYS_LOCKED) == {
        Attribute.FACE, Attribute.BODY_PROPORTIONS, Attribute.SKIN_TONE
    }


# -- OFF: nothing lingers ---------------------------------------------------


@pytest.mark.parametrize("dialect", DIALECTS)
def test_disabled_removes_the_clause_entirely(dialect) -> None:
    prompt = compile_one(enforce=False, dialect=dialect).request.prompt.lower()
    assert "high closed collar" not in prompt
    assert "legs completely covered" not in prompt


@pytest.mark.parametrize("dialect", DIALECTS)
def test_disabled_removes_the_negatives(dialect) -> None:
    """A leftover 'no bare shoulders' would quietly keep constraining her
    after the restriction was lifted, and nobody would think to look here."""
    negative = compile_one(enforce=False, dialect=dialect).request.negative_prompt.lower()
    for term in ("cleavage", "bare shoulders", "sleeveless", "bikini"):
        assert term not in negative, f"'{term}' survived the policy being turned off"


def test_disabled_still_protects_identity_and_proportions() -> None:
    """Turning off coverage must not touch the things that are NOT
    negotiable. Lifting a styling restriction is not permission to reshape
    her body."""
    compiled = compile_one(enforce=False)
    prompt = compiled.request.prompt.lower()
    assert "without slimming" in prompt
    assert "facial identity" in prompt
    assert "face slimming" in compiled.request.negative_prompt.lower()
    assert "extra fingers" in compiled.request.negative_prompt.lower()


def test_disabled_renders_the_look_as_authored() -> None:
    """The catalog keeps its real recipes. With the policy off they reach the
    prompt intact, which is the whole reason for not rewriting them."""
    compiled = compile_one(enforce=False, look=reckless_look())
    prompt = compiled.request.prompt.lower()
    assert "strapless mini dress" in prompt


def test_the_regime_is_recorded_on_every_image() -> None:
    """So it is always possible to tell which rules a given result was
    produced under - necessary when comparing restricted test output against
    later unrestricted work."""
    assert compile_one(enforce=True).rationale["coverage_enforced"] is True
    assert compile_one(enforce=False).rationale["coverage_enforced"] is False


# -- the judge follows the same switch --------------------------------------


def build_judge(enforce: bool):
    from app.gate.judge import VisualJudge

    return VisualJudge(api_key="", model="claude-haiku-4-5", enforce_coverage=enforce)


def test_the_judge_asks_about_coverage_when_enabled() -> None:
    rubric = build_judge(True).rubric
    assert "COVERAGE" in rubric
    assert "coverage_ok=false" in rubric


def test_the_judge_stops_asking_when_disabled() -> None:
    rubric = build_judge(False).rubric
    assert "COVERAGE" not in rubric
    # The rest of the inspection is unaffected.
    assert "HANDS" in rubric


def test_the_rubric_is_byte_stable_for_a_given_policy() -> None:
    """Prompt caching only pays off if the prefix never wobbles between
    calls."""
    judge = build_judge(True)
    assert judge.rubric == judge.rubric


def test_a_coverage_breach_is_never_repairable() -> None:
    """Inpainting a neckline reworks the body, and the body is the one thing
    that must not move. A breach means discard and regenerate."""
    from app.contracts.qa_report import Defect, DefectKind

    breach = Defect(
        kind=DefectKind.OTHER,
        bbox=None,
        severity=1.0,
        detail="no cumple la cobertura requerida",
        source="judge.coverage",
    )
    assert not breach.is_repairable


# -- the setting itself -----------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [("full", True), ("FULL", True), ("", True), ("off", False),
     ("OFF", False), ("none", False), ("false", False), ("0", False)],
)
def test_the_setting_parses_sensibly(value: str, expected: bool, monkeypatch) -> None:
    """Defaults to ON. Someone who has not set it should get the restricted
    behaviour, not the permissive one."""
    import importlib

    monkeypatch.setenv("COVERAGE_POLICY", value)
    import app.config as config

    reloaded = importlib.reload(config)
    try:
        assert reloaded.settings.coverage_enforced is expected
    finally:
        monkeypatch.delenv("COVERAGE_POLICY", raising=False)
        importlib.reload(config)


# ---------------------------------------------------------------------------
# The recipe's own wording must not contradict the policy
# ---------------------------------------------------------------------------


def test_the_recipe_cannot_ask_for_what_the_policy_forbids() -> None:
    """The failure this closes.

    A negative prompt alone was not enough. moda_terraza_atardecer specifies
    'tirantes finos, espalda descubierta' and that text went into the SAME
    prompt as 'no bare back, no bare shoulders'. The model was handed a
    contradiction and resolved it however it liked - which is why exposure
    kept appearing while every audit correctly reported the policy as active.
    """
    from app.compile.compiler import cover_recipe_text

    assert "descubierta" not in (cover_recipe_text("tirantes finos, espalda descubierta") or "")
    assert "abierto" not in (cover_recipe_text("cuello abierto, sin estampado") or "")


@pytest.mark.parametrize(
    "wording",
    [
        "strapless mini dress, backless, spaghetti straps",
        "sheer top, exposed midriff, thigh slit",
        "sleeveless blouse, low neckline",
        "tirantes finos, espalda descubierta",
        "conjunto de encaje, transparente",
        "bikini, toalla",
    ],
)
def test_exposing_wording_never_survives_in_either_language(wording: str) -> None:
    """The catalog is written in Spanish, but nothing ENFORCES that.

    A look or a chip authored in English would walk straight past a
    Spanish-only filter - the quietest way for this policy to stop working
    while every test still reports it on.
    """
    from app.compile.compiler import cover_recipe_text

    result = (cover_recipe_text(wording) or "").lower()
    for banned in (
        "strapless", "backless", "spaghetti", "sheer", "midriff", "slit",
        "sleeveless", "low neckline", "descubierta", "tirantes", "encaje",
        "transparente", "bikini", "toalla",
    ):
        assert banned not in result, f"{banned!r} survived in {result!r}"


def test_already_covered_wording_is_left_alone() -> None:
    """A filter that mangles innocent text would be worse than none - the
    looks would quietly lose the detail that makes them look like anything."""
    from app.compile.compiler import cover_recipe_text

    for safe in (
        "manga larga, cuello alto, falda hasta el suelo",
        "cinturon anudado, cuello subido, bufanda de punto",
        "long wool coat, high collar, full length",
    ):
        assert cover_recipe_text(safe) == safe


def test_the_clause_states_all_three_requirements() -> None:
    """Neck, body, legs - named explicitly by the client, so pinned here
    rather than left to a reviewer's reading of a long sentence."""
    from app.compile.compiler import COVERAGE_CLAUSE

    clause = COVERAGE_CLAUSE.lower()
    assert "throat" in clause and "collar" in clause      # neck closed
    assert "torso" in clause and "midriff" in clause      # body covered
    assert "ankle" in clause                              # legs covered
    assert "opaque" in clause                             # and not see-through


def test_no_visible_look_carries_exposing_wording() -> None:
    """The catalog itself, not just the compiler. A look offered to her should
    not need rescuing by a filter - the filter is the backstop, not the plan.
    """
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    offenders = []
    for path in sorted((root / "catalog").glob("*.json")):
        look = json.loads(path.read_text(encoding="utf-8"))
        if look.get("requires_coverage_off"):
            continue  # already withheld while the policy is on
        garment = (look.get("recipe") or {}).get("garment") or {}
        text = f"{garment.get('type', '')} {garment.get('details', '')}".lower()
        # Strong markers only. A minor detail such as "cuello abierto" on a
        # corporate shirt is rewritten by the compiler and does not justify
        # withholding a whole look; a bare back does, and that look is
        # withheld rather than edited - "temporarily" has to mean the authored
        # look survives intact for the handover.
        for banned in ("descubiert", "tirantes", "escote", "sin mangas"):
            if banned in text:
                offenders.append(f"{look['id']}: {banned}")
    assert offenders == [], f"visible looks still describe exposure: {offenders}"


# ---------------------------------------------------------------------------
# hands and limbs - what the client actually complained about
# ---------------------------------------------------------------------------


def test_anatomy_is_stated_positively_in_every_prompt() -> None:
    """A negative list can only steer away from a failure. It cannot describe
    the thing that should be there instead, and samplers respond better to a
    description than to a prohibition."""
    from app.compile.compiler import ANATOMY_CLAUSE

    clause = ANATOMY_CLAUSE.lower()
    assert "five fingers" in clause
    assert "legs" in clause
    assert "proportioned" in clause


def test_anatomy_survives_with_the_coverage_policy_off() -> None:
    """Coverage is a temporary client preference; correct hands are not. The
    two must not share a switch."""
    import inspect

    from app.compile.compiler import PromptCompiler

    source = inspect.getsource(PromptCompiler)
    used = [line for line in source.splitlines() if "ANATOMY_CLAUSE" in line]
    assert used, "ANATOMY_CLAUSE is never used"
    assert not any("enforce_coverage" in line for line in used), (
        "anatomy must not be conditional on the coverage policy"
    )


def test_the_specific_failures_are_named() -> None:
    """"Distorted anatomy" is too vague to steer a sampler away from the
    particular things that go wrong. The client reported fingers and legs."""
    from app.compile.compiler import _BASE_NEGATIVES

    negatives = {n.lower() for n in _BASE_NEGATIVES}
    for expected in (
        "extra fingers", "fused fingers", "six fingers", "malformed hands",
        "extra legs", "fused legs", "misshapen legs", "malformed feet",
    ):
        assert expected in negatives, f"{expected!r} is not in the negative list"


def test_a_chip_that_IS_the_exposure_is_dropped_not_restored() -> None:
    """The hole the audit found, and it was mine.

    cover_recipe_text returns None when a value is exposure and nothing else -
    "bikini", "encaje", "toalla". The call site read
    `cover_recipe_text(value) or value`, written to avoid an empty clause, and
    that fallback put the original straight back: the positive prompt said
    "wearing bikini" while the negative said "bikini".

    A test asserted the rewrite worked and passed, because it tested the
    function and not the call site.
    """
    from app.compile.compiler import cover_recipe_text

    for value in ("bikini", "conjunto de encaje", "toalla"):
        assert cover_recipe_text(value) is None
        # ...and the compiler must therefore emit no garment clause at all.
    import inspect

    from app.compile.compiler import PromptCompiler

    source = inspect.getsource(PromptCompiler)
    assert "cover_recipe_text(value) or value" not in source, (
        "the fallback that reinstates dropped exposure is back"
    )


def test_fabric_is_covered_too() -> None:
    """Fabric was the one garment field the rewrite never touched, so
    "encaje transparente" reached the same prompt as "Opaque fabric only"."""
    from app.compile.compiler import cover_recipe_text

    assert cover_recipe_text("encaje transparente") is None
    assert cover_recipe_text("lana gruesa") == "lana gruesa"


def test_negatives_reach_a_provider_that_cannot_take_them() -> None:
    """@cf/black-forest-labs/flux-2-klein-4b has NO negative_prompt parameter -
    its documented inputs are prompt, input_image_0..3, guidance, width,
    height, seed.

    So the entire coverage and anatomy negative list was silently dropped on
    every preview. The compiler was doing its job; the words never left the
    building. They are now folded into the positive prompt, which is the only
    channel the model has.
    """
    from app.providers.cloudflare import _condense

    folded = _condense(
        "extra fingers, fused fingers, bare shoulders, cleavage, extra fingers"
    )
    assert "fused fingers" in folded
    assert "cleavage" in folded
    assert folded.count("extra fingers") == 1, "duplicates waste the budget"
