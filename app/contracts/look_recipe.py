"""LookRecipe - one catalog entry.

Where the photographer's expertise is stored so she never has to supply any.
A look is a structured recipe, never a text prompt: the compiler renders it
into whatever dialect a provider speaks, which is what keeps providers
swappable.

The catalog is data, not code.  Adding a look is one JSON file in catalog/.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from app.contracts.common import ALWAYS_LOCKED, SELECTABLE, Attribute, Framing
from app.contracts.selections import Selections

RECIPE_VERSION = "1.0"


class GarmentSpec(BaseModel):
    type: str
    fabric: str | None = None
    colors: list[str] = Field(default_factory=list)
    details: str | None = None


class SceneSpec(BaseModel):
    place: str
    time: str | None = None
    depth: str | None = None


class LightingSpec(BaseModel):
    key: str
    fill: str | None = None
    mood: str | None = None


class CameraSpec(BaseModel):
    focal_mm: int = 85
    aperture: str = "f/2.0"
    height: str = "pecho"
    framing: Framing = Framing.MEDIUM


class Recipe(BaseModel):
    garment: GarmentSpec | None = None
    scene: SceneSpec | None = None
    lighting: LightingSpec | None = None
    camera: CameraSpec = Field(default_factory=CameraSpec)
    pose_family: list[str] = Field(default_factory=list)


class AppliesTo(BaseModel):
    """What makes contextual proposal possible.

    Rules out a full-body scene when she uploaded a head-and-shoulders photo.
    This is a hard filter, applied before any ranking.
    """

    framing: list[Framing] = Field(default_factory=list)
    needs_body: bool = False
    replaces_background: bool = False
    min_source_quality: float = Field(default=0.0, ge=0.0, le=1.0)
    requires_outdoor: bool | None = None


class ChipStat(BaseModel):
    """Per-chip statistics, accumulated from real sessions.

    ``first_try_rate`` is what orders the GESTO row by reliability: hands are
    where generators fail most, and the gate already measures it, so reliable
    poses surface first and fragile ones sink.  She never sees the mechanism.
    """

    shown: int = 0
    kept: int = 0
    passed_gate: int = 0
    generated: int = 0

    @property
    def keep_rate(self) -> float | None:
        return self.kept / self.shown if self.shown else None

    @property
    def first_try_rate(self) -> float | None:
        return self.passed_gate / self.generated if self.generated else None


class LookStats(BaseModel):
    first_try_rate: float | None = None
    avg_cost_usd: float | None = None
    keep_rate: float | None = None
    sessions: int = 0
    last_shown_at: float | None = None  # unix ts; drives freshness


class LookRecipe(BaseModel):
    """One entry in the catalog."""

    version: str = RECIPE_VERSION
    id: str
    name: str
    category: str
    cover_image: str | None = None

    recipe: Recipe = Field(default_factory=Recipe)
    applies_to: AppliesTo = Field(default_factory=AppliesTo)

    #: What the compiler may vary when she leaves a row untouched.
    variation_axes: list[Attribute] = Field(default_factory=list)
    #: What must never move.  Identity attributes are forced in regardless.
    locks: list[Attribute] = Field(default_factory=list)
    #: Which rows appear on the style screen for this look.
    selectable: list[Attribute] = Field(default_factory=list)
    #: The tap vocabulary per row.
    chips: dict[Attribute, list[str]] = Field(default_factory=dict)

    chip_stats: dict[str, ChipStat] = Field(default_factory=dict)
    route_hint: str = "in_place_edit"
    learned_params: dict[str, object] = Field(default_factory=dict)
    stats: LookStats = Field(default_factory=LookStats)

    enabled: bool = True

    #: Looks that only make sense with the coverage policy relaxed.
    #:
    #: Swimwear, lingerie, bath scenes. They are authored in full and kept in
    #: the catalog, but stay hidden while COVERAGE_POLICY is enforced - not
    #: merely toned down, hidden. A lingerie look rendered under a coverage
    #: clause is not a milder version of itself; it is a contradiction that
    #: wastes a generation and reads as a bug.
    #:
    #: Deliberately NOT the `enabled` flag. `enabled` means retired. This
    #: means waiting on a decision not yet taken, and one line in .env brings
    #: every one of them back at handover.
    requires_coverage_off: bool = False

    # -- validation ---------------------------------------------------------

    @field_validator("locks")
    @classmethod
    def _force_identity_locks(cls, v: list[Attribute]) -> list[Attribute]:
        return sorted(set(v) | ALWAYS_LOCKED, key=lambda a: a.value)

    @field_validator("selectable")
    @classmethod
    def _selectable_is_selectable(cls, v: list[Attribute]) -> list[Attribute]:
        bad = [a.value for a in v if a not in SELECTABLE]
        if bad:
            raise ValueError(f"not selectable attributes: {', '.join(bad)}")
        return v

    @field_validator("variation_axes")
    @classmethod
    def _axes_are_not_identity(cls, v: list[Attribute]) -> list[Attribute]:
        bad = [a.value for a in v if a in ALWAYS_LOCKED]
        if bad:
            raise ValueError(
                f"identity attributes cannot be variation axes: {', '.join(bad)}"
            )
        return v

    def model_post_init(self, __context: object) -> None:
        # A row can only be offered if the look actually declares chips for it.
        self.selectable = [a for a in self.selectable if self.chips.get(a)]
        # An attribute cannot be both locked and varied.
        locked = set(self.locks)
        self.variation_axes = [a for a in self.variation_axes if a not in locked]

    # -- behaviour ----------------------------------------------------------

    def chip_key(self, attribute: Attribute, value: str) -> str:
        return f"{attribute.value}:{value}"

    def ordered_chips(self, attribute: Attribute) -> list[str]:
        """Chips for one row, most reliable first.

        Chips with no data yet keep their authored order and sit after those
        with a proven record, so a new chip is neither promoted nor buried.
        """
        chips = list(self.chips.get(attribute) or [])
        if not chips:
            return []

        def sort_key(item: tuple[int, str]) -> tuple[int, float, int]:
            index, value = item
            stat = self.chip_stats.get(self.chip_key(attribute, value))
            rate = stat.first_try_rate if stat else None
            if rate is None:
                return (1, 0.0, index)  # unproven: keep authored order, after
            return (0, -rate, index)  # proven: best first

        return [value for _, value in sorted(enumerate(chips), key=sort_key)]

    def as_selections(self) -> Selections:
        """A quick style is just a saved selection.

        Tapping [ Editorial moda ] fills every row at once and jumps straight
        to previews.  One mechanism underneath with shortcuts on top, rather
        than two systems to build and keep in step.
        """
        values: dict[Attribute, list[str]] = {}
        if self.recipe.garment:
            values[Attribute.GARMENT] = [self.recipe.garment.type]
            if self.recipe.garment.colors:
                values[Attribute.GARMENT_COLOR] = list(self.recipe.garment.colors)
        if self.recipe.scene:
            values[Attribute.SCENE] = [self.recipe.scene.place]
        if self.recipe.lighting:
            values[Attribute.LIGHT] = [self.recipe.lighting.key]
        values[Attribute.FRAMING] = [self.recipe.camera.framing.value]
        if self.recipe.pose_family:
            values[Attribute.GESTURE] = list(self.recipe.pose_family)
        # Only keep rows this look actually exposes.
        allowed = set(self.selectable) or SELECTABLE
        return Selections(
            values={a: v for a, v in values.items() if a in allowed and v}
        )
