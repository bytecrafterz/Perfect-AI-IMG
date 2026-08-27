"""A local generator that costs nothing.

Its purpose is NOT to make convincing photographs.  It exists so that:

  * the whole pipeline - propose, compile, route, generate, gate, repair,
    deliver - runs end to end on any machine with no API keys and no spend
  * the preview grid can be seen and clicked before a single euro is spent
  * tests exercise real orchestration instead of a stubbed-out fake

Output is deterministic in the seed and visibly different per slot, so a
six-preview grid looks like six options rather than one image repeated.  Every
tile is stamped MUESTRA so a mock image can never be mistaken for a result.
"""

from __future__ import annotations

import asyncio
import colorsys
import hashlib
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from app.contracts.provider import (
    Capability,
    GenerationRequest,
    GenerationResult,
    PromptDialect,
    ProviderDescriptor,
    Tier,
)
from app.providers.base import ImageProvider


def _hash_int(text: str) -> int:
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:12], 16)


def _font(size: int) -> ImageFont.ImageFont:
    for name in ("DejaVuSans.ttf", "arial.ttf", "Helvetica.ttc"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


class MockProvider(ImageProvider):
    """Renders a gradient card carrying the prompt that produced it."""

    def __init__(
        self,
        *,
        provider_id: str,
        tier: Tier,
        output_dir: Path,
        latency_s: float = 0.35,
        cost_usd: float = 0.0,
    ) -> None:
        self._id = provider_id
        self._tier = tier
        self._output_dir = output_dir
        self._latency = latency_s
        self._cost = cost_usd
        self._output_dir.mkdir(parents=True, exist_ok=True)

    @property
    def descriptor(self) -> ProviderDescriptor:
        return ProviderDescriptor(
            id=self._id,
            tiers=[self._tier],
            capabilities=[
                Capability.TEXT_TO_IMAGE,
                Capability.IMAGE_TO_IMAGE,
                Capability.INPAINT,
                Capability.IDENTITY_REFERENCE,
                Capability.UPSCALE,
            ],
            max_resolution=(2048, 2048),
            cost_per_call_usd=self._cost,
            p50_latency_s=self._latency,
            prompt_dialect=PromptDialect.NATURAL_VERBOSE,
            quality_prior={"identity": 0.9, "hands": 0.9, "proportions": 0.9},
            notes="local test generator - produces placeholder cards, not photographs",
        )

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        started = time.monotonic()
        # Simulate provider latency so concurrency behaviour is exercised
        # honestly: a batch that fans out finishes far sooner than one that
        # does not, and the tests can see the difference.
        await asyncio.sleep(self._latency)

        seed = request.seed if request.seed is not None else _hash_int(request.prompt)
        image = await asyncio.to_thread(self._render, request, seed)

        path = self._output_dir / f"{self._id.replace('.', '_')}_{seed}_{int(time.time()*1000)}.png"
        await asyncio.to_thread(image.save, path, "PNG")

        return GenerationResult(
            image_path=str(path),
            provider_id=self._id,
            cost_usd=self._cost,
            elapsed_s=time.monotonic() - started,
            seed=seed,
            # Everything needed to rebuild this at full resolution.  Real
            # adapters carry the same shape; the orchestrator does not care
            # what is inside it.
            reproduction={
                "prompt": request.prompt,
                "negative_prompt": request.negative_prompt,
                "seed": seed,
                "guidance": request.guidance,
                "steps": request.steps,
                "source_image_path": request.source_image_path,
                "provider_id": self._id,
            },
        )

    async def inpaint(self, request: GenerationRequest) -> GenerationResult:
        """Repaint inside the mask only.

        The mock proves the contract that matters: pixels outside the mask are
        copied verbatim from the source, so the gate's containment check has
        something real to measure.
        """
        started = time.monotonic()
        await asyncio.sleep(self._latency * 0.6)

        if not request.source_image_path or not request.mask_path:
            from app.providers.base import ProviderError

            raise ProviderError(self._id, "inpaint needs a source and a mask", retryable=False)

        seed = request.seed if request.seed is not None else _hash_int(request.prompt)
        image = await asyncio.to_thread(self._render_inpaint, request, seed)

        path = self._output_dir / f"{self._id.replace('.', '_')}_repair_{seed}_{int(time.time()*1000)}.png"
        await asyncio.to_thread(image.save, path, "PNG")

        return GenerationResult(
            image_path=str(path),
            provider_id=self._id,
            cost_usd=self._cost,
            elapsed_s=time.monotonic() - started,
            seed=seed,
            reproduction={"repaired_from": request.source_image_path, "seed": seed},
        )

    # -- rendering ---------------------------------------------------------

    def _render(self, request: GenerationRequest, seed: int) -> Image.Image:
        w, h = request.width, request.height
        hue = (seed % 360) / 360.0
        top = tuple(int(c * 255) for c in colorsys.hsv_to_rgb(hue, 0.45, 0.92))
        bottom = tuple(
            int(c * 255) for c in colorsys.hsv_to_rgb((hue + 0.12) % 1.0, 0.60, 0.35)
        )

        image = Image.new("RGB", (w, h), top)
        draw = ImageDraw.Draw(image)
        for y in range(h):
            t = y / max(1, h - 1)
            draw.line(
                [(0, y), (w, y)],
                fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)),
            )

        # A soft ellipse where a subject would be, so framing checks and the
        # thumbnail grid have something with structure rather than flat colour.
        cx, cy = w // 2, int(h * 0.42)
        rx, ry = int(w * 0.22), int(h * 0.26)
        overlay = Image.new("RGB", (w, h), (0, 0, 0))
        ImageDraw.Draw(overlay).ellipse(
            [cx - rx, cy - ry, cx + rx, cy + ry], fill=(255, 255, 255)
        )
        overlay = overlay.filter(ImageFilter.GaussianBlur(radius=w // 18))
        image = Image.blend(image, overlay, 0.18)

        draw = ImageDraw.Draw(image)
        draw.text((16, 14), "MUESTRA", font=_font(max(14, w // 26)), fill=(255, 255, 255))
        draw.text(
            (16, 14 + max(20, w // 20)),
            f"seed {seed}",
            font=_font(max(11, w // 44)),
            fill=(255, 255, 255),
        )

        # The prompt, wrapped, so a grid of previews is self-explaining while
        # developing: you can see which slot produced which tile.
        y = int(h * 0.72)
        small = _font(max(10, w // 48))
        line = ""
        for word in request.prompt.split():
            probe = f"{line} {word}".strip()
            if draw.textlength(probe, font=small) > w - 32:
                draw.text((16, y), line, font=small, fill=(255, 255, 255))
                y += max(13, w // 40)
                line = word
                if y > h - 30:
                    break
            else:
                line = probe
        if line and y <= h - 30:
            draw.text((16, y), line, font=small, fill=(255, 255, 255))

        return image

    def _render_inpaint(self, request: GenerationRequest, seed: int) -> Image.Image:
        source = Image.open(request.source_image_path).convert("RGB")
        mask = Image.open(request.mask_path).convert("L").resize(source.size)
        patch = self._render(
            request.model_copy(update={"width": source.width, "height": source.height}),
            seed,
        )
        # Composite through the mask: outside it, the original survives
        # untouched.  That is the property the repair loop depends on.
        return Image.composite(patch, source, mask)


def build_mock_providers(output_dir: Path) -> list[MockProvider]:
    """The two tiers the two-stage design needs."""
    return [
        MockProvider(
            provider_id="mock.fast",
            tier=Tier.PREVIEW,
            output_dir=output_dir,
            latency_s=0.25,
            cost_usd=0.0,
        ),
        MockProvider(
            provider_id="mock.quality",
            tier=Tier.FINAL,
            output_dir=output_dir,
            latency_s=0.5,
            cost_usd=0.0,
        ),
    ]
