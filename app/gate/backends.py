"""Image measurement primitives, and honest capability reporting.

Two tiers of measurement:

  ALWAYS AVAILABLE   sharpness, exposure, SSIM, CIELAB skin distance.
                     Pure numpy + Pillow, no native models, works anywhere.

  NEEDS MODELS       face identity (ArcFace), body proportions (pose
                     keypoints), hand integrity (hand keypoints).
                     Require onnxruntime + insightface + downloaded weights.

When the model tier is unavailable, the corresponding checks report UNKNOWN.
They never report PASS.  This matters more than it looks: the gate is the only
approver of finals, so a check that cannot run must block the image rather
than wave it through, and the operator must be able to see that identity
verification is off.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

# ---------------------------------------------------------------------------
# Capability detection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CVCapabilities:
    onnxruntime: bool
    insightface: bool
    opencv: bool
    face_model: bool
    pose_model: bool
    #: Optional. A dedicated hand-landmark model would allow finger COUNTING,
    #: which pose cannot do. Not required: hand LOCATION comes from the pose
    #: wrists, and whether a hand is broken is the visual judge's call.
    hand_model: bool = False

    @property
    def identity_available(self) -> bool:
        return self.insightface and self.face_model

    @property
    def face_detector_available(self) -> bool:
        """Whether skin can be sampled from a real face rather than guessed.

        Same requirement as identity today, because the detector arrives with
        insightface.  Kept separate because they are different questions: the
        skin check needs a BOX, not an embedding, and if a lighter detector is
        ever added this is the flag that should follow it.
        """
        return self.insightface and self.face_model

    @property
    def proportions_available(self) -> bool:
        return self.onnxruntime and self.pose_model

    @property
    def hands_available(self) -> bool:
        # Same requirement as proportions: COCO-17 pose gives the wrists, and
        # a located hand is what the repair loop needs.
        return self.proportions_available

    @property
    def fully_available(self) -> bool:
        return self.identity_available and self.proportions_available

    def missing_es(self) -> list[str]:
        out: list[str] = []
        if not self.identity_available:
            out.append(
                "verificacion de identidad no disponible "
                "(falta insightface o el modelo buffalo_l)"
            )
        if not self.proportions_available:
            out.append(
                "verificacion de proporciones y manos no disponible "
                "(falta onnxruntime o el modelo de pose) - "
                "EL CONTROL ANTI-ADELGAZAMIENTO NO ESTA ACTIVO"
            )
        return out


def _module_available(name: str) -> bool:
    try:
        __import__(name)
        return True
    except Exception:
        return False


@functools.lru_cache(maxsize=1)
def detect_capabilities(models_dir: Path | None = None) -> CVCapabilities:
    models_dir = models_dir or Path("data/models")
    return CVCapabilities(
        onnxruntime=_module_available("onnxruntime"),
        insightface=_module_available("insightface"),
        opencv=_module_available("cv2"),
        face_model=(models_dir / "buffalo_l").exists(),
        pose_model=(models_dir / "yolov8n-pose.onnx").exists(),
        hand_model=False,  # optional; finger counting is not implemented
    )


# ---------------------------------------------------------------------------
# Always-available measurements
# ---------------------------------------------------------------------------


def load_rgb(path: str | Path, *, max_side: int = 1024) -> np.ndarray:
    """Load as float RGB in [0, 1], upright, downscaled for speed.

    Every metric here is scale-tolerant, and capping the long side keeps the
    CPU budget inside the latency target on a 4-core box.

    EXIF ROTATION IS APPLIED FIRST, and it is not a nicety. A phone shoots in
    portrait and stores the pixels landscape with an Orientation tag; read
    raw, the image arrives on its side. Sharpness and exposure survive that,
    but the skin patches are sampled from fixed regions that assume an upright
    subject, and pose keypoints would be decoded against a rotated frame. Both
    would produce confident, meaningless numbers rather than an error.
    """
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        im = im.convert("RGB")
        if max(im.size) > max_side:
            scale = max_side / max(im.size)
            im = im.resize(
                (max(1, int(im.width * scale)), max(1, int(im.height * scale))),
                Image.LANCZOS,
            )
        return np.asarray(im, dtype=np.float32) / 255.0


def to_luma(rgb: np.ndarray) -> np.ndarray:
    return rgb @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)


def sharpness(rgb: np.ndarray) -> float:
    """Normalised Laplacian variance.

    Used to reject a blurred profile photo, and to catch a generator that
    returned mush.  Normalised to roughly [0, 1] for a threshold that reads
    the same across resolutions.
    """
    luma = to_luma(rgb)
    lap = (
        -4.0 * luma[1:-1, 1:-1]
        + luma[:-2, 1:-1]
        + luma[2:, 1:-1]
        + luma[1:-1, :-2]
        + luma[1:-1, 2:]
    )
    variance = float(np.var(lap))
    # 0.002 is empirically about the boundary between soft and acceptably
    # sharp for phone photos; the calibration stage replaces this constant.
    return float(min(1.0, variance / 0.002))


def exposure(rgb: np.ndarray) -> tuple[float, float]:
    """Mean luma and clipped fraction.  Catches a black frame or a blowout."""
    luma = to_luma(rgb)
    clipped = float(np.mean((luma < 0.02) | (luma > 0.98)))
    return float(np.mean(luma)), clipped


def _box_filter(x: np.ndarray, radius: int) -> np.ndarray:
    """Mean over a (2r+1)^2 window via an integral image - O(n) regardless of
    window size, which keeps SSIM cheap enough to run on every candidate."""
    pad = radius + 1
    padded = np.pad(x, pad, mode="edge")
    integral = padded.cumsum(axis=0).cumsum(axis=1)
    integral = np.pad(integral, ((1, 0), (1, 0)), mode="constant")

    h, w = x.shape
    y0, y1 = pad - radius, pad + radius + 1
    x0, x1 = pad - radius, pad + radius + 1
    total = (
        integral[y1 : y1 + h, x1 : x1 + w]
        - integral[y0 : y0 + h, x1 : x1 + w]
        - integral[y1 : y1 + h, x0 : x0 + w]
        + integral[y0 : y0 + h, x0 : x0 + w]
    )
    return total / ((2 * radius + 1) ** 2)


def ssim(a: np.ndarray, b: np.ndarray, *, radius: int = 4) -> float:
    """Mean SSIM over the luma channel.

    This is the change-containment check: measured OUTSIDE the region the
    request was allowed to touch, it answers "did the generator alter
    something nobody asked it to?"
    """
    if a.shape != b.shape:
        b = np.asarray(
            Image.fromarray((b * 255).astype(np.uint8)).resize(
                (a.shape[1], a.shape[0]), Image.LANCZOS
            ),
            dtype=np.float32,
        ) / 255.0

    x, y = to_luma(a), to_luma(b)
    c1, c2 = 0.01**2, 0.03**2

    mu_x, mu_y = _box_filter(x, radius), _box_filter(y, radius)
    mu_x2, mu_y2, mu_xy = mu_x * mu_x, mu_y * mu_y, mu_x * mu_y
    sigma_x = _box_filter(x * x, radius) - mu_x2
    sigma_y = _box_filter(y * y, radius) - mu_y2
    sigma_xy = _box_filter(x * y, radius) - mu_xy

    numerator = (2 * mu_xy + c1) * (2 * sigma_xy + c2)
    denominator = (mu_x2 + mu_y2 + c1) * (sigma_x + sigma_y + c2)
    return float(np.mean(numerator / np.maximum(denominator, 1e-12)))


def ssim_outside(
    a: np.ndarray, b: np.ndarray, mask: np.ndarray | None, *, radius: int = 4
) -> float:
    """SSIM restricted to pixels the request was NOT allowed to change.

    With no mask - a whole-frame regeneration - everything is fair game and
    containment is not a meaningful question, so it returns 1.0 rather than
    inventing a failure.
    """
    if mask is None:
        return 1.0
    if a.shape != b.shape:
        return ssim(a, b, radius=radius)

    x, y = to_luma(a), to_luma(b)
    c1, c2 = 0.01**2, 0.03**2
    mu_x, mu_y = _box_filter(x, radius), _box_filter(y, radius)
    sigma_x = _box_filter(x * x, radius) - mu_x * mu_x
    sigma_y = _box_filter(y * y, radius) - mu_y * mu_y
    sigma_xy = _box_filter(x * y, radius) - mu_x * mu_y
    smap = ((2 * mu_x * mu_y + c1) * (2 * sigma_xy + c2)) / np.maximum(
        (mu_x * mu_x + mu_y * mu_y + c1) * (sigma_x + sigma_y + c2), 1e-12
    )

    outside = mask < 0.5
    if not bool(outside.any()):
        return 1.0
    return float(np.mean(smap[outside]))


# -- CIELAB ------------------------------------------------------------------


def _srgb_to_linear(c: np.ndarray) -> np.ndarray:
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def rgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    """sRGB (D65) to CIELAB.

    Lab rather than RGB because perceptual distance in Lab is what a person
    actually notices - which is the only useful definition of "her skin tone
    changed".
    """
    linear = _srgb_to_linear(np.clip(rgb, 0.0, 1.0))
    matrix = np.array(
        [
            [0.4124564, 0.3575761, 0.1804375],
            [0.2126729, 0.7151522, 0.0721750],
            [0.0193339, 0.1191920, 0.9503041],
        ],
        dtype=np.float32,
    )
    xyz = linear @ matrix.T
    white = np.array([0.95047, 1.00000, 1.08883], dtype=np.float32)
    xyz = xyz / white

    eps, kappa = 216 / 24389, 24389 / 27
    f = np.where(xyz > eps, np.cbrt(np.maximum(xyz, 1e-12)), (kappa * xyz + 16) / 116)
    fx, fy, fz = f[..., 0], f[..., 1], f[..., 2]
    return np.stack([116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)], axis=-1)


def skin_patches(rgb: np.ndarray, boxes: list[tuple[float, float, float, float]] | None = None) -> np.ndarray:
    """Mean Lab over sampling patches.

    Without a face detector we fall back to fixed regions where skin usually
    is - upper-centre for the face, mid-flanks for arms.  Crude, and honest
    about it: this is a fallback, and the check reports lower confidence.
    """
    h, w = rgb.shape[:2]
    boxes = boxes or [(0.40, 0.18, 0.60, 0.32), (0.20, 0.45, 0.32, 0.60), (0.68, 0.45, 0.80, 0.60)]
    samples: list[np.ndarray] = []
    for x0, y0, x1, y1 in boxes:
        patch = rgb[int(y0 * h) : int(y1 * h), int(x0 * w) : int(x1 * w)]
        if patch.size:
            samples.append(rgb_to_lab(patch).reshape(-1, 3))
    if not samples:
        return np.zeros(3, dtype=np.float32)
    return np.mean(np.concatenate(samples, axis=0), axis=0)


def delta_e76(a: np.ndarray, b: np.ndarray) -> float:
    """CIE76.  Cruder than CIEDE2000, but the threshold is fitted to her own
    photos during calibration, so the simpler metric stays honest.
    Roughly: <2 invisible, <4 acceptable, >6 obvious."""
    return float(np.sqrt(np.sum((np.asarray(a) - np.asarray(b)) ** 2)))


# ---------------------------------------------------------------------------
# Model-backed measurements
# ---------------------------------------------------------------------------


class ModelUnavailable(RuntimeError):
    """Raised when a check needs weights that are not installed.

    Caught by the gate and turned into UNKNOWN - never into PASS.
    """


class FaceBackend:
    """ArcFace embeddings for identity comparison."""

    def __init__(self, models_dir: Path) -> None:
        self._models_dir = models_dir
        self._app = None

    def _ensure(self) -> None:
        if self._app is not None:
            return
        caps = detect_capabilities(self._models_dir)
        if not caps.identity_available:
            raise ModelUnavailable("insightface or buffalo_l is not installed")
        from insightface.app import FaceAnalysis  # type: ignore[import-not-found]

        app = FaceAnalysis(name="buffalo_l", root=str(self._models_dir))
        app.prepare(ctx_id=-1, det_size=(640, 640))  # ctx_id=-1 -> CPU
        self._app = app

    def embed(self, path: str | Path) -> np.ndarray:
        """Normalised embedding of the largest face, or raise."""
        self._ensure()
        import cv2  # type: ignore[import-not-found]

        image = cv2.imread(str(path))
        if image is None:
            raise ModelUnavailable(f"could not read {path}")
        faces = self._app.get(image)  # type: ignore[union-attr]
        if not faces:
            raise ModelUnavailable("no face found")
        largest = max(faces, key=lambda f: float((f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1])))
        vector = np.asarray(largest.normed_embedding, dtype=np.float32)
        norm = float(np.linalg.norm(vector))
        return vector / norm if norm else vector

    def face_boxes(self, rgb: np.ndarray) -> list[tuple[float, float, float, float]]:
        """Detected faces as relative (x0, y0, x1, y1), largest first.

        Relative rather than pixels because skin_patches works in fractions of
        the frame, and the gate inspects images at several sizes.

        Returns an empty list when there is a detector but no face - which is
        a real answer, and different from having no detector at all.
        """
        self._ensure()
        image = (rgb * 255.0).astype(np.uint8)[:, :, ::-1]  # RGB float -> BGR uint8
        faces = self._app.get(image)  # type: ignore[union-attr]
        if not faces:
            return []
        h, w = rgb.shape[:2]
        boxes: list[tuple[float, float, float, float]] = []
        for face in sorted(
            faces,
            key=lambda f: float((f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1])),
            reverse=True,
        ):
            x0, y0, x1, y1 = (float(v) for v in face.bbox)
            # Inset. An ArcFace box includes hair and background at the edges,
            # and those would pull the mean away from skin - which is the whole
            # thing being measured.
            dx, dy = (x1 - x0) * 0.20, (y1 - y0) * 0.20
            boxes.append(
                (
                    max(0.0, (x0 + dx) / w),
                    max(0.0, (y0 + dy) / h),
                    min(1.0, (x1 - dx) / w),
                    min(1.0, (y1 - dy) / h),
                )
            )
        return boxes

    @staticmethod
    def cosine(a: np.ndarray, b: np.ndarray) -> float:
        denominator = float(np.linalg.norm(a) * np.linalg.norm(b))
        return float(np.dot(a, b) / denominator) if denominator else 0.0


class PoseBackend:
    """Body keypoints, and the proportions measured from them.

    THE ANTI-SLIMMING CHECK. Without it a generator can quietly reshape her
    and nothing in the system notices, which is why its absence is reported
    loudly rather than tolerated.

    The session is created once and reused: loading an ONNX graph costs
    hundreds of milliseconds, and doing it per candidate would blow the
    latency budget on a six-image batch.
    """

    def __init__(self, models_dir: Path) -> None:
        self._models_dir = models_dir
        self._session = None
        self._input_name: str | None = None

    def _ensure(self) -> None:
        if self._session is not None:
            return
        caps = detect_capabilities(self._models_dir)
        if not caps.proportions_available:
            raise ModelUnavailable("onnxruntime or the pose model is not installed")

        # Guarded even though the capability check just passed. That check is
        # cached for the process lifetime, so it can be stale - a package
        # removed, an environment swapped, a partially-installed wheel. An
        # unguarded import would then raise ModuleNotFoundError, which the
        # gate does not catch, and the whole batch dies instead of reporting
        # that one measurement is unavailable.
        try:
            import onnxruntime as ort  # type: ignore[import-not-found]
        except Exception as exc:  # noqa: BLE001
            raise ModelUnavailable(f"onnxruntime could not be imported: {exc}") from exc

        options = ort.SessionOptions()
        # One thread per core would fight the other candidates being gated
        # concurrently; the orchestrator already caps CV parallelism.
        options.intra_op_num_threads = 2
        try:
            self._session = ort.InferenceSession(
                str(self._models_dir / "yolov8n-pose.onnx"),
                sess_options=options,
                providers=["CPUExecutionProvider"],
            )
        except Exception as exc:  # noqa: BLE001
            # A missing or corrupt weights file must degrade to "cannot
            # measure", not take down the gate. onnxruntime raises its own
            # exception types, so this is deliberately broad: the gate knows
            # what to do with ModelUnavailable and nothing else.
            raise ModelUnavailable(f"could not load the pose model: {exc}") from exc
        self._input_name = self._session.get_inputs()[0].name

    def poses(self, path: str | Path):
        """Every person found, largest first. Raises if unavailable."""
        from app.gate.pose import decode_pose_output, letterbox_image

        self._ensure()
        rgb = load_rgb(path, max_side=1280)
        tensor, box = letterbox_image(rgb)
        outputs = self._session.run(None, {self._input_name: tensor})  # type: ignore[union-attr]
        return decode_pose_output(outputs[0], box)

    def subject(self, path: str | Path):
        """The person this photograph is of.

        The largest detection: in her photos she is the subject, and a
        passer-by must never define her proportions.
        """
        found = self.poses(path)
        if not found:
            raise ModelUnavailable("no person found in the image")
        return found[0]

    def eyes(self, path: str | Path) -> dict[str, float] | None:
        """Eye geometry for the largest person, or None if not both visible."""
        from app.gate.pose import eye_geometry

        return eye_geometry(self.subject(path))

    def proportions(self, path: str | Path):
        from app.gate.pose import measure

        return measure(self.subject(path))


class HandBackend:
    """Where the hands are.

    Deliberately narrower than it first appears. COCO-17 pose gives WRISTS,
    not fingers, so this locates hands rather than counting digits - and
    locating them is what the repair loop actually needs, because it inpaints
    a region rather than judging one.

    Whether a hand is broken is the visual judge's call on finals. That split
    is intentional: a pixel metric cannot tell six fingers from five, and a
    language model asked to measure a body will agree with whatever it is
    shown. Each is used for what it is good at.
    """

    def __init__(self, models_dir: Path) -> None:
        self._models_dir = models_dir
        self._pose = PoseBackend(models_dir)

    def hands(self, path: str | Path) -> list[dict[str, object]]:
        """Hand regions with the confidence of the wrist that located them."""
        from app.contracts.common import BBox
        from app.gate.pose import hand_regions

        caps = detect_capabilities(self._models_dir)
        if not caps.proportions_available:
            raise ModelUnavailable("onnxruntime or the pose model is not installed")

        pose = self._pose.subject(path)
        out: list[dict[str, object]] = []
        for name, (x0, y0, x1, y1) in hand_regions(pose):
            wrist = pose.keypoints.get(name.replace("_hand", "_wrist"))
            out.append(
                {
                    "name": name,
                    "bbox": BBox(x0=x0, y0=y0, x1=x1, y1=y1),
                    "confidence": float(wrist.confidence) if wrist else 0.0,
                }
            )
        return out
