"""The anti-slimming check, tested against synthetic tensors.

No model weights needed: the decoding and the proportion maths are pure
numpy, and they are where the mistakes live. A letterbox off by a few pixels
does not throw - it silently shifts every keypoint, which then reads as a body
that changed shape. That is the failure this file exists to prevent.

The measurements are all ratios against torso length, so the tests check the
property that actually matters: the same person photographed at a different
scale, crop or aspect ratio must measure the SAME.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.contracts.attribute_ir import BodyProportions
from app.gate.pose import (
    KEYPOINT_INDEX,
    KEYPOINT_NAMES,
    Keypoint,
    Letterbox,
    Pose,
    decode_pose_output,
    hand_regions,
    letterbox_image,
    measure,
    non_max_suppression,
    torso_length,
)

MODEL_SIZE = 640


# -- letterbox ----------------------------------------------------------------


def test_letterbox_maps_a_corner_back_to_itself() -> None:
    box = Letterbox.fit(width=800, height=1200)
    # The top-left of the real image sits at the padding offset in model space.
    x, y = box.to_normalised(box.pad_x, box.pad_y)
    assert x == pytest.approx(0.0, abs=1e-6)
    assert y == pytest.approx(0.0, abs=1e-6)


def test_letterbox_maps_the_far_corner_back_to_one() -> None:
    box = Letterbox.fit(width=800, height=1200)
    x, y = box.to_normalised(
        box.pad_x + 800 * box.scale, box.pad_y + 1200 * box.scale
    )
    assert x == pytest.approx(1.0, abs=1e-6)
    assert y == pytest.approx(1.0, abs=1e-6)


@pytest.mark.parametrize(
    "width,height", [(640, 640), (1920, 1080), (1080, 1920), (500, 1500), (3000, 400)]
)
def test_letterbox_round_trips_at_any_aspect_ratio(width: int, height: int) -> None:
    """Phone photos are portrait, generated images are often not. A mapping
    that only works on squares would corrupt every real measurement."""
    box = Letterbox.fit(width, height)
    for fx, fy in [(0.25, 0.25), (0.5, 0.5), (0.75, 0.1), (0.9, 0.9)]:
        model_x = box.pad_x + fx * width * box.scale
        model_y = box.pad_y + fy * height * box.scale
        x, y = box.to_normalised(model_x, model_y)
        assert x == pytest.approx(fx, abs=1e-6)
        assert y == pytest.approx(fy, abs=1e-6)


def test_letterbox_image_produces_the_tensor_the_model_expects() -> None:
    rgb = np.random.default_rng(0).random((1200, 800, 3)).astype(np.float32)
    tensor, box = letterbox_image(rgb)
    assert tensor.shape == (1, 3, MODEL_SIZE, MODEL_SIZE)
    assert tensor.dtype == np.float32
    assert 0.0 <= tensor.min() and tensor.max() <= 1.0
    assert box.original_width == 800 and box.original_height == 1200


# -- nms ----------------------------------------------------------------------


def test_nms_keeps_the_best_of_an_overlapping_cluster() -> None:
    boxes = np.array(
        [[0, 0, 10, 10], [1, 1, 11, 11], [100, 100, 110, 110]], dtype=np.float32
    )
    scores = np.array([0.9, 0.8, 0.7], dtype=np.float32)
    keep = non_max_suppression(boxes, scores)
    assert keep == [0, 2]


def test_nms_on_a_single_box() -> None:
    boxes = np.array([[0, 0, 10, 10]], dtype=np.float32)
    assert non_max_suppression(boxes, np.array([0.9])) == [0]


# -- decoding -----------------------------------------------------------------


def make_output(people: list[dict], *, transposed: bool = False) -> np.ndarray:
    """Build a YOLOv8-pose output tensor: (1, 56, N)."""
    rows = []
    for person in people:
        row = np.zeros(56, dtype=np.float32)
        row[0:4] = person["box"]  # cx, cy, w, h in model pixels
        row[4] = person["conf"]
        for name, (x, y, c) in person["keypoints"].items():
            base = 5 + KEYPOINT_INDEX[name] * 3
            row[base : base + 3] = (x, y, c)
        rows.append(row)

    # Pad out to a realistic number of anchors, all below threshold.
    while len(rows) < 20:
        rows.append(np.zeros(56, dtype=np.float32))

    array = np.stack(rows, axis=1)[None, ...]  # (1, 56, N)
    return array.transpose(0, 2, 1) if transposed else array


def full_body_keypoints(scale: float = 1.0, offset: float = 0.0) -> dict:
    """A plausible standing figure, in model-input pixels."""
    base = {
        "nose": (320, 80),
        "left_eye": (310, 74), "right_eye": (330, 74),
        "left_ear": (300, 78), "right_ear": (340, 78),
        "left_shoulder": (280, 140), "right_shoulder": (360, 140),
        "left_elbow": (260, 220), "right_elbow": (380, 220),
        "left_wrist": (250, 300), "right_wrist": (390, 300),
        "left_hip": (295, 300), "right_hip": (345, 300),
        "left_knee": (290, 420), "right_knee": (350, 420),
        "left_ankle": (288, 540), "right_ankle": (352, 540),
    }
    return {
        name: ((x - 320) * scale + 320 + offset, (y - 300) * scale + 300, 0.95)
        for name, (x, y) in base.items()
    }


def test_decode_reads_every_keypoint() -> None:
    output = make_output(
        [{"box": (320, 300, 200, 500), "conf": 0.9, "keypoints": full_body_keypoints()}]
    )
    poses = decode_pose_output(output, Letterbox.fit(640, 640))

    assert len(poses) == 1
    assert set(poses[0].keypoints) == set(KEYPOINT_NAMES)
    assert poses[0].box_confidence == pytest.approx(0.9)


def test_decode_accepts_the_transposed_export() -> None:
    """Exporters disagree about which way round to emit this. A silent
    transpose would scramble every coordinate."""
    people = [{"box": (320, 300, 200, 500), "conf": 0.9, "keypoints": full_body_keypoints()}]
    normal = decode_pose_output(make_output(people), Letterbox.fit(640, 640))
    flipped = decode_pose_output(
        make_output(people, transposed=True), Letterbox.fit(640, 640)
    )
    assert normal[0].keypoints["nose"].x == pytest.approx(flipped[0].keypoints["nose"].x)


def test_decode_returns_nothing_when_nobody_is_confident() -> None:
    output = make_output(
        [{"box": (320, 300, 200, 500), "conf": 0.05, "keypoints": full_body_keypoints()}]
    )
    assert decode_pose_output(output, Letterbox.fit(640, 640)) == []


def test_the_biggest_person_comes_first() -> None:
    """In her photos she is the subject. A passer-by must never define her
    proportions."""
    output = make_output(
        [
            {"box": (100, 300, 40, 90), "conf": 0.9, "keypoints": full_body_keypoints(0.2)},
            {"box": (400, 300, 220, 520), "conf": 0.85, "keypoints": full_body_keypoints()},
        ]
    )
    poses = decode_pose_output(output, Letterbox.fit(640, 640))
    assert len(poses) == 2
    subject, passerby = poses
    assert subject.distance("left_shoulder", "right_shoulder") > passerby.distance(
        "left_shoulder", "right_shoulder"
    )


def test_a_wrong_shape_is_rejected_rather_than_misread() -> None:
    with pytest.raises(ValueError, match="56"):
        decode_pose_output(np.zeros((1, 40, 100), dtype=np.float32), Letterbox.fit(640, 640))


# -- proportions --------------------------------------------------------------


def pose_from(keypoints: dict) -> Pose:
    return Pose(
        keypoints={n: Keypoint(x=x / 640, y=y / 640, confidence=c)
                   for n, (x, y, c) in keypoints.items()},
        box_confidence=0.9,
    )


def test_measurements_are_invariant_to_scale() -> None:
    """THE PROPERTY THE WHOLE CHECK RESTS ON.

    A generated image is a different crop and zoom. If measurements moved with
    scale, every image would look like a body that changed, and the check
    would be worthless.
    """
    small = measure(pose_from(full_body_keypoints(scale=0.5)))
    large = measure(pose_from(full_body_keypoints(scale=1.5)))

    assert small.shoulder_hip_ratio == pytest.approx(large.shoulder_hip_ratio, rel=1e-6)
    assert small.jaw_width_ratio == pytest.approx(large.jaw_width_ratio, rel=1e-6)
    assert small.height_in_heads == pytest.approx(large.height_in_heads, rel=1e-6)
    assert small.max_relative_drift(large) == pytest.approx(0.0, abs=1e-6)


def test_measurements_are_invariant_to_position() -> None:
    left = measure(pose_from(full_body_keypoints(offset=-120)))
    right = measure(pose_from(full_body_keypoints(offset=120)))
    assert left.max_relative_drift(right) == pytest.approx(0.0, abs=1e-6)


def test_low_confidence_keypoints_are_treated_as_absent() -> None:
    """A hallucinated hip would produce a confident, wrong proportion - far
    worse than reporting that we could not measure."""
    keypoints = full_body_keypoints()
    keypoints["left_hip"] = (*keypoints["left_hip"][:2], 0.05)
    keypoints["right_hip"] = (*keypoints["right_hip"][:2], 0.05)

    pose = pose_from(keypoints)
    assert torso_length(pose) is None
    assert measure(pose).shoulder_hip_ratio is None


def test_a_close_up_reports_nothing_rather_than_guessing() -> None:
    keypoints = {
        name: value
        for name, value in full_body_keypoints().items()
        if name in {"nose", "left_eye", "right_eye", "left_ear", "right_ear"}
    }
    proportions = measure(pose_from(keypoints))
    assert proportions.shoulder_hip_ratio is None
    assert proportions.height_in_heads is None


def test_waist_is_left_unmeasured_rather_than_invented() -> None:
    """COCO-17 has no waist keypoint. Interpolating one from hips and
    shoulders would move with clothing and posture and mean nothing."""
    assert measure(pose_from(full_body_keypoints())).waist_hip_ratio is None


# -- the check that catches the complaint -------------------------------------


def test_slimming_is_detected() -> None:
    """The exact thing she complained about: a tool made her narrower without
    being asked."""
    baseline = measure(pose_from(full_body_keypoints()))

    slimmed = full_body_keypoints()
    for name in ("left_hip", "right_hip", "left_shoulder", "right_shoulder"):
        x, y, c = slimmed[name]
        slimmed[name] = (320 + (x - 320) * 0.85, y, c)  # 15% narrower body
    measured = measure(pose_from(slimmed))

    drift = baseline.max_relative_drift(measured)
    assert drift is not None
    assert drift > 0.06, f"a 15% narrowing must exceed the 6% threshold, got {drift:.1%}"


def test_a_narrowed_face_is_detected() -> None:
    baseline = measure(pose_from(full_body_keypoints()))

    narrowed = full_body_keypoints()
    for name in ("left_ear", "right_ear"):
        x, y, c = narrowed[name]
        narrowed[name] = (320 + (x - 320) * 0.8, y, c)
    measured = measure(pose_from(narrowed))

    assert baseline.jaw_width_ratio > measured.jaw_width_ratio
    assert baseline.max_relative_drift(measured) > 0.06


def test_the_same_body_in_a_different_pose_still_passes() -> None:
    """The check must not fire just because she moved. An arm that swings
    changes limb ratios slightly; a body that was reshaped does not.
    """
    baseline_kp = full_body_keypoints()
    baseline = measure(pose_from(baseline_kp))

    moved = dict(baseline_kp)
    # Arms raised: elbows and wrists move a long way, torso and hips do not.
    moved["left_elbow"] = (250, 160, 0.9)
    moved["right_elbow"] = (390, 160, 0.9)
    moved["left_wrist"] = (240, 100, 0.9)
    moved["right_wrist"] = (400, 100, 0.9)

    measured = measure(pose_from(moved))
    assert baseline.shoulder_hip_ratio == pytest.approx(measured.shoulder_hip_ratio, rel=1e-6)
    assert baseline.jaw_width_ratio == pytest.approx(measured.jaw_width_ratio, rel=1e-6)


def test_an_empty_baseline_reports_unknown_not_fine() -> None:
    empty = BodyProportions()
    assert empty.max_relative_drift(measure(pose_from(full_body_keypoints()))) is None


# -- hand regions -------------------------------------------------------------


def test_hands_are_located_for_the_repair_loop() -> None:
    regions = hand_regions(pose_from(full_body_keypoints()))
    assert {name for name, _ in regions} == {"left_hand", "right_hand"}
    for _, (x0, y0, x1, y1) in regions:
        assert 0.0 <= x0 < x1 <= 1.0
        assert 0.0 <= y0 < y1 <= 1.0


def test_a_hand_box_sits_beyond_the_wrist() -> None:
    """A hand continues in the direction the forearm was going, so the box is
    extrapolated past the wrist rather than centred on it."""
    pose = pose_from(full_body_keypoints())
    regions = dict(hand_regions(pose))
    x0, y0, x1, y1 = regions["left_hand"]
    centre_y = (y0 + y1) / 2
    assert centre_y > pose.keypoints["left_wrist"].y


def test_no_wrist_means_no_hand_region() -> None:
    keypoints = full_body_keypoints()
    for name in ("left_wrist", "right_wrist"):
        x, y, _ = keypoints[name]
        keypoints[name] = (x, y, 0.02)
    assert hand_regions(pose_from(keypoints)) == []


def test_uniform_slimming_is_detected_even_though_the_hip_ratio_is_blind() -> None:
    """The gap that a live-shaped test found.

    Narrowing shoulders AND hips together barely moves shoulder_hip_ratio -
    both terms shrink, so the ratio survives. That is exactly the failure she
    complained about: made thinner overall, not reshaped. It is caught by
    measuring width against torso LENGTH, which slimming does not touch.
    """
    baseline = measure(pose_from(full_body_keypoints()))

    slimmed_kp = full_body_keypoints()
    for name in ("left_hip", "right_hip", "left_shoulder", "right_shoulder"):
        x, y, c = slimmed_kp[name]
        slimmed_kp[name] = (320 + (x - 320) * 0.85, y, c)
    slimmed = measure(pose_from(slimmed_kp))

    # The blind measure stays put - this is the point.
    assert baseline.shoulder_hip_ratio == pytest.approx(
        slimmed.shoulder_hip_ratio, rel=0.03
    )
    # The width-over-length measures move by the full 15%.
    assert slimmed.shoulder_torso_ratio < baseline.shoulder_torso_ratio
    assert slimmed.hip_torso_ratio < baseline.hip_torso_ratio
    assert baseline.max_relative_drift(slimmed) > 0.06
