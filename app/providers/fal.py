"""fal.ai adapter.

Pay per call, no subscription, no minimum - which is the requirement that
keeps the fixed monthly cost at zero. One model per descriptor, so the same
adapter class serves both tiers: a cheap fast model for previews and a quality
model for finals.

The adapter maps a GenerationRequest onto an HTTP body and back. It makes no
creative decisions: the prompt arrives already rendered into this provider's
dialect by the compiler. If this file ever starts choosing words, that belongs
upstream.

VERIFY ON FIRST RUN. The request and response shapes below match fal's
documented synchronous API, but model input schemas differ between models and
change between versions. `python scripts/check_provider.py` makes one real
call and prints exactly what came back, so a mismatch is a two-minute fix
rather than a batch that dies after paying for every image.
"""

from __future__ import annotations

import time
from urllib.parse import urlparse
import uuid
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
from app.providers.http_base import HttpProviderClient, first_image_url, to_data_uri

FAL_BASE_URL = "https://fal.run"


class FalProvider(ImageProvider):
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
        p50_latency_s: float = 6.0,
        max_resolution: tuple[int, int] = (1024, 1024),
        prompt_dialect: PromptDialect = PromptDialect.NATURAL_VERBOSE,
        quality_prior: dict[str, float] | None = None,
        extra_params: dict | None = None,
        inpaint_model: str | None = None,
        i2i_model: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._id = provider_id
        self._model = model
        self._inpaint_model = inpaint_model
        #: Separate endpoint for image-to-image, where the model needs one.
        #:
        #: On fal a model is not one endpoint with a mode switch: flux schnell
        #: text-to-image and flux schnell image-to-image are DIFFERENT paths.
        #: Posting an image_url to the text-to-image path does not fail - the
        #: endpoint accepts the request and ignores the image, so a job meant
        #: to edit her photo silently returns an invented stranger that looks
        #: plausible on the screen and is wrong in the only way that matters.
        #:
        #: None means this model serves image-to-image on its own endpoint,
        #: which is true of the edit models (kontext).
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

        self._client = HttpProviderClient(
            provider_id=provider_id,
            base_url=FAL_BASE_URL,
            # fal uses "Key <token>", not "Bearer".
            headers={
                "Authorization": f"Key {api_key}",
                "Content-Type": "application/json",
            },
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
            notes=f"fal.ai {self._model}",
        )

    async def close(self) -> None:
        await self._client.close()

    # -- generation --------------------------------------------------------

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        body = self._build_body(request)
        return await self._call(self._endpoint_for(request), body, request, kind="generate")

    def _endpoint_for(self, request: GenerationRequest) -> str:
        """Which fal path this request belongs on.

        Refusing is deliberate. The alternative - posting the image to the
        text-to-image path anyway - is the worst available outcome: it returns
        200, it costs money, and it produces a photograph of somebody else.
        A loud failure here is caught by check_provider.py for a fraction of a
        cent; the silent one is caught by the client noticing it is not her.
        """
        if not request.source_image_path:
            return self._model
        if self._i2i_model:
            return self._i2i_model
        if Capability.IMAGE_TO_IMAGE not in self._capabilities:
            raise ProviderError(
                self._id,
                "este modelo no hace imagen-a-imagen",
                retryable=False,
            )
        # Declares i2i and names no separate endpoint: the model serves both
        # from one path. True of the edit models.
        return self._model

    async def inpaint(self, request: GenerationRequest) -> GenerationResult:
        if Capability.INPAINT not in self._capabilities:
            raise ProviderError(self._id, "este modelo no hace inpainting", retryable=False)
        if not request.source_image_path or not request.mask_path:
            raise ProviderError(self._id, "inpaint necesita imagen y mascara", retryable=False)

        body = self._build_body(request)
        body["mask_url"] = to_data_uri(request.mask_path)
        return await self._call(
            self._inpaint_model or self._model, body, request, kind="inpaint"
        )

    # -- internals ---------------------------------------------------------

    def _build_body(self, request: GenerationRequest) -> dict:
        body: dict[str, object] = {
            "prompt": request.prompt,
            "num_images": 1,
            # Off deliberately. The subject is a real adult woman in ordinary
            # clothing and false positives would silently cost a generation
            # she has already paid for. Consent and use policy are handled at
            # the product level, not by a per-call classifier.
            "enable_safety_checker": False,
        }

        if request.negative_prompt:
            body["negative_prompt"] = request.negative_prompt
        if request.seed is not None:
            body["seed"] = int(request.seed)
        if request.steps is not None:
            body["num_inference_steps"] = int(request.steps)
        if request.guidance is not None:
            body["guidance_scale"] = float(request.guidance)

        width = min(request.width, self._max_resolution[0])
        height = min(request.height, self._max_resolution[1])
        body["image_size"] = {"width": width, "height": height}

        if request.source_image_path:
            # Sent inline rather than as a link. The box has no public URL for
            # its files, and these are photographs of a real person.
            body["image_url"] = to_data_uri(request.source_image_path)
            if request.strength is not None:
                body["strength"] = float(request.strength)

        for path in request.reference_image_paths:
            body.setdefault("image_urls", []).append(to_data_uri(path))  # type: ignore[union-attr]

        # Per-model knobs from providers.json - anything this adapter should
        # not need to know about.
        body.update(self._extra)
        body.update({k: v for k, v in request.extra.items() if not k.startswith("_")})
        return body

    async def _call(
        self, model: str, body: dict, request: GenerationRequest, *, kind: str
    ) -> GenerationResult:
        started = time.monotonic()
        payload = await self._client.request_json("POST", f"/{model}", json=body)

        url = first_image_url(payload)
        if not url:
            raise ProviderError(
                self._id,
                f"la respuesta no traia imagen: {str(payload)[:300]}",
                retryable=False,
            )

        # Suffix from the URL, not assumed. The Cloudflare adapter hardcoded
        # .png while the service returned JPEG, and browsers refuse to render
        # a JPEG declared as image/png - a clean 200 and a blank rectangle.
        # This adapter downloads from a URL, which usually carries the real
        # extension; _serve derives the media type from the filename, so
        # getting it wrong here is the same failure by a different route.
        suffix = Path(urlparse(url).path).suffix.lower()
        if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
            suffix = ".png"
        destination = (
            self._output_dir
            / f"{self._id.replace('.', '_')}_{uuid.uuid4().hex[:12]}{suffix}"
        )
        await self._client.download(url, destination)

        returned_seed = payload.get("seed", request.seed)
        try:
            seed = int(returned_seed) if returned_seed is not None else None
        except (TypeError, ValueError):
            seed = request.seed

        return GenerationResult(
            image_path=str(destination),
            provider_id=self._id,
            cost_usd=self._cost,
            elapsed_s=time.monotonic() - started,
            seed=seed,
            # Everything needed to rebuild this image at full resolution.
            # This is what carries a chosen preview into its final, and it is
            # why what she picked is what she receives.
            reproduction={
                "provider_id": self._id,
                "model": model,
                "prompt": request.prompt,
                "negative_prompt": request.negative_prompt,
                "seed": seed,
                "guidance": request.guidance,
                "steps": request.steps,
                "source_image_path": request.source_image_path,
                "kind": kind,
            },
        )
