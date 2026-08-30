"""Three defects the re-audit found, two of them mine.

Installing the pose model did not just enable the anti-slimming check - it
made previously unreachable code run for the first time, and unreachable code
is untested code. Two of these were dormant until that install, and one was a
fix that only looked like a fix.

  1. measure() divided a width-normalised distance by a height-normalised one,
     so every ratio moved with the frame's shape. Padding a photo to 4:5 -
     black bars, not one body pixel altered - moved shoulder_torso_ratio by
     -33% against a 6% threshold. The check would have accused the generator
     of exactly what it exists to prevent.

  2. _check_hands reported PASS with a perfect 1.0 when it had located zero
     hands: the loop that computes the score simply never ran and the
     initial value survived. True for 11 of her 13 photos.

  3. resolve_strict - my own fix for the strict-mode trap - only asked
     "reference present, capability missing" and not the reverse. For the
     live profile, installing insightface would have returned strict=True with
     NO stated reason while identity and proportions were still UNKNOWN. That
     discards every image: the precise failure the function was written to
     prevent, in the state it was written for.

The third is the one worth remembering. A half-correct guard reads as a
correct guard and stops anyone looking again.
"""

from __future__ import annotations

import itertools

import pytest

from app.contracts.qa_report import CheckOutcome
from app.gate.backends import CVCapabilities
from app.gate.gate import resolve_strict
from app.gate.pose import Keypoint, Pose, measure, torso_length
from app.profile.model import IdentityProfile

# One fixed body in pixels. Every test below expresses it in different frames.
BODY_PX = {
    "left_shoulder": (400, 300),
    "right_shoulder": (600, 300),
    "left_hip": (430, 700),
    "right_hip": (570, 700),
    "left_ear": (470, 200),
    "right_ear": (530, 200),
}


def pose_in_frame(width: int, height: int, body=BODY_PX) -> Pose:
    """The same body, in a frame of the given shape.

    Pixel geometry is identical; only the normalisation denominators change.
    Any difference in the measured ratios is therefore an artefact.
    """
    return Pose(
        keypoints={
            name: Keypoint(x=x / width, y=y / height, confidence=0.99)
            for name, (x, y) in body.items()
        },
        box_confidence=0.99,
        aspect=height / width,
    )


# ---------------------------------------------------------------------------
# 1. proportions must not move with the shape of the frame
# ---------------------------------------------------------------------------

FRAMES = [(1000, 1000), (1000, 1250), (1000, 600), (1400, 1000), (900, 1600)]


@pytest.mark.parametrize("width,height", FRAMES)
def test_ratios_do_not_move_with_frame_shape(width: int, height: int) -> None:
    square = measure(pose_in_frame(1000, 1000))
    other = measure(pose_in_frame(width, height))

    for field in ("shoulder_torso_ratio", "hip_torso_ratio", "shoulder_hip_ratio"):
        a, b = getattr(square, field), getattr(other, field)
        assert a is not None and b is not None
        assert b == pytest.approx(a, rel=1e-9), f"{field} moved with the frame"


def test_the_measured_failure_case() -> None:
    """A portrait photo padded to 4:5 gave -33% before the fix, against a
    threshold of 6%."""
    portrait = measure(pose_in_frame(1000, 1333))
    padded = measure(pose_in_frame(1000, 1250))
    drift = abs(padded.shoulder_torso_ratio - portrait.shoulder_torso_ratio) / portrait.shoulder_torso_ratio
    assert drift < 0.001, f"reframing still reads as a {drift:.1%} body change"


def test_torso_length_does_not_move_with_frame_HEIGHT() -> None:
    """The denominator of every ratio, and it did its own raw hypot on
    midpoints rather than going through the corrected helper - so the error
    reached all of them even after distance() was fixed.

    Held at constant WIDTH on purpose: the value is expressed in units of
    image width, so a wider frame legitimately yields a smaller number. What
    must not change is the response to height, which is pure frame shape.
    """
    lengths = {torso_length(pose_in_frame(1000, h)) for h in (600, 1000, 1250, 1600)}
    assert max(lengths) == pytest.approx(min(lengths), rel=1e-9)


def test_frame_width_scales_torso_length_exactly_as_it_should() -> None:
    """And the width response is the honest one: double the frame width and
    the same body spans half as much of it. This is why every consumer uses
    torso_length as a denominator rather than a measurement."""
    assert torso_length(pose_in_frame(2000, 1000)) == pytest.approx(
        torso_length(pose_in_frame(1000, 1000)) / 2, rel=1e-9
    )


def test_a_real_narrowing_is_still_caught() -> None:
    """The fix must not have made the check blind - that would be worse than
    the false positive it removes."""
    narrowed = dict(BODY_PX)
    narrowed["left_shoulder"] = (415, 300)   # 15% narrower shoulders
    narrowed["right_shoulder"] = (585, 300)

    before = measure(pose_in_frame(1000, 1250))
    after = measure(pose_in_frame(1000, 1250, narrowed))
    drift = abs(after.shoulder_torso_ratio - before.shoulder_torso_ratio) / before.shoulder_torso_ratio
    assert drift > 0.06, "a 15% narrowing must exceed the 6% threshold"


# ---------------------------------------------------------------------------
# 2. locating no hands is not the same as finding good ones
# ---------------------------------------------------------------------------


def test_no_hands_located_is_unknown_not_a_perfect_score(monkeypatch) -> None:
    """Before: the scoring loop never ran, `worst` kept its 1.0 initial value,
    and the check returned PASS with a perfect score on 11 of 13 photos."""
    from pathlib import Path

    from app.config import Thresholds
    from app.gate.gate import Gate

    gate = Gate(
        profile=IdentityProfile(),
        thresholds=Thresholds(),
        models_dir=Path("does-not-exist"),
        strict=False,
    )
    monkeypatch.setattr(
        gate,
        "capabilities",
        CVCapabilities(
            onnxruntime=True, insightface=False, opencv=True,
            face_model=False, pose_model=True,
        ),
    )
    monkeypatch.setattr(gate._hands, "hands", lambda _path: [])

    check, defects = gate._check_hands("irrelevant.png")
    assert check.outcome is CheckOutcome.UNKNOWN
    assert check.outcome is not CheckOutcome.PASS
    assert check.value != 1.0
    assert defects == []


def test_located_hands_are_still_scored(monkeypatch) -> None:
    from pathlib import Path

    from app.config import Thresholds
    from app.gate.gate import Gate

    gate = Gate(
        profile=IdentityProfile(),
        thresholds=Thresholds(),
        models_dir=Path("does-not-exist"),
        strict=False,
    )
    monkeypatch.setattr(
        gate,
        "capabilities",
        CVCapabilities(
            onnxruntime=True, insightface=False, opencv=True,
            face_model=False, pose_model=True,
        ),
    )
    monkeypatch.setattr(
        gate._hands, "hands",
        lambda _p: [{"confidence": 0.95, "bbox": (0.1, 0.1, 0.2, 0.2)}],
    )
    check, _ = gate._check_hands("irrelevant.png")
    assert check.outcome is CheckOutcome.PASS
    assert check.value == pytest.approx(0.95)


# ---------------------------------------------------------------------------
# 3. strict must never be on while a check can return UNKNOWN
# ---------------------------------------------------------------------------


def _caps(insightface: bool, pose: bool) -> CVCapabilities:
    # insightface and buffalo_l arrive together and provide BOTH identity and
    # the face detector, so they are one switch and not two.
    return CVCapabilities(
        onnxruntime=pose, insightface=insightface, opencv=True,
        face_model=insightface, pose_model=pose,
    )


def _profile(identity: bool, proportions: bool, skin: bool) -> IdentityProfile:
    p = IdentityProfile()
    if identity:
        p.centroid = [0.1] * 512
    if proportions:
        p.proportions.shoulder_torso_ratio = 0.86
    if skin:
        p.skin_lab = [49.0, 14.0, 15.0]
    return p


def test_strict_is_never_on_while_something_is_unmeasurable() -> None:
    """Exhaustive over every profile x capability state.

    A check is UNKNOWN when EITHER half is missing - the reference, or what
    measures it - and in strict mode any UNKNOWN discards. So strict=True with
    any half missing anywhere means every image is discarded.

    The earlier version failed 21 of these.
    """
    offenders = []
    for pi, pp, ps, ci, cp in itertools.product([0, 1], repeat=5):
        caps, profile = _caps(bool(ci), bool(cp)), _profile(bool(pi), bool(pp), bool(ps))
        strict, _ = resolve_strict(profile, caps)
        if not strict:
            continue
        unknown = [
            name
            for name, measurable in (
                ("identity", pi and caps.identity_available),
                ("proportions", pp and caps.proportions_available),
                ("skin", ps and caps.face_detector_available),
            )
            if not measurable
        ]
        if unknown:
            offenders.append((pi, pp, ps, ci, cp, unknown))

    assert offenders == [], f"{len(offenders)} states would discard every image"


def test_the_live_state_does_not_arm_itself_by_installing_insightface() -> None:
    """The concrete regression: a profile holding a skin reference and nothing
    else. The earlier version returned strict=True with an EMPTY reason list.
    """
    live = _profile(identity=False, proportions=False, skin=True)
    strict, why = resolve_strict(live, _caps(insightface=True, pose=True))
    assert strict is False
    assert why, "silently strict is worse than strict - there must be a reason"


def test_strict_is_reachable_when_everything_is_present() -> None:
    """A guard that can never be satisfied is not a guard, it is an outage."""
    complete = _profile(identity=True, proportions=True, skin=True)
    strict, why = resolve_strict(complete, _caps(insightface=True, pose=True))
    assert strict is True
    assert why == []
