"""The quality gate - the only approver.

There is no human behind this.  Nothing reaches her that has not passed here,
and nothing passes here that could not be measured.  Two entry points:

    screen(...)   PREVIEWS.  Free CPU checks only, no language model.  Cheap
                  enough to run on every candidate, because a preview she may
                  not choose does not deserve paid scrutiny.

    inspect(...)  FINALS.  Everything, plus the visual judge.  This is the
                  last thing between a generator and her screen.

The three-state discipline from QAReport is the point of this module:
PASS / FAIL / UNKNOWN, where UNKNOWN blocks.  A check that cannot run is not
a check that passed.
"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np

from app.config import Thresholds
from app.contracts.qa_report import (
    Check,
    CheckOutcome,
    Defect,
    DefectKind,
    QAReport,
    Verdict,
)
from app.gate import backends
from app.gate.backends import (
    CVCapabilities,
    FaceBackend,
    HandBackend,
    ModelUnavailable,
    PoseBackend,
)
from app.profile.model import IdentityProfile

#: How much each check contributes to the ranking score that orders the
#: preview grid.  Identity dominates deliberately: an image that is beautiful
#: but not quite her is worthless for this product.
SCORE_WEIGHTS: dict[str, float] = {
    "identity": 3.0,
    "proportions": 2.0,
    "containment": 1.5,
    "skin_tone": 1.0,
    "sharpness": 1.0,
    "exposure": 0.5,
    "hands": 1.5,
    # Second only to identity, and above hands. Eyes carry most of the
    # resemblance, they are the first thing anyone looks at, and unlike a bad
    # hand they cannot be cropped out of the photograph.
    "eyes": 2.5,
    "judge": 2.0,
}


def resolve_strict(
    profile: IdentityProfile,
    capabilities: CVCapabilities,
    policy: str = "auto",
) -> tuple[bool, list[str]]:
    """Decide whether unmeasurable checks should block, and say why not.

    This used to be `strict = profile.can_check_identity`, which coupled the
    whole gate to one field and set a trap: building an identity centroid
    flipped strict on, and strict discards on ANY unknown - so the moment
    somebody installed insightface and enrolled her face, every image would
    have been discarded because proportions were still unmeasurable.  The
    symptom would have looked like the install breaking the app, and the
    obvious response - uninstall it - would have been exactly wrong.

    The honest rule is narrower: block on unknowns only when every check we
    hold a reference for can actually be measured.  A reference without the
    capability to use it is the definition of a check that cannot run.

    Returns (strict, reasons_it_is_not).  The reasons are shown to her rather
    than logged, because "not verifying" is a fact about her photographs and
    not an implementation detail.
    """
    policy = (policy or "auto").strip().lower()
    if policy in {"on", "true", "1", "strict"}:
        return True, []
    if policy in {"off", "false", "0"}:
        return False, ["STRICT_GATE=off: las comprobaciones no verificadas no bloquean"]

    # A check reports UNKNOWN when EITHER half is missing: the reference it
    # compares against, or the capability that measures it.  Strict makes any
    # UNKNOWN discard the image, so strict is only safe when NEITHER half is
    # missing anywhere.
    #
    # Getting this half-right is worse than not doing it.  An earlier version
    # only asked "reference present but capability missing", which reads as the
    # careful question and is exactly backwards for the live profile here: it
    # holds a skin reference and nothing else, so with insightface installed
    # that version returned strict=True and NO reasons - while identity and
    # proportions were still unmeasurable.  Every image discarded, immediately
    # after an install meant to improve things.
    blocked: list[str] = []

    def needs(name: str, has_reference: bool, has_capability: bool, missing: str) -> None:
        if has_reference and has_capability:
            return
        if not has_reference and not has_capability:
            blocked.append(f"{name}: no hay referencia ni con que medirla")
        elif not has_reference:
            blocked.append(f"{name}: no hay referencia en el perfil")
        else:
            blocked.append(f"{name}: {missing}")

    needs(
        "identidad",
        profile.can_check_identity,
        capabilities.identity_available,
        "falta insightface o buffalo_l",
    )
    needs(
        "proporciones",
        profile.can_check_proportions,
        capabilities.proportions_available,
        "falta onnxruntime o el modelo de pose",
    )
    needs(
        "piel",
        profile.can_check_skin,
        capabilities.face_detector_available,
        "no hay detector de caras",
    )

    return (not blocked), blocked


class Gate:
    def __init__(
        self,
        *,
        profile: IdentityProfile,
        thresholds: Thresholds,
        models_dir: Path,
        judge=None,
        strict: bool = True,
    ) -> None:
        """``strict`` decides what an unmeasurable check means.

        True  (production): UNKNOWN blocks the image.
        False (development without CV models): UNKNOWN is recorded as such and
              does not block, so the pipeline can be demonstrated - but every
              report carries the fact, and the UI shows a banner.  It is never
              silently upgraded to PASS.
        """
        self.profile = profile
        self.thresholds = thresholds
        self.strict = strict
        self.capabilities: CVCapabilities = backends.detect_capabilities(models_dir)
        self._face = FaceBackend(models_dir)
        self._pose = PoseBackend(models_dir)
        self._hands = HandBackend(models_dir)
        self._judge = judge

    # -- public entry points ----------------------------------------------

    def screen(self, image_path: str | Path) -> QAReport:
        """Stage A. Free, CPU-only, no paid call."""
        started = time.monotonic()
        report = QAReport(image_id=str(image_path), stage="preview")
        rgb = backends.load_rgb(image_path, max_side=640)

        report.checks.append(self._check_sanity(rgb))
        report.checks.append(self._check_sharpness(rgb))
        report.checks.append(self._check_identity(image_path))
        report.checks.append(self._check_proportions(image_path))
        report.checks.append(self._check_eyes(image_path))
        hands_check, hand_defects = self._check_hands(image_path)
        report.checks.append(hands_check)
        report.defects.extend(hand_defects)

        self._finalise(report, started)
        return report

    def inspect(
        self,
        image_path: str | Path,
        *,
        source_path: str | Path | None = None,
        change_mask: np.ndarray | None = None,
        request_summary: str = "",
    ) -> QAReport:
        """Stage B. Everything, including the paid visual judge."""
        started = time.monotonic()
        report = QAReport(image_id=str(image_path), stage="final")
        rgb = backends.load_rgb(image_path)

        report.checks.append(self._check_sanity(rgb))
        report.checks.append(self._check_sharpness(rgb))
        report.checks.append(self._check_identity(image_path))
        report.checks.append(self._check_proportions(image_path, source_path))
        report.checks.append(self._check_skin(rgb))
        report.checks.append(self._check_eyes(image_path))

        hands_check, hand_defects = self._check_hands(image_path)
        report.checks.append(hands_check)
        report.defects.extend(hand_defects)

        if source_path is not None:
            report.checks.append(
                self._check_containment(rgb, Path(source_path), change_mask)
            )

        if self._judge is not None:
            judge_check, judge_defects = self._judge.evaluate(
                image_path=image_path,
                source_path=source_path,
                request_summary=request_summary,
            )
            report.checks.append(judge_check)
            report.defects.extend(judge_defects)
            report.cost_usd += judge_check.cost_usd

        self._finalise(report, started)
        return report

    # -- individual checks -------------------------------------------------

    def _check_sanity(self, rgb: np.ndarray) -> Check:
        mean, clipped = backends.exposure(rgb)
        if mean < 0.03 or mean > 0.97 or clipped > 0.6:
            return Check(
                name="exposure",
                outcome=CheckOutcome.FAIL,
                value=float(mean),
                detail=f"imagen vacia o quemada (recorte {clipped:.0%})",
            )
        return Check(name="exposure", outcome=CheckOutcome.PASS, value=float(mean))

    def _check_sharpness(self, rgb: np.ndarray) -> Check:
        value = backends.sharpness(rgb)
        threshold = self.thresholds.min_sharpness
        return Check(
            name="sharpness",
            outcome=CheckOutcome.PASS if value >= threshold else CheckOutcome.FAIL,
            value=value,
            threshold=threshold,
            detail="" if value >= threshold else "imagen blanda o emborronada",
        )

    def _check_identity(self, image_path: str | Path) -> Check:
        """ArcFace cosine against the profile centroid.

        The single most important measurement in the product.
        """
        from app.contracts.common import Attribute

        if not self.profile.can_check_identity:
            return Check(
                name="identity",
                outcome=CheckOutcome.UNKNOWN,
                attribute=Attribute.FACE,
                detail="el perfil no tiene centroide de identidad",
            )
        try:
            embedding = self._face.embed(image_path)
        except ModelUnavailable as exc:
            return Check(
                name="identity",
                outcome=CheckOutcome.UNKNOWN,
                attribute=Attribute.FACE,
                detail=str(exc),
            )

        similarity = FaceBackend.cosine(embedding, self.profile.centroid)  # type: ignore[arg-type]
        accept = self.profile.suggested_identity_threshold(
            self.thresholds.identity_accept
        )
        outcome = CheckOutcome.PASS if similarity >= accept else CheckOutcome.FAIL
        return Check(
            name="identity",
            outcome=outcome,
            value=float(similarity),
            threshold=float(accept),
            attribute=Attribute.FACE,
            detail="" if outcome is CheckOutcome.PASS else "no se parece lo suficiente",
        )

    def _check_proportions(
        self, image_path: str | Path, source_path: str | Path | None = None
    ) -> Check:
        """THE ANTI-SLIMMING CHECK.

        Compares the generated image against HER SOURCE PHOTOGRAPH when there
        is one, and against the stored baseline only when there is not.

        WHY THE SOURCE AND NOT THE BASELINE

        The ratios are width-over-LENGTH, and the claim that they "survive a
        different crop or zoom" is false in a way that only shows up with real
        photographs. Perspective foreshortens the torso by an amount that
        depends on camera distance, so the same woman measures differently
        from two metres and from five.

        Measured on her own 24 photographs: within one framing the spread is
        2.8-8.2%, and ACROSS framings it is 19.2%. Built as a pooled baseline
        and compared at a 6% threshold, all 10 of her own measurable
        photographs were rejected as "not her body" - drifts of 6.0% to 36.8%.
        A check that rejects the person it was built from is not a check.

        The source photograph does not have that problem, because in
        image-to-image the output inherits the input's framing. Perspective
        cancels on both sides of the comparison, and the question being asked
        becomes the honest one: did the generator change her BETWEEN the photo
        she gave it and the photo it returned? That is exactly the complaint
        this check exists to answer, and the source is always available.

        The baseline remains the fallback for text-to-image, where there is no
        source to compare against.
        """
        from app.contracts.common import Attribute

        reference = None
        against = "tu foto original"
        if source_path is not None:
            try:
                reference = self._pose.proportions(source_path)
                if reference.shoulder_torso_ratio is None:
                    reference = None
                elif not self._same_framing(source_path, image_path):
                    # Different framing makes the comparison meaningless, and
                    # a meaningless comparison that returns a NUMBER is the
                    # dangerous kind. Refuse it.
                    return Check(
                        name="proportions",
                        outcome=CheckOutcome.UNKNOWN,
                        attribute=Attribute.BODY_PROPORTIONS,
                        detail=(
                            "el encuadre ha cambiado, no puedo comparar "
                            "proporciones de forma fiable"
                        ),
                    )
            except ModelUnavailable:
                reference = None

        if reference is None:
            reference = self.profile.proportions
            against = "tu perfil"

        if reference is self.profile.proportions and not self.profile.can_check_proportions:
            return Check(
                name="proportions",
                outcome=CheckOutcome.UNKNOWN,
                attribute=Attribute.BODY_PROPORTIONS,
                detail="no hay linea base de proporciones (faltan fotos de cuerpo entero)",
            )
        try:
            measured = self._pose.proportions(image_path)
        except ModelUnavailable as exc:
            return Check(
                name="proportions",
                outcome=CheckOutcome.UNKNOWN,
                attribute=Attribute.BODY_PROPORTIONS,
                detail=str(exc),
            )

        drift = reference.max_relative_drift(measured)
        if drift is None:
            # Keypoints were found but nothing comparable came out of them -
            # a close-up with no hips, most often. Unknown, not fine.
            return Check(
                name="proportions",
                outcome=CheckOutcome.UNKNOWN,
                attribute=Attribute.BODY_PROPORTIONS,
                detail="no hay medidas comparables en esta imagen",
            )

        threshold = self.thresholds.proportion_drift
        passed = drift <= threshold
        return Check(
            name="proportions",
            outcome=CheckOutcome.PASS if passed else CheckOutcome.FAIL,
            # Reported so the transparency card can show the number. Inverted
            # against the threshold so that, like every other check, a higher
            # value is better and the ranking score needs no special case.
            value=float(max(0.0, 1.0 - drift / threshold)) if threshold else None,
            threshold=1.0,
            attribute=Attribute.BODY_PROPORTIONS,
            detail=(
                ""
                if passed
                else f"tu cuerpo ha cambiado un {drift:.0%} respecto a "
                f"{against} (limite {threshold:.0%}) - {self._describe_drift(measured)}"
            ),
        )

    def _same_framing(self, a: str | Path, b: str | Path) -> bool:
        """Do these two images show the SAME parts of her?

        Width-over-length ratios are sensitive to camera distance, and the
        generator reframes. A generation that moved her torso from 34% to 49%
        of the frame measured 31.7% "narrower" with every ratio moving the
        same way - indistinguishable, by the numbers alone, from real
        slimming.

        So the comparison is only made when both images show the same
        landmarks and the torso occupies a similar share of the frame. When
        they do not, the answer is UNKNOWN - which blocks in strict mode and
        is recorded either way. That is the whole discipline of this gate: a
        check that cannot be made honestly is not made.
        """
        try:
            pa, pb = self._pose.subject(a), self._pose.subject(b)
        except ModelUnavailable:
            return False

        landmarks = ("left_hip", "right_hip", "left_knee", "left_ankle")
        if any((pa.get(k) is None) != (pb.get(k) is None) for k in landmarks):
            return False

        def torso_span(pose) -> float | None:
            sh, hp = pose.midpoint("left_shoulder", "right_shoulder"), pose.midpoint("left_hip", "right_hip")
            return pose.span(sh, hp) if sh and hp else None

        sa, sb = torso_span(pa), torso_span(pb)
        if sa is None or sb is None:
            return False
        # Within a fifth of each other. Loose enough for a natural shift in
        # stance, tight enough to catch the reframing that caused the false
        # readings.
        return abs(sa - sb) / max(sa, sb) <= 0.20

    def _describe_drift(self, measured) -> str:
        """Name what moved, in her language.

        "Proportions changed" is not actionable. "Shoulders narrower relative
        to hips" is something she can look at and agree or disagree with -
        which is exactly what the calibration session needs.
        """
        baseline = self.profile.proportions
        # The width-over-torso pair comes first because they are the primary
        # anti-slimming measures: shoulder_hip_ratio alone is blind to a body
        # narrowed uniformly, which is precisely the complaint being guarded
        # against.
        labels = {
            "shoulder_torso_ratio": ("anchura de hombros", "hombros"),
            "hip_torso_ratio": ("anchura de caderas", "caderas"),
            "shoulder_hip_ratio": ("hombros respecto a caderas", "proporcion"),
            "jaw_width_ratio": ("anchura de la cara", "cara"),
            "height_in_heads": ("altura respecto a la cabeza", "altura"),
        }
        worst_name, worst_delta = "", 0.0
        for field, (label, _short) in labels.items():
            a, b = getattr(baseline, field), getattr(measured, field)
            if a and b:
                delta = (b - a) / a
                if abs(delta) > abs(worst_delta):
                    worst_name, worst_delta = label, delta
        if not worst_name:
            return "proporciones del cuerpo"
        direction = "mas estrecho" if worst_delta < 0 else "mas ancho"
        return f"{worst_name}: {direction} ({worst_delta:+.0%})"

    def _check_skin(self, rgb: np.ndarray) -> Check:
        from app.contracts.common import Attribute

        if not self.profile.can_check_skin:
            return Check(
                name="skin_tone",
                outcome=CheckOutcome.UNKNOWN,
                attribute=Attribute.SKIN_TONE,
                detail="el perfil no tiene referencia de piel",
            )
        # Without a face detector there are no real skin boxes, only three
        # fixed rectangles guessing where a face and two arms usually are.  On
        # a clothed or full-length photo those rectangles land on coat and
        # background, and the number they produce is not a skin measurement at
        # all.
        #
        # Measured against her own reference set - the very photos the
        # reference is the mean of - that fallback fails 6 of 13.  On other
        # real photos of her it failed 10 of 10.  Left as a FAIL it discards
        # every final she pays for, and the failure is indistinguishable from
        # the generator genuinely altering her skin.
        #
        # So it reports UNKNOWN, which is what the check honestly is here.
        # The same principle as everywhere else in this gate: a check that
        # cannot run has not passed - and it has not failed either.  UNKNOWN
        # is recorded, surfaced, and blocks in strict mode.  Installing a face
        # detector turns this back into a real measurement.
        if not self.capabilities.face_detector_available:
            return Check(
                name="skin_tone",
                outcome=CheckOutcome.UNKNOWN,
                attribute=Attribute.SKIN_TONE,
                detail="sin detector de caras: no se puede medir la piel de forma fiable",
            )

        measured = backends.skin_patches(rgb, self._face_boxes(rgb))
        distance = backends.delta_e76(measured, np.asarray(self.profile.skin_lab))
        threshold = self.thresholds.skin_delta_e
        return Check(
            name="skin_tone",
            outcome=CheckOutcome.PASS if distance <= threshold else CheckOutcome.FAIL,
            value=float(distance),
            threshold=float(threshold),
            attribute=Attribute.SKIN_TONE,
            detail="" if distance <= threshold else "el tono de piel ha cambiado",
        )

    def _face_boxes(self, rgb: np.ndarray) -> list[tuple[float, float, float, float]] | None:
        """Real face boxes when a detector can give them, else None.

        None makes skin_patches fall back to its fixed rectangles, which is
        only reached when a detector exists but found no face - a different
        situation from having no detector at all.
        """
        try:
            boxes = self._face.face_boxes(rgb)
        except Exception:  # noqa: BLE001 - a detector that errors is no detector
            return None
        return boxes or None

    def _check_eyes(self, image_path: str | Path) -> Check:
        """Are the eyes where a face would put them?

        Geometry only. The pose model gives eye CENTRES, so this catches an
        eye placed independently of the face - the classic generator failure,
        and the one the client reported - and says nothing about whether an
        iris looks right. That is the visual judge's job, and pretending
        otherwise would mean claiming to check something we cannot see.

        Eyes earn their own check rather than riding along with proportions
        because they are the first thing anyone looks at, they carry most of
        the resemblance, and unlike a bad hand they cannot be cropped out.
        """
        if not self.capabilities.proportions_available:
            return Check(
                name="eyes",
                outcome=CheckOutcome.UNKNOWN,
                detail="modelo de pose no instalado",
            )
        try:
            geometry = self._pose.eyes(image_path)
        except ModelUnavailable as exc:
            return Check(name="eyes", outcome=CheckOutcome.UNKNOWN, detail=str(exc))

        if not geometry:
            # One eye visible is normal in profile. Not a failure, and not a
            # pass either - there is nothing here to compare.
            return Check(
                name="eyes",
                outcome=CheckOutcome.UNKNOWN,
                detail="no se ven los dos ojos",
            )

        t = self.thresholds
        confidence = geometry["confidence"]
        if confidence < t.eye_confidence:
            # A mangled eye stops looking like an eye and the detector loses
            # it. Same signal the wrist check relies on.
            return Check(
                name="eyes",
                outcome=CheckOutcome.FAIL,
                value=float(confidence),
                threshold=float(t.eye_confidence),
                detail="los ojos no se reconocen bien",
            )

        tilt = geometry.get("tilt_disagreement_deg")
        if tilt is not None and tilt > t.eye_tilt_deg:
            return Check(
                name="eyes",
                outcome=CheckOutcome.FAIL,
                value=float(tilt),
                threshold=float(t.eye_tilt_deg),
                detail="los ojos no estan alineados con la cara",
            )

        spacing = geometry.get("spacing_ratio")
        if spacing is not None and not (
            t.eye_spacing_ratio_min <= spacing <= t.eye_spacing_ratio_max
        ):
            return Check(
                name="eyes",
                outcome=CheckOutcome.FAIL,
                value=float(spacing),
                threshold=float(t.eye_spacing_ratio_max),
                detail="la separacion de los ojos no es natural",
            )

        return Check(
            name="eyes",
            outcome=CheckOutcome.PASS,
            value=float(confidence),
            threshold=float(t.eye_confidence),
        )

    def _check_hands(self, image_path: str | Path) -> tuple[Check, list[Defect]]:
        """Locate the hands, and flag the ones we are not confident about.

        This does NOT count fingers - COCO-17 has wrists, not digits. What it
        produces is a REGION, which is what the repair loop needs in order to
        repaint a bad hand without touching the rest of an image the gate has
        already validated. Whether a hand is actually broken is the visual
        judge's call on finals.

        A low-confidence wrist is a genuine signal in its own right: pose
        models lose the wrist precisely where the hand is mangled, because a
        mangled hand does not look like a hand.
        """
        if not self.capabilities.proportions_available:
            return (
                Check(
                    name="hands",
                    outcome=CheckOutcome.UNKNOWN,
                    detail="modelo de pose no instalado",
                ),
                [],
            )
        try:
            found = self._hands.hands(image_path)
        except ModelUnavailable as exc:
            return (
                Check(name="hands", outcome=CheckOutcome.UNKNOWN, detail=str(exc)),
                [],
            )

        if not found:
            # Zero hands located is not two good hands.
            #
            # This only became reachable when the pose model was installed:
            # before that the check returned UNKNOWN because the model was
            # missing, and the empty-result path never ran. With the model
            # present it fell through the loop below with worst still at its
            # 1.0 initial value and reported PASS - a perfect score - on 11 of
            # her 13 photos, where the model finds a person but no wrists.
            #
            # A close-up with no hands in frame lands here too, and reporting
            # UNKNOWN for it is right rather than merely safe: we genuinely
            # cannot say anything about hands we cannot see. In strict mode
            # that blocks, which is the conservative direction - and strict
            # already requires a complete profile and a complete CV stack.
            return (
                Check(
                    name="hands",
                    outcome=CheckOutcome.UNKNOWN,
                    threshold=self.thresholds.hand_confidence,
                    detail="no se ha localizado ninguna mano",
                ),
                [],
            )

        defects: list[Defect] = []
        worst = 1.0
        for hand in found:
            confidence = float(hand.get("confidence", 0.0))
            worst = min(worst, confidence)
            if confidence < self.thresholds.hand_confidence:
                defects.append(
                    Defect(
                        kind=DefectKind.HAND,
                        bbox=hand.get("bbox"),  # type: ignore[arg-type]
                        severity=1.0 - confidence,
                        detail=f"mano poco fiable ({confidence:.2f})",
                        source="hands",
                    )
                )
        return (
            Check(
                name="hands",
                outcome=CheckOutcome.PASS if not defects else CheckOutcome.FAIL,
                value=worst,
                threshold=self.thresholds.hand_confidence,
            ),
            defects,
        )

    def _check_containment(
        self, rgb: np.ndarray, source_path: Path, mask: np.ndarray | None
    ) -> Check:
        """Did the generator change something nobody asked it to?

        The measurable half of "modify only what I requested".
        """
        try:
            source = backends.load_rgb(source_path, max_side=rgb.shape[1])
        except Exception as exc:  # noqa: BLE001 - a missing source is UNKNOWN
            return Check(
                name="containment", outcome=CheckOutcome.UNKNOWN, detail=str(exc)
            )

        if source.shape != rgb.shape:
            from PIL import Image

            source = (
                np.asarray(
                    Image.fromarray((source * 255).astype(np.uint8)).resize(
                        (rgb.shape[1], rgb.shape[0]), Image.LANCZOS
                    ),
                    dtype=np.float32,
                )
                / 255.0
            )

        value = backends.ssim_outside(source, rgb, mask)
        threshold = self.thresholds.containment_ssim
        return Check(
            name="containment",
            outcome=CheckOutcome.PASS if value >= threshold else CheckOutcome.FAIL,
            value=float(value),
            threshold=float(threshold),
            detail="" if value >= threshold else "ha cambiado zonas que no se pidieron",
        )

    # -- verdict -----------------------------------------------------------

    def _finalise(self, report: QAReport, started: float) -> None:
        report.elapsed_s = time.monotonic() - started

        if not self.strict:
            # Development without CV models.  Unknowns are recorded verbatim
            # and excluded from the verdict, and the fact is written into the
            # report so the UI can say so.  Never rewritten as PASS.
            blocking = [c for c in report.checks if c.failed]
            unknown_names = [c.name for c in report.unknowns]
            if blocking:
                report.verdict = Verdict.DISCARD
                report.notes = "fallo: " + ", ".join(c.name for c in blocking)
            elif report.blocking_defects:
                report.verdict = Verdict.DISCARD
                report.notes = "defecto no reparable"
            elif report.repairable_defects:
                report.verdict = Verdict.REPAIR
                report.notes = "reparable"
            else:
                report.verdict = Verdict.ACCEPT
                report.notes = "ok"
            if unknown_names:
                report.notes += f" | SIN VERIFICAR: {', '.join(unknown_names)}"
        else:
            report.decide()

        report.compute_score(SCORE_WEIGHTS)

    # -- reporting ---------------------------------------------------------

    def status_es(self) -> list[str]:
        """What the gate can and cannot currently check, in plain words."""
        lines: list[str] = []
        lines.extend(self.capabilities.missing_es())
        if not self.profile.can_check_identity:
            lines.append("el perfil aun no tiene centroide de identidad")
        if not self.strict:
            lines.append(
                "MODO NO ESTRICTO: las comprobaciones que no se pueden medir "
                "no bloquean la imagen. No usar asi en produccion."
            )
        return lines
