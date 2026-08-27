"""The identity profile - who she is, measured once.

Built in Stage 1 from the photos already received, then compared against every
generated image for the life of the project.  Three things live here:

  IDENTITY CENTROID     mean ArcFace embedding across her accepted photos.
                        A centroid rather than a single reference so one
                        unflattering angle cannot define her.

  PROPORTION BASELINE   shoulder/hip, height-in-heads, jaw width.  THE
                        ANTI-SLIMMING REFERENCE.  Without this the system
                        cannot tell that a generator made her thinner, which
                        is the exact complaint she raised about an earlier
                        tool.

  SKIN REFERENCE        mean CIELAB over skin patches.

The profile is stored as JSON plus a .npy for the embedding, so it survives a
restart and can be inspected by hand.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from app.contracts.attribute_ir import BodyProportions

PROFILE_VERSION = "1.0"


@dataclass
class PhotoVerdict:
    """One profile photo, screened."""

    path: str
    accepted: bool
    framing: str = "desconocido"
    sharpness: float = 0.0
    reasons: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, object]:
        return {
            "path": self.path,
            "accepted": self.accepted,
            "framing": self.framing,
            "sharpness": round(self.sharpness, 4),
            "reasons": self.reasons,
        }


@dataclass
class Coverage:
    """Whether the received photos actually support what we want to build.

    Full-body coverage is the one that matters most: it decides whether a LoRA
    is viable AND whether the proportion baseline exists at all.  Reported on
    day 1 so a shortfall becomes one targeted request rather than a discovery
    in week two.
    """

    close_up: int = 0
    medium: int = 0
    full_body: int = 0

    @property
    def total(self) -> int:
        return self.close_up + self.medium + self.full_body

    @property
    def lora_viable(self) -> bool:
        return self.total >= 15 and self.full_body >= 4

    @property
    def proportion_baseline_viable(self) -> bool:
        return self.full_body >= 3

    def report_es(self) -> list[str]:
        lines = [
            f"Fotos utilizables: {self.total}",
            f"  cuerpo entero {self.full_body}  medio cuerpo {self.medium}"
            f"  primer plano {self.close_up}",
        ]
        if not self.proportion_baseline_viable:
            lines.append(
                "AVISO: menos de 3 fotos de cuerpo entero. Sin ellas no puedo "
                "medir tus proporciones reales, que es justo lo que evita que "
                "la IA te adelgace sin permiso."
            )
        if not self.lora_viable:
            lines.append(
                "Con este material trabajo editando tus fotos reales. Para "
                "escenas y poses nuevas necesitaria mas fotos de cuerpo entero."
            )
        return lines


@dataclass
class IdentityProfile:
    version: str = PROFILE_VERSION
    owner: str = "owner"

    #: Mean normalised ArcFace embedding.  None when identity verification was
    #: unavailable at build time - which the gate must treat as "cannot check".
    centroid: np.ndarray | None = None
    #: Spread of embeddings around the centroid.  A wide spread means her
    #: photos disagree with each other, so the accept threshold should be
    #: looser for her than a textbook value would suggest.
    dispersion: float | None = None

    proportions: BodyProportions = field(default_factory=BodyProportions)
    skin_lab: tuple[float, float, float] | None = None

    coverage: Coverage = field(default_factory=Coverage)
    verdicts: list[PhotoVerdict] = field(default_factory=list)
    built_at: float = 0.0

    # -- capability --------------------------------------------------------

    @property
    def can_check_identity(self) -> bool:
        return self.centroid is not None

    @property
    def can_check_proportions(self) -> bool:
        """Whether the anti-slimming baseline actually exists.

        Explicit rather than clever, because everything downstream keys off
        it: False here means the gate reports UNKNOWN for proportions, and a
        generator could narrow her with nothing to notice.
        """
        p = self.proportions
        return any(
            value is not None
            for value in (
                p.shoulder_torso_ratio,
                p.hip_torso_ratio,
                p.shoulder_hip_ratio,
                p.jaw_width_ratio,
                p.height_in_heads,
            )
        ) or bool(p.limb_ratios)

    @property
    def can_check_skin(self) -> bool:
        return self.skin_lab is not None

    def suggested_identity_threshold(self, default: float) -> float:
        """Loosen the cutoff when her own photos disagree with each other.

        A person photographed across years, lighting and hairstyles has a
        genuinely wider embedding spread, and holding her to a textbook
        threshold would reject good images.  Calibration in Stage 5 replaces
        this with a fitted value; until then this is a defensible starting
        point rather than a guess.
        """
        if self.dispersion is None:
            return default
        return float(max(0.45, min(default, default - 0.5 * self.dispersion)))

    # -- persistence -------------------------------------------------------

    def save(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        if self.centroid is not None:
            np.save(directory / "centroid.npy", self.centroid)
        payload = {
            "version": self.version,
            "owner": self.owner,
            "dispersion": self.dispersion,
            "proportions": self.proportions.model_dump(),
            "skin_lab": list(self.skin_lab) if self.skin_lab else None,
            "coverage": {
                "close_up": self.coverage.close_up,
                "medium": self.coverage.medium,
                "full_body": self.coverage.full_body,
            },
            "verdicts": [v.to_json() for v in self.verdicts],
            "built_at": self.built_at,
            "has_centroid": self.centroid is not None,
        }
        (directory / "profile.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    @classmethod
    def load(cls, directory: Path) -> "IdentityProfile | None":
        path = directory / "profile.json"
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))

        centroid = None
        centroid_path = directory / "centroid.npy"
        if payload.get("has_centroid") and centroid_path.exists():
            centroid = np.load(centroid_path)

        coverage_data = payload.get("coverage") or {}
        return cls(
            version=payload.get("version", PROFILE_VERSION),
            owner=payload.get("owner", "owner"),
            centroid=centroid,
            dispersion=payload.get("dispersion"),
            proportions=BodyProportions(**(payload.get("proportions") or {})),
            skin_lab=tuple(payload["skin_lab"]) if payload.get("skin_lab") else None,
            coverage=Coverage(
                close_up=coverage_data.get("close_up", 0),
                medium=coverage_data.get("medium", 0),
                full_body=coverage_data.get("full_body", 0),
            ),
            verdicts=[
                PhotoVerdict(
                    path=v["path"],
                    accepted=v["accepted"],
                    framing=v.get("framing", "desconocido"),
                    sharpness=v.get("sharpness", 0.0),
                    reasons=v.get("reasons", []),
                )
                for v in payload.get("verdicts", [])
            ],
            built_at=payload.get("built_at", 0.0),
        )
