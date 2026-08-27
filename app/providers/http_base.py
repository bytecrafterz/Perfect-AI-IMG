"""Shared plumbing for HTTP image providers.

Everything here is provider-neutral: sending an image, getting one back,
retrying sensibly, and failing in a way the orchestrator can act on.

Two decisions worth knowing about:

  IMAGES GO OUT AS DATA URIs.  The box has no public URL for its own files -
  it sits behind Caddy with no directory listing, and these are photographs of
  a real person that must not be exposed to fetch a generation.  Base64 in the
  request body is larger on the wire and completely private.

  RETRIES ARE NARROW.  A 429 or a 5xx is worth retrying; a 400 means the
  request was wrong and retrying it just spends money on the same mistake.
"""

from __future__ import annotations

import asyncio
import base64
import mimetypes
import random
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.providers.base import ProviderError

#: Generation is slow by nature. The read timeout has to outlast a busy
#: queue, but not so long that a wedged request holds a slot for ever.
DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=180.0, write=60.0, pool=10.0)

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


def to_data_uri(path: str | Path) -> str:
    path = Path(path)
    media_type = mimetypes.guess_type(path.name)[0] or "image/png"
    encoded = base64.standard_b64encode(path.read_bytes()).decode("ascii")
    return f"data:{media_type};base64,{encoded}"


@dataclass(frozen=True)
class Attempt:
    """One try, and why it failed if it did."""

    status: int | None
    detail: str
    retryable: bool


class HttpProviderClient:
    """A thin, shared HTTP client with the retry policy attached.

    One client per provider so connection pools are reused; closed by the
    registry at shutdown.
    """

    def __init__(
        self,
        *,
        provider_id: str,
        base_url: str,
        headers: dict[str, str],
        max_attempts: int = 3,
        timeout: httpx.Timeout | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._provider_id = provider_id
        self._max_attempts = max_attempts
        self._timeout = timeout or DEFAULT_TIMEOUT
        # Kept so downloads can share it. In production it is None and httpx
        # uses the real network; in tests it is a MockTransport, which is what
        # lets the whole request/response path be exercised without a key.
        self._transport = transport
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers=headers,
            timeout=self._timeout,
            transport=transport,
            follow_redirects=True,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def request_json(
        self, method: str, url: str, *, json: dict | None = None
    ) -> dict:
        """Send, retry what is worth retrying, and raise something useful."""
        last: Attempt | None = None

        for attempt in range(1, self._max_attempts + 1):
            try:
                response = await self._client.request(method, url, json=json)
            except httpx.TimeoutException as exc:
                last = Attempt(None, f"timeout: {exc}", retryable=True)
            except httpx.HTTPError as exc:
                last = Attempt(None, f"network: {exc}", retryable=True)
            else:
                if response.status_code < 400:
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise ProviderError(
                            self._provider_id,
                            f"respuesta no es JSON: {exc}",
                            retryable=False,
                        ) from exc

                detail = _short_body(response)
                retryable = response.status_code in RETRYABLE_STATUS
                last = Attempt(response.status_code, detail, retryable)

                # A 4xx that is not a rate limit means the request itself was
                # wrong. Retrying spends money repeating the same mistake.
                if not retryable:
                    raise ProviderError(
                        self._provider_id,
                        f"HTTP {response.status_code}: {detail}",
                        retryable=False,
                    )

            if attempt < self._max_attempts:
                await asyncio.sleep(_backoff(attempt))

        assert last is not None
        raise ProviderError(
            self._provider_id,
            f"agotados {self._max_attempts} intentos - {last.detail}",
            retryable=True,
        )

    async def download(self, url: str, destination: Path) -> Path:
        """Fetch a generated image to disk.

        Result URLs are usually signed and short-lived, so this happens
        immediately rather than storing the URL and fetching later.
        """
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            # A SEPARATE client on purpose: the result URL points at whatever
            # CDN the provider uses, and our API key has no business being
            # sent there. Same transport, no headers.
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as fetcher:
                response = await fetcher.get(url, follow_redirects=True)
                response.raise_for_status()
                destination.write_bytes(response.content)
        except httpx.HTTPError as exc:
            raise ProviderError(
                self._provider_id, f"no he podido descargar el resultado: {exc}"
            ) from exc
        return destination


def _backoff(attempt: int) -> float:
    """Exponential, with jitter so parallel slots in one batch do not all
    retry on the same beat and hammer a provider that is already struggling."""
    return min(8.0, (2 ** (attempt - 1))) * (0.6 + random.random() * 0.8)


def _short_body(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:200]
    for key in ("detail", "error", "message", "title"):
        if key in payload:
            return str(payload[key])[:300]
    return str(payload)[:300]


def first_image_url(payload: dict) -> str | None:
    """Find the image URL in a provider response.

    Deliberately tolerant. Response envelopes differ between providers and
    change between model versions, and the failure mode of guessing one shape
    is a batch that dies after paying for every image. The shapes covered:

        {"images": [{"url": ...}]}      fal and most of its models
        {"image": {"url": ...}}         single-image variants
        {"output": ["https://..."]}     replicate
        {"output": "https://..."}       replicate, single output
        {"url": ...}                    bare
    """
    images = payload.get("images")
    if isinstance(images, list) and images:
        first = images[0]
        if isinstance(first, dict):
            url = first.get("url")
            if isinstance(url, str):
                return url
        elif isinstance(first, str):
            return first

    image = payload.get("image")
    if isinstance(image, dict) and isinstance(image.get("url"), str):
        return image["url"]
    if isinstance(image, str):
        return image

    output = payload.get("output")
    if isinstance(output, list) and output:
        candidate = output[0]
        if isinstance(candidate, str):
            return candidate
        if isinstance(candidate, dict) and isinstance(candidate.get("url"), str):
            return candidate["url"]
    if isinstance(output, str):
        return output

    url = payload.get("url")
    return url if isinstance(url, str) else None
