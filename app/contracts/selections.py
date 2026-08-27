"""Selections - what she marked on the style screen.

The heart of the interaction model.  Every attribute row accepts many values,
and how many she picked is what tells the compiler what to do:

    NOTHING selected  ->  vary it freely across the previews
    ONE selected      ->  fixed; identical in all of them
    SEVERAL selected  ->  vary across exactly those values, nothing else

One uniform rule, one field on the request.  She is not choosing a style, she
is describing a space; the previews are drawn from inside it.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, field_validator

from app.contracts.common import ALWAYS_LOCKED, SELECTABLE, Attribute


class RowState(str, Enum):
    """Derived, never stored - the state of one attribute row."""

    FREE = "free"  # nothing selected: robot varies it
    FIXED = "fixed"  # one selected: identical everywhere
    CONSTRAINED = "constrained"  # several: varies across exactly those


class Selections(BaseModel):
    """Attribute -> chosen values.  An absent or empty list means FREE."""

    values: dict[Attribute, list[str]] = Field(default_factory=dict)

    @field_validator("values")
    @classmethod
    def _only_selectable(
        cls, v: dict[Attribute, list[str]]
    ) -> dict[Attribute, list[str]]:
        for attribute, choices in v.items():
            if attribute in ALWAYS_LOCKED:
                raise ValueError(
                    f"{attribute.value} is an identity attribute and can never "
                    "be selected; see SAFE DEFAULTS in the spec"
                )
            if attribute not in SELECTABLE:
                raise ValueError(f"{attribute.value} is not a selectable attribute")
            # Deduplicate while preserving the order she tapped them in - the
            # order matters, because the combination walk is deterministic and
            # her first choice should lead.
            seen: set[str] = set()
            deduped = [c for c in choices if not (c in seen or seen.add(c))]
            v[attribute] = deduped
        return v

    def state_of(self, attribute: Attribute) -> RowState:
        chosen = self.values.get(attribute) or []
        if not chosen:
            return RowState.FREE
        if len(chosen) == 1:
            return RowState.FIXED
        return RowState.CONSTRAINED

    def chosen(self, attribute: Attribute) -> list[str]:
        return list(self.values.get(attribute) or [])

    @property
    def constrained_attributes(self) -> list[Attribute]:
        """Rows she actually touched, in a stable order.

        These define the combination space.  Sorted by attribute value so the
        same selection always produces the same walk - reproducibility matters
        for [ Otras 6 ] to continue rather than repeat.
        """
        touched = [a for a in self.values if self.values[a]]
        return sorted(touched, key=lambda a: a.value)

    @property
    def combination_count(self) -> int:
        """The number the live counter shows her before she commits.

        Cartesian product of every row she touched.  Rows left untouched do
        not multiply the space - the robot fills those in freely.
        """
        total = 1
        for attribute in self.constrained_attributes:
            total *= len(self.values[attribute])
        return total

    def describe(self) -> str:
        """The sticky bar text: '2 ropas x 2 gestos = 4 combinaciones'."""
        labels = {
            Attribute.GARMENT: ("ropa", "ropas"),
            Attribute.GARMENT_COLOR: ("color", "colores"),
            Attribute.HAIR: ("peinado", "peinados"),
            Attribute.GESTURE: ("gesto", "gestos"),
            Attribute.EXPRESSION: ("expresion", "expresiones"),
            Attribute.SCENE: ("escenario", "escenarios"),
            Attribute.LIGHT: ("luz", "luces"),
            Attribute.FRAMING: ("encuadre", "encuadres"),
        }
        parts: list[str] = []
        for attribute in self.constrained_attributes:
            n = len(self.values[attribute])
            singular, plural = labels.get(attribute, (attribute.value, attribute.value))
            parts.append(f"{n} {singular if n == 1 else plural}")
        if not parts:
            return "Sin filtros - te preparo una variedad"
        total = self.combination_count
        combos = "combinacion" if total == 1 else "combinaciones"
        return f"{' x '.join(parts)} = {total} {combos}"

    def merged_with(self, other: "Selections") -> "Selections":
        """Overlay ``other`` on top of self, row by row.

        Used when a quick style pre-fills every row and she then changes one:
        her tap wins for that row, the style's values survive elsewhere.
        """
        merged = {a: list(v) for a, v in self.values.items()}
        merged.update({a: list(v) for a, v in other.values.items() if v})
        return Selections(values=merged)
