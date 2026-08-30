"""The free generation path.

Every other stage of this pipeline already costs nothing - the gate, the
catalog, the combination walk, the proportion measurement are all local
Python. Generation was the one step this box cannot do: measured at
231 GFLOP/s, the fastest distilled model on this CPU is ~63 s per image
against a promise of six previews in under two minutes.

Cloudflare Workers AI runs it on a GPU under a genuinely recurring free
allocation - 10,000 neurons a day, reset at 00:00 UTC, hard-stopping rather
than billing. Roughly 19 complete sessions daily at no cost.

Three things about this adapter differ from fal and replicate, and each is
tested here because each would fail in a way that is easy to misread:

  multipart, not JSON     the endpoint takes form data even with no image
  base64 inline           the image comes back in the envelope, not as a URL
  429 means two things    ordinary throttling, or the daily allocation gone
"""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path

import httpx
import pytest
from PIL import Image

from app.contracts.provider import Capability, GenerationRequest, PromptDialect, Tier
from app.providers.base import ProviderError
from app.providers.cloudflare import MAX_INPUT_SIDE, CloudflareProvider


def _png_bytes(width: int = 64, height: int = 64) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (120, 90, 70)).save(buffer, format="PNG")
    return buffer.getvalue()


class _Recorder:
    """Captures the outbound request so the multipart body can be asserted on."""

    def __init__(self, responder=None) -> None:
        self.requests: list[httpx.Request] = []
        self._responder = responder or self._ok

    @staticmethod
    def _ok(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "result": {"image": base64.b64encode(_png_bytes()).decode()},
                "success": True,
                "errors": [],
            },
        )

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return self._responder(request)

        return httpx.MockTransport(handle)

    @property
    def body(self) -> bytes:
        return self.requests[0].content

    @property
    def content_type(self) -> str:
        return self.requests[0].headers.get("content-type", "")


def _provider(tmp_path: Path, recorder: _Recorder, **overrides) -> CloudflareProvider:
    kwargs: dict = dict(
        provider_id="cloudflare.test",
        model="@cf/black-forest-labs/flux-2-klein-4b",
        tiers=[Tier.PREVIEW],
        capabilities=[Capability.TEXT_TO_IMAGE, Capability.IMAGE_TO_IMAGE],
        cost_per_call_usd=0.0,
        output_dir=tmp_path / "out",
        api_key="cf-test-token",
        account_id="acct123",
        prompt_dialect=PromptDialect.INSTRUCTIONAL,
        transport=recorder.transport(),
    )
    kwargs.update(overrides)
    return CloudflareProvider(**kwargs)  # type: ignore[arg-type]


def _request(**kwargs) -> GenerationRequest:
    base = dict(prompt="wearing a dark wool coat", width=512, height=512)
    base.update(kwargs)
    return GenerationRequest(**base)  # type: ignore[arg-type]


@pytest.fixture()
def source(tmp_path: Path) -> Path:
    p = tmp_path / "hers.png"
    p.write_bytes(_png_bytes(1400, 1800))  # a phone-sized portrait
    return p


# ---------------------------------------------------------------------------
# transport
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_it_posts_multipart_not_json(tmp_path: Path) -> None:
    """The endpoint takes form data even when there is no image to send."""
    rec = _Recorder()
    p = _provider(tmp_path, rec)
    await p.generate(_request())
    await p.close()

    assert "multipart/form-data" in rec.content_type
    assert b"boundary=" in rec.content_type.encode() or "boundary=" in rec.content_type


@pytest.mark.asyncio
async def test_the_account_id_is_in_the_path(tmp_path: Path) -> None:
    """It is a path segment, not a header - a missing account is a 404 rather
    than an auth error, which reads as the wrong problem entirely."""
    rec = _Recorder()
    p = _provider(tmp_path, rec)
    await p.generate(_request())
    await p.close()

    assert "/accounts/acct123/ai/run/" in rec.requests[0].url.path
    assert rec.requests[0].headers["authorization"] == "Bearer cf-test-token"


@pytest.mark.asyncio
async def test_content_type_is_left_for_httpx_to_set(tmp_path: Path) -> None:
    """Setting it by hand loses the boundary and the server cannot parse the
    body - a 400 that looks like a bad prompt."""
    rec = _Recorder()
    p = _provider(tmp_path, rec)
    await p.generate(_request())
    await p.close()

    assert rec.content_type.startswith("multipart/form-data; boundary=")


# ---------------------------------------------------------------------------
# image-to-image - the whole point
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_her_photo_is_sent_as_input_image_0(tmp_path: Path, source: Path) -> None:
    """Index matters: the prompt can address references by number, and 0 is
    the subject everywhere in this codebase."""
    rec = _Recorder()
    p = _provider(tmp_path, rec)
    await p.generate(_request(source_image_path=str(source)))
    await p.close()

    assert b'name="input_image_0"' in rec.body


@pytest.mark.asyncio
async def test_input_images_are_downscaled_below_the_cap(
    tmp_path: Path, source: Path
) -> None:
    """Cloudflare rejects anything 512 or larger on either axis, so this is
    not an optimisation - an oversized reference is a hard failure."""
    rec = _Recorder()
    p = _provider(tmp_path, rec)
    await p.generate(_request(source_image_path=str(source)))
    await p.close()

    start = rec.body.index(b"\x89PNG")
    sent = Image.open(io.BytesIO(rec.body[start:]))
    assert max(sent.size) <= MAX_INPUT_SIDE
    assert sent.size[0] < 512 and sent.size[1] < 512
    # aspect ratio preserved - a squashed reference is a different body
    assert sent.size[1] > sent.size[0]


@pytest.mark.asyncio
async def test_extra_references_follow_in_order(tmp_path: Path, source: Path) -> None:
    extra = tmp_path / "ref.png"
    extra.write_bytes(_png_bytes(800, 800))
    rec = _Recorder()
    p = _provider(tmp_path, rec)
    await p.generate(
        _request(source_image_path=str(source), reference_image_paths=[str(extra)])
    )
    await p.close()

    assert b'name="input_image_0"' in rec.body
    assert b'name="input_image_1"' in rec.body


@pytest.mark.asyncio
async def test_no_source_image_sends_no_reference(tmp_path: Path) -> None:
    rec = _Recorder()
    p = _provider(tmp_path, rec)
    await p.generate(_request())
    await p.close()

    assert b'name="input_image_0"' not in rec.body
    assert b'name="prompt"' in rec.body


@pytest.mark.asyncio
async def test_a_text_only_provider_refuses_a_source_image(
    tmp_path: Path, source: Path
) -> None:
    rec = _Recorder()
    p = _provider(tmp_path, rec, capabilities=[Capability.TEXT_TO_IMAGE])
    with pytest.raises(ProviderError):
        await p.generate(_request(source_image_path=str(source)))
    await p.close()
    assert not rec.requests, "a refusal must not reach the network"


@pytest.mark.asyncio
async def test_steps_are_never_sent(tmp_path: Path) -> None:
    """The model is distilled to a fixed 4 steps and ignores the parameter.
    Sending it invites the belief that quality can be traded for time."""
    rec = _Recorder()
    p = _provider(tmp_path, rec)
    await p.generate(_request(steps=30))
    await p.close()

    assert b'name="steps"' not in rec.body


# ---------------------------------------------------------------------------
# response
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_base64_image_is_decoded_and_written(tmp_path: Path) -> None:
    """It comes back inline in the envelope, not as a URL - so unlike fal and
    replicate there is no second fetch."""
    rec = _Recorder()
    p = _provider(tmp_path, rec)
    result = await p.generate(_request())
    await p.close()

    written = Path(result.image_path)
    assert written.exists()
    assert Image.open(written).size == (64, 64)
    assert result.cost_usd == 0.0
    assert result.reproduction["provider_id"] == "cloudflare.test"


@pytest.mark.asyncio
async def test_a_success_false_envelope_is_an_error(tmp_path: Path) -> None:
    """HTTP 200 with success:false is Cloudflare's normal failure shape.
    Treating 200 as success would store an empty file and call it a photo."""
    rec = _Recorder(
        lambda _r: httpx.Response(
            200, json={"success": False, "errors": [{"code": 1234, "message": "nope"}], "result": None}
        )
    )
    p = _provider(tmp_path, rec)
    with pytest.raises(ProviderError, match="nope"):
        await p.generate(_request())
    await p.close()


@pytest.mark.asyncio
async def test_a_corrupt_base64_payload_fails_loudly(tmp_path: Path) -> None:
    rec = _Recorder(
        lambda _r: httpx.Response(200, json={"success": True, "result": {"image": "not!base64!"}})
    )
    p = _provider(tmp_path, rec)
    with pytest.raises(ProviderError):
        await p.generate(_request())
    await p.close()


# ---------------------------------------------------------------------------
# the daily allocation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quota_exhaustion_is_not_retried(tmp_path: Path) -> None:
    """429 means two different things here.

    Ordinary throttling is worth retrying. The daily free allocation running
    out is not - it returns at 00:00 UTC and no amount of backoff hurries it.
    429 is in RETRYABLE_STATUS, so without special-casing error 3036 every
    request for the rest of the day would spend three attempts and the full
    backoff to re-learn the same fact.

    The code is 3036, verified against Cloudflare's error table. An earlier
    draft used 4006, which does not appear in that table at all - the check
    would have matched nothing and the retry storm would have happened
    anyway, which is precisely the failure this test exists to prevent.
    """
    calls = {"n": 0}

    def responder(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            429,
            json={
                "success": False,
                "errors": [
                    {"code": 3036, "message": "You have used up your daily free allocation of 10,000 neurons."}
                ],
            },
        )

    rec = _Recorder(responder)
    p = _provider(tmp_path, rec)
    with pytest.raises(ProviderError) as caught:
        await p.generate(_request())
    await p.close()

    assert calls["n"] == 1, "quota exhaustion must not be retried"
    assert caught.value.retryable is False
    assert "00:00" in str(caught.value), "the message should say when it comes back"


@pytest.mark.asyncio
async def test_ordinary_throttling_is_still_retried(tmp_path: Path) -> None:
    """The counterpart: a plain 429 must keep its retry, or a busy minute
    becomes a failed session."""
    calls = {"n": 0}

    def responder(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            # 3040 "Out of capacity" - a different 429 entirely, and one that
            # must keep its retry or a busy minute becomes a failed session.
            return httpx.Response(
                429,
                json={"success": False, "errors": [{"code": 3040, "message": "Out of capacity"}]},
            )
        return _Recorder._ok(_request)

    rec = _Recorder(responder)
    p = _provider(tmp_path, rec)
    result = await p.generate(_request())
    await p.close()

    assert calls["n"] == 2
    assert Path(result.image_path).exists()


@pytest.mark.asyncio
async def test_inpaint_declines_rather_than_pretending(tmp_path: Path, source: Path) -> None:
    """The endpoint has no mask parameter. Accepting the call and rewriting
    the whole photograph would return a plausible image that quietly discards
    the localisation the repair loop asked for."""
    rec = _Recorder()
    p = _provider(tmp_path, rec, capabilities=[Capability.INPAINT, Capability.IMAGE_TO_IMAGE])
    with pytest.raises(ProviderError, match="mascara"):
        await p.inpaint(_request(source_image_path=str(source)))
    await p.close()
    assert not rec.requests


# ---------------------------------------------------------------------------
# the shipped configuration
# ---------------------------------------------------------------------------


def _entries() -> list[dict]:
    root = Path(__file__).resolve().parent.parent
    return json.loads((root / "providers.json").read_text(encoding="utf-8"))["providers"]


def test_only_the_permissively_licensed_model_is_configured() -> None:
    """The 4B weights are Apache 2.0; klein-9B and flux-2-dev are
    NON-COMMERCIAL. This is paid client work, so a well-meaning upgrade to the
    bigger model would be a licence breach rather than a quality improvement,
    and it would be invisible until someone asked the right question.

    This asserts the weight licence only. Use of Cloudflare's HOSTED endpoint
    is additionally governed by BFL's Terms of Service, which the model page
    links for the 9B as well - so this test is a floor, not a clearance.
    """
    for entry in _entries():
        if entry.get("adapter") != "cloudflare":
            continue
        assert entry["model"] == "@cf/black-forest-labs/flux-2-klein-4b", (
            f"{entry['id']} uses {entry['model']} - only the 4B is Apache 2.0"
        )


def test_the_free_preview_tier_is_enabled_and_costs_nothing() -> None:
    entry = next(e for e in _entries() if e["id"] == "cloudflare.flux-klein-preview")
    assert entry["enabled"] is True
    assert entry["cost_per_call_usd"] == 0.0
    assert "i2i" in entry["capabilities"]
    assert entry["max_resolution"] == [512, 512]


def test_the_free_final_tier_ships_disabled() -> None:
    """It works, but klein-4B is below fal's kontext, and finals are what she
    keeps and shows people. Enabling it is a deliberate budget decision, not
    a default."""
    entry = next(e for e in _entries() if e["id"] == "cloudflare.flux-klein-final")
    assert entry["enabled"] is False
