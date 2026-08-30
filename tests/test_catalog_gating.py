"""Looks held back by the coverage policy.

Two different reasons a look can be missing, and conflating them is how a
temporary decision becomes permanent:

    enabled=False              retired. Someone must edit the file.
    requires_coverage_off      ready, waiting on a policy decision. One line
                               in .env brings the whole set back.

The tests that matter here are the negative ones: that a withheld look does
not leak into the offered list, and that relaxing the policy is genuinely all
it takes to get it back. If the second stops being true the flag has quietly
become a second `enabled` and the handover is a file-editing job again.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.catalog import Catalog
from app.contracts.look_recipe import LookRecipe

CATALOG_DIR = Path(__file__).resolve().parent.parent / "catalog"


def _look(look_id: str, *, enabled: bool = True, gated: bool = False) -> dict:
    return {
        "id": look_id,
        "name": look_id,
        "category": "prueba",
        "enabled": enabled,
        "requires_coverage_off": gated,
    }


@pytest.fixture()
def catalog(tmp_path: Path) -> Catalog:
    for payload in (
        _look("abierto"),
        _look("tambien_abierto"),
        _look("retenido", gated=True),
        _look("retirado", enabled=False),
        _look("retirado_y_retenido", enabled=False, gated=True),
    ):
        (tmp_path / f"{payload['id']}.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
    return Catalog(tmp_path).load()


def test_withheld_look_is_hidden_while_coverage_is_enforced(catalog: Catalog) -> None:
    shown = {look.id for look in catalog.all(coverage_enforced=True)}
    assert "retenido" not in shown
    assert shown == {"abierto", "tambien_abierto"}


def test_relaxing_the_policy_is_all_it_takes(catalog: Catalog) -> None:
    """The whole point of the flag: handover is a decision, not a task."""
    shown = {look.id for look in catalog.all(coverage_enforced=False)}
    assert "retenido" in shown


def test_retired_stays_retired_whatever_the_policy(catalog: Catalog) -> None:
    """`enabled=False` is not a coverage decision and must not move with one."""
    for enforced in (True, False):
        shown = {look.id for look in catalog.all(coverage_enforced=enforced)}
        assert "retirado" not in shown
        assert "retirado_y_retenido" not in shown


def test_withheld_reports_only_what_the_policy_is_holding(catalog: Catalog) -> None:
    """A catalog quietly showing fewer entries than it holds gets diagnosed as
    a loading bug. `withheld()` makes the state inspectable instead."""
    assert {look.id for look in catalog.withheld()} == {"retenido"}


def test_nothing_withheld_when_no_look_asks_for_it(tmp_path: Path) -> None:
    (tmp_path / "solo.json").write_text(json.dumps(_look("solo")), encoding="utf-8")
    assert Catalog(tmp_path).load().withheld() == []


def test_flag_defaults_to_off() -> None:
    """Authoring a look must never withhold it by accident."""
    assert LookRecipe(id="x", name="x", category="c").requires_coverage_off is False


# -- the real catalog -------------------------------------------------------


def _real_looks() -> list[LookRecipe]:
    return [
        LookRecipe.model_validate(json.loads(p.read_text(encoding="utf-8")))
        for p in sorted(CATALOG_DIR.glob("*.json"))
    ]


def test_every_catalog_file_loads() -> None:
    """Catalog.load() skips a malformed look and carries on, which is right in
    production and hides a typo here. This is the loud version."""
    looks = _real_looks()
    assert len(looks) >= 20, f"expected the full catalog, found {len(looks)}"


def test_the_revealing_looks_are_the_ones_held_back() -> None:
    """Named explicitly. If someone adds a fourth revealing look and forgets
    the flag, this fails rather than shipping it to her screen."""
    withheld = {look.id for look in _real_looks() if look.requires_coverage_off}
    assert withheld == {
        "playa_bikini_verano",
        "intimo_lenceria_editorial",
        "intimo_bano_luz_suave",
        # Added when the client asked for exposure to be eliminated entirely.
        # A long dress, but with thin straps and a bare back - real exposure,
        # not a detail the compiler can rewrite around. Withheld rather than
        # edited so the authored look survives for the handover.
        "moda_terraza_atardecer",
    }


@pytest.mark.parametrize("look", _real_looks(), ids=lambda look: look.id)
def test_identity_is_locked_in_every_look(look: LookRecipe) -> None:
    """Face, proportions and skin tone are locked everywhere, withheld looks
    included - they are the looks where a drift in body shape would be least
    acceptable, and least likely to be reported."""
    locked = {a.value for a in look.locks}
    assert {"face", "body_proportions", "skin_tone"} <= locked
