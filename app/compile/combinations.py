"""The combination walk - turning what she marked into N concrete slots.

Her selections describe a SPACE, and the previews are drawn from inside it.
This module decides which points of that space the batch covers.

Two spaces, walked together:

    CONSTRAINED   the cartesian product of the rows she actually touched
    FREE          the rows she left alone, which the robot may vary

Both are walked with a stride that is coprime to the space size, so:

  * no combination repeats until the whole space has been covered
  * consecutive samples land far apart, so a batch is never six variations of
    one corner - which would leave her nothing to choose between
  * the walk is deterministic and resumable, so [ Otras 6 ] CONTINUES the
    walk rather than repeating the six she has already rejected

Pure stdlib.  No pydantic, no I/O - this is the part most likely to hide an
off-by-one, so it is kept trivially testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import gcd

from app.contracts.common import Attribute

#: 1/phi.  The classic low-discrepancy stride: successive multiples land as
#: far from each other as an irrational rotation allows, which is exactly the
#: "don't cluster" property a preview grid needs.
_INV_PHI = 0.6180339887498949


def coprime_stride(size: int) -> int:
    """A step near size/phi that is coprime to size.

    Coprimality is what guarantees the walk visits every point before
    repeating any.  Searching outward from the golden-ratio point keeps the
    spread property while satisfying it.
    """
    if size <= 2:
        return 1
    target = max(1, int(size * _INV_PHI))
    for delta in range(size):
        for candidate in {target - delta, target + delta}:
            if 1 <= candidate < size and gcd(candidate, size) == 1:
                return candidate
    return 1  # unreachable for size >= 2, but never return 0


@dataclass(frozen=True)
class Axis:
    """One dimension of a space: an attribute and its permitted values."""

    attribute: Attribute
    values: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.values:
            raise ValueError(f"axis {self.attribute.value} has no values")

    @property
    def size(self) -> int:
        return len(self.values)


@dataclass(frozen=True)
class Space:
    """A cartesian product of axes, addressable by a single integer index."""

    axes: tuple[Axis, ...] = ()

    @property
    def size(self) -> int:
        total = 1
        for axis in self.axes:
            total *= axis.size
        return total

    def at(self, index: int) -> dict[Attribute, str]:
        """Decode an index into a concrete assignment.

        Plain mixed-radix odometer; the spreading is done by the stride, not
        by the decoding, which keeps this half easy to verify.
        """
        if self.size == 0:
            return {}
        i = index % self.size
        out: dict[Attribute, str] = {}
        for axis in self.axes:
            i, digit = divmod(i, axis.size)
            out[axis.attribute] = axis.values[digit]
        return out

    def walk(self, start: int, count: int) -> list[int]:
        """`count` indices from `start`, spread across the space."""
        size = self.size
        if size <= 1:
            return [0] * count
        stride = coprime_stride(size)
        return [((start + k) * stride) % size for k in range(count)]


@dataclass(frozen=True)
class Slot:
    """One concrete preview to generate."""

    index: int
    #: Values she asked for.  Present in every slot of the batch.
    constrained: dict[Attribute, str] = field(default_factory=dict)
    #: Values the robot chose for rows she left alone.
    free: dict[Attribute, str] = field(default_factory=dict)
    #: Distinguishes slots whose attributes happen to coincide, so a batch is
    #: never literally the same image twice.
    seed: int = 0

    @property
    def values(self) -> dict[Attribute, str]:
        """The full assignment.  Her choices win over the robot's."""
        merged = dict(self.free)
        merged.update(self.constrained)
        return merged

    def describe(self) -> str:
        return ", ".join(f"{a.value}={v}" for a, v in sorted(
            self.values.items(), key=lambda kv: kv[0].value
        ))


@dataclass(frozen=True)
class BatchPlan:
    slots: tuple[Slot, ...]
    constrained_size: int
    free_size: int
    next_cursor: int
    #: True when the batch could not fill N distinct combinations and had to
    #: fall back to reseeding.  Surfaced so it is never a silent truncation.
    reseeded: bool = False

    @property
    def covers_whole_space(self) -> bool:
        return self.constrained_size <= len(self.slots)


def build_space(rows: dict[Attribute, list[str]]) -> Space:
    """Axes in a stable order, so the same selection always walks the same
    way - which is what lets [ Otras 6 ] continue rather than repeat."""
    axes = tuple(
        Axis(attribute=a, values=tuple(v))
        for a, v in sorted(rows.items(), key=lambda kv: kv[0].value)
        if v
    )
    return Space(axes=axes)


def plan_batch(
    *,
    constrained_rows: dict[Attribute, list[str]],
    free_rows: dict[Attribute, list[str]],
    count: int,
    cursor: int = 0,
    seed_base: int = 0,
) -> BatchPlan:
    """Plan `count` previews.

    ``constrained_rows``  what she selected (one value = fixed, several = vary
                          across exactly those)
    ``free_rows``         rows she left untouched that the look permits varying
    ``cursor``            where the previous batch stopped; 0 for a fresh start

    Behaviour, matching the spec:

      constrained space <= count   every combination is covered, and the
                                   leftover slots vary a free row
      constrained space >  count   an even sample of `count` points, with the
                                   cursor advanced so the next batch continues
    """
    if count <= 0:
        return BatchPlan(
            slots=(), constrained_size=0, free_size=0, next_cursor=cursor
        )

    constrained = build_space(constrained_rows)
    free = build_space(free_rows)

    # Walk the COMBINED space, not the two spaces side by side.
    #
    # Walking them separately looks equivalent and is not: when both happen to
    # be the same size they share a stride, move in lockstep, and wrap
    # together - so slot 5 comes back as an exact duplicate of slot 1 and the
    # grid quietly loses two of its six options.
    #
    # Indexing the product instead gives the behaviour the spec describes, for
    # free: i % C cycles the combinations she asked for, i // C moves the row
    # she left alone. The stride is coprime to C*F and therefore also to C, so
    # the first C slots still cover every combination exactly once.
    combined_size = constrained.size * free.size
    stride = coprime_stride(combined_size)

    slots: list[Slot] = []
    seen: set[tuple[tuple[Attribute, str], ...]] = set()
    reseeded = False

    for k in range(count):
        i = ((cursor + k) * stride) % combined_size if combined_size > 1 else 0
        c_values = constrained.at(i % constrained.size)
        f_values = free.at(i // constrained.size)
        merged = {**f_values, **c_values}
        signature = tuple(sorted(((a, v) for a, v in merged.items()), key=lambda kv: kv[0].value))
        if signature in seen:
            # The available space is smaller than the batch.  Still produce a
            # distinct image by moving the seed, and record that we did - a
            # silent duplicate would look like a broken generator.
            reseeded = True
        seen.add(signature)
        slots.append(
            Slot(
                index=k,
                constrained=c_values,
                free=f_values,
                seed=seed_base + cursor + k,
            )
        )

    return BatchPlan(
        slots=tuple(slots),
        constrained_size=constrained.size,
        free_size=free.size,
        next_cursor=cursor + count,
        reseeded=reseeded,
    )
