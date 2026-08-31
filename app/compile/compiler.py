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

#: Recipe wording that describes exposure, and what it becomes when the
#: coverage policy is on.
#:
#: A negative prompt is not enough on its own. The catalog is written in
#: Spanish and describes real garments, so "vestido largo, tirantes finos,
#: espalda descubierta" went into the SAME prompt as "no bare back, no bare
#: shoulders". The model was handed a contradiction and resolved it however it
#: liked - which is why exposure kept appearing while the policy was active
#: and every audit said the policy was on.
#:
#: Substitution rather than deletion where a covered equivalent exists: an
#: evening dress still needs sleeves described, or the model invents them.
_COVERED_SUBSTITUTIONS: dict[str, str] = {
    "cuello abierto": "cuello alto cerrado",
    "tirantes finos": "manga larga",
    "tirantes": "manga larga",
    "espalda descubierta": "espalda completamente cubierta",
    "hombros descubiertos": "hombros cubiertos",
    "sin mangas": "manga larga",
    "palabra de honor": "cuello alto con manga larga",
    "escote pronunciado": "cuello alto",
    "escote": "cuello alto cerrado",
    # A chip she can tap. Mild, but "open" over a covered torso still invites
    # the model to show what is underneath.
    "abrigo abierto": "abrigo cerrado y abrochado",
    "camisa abierta": "camisa abrochada hasta el cuello",
    # English too. The catalog is written in Spanish, but nothing ENFORCES
    # that - a look or a chip authored in English would otherwise walk
    # straight past a Spanish-only filter, which is the quietest way for this
    # policy to stop working while every test still says it is on.
    "strapless": "high collar with long sleeves",
    "spaghetti straps": "long sleeves",
    "off-shoulder": "covered shoulders",
    "off the shoulder": "covered shoulders",
    "bare shoulders": "covered shoulders",
    "open back": "fully covered back",
    "backless": "fully covered back",
    "bare back": "fully covered back",
    "low neckline": "high closed collar",
    "plunging neckline": "high closed collar",
    "deep v-neck": "high closed collar",
    "halter": "high closed collar with long sleeves",
    "sleeveless": "long sleeves",
    "open collar": "collar fastened to the throat",
    "unbuttoned": "fully buttoned",
    "mini dress": "floor-length dress",
    "mini skirt": "floor-length skirt",
    "short skirt": "floor-length skirt",
    "shorts": "full-length trousers",
}

#: Fragments with no covered equivalent - the garment IS the exposure, so the
#: fragment is dropped rather than rewritten.
_EXPOSING_FRAGMENTS: tuple[str, ...] = (
    # Spanish - how the catalog is written today
    "transparente", "translucido", "semitransparente",
    "abertura", "crop", "ombligo", "descubierto", "descubierta",
    "al aire", "encaje", "lenceria", "bikini", "toalla",
    # English - because nothing enforces the catalog's language
    "sheer", "see-through", "see through", "transparent", "mesh",
    "crop top", "midriff", "cleavage", "slit", "lingerie", "swimsuit",
    "bikini", "underwear", "nightgown", "towel", "bathrobe", "lace",
    "bare legs", "bare chest", "bare neck", "topless", "nude",
)


def cover_recipe_text(text: str | None) -> str | None:
    """Rewrite one recipe fragment so it cannot ask for exposure.

    Applied to garment type, details and chips whenever the coverage policy is
    enforced. Comma-separated because that is how the catalog writes details,
    and dropping one clause must not take the rest of the sentence with it.
    """
    if not text:
        return text
    kept: list[str] = []
    for fragment in text.split(","):
        piece = fragment.strip()
        if not piece:
            continue
        lowered = piece.lower()
        replaced = None
        for phrase, covered in _COVERED_SUBSTITUTIONS.items():
            if phrase in lowered:
                replaced = covered
                break
        if replaced is not None:
            if replaced not in kept:
                kept.append(replaced)
            continue
        if any(bad in lowered for bad in _EXPOSING_FRAGMENTS):
            continue  # no covered version of this exists - drop it
        kept.append(piece)
    return ", ".join(kept) if kept else None


#: Said positively, in every prompt. A negative list can only steer away from
#: a failure; it cannot describe the thing that should be there instead, and
#: samplers respond better to a description than to a prohibition.
ANATOMY_CLAUSE = (
    "anatomically correct hands with exactly five fingers on each hand, "
    "fingers clearly separated and naturally posed; both legs complete and "
    "correctly proportioned, knees and ankles natural, feet "
    "fully within the frame or cleanly outside it"
)

COVERAGE_CLAUSE = (
    "modest full coverage, strictly enforced: a high closed collar fastened to "
    "the base of the throat, covering the neck, collarbones and chest "
    "completely; long sleeves to the wrist covering both shoulders and arms; "
    "the back, torso and midriff fully covered with no opening of any kind; "
    "legs completely covered to the ankle by full-length trousers or a "
    "floor-length skirt or dress. Opaque fabric only. No cleavage, no open "
    "collar, no bare neck, no bare shoulders, no bare arms, no bare back, no "
    "exposed midriff, no bare legs, no slit, no sheer or see-through fabric"
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
    # The three the client named explicitly: neck, body, legs.
    "open collar",
    "unbuttoned",
    "bare neck",
    "bare chest",
    "deep v-neck",
    "halter neck",
    "thigh slit",
    "high slit",
    "shorts",
    "swimsuit",
    "bikini",
    "lingerie",
    "underwear",
    "nightgown",
    "towel",
    "bathrobe",
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
    # Hands and the ends of limbs, named in detail on purpose. These are
    # where few-step models fail first and hardest, and where the client
    # noticed it - "the fingers and legs are inaccurate". A generic
    # "distorted anatomy" is too vague to steer a sampler away from the
    # specific failures, so the specific failures are listed.
    "extra fingers",
    "missing fingers",
    "fused fingers",
    "webbed fingers",
    "six fingers",
    "deformed hands",
    "malformed hands",
    "mangled hands",
    "twisted wrist",
    "extra arms",
    "extra legs",
    "extra limbs",
    "missing limb",
    "fused legs",
    "bent knee backwards",
    "deformed knee",
    "misshapen legs",
    "disproportionate legs",
    "malformed feet",
    "extra toes",
    "floating limb",
    "disconnected limb",
    "distorted anatomy",
    "anatomically incorrect",
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
                # Her own taps go through the same rewrite as the recipe. A
                # chip like "abrigo abierto" is a value she chose, but under
                # the coverage policy it still contradicts "torso fully
                # covered" - and a contradiction the model resolves at random
                # is exactly the failure the policy exists to prevent.
                if self._enforce_coverage:
                    covered_value = cover_recipe_text(value)
                    if not covered_value:
                        # The rewrite removed everything, which means the
                        # value IS the exposure - "bikini", "encaje",
                        # "toalla". Falling back to the original here (which
                        # this line used to do, to avoid an empty clause) put
                        # it straight back into the prompt: the positive said
                        # "wearing bikini" while the negative said "bikini".
                        # Dropping the clause is the whole point.
                        continue
                    value = covered_value
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
                # Under the coverage policy the recipe's own wording is
                # rewritten before it reaches the prompt, so the positive and
                # the negative cannot contradict each other.
                def covered(value: str | None) -> str | None:
                    return cover_recipe_text(value) if self._enforce_coverage else value

                if Attribute.GARMENT not in chosen:
                    garment_type = covered(recipe.garment.type)
                    if garment_type:
                        clauses.append(f"wearing {garment_type}")
                details = covered(recipe.garment.details)
                if details:
                    clauses.append(details)
                # Fabric goes through the rewrite too. It was the one field
                # that did not, so "encaje transparente y malla" reached the
                # same prompt as "Opaque fabric only".
                fabric = covered(recipe.garment.fabric)
                if fabric:
                    clauses.append(f"{fabric} fabric")
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
        # Anatomy always, coverage only under the policy. The negative list
        # can say what must not appear; only this can say what should.
        clauses = [ANATOMY_CLAUSE]
        if self._enforce_coverage:
            clauses.append(COVERAGE_CLAUSE)
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
