"""Regression: the leftover slots must genuinely vary a free row.

Found in a live run, not by the earlier tests, because it only appears when
the constrained and free spaces are the SAME SIZE. Walking the two spaces
independently gave them the same stride, so they advanced in lockstep and
wrapped together - and preview 5 came back as a pixel-identical repeat of
preview 1.

For her that is not a subtle indexing detail: it is a grid of six options
that only contains four, twice, with nothing to say so.
"""

from __future__ import annotations

import pytest

from app.compile.combinations import plan_batch
from app.contracts.common import Attribute

GARMENT = Attribute.GARMENT
GESTURE = Attribute.GESTURE
EXPRESSION = Attribute.EXPRESSION
COLOR = Attribute.GARMENT_COLOR
LIGHT = Attribute.LIGHT


def signatures(plan) -> list[tuple]:
    return [tuple(sorted(s.values.items(), key=lambda kv: kv[0].value)) for s in plan.slots]


def test_equal_sized_spaces_do_not_move_in_lockstep() -> None:
    """The exact shape that failed: 4 constrained combinations, 4 free values,
    six slots. Every slot must be a different photograph."""
    plan = plan_batch(
        constrained_rows={
            GARMENT: ["vestido largo", "traje sastre"],
            GESTURE: ["caminando", "girando"],
        },
        free_rows={COLOR: ["negro", "burdeos", "verde oscuro", "crema"]},
        count=6,
    )
    assert plan.constrained_size == 4
    assert plan.free_size == 4

    unique = set(signatures(plan))
    assert len(unique) == 6, f"expected 6 distinct slots, got {len(unique)}"
    assert not plan.reseeded


@pytest.mark.parametrize(
    "constrained_values,free_values",
    [
        (2, 2), (3, 3), (4, 4), (6, 6), (2, 4), (4, 2), (3, 6), (5, 5),
    ],
)
def test_no_duplicate_slots_for_any_space_pairing(
    constrained_values: int, free_values: int
) -> None:
    """Sweep the sizes, because the bug hid in one specific relationship
    between them and a single example would not have caught it."""
    plan = plan_batch(
        constrained_rows={GARMENT: [f"g{i}" for i in range(constrained_values)]},
        free_rows={LIGHT: [f"l{i}" for i in range(free_values)]},
        count=6,
    )
    unique = set(signatures(plan))
    expected = min(6, constrained_values * free_values)
    assert len(unique) == expected


def test_every_requested_combination_still_appears_first() -> None:
    """The fix must not cost the original guarantee: when her selection fits
    inside the batch, all of it is covered."""
    plan = plan_batch(
        constrained_rows={
            GARMENT: ["vestido", "traje"],
            GESTURE: ["de pie", "caminando"],
        },
        free_rows={COLOR: ["negro", "crema", "burdeos"]},
        count=4,
    )
    combos = {(s.values[GARMENT], s.values[GESTURE]) for s in plan.slots}
    assert combos == {
        ("vestido", "de pie"), ("vestido", "caminando"),
        ("traje", "de pie"), ("traje", "caminando"),
    }


def test_leftover_slots_repeat_the_combination_but_change_the_free_row() -> None:
    """The intended semantics, stated directly: she asked for four
    combinations and wanted six photographs, so two combinations come back a
    second time - looking different, because the row she left alone moved."""
    plan = plan_batch(
        constrained_rows={
            GARMENT: ["vestido", "traje"],
            GESTURE: ["de pie", "caminando"],
        },
        free_rows={COLOR: ["negro", "crema", "burdeos", "verde"]},
        count=6,
    )
    by_combo: dict[tuple, list[str]] = {}
    for slot in plan.slots:
        key = (slot.values[GARMENT], slot.values[GESTURE])
        by_combo.setdefault(key, []).append(slot.values[COLOR])

    repeated = [colors for colors in by_combo.values() if len(colors) > 1]
    assert repeated, "with 4 combinations in 6 slots something must repeat"
    for colors in repeated:
        assert len(set(colors)) == len(colors), "a repeat must change the free row"


def test_continuation_still_holds_with_free_rows() -> None:
    rows = {GARMENT: ["a", "b", "c"], GESTURE: ["x", "y"]}
    free = {COLOR: ["1", "2"]}
    first = plan_batch(constrained_rows=rows, free_rows=free, count=6)
    second = plan_batch(
        constrained_rows=rows, free_rows=free, count=6, cursor=first.next_cursor
    )
    assert not (set(signatures(first)) & set(signatures(second)))


def test_no_free_rows_falls_back_to_reseeding_and_says_so() -> None:
    plan = plan_batch(
        constrained_rows={GARMENT: ["vestido", "traje"]},
        free_rows={},
        count=6,
    )
    assert plan.reseeded is True
    assert len({s.seed for s in plan.slots}) == 6
