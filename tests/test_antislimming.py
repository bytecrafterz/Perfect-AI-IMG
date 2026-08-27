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
    report = gate.screen(photo(tmp_path))

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
    report = gate.screen(photo(tmp_path))

    check = report.check("proportions")
    assert check.outcome is CheckOutcome.FAIL
    assert report.verdict is Verdict.DISCARD
    # And it says WHAT changed, not just that something did.
    assert "mas estrecho" in check.detail
    assert "%" in check.detail


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
    unmeasurable check must block rather than wave the image through."""
    profile = IdentityProfile(owner="test")  # no proportions measured
    gate = Gate(
        profile=profile,
        thresholds=Thresholds(min_sharpness=0.0),
        models_dir=tmp_path / "models",
        strict=True,
    )
    report = gate.screen(photo(tmp_path))

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
    report = gate.screen(photo(tmp_path))

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
