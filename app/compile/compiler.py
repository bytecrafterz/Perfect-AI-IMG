"""The prompt compiler.

Where the claim "better prompts than she could write by hand" is actually
cashed.  Not because a model is clever, but because a photographer wrote the
recipe once, the identity description is measured rather than remembered, and
the locks are stated explicitly every single time instead of when someone
happens to think of it.

Input:  LookRecipe + AttributeIR + Slot + dialect
Output: GenerationRequest, already in the provider's dialect

The compiler is the ONLY place that turns structure into words.  Adapters map
fields onto HTTP bodies and nothing else.
"""

from __future__ import annotations

from app.compile.combinations import Slot
from app.contracts.attribute_ir import AttributeIR
from app.contracts.common import ALWAYS_LOCKED, Attribute
from app.contracts.look_recipe import LookRecipe
from app.contracts.provider import GenerationRequest, PromptDialect

# ---------------------------------------------------------------------------
# Identity preservation
#
# These phrases exist because of a specific complaint: an earlier tool made
# her look slimmer without being asked.  The generator is told, on every
# single call, that the body is not its to adjust.  The gate then MEASURES
# whether it complied - the words are the request, the proportion check is
# the enforcement.  Neither is sufficient alone.
# ---------------------------------------------------------------------------

_LOCK_PHRASES: dict[Attribute, str] = {
    Attribute.FACE: (
        "preserve the exact facial identity, bone structure, jawline width, "
        "eye shape and spacing, nose and mouth of the person in the source photo"
    ),
    Attribute.BODY_PROPORTIONS: (
        "preserve the exact body proportions, build, shoulder and hip width, "
        "waist and limb thickness of the source photo without slimming, "
        "reshaping, lengthening or idealising the body in any way"
    ),
    Attribute.SKIN_TONE: (
        "preserve the exact skin tone, undertone and natural skin texture "
        "including pores, moles and marks"
    ),
    Attribute.HAIR: (
        "preserve the exact hair colour, length, texture and parting"
    ),
}

# ---------------------------------------------------------------------------
# Coverage policy
#
# When enabled: closed neckline, covered shoulders, arms, midriff and back,
# legs covered to the ankle, nothing sheer.
#
# SWITCHABLE, via COVERAGE_POLICY in .env. It is on during the POC because
# testing is happening under a restricted brief; the intention is that the
# subject eventually gets the full styling range she originally asked for.
#
# While it IS on it behaves like identity rather than like a style hint:
# stated on every single call AND verified afterwards by the judge. A
# generator asked politely for a high neckline produces one most of the time,
# and "most of the time" is not a standard anyone can deliver work against.
#
# While on, it overrides look and chip alike - a recipe asking for bare
# shoulders is neutralised rather than obeyed. Turn it off and those same
# recipes render exactly as authored, with nothing to unpick.
# ---------------------------------------------------------------------------

COVERAGE_CLAUSE = (
    "modest full coverage: a high closed neckline covering the collarbones and "
    "throat, sleeves covering the shoulders and upper arms, the torso and "
    "midriff fully covered, and legs completely covered to the ankle by "
    "trousers or a full-length skirt or dress. No cleavage, no bare shoulders, "
    "no bare back, no exposed midriff, no bare legs, no sheer or "
    "see-through fabric"
)

#: Negatives specific to the coverage policy. Separated from the general list
#: so the policy can be audited in one place rather than grepped for.
_COVERAGE_NEGATIVES: tuple[str, ...] = (
    "cleavage",
    "low neckline",
    "plunging neckline",
    "bare shoulders",
    "off-shoulder",
    "spaghetti straps",
    "strapless",
    "sleeveless",
    "bare back",
    "open back",
    "backless",
    "exposed midriff",
    "crop top",
    "bare legs",
    "short skirt",
    "mini skirt",
    "shorts",
    "swimwear",
    "bikini",
    "lingerie",
    "underwear",
    "sheer fabric",
    "see-through clothing",
    "tight revealing clothing",
    "nudity",
    "partial nudity",
)

#: Applied to every request.  The first three are the beautification defaults
#: that most pipelines apply silently and that she explicitly does not want.
_BASE_NEGATIVES: tuple[str, ...] = (
    "beautification",
    "face slimming",
    "body slimming",
    "airbrushed skin",
    "plastic skin",
    "waxy skin",
    "enlarged eyes",
    "altered facial structure",
    "extra fingers",
    "missing fingers",
    "fused fingers",
    "malformed hands",
    "extra limbs",
    "distorted anatomy",
    "watermark",
    "text overlay",
    "low resolution",
    "blurry",
)

_ATTRIBUTE_PHRASING: dict[Attribute, str] = {
    Attribute.GARMENT: "wearing {value}",
    Attribute.GARMENT_COLOR: "in {value}",
    Attribute.HAIR: "hair {value}",
    Attribute.GESTURE: "{value}",
    Attribute.EXPRESSION: "expression {value}",
    Attribute.SCENE: "in {value}",
    Attribute.LIGHT: "lit by {value}",
    Attribute.FRAMING: "{value} shot",
}

#: Values meaning "leave this as it is in the source photo".  They must not
#: reach the prompt as literal words, or the generator is told to render the
#: phrase "como esta".
_PASSTHROUGH_VALUES = {"como esta", "como está", "el mio", "el mío", "la del look", "la del estilo"}


def _is_passthrough(value: str) -> bool:
    return value.strip().lower() in _PASSTHROUGH_VALUES


class CompiledPrompt:
    """The rendered request plus the reasoning behind it.

    ``rationale`` is not decoration: it is what the transparency card shows
    when she taps "ver detalle", and what makes a bad session debuggable
    without re-running it.
    """

    def __init__(
        self,
        request: GenerationRequest,
        *,
        rationale: dict[str, object],
    ) -> None:
        self.request = request
        self.rationale = rationale


class PromptCompiler:
    def __init__(
        self,
        *,
        mined_negatives: tuple[str, ...] = (),
        enforce_coverage: bool | None = None,
    ) -> None:
        #: Defects the gate has seen repeatedly become permanent negatives.
        #: This is one of the three real learning mechanisms - no training,
        #: just accumulated evidence about what goes wrong for her.
        self._mined_negatives = mined_negatives

        # Defaults to the configured policy, but injectable so a test can
        # exercise both states without touching the environment.
        if enforce_coverage is None:
            from app.config import settings

            enforce_coverage = settings.coverage_enforced
        self._enforce_coverage = enforce_coverage

    @property
    def enforces_coverage(self) -> bool:
        return self._enforce_coverage

    # -- public ------------------------------------------------------------

    def compile(
        self,
        *,
        look: LookRecipe | None,
        ir: AttributeIR,
        slot: Slot,
        dialect: PromptDialect,
        width: int,
        height: int,
        source_image_path: str | None = None,
        for_final: bool = False,
    ) -> CompiledPrompt:
        subject = self._subject_clause(ir)
        scene = self._scene_clauses(look, slot)
        locks = self._lock_clauses(ir)
        quality = self._quality_clause(look, for_final=for_final)

        if dialect is PromptDialect.TAG_WEIGHTED:
            prompt = self._render_tags(subject, scene, locks, quality)
        elif dialect is PromptDialect.INSTRUCTIONAL:
            prompt = self._render_instructional(subject, scene, locks, quality, ir)
        else:
            prompt = self._render_natural(subject, scene, locks, quality)

        negative = self._render_negative(dialect)
        params = self._parameters(look, for_final=for_final)

        request = GenerationRequest(
            prompt=prompt,
            negative_prompt=negative,
            source_image_path=source_image_path,
            width=width,
            height=height,
            seed=slot.seed,
            steps=params.get("steps"),
            guidance=params.get("guidance"),
            strength=params.get("strength"),
            extra={k: v for k, v in params.items() if k not in {"steps", "guidance", "strength"}},
        )

        return CompiledPrompt(
            request,
            rationale={
                "look": look.id if look else None,
                "slot": slot.describe(),
                "constrained": {a.value: v for a, v in slot.constrained.items()},
                "free": {a.value: v for a, v in slot.free.items()},
                "locks": [a.value for a in ir.locks],
                "dialect": dialect.value,
                "parameters": params,
                "learned": bool(look and look.learned_params),
                # Recorded per image so it is always possible to tell which
                # regime a result was produced under - useful when comparing
                # test output against later, unrestricted work.
                "coverage_enforced": self._enforce_coverage,
            },
        )

    # -- clause building ---------------------------------------------------

    def _subject_clause(self, ir: AttributeIR) -> str:
        """Describe her from measurement, not from memory.

        Assembled from the profile the system built in Stage 1, so it is the
        same description on every call - which is precisely the consistency a
        person writing prompts by hand cannot maintain.
        """
        bits: list[str] = ["a photograph of the same woman from the source photo"]
        face = ir.subject.face
        details = [
            face.shape and f"{face.shape} face",
            face.jaw and f"{face.jaw} jawline",
            face.eyes_color and f"{face.eyes_color} eyes",
            face.eyes_shape and f"{face.eyes_shape} eye shape",
            face.nose and f"{face.nose} nose",
            face.lips and f"{face.lips} lips",
        ]
        hair = ir.subject.hair
        details += [
            hair.color and f"{hair.color} hair",
            hair.length and f"{hair.length} length",
            hair.texture and f"{hair.texture} texture",
        ]
        if ir.subject.build:
            details.append(f"{ir.subject.build} build")
        kept = [d for d in details if d]
        if kept:
            bits.append(", ".join(kept))
        return ", ".join(bits)

    def _scene_clauses(self, look: LookRecipe | None, slot: Slot) -> list[str]:
        """What she asked for, plus what the recipe supplies underneath."""
        clauses: list[str] = []
        for attribute, value in sorted(
            slot.values.items(), key=lambda kv: kv[0].value
        ):
            if _is_passthrough(value):
                continue
            template = _ATTRIBUTE_PHRASING.get(attribute)
            if template:
                clauses.append(template.format(value=value))

        if look:
            recipe = look.recipe
            chosen = slot.values
            if recipe.scene and Attribute.SCENE not in chosen:
                place = recipe.scene.place
                if recipe.scene.time:
                    place = f"{place}, {recipe.scene.time}"
                clauses.append(f"in {place}")
            if recipe.scene and recipe.scene.depth:
                clauses.append(recipe.scene.depth)
            if recipe.lighting and Attribute.LIGHT not in chosen:
                lighting = recipe.lighting.key
                if recipe.lighting.fill:
                    lighting = f"{lighting} with {recipe.lighting.fill}"
                clauses.append(f"lit by {lighting}")

            if recipe.garment:
                # The garment TYPE and DETAILS were previously dropped unless
                # she happened to tap a garment chip - only the fabric ever
                # reached the prompt. That silently discarded most of what a
                # look actually specifies: "vestido largo, tirantes finos,
                # espalda descubierta" arrived as nothing but "satinado".
                #
                # Same rule as scene and lighting: the recipe supplies it when
                # she has not chosen for herself, and her choice wins when she
                # has.
                if Attribute.GARMENT not in chosen:
                    clauses.append(f"wearing {recipe.garment.type}")
                if recipe.garment.details:
                    clauses.append(recipe.garment.details)
                if recipe.garment.fabric:
                    clauses.append(f"{recipe.garment.fabric} fabric")
            cam = recipe.camera
            clauses.append(
                f"shot on a {cam.focal_mm}mm lens at {cam.aperture}, "
                f"camera at {cam.height} height"
            )
        return clauses

    def _lock_clauses(self, ir: AttributeIR) -> list[str]:
        # While the policy is on, coverage leads and does NOT come from
        # ir.locks - so it cannot be dropped by editing an IR, a look or a
        # chip. Turning the policy off is the only way to remove it, and that
        # is a deliberate act in .env rather than a side effect of styling.
        clauses = [COVERAGE_CLAUSE] if self._enforce_coverage else []
        return clauses + [
            _LOCK_PHRASES[a] for a in ir.locks if a in _LOCK_PHRASES
        ]

    def _quality_clause(self, look: LookRecipe | None, *, for_final: bool) -> str:
        base = (
            "photorealistic, natural skin texture, realistic lighting, "
            "sharp focus, professional photography"
        )
        return base + (", high detail" if for_final else "")

    # -- dialect rendering -------------------------------------------------

    def _render_natural(
        self, subject: str, scene: list[str], locks: list[str], quality: str
    ) -> str:
        parts = [subject]
        if scene:
            parts.append(", ".join(scene))
        parts.append(quality)
        if locks:
            parts.append("Critically: " + "; ".join(locks) + ".")
        return ". ".join(p.rstrip(". ") for p in parts if p) + "."

    def _render_tags(
        self, subject: str, scene: list[str], locks: list[str], quality: str
    ) -> str:
        tags = [subject, *scene, quality]
        # Weighted syntax is how tag-dialect models express priority; identity
        # gets the highest weight because it is the only thing that cannot be
        # fixed afterwards.
        tags += [f"({lock}:1.4)" for lock in locks]
        return ", ".join(t.strip().rstrip(".") for t in tags if t)

    def _render_instructional(
        self,
        subject: str,
        scene: list[str],
        locks: list[str],
        quality: str,
        ir: AttributeIR,
    ) -> str:
        """For edit models, which respond to "change X, keep Y" rather than to
        a description of the finished image."""
        changes = "; ".join(scene) if scene else "improve the lighting only"
        keeps = "; ".join(locks)
        return (
            f"Edit this photograph of the same person. Change: {changes}. "
            f"Keep unchanged: {keeps}. "
            f"Do not alter anything not listed under Change. Result should be {quality}."
        )

    def _render_negative(self, dialect: PromptDialect) -> str:
        # Coverage negatives lead when active: some providers weight the head
        # of the negative prompt more heavily, and if anything in this list is
        # going to be honoured it should be these.
        coverage = _COVERAGE_NEGATIVES if self._enforce_coverage else ()
        negatives = [*coverage, *_BASE_NEGATIVES, *self._mined_negatives]
        seen: set[str] = set()
        unique = [n for n in negatives if not (n in seen or seen.add(n))]
        return ", ".join(unique)

    # -- parameters --------------------------------------------------------

    def _parameters(
        self, look: LookRecipe | None, *, for_final: bool
    ) -> dict[str, object]:
        """Defaults, overlaid with whatever this look has learned.

        ``learned_params`` are promoted from the settings behind images she
        actually kept, which is the second of the three learning mechanisms.
        """
        params: dict[str, object] = {
            "steps": 30 if for_final else 12,
            "guidance": 4.0,
            # Low denoise on an edit: the source photo is where her real
            # proportions come from, so the generator is kept close to it.
            "strength": 0.55 if for_final else 0.6,
        }
        if look and look.learned_params:
            scope = "final" if for_final else "preview"
            params.update(
                {
                    k: v
                    for k, v in look.learned_params.items()
                    if not k.startswith("_") and k not in {"preview", "final"}
                }
            )
            stage_specific = look.learned_params.get(scope)
            if isinstance(stage_specific, dict):
                params.update(stage_specific)
        return params
