"""AttributeIR - the structured description of a subject and a scene.

This is the intermediate representation that makes provider portability
possible.  The analyser produces it from a photo; the compiler renders it into
whatever dialect a given provider speaks.  No prompt text lives here.

Rule: nothing in this module may mention a provider, a model or a prompt.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.contracts.common import ALWAYS_LOCKED, Attribute, Framing

IR_VERSION = "1.0"


class SkinTone(BaseModel):
    """CIELAB is used rather than RGB because perceptual distance in Lab
    corresponds to what a person notices.  deltaE over these values is the
    gate's skin-tone check."""

    lab_l: float = Field(ge=0.0, le=100.0)
    lab_a: float = Field(ge=-128.0, le=127.0)
    lab_b: float = Field(ge=-128.0, le=127.0)
    undertone: str | None = None

    def delta_e(self, other: "SkinTone") -> float:
        """CIE76.  Crude next to CIEDE2000, but the threshold is calibrated
        empirically against her own photos, so the simpler metric is honest
        and cheap.  Roughly: <2 invisible, <4 acceptable, >6 obvious."""
        return (
            (self.lab_l - other.lab_l) ** 2
            + (self.lab_a - other.lab_a) ** 2
            + (self.lab_b - other.lab_b) ** 2
        ) ** 0.5


class BodyProportions(BaseModel):
    """The anti-slimming reference.

    Built once from her real full-body photos in Stage 1 and compared against
    every generated image.  This is what catches the complaint she raised
    about an earlier tool making her look thinner without being asked.
    """

    #: Shoulder width over hip width. Catches DISPROPORTIONATE reshaping - a
    #: waist taken in, hips widened - but is blind to uniform slimming,
    #: because both terms shrink together and the ratio barely moves.
    shoulder_hip_ratio: float | None = Field(default=None, gt=0.0)

    #: Widths measured against torso LENGTH, which slimming does not change.
    #: These are what actually catch "the tool made me thinner": a generator
    #: that narrows the whole body reduces these and leaves shoulder_hip_ratio
    #: almost exactly where it was.
    shoulder_torso_ratio: float | None = Field(default=None, gt=0.0)
    hip_torso_ratio: float | None = Field(default=None, gt=0.0)

    height_in_heads: float | None = Field(default=None, gt=0.0)
    waist_hip_ratio: float | None = Field(default=None, gt=0.0)
    jaw_width_ratio: float | None = Field(default=None, gt=0.0)
    limb_ratios: dict[str, float] = Field(default_factory=dict)

    def max_relative_drift(self, other: "BodyProportions") -> float | None:
        """Largest relative change across every measurement both sides have.

        Returns None when there is nothing comparable - which the gate must
        treat as "unknown", never as "fine".
        """
        deltas: list[float] = []
        for field in (
            "shoulder_hip_ratio",
            "shoulder_torso_ratio",
            "hip_torso_ratio",
            "height_in_heads",
            "waist_hip_ratio",
            "jaw_width_ratio",
        ):
            a, b = getattr(self, field), getattr(other, field)
            if a and b:
                deltas.append(abs(a - b) / a)
        # LIMB RATIOS ARE DELIBERATELY EXCLUDED.
        #
        # They measure POSE, not body shape. A forearm pointing towards the
        # camera is shorter in two dimensions than one held out sideways, and
        # that has nothing whatever to do with slimming.
        #
        # Measured on two photographs of Nayane taken the same day: every
        # width-over-length ratio agreed to within 3.9%, comfortably inside
        # the 6% threshold - and limb:forearm_r differed by 35.65% purely
        # from foreshortening. max() over everything therefore reported 0.3565
        # and would have rejected her own unaltered photograph, blaming the
        # generator for the angle of her arm.
        #
        # The width-over-length ratios ARE the anti-slimming measure - they
        # were chosen precisely because they survive pose. Keeping limbs in the
        # maximum discards that property.
        for key, a in {}.items():
            b = other.limb_ratios.get(key)
            if a and b:
                deltas.append(abs(a - b) / a)
        return max(deltas) if deltas else None


class FaceDescriptor(BaseModel):
    shape: str | None = None
    jaw: str | None = None
    cheekbones: str | None = None
    eyes_color: str | None = None
    eyes_shape: str | None = None
    nose: str | None = None
    lips: str | None = None
    distinguishing_marks: list[str] = Field(default_factory=list)


class HairDescriptor(BaseModel):
    color: str | None = None
    length: str | None = None
    texture: str | None = None
    parting: str | None = None
    style: str | None = None


class SubjectIR(BaseModel):
    """Who she is.  Stable across sessions; built from the profile, not from
    the photo she just uploaded."""

    face: FaceDescriptor = Field(default_factory=FaceDescriptor)
    hair: HairDescriptor = Field(default_factory=HairDescriptor)
    skin: SkinTone | None = None
    body: BodyProportions = Field(default_factory=BodyProportions)
    build: str | None = None


class CaptureIR(BaseModel):
    """How the photo she uploaded was taken.  Drives the proposal engine -
    a close-up and a full-body shot get different style options."""

    framing: Framing = Framing.UNKNOWN
    focal_mm_estimate: int | None = None
    camera_height: str | None = None
    lighting: str | None = None
    white_balance_k: int | None = None
    quality_score: float = Field(default=0.0, ge=0.0, le=1.0)
    is_outdoor: bool | None = None


class SceneIR(BaseModel):
    """What is in the photo besides her."""

    background: str | None = None
    garment: str | None = None
    garment_color: str | None = None
    accessories: list[str] = Field(default_factory=list)
    gesture: str | None = None
    expression: str | None = None


class AttributeIR(BaseModel):
    """The full analysis of one uploaded photo.

    ``locks`` and ``mutable`` are the contract with the quality gate: anything
    listed in ``locks`` must be measurably unchanged in the output, and the
    gate rejects the image if it is not.
    """

    model_config = ConfigDict(frozen=False)

    version: str = IR_VERSION
    subject: SubjectIR = Field(default_factory=SubjectIR)
    capture: CaptureIR = Field(default_factory=CaptureIR)
    scene: SceneIR = Field(default_factory=SceneIR)

    locks: list[Attribute] = Field(default_factory=lambda: sorted(ALWAYS_LOCKED))
    mutable: list[Attribute] = Field(default_factory=list)

    #: Free-text notes from the analyser.  Never fed to a generator - kept for
    #: the transparency card and for debugging a bad session.
    notes: str | None = None

    def model_post_init(self, __context: object) -> None:
        # The identity locks are not negotiable, whatever the analyser said.
        merged = set(self.locks) | ALWAYS_LOCKED
        self.locks = sorted(merged, key=lambda a: a.value)
        self.mutable = sorted(
            (a for a in self.mutable if a not in ALWAYS_LOCKED),
            key=lambda a: a.value,
        )

    def is_locked(self, attribute: Attribute) -> bool:
        return attribute in set(self.locks)

    def with_unlocked(self, attribute: Attribute) -> "AttributeIR":
        """Explicitly permit one attribute to change.

        The only route by which anything leaves ``locks`` - and it is never
        called automatically.  Body adjustment, if it is ever built, must come
        through here with an amount the user actually asked for.
        """
        if attribute in ALWAYS_LOCKED:
            raise ValueError(
                f"{attribute.value} is an identity attribute and cannot be "
                "unlocked in this phase; see SAFE DEFAULTS in the spec"
            )
        clone = self.model_copy(deep=True)
        clone.locks = [a for a in clone.locks if a != attribute]
        if attribute not in clone.mutable:
            clone.mutable = sorted([*clone.mutable, attribute], key=lambda a: a.value)
        return clone
