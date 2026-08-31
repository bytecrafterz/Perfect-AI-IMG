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

WHY THE 4B AND NOT THE 9B - AND AN OPEN LICENCE QUESTION
The WEIGHTS differ decisively: FLUX.2-klein-4B is Apache 2.0 on its Hugging
Face model card, "released for commercial use", while klein-9B and flux-2-dev
are NON-COMMERCIAL. So the 9B is unusable here however much better it looks,
and the model string must not be "upgraded" without re-reading the licence.

But note carefully what that does and does not settle. Apache 2.0 governs the
downloadable weights. Using Cloudflare's HOSTED endpoint is governed by
Cloudflare's terms plus Black Forest Labs' Terms of Service, which is the only
thing Cloudflare's model page actually links under "Terms and License" - it
does not mention Apache 2.0, and it links the SAME ToS for the non-commercial
9B. So the permissive weight licence is necessary but not sufficient evidence.

BEFORE DELIVERING PAID CLIENT WORK ON THIS, read the commercial-use clause of
https://bfl.ai/legal/terms-of-service directly. That check has not been done.

WHAT IT ACTUALLY DOES
It is an instruct-edit model in the Kontext family, not SD-style img2img: it
takes up to four reference images and a prompt that may address them by index
("take the subject of image 1 and style it like image 0").

Cloudflare documents the mechanism and NOT the quality of identity
preservation. A widely repeated line about changing background and pose
"without accidentally changing the face of your model" is community framing
that appears nowhere in Cloudflare's or BFL's documentation - do not treat it
as a guarantee, and do not repeat it to the client. Whether her face survives
is a question for a test on her actual photographs.

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

#: Cloudflare answers 429 for at least two different situations, and they want
#: opposite handling:
#:
#:    3036  "You have used up your daily free allocation of 10,000 neurons."
#:          Gone until 00:00 UTC. Retrying cannot help.
#:    3040  "Out of capacity." Transient. Retrying is exactly right.
#:
#: 429 is in RETRYABLE_STATUS, so without separating these, an exhausted
#: allocation would cost three attempts and the full backoff on every request
#: for the rest of the day to re-learn the same fact - while a genuine
#: capacity blip must keep its retry or a busy minute becomes a failed session.
#:
#: The code was verified against Cloudflare's error table. An earlier draft of
#: this adapter used 3036's near neighbour 4006, which does not appear in that
#: table at all: the quota check would have matched nothing and the retry
#: storm would have happened anyway.
QUOTA_MARKERS = ("3036", "daily free allocation", "used up your daily")


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

        # THIS MODEL HAS NO negative_prompt PARAMETER. Its documented inputs
        # are prompt, input_image_0..3, guidance, width, height and seed -
        # nothing else. So a negative prompt handed to this adapter was simply
        # dropped, and every preview went out with the entire coverage and
        # anatomy negative list silently discarded. The compiler was doing its
        # job; the words never left the building.
        #
        # The only channel available is the positive prompt, so they are
        # folded in as an explicit prohibition. Trimmed, because a distilled
        # four-step model given a hundred comma-separated negatives attends to
        # none of them - the ones kept are the failures the client actually
        # reported.
        prompt = request.prompt
        if request.negative_prompt:
            prompt = f"{prompt}. Avoid entirely: {_condense(request.negative_prompt)}"

        fields: dict[str, str] = {
            "prompt": prompt,
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
        #
        # The EXTENSION MUST MATCH THE BYTES. This was hardcoded to .png and
        # Cloudflare returns JPEG, so every photograph was served as
        # Content-Type: image/png containing JPEG data - which browsers refuse
        # to render. The request was a clean 200 and the picture was simply
        # blank, which is a peculiarly hard failure to read: nothing is
        # missing, nothing errors, and the file opens perfectly in any tool
        # that sniffs content instead of trusting the name.
        suffix = _suffix_for(raw)
        destination = (
            self._output_dir
            / f"{self._id.replace('.', '_')}-{int(time.time() * 1000)}{suffix}"
        )
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


#: How many negative terms to fold into the positive prompt.
#:
#: A four-step distilled model given a long list attends to none of it, so
#: this keeps the head of the list - which the compiler already orders with
#: the identity and anatomy terms first.
_MAX_FOLDED_NEGATIVES = 14


def _condense(negative: str) -> str:
    """The most important negatives, as a short phrase.

    Deduplicated preserving order, because the compiler concatenates several
    lists and the same term appearing three times spends the budget without
    adding anything.
    """
    seen: list[str] = []
    for raw in negative.split(","):
        term = raw.strip()
        if term and term.lower() not in {s.lower() for s in seen}:
            seen.append(term)
        if len(seen) >= _MAX_FOLDED_NEGATIVES:
            break
    return ", ".join(seen)


def _suffix_for(raw: bytes) -> str:
    """The file extension the bytes actually deserve.

    Sniffed from the magic number rather than assumed. The provider is free to
    return whatever format it likes and has no obligation to announce a
    change, and getting this wrong fails in a genuinely confusing way: the
    request is a clean 200, the file is intact, every tool that sniffs content
    opens it perfectly - and the browser, which trusts the Content-Type
    derived from the extension, renders nothing at all.

    Written with fromhex rather than escape sequences because this is exactly
    the sort of constant that gets silently corrupted by one layer of quoting.
    """
    signatures = (
        ("ffd8ff", ".jpg"),
        ("89504e470d0a1a0a", ".png"),
        ("47494638", ".gif"),
    )
    for hex_prefix, suffix in signatures:
        if raw.startswith(bytes.fromhex(hex_prefix)):
            return suffix
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return ".webp"
    # Unrecognised: .png keeps it readable by anything that sniffs, and the
    # gate will reject it soon enough if it is not an image at all.
    return ".png"


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
