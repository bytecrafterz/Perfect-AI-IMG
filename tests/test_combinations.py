"""Tests for the combination walk.

The properties that matter to the product, stated as tests:

  1. she gets what she asked for   - every constrained row appears with only
                                     the values she selected
  2. a batch is a choice           - no duplicate combinations while the space
                                     is big enough to avoid them
  3. even spread                   - never six variations of one corner
  4. [ Otras 6 ] continues         - the next batch does not repeat the six
                                     she has already rejected
  5. full coverage when it fits    - a small space is covered exhaustively
"""

from __future__ import annotations

from math import gcd

import pytest

from app.compile.combinations import (
    Axis,
    Space,
    build_space,
    coprime_stride,
    plan_batch,
)
from app.contracts.common import Attribute

GARMENT = Attribute.GARMENT
GESTURE = Attribute.GESTURE
EXPRESSION = Attribute.EXPRESSION
LIGHT = Attribute.LIGHT


# -- stride -------------------------------------------------------------------


@pytest.mark.parametrize("size", range(1, 200))
def test_stride_is_coprime_and_in_range(size: int) -> None:
    s = coprime_stride(size)
    assert 1 <= s <= max(1, size - 1)
    if size > 2:
        assert gcd(s, size) == 1


def test_stride_visits_every_point_before_repeating() -> None:
    for size in (3, 4, 5, 6, 7, 8, 12, 30, 64, 101):
        stride = coprime_stride(size)
        visited = {(k * stride) % size for k in range(size)}
        assert len(visited) == size, f"stride {stride} does not cover size {size}"


# -- space --------------------------------------------------------------------


def test_empty_space_has_one_point() -> None:
    space = Space()
    assert space.size == 1
    assert space.at(0) == {}


def test_space_decodes_every_index_uniquely() -> None:
    space = build_space({GARMENT: ["a", "b", "c"], GESTURE: ["x", "y"]})
    assert space.size == 6
    seen = {tuple(sorted(space.at(i).items())) for i in range(6)}
    assert len(seen) == 6


def test_axis_rejects_empty_values() -> None:
    with pytest.raises(ValueError):
        Axis(attribute=GARMENT, values=())


# -- the promise to her -------------------------------------------------------


def test_only_selected_values_are_used() -> None:
    """A constrained row must never produce a value she did not tap."""
    plan = plan_batch(
        constrained_rows={GARMENT: ["vestido", "casual"], GESTURE: ["de pie"]},
        free_rows={LIGHT: ["natural", "dorada", "estudio"]},
        count=6,
    )
    for slot in plan.slots:
        assert slot.values[GARMENT] in {"vestido", "casual"}
        assert slot.values[GESTURE] == "de pie"


def test_single_selection_is_fixed_everywhere() -> None:
    """ONE selected -> identical in all previews."""
    plan = plan_batch(
        constrained_rows={EXPRESSION: ["sonrisa suave"]},
        free_rows={GESTURE: ["de pie", "caminando", "sentada"]},
        count=6,
    )
    assert {s.values[EXPRESSION] for s in plan.slots} == {"sonrisa suave"}


def test_untouched_row_is_varied_by_the_robot() -> None:
    """NOTHING selected -> the robot varies it."""
    plan = plan_batch(
        constrained_rows={},
        free_rows={GESTURE: ["de pie", "caminando", "sentada"]},
        count=6,
    )
    assert len({s.values[GESTURE] for s in plan.slots}) == 3


def test_small_space_is_covered_exhaustively() -> None:
    """2 garments x 2 gestures = 4 combinations, and all four appear."""
    plan = plan_batch(
        constrained_rows={
            GARMENT: ["vestido", "casual"],
            GESTURE: ["de pie", "caminando"],
        },
        free_rows={},
        count=6,
    )
    assert plan.constrained_size == 4
    assert plan.covers_whole_space
    combos = {(s.values[GARMENT], s.values[GESTURE]) for s in plan.slots}
    assert combos == {
        ("vestido", "de pie"),
        ("vestido", "caminando"),
        ("casual", "de pie"),
        ("casual", "caminando"),
    }


def test_leftover_slots_vary_a_free_row() -> None:
    """4 combinations into 6 slots: the extra two differ by a free row."""
    plan = plan_batch(
        constrained_rows={
            GARMENT: ["vestido", "casual"],
            GESTURE: ["de pie", "caminando"],
        },
        free_rows={LIGHT: ["natural", "dorada", "estudio"]},
        count=6,
    )
    signatures = {
        (s.values[GARMENT], s.values[GESTURE], s.values[LIGHT]) for s in plan.slots
    }
    assert len(signatures) == 6, "leftover slots must still be distinct photographs"
    assert not plan.reseeded


def test_no_duplicates_when_the_space_is_large_enough() -> None:
    plan = plan_batch(
        constrained_rows={
            GARMENT: ["vestido", "traje", "casual"],
            GESTURE: ["de pie", "caminando", "sentada"],
            EXPRESSION: ["neutra", "sonrisa suave"],
        },
        free_rows={},
        count=6,
    )
    assert plan.constrained_size == 18
    signatures = {tuple(sorted(s.values.items())) for s in plan.slots}
    assert len(signatures) == 6
    assert not plan.reseeded


def test_reseed_is_reported_not_hidden() -> None:
    """One combination, six slots: duplicates are unavoidable and must be
    flagged rather than silently shipped as 'variety'."""
    plan = plan_batch(
        constrained_rows={GARMENT: ["vestido"]},
        free_rows={},
        count=6,
    )
    assert plan.reseeded is True
    assert len({s.seed for s in plan.slots}) == 6, "reseeded slots must differ"


# -- even spread --------------------------------------------------------------


def test_batch_is_not_six_variations_of_one_corner() -> None:
    """The failure mode a naive sequential walk produces: with garment as the
    slow dimension, the first six of an odometer walk would all share one
    garment, leaving her nothing to choose between."""
    plan = plan_batch(
        constrained_rows={
            GARMENT: ["vestido", "traje", "casual"],
            GESTURE: ["de pie", "caminando", "sentada", "apoyada"],
        },
        free_rows={},
        count=6,
    )
    garments = {s.values[GARMENT] for s in plan.slots}
    gestures = {s.values[GESTURE] for s in plan.slots}
    assert len(garments) >= 2, "a six-preview batch must span more than one garment"
    assert len(gestures) >= 3


def test_spread_is_balanced_across_a_dimension() -> None:
    """Over a full pass, every value of a dimension appears equally often."""
    values = ["a", "b", "c", "d"]
    plan = plan_batch(
        constrained_rows={GARMENT: values, GESTURE: ["x", "y"]},
        free_rows={},
        count=8,
    )
    counts = {v: 0 for v in values}
    for slot in plan.slots:
        counts[slot.values[GARMENT]] += 1
    assert max(counts.values()) - min(counts.values()) <= 1


# -- [ Otras 6 ] --------------------------------------------------------------


def test_next_batch_continues_instead_of_repeating() -> None:
    rows = {
        GARMENT: ["vestido", "traje", "casual"],
        GESTURE: ["de pie", "caminando", "sentada"],
        EXPRESSION: ["neutra", "sonrisa suave"],
    }
    first = plan_batch(constrained_rows=rows, free_rows={}, count=6)
    second = plan_batch(
        constrained_rows=rows, free_rows={}, count=6, cursor=first.next_cursor
    )

    first_sigs = {tuple(sorted(s.values.items())) for s in first.slots}
    second_sigs = {tuple(sorted(s.values.items())) for s in second.slots}

    assert first.next_cursor == 6
    assert second.next_cursor == 12
    assert not (first_sigs & second_sigs), "[ Otras 6 ] must not repeat rejected options"


def test_walk_eventually_covers_the_whole_space() -> None:
    rows = {GARMENT: ["a", "b", "c"], GESTURE: ["x", "y", "z"]}
    seen: set[tuple] = set()
    cursor = 0
    for _ in range(3):  # 3 batches of 3 == the whole 9-point space
        plan = plan_batch(constrained_rows=rows, free_rows={}, count=3, cursor=cursor)
        seen |= {tuple(sorted(s.values.items())) for s in plan.slots}
        cursor = plan.next_cursor
    assert len(seen) == 9


def test_walk_is_deterministic() -> None:
    rows = {GARMENT: ["a", "b", "c"], GESTURE: ["x", "y"]}
    a = plan_batch(constrained_rows=rows, free_rows={}, count=4)
    b = plan_batch(constrained_rows=rows, free_rows={}, count=4)
    assert [s.values for s in a.slots] == [s.values for s in b.slots]


# -- degenerate input ---------------------------------------------------------


def test_zero_count_returns_nothing() -> None:
    plan = plan_batch(constrained_rows={GARMENT: ["a"]}, free_rows={}, count=0)
    assert plan.slots == ()


def test_empty_lists_are_ignored_not_crashed_on() -> None:
    plan = plan_batch(
        constrained_rows={GARMENT: [], GESTURE: ["de pie"]},
        free_rows={LIGHT: []},
        count=3,
    )
    assert plan.constrained_size == 1
    assert all(s.values[GESTURE] == "de pie" for s in plan.slots)
    assert GARMENT not in plan.slots[0].values
