"""The visual judge - the one paid check, and only on finals.

Runs on FINALS ONLY.  Previews are screened by the free CPU checks, because
paying a language model to scrutinise an image she may never choose is exactly
the waste the two-stage design exists to remove.

The rubric below is byte-identical on every call, which is what makes prompt
caching worth enabling: the cached prefix is read rather than re-billed.

What the judge is for, and what it is NOT for:

  FOR      "does this image satisfy what was asked, and what is visibly wrong
            with it, and where" - semantic questions no pixel metric answers
  NOT FOR  identity, proportions, skin tone.  Those are measured numerically
            by the gate, because a model asked "is this the same person?"
            will agree far too readily, and identity is the whole product.
"""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path

from pydantic import BaseModel, Field

from app.contracts.common import BBox
from app.contracts.qa_report import Check, CheckOutcome, Defect, DefectKind

#: Approximate cost of one judgement: ~4k input tokens (two images plus the
#: rubric) and ~500 output, at Haiku 4.5's $1/$5 per MTok.
JUDGE_COST_USD = 0.0065

_RUBRIC = """You are the final quality inspector for a professional photo production system.

You will see a GENERATED image, and usually the SOURCE photograph it was derived from.
Your job is to find what is visibly wrong with the generated image, and to say whether
it satisfies the request.

Report a defect ONLY if a professional retoucher would refuse to deliver the image.
Be strict about anatomy and sloppy about taste: you are not judging whether the photo
is beautiful, you are judging whether it is broken.

Look hard at, in this order:

1. HANDS - finger count, fused or missing fingers, impossible joints. This is the most
   common failure and the most important thing you check.
2. LIMBS - extra or missing arms and legs, impossible bends, limbs merging into objects.
3. ARTEFACTS - warped straight lines, smeared textures, duplicated features, garbled
   text on signs or clothing.
4. BACKGROUND - objects that melt into each other, impossible geometry.
5. REQUEST SATISFACTION - does the image actually show what was asked for?

Do NOT judge these, they are measured numerically elsewhere and your opinion on them
is not wanted:
- whether it is the same person
- body proportions, weight or build
- skin tone

For every defect give a normalised bounding box with values between 0 and 1, where
(0,0) is the top-left corner. Be tight: the box is used to repaint that region only,
so an oversized box repaints things that were fine.

If the image is clean, return an empty defect list and satisfies_request=true."""


class JudgedDefect(BaseModel):
    kind: str = Field(description="one of: hand, limb, artefact, text, background, other")
    x0: float = Field(ge=0.0, le=1.0)
    y0: float = Field(ge=0.0, le=1.0)
    x1: float = Field(ge=0.0, le=1.0)
    y1: float = Field(ge=0.0, le=1.0)
    severity: float = Field(ge=0.0, le=1.0, description="1.0 = undeliverable")
    detail: str = Field(description="what is wrong, in one short phrase")


class Judgement(BaseModel):
    coverage_ok: bool = Field(
        description="modest full coverage as defined in the rubric; false if any "
        "part of the policy is visibly broken"
    )
    coverage_detail: str = Field(
        default="", description="what is exposed, if anything. Spanish."
    )
    satisfies_request: bool
    defects: list[JudgedDefect] = Field(default_factory=list)
    overall_quality: float = Field(
        ge=0.0, le=1.0, description="technical execution, not taste"
    )
    summary: str = Field(description="one sentence, Spanish")


_KIND_MAP = {
    "hand": DefectKind.HAND,
    "limb": DefectKind.LIMB,
    "artefact": DefectKind.ARTEFACT,
    "artifact": DefectKind.ARTEFACT,
    "text": DefectKind.TEXT,
    "background": DefectKind.BACKGROUND,
    "face": DefectKind.FACE,
}


def _image_block(path: str | Path) -> dict:
    path = Path(path)
    media_type = mimetypes.guess_type(path.name)[0] or "image/png"
    data = base64.standard_b64encode(path.read_bytes()).decode("ascii")
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": data},
    }


#: The coverage section of the rubric. Injected only while the policy is on,
#: so that turning it off does not leave the judge quietly failing images for
#: a rule nobody is applying any more.
_COVERAGE_RUBRIC = """
0. COVERAGE. This subject currently requires modest full coverage, and it is a hard
   requirement rather than a preference. Set coverage_ok=false if ANY of these is true:
     - the neckline is open, low, or shows cleavage or the collarbones
     - the shoulders or upper arms are bare
     - the back is bare or the garment is open-backed
     - the midriff or waist is exposed
     - the legs are bare anywhere below the hem, or the hem sits above the ankle
     - the fabric is sheer or see-through anywhere over the body
   Judge only what you can see. If the lower body is out of frame, do not fail it for
   legs you cannot assess - say so in the summary instead. Be strict about what IS
   visible: an image that fails coverage cannot be delivered no matter how good it
   otherwise looks.
"""


class VisualJudge:
    """Wraps one Claude call.  Import of the SDK is lazy so the app boots, and
    the whole pipeline runs on the mock provider, without anthropic installed."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "claude-haiku-4-5",
        enforce_coverage: bool | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._client = None

        if enforce_coverage is None:
            from app.config import settings

            enforce_coverage = settings.coverage_enforced
        self._enforce_coverage = enforce_coverage

    @property
    def rubric(self) -> str:
        """The rubric actually sent.

        Assembled rather than stored so that the cached prefix stays
        byte-identical for a given policy - the caching only pays off if the
        text never wobbles between calls.
        """
        if not self._enforce_coverage:
            return _RUBRIC
        return _RUBRIC.replace(
            "Look hard at, in this order:\n",
            "Look hard at, in this order:\n" + _COVERAGE_RUBRIC,
        )

    def _ensure_client(self):
        if self._client is None:
            import anthropic  # lazy: not needed unless a final is judged

            self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client

    def evaluate(
        self,
        *,
        image_path: str | Path,
        source_path: str | Path | None = None,
        request_summary: str = "",
    ) -> tuple[Check, list[Defect]]:
        """Returns a Check plus any defects, with boxes ready for repair.

        A judge that errors returns UNKNOWN, which blocks the image in strict
        mode.  That is deliberate: if the last inspection could not run, the
        image has not been inspected.
        """
        try:
            client = self._ensure_client()
        except Exception as exc:  # noqa: BLE001 - SDK missing is a real state
            return (
                Check(
                    name="judge",
                    outcome=CheckOutcome.UNKNOWN,
                    detail=f"juez no disponible: {exc}",
                ),
                [],
            )

        content: list[dict] = []
        if source_path is not None:
            content.append({"type": "text", "text": "SOURCE photograph:"})
            content.append(_image_block(source_path))
        content.append({"type": "text", "text": "GENERATED image:"})
        content.append(_image_block(image_path))
        content.append(
            {
                "type": "text",
                "text": f"What was requested: {request_summary or 'no description supplied'}",
            }
        )

        try:
            response = client.messages.parse(
                model=self._model,
                max_tokens=2048,
                system=[
                    {
                        "type": "text",
                        "text": self.rubric,
                        # The rubric never changes, so it is a stable cache
                        # prefix.  Caching only engages above the minimum
                        # cacheable length; below that this is a harmless no-op.
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": content}],
                output_format=Judgement,
            )
            judgement: Judgement = response.parsed_output
        except Exception as exc:  # noqa: BLE001
            return (
                Check(
                    name="judge",
                    outcome=CheckOutcome.UNKNOWN,
                    detail=f"el juez fallo: {exc}",
                    cost_usd=0.0,
                ),
                [],
            )

        defects: list[Defect] = []
        for found in judgement.defects:
            try:
                bbox = BBox(x0=found.x0, y0=found.y0, x1=found.x1, y1=found.y1)
            except Exception:  # noqa: BLE001 - a degenerate box is not fatal
                bbox = None
            defects.append(
                Defect(
                    kind=_KIND_MAP.get(found.kind.lower().strip(), DefectKind.OTHER),
                    bbox=bbox,
                    severity=found.severity,
                    detail=found.detail,
                    source="judge",
                )
            )

        # Coverage is not one signal among several while the policy is on: a
        # breach makes the image undeliverable regardless of how good it
        # otherwise is. It is recorded as its own defect and is NOT repairable
        # - inpainting a neckline reworks the body, which is the one thing
        # that must not move.
        #
        # Guarded on the policy so that, once it is switched off, a model that
        # volunteers coverage_ok=false out of habit cannot start silently
        # discarding perfectly acceptable images.
        if self._enforce_coverage and not judgement.coverage_ok:
            defects.append(
                Defect(
                    kind=DefectKind.OTHER,
                    bbox=None,
                    severity=1.0,
                    detail=(
                        "no cumple la cobertura requerida: "
                        f"{judgement.coverage_detail or 'zona expuesta'}"
                    ),
                    source="judge.coverage",
                )
            )

        coverage_failed = self._enforce_coverage and not judgement.coverage_ok
        satisfied = judgement.satisfies_request and not defects and not coverage_failed
        return (
            Check(
                name="judge",
                outcome=CheckOutcome.PASS if satisfied else CheckOutcome.FAIL,
                value=judgement.overall_quality,
                threshold=0.5,
                detail=(
                    f"COBERTURA: {judgement.coverage_detail}"
                    if coverage_failed
                    else judgement.summary
                ),
                cost_usd=JUDGE_COST_USD,
            ),
            defects,
        )
