"""QAReport - the verdict on one generated image.

The gate is the ONLY approver of finals.  There is no human behind it, which
is why this contract distinguishes three things that are easy to conflate:

    PASS       measured, and within threshold
    FAIL       measured, and outside threshold
    UNKNOWN    could not be measured

UNKNOWN must never be treated as PASS.  If the CV backend is missing, a model
failed to load, or no face was found, the honest answer is that we do not
know - and an unknown identity check is a reason to discard a candidate, not
to ship it.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from app.contracts.common import Attribute, BBox

REPORT_VERSION = "1.0"


class CheckOutcome(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"


class Verdict(str, Enum):
    ACCEPT = "accept"
    REPAIR = "repair"  # localised defect; inpaint that region only
    DISCARD = "discard"  # unsalvageable; regenerate silently


class DefectKind(str, Enum):
    HAND = "hand"
    FACE = "face"
    LIMB = "limb"
    ARTEFACT = "artefact"
    TEXT = "text"
    BACKGROUND = "background"
    OTHER = "other"

    @property
    def is_repairable(self) -> bool:
        """Whether a localised inpaint can plausibly fix this.

        Faces are deliberately excluded: repainting a face is how identity
        drifts, and identity is the whole product.  A bad face means discard
        and regenerate, never repair.
        """
        return self in {
            DefectKind.HAND,
            DefectKind.LIMB,
            DefectKind.ARTEFACT,
            DefectKind.TEXT,
            DefectKind.BACKGROUND,
        }


class Defect(BaseModel):
    kind: DefectKind
    bbox: BBox | None = None
    severity: float = Field(default=0.5, ge=0.0, le=1.0)
    detail: str = ""
    source: str = "gate"  # which check raised it

    @property
    def is_repairable(self) -> bool:
        # A defect with no location cannot be repaired locally, whatever its
        # kind - there is nothing to mask.
        return self.kind.is_repairable and self.bbox is not None


class Check(BaseModel):
    """One measurement.  ``value`` and ``threshold`` are kept even on PASS so
    the transparency card can show her the numbers."""

    name: str
    outcome: CheckOutcome
    value: float | None = None
    threshold: float | None = None
    detail: str = ""
    attribute: Attribute | None = None
    cost_usd: float = 0.0

    @property
    def failed(self) -> bool:
        return self.outcome is CheckOutcome.FAIL

    @property
    def unknown(self) -> bool:
        return self.outcome is CheckOutcome.UNKNOWN


class QAReport(BaseModel):
    """The full verdict on one candidate."""

    version: str = REPORT_VERSION
    image_id: str
    stage: str = Field(description="'preview' or 'final'")
    checks: list[Check] = Field(default_factory=list)
    defects: list[Defect] = Field(default_factory=list)
    verdict: Verdict = Verdict.DISCARD
    score: float = Field(default=0.0, ge=0.0, le=1.0)
    cost_usd: float = 0.0
    elapsed_s: float = 0.0
    notes: str = ""

    # -- derived views ------------------------------------------------------

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.failed]

    @property
    def unknowns(self) -> list[Check]:
        return [c for c in self.checks if c.unknown]

    @property
    def repairable_defects(self) -> list[Defect]:
        return [d for d in self.defects if d.is_repairable]

    @property
    def blocking_defects(self) -> list[Defect]:
        return [d for d in self.defects if not d.is_repairable]

    def check(self, name: str) -> Check | None:
        for c in self.checks:
            if c.name == name:
                return c
        return None

    def decide(self) -> Verdict:
        """Turn measurements into an action.

        Order matters, and it is deliberately conservative:
          1. anything unmeasurable  -> discard (we do not ship what we cannot check)
          2. any hard failure       -> discard
          3. only repairable defects-> repair
          4. otherwise              -> accept
        """
        if self.unknowns:
            self.verdict = Verdict.DISCARD
            self.notes = (
                "no medible: " + ", ".join(c.name for c in self.unknowns)
            )
        elif self.failures:
            self.verdict = Verdict.DISCARD
            self.notes = "fallo: " + ", ".join(c.name for c in self.failures)
        elif self.blocking_defects:
            self.verdict = Verdict.DISCARD
            self.notes = "defecto no reparable: " + ", ".join(
                d.kind.value for d in self.blocking_defects
            )
        elif self.repairable_defects:
            self.verdict = Verdict.REPAIR
            self.notes = "reparable: " + ", ".join(
                d.kind.value for d in self.repairable_defects
            )
        else:
            self.verdict = Verdict.ACCEPT
            self.notes = "ok"
        return self.verdict

    def compute_score(self, weights: dict[str, float] | None = None) -> float:
        """Composite ranking score, used to order the preview grid.

        Only PASS checks with a numeric value contribute.  A report with
        nothing measurable scores 0, which sorts it last - correct, because we
        do not know anything good about it.
        """
        weights = weights or {}
        total_weight = 0.0
        accumulated = 0.0
        for c in self.checks:
            if c.outcome is not CheckOutcome.PASS or c.value is None:
                continue
            w = weights.get(c.name, 1.0)
            # Normalise against the threshold where there is one; a check that
            # passes comfortably should score above one that scrapes through.
            if c.threshold:
                normalised = min(1.0, max(0.0, c.value / c.threshold))
            else:
                normalised = min(1.0, max(0.0, c.value))
            accumulated += w * normalised
            total_weight += w
        self.score = accumulated / total_weight if total_weight else 0.0
        # A repair, even a successful one, is a small mark against the image
        # when ranking it next to one that needed nothing.
        if self.repairable_defects:
            self.score *= 0.95
        return self.score

    def summary_line(self) -> str:
        """One line for the transparency card, in her language."""
        bits: list[str] = []
        identity = self.check("identity")
        if identity and identity.value is not None:
            bits.append(f"identidad {identity.value:.2f}")
        proportions = self.check("proportions")
        if proportions and proportions.outcome is CheckOutcome.PASS:
            bits.append("proporciones OK")
        skin = self.check("skin_tone")
        if skin and skin.outcome is CheckOutcome.PASS:
            bits.append("piel OK")
        if self.repairable_defects:
            bits.append(
                "corregido: " + ", ".join(d.kind.value for d in self.repairable_defects)
            )
        return " - ".join(bits) if bits else "sin datos"
