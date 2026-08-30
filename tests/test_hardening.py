"""Guards for three things that were true but not enforced.

Each was a promise the code made and did not keep, and none of them showed up
as a failure - they showed up as a limit that was never reached, a cap that
quietly reset, and a mode that would have switched itself on at the worst
possible moment.
"""

from __future__ import annotations

import time

import pytest

from app.gate.backends import CVCapabilities
from app.gate.gate import resolve_strict
from app.ledger import BudgetExceeded, Ledger
from app.profile.model import IdentityProfile


def _caps(**kw) -> CVCapabilities:
    base = dict(
        onnxruntime=False,
        insightface=False,
        opencv=False,
        face_model=False,
        pose_model=False,
    )
    base.update(kw)
    return CVCapabilities(**base)


FULL = _caps(onnxruntime=True, insightface=True, opencv=True, face_model=True, pose_model=True)
POSE_ONLY = _caps(onnxruntime=True, opencv=True, pose_model=True)


# ---------------------------------------------------------------------------
# strict mode must not switch itself on into a state that discards everything
# ---------------------------------------------------------------------------


def test_enrolling_a_face_does_not_brick_the_gate() -> None:
    """The trap this replaces.

    strict used to be `profile.can_check_identity`. Installing insightface and
    enrolling her face flipped it True - and strict discards on ANY unknown.
    Proportions would still have been unmeasurable, so every image would have
    been discarded, immediately after an install that was supposed to improve
    things. The obvious response, uninstalling, would have been exactly wrong.
    """
    profile = IdentityProfile(centroid=[0.1] * 512)
    strict, why = resolve_strict(profile, _caps(insightface=True, face_model=True))
    assert strict is True, "identity enrolled and measurable - strict is correct here"

    # ...but add a proportions baseline with no pose model and it must back off
    profile.proportions.shoulder_torso_ratio = 0.86
    strict, why = resolve_strict(profile, _caps(insightface=True, face_model=True))
    assert strict is False
    assert any("proporciones" in r for r in why)


def test_strict_when_every_reference_can_be_measured() -> None:
    profile = IdentityProfile(centroid=[0.1] * 512, skin_lab=[49.0, 14.0, 15.0])
    profile.proportions.shoulder_torso_ratio = 0.86
    strict, why = resolve_strict(profile, FULL)
    assert strict is True
    assert why == []


def test_an_empty_profile_is_not_strict() -> None:
    """Blocking everything because nothing has been enrolled would be
    consistent and useless."""
    strict, why = resolve_strict(IdentityProfile(), FULL)
    assert strict is False
    assert any("referencia" in r for r in why)


def test_a_skin_reference_without_a_detector_holds_strict_off() -> None:
    """The exact shape of the bug that discarded every final: a reference we
    hold but cannot measure against."""
    profile = IdentityProfile(skin_lab=[49.0, 14.0, 15.0])
    strict, why = resolve_strict(profile, POSE_ONLY)
    assert strict is False
    assert any("piel" in r for r in why)


@pytest.mark.parametrize("policy,expected", [("on", True), ("off", False)])
def test_the_override_wins(policy: str, expected: bool) -> None:
    strict, _ = resolve_strict(IdentityProfile(), _caps(), policy)
    assert strict is expected


# ---------------------------------------------------------------------------
# the daily cap must survive a restart
# ---------------------------------------------------------------------------


def _ledger(**kw) -> Ledger:
    base = dict(per_session_usd=1.5, per_day_usd=10.0)
    base.update(kw)
    return Ledger(**base)


def test_daily_cap_survives_a_restart() -> None:
    """Ledger is an in-memory accumulator, so a fresh process starts at zero.

    Under a watchdog set to restart 999 times, that made the $10/day limit
    re-earnable all afternoon.
    """
    now = time.time()
    already_spent = [
        {"session_id": "s1", "kind": "final", "provider_id": "fal", "usd": 4.0,
         "detail": "", "at": now - 3600},
        {"session_id": "s2", "kind": "final", "provider_id": "fal", "usd": 5.5,
         "detail": "", "at": now - 60},
    ]
    fresh = _ledger()
    assert fresh.day_total() == 0.0, "this is the bug: a new process knows nothing"

    fresh.rehydrate(already_spent)
    assert fresh.day_total() == pytest.approx(9.5)

    with pytest.raises(BudgetExceeded):
        fresh.check(session_id="s3", additional_usd=1.0)


def test_rehydrate_does_not_double_charge_the_prepaid_balance() -> None:
    """These calls were already paid for. Replaying them against the balance
    would refuse spending that is genuinely affordable."""
    led = _ledger(balance_usd=20.0)
    led.rehydrate(
        [{"session_id": "s", "kind": "final", "provider_id": "p", "usd": 5.0,
          "detail": "", "at": time.time()}]
    )
    assert led.balance_usd == 20.0
    assert led.day_total() == pytest.approx(5.0)


def test_rehydrated_entries_are_ordered() -> None:
    now = time.time()
    led = _ledger()
    led.rehydrate(
        [
            {"session_id": "s", "kind": "a", "provider_id": "p", "usd": 1.0, "detail": "", "at": now},
            {"session_id": "s", "kind": "b", "provider_id": "p", "usd": 1.0, "detail": "", "at": now - 500},
        ]
    )
    assert [e.at for e in led.entries] == sorted(e.at for e in led.entries)


def test_spend_outside_the_window_is_not_loaded() -> None:
    """costs_since is what bounds this; the ledger trusts what it is handed."""
    led = _ledger()
    led.rehydrate(
        [{"session_id": "s", "kind": "final", "provider_id": "p", "usd": 3.0,
          "detail": "", "at": time.time() - 200_000}]
    )
    assert led.day_total() == 0.0
    assert led.total() == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# the session cap must bind the paths that fire when things go wrong
# ---------------------------------------------------------------------------


def test_the_session_cap_actually_refuses() -> None:
    """run_previews and run_finals each check once, with an estimate. Repair
    and retry then spent without asking again - up to 2.9x the cap."""
    led = _ledger(per_session_usd=0.20)
    led.record(session_id="s", kind="final", provider_id="fal", usd=0.18)

    with pytest.raises(BudgetExceeded) as caught:
        led.check(session_id="s", additional_usd=0.04)
    assert "sesion" in caught.value.message_es()


def test_a_cap_check_is_per_session_not_global() -> None:
    led = _ledger(per_session_usd=0.20)
    led.record(session_id="s1", kind="final", provider_id="fal", usd=0.19)
    led.check(session_id="s2", additional_usd=0.19)  # must not raise
