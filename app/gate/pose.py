"""Body keypoints, and the proportions derived from them.

THIS IS THE ANTI-SLIMMING CHECK. It is the reason the project exists in the
shape it does: she told us an earlier tool had made her look thinner without
being asked, and every generator is instructed on every call not to. Words in
a prompt are a request. This file is the enforcement.

Two halves:

  DECODING      YOLOv8-pose ONNX output -> 17 COCO keypoints. Pure numpy, so
                it is testable against synthetic tensors without the weights.

  PROPORTIONS   keypoints -> ratios, ALL NORMALISED BY TORSO LENGTH.

That normalisation is the whole trick. A generated image is a different crop,
a different zoom and often a different pose, so raw pixel distances are
meaningless between the source and the result. Ratios against the torso are
invariant to all three, which means a change in one is a change in HER, not in
the framing.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from app.contracts.attribute_ir import BodyProportions

#: COCO-17, in the order YOLOv8-pose emits them.
KEYPOINT_NAMES: tuple[str, ...] = (
    "nose", "left_eye", "right_eye", "left_ear", "right_ear",
    "left_shoulder", "right_shoulder", "left_elbow", "right_elbow",
    "left_wrist", "right_wrist", "left_hip", "right_hip",
    "left_knee", "right_knee", "left_ankle", "right_ankle",
)
KEYPOINT_INDEX = {name: i for i, name in enumerate(KEYPOINT_NAMES)}

#: Below this a keypoint is treated as absent rather than approximate. A
#: hallucinated hip position would produce a confident, wrong proportion - far
#: worse than reporting that we could not measure.
MIN_KEYPOINT_CONFIDENCE = 0.35

MODEL_INPUT_SIZE = 640


@dataclass(frozen=True)
class Keypoint:
    x: float  # normalised 0..1 against the original image
    y: float
    confidence: float

    @property
    def ok(self) -> bool:
        return self.confidence >= MIN_KEYPOINT_CONFIDENCE


@dataclass(frozen=True)
class Pose:
    """One detected person."""

    keypoints: dict[str, Keypoint]
    box_confidence: float
    #: height / width of the ORIGINAL frame.
    #:
    #: Keypoint x is normalised against image width and y against image height,
    #: which is right for bounding boxes and wrong for lengths: a horizontal
    #: distance comes out in units of width and a vertical one in units of
    #: height. Every ratio in measure() divides a mostly-horizontal distance
    #: (shoulder width, hip width) by a mostly-vertical one (torso length), so
    #: without this the answer moves with the frame's shape.
    #:
    #: Measured, not theorised: padding one of her photos to 4:5 with black
    #: bars - not one body pixel altered - moved shoulder_torso_ratio by -33%
    #: against a 6% threshold. The anti-slimming check would have accused the
    #: generator of the exact thing it exists to prevent, and the accusation
    #: would have looked entirely credible.
    aspect: float = 1.0

    def get(self, name: str) -> Keypoint | None:
        kp = self.keypoints.get(name)
        return kp if kp is not None and kp.ok else None

    def midpoint(self, a: str, b: str) -> tuple[float, float] | None:
        ka, kb = self.get(a), self.get(b)
        if ka is None or kb is None:
            return None
        return ((ka.x + kb.x) / 2, (ka.y + kb.y) / 2)

    def span(self, p: tuple[float, float], q: tuple[float, float]) -> float:
        """Isotropic distance between two normalised points.

        Same correction as distance(), for the callers that work with
        midpoints rather than named keypoints. It exists so there is ONE place
        that knows y needs scaling: torso_length and height_in_heads each did
        their own raw hypot, and torso_length is the denominator of every
        ratio in measure(), so it carried the error into all of them.
        """
        return float(np.hypot(p[0] - q[0], (p[1] - q[1]) * self.aspect))

    def distance(self, a: str, b: str) -> float | None:
        """Isotropic distance, in units of image WIDTH.

        dy is scaled by the frame aspect so both axes share one unit. Which
        unit does not matter - every consumer uses these as ratios against
        torso length - but that they MATCH matters entirely.
        """
        ka, kb = self.get(a), self.get(b)
        if ka is None or kb is None:
            return None
        return float(np.hypot(ka.x - kb.x, (ka.y - kb.y) * self.aspect))


# ---------------------------------------------------------------------------
# Letterbox
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Letterbox:
    """How the image was fitted into the square model input.

    Kept so coordinates can be mapped back. Getting this wrong does not throw -
    it silently shifts every keypoint, which would then read as a body that
    changed shape. It is the most dangerous small mistake in this file, which
    is why it is its own type with its own tests.
    """

    scale: float
    pad_x: float
    pad_y: float
    original_width: int
    original_height: int

    @classmethod
    def fit(cls, width: int, height: int, size: int = MODEL_INPUT_SIZE) -> "Letterbox":
        scale = min(size / width, size / height)
        return cls(
            scale=scale,
            pad_x=(size - width * scale) / 2,
            pad_y=(size - height * scale) / 2,
            original_width=width,
            original_height=height,
        )

    def to_normalised(self, x: float, y: float) -> tuple[float, float]:
        """Model-input pixels -> 0..1 in the ORIGINAL image."""
        ox = (x - self.pad_x) / self.scale
        oy = (y - self.pad_y) / self.scale
        return ox / self.original_width, oy / self.original_height


def letterbox_image(rgb: np.ndarray, size: int = MODEL_INPUT_SIZE) -> tuple[np.ndarray, Letterbox]:
    """Resize preserving aspect ratio, pad to square, return NCHW float32."""
    height, width = rgb.shape[:2]
    box = Letterbox.fit(width, height, size)

    new_w = max(1, int(round(width * box.scale)))
    new_h = max(1, int(round(height * box.scale)))

    from PIL import Image

    resized = np.asarray(
        Image.fromarray((np.clip(rgb, 0, 1) * 255).astype(np.uint8)).resize(
            (new_w, new_h), Image.BILINEAR
        ),
        dtype=np.float32,
    ) / 255.0

    # 0.447 is the conventional grey pad for this family of detectors; a black
    # pad reads as a hard edge and can invent detections along it.
    canvas = np.full((size, size, 3), 0.447, dtype=np.float32)
    top = int(round(box.pad_y))
    left = int(round(box.pad_x))
    canvas[top : top + new_h, left : left + new_w] = resized

    return np.ascontiguousarray(canvas.transpose(2, 0, 1)[None, ...]), box


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------


def _iou(box: np.ndarray, boxes: np.ndarray) -> np.ndarray:
    """IoU of one xyxy box against many."""
    x0 = np.maximum(box[0], boxes[:, 0])
    y0 = np.maximum(box[1], boxes[:, 1])
    x1 = np.minimum(box[2], boxes[:, 2])
    y1 = np.minimum(box[3], boxes[:, 3])
    overlap = np.clip(x1 - x0, 0, None) * np.clip(y1 - y0, 0, None)

    area = (box[2] - box[0]) * (box[3] - box[1])
    areas = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
    return overlap / np.maximum(area + areas - overlap, 1e-9)


def non_max_suppression(
    boxes: np.ndarray, scores: np.ndarray, iou_threshold: float = 0.45
) -> list[int]:
    order = np.argsort(-scores)
    keep: list[int] = []
    while order.size:
        best = int(order[0])
        keep.append(best)
        if order.size == 1:
            break
        overlaps = _iou(boxes[best], boxes[order[1:]])
        order = order[1:][overlaps < iou_threshold]
    return keep


def decode_pose_output(
    output: np.ndarray,
    box: Letterbox,
    *,
    confidence_threshold: float = 0.35,
    iou_threshold: float = 0.45,
) -> list[Pose]:
    """YOLOv8-pose raw output -> poses, largest person first.

    Expected shape (1, 56, 8400):
        rows 0-3   cx, cy, w, h   in model-input pixels
        row  4     person confidence
        rows 5-55  17 keypoints as (x, y, confidence)

    Both (1, 56, N) and (1, N, 56) are accepted, because exporters disagree
    about which way round to emit it and a silent transpose would scramble
    every coordinate.
    """
    array = np.asarray(output)
    if array.ndim == 3:
        array = array[0]
    if array.ndim != 2:
        raise ValueError(f"unexpected pose output shape {np.asarray(output).shape}")

    # 56 rows and many columns is the documented layout; the transpose is the
    # other common export.
    if array.shape[0] != 56 and array.shape[1] == 56:
        array = array.T
    if array.shape[0] != 56:
        raise ValueError(
            f"expected 56 channels (4 box + 1 conf + 51 keypoint), got {array.shape[0]}"
        )

    predictions = array.T  # (N, 56)
    scores = predictions[:, 4]
    hits = predictions[scores >= confidence_threshold]
    if hits.size == 0:
        return []

    cx, cy, w, h = hits[:, 0], hits[:, 1], hits[:, 2], hits[:, 3]
    xyxy = np.stack([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2], axis=1)
    keep = non_max_suppression(xyxy, hits[:, 4], iou_threshold)

    poses: list[Pose] = []
    for index in keep:
        row = hits[index]
        raw = row[5:].reshape(17, 3)
        keypoints: dict[str, Keypoint] = {}
        for name, (kx, ky, kc) in zip(KEYPOINT_NAMES, raw):
            nx, ny = box.to_normalised(float(kx), float(ky))
            keypoints[name] = Keypoint(x=nx, y=ny, confidence=float(kc))
        poses.append(
            Pose(
                keypoints=keypoints,
                box_confidence=float(row[4]),
                aspect=box.original_height / box.original_width,
            )
        )

    # Largest person first: in her photos the subject is the biggest thing in
    # the frame, and a passer-by must never define her proportions.
    def area(index: int) -> float:
        row = hits[index]
        return float(row[2] * row[3])

    order = sorted(range(len(keep)), key=lambda i: -area(keep[i]))
    return [poses[i] for i in order]


# ---------------------------------------------------------------------------
# Proportions
# ---------------------------------------------------------------------------


def torso_length(pose: Pose) -> float | None:
    """Shoulder midpoint to hip midpoint.

    The reference every other measurement is divided by. Chosen because it is
    the most reliably detected span on a clothed body and it barely changes
    with pose, unlike anything involving limbs.
    """
    shoulders = pose.midpoint("left_shoulder", "right_shoulder")
    hips = pose.midpoint("left_hip", "right_hip")
    if shoulders is None or hips is None:
        return None
    length = pose.span(shoulders, hips)
    return length if length > 1e-4 else None


def measure(pose: Pose) -> BodyProportions:
    """Keypoints -> scale-invariant ratios.

    Every value is a ratio against torso length, so it survives a different
    crop, zoom or aspect ratio. A field that cannot be measured is left None
    rather than estimated - the gate treats None as "unknown", and unknown
    blocks. A confident wrong number here would wave through exactly the
    change this check exists to catch.
    """
    torso = torso_length(pose)
    if torso is None:
        return BodyProportions()

    shoulder_width = pose.distance("left_shoulder", "right_shoulder")
    hip_width = pose.distance("left_hip", "right_hip")
    ear_width = pose.distance("left_ear", "right_ear")
    eye_width = pose.distance("left_eye", "right_eye")

    limbs: dict[str, float] = {}
    for name, (a, b) in {
        "upper_arm_l": ("left_shoulder", "left_elbow"),
        "upper_arm_r": ("right_shoulder", "right_elbow"),
        "forearm_l": ("left_elbow", "left_wrist"),
        "forearm_r": ("right_elbow", "right_wrist"),
        "thigh_l": ("left_hip", "left_knee"),
        "thigh_r": ("right_hip", "right_knee"),
        "shin_l": ("left_knee", "left_ankle"),
        "shin_r": ("right_knee", "right_ankle"),
    }.items():
        distance = pose.distance(*b if isinstance(b, tuple) else (a, b))  # type: ignore[arg-type]
        if distance is not None:
            limbs[name] = distance / torso

    # Head scale, used for height-in-heads. Ears when both are visible, eyes
    # otherwise - eyes are narrower, so the factor differs and is applied
    # rather than pretending the two are interchangeable.
    head_width = ear_width if ear_width else (eye_width * 2.2 if eye_width else None)

    height_in_heads = None
    ankles = pose.midpoint("left_ankle", "right_ankle")
    nose = pose.get("nose")
    if head_width and ankles is not None and nose is not None:
        full_height = pose.span((nose.x, nose.y), ankles)
        # head_width * ~1.35 approximates head HEIGHT from its width.
        head_height = head_width * 1.35
        if head_height > 1e-4:
            height_in_heads = full_height / head_height

    return BodyProportions(
        # Catches disproportionate reshaping, and only that: both terms are
        # widths, so a body narrowed uniformly leaves this almost unmoved.
        shoulder_hip_ratio=(shoulder_width / hip_width) if shoulder_width and hip_width else None,
        # THE ACTUAL ANTI-SLIMMING MEASUREMENTS. A width over a LENGTH.
        # Slimming reduces width and leaves torso length alone, so these move
        # when she is made thinner and the ratio above does not.
        shoulder_torso_ratio=(shoulder_width / torso) if shoulder_width else None,
        hip_torso_ratio=(hip_width / torso) if hip_width else None,
        height_in_heads=height_in_heads,
        # No waist keypoint exists in COCO-17. Rather than invent one from an
        # interpolated hip-shoulder midpoint - which would move with clothing
        # and posture and mean nothing - it is left unmeasured.
        waist_hip_ratio=None,
        jaw_width_ratio=(ear_width / torso) if ear_width else None,
        limb_ratios=limbs,
    )


def hand_regions(pose: Pose) -> list[tuple[str, tuple[float, float, float, float]]]:
    """Where the hands are, as normalised boxes.

    COCO-17 has wrists, not fingers, so this cannot count fingers - that is
    the visual judge's job on finals. What it provides is the LOCATION, which
    is what the repair loop needs to inpaint a bad hand without touching the
    rest of the frame.

    The box is extrapolated past the wrist along the forearm, because a hand
    continues in the direction the arm was going.
    """
    regions: list[tuple[str, tuple[float, float, float, float]]] = []
    torso = torso_length(pose)
    if torso is None:
        return regions

    for side in ("left", "right"):
        wrist = pose.get(f"{side}_wrist")
        elbow = pose.get(f"{side}_elbow")
        if wrist is None:
            continue

        # A hand is roughly a third of a torso across; scaling from the torso
        # keeps the box right whether she is close up or full length.
        radius = torso * 0.33

        cx, cy = wrist.x, wrist.y
        if elbow is not None:
            dx, dy = wrist.x - elbow.x, wrist.y - elbow.y
            norm = float(np.hypot(dx, dy))
            if norm > 1e-6:
                cx += (dx / norm) * radius * 0.55
                cy += (dy / norm) * radius * 0.55

        regions.append(
            (
                f"{side}_hand",
                (
                    max(0.0, cx - radius),
                    max(0.0, cy - radius),
                    min(1.0, cx + radius),
                    min(1.0, cy + radius),
                ),
            )
        )
    return regions
