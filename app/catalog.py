"""The look catalog, and the proposal engine that matches it to a photo.

The catalog is DATA, not code: one JSON file per look in catalog/.  Adding a
look is editing a file; it needs no deploy and no developer.

The proposal engine is what makes the style options feel chosen rather than
listed.  When she uploads a photo, the robot reads it first and offers only
the styles that genuinely apply - a close-up gets studio portrait and black
and white, a full-body shot in a plain room gets exterior scenes and wardrobe
changes.  That is the moment the product stops looking like a tool.

Ranking is rules and statistics.  No training, because with one user there is
no data to train on and pretending otherwise would be selling air.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from app.contracts.attribute_ir import AttributeIR
from app.contracts.common import Attribute, Framing
from app.contracts.look_recipe import LookRecipe


class Catalog:
    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._looks: dict[str, LookRecipe] = {}

    def load(self) -> "Catalog":
        self._looks.clear()
        if not self._directory.exists():
            return self
        for path in sorted(self._directory.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                look = LookRecipe.model_validate(payload)
            except Exception as exc:  # noqa: BLE001
                # One malformed look must not take the catalog down - she
                # would lose every style because of a stray comma.
                print(f"[catalog] skipping {path.name}: {exc}")
                continue
            self._looks[look.id] = look
        return self

    def save(self, look: LookRecipe) -> None:
        self._directory.mkdir(parents=True, exist_ok=True)
        path = self._directory / f"{look.id}.json"
        path.write_text(
            json.dumps(look.model_dump(mode="json"), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        self._looks[look.id] = look

    def all(self, *, coverage_enforced: bool | None = None) -> list[LookRecipe]:
        """Every look she may be offered right now.

        Two different kinds of "not available" live here, and keeping them
        apart is the point:

        ``enabled=False``               retired. Gone until someone edits it.
        ``requires_coverage_off=True``  authored and ready, but waiting on a
                                        decision about coverage that has not
                                        been taken yet.

        The second set comes back with one line in .env - no file edits, no
        deploy - which is what makes handover a decision rather than a task.
        """
        if coverage_enforced is None:
            from app.config import settings

            coverage_enforced = settings.coverage_enforced
        return [
            look
            for look in self._looks.values()
            if look.enabled and not (look.requires_coverage_off and coverage_enforced)
        ]

    def withheld(self) -> list[LookRecipe]:
        """Looks held back only by the coverage policy.

        So the state is inspectable. A catalog that silently shows fewer
        entries than it holds is the kind of thing that gets diagnosed as a
        loading bug at exactly the wrong moment.
        """
        return [
            look
            for look in self._looks.values()
            if look.enabled and look.requires_coverage_off
        ]

    def get(self, look_id: str) -> LookRecipe | None:
        return self._looks.get(look_id)

    def __len__(self) -> int:
        return len(self._looks)


@dataclass(frozen=True)
class Proposal:
    look: LookRecipe
    score: float
    reason: str

    def _cover_url(self) -> str | None:
        """The cover's URL, or None when no file was ever produced."""
        if not self.look.cover_image:
            return None
        from app.config import settings

        name = Path(self.look.cover_image).name
        if not (Path(settings.catalog_dir) / "covers" / name).exists():
            return None
        return f"/covers/{name}"

    def public(self) -> dict:
        return {
            "id": self.look.id,
            "name": self.look.name,
            "category": self.look.category,
            # The FILE, not just the field. Every look declares a cover_image
            # whether or not one was ever produced, so a truthiness check on
            # the field emitted an <img> pointing at a 404 - twenty-one broken
            # image icons on the style screen, which is the first thing she
            # sees after uploading.
            #
            # The template already falls back to her own photograph, which is
            # a better placeholder than anything generic; it simply never ran.
            "cover": self._cover_url(),
            "reason": self.reason,
        }


class ProposalEngine:
    """Catalog x photo -> the tiles she sees."""

    def __init__(
        self,
        *,
        weight_keep: float = 1.0,
        weight_freshness: float = 0.5,
        weight_reliability: float = 0.75,
        freshness_window_s: float = 7 * 24 * 3600,
    ) -> None:
        self._w_keep = weight_keep
        self._w_fresh = weight_freshness
        self._w_reliable = weight_reliability
        self._freshness_window = freshness_window_s

    def applicable(self, look: LookRecipe, ir: AttributeIR) -> tuple[bool, str]:
        """The hard filter, applied before any ranking.

        A style that cannot work on this photo is not ranked low - it is
        removed. Offering a full-body scene for a head-and-shoulders photo
        would undermine the impression that the robot looked at all.
        """
        applies = look.applies_to

        if applies.framing and ir.capture.framing is not Framing.UNKNOWN:
            if ir.capture.framing not in applies.framing:
                return False, f"necesita {', '.join(f.value for f in applies.framing)}"

        if applies.needs_body and ir.capture.framing is Framing.CLOSE_UP:
            return False, "necesita cuerpo visible"

        if ir.capture.quality_score < applies.min_source_quality:
            return False, "la foto de origen no tiene calidad suficiente"

        if applies.requires_outdoor is not None and ir.capture.is_outdoor is not None:
            if applies.requires_outdoor != ir.capture.is_outdoor:
                return False, "no encaja con el tipo de escenario"

        return True, "encaja con esta foto"

    def rank(
        self,
        looks: list[LookRecipe],
        ir: AttributeIR,
        *,
        limit: int = 6,
        now: float | None = None,
    ) -> list[Proposal]:
        now = now if now is not None else time.time()
        proposals: list[Proposal] = []

        for look in looks:
            ok, why = self.applicable(look, ir)
            if not ok:
                continue

            stats = look.stats
            keep = stats.keep_rate if stats.keep_rate is not None else 0.5
            reliability = (
                stats.first_try_rate if stats.first_try_rate is not None else 0.5
            )
            freshness = self._freshness(stats.last_shown_at, now)

            score = (
                self._w_keep * keep
                + self._w_reliable * reliability
                + self._w_fresh * freshness
            )
            proposals.append(Proposal(look=look, score=score, reason=why))

        proposals.sort(key=lambda p: p.score, reverse=True)
        return proposals[:limit]

    def _freshness(self, last_shown_at: float | None, now: float) -> float:
        """1.0 for never shown, decaying to 0 for shown just now.

        Keeps the grid alive.  Without it the same four tiles win every week
        and the catalog feels smaller than it is - which the spec names as the
        main product risk.
        """
        if last_shown_at is None:
            return 1.0
        age = max(0.0, now - last_shown_at)
        return float(min(1.0, age / self._freshness_window))


def default_chip_rows(look: LookRecipe | None) -> dict[Attribute, list[str]]:
    """The multi-select rows for the style screen.

    Falls back to a catalog-wide vocabulary when she has not picked a look, so
    the rows are never empty and she can always describe what she wants.
    """
    if look is not None and look.selectable:
        return {a: look.ordered_chips(a) for a in look.selectable}
    return {
        Attribute.GARMENT: [
            "vestido", "traje", "casual", "deportivo", "abrigo",
        ],
        Attribute.HAIR: ["el mio", "suelto", "recogido", "coleta", "ondas"],
        Attribute.GESTURE: [
            "de pie", "caminando", "sentada", "apoyada",
            "manos en la cintura", "mano en el pelo",
        ],
        Attribute.EXPRESSION: [
            "neutra", "sonrisa suave", "sonrisa amplia", "seria", "mirada baja",
        ],
        Attribute.SCENE: ["como esta", "exterior", "estudio", "ciudad", "playa"],
        Attribute.LIGHT: ["como esta", "natural", "dorada", "estudio", "dramatica"],
        Attribute.FRAMING: ["como esta", "cuerpo entero", "medio", "primer plano"],
    }
