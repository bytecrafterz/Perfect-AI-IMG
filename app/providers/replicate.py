"""Replicate adapter.

A second real provider, and the point is not redundancy - it is proof.  The
spec makes a promise she asked for directly: "no quiero quedar atada a una
unica API".  A promise like that is only credible if it is demonstrable, so
there are two adapters and swapping between them is a line in providers.json.

Replicate is asynchronous by default: create a prediction, then poll until it
settles.  The `Prefer: wait` header collapses that into one call when the
model is quick, which is worth having for the preview tier where the whole
latency budget is 25 seconds.  The polling path is kept for when it is not.

VERIFY ON FIRST RUN - see the note in fal.py.  `python scripts/check_provider.py`
makes one real call and prints what came back.
"""

from __future__ import annotations

import asyncio
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

REPLICATE_BASE_URL = "https://api.replicate.com"

TERMINAL_STATES = {"succeeded", "failed", "canceled"}


class ReplicateProvider(ImageProvider):
    def __init__(
        self,
        *,
        provider_id: str,
        model: str,
        tiers: list[Tier],
        capabilities: list[Capability],
        cost_per_call_usd: float,
        output_dir: Path,
        api_token: str,
        version: str | None = None,
        p50_latency_s: float = 8.0,
        max_resolution: tuple[int, int] = (1024, 1024),
        prompt_dialect: PromptDialect = PromptDialect.NATURAL_VERBOSE,
        quality_prior: dict[str, float] | None = None,
        extra_params: dict | None = None,
        poll_interval_s: float = 1.0,
        max_wait_s: float = 180.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._id = provider_id
        self._model = model
        self._version = version
        self._tiers = tiers
        self._capabilities = capabilities
        self._cost = cost_per_call_usd
        self._latency = p50_latency_s
        self._max_resolution = max_resolution
        self._dialect = prompt_dialect
        self._quality_prior = quality_prior or {}
        self._extra = extra_params or {}
        self._poll_interval = poll_interval_s
        self._max_wait = max_wait_s
        self._output_dir = output_dir
        self._output_dir.mkdir(parents=True, exist_ok=True)

        self._client = HttpProviderClient(
            provider_id=provider_id,
            base_url=REPLICATE_BASE_URL,
            headers={
                "Authorization": f"Bearer {api_token}",
                "Content-Type": "application/json",
                # Ask the API to hold the connection open until the
                # prediction settles. When it does, one round trip; when it
                # cannot, we fall through to polling below.
                "Prefer": "wait=60",
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
            notes=f"replicate {self._model}",
        )

    async def close(self) -> None:
        await self._client.close()

    # -- generation --------------------------------------------------------

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        return await self._call(self._build_input(request), request, kind="generate")

    async def inpaint(self, request: GenerationRequest) -> GenerationResult:
        if Capability.INPAINT not in self._capabilities:
            raise ProviderError(self._id, "este modelo no hace inpainting", retryable=False)
        if not request.source_image_path or not request.mask_path:
            raise ProviderError(self._id, "inpaint necesita imagen y mascara", retryable=False)

        payload = self._build_input(request)
        payload["mask"] = to_data_uri(request.mask_path)
        return await self._call(payload, request, kind="inpaint")

    # -- internals ---------------------------------------------------------

    def _build_input(self, request: GenerationRequest) -> dict:
        payload: dict[str, object] = {"prompt": request.prompt}

        if request.negative_prompt:
            payload["negative_prompt"] = request.negative_prompt
        if request.seed is not None:
            payload["seed"] = int(request.seed)
        if request.steps is not None:
            payload["num_inference_steps"] = int(request.steps)
        if request.guidance is not None:
            payload["guidance_scale"] = float(request.guidance)

        payload["width"] = min(request.width, self._max_resolution[0])
        payload["height"] = min(request.height, self._max_resolution[1])

        if request.source_image_path:
            payload["image"] = to_data_uri(request.source_image_path)
            if request.strength is not None:
                payload["prompt_strength"] = float(request.strength)

        payload.update(self._extra)
        payload.update({k: v for k, v in request.extra.items() if not k.startswith("_")})
        return payload

    def _create_url(self) -> str:
        # A pinned version is reproducible; an official model slug tracks the
        # latest. Pin in providers.json for anything she will rely on, because
        # a model that changes under her is a quality regression nobody
        # ordered.
        if self._version:
            return "/v1/predictions"
        return f"/v1/models/{self._model}/predictions"

    async def _call(
        self, model_input: dict, request: GenerationRequest, *, kind: str
    ) -> GenerationResult:
        started = time.monotonic()

        body: dict[str, object] = {"input": model_input}
        if self._version:
            body["version"] = self._version

        prediction = await self._client.request_json("POST", self._create_url(), json=body)
        prediction = await self._settle(prediction)

        status = prediction.get("status")
        if status != "succeeded":
            detail = prediction.get("error") or status or "sin estado"
            raise ProviderError(
                self._id,
                f"la prediccion no ha salido bien: {detail}",
                # A failed prediction is usually transient (a busy GPU, an OOM)
                # rather than a bad request, so the orchestrator may retry.
                retryable=status != "canceled",
            )

        url = first_image_url(prediction)
        if not url:
            raise ProviderError(
                self._id,
                f"la respuesta no traia imagen: {str(prediction.get('output'))[:300]}",
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

        return GenerationResult(
            image_path=str(destination),
            provider_id=self._id,
            cost_usd=self._cost,
            elapsed_s=time.monotonic() - started,
            seed=request.seed,
            reproduction={
                "provider_id": self._id,
                "model": self._model,
                "version": self._version,
                "prompt": request.prompt,
                "negative_prompt": request.negative_prompt,
                "seed": request.seed,
                "guidance": request.guidance,
                "steps": request.steps,
                "source_image_path": request.source_image_path,
                "kind": kind,
            },
        )

    async def _settle(self, prediction: dict) -> dict:
        """Poll until the prediction reaches a terminal state.

        Usually a no-op: `Prefer: wait` means it has normally already settled
        by the time the create call returns.
        """
        if prediction.get("status") in TERMINAL_STATES:
            return prediction

        get_url = (prediction.get("urls") or {}).get("get")
        prediction_id = prediction.get("id")
        if not get_url and not prediction_id:
            raise ProviderError(
                self._id, "no puedo seguir la prediccion: falta id y url", retryable=False
            )
        path = get_url or f"/v1/predictions/{prediction_id}"

        deadline = time.monotonic() + self._max_wait
        delay = self._poll_interval
        while time.monotonic() < deadline:
            await asyncio.sleep(delay)
            prediction = await self._client.request_json("GET", path)
            if prediction.get("status") in TERMINAL_STATES:
                return prediction
            # Ease off gradually so a slow model does not generate hundreds of
            # polls, while a quick one is still noticed promptly.
            delay = min(delay * 1.5, 5.0)

        raise ProviderError(
            self._id, f"la prediccion no ha terminado en {self._max_wait:.0f}s"
        )
