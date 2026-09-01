"""Stage 1 - build the identity profile from the photos already received.

One pass, nothing asked of her. Produces:

  IDENTITY CENTROID    mean ArcFace embedding across her accepted photos
  PROPORTION BASELINE  THE ANTI-SLIMMING REFERENCE. Without it, a generator
                       can quietly reshape her and nothing in the system will
                       notice - which is the exact complaint she raised about
                       an earlier tool.
  SKIN REFERENCE       mean CIELAB over skin patches
  COVERAGE REPORT      whether the material supports what we want to build

Run:  python scripts/build_profile.py /path/to/her/photos

The report it prints at the end is the one short message she gets, and the
only thing she is asked in the entire build.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.contracts.attribute_ir import BodyProportions  # noqa: E402
from app.gate import backends  # noqa: E402
from app.gate.backends import FaceBackend, ModelUnavailable  # noqa: E402
from app.profile.model import (  # noqa: E402
    Coverage,
    IdentityProfile,
    PhotoVerdict,
)

SUPPORTED = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".bmp", ".tif", ".tiff"}

MIN_SHARPNESS = 0.20
MIN_PIXELS = 640 * 640


def classify_framing(width: int, height: int, path: Path | None = None) -> str:
    """How much of her the photograph actually contains.

    The docstring here used to promise that pose keypoints would replace the
    aspect-ratio guess "once the CV models are installed". They are installed,
    and the guess had to go, because it was not weak - it was blind.

    Every photograph from one phone in one orientation has the same aspect
    ratio. All thirteen of hers are 1.333, so the old rule returned "medio
    cuerpo" for all thirteen regardless of content - including the two that do
    show her hips. It was classifying the FILE, not the photograph, and no
    photograph she could ever send on that phone would have been counted as
    full body. She would have done exactly what was asked and been told she
    had sent nothing usable.

    Keypoints answer the actual question. Ankles visible means head to feet;
    hips means at least half; neither means a portrait.
    """
    if path is not None:
        try:
            from app.gate.backends import PoseBackend

            pose = PoseBackend(settings.models_dir).subject(path)
            both = lambda a, b: pose.get(a) is not None and pose.get(b) is not None
            if both("left_ankle", "right_ankle"):
                return "cuerpo entero"
            if both("left_hip", "right_hip"):
                return "medio cuerpo"
            if both("left_shoulder", "right_shoulder"):
                return "medio cuerpo"
            return "primer plano"
        except Exception:  # noqa: BLE001 - no model, or no person found
            pass

    # Fallback only. Kept so the script still runs without the CV stack, and
    # deliberately NOT trusted when keypoints are available.
    aspect = height / max(1, width)
    if aspect > 1.45:
        return "cuerpo entero"
    if aspect > 1.12:
        return "medio cuerpo"
    return "primer plano"


def screen(path: Path) -> PhotoVerdict:
    reasons: list[str] = []

    # The ORIGINAL dimensions, AFTER applying EXIF rotation.
    #
    # Two separate traps, both of which silently corrupt the result rather
    # than failing:
    #
    #   1. Measuring on the downscaled working copy below would reject every
    #      ordinary phone photo as too small. The downscale is ours, not hers.
    #
    #   2. A phone photographs in portrait and stores the pixels in landscape
    #      with an Orientation tag saying "rotate me". Read raw, a 2316x3088
    #      portrait shot arrives as 3088x2316, and the framing heuristic then
    #      calls a full-length photo a close-up - with no error anywhere.
    #      Every one of the subject's photos is Orientation 6.
    try:
        from PIL import Image, ImageOps

        with Image.open(path) as probe:
            rotated = ImageOps.exif_transpose(probe)
            original_width, original_height = rotated.size
    except Exception as exc:  # noqa: BLE001
        return PhotoVerdict(path=str(path), accepted=False, reasons=[f"ilegible: {exc}"])

    try:
        rgb = backends.load_rgb(path, max_side=768)
    except Exception as exc:  # noqa: BLE001
        return PhotoVerdict(path=str(path), accepted=False, reasons=[f"ilegible: {exc}"])

    width, height = original_width, original_height
    sharpness = backends.sharpness(rgb)
    mean_luma, clipped = backends.exposure(rgb)

    if width * height < MIN_PIXELS:
        reasons.append("resolucion demasiado baja")
    if sharpness < MIN_SHARPNESS:
        reasons.append("movida o desenfocada")
    if mean_luma < 0.12:
        reasons.append("demasiado oscura")
    if mean_luma > 0.92:
        reasons.append("quemada")
    if clipped > 0.35:
        reasons.append("demasiado contraste, se pierde detalle")

    return PhotoVerdict(
        path=str(path),
        accepted=not reasons,
        framing=classify_framing(width, height, path),
        sharpness=sharpness,
        reasons=reasons,
    )


def build_proportion_baseline(
    accepted: list[PhotoVerdict],
) -> tuple[BodyProportions, str, int]:
    """Her real body, measured from her real photos.

    THE ANTI-SLIMMING REFERENCE. Every generated image is compared against
    this, and without it the instruction "do not slim her" is a request with
    nothing behind it.

    The MEDIAN across photos, not the mean: one bad pose detection - an arm
    across the body, a crop that clips the hips - would drag a mean and quietly
    move her baseline. A median ignores it.
    """
    from app.gate.backends import PoseBackend

    pose_backend = PoseBackend(settings.models_dir)
    samples: list[BodyProportions] = []
    note = ""

    for verdict in accepted:
        try:
            samples.append(pose_backend.proportions(verdict.path))
        except ModelUnavailable as exc:
            note = str(exc)
            break
        except Exception:  # noqa: BLE001 - one unreadable photo is not fatal
            continue

    if not samples:
        return BodyProportions(), note or "no se ha podido medir ninguna foto", 0

    def median_of(field: str) -> float | None:
        values = [
            getattr(s, field) for s in samples if getattr(s, field) is not None
        ]
        # Three is the floor for a median to mean anything. Below that, report
        # nothing rather than a baseline built on one lucky detection.
        return float(np.median(values)) if len(values) >= 3 else None

    limb_keys = {k for s in samples for k in s.limb_ratios}
    limbs: dict[str, float] = {}
    for key in limb_keys:
        values = [s.limb_ratios[key] for s in samples if key in s.limb_ratios]
        if len(values) >= 3:
            limbs[key] = float(np.median(values))

    baseline = BodyProportions(
        shoulder_hip_ratio=median_of("shoulder_hip_ratio"),
        shoulder_torso_ratio=median_of("shoulder_torso_ratio"),
        hip_torso_ratio=median_of("hip_torso_ratio"),
        height_in_heads=median_of("height_in_heads"),
        jaw_width_ratio=median_of("jaw_width_ratio"),
        limb_ratios=limbs,
    )
    return baseline, note, len(samples)


def main(source: Path) -> int:
    if not source.exists():
        print(f"no existe: {source}")
        return 1

    settings.ensure_dirs()
    photos = sorted(p for p in source.rglob("*") if p.suffix.lower() in SUPPORTED)
    if not photos:
        print(f"no he encontrado fotos en {source}")
        return 1

    print(f"revisando {len(photos)} fotos...\n")

    verdicts = [screen(path) for path in photos]
    accepted = [v for v in verdicts if v.accepted]

    coverage = Coverage(
        close_up=sum(1 for v in accepted if v.framing == "primer plano"),
        medium=sum(1 for v in accepted if v.framing == "medio cuerpo"),
        full_body=sum(1 for v in accepted if v.framing == "cuerpo entero"),
    )

    # -- identity centroid -------------------------------------------------
    face = FaceBackend(settings.models_dir)
    embeddings: list[np.ndarray] = []
    identity_note = ""
    for verdict in accepted:
        try:
            embeddings.append(face.embed(verdict.path))
        except ModelUnavailable as exc:
            identity_note = str(exc)
            break

    centroid: np.ndarray | None = None
    dispersion: float | None = None
    if embeddings:
        stacked = np.vstack(embeddings)
        centroid = stacked.mean(axis=0)
        norm = float(np.linalg.norm(centroid))
        if norm:
            centroid = centroid / norm
        # How much her own photos disagree with each other.  A person shot
        # across years, lighting and hairstyles genuinely has a wider spread,
        # and holding her to a textbook threshold would reject good images.
        similarities = [FaceBackend.cosine(e, centroid) for e in embeddings]
        dispersion = float(np.std(similarities))

    # -- skin reference ----------------------------------------------------
    skin_samples = [
        backends.skin_patches(backends.load_rgb(v.path, max_side=512))
        for v in accepted[:12]
    ]
    skin_lab = (
        tuple(float(x) for x in np.mean(np.vstack(skin_samples), axis=0))
        if skin_samples
        else None
    )

    # -- proportion baseline: THE ANTI-SLIMMING REFERENCE ------------------
    proportions, proportion_note, measured_count = build_proportion_baseline(accepted)

    profile = IdentityProfile(
        owner=settings.owner_name,
        centroid=centroid,
        dispersion=dispersion,
        proportions=proportions,
        skin_lab=skin_lab,  # type: ignore[arg-type]
        coverage=coverage,
        verdicts=verdicts,
        built_at=time.time(),
    )
    profile.save(settings.profile_dir)

    # -- the report --------------------------------------------------------
    print("=" * 62)
    print(f"PERFIL DE {settings.owner_name.upper()}")
    print("=" * 62)
    for line in coverage.report_es():
        print(line)

    rejected = [v for v in verdicts if not v.accepted]
    if rejected:
        print(f"\nDescartadas {len(rejected)}:")
        for verdict in rejected:
            print(f"  {Path(verdict.path).name}: {', '.join(verdict.reasons)}")

    print()
    if centroid is not None:
        print(f"Identidad: centroide de {len(embeddings)} fotos, dispersion {dispersion:.3f}")
        print(f"Umbral sugerido: {profile.suggested_identity_threshold(0.62):.3f}")
    else:
        print("Identidad: NO CONSTRUIDA")
        print(f"  motivo: {identity_note or 'insightface no instalado'}")
        print("  sin esto el sistema NO puede comprobar que sales tu en el resultado")

    print()
    if profile.can_check_proportions:
        p = profile.proportions
        print(f"Proporciones: medidas en {measured_count} fotos")
        for label, value in (
            ("hombros / torso", p.shoulder_torso_ratio),
            ("caderas / torso", p.hip_torso_ratio),
            ("hombros / caderas", p.shoulder_hip_ratio),
            ("anchura de cara", p.jaw_width_ratio),
            ("altura en cabezas", p.height_in_heads),
        ):
            if value is not None:
                print(f"  {label:20s} {value:.3f}")
        print(f"  {len(p.limb_ratios)} proporciones de extremidades")
        print()
        print("  El control anti-adelgazamiento ESTA ACTIVO.")
        print(f"  Se rechazara cualquier imagen que se desvie mas de un "
              f"{settings.thresholds.proportion_drift:.0%}.")
    else:
        print("Proporciones: NO MEDIDAS")
        print(f"  motivo: {proportion_note or 'no hay suficientes fotos de cuerpo entero'}")
        print("  EL CONTROL ANTI-ADELGAZAMIENTO NO ESTA ACTIVO")
        print("  la IA puede adelgazarte sin que el sistema se entere")

    print()
    print(f"guardado en {settings.profile_dir}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        print("uso: python scripts/build_profile.py /ruta/a/las/fotos")
        raise SystemExit(1)
    raise SystemExit(main(Path(sys.argv[1])))
