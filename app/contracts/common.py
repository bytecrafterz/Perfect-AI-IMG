"""Shared value types used across the four contracts.

Nothing here knows about a provider, a model, or an HTTP client.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, field_validator


class Attribute(str, Enum):
    """Every attribute the system can hold constant, vary, or measure.

    This enum is the single vocabulary shared by LookRecipe.selectable,
    Selections, AttributeIR.locks and the compiler's combination walk.  A
    string that is not in here cannot be selected, locked, or varied.
    """

    GARMENT = "garment"
    GARMENT_COLOR = "garment_color"
    HAIR = "hair"
    GESTURE = "gesture"
    EXPRESSION = "expression"
    SCENE = "scene"
    LIGHT = "light"
    FRAMING = "framing"
    CAMERA_ANGLE = "camera_angle"

    # Identity attributes.  Never selectable, never variation axes.
    # Present so QAReport can name what drifted.
    FACE = "face"
    BODY_PROPORTIONS = "body_proportions"
    SKIN_TONE = "skin_tone"


#: Attributes that are locked on every request, in every look, always.
#: The gate blocks any unrequested change to these.  Section 3 of the spec,
#: "SAFE DEFAULTS": body adjustment is deferred to phase 2 and never automatic.
ALWAYS_LOCKED: frozenset[Attribute] = frozenset(
    {Attribute.FACE, Attribute.BODY_PROPORTIONS, Attribute.SKIN_TONE}
)

#: Attributes a look may expose as multi-select rows in the UI.
SELECTABLE: frozenset[Attribute] = frozenset(
    {
        Attribute.GARMENT,
        Attribute.GARMENT_COLOR,
        Attribute.HAIR,
        Attribute.GESTURE,
        Attribute.EXPRESSION,
        Attribute.SCENE,
        Attribute.LIGHT,
        Attribute.FRAMING,
    }
)


class Framing(str, Enum):
    CLOSE_UP = "primer plano"
    MEDIUM = "medio"
    FULL_BODY = "cuerpo entero"
    UNKNOWN = "desconocido"


class BBox(BaseModel):
    """Normalised bounding box, origin top-left, values in [0, 1].

    Normalised rather than pixel-based so a box found on a 512px preview
    transfers unchanged to a 2048px final.
    """

    x0: float = Field(ge=0.0, le=1.0)
    y0: float = Field(ge=0.0, le=1.0)
    x1: float = Field(ge=0.0, le=1.0)
    y1: float = Field(ge=0.0, le=1.0)

    @field_validator("x1")
    @classmethod
    def _x_ordered(cls, v: float, info) -> float:
        x0 = info.data.get("x0")
        if x0 is not None and v <= x0:
            raise ValueError("x1 must be greater than x0")
        return v

    @field_validator("y1")
    @classmethod
    def _y_ordered(cls, v: float, info) -> float:
        y0 = info.data.get("y0")
        if y0 is not None and v <= y0:
            raise ValueError("y1 must be greater than y0")
        return v

    def to_pixels(self, width: int, height: int) -> tuple[int, int, int, int]:
        return (
            int(self.x0 * width),
            int(self.y0 * height),
            int(self.x1 * width),
            int(self.y1 * height),
        )

    def dilated(self, factor: float, *, clamp: bool = True) -> "BBox":
        """Grow the box around its centre.  Used before inpainting so the
        repair has context to blend into rather than a hard edge."""
        cx, cy = (self.x0 + self.x1) / 2, (self.y0 + self.y1) / 2
        hw, hh = (self.x1 - self.x0) / 2 * factor, (self.y1 - self.y0) / 2 * factor
        x0, y0, x1, y1 = cx - hw, cy - hh, cx + hw, cy + hh
        if clamp:
            x0, y0 = max(0.0, x0), max(0.0, y0)
            x1, y1 = min(1.0, x1), min(1.0, y1)
        return BBox(x0=x0, y0=y0, x1=x1, y1=y1)

    @property
    def area(self) -> float:
        return (self.x1 - self.x0) * (self.y1 - self.y0)


class Money(BaseModel):
    """USD amounts, carried as a value object so no float ever reaches the
    ledger by accident and every cost has a source attached."""

    usd: float = Field(ge=0.0)
    source: str = Field(description="what was billed, e.g. 'preview:mock.fast'")

    def __add__(self, other: "Money") -> "Money":
        return Money(usd=self.usd + other.usd, source="sum")

    @classmethod
    def zero(cls, source: str = "free") -> "Money":
        return cls(usd=0.0, source=source)
