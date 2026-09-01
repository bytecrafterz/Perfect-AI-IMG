"""The anti-slimming check, end to end through the gate.

The other pose tests verify the maths. This one verifies the thing she was
actually promised: an image in which a generator made her thinner does not
reach her.

The pose session is stubbed rather than run, because the point under test is
the wiring - letterbox, decode, measure, compare, verdict - not the neural
network. A real model is verified by scripts/fetch_models.py plus one real
generation; what breaks in daily use is the plumbing around it.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from app.config import Thresholds
from app.contracts.qa_report import CheckOutcome, Verdict
from app.gate import backends
from app.gate.gate import Gate
from app.gate.pose import Keypoint, Pose, measure
from app.profile.model import Coverage, IdentityProfile
from tests.test_pose import full_body_keypoints, pose_from


@pytest.fixture(autouse=True)
def _pretend_the_models_are_installed(monkeypatch):
    """Capability detection is cached and reads the filesystem; here we assert
    the behaviour that follows from the models being present."""
    monkeypatch.setattr(
        backends,
        "detect_capabilities",
        lambda models_dir=None: backends.CVCapabilities(
            onnxruntime=True,
            insightface=False,
            opencv=True,
            face_model=False,
            pose_model=True,
            hand_model=False,
        ),
    )


def photo(tmp_path: Path, name: str = "shot.png") -> Path:
    from PIL import Image

    rng = np.random.default_rng(0)
    array = (rng.random((800, 512, 3)) * 60 + 150).astype("uint8")
    path = tmp_path / name
    Image.fromarray(array).save(path)
    return path


def build_gate(tmp_path: Path, *, pose_for_image: Pose, strict: bool = True) -> Gate:
    """A gate whose pose backend returns a known figure."""
    profile = IdentityProfile(
        owner="test",
        proportions=measure(pose_from(full_body_keypoints())),
        coverage=Coverage(full_body=6),
    )
    gate = Gate(
        profile=profile,
        thresholds=Thresholds(min_sharpness=0.0),
        models_dir=tmp_path / "models",
        strict=strict,
    )
    gate._pose.proportions = lambda path: measure(pose_for_image)  # type: ignore[assignment]
    gate._pose.subject = lambda path: pose_for_image  # type: ignore[assignment]

    # HandBackend owns its own PoseBackend, so it needs stubbing separately -
    # otherwise it goes looking for the real weights.
    from app.contracts.common import BBox
    from app.gate.pose import hand_regions

    def stub_hands(path):
        out = []
        for name, (x0, y0, x1, y1) in hand_regions(pose_for_image):
            wrist = pose_for_image.keypoints.get(name.replace("_hand", "_wrist"))
            out.append(
                {
                    "name": name,
                    "bbox": BBox(x0=x0, y0=y0, x1=x1, y1=y1),
                    "confidence": float(wrist.confidence) if wrist else 0.0,
                }
            )
        return out

    gate._hands.hands = stub_hands  # type: ignore[assignment]
    return gate


# -- the promise --------------------------------------------------------------


def test_a_faithful_image_passes(tmp_path: Path) -> None:
    gate = build_gate(tmp_path, pose_for_image=pose_from(full_body_keypoints()))
    report = gate.inspect(photo(tmp_path))

    check = report.check("proportions")
    assert check.outcome is CheckOutcome.PASS
    assert not check.detail
    # The verdict is not asserted here: this profile has no identity centroid,
    # so identity is UNKNOWN and strict mode discards on that alone. Correct
    # behaviour, and a separate concern from the proportion measurement.


def test_a_slimmed_image_is_rejected(tmp_path: Path) -> None:
    """THE WHOLE POINT. She told us a tool had made her thinner without being
    asked. This is the code that stops it reaching her."""
    slimmed = full_body_keypoints()
    for name in ("left_hip", "right_hip", "left_shoulder", "right_shoulder"):
        x, y, c = slimmed[name]
        slimmed[name] = (320 + (x - 320) * 0.85, y, c)

    gate = build_gate(tmp_path, pose_for_image=pose_from(slimmed))

    # The measurement fires at BOTH stages - a slimmed preview is ranked down
    # and never presented as a good option.
    preview = gate.screen(photo(tmp_path))
    check = preview.check("proportions")
    assert check.outcome is CheckOutcome.FAIL
    # And it says WHAT changed, not just that something did.
    assert "mas estrecho" in check.detail
    assert "%" in check.detail

    # ...and the stage that DELIVERS refuses it. That is where "stops it
    # reaching her" is enforced, because a preview is a proposal she chooses
    # between and a final is a photograph she receives.
    #
    # The distinction had to be drawn: with identity enrolled, the free
    # preview model scores ~0.49 against her centroid where her own photos
    # score 0.83-0.87, so vetoing at preview stage discarded every candidate
    # and she was offered nothing at all - which does not protect her, it just
    # leaves her with no product.
    delivered = gate.inspect(photo(tmp_path))
    assert delivered.check("proportions").outcome is CheckOutcome.FAIL
    assert delivered.verdict is Verdict.DISCARD


def test_the_same_body_photographed_closer_still_passes(tmp_path: Path) -> None:
    """A generated image is a different crop and zoom. If that registered as a
    change, every image would be rejected and the check would be worthless."""
    gate = build_gate(
        tmp_path, pose_for_image=pose_from(full_body_keypoints(scale=1.6, offset=40))
    )
    report = gate.screen(photo(tmp_path))
    assert report.check("proportions").outcome is CheckOutcome.PASS


def test_a_small_change_within_tolerance_passes(tmp_path: Path) -> None:
    """Pose estimation is noisy. A 2% wobble is measurement error, not a body
    that was reshaped, and rejecting it would burn her money on regenerations
    that were never wrong."""
    barely = full_body_keypoints()
    for name in ("left_hip", "right_hip"):
        x, y, c = barely[name]
        barely[name] = (320 + (x - 320) * 0.98, y, c)

    gate = build_gate(tmp_path, pose_for_image=pose_from(barely))
    assert gate.screen(photo(tmp_path)).check("proportions").outcome is CheckOutcome.PASS


def test_a_widened_body_is_rejected_too(tmp_path: Path) -> None:
    """The check is symmetric. "Only what I asked for" cuts both ways - an
    unrequested change is unrequested in either direction."""
    widened = full_body_keypoints()
    for name in ("left_hip", "right_hip"):
        x, y, c = widened[name]
        widened[name] = (320 + (x - 320) * 1.2, y, c)

    gate = build_gate(tmp_path, pose_for_image=pose_from(widened))
    report = gate.screen(photo(tmp_path))
    assert report.check("proportions").outcome is CheckOutcome.FAIL
    assert "mas ancho" in report.check("proportions").detail


# -- honesty when it cannot run -----------------------------------------------


def test_no_baseline_reports_unknown_and_blocks(tmp_path: Path) -> None:
    """Without a baseline the check cannot run, and in strict mode an
    unmeasurable check must block rather than wave the image through.

    Asserted through inspect(), not screen(). Strictness belongs to the stage
    that DELIVERS. screen() deliberately never blocks - a preview is a
    proposal she chooses between, and with identity enrolled the free preview
    model scored 0.49 against her centroid where her own photographs score
    0.83-0.87, so a strict screen discarded every candidate and the session
    produced nothing at all.
    """
    profile = IdentityProfile(owner="test")  # no proportions measured
    gate = Gate(
        profile=profile,
        thresholds=Thresholds(min_sharpness=0.0),
        models_dir=tmp_path / "models",
        strict=True,
    )
    report = gate.inspect(photo(tmp_path))

    check = report.check("proportions")
    assert check.outcome is CheckOutcome.UNKNOWN
    assert "linea base" in check.detail
    assert report.verdict is Verdict.DISCARD


def test_no_person_in_the_image_is_unknown_not_pass(tmp_path: Path) -> None:
    from app.gate.backends import ModelUnavailable

    gate = build_gate(tmp_path, pose_for_image=pose_from(full_body_keypoints()))

    def no_person(path):
        raise ModelUnavailable("no person found in the image")

    gate._pose.proportions = no_person  # type: ignore[assignment]
    # inspect(), because that is the stage where an unmeasurable check must
    # discard - see the note on Gate.screen.
    report = gate.inspect(photo(tmp_path))

    assert report.check("proportions").outcome is CheckOutcome.UNKNOWN
    assert report.verdict is Verdict.DISCARD


def test_a_close_up_with_no_hips_is_unknown_not_pass(tmp_path: Path) -> None:
    """Keypoints found, but nothing comparable came out of them. Unknown, not
    fine - a close-up cannot testify about a body it does not show."""
    head_only = {
        name: value
        for name, value in full_body_keypoints().items()
        if name in {"nose", "left_eye", "right_eye", "left_ear", "right_ear"}
    }
    gate = build_gate(tmp_path, pose_for_image=pose_from(head_only))
    report = gate.screen(photo(tmp_path))

    check = report.check("proportions")
    assert check.outcome is CheckOutcome.UNKNOWN
    assert "comparables" in check.detail


# -- hands --------------------------------------------------------------------


def test_hands_are_located_so_repair_has_a_target(tmp_path: Path) -> None:
    """This does not count fingers - COCO-17 has wrists, not digits. It gives
    the repair loop a region, which is what it actually needs."""
    gate = build_gate(tmp_path, pose_for_image=pose_from(full_body_keypoints()))
    report = gate.screen(photo(tmp_path))

    hands = report.check("hands")
    assert hands.outcome is CheckOutcome.PASS
    assert hands.value is not None and hands.value > 0.5


def test_an_uncertain_wrist_is_flagged_with_a_box(tmp_path: Path) -> None:
    """Pose models lose the wrist precisely where the hand is mangled, because
    a mangled hand does not look like a hand. The low confidence is the
    signal, and the box is what gets repainted."""
    keypoints = full_body_keypoints()
    keypoints["left_wrist"] = (*keypoints["left_wrist"][:2], 0.40)

    gate = build_gate(tmp_path, pose_for_image=pose_from(keypoints))
    report = gate.screen(photo(tmp_path))

    assert report.check("hands").outcome is CheckOutcome.FAIL
    defects = [d for d in report.defects if d.kind.value == "hand"]
    assert defects and defects[0].bbox is not None
    assert defects[0].is_repairable, "a located hand defect must be repairable"


# ---------------------------------------------------------------------------
# Pose is not body shape
# ---------------------------------------------------------------------------


def test_drift_ignores_limb_ratios() -> None:
    """The check would have rejected her own unaltered photograph.

    Measured on two photographs of Nayane taken the same day: every
    width-over-length ratio agreed to within 3.9%, well inside the 6%
    threshold - and limb:forearm_r differed by 35.65%, purely because her arm
    was at a different angle. max() over everything reported 0.3565 and would
    have blamed the generator for foreshortening.

    The width-over-length ratios ARE the anti-slimming measure. They were
    chosen because they survive pose; including limbs in the maximum threw
    that property away.
    """
    from app.contracts.attribute_ir import BodyProportions

    a = BodyProportions(
        shoulder_torso_ratio=0.86, hip_torso_ratio=0.59,
        limb_ratios={"forearm_r": 0.42},
    )
    b = BodyProportions(
        shoulder_torso_ratio=0.86, hip_torso_ratio=0.59,
        limb_ratios={"forearm_r": 0.27},   # 35% shorter: a different arm angle
    )
    drift = a.max_relative_drift(b)
    assert drift is not None
    assert drift < 0.01, f"pose leaked into the body-shape measure: {drift:.4f}"


def test_a_real_narrowing_is_still_caught_without_limbs() -> None:
    """Removing limbs must not make the check blind - that would be worse than
    the false rejection it fixes."""
    from app.contracts.attribute_ir import BodyProportions

    real = BodyProportions(shoulder_torso_ratio=0.86, hip_torso_ratio=0.59)
    slimmed = BodyProportions(shoulder_torso_ratio=0.73, hip_torso_ratio=0.50)
    assert real.max_relative_drift(slimmed) > 0.06


def test_nothing_comparable_is_unknown_not_fine() -> None:
    from app.contracts.attribute_ir import BodyProportions

    assert BodyProportions().max_relative_drift(BodyProportions()) is None


# ---------------------------------------------------------------------------
# A comparison across different framings is not a comparison
# ---------------------------------------------------------------------------


def test_a_reframed_generation_is_unknown_not_failed() -> None:
    """The measurement that nearly shipped a lie.

    Width-over-length ratios are sensitive to camera distance, and the
    generator reframes. A real generation moved her torso from 34% to 49% of
    the frame and measured 31.7% "narrower", every ratio moving the same way -
    which by the numbers alone is indistinguishable from real slimming.

    Reported as FAIL it would have blamed the tool for its own crop. Reported
    as UNKNOWN it is honest: the comparison could not be made. That is the
    whole discipline of this gate.
    """
    import inspect

    from app.gate.gate import Gate

    source = inspect.getsource(Gate._check_proportions)
    assert "_same_framing" in source
    assert "CheckOutcome.UNKNOWN" in source

    framing = inspect.getsource(Gate._same_framing)
    for landmark in ("left_hip", "left_knee", "left_ankle"):
        assert landmark in framing, "framing must compare which parts are visible"
    assert "torso_span" in framing, "framing must compare apparent camera distance"


def test_the_torso_ratios_are_still_compared() -> None:
    """They are the only measure that catches uniform slimming - the client's
    actual complaint - so removing them to stop the false failures would have
    thrown away the point of the check.

    An earlier attempt did exactly that, and three tests encoding the
    requirement failed. They were right and the change was wrong.
    """
    from app.contracts.attribute_ir import BodyProportions

    a = BodyProportions(shoulder_hip_ratio=1.46, shoulder_torso_ratio=0.86,
                        hip_torso_ratio=0.59)
    # Everything narrowed by 15%: shoulder_hip barely moves, torso ratios do.
    b = BodyProportions(shoulder_hip_ratio=1.46, shoulder_torso_ratio=0.731,
                        hip_torso_ratio=0.501)
    assert a.max_relative_drift(b) > 0.10


def test_the_threshold_accepts_her_own_photographs() -> None:
    """6% was chosen perceptually and never checked against the measurement's
    own repeatability. Her own drift from her own baseline ranges 3.7-13.3%,
    so at 6% the check rejected 6 of her 10 measurable photographs.

    A threshold below the noise floor does not make a check strict, it makes
    it wrong.
    """
    from app.config import Thresholds

    assert Thresholds().proportion_drift >= 0.14, (
        "threshold is back below her own measured variation"
    )


def test_a_preview_is_offered_even_when_it_cannot_be_verified(tmp_path: Path) -> None:
    """The counterpart, and it needs asserting or the change above reads as a
    loosening of the guarantee.

    screen() must NOT block. With identity enrolled and the gate strict, the
    free preview model scored 0.49 against her centroid - her own photographs
    score 0.83-0.87 - so every preview was discarded and the session produced
    nothing whatever to choose from. The final stage, which CAN preserve her,
    was never reached.

    Every check still runs and every result is still recorded. What changes is
    that an unverifiable preview is offered rather than destroyed.
    """
    gate = Gate(
        profile=IdentityProfile(owner="test"),
        thresholds=Thresholds(min_sharpness=0.0),
        models_dir=tmp_path / "models",
        strict=True,
    )
    report = gate.screen(photo(tmp_path))

    assert report.check("proportions").outcome is CheckOutcome.UNKNOWN
    assert report.verdict is not Verdict.DISCARD, (
        "a strict screen destroys every candidate and the session delivers nothing"
    )
    assert gate.strict is True, "screen must not leave the gate permanently relaxed"
