"""Cloudflare Workers AI adapter - the free tier.

WHY THIS EXISTS
The rest of the pipeline is free: the gate, the catalog, the combination walk,
the proportion measurement are all local Python and cost nothing. Generation
was the one step that could not be done on this box - measured at 231 GFLOP/s,
the fastest distilled model on this CPU is about 63 s per image, against a
promise of six previews in under two minutes.

Workers AI runs it on somebody else's GPU, and the free allocation is real:
10,000 Neurons per day, reset at 00:00 UTC, no credit card, and it HARD STOPS
rather than billing you. Verified against the pricing page rather than assumed.

    preview   512x512  + 1 reference =  31.4 neurons  ->  318/day
    final    1024x1024 + 1 reference = 109.6 neurons  ->   91/day
    a session (6 previews + 3 finals) = 517 neurons   ->  ~19 sessions/day

WHY THE 4B AND NOT THE 9B
Licence, and it is not close. FLUX.2-klein-4B is Apache 2.0 - "fully open,
released for commercial use" on the official model card. FLUX.2-klein-9B is
released under a NON-COMMERCIAL licence, as is flux-2-dev. This is paid client
work, so the 9B is unusable here however much better it looks. Do not "upgrade"
the model string without re-reading its licence.

WHAT IT ACTUALLY DOES
It is an instruct-edit model in the Kontext family, not SD-style img2img: it
takes up to four reference images and a prompt that may address them by index.
Cloudflare's own description is close to the client's brief - change the
background, lighting or pose "without accidentally changing the face of your
model".

THE CONSTRAINT THAT MATTERS
    All input images must be smaller than 512x512.
Her photograph is downscaled before upload. On a full-length shot that leaves
a face perhaps 80-120 px across, which is the real risk to identity here and
the thing to test on her actual photographs before promising anything. The
gate's identity check is the backstop, and it is worth having strict mode on
before this is used for delivery.

STEPS ARE FIXED AT 4. It is a distilled model; a steps parameter is ignored,
so quality cannot be traded for time in the usual way.
"""

from __future__ import annotations

import base64
import binascii
import io
import time
from pathlib import Path

import httpx

from app.contracts.provider import (
    Capability,
    GenerationRequest,
    GenerationResult,
    PromptDialect,
    ProviderDescriptor,
    Tier,
)
from app.providers.base import ImageProvider, ProviderError
from app.providers.http_base import HttpProviderClient

CLOUDFLARE_BASE_URL = "https://api.cloudflare.com"

#: Every input image must be strictly under this on both axes.
MAX_INPUT_SIDE = 511

#: Cloudflare answers 429 both for ordinary rate limiting and for the daily
#: free allocation running out. The first is worth retrying; the second is not
#: worth retrying until tomorrow, and 429 is in RETRYABLE_STATUS - so without
#: naming it, every request for the rest of the day would cost three attempts
#: and the full backoff to re-discover the same thing.
QUOTA_MARKERS = ("4006", "daily free allocation", "out of neurons")


class CloudflareProvider(ImageProvider):
    def __init__(
        self,
        *,
        provider_id: str,
        model: str,
        tiers: list[Tier],
        capabilities: list[Capability],
        cost_per_call_usd: float,
        output_dir: Path,
        api_key: str,
        account_id: str,
        p50_latency_s: float = 3.0,
        max_resolution: tuple[int, int] = (1024, 1024),
        prompt_dialect: PromptDialect = PromptDialect.INSTRUCTIONAL,
        quality_prior: dict[str, float] | None = None,
        extra_params: dict | None = None,
        i2i_model: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._id = provider_id
        self._model = model
        # One endpoint generates and edits, so there is no separate i2i path.
        # Accepted and ignored so the loader can pass every entry uniformly.
        self._i2i_model = i2i_model
        self._tiers = tiers
        self._capabilities = capabilities
        self._cost = cost_per_call_usd
        self._latency = p50_latency_s
        self._max_resolution = max_resolution
        self._dialect = prompt_dialect
        self._quality_prior = quality_prior or {}
        self._extra = extra_params or {}
        self._output_dir = output_dir
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._account_id = account_id

        self._client = HttpProviderClient(
            provider_id=provider_id,
            base_url=CLOUDFLARE_BASE_URL,
            # Bearer, unlike fal's "Key <token>". Content-Type is deliberately
            # unset: httpx writes the multipart boundary itself, and setting it
            # by hand produces a body the server cannot parse.
            headers={"Authorization": f"Bearer {api_key}"},
            transport=transport,
        )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return ProviderDescriptor(
            id=self._id,
            tiers=self._tiers,
            capabilities=self._capabilities,
            max_resolution=self._max_resolution,
            cost_per_call_usd=self._cost,
            p50_latency_s=self._latency,
            prompt_dialect=self._dialect,
            quality_prior=self._quality_prior,
            notes=f"Cloudflare Workers AI {self._model} (free tier)",
        )

    async def close(self) -> None:
        await self._client.close()

    # -- generation --------------------------------------------------------

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        if request.source_image_path and Capability.IMAGE_TO_IMAGE not in self._capabilities:
            raise ProviderError(
                self._id, "este modelo no hace imagen-a-imagen", retryable=False
            )
        return await self._call(request, kind="generate")

    async def inpaint(self, request: GenerationRequest) -> GenerationResult:
        # No mask parameter on this endpoint. Saying so is better than sending
        # a mask that is silently ignored and returning a whole-image rewrite
        # as though it were a localised repair.
        raise ProviderError(
            self._id,
            "flux-2-klein no acepta mascara: no hace inpainting dirigido",
            retryable=False,
        )

    # -- internals ---------------------------------------------------------

    def _fields(self, request: GenerationRequest) -> dict[str, str]:
        width = min(request.width, self._max_resolution[0])
        height = min(request.height, self._max_resolution[1])

        fields: dict[str, str] = {
            "prompt": request.prompt,
            "width": str(width),
            "height": str(height),
        }
        if request.seed is not None:
            fields["seed"] = str(int(request.seed))
        # No steps: the model is distilled to a fixed 4 and ignores it.
        for key, value in self._extra.items():
            fields[key] = str(value)
        for key, value in request.extra.items():
            if not key.startswith("_"):
                fields[key] = str(value)
        return fields

    def _reference_images(self, request: GenerationRequest) -> dict[str, tuple[str, bytes, str]]:
        """Her photo first, then any extra references, as input_image_N.

        Order is meaningful: the prompt may address images by index, and index
        0 is the subject everywhere in this codebase.
        """
        paths: list[Path] = []
        if request.source_image_path:
            paths.append(Path(request.source_image_path))
        paths.extend(Path(p) for p in request.reference_image_paths)

        files: dict[str, tuple[str, bytes, str]] = {}
        for index, path in enumerate(paths[:4]):  # the model takes four
            files[f"input_image_{index}"] = (
                f"input_image_{index}.png",
                _downscaled_png(path),
                "image/png",
            )
        return files

    async def _await_call(self, request: GenerationRequest) -> dict:
        return await self._client.request_multipart(
            "POST",
            f"/client/v4/accounts/{self._account_id}/ai/run/{self._model}",
            data=self._fields(request),
            files=self._reference_images(request),
            non_retryable_body=QUOTA_MARKERS,
        )

    async def _call(self, request: GenerationRequest, *, kind: str) -> GenerationResult:
        started = time.monotonic()
        try:
            payload = await self._await_call(request)
        except ProviderError as exc:
            # The transport raises on a non-retryable status before this
            # adapter ever sees the body, so the quota case is rewritten here
            # to say when it comes back. "HTTP 429" alone sends someone
            # looking for a rate limit they cannot find.
            if any(m in str(exc) for m in QUOTA_MARKERS):
                raise ProviderError(
                    self._id,
                    "se ha agotado la cuota diaria gratuita de Cloudflare; "
                    "se renueva a las 00:00 UTC",
                    retryable=False,
                ) from exc
            raise

        if not payload.get("success", True):
            "POST",
            errors = payload.get("errors") or [{"message": "sin detalle"}]
            message = "; ".join(str(e.get("message", e)) for e in errors)
            quota = any(m in message for m in QUOTA_MARKERS)
            raise ProviderError(
                self._id,
                (
                    "se ha agotado la cuota diaria gratuita de Cloudflare "
                    f"(se renueva a las 00:00 UTC): {message}"
                    if quota
                    else f"error de Cloudflare: {message}"
                ),
                retryable=False,
            )

        encoded = (payload.get("result") or {}).get("image")
        if not encoded:
            raise ProviderError(
                self._id, "la respuesta no traia imagen", retryable=False
            )
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ProviderError(
                self._id, f"imagen base64 invalida: {exc}", retryable=False
            ) from exc

        # Returned inline rather than as a URL, so there is no second fetch.
        destination = self._output_dir / f"{self._id.replace('.', '_')}-{int(time.time()*1000)}.png"
        destination.write_bytes(raw)

        return GenerationResult(
            image_path=str(destination),
            provider_id=self._id,
            cost_usd=self._cost,
            elapsed_s=time.monotonic() - started,
            seed=request.seed,
            reproduction={
                "prompt": request.prompt,
                "seed": request.seed,
                "provider_id": self._id,
                "model": self._model,
                "kind": kind,
            },
        )


def _downscaled_png(path: Path) -> bytes:
    """Fit inside the 512x512 input cap, preserving aspect ratio.

    Cloudflare rejects anything at or above 512 on either axis, so this is not
    an optimisation - an oversized reference is a hard failure. Downscaling
    here rather than at the call site keeps the rule in one place.

    Uses the project's own loader so EXIF orientation is honoured: her phone
    writes Orientation 6 on every photograph, and an upload rotated 90 degrees
    would be sent to the model sideways, which no prompt recovers from.
    """
    from PIL import Image, ImageOps

    with Image.open(path) as opened:
        # Her phone writes Orientation 6 on every photograph. Without this the
        # reference goes up sideways, and no prompt recovers from that - the
        # same bug that once made all 13 of her photos read as landscape.
        image = ImageOps.exif_transpose(opened)
        image = image.convert("RGB")
        image.thumbnail((MAX_INPUT_SIDE, MAX_INPUT_SIDE), Image.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()
