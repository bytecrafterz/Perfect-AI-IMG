"""The eye check.

The client sent back a photograph and said "the eyes are completely wrong".
The gate had checks for exposure, sharpness, identity, proportions, skin and
hands, and nothing whatever for eyes - the first thing anyone looks at, the
place most of the resemblance lives, and the one defect that cannot be hidden
by a crop.

THREE LAYERS, EACH DOING ONLY WHAT IT HONESTLY CAN

  prompt   describes correct eyes positively, and names the specific failures
           negatively. Prevention is free.
  gate     GEOMETRY from the pose model: are the eyes where a face would put
           them? COCO-17 gives eye CENTRES, so this catches an eye placed
           independently of the face and says NOTHING about whether an iris
           looks right.
  judge    APPEARANCE. A vision model is the only thing that can see a melted
           pupil, so it is told to look there first.

Conflating the last two would mean claiming to check something we cannot see,
which is the failure mode this whole gate exists to avoid.
"""

from __future__ import annotations

import pytest

from app.config import Thresholds
from app.contracts.qa_report import CheckOutcome, DefectKind
from app.gate.pose import Keypoint, Pose, eye_geometry


def face(
    *,
    left_eye=(440, 300),
    right_eye=(560, 300),
    left_ear=(390, 310),
    right_ear=(610, 310),
    confidence=0.95,
    width=1000,
    height=1250,
) -> Pose:
    """A face in a frame, expressed as the detector would report it."""
    points = {
        "left_eye": left_eye,
        "right_eye": right_eye,
        "left_ear": left_ear,
        "right_ear": right_ear,
    }
    return Pose(
        keypoints={
            name: Keypoint(x=x / width, y=y / height, confidence=confidence)
            for name, (x, y) in points.items()
        },
        box_confidence=0.99,
        aspect=height / width,
    )


# ---------------------------------------------------------------------------
# geometry
# ---------------------------------------------------------------------------


def test_a_normal_face_measures_cleanly() -> None:
    g = eye_geometry(face())
    assert g is not None
    assert g["tilt_disagreement_deg"] < 1.0
    assert 0.3 < g["spacing_ratio"] < 0.85


def test_an_eye_placed_independently_of_the_face_is_caught() -> None:
    """The classic generator failure: the head tilts one way and one eye does
    not follow. Real eyes tilt WITH the head, so the disagreement between the
    eye line and the ear line is the signal - not the tilt itself.
    """
    tilted_head = face(left_ear=(390, 260), right_ear=(610, 360))  # head tilted
    straight_eyes = eye_geometry(tilted_head)
    assert straight_eyes["tilt_disagreement_deg"] > 12.0

    # ...and a head tilted with its eyes following is fine.
    consistent = face(
        left_eye=(440, 270), right_eye=(560, 330),
        left_ear=(390, 260), right_ear=(610, 360),
    )
    assert eye_geometry(consistent)["tilt_disagreement_deg"] < 12.0


def test_measurement_does_not_move_with_the_frame_shape() -> None:
    """Same lesson as the proportion ratios: x is normalised against width and
    y against height, so an angle computed without the aspect correction moves
    with the frame."""
    shapes = [(1000, 1000), (1000, 1250), (1000, 600), (1400, 1000)]
    tilts, spacings = set(), set()
    for w, h in shapes:
        g = eye_geometry(face(width=w, height=h))
        tilts.add(round(g["tilt_disagreement_deg"], 6))
        spacings.add(round(g["spacing_ratio"], 6))
    assert len(tilts) == 1, f"tilt moved with the frame: {tilts}"
    assert len(spacings) == 1, f"spacing moved with the frame: {spacings}"


def test_one_eye_visible_is_not_an_answer() -> None:
    """A profile shot legitimately shows one eye. That is UNKNOWN, not a
    failure - and certainly not a pass."""
    profile = Pose(
        keypoints={"left_eye": Keypoint(x=0.44, y=0.24, confidence=0.9)},
        box_confidence=0.9,
        aspect=1.25,
    )
    assert eye_geometry(profile) is None


# ---------------------------------------------------------------------------
# the check
# ---------------------------------------------------------------------------


def _gate(monkeypatch, geometry):
    from pathlib import Path

    from app.gate.backends import CVCapabilities
    from app.gate.gate import Gate
    from app.profile.model import IdentityProfile

    gate = Gate(
        profile=IdentityProfile(),
        thresholds=Thresholds(),
        models_dir=Path("does-not-exist"),
        strict=False,
    )
    monkeypatch.setattr(
        gate, "capabilities",
        CVCapabilities(onnxruntime=True, insightface=False, opencv=True,
                       face_model=False, pose_model=True),
    )
    monkeypatch.setattr(gate._pose, "eyes", lambda _p: geometry)
    return gate


def test_unrecognisable_eyes_fail(monkeypatch) -> None:
    """A mangled eye stops looking like an eye and the detector loses it -
    the same signal the wrist check relies on."""
    gate = _gate(monkeypatch, {"spacing": 0.1, "confidence": 0.2})
    assert gate._check_eyes("x.png").outcome is CheckOutcome.FAIL


def test_misaligned_eyes_fail(monkeypatch) -> None:
    gate = _gate(monkeypatch, {
        "spacing": 0.1, "confidence": 0.95,
        "tilt_disagreement_deg": 25.0, "spacing_ratio": 0.5,
    })
    check = gate._check_eyes("x.png")
    assert check.outcome is CheckOutcome.FAIL
    assert "alineados" in check.detail


def test_unnatural_spacing_fails(monkeypatch) -> None:
    gate = _gate(monkeypatch, {
        "spacing": 0.1, "confidence": 0.95,
        "tilt_disagreement_deg": 1.0, "spacing_ratio": 1.4,
    })
    assert gate._check_eyes("x.png").outcome is CheckOutcome.FAIL


def test_a_good_face_passes(monkeypatch) -> None:
    """Measured on her real photographs: tilt 0.04-1.18 degrees, spacing
    0.41-0.45, confidence 0.97-0.99. The thresholds are deliberately generous
    against that - a check that fires on genuine photographs is worse than no
    check, and this one accuses the tool of the client's own complaint."""
    gate = _gate(monkeypatch, {
        "spacing": 0.1, "confidence": 0.98,
        "tilt_disagreement_deg": 1.1, "spacing_ratio": 0.44,
    })
    assert gate._check_eyes("x.png").outcome is CheckOutcome.PASS


def test_no_pose_model_is_unknown_not_pass(monkeypatch) -> None:
    from pathlib import Path

    from app.gate.backends import CVCapabilities
    from app.gate.gate import Gate
    from app.profile.model import IdentityProfile

    gate = Gate(profile=IdentityProfile(), thresholds=Thresholds(),
                models_dir=Path("does-not-exist"), strict=False)
    monkeypatch.setattr(gate, "capabilities", CVCapabilities(
        onnxruntime=False, insightface=False, opencv=False,
        face_model=False, pose_model=False))
    assert gate._check_eyes("x.png").outcome is CheckOutcome.UNKNOWN


def test_both_eyes_not_visible_is_unknown(monkeypatch) -> None:
    gate = _gate(monkeypatch, None)
    check = gate._check_eyes("x.png")
    assert check.outcome is CheckOutcome.UNKNOWN
    assert check.outcome is not CheckOutcome.PASS


# ---------------------------------------------------------------------------
# the other two layers
# ---------------------------------------------------------------------------


def test_the_prompt_asks_for_correct_eyes() -> None:
    from app.compile.compiler import ANATOMY_CLAUSE, _BASE_NEGATIVES

    clause = ANATOMY_CLAUSE.lower()
    for expected in ("eyes", "pupil", "iris"):
        assert expected in clause

    negatives = {n.lower() for n in _BASE_NEGATIVES}
    for expected in ("misaligned eyes", "lazy eye", "mismatched pupils",
                     "unequal eye size", "melted eyes"):
        assert expected in negatives


def test_an_eye_defect_is_never_repaired() -> None:
    """Eyes are face, and FACE is deliberately not repairable: repainting a
    face is how identity drifts, and identity is the whole product. A bad eye
    means discard and regenerate - never inpaint a new eye onto her.
    """
    import app.gate.judge as judge

    mapping = next(
        v for v in vars(judge).values()
        if isinstance(v, dict) and v.get("face") is DefectKind.FACE
    )
    for word in ("eye", "eyes", "pupil", "iris"):
        assert mapping[word] is DefectKind.FACE
        assert mapping[word].is_repairable is False


def test_the_judge_is_told_to_look_at_eyes_first() -> None:
    """It was never told to look at them at all. A vision model is the only
    thing that can see a melted pupil."""
    import inspect

    import app.gate.judge as judge

    rubric = inspect.getsource(judge).lower()
    assert "1. eyes" in rubric
    position = rubric.index("1. eyes")
    assert position < rubric.index("hands"), "eyes must come before hands"


def test_eyes_carry_weight_in_the_ranking() -> None:
    """The grid is ordered by score. Eyes above hands, below identity."""
    from app.gate.gate import SCORE_WEIGHTS

    assert SCORE_WEIGHTS["eyes"] > SCORE_WEIGHTS["hands"]
    assert SCORE_WEIGHTS["eyes"] < SCORE_WEIGHTS["identity"]


@pytest.mark.parametrize("stage", ["screen", "inspect"])
def test_eyes_are_checked_at_both_stages(stage: str) -> None:
    """Previews too. She chooses from them, and choosing a photograph whose
    eyes are wrong wastes the final it becomes."""
    import inspect

    from app.gate.gate import Gate

    source = inspect.getsource(getattr(Gate, stage))
    assert "_check_eyes" in source


# ---------------------------------------------------------------------------
# Her original must reach the model that produces the photograph she keeps
# ---------------------------------------------------------------------------


def test_the_final_edits_her_photograph_not_the_preview() -> None:
    """The single most damaging decision in the project, and its correction.

    _one_final passed source_image_path=candidate.image_path - the accepted
    PREVIEW - so the chain was her photo -> a free 512px 4-step preview -> the
    final. Measured against her ArcFace centroid, where her own photographs
    score 0.83-0.87:

        final from the chosen preview   0.003   a stranger
        final from HER photograph       0.703   her

    Identity was already gone by the second step and the third faithfully
    preserved its absence. Passing her photo as an additional
    identity_reference did not help - kontext takes a single image_url and
    ignores the rest.

    Preview fidelity is not abandoned: the prompt, seed and look all still
    come from the preview she chose. What changes is that the style is applied
    to HER photograph rather than to a degraded copy of it, which is what
    in_place_edit meant in the first place.
    """
    import inspect

    from app.orchestrator.engine import Orchestrator

    for method in (Orchestrator._one_final, Orchestrator._retry_final):
        source = inspect.getsource(method)
        assert "source_image_path=state.source_path" in source, (
            f"{method.__name__} still edits the preview instead of her photograph"
        )
        # The style must still come from what she chose.
        assert "repro" in source, f"{method.__name__} lost the chosen preview's prompt"
