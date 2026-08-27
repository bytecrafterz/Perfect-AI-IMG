"""Is she in this photo?

One gesture, two meanings, resolved by the robot.

She uploads a photo.  It is either a photograph OF her - the thing to
transform - or a reference: a dress from Instagram, a hairstyle from a
magazine, a location she likes.  She should never have to tell the app which,
and she should never have to know there were two things she could have meant.

    SHE IS IN IT      -> analyse as source, show style options
    SHE IS NOT IN IT  -> ask what to take from it, and turn that into a chip
                         in the ROPA row so it combines with everything else

When identity verification is unavailable the honest answer is UNSURE, and the
UI asks her a single question rather than guessing and being wrong half the
time.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from app.gate.backends import FaceBackend, ModelUnavailable
from app.profile.model import IdentityProfile


class PhotoRole(str, Enum):
    SOURCE = "source"  # a photo of her, to transform
    REFERENCE = "reference"  # something she wants taken from it
    UNSURE = "unsure"  # cannot tell; ask her


@dataclass(frozen=True)
class RoleDecision:
    role: PhotoRole
    similarity: float | None = None
    detail: str = ""

    @property
    def needs_question(self) -> bool:
        return self.role is not PhotoRole.SOURCE


class PhotoRouter:
    def __init__(
        self,
        *,
        profile: IdentityProfile,
        face: FaceBackend,
        source_threshold: float = 0.45,
    ) -> None:
        self._profile = profile
        self._face = face
        # Deliberately looser than the gate's accept threshold.  This question
        # is "is this her at all", not "is this a good likeness" - a bad photo
        # of her is still a photo of her, and treating it as a reference would
        # be baffling.
        self._threshold = source_threshold

    def classify(self, image_path: str | Path) -> RoleDecision:
        if not self._profile.can_check_identity:
            return RoleDecision(
                role=PhotoRole.UNSURE,
                detail="sin verificacion de identidad no puedo saber si sales tu",
            )
        try:
            embedding = self._face.embed(image_path)
        except ModelUnavailable as exc:
            detail = str(exc)
            if "no face" in detail.lower():
                # No face at all is strong evidence: a garment shot, a
                # location, a flat lay.  This is the confident case.
                return RoleDecision(
                    role=PhotoRole.REFERENCE,
                    detail="no hay ninguna persona en la foto",
                )
            return RoleDecision(role=PhotoRole.UNSURE, detail=detail)

        similarity = FaceBackend.cosine(embedding, self._profile.centroid)  # type: ignore[arg-type]
        if similarity >= self._threshold:
            return RoleDecision(
                role=PhotoRole.SOURCE, similarity=similarity, detail="eres tu"
            )
        return RoleDecision(
            role=PhotoRole.REFERENCE,
            similarity=similarity,
            detail="hay una persona, pero no eres tu",
        )


#: What she can take from a reference photo.  One tap each, and the extracted
#: value becomes a chip that combines with every other row.
REFERENCE_TARGETS: tuple[tuple[str, str], ...] = (
    ("garment", "La ropa"),
    ("hair", "El peinado"),
    ("gesture", "La pose"),
    ("light", "La luz"),
    ("scene", "El sitio"),
    ("all", "Todo el estilo"),
)
