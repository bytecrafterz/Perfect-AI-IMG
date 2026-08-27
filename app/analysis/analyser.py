"""Photo -> AttributeIR.

Runs ONCE per uploaded photo, not per image.  That is what makes it
affordable to use the strongest model here: the analysis is amortised across
every preview in the session, so it costs a fraction of a cent per delivered
photograph while being the input everything downstream depends on.

Two implementations behind one interface:

  ClaudeAnalyser     the real one.  Reads framing, setting, garment, lighting,
                     gesture, expression and quality into the structured IR.
  HeuristicAnalyser  no API key required.  Measures what can be measured from
                     pixels alone and is HONEST about the rest, leaving fields
                     None rather than inventing them.

The fallback exists so the app runs end to end with no keys.  It is not a
substitute: an IR full of Nones produces a thinner prompt, and the settings
screen says so.
"""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path

import numpy as np
from pydantic import BaseModel, Field

from app.contracts.attribute_ir import (
    AttributeIR,
    CaptureIR,
    FaceDescriptor,
    HairDescriptor,
    SceneIR,
    SubjectIR,
)
from app.contracts.common import Framing
from app.gate import backends

ANALYSIS_COST_USD = 0.004


class _PhotoReading(BaseModel):
    """What the model is asked to extract.  Deliberately descriptive, never
    evaluative: it reports what is in the photograph, it does not judge it."""

    framing: str = Field(description="one of: primer plano, medio, cuerpo entero")
    is_outdoor: bool
    background: str = Field(description="short phrase")
    garment: str = Field(description="what the person is wearing, short phrase")
    garment_color: str
    gesture: str = Field(description="body position, short phrase")
    expression: str
    lighting: str = Field(description="e.g. 'luz de ventana lateral'")
    camera_height: str = Field(description="e.g. 'pecho', 'ojos', 'bajo'")
    focal_mm_estimate: int = Field(ge=10, le=300)

    face_shape: str = ""
    jaw: str = ""
    eyes_color: str = ""
    eyes_shape: str = ""
    nose: str = ""
    lips: str = ""
    hair_color: str = ""
    hair_length: str = ""
    hair_texture: str = ""
    build: str = ""


_SYSTEM = """You describe photographs for a professional photo production system.

Report only what you can actually see. Where you cannot tell, say so with an empty
string rather than guessing - a confident wrong answer about hair colour or build
poisons every image generated afterwards.

Describe the person's features factually and neutrally, as a casting director would
for a continuity sheet. Do not comment on attractiveness, do not flatter, do not
euphemise body type. Accuracy is what preserves this person's likeness; anything
softened here comes back as a face that is not theirs.

Answer field values in Spanish, except framing which must be exactly one of:
"primer plano", "medio", "cuerpo entero"."""

_FRAMING_MAP = {
    "primer plano": Framing.CLOSE_UP,
    "medio": Framing.MEDIUM,
    "cuerpo entero": Framing.FULL_BODY,
}


def _image_block(path: str | Path) -> dict:
    path = Path(path)
    media_type = mimetypes.guess_type(path.name)[0] or "image/png"
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.standard_b64encode(path.read_bytes()).decode("ascii"),
        },
    }


class HeuristicAnalyser:
    """Pixel measurement only.  Leaves unknown fields None on purpose."""

    name = "heuristic"
    cost_usd = 0.0

    def analyse(self, image_path: str | Path) -> AttributeIR:
        rgb = backends.load_rgb(image_path, max_side=512)
        height, width = rgb.shape[:2]

        sharpness = backends.sharpness(rgb)
        mean_luma, clipped = backends.exposure(rgb)
        quality = float(max(0.0, min(1.0, 0.6 * sharpness + 0.4 * (1.0 - clipped))))

        # Aspect ratio is a weak but real signal, and weak-but-real beats
        # invented.  A tall frame is usually a standing full-length shot.
        aspect = height / max(1, width)
        if aspect > 1.5:
            framing = Framing.FULL_BODY
        elif aspect > 1.15:
            framing = Framing.MEDIUM
        else:
            framing = Framing.CLOSE_UP

        # Outdoor light is bluer and brighter at the top of the frame.
        top = rgb[: height // 3]
        blueness = float(np.mean(top[..., 2]) - np.mean(top[..., 0]))
        is_outdoor = bool(blueness > 0.05 and mean_luma > 0.45)

        skin = backends.skin_patches(rgb)

        return AttributeIR(
            subject=SubjectIR(
                face=FaceDescriptor(),
                hair=HairDescriptor(),
                skin=None,
            ),
            capture=CaptureIR(
                framing=framing,
                quality_score=quality,
                is_outdoor=is_outdoor,
            ),
            scene=SceneIR(),
            notes=(
                "Analisis basico sin modelo de lenguaje: solo se ha medido "
                f"nitidez ({sharpness:.2f}), exposicion ({mean_luma:.2f}) y "
                f"encuadre estimado por proporciones. Lab piel {skin.round(1).tolist()}."
            ),
        )


class ClaudeAnalyser:
    """The real analyser."""

    name = "claude"
    cost_usd = ANALYSIS_COST_USD

    def __init__(self, *, api_key: str, model: str = "claude-opus-5") -> None:
        self._api_key = api_key
        self._model = model
        self._client = None
        self._fallback = HeuristicAnalyser()

    def _ensure_client(self):
        if self._client is None:
            import anthropic  # lazy: the app must boot without the SDK

            self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client

    def analyse(self, image_path: str | Path) -> AttributeIR:
        # Measured values come from pixels even when the model is available:
        # sharpness and exposure are arithmetic, and asking a language model
        # to estimate them would be slower, dearer and less accurate.
        base = self._fallback.analyse(image_path)

        try:
            client = self._ensure_client()
            response = client.messages.parse(
                model=self._model,
                max_tokens=2048,
                system=[{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
                messages=[
                    {
                        "role": "user",
                        "content": [
                            _image_block(image_path),
                            {"type": "text", "text": "Describe this photograph."},
                        ],
                    }
                ],
                output_format=_PhotoReading,
            )
            reading: _PhotoReading = response.parsed_output
        except Exception as exc:  # noqa: BLE001
            base.notes = f"{base.notes} | el analisis con modelo fallo: {exc}"
            return base

        return AttributeIR(
            subject=SubjectIR(
                face=FaceDescriptor(
                    shape=reading.face_shape or None,
                    jaw=reading.jaw or None,
                    eyes_color=reading.eyes_color or None,
                    eyes_shape=reading.eyes_shape or None,
                    nose=reading.nose or None,
                    lips=reading.lips or None,
                ),
                hair=HairDescriptor(
                    color=reading.hair_color or None,
                    length=reading.hair_length or None,
                    texture=reading.hair_texture or None,
                ),
                build=reading.build or None,
            ),
            capture=CaptureIR(
                framing=_FRAMING_MAP.get(reading.framing.strip().lower(), base.capture.framing),
                focal_mm_estimate=reading.focal_mm_estimate,
                camera_height=reading.camera_height or None,
                lighting=reading.lighting or None,
                # Kept from measurement, not from the model.
                quality_score=base.capture.quality_score,
                is_outdoor=reading.is_outdoor,
            ),
            scene=SceneIR(
                background=reading.background or None,
                garment=reading.garment or None,
                garment_color=reading.garment_color or None,
                gesture=reading.gesture or None,
                expression=reading.expression or None,
            ),
        )


def build_analyser(*, api_key: str, model: str):
    """Whichever is available.  The caller does not branch."""
    return ClaudeAnalyser(api_key=api_key, model=model) if api_key else HeuristicAnalyser()
