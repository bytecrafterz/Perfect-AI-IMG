"""The real provider adapters, against a mock HTTP transport.

No network and no key, but the parts that actually break are exercised for
real: what goes out on the wire, what comes back, what happens when it comes
back wrong, and whether a failure is retried or refused.

What this CANNOT prove is that a live service accepts these bodies. Model
input schemas differ between models and change between versions. That is what
`scripts/check_provider.py` is for - one real call, printing exactly what came
back, so a mismatch is a two-minute fix rather than a batch that dies after
paying for every image.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.contracts.provider import (
    Capability,
    GenerationRequest,
    PromptDialect,
    Tier,
)
from app.providers.base import ProviderError
from app.providers.fal import FalProvider
from app.providers.http_base import first_image_url, to_data_uri
from app.providers.loader import build_registry
from app.providers.replicate import ReplicateProvider

PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6360000002000100ffff03000006"
    "000557bfabd40000000049454e44ae426082"
)


@pytest.fixture
def source_image(tmp_path: Path) -> Path:
    path = tmp_path / "source.png"
    path.write_bytes(PNG_1PX)
    return path


class Recorder:
    """Captures outbound requests so the body can be asserted on."""

    def __init__(self, responder) -> None:
        self.requests: list[httpx.Request] = []
        self._responder = responder

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return self._responder(request)

        return httpx.MockTransport(handle)

    def body(self, index: int = 0) -> dict:
        return json.loads(self.requests[index].content)


def fal_provider(tmp_path: Path, recorder: Recorder, **overrides) -> FalProvider:
    kwargs = dict(
        provider_id="fal.test",
        model="fal-ai/flux/schnell",
        tiers=[Tier.PREVIEW],
        capabilities=[Capability.TEXT_TO_IMAGE, Capability.IMAGE_TO_IMAGE, Capability.INPAINT],
        cost_per_call_usd=0.003,
        output_dir=tmp_path / "out",
        api_key="test-key",
        transport=recorder.transport(),
    )
    kwargs.update(overrides)
    return FalProvider(**kwargs)  # type: ignore[arg-type]


def image_response(request: httpx.Request) -> httpx.Response:
    """Success for a generate call, and the bytes for the download that
    follows it."""
    if request.url.host != "fal.run":
        return httpx.Response(200, content=PNG_1PX)
    return httpx.Response(
        200,
        json={"images": [{"url": "https://cdn.example/out.png"}], "seed": 4242},
    )


# -- response shapes ----------------------------------------------------------


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"images": [{"url": "A"}]}, "A"),
        ({"images": ["A"]}, "A"),
        ({"image": {"url": "A"}}, "A"),
        ({"image": "A"}, "A"),
        ({"output": ["A", "B"]}, "A"),
        ({"output": "A"}, "A"),
        ({"output": [{"url": "A"}]}, "A"),
        ({"url": "A"}, "A"),
        ({"nothing": 1}, None),
        ({"images": []}, None),
    ],
)
def test_image_url_is_found_in_every_shape_providers_use(payload, expected) -> None:
    """Guessing one envelope is how a batch dies after paying for every image."""
    assert first_image_url(payload) == expected


# -- fal ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fal_sends_prompt_and_saves_the_image(tmp_path: Path) -> None:
    recorder = Recorder(image_response)
    provider = fal_provider(tmp_path, recorder)

    result = await provider.generate(
        GenerationRequest(prompt="una mujer en una terraza", width=512, height=640, seed=7)
    )

    body = recorder.body()
    assert body["prompt"] == "una mujer en una terraza"
    assert body["seed"] == 7
    assert body["image_size"] == {"width": 512, "height": 640}
    assert Path(result.image_path).exists()
    assert result.cost_usd == 0.003
    assert result.seed == 4242  # the provider's seed wins, not ours
    await provider.close()


@pytest.mark.asyncio
async def test_fal_sends_the_photo_inline_never_as_a_link(
    tmp_path: Path, source_image: Path
) -> None:
    """These are photographs of a real person and the box has no public URL
    for its files. They go in the body, base64, or not at all."""
    recorder = Recorder(image_response)
    provider = fal_provider(tmp_path, recorder)

    await provider.generate(
        GenerationRequest(prompt="p", source_image_path=str(source_image), strength=0.4)
    )

    body = recorder.body()
    assert body["image_url"].startswith("data:image/png;base64,")
    assert body["strength"] == 0.4
    await provider.close()


@pytest.mark.asyncio
async def test_fal_resolution_is_clamped_to_what_the_model_supports(
    tmp_path: Path,
) -> None:
    recorder = Recorder(image_response)
    provider = fal_provider(tmp_path, recorder, max_resolution=(768, 768))

    await provider.generate(GenerationRequest(prompt="p", width=4096, height=4096))

    assert recorder.body()["image_size"] == {"width": 768, "height": 768}
    await provider.close()


@pytest.mark.asyncio
async def test_fal_inpaint_sends_a_mask(tmp_path: Path, source_image: Path) -> None:
    mask = tmp_path / "mask.png"
    mask.write_bytes(PNG_1PX)
    recorder = Recorder(image_response)
    provider = fal_provider(tmp_path, recorder, inpaint_model="fal-ai/flux-pro/v1/fill")

    await provider.inpaint(
        GenerationRequest(
            prompt="arregla la mano",
            source_image_path=str(source_image),
            mask_path=str(mask),
        )
    )

    assert "fill" in str(recorder.requests[0].url)
    assert recorder.body()["mask_url"].startswith("data:image/png;base64,")
    await provider.close()


@pytest.mark.asyncio
async def test_fal_inpaint_refuses_without_a_mask(
    tmp_path: Path, source_image: Path
) -> None:
    """Silently regenerating the whole frame would destroy the identity the
    gate already validated everywhere else in the image."""
    recorder = Recorder(image_response)
    provider = fal_provider(tmp_path, recorder)

    with pytest.raises(ProviderError) as exc:
        await provider.inpaint(
            GenerationRequest(prompt="p", source_image_path=str(source_image))
        )
    assert exc.value.retryable is False
    await provider.close()


@pytest.mark.asyncio
async def test_a_response_without_an_image_fails_loudly(tmp_path: Path) -> None:
    recorder = Recorder(lambda r: httpx.Response(200, json={"status": "ok"}))
    provider = fal_provider(tmp_path, recorder)

    with pytest.raises(ProviderError) as exc:
        await provider.generate(GenerationRequest(prompt="p"))
    assert "no traia imagen" in str(exc.value)
    await provider.close()


@pytest.mark.asyncio
async def test_the_api_key_never_reaches_the_cdn(tmp_path: Path) -> None:
    """The result URL points at someone else's CDN. Our key has no business
    being sent there, and a leaked key is someone else's generations on her
    bill."""
    recorder = Recorder(image_response)
    provider = fal_provider(tmp_path, recorder)

    await provider.generate(GenerationRequest(prompt="p"))

    api_calls = [r for r in recorder.requests if r.url.host == "fal.run"]
    cdn_calls = [r for r in recorder.requests if r.url.host != "fal.run"]

    assert api_calls and cdn_calls, "expected one API call and one download"
    assert api_calls[0].headers["authorization"] == "Key test-key"
    assert "authorization" not in {k.lower() for k in cdn_calls[0].headers}
    await provider.close()


# -- retry policy -------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_bad_request_is_not_retried(tmp_path: Path) -> None:
    """Retrying a 400 spends money repeating the same mistake."""
    recorder = Recorder(lambda r: httpx.Response(400, json={"detail": "prompt vacio"}))
    provider = fal_provider(tmp_path, recorder)

    with pytest.raises(ProviderError) as exc:
        await provider.generate(GenerationRequest(prompt="p"))

    assert exc.value.retryable is False
    assert len(recorder.requests) == 1, "a 400 must be attempted exactly once"
    assert "prompt vacio" in str(exc.value)
    await provider.close()


@pytest.mark.asyncio
async def test_a_rate_limit_is_retried_then_succeeds(tmp_path: Path) -> None:
    calls = {"n": 0}

    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.host != "fal.run":
            return httpx.Response(200, content=PNG_1PX)
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"detail": "slow down"})
        return httpx.Response(200, json={"images": [{"url": "https://cdn/x.png"}]})

    recorder = Recorder(responder)
    provider = fal_provider(tmp_path, recorder)

    result = await provider.generate(GenerationRequest(prompt="p"))
    assert Path(result.image_path).exists()
    assert calls["n"] == 2
    await provider.close()


# -- replicate ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_replicate_polls_until_the_prediction_settles(tmp_path: Path) -> None:
    states = ["starting", "processing", "succeeded"]
    calls = {"n": 0}

    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.replicate.com":
            return httpx.Response(200, content=PNG_1PX)
        status = states[min(calls["n"], len(states) - 1)]
        calls["n"] += 1
        payload = {
            "id": "pred1",
            "status": status,
            "urls": {"get": "/v1/predictions/pred1"},
        }
        if status == "succeeded":
            payload["output"] = ["https://cdn.example/out.png"]
        return httpx.Response(200, json=payload)

    recorder = Recorder(responder)
    provider = ReplicateProvider(
        provider_id="replicate.test",
        model="black-forest-labs/flux-schnell",
        tiers=[Tier.PREVIEW],
        capabilities=[Capability.TEXT_TO_IMAGE],
        cost_per_call_usd=0.003,
        output_dir=tmp_path / "out",
        api_token="test-token",
        poll_interval_s=0.01,
        transport=recorder.transport(),
    )

    result = await provider.generate(GenerationRequest(prompt="p", seed=11))

    assert Path(result.image_path).exists()
    assert calls["n"] == 3, "should have polled until succeeded"
    assert recorder.requests[0].headers["authorization"] == "Bearer test-token"
    await provider.close()


@pytest.mark.asyncio
async def test_replicate_reports_a_failed_prediction(tmp_path: Path) -> None:
    recorder = Recorder(
        lambda r: httpx.Response(
            200, json={"id": "p", "status": "failed", "error": "OOM en la GPU"}
        )
    )
    provider = ReplicateProvider(
        provider_id="replicate.test",
        model="m",
        tiers=[Tier.PREVIEW],
        capabilities=[Capability.TEXT_TO_IMAGE],
        cost_per_call_usd=0.003,
        output_dir=tmp_path / "out",
        api_token="t",
        transport=recorder.transport(),
    )

    with pytest.raises(ProviderError) as exc:
        await provider.generate(GenerationRequest(prompt="p"))
    assert "OOM" in str(exc.value)
    # A busy or out-of-memory GPU is transient, so the orchestrator may retry.
    assert exc.value.retryable is True
    await provider.close()


@pytest.mark.asyncio
async def test_replicate_uses_the_versioned_endpoint_when_pinned(tmp_path: Path) -> None:
    """A model that changes under her is a quality regression nobody ordered."""
    recorder = Recorder(
        lambda r: httpx.Response(200, content=PNG_1PX)
        if r.url.host != "api.replicate.com"
        else httpx.Response(
            200, json={"status": "succeeded", "output": "https://cdn/x.png"}
        )
    )
    provider = ReplicateProvider(
        provider_id="replicate.pinned",
        model="owner/name",
        version="abc123",
        tiers=[Tier.FINAL],
        capabilities=[Capability.TEXT_TO_IMAGE],
        cost_per_call_usd=0.02,
        output_dir=tmp_path / "out",
        api_token="t",
        transport=recorder.transport(),
    )

    await provider.generate(GenerationRequest(prompt="p"))

    assert recorder.requests[0].url.path == "/v1/predictions"
    assert recorder.body()["version"] == "abc123"
    await provider.close()


# -- the loader ---------------------------------------------------------------


def test_providers_without_keys_are_skipped_and_the_mock_takes_over(
    tmp_path: Path,
) -> None:
    registry, report = build_registry(
        config_path=Path("providers.json"), output_dir=tmp_path, env={}
    )
    assert report.using_mock is True
    assert registry.candidates(tier=Tier.PREVIEW), "must still be able to work"
    assert any("generador de prueba" in m for m in report.messages_es())


def test_a_configured_key_loads_that_provider(tmp_path: Path) -> None:
    registry, report = build_registry(
        config_path=Path("providers.json"),
        output_dir=tmp_path,
        env={"FAL_API_KEY": "abc"},
    )
    assert report.using_mock is False
    assert "fal.flux-schnell" in report.loaded
    assert registry.candidates(tier=Tier.PREVIEW)
    assert registry.candidates(tier=Tier.FINAL)
    # Replicate has no token, so it is skipped quietly rather than shouted about.
    assert not any("replicate" in m.lower() for m in report.messages_es())


def test_a_broken_entry_does_not_take_down_the_registry(tmp_path: Path) -> None:
    """One stray comma should not cost her every provider."""
    config = tmp_path / "providers.json"
    config.write_text(
        json.dumps(
            {
                "providers": [
                    {"id": "broken", "adapter": "fal", "api_key_env": "K"},
                    {
                        "id": "good",
                        "adapter": "fal",
                        "model": "m",
                        "api_key_env": "K",
                        "tiers": ["preview"],
                        "capabilities": ["t2i"],
                        "cost_per_call_usd": 0.01,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    registry, report = build_registry(
        config_path=config, output_dir=tmp_path, env={"K": "key"}
    )
    assert report.loaded == ["good"]
    assert any("broken" in pid for pid, _ in report.skipped)


def test_a_missing_config_falls_back_rather_than_crashing(tmp_path: Path) -> None:
    registry, report = build_registry(
        config_path=tmp_path / "nope.json", output_dir=tmp_path, env={}
    )
    assert report.using_mock is True
    assert registry.all()


def test_prices_come_from_config_not_code(tmp_path: Path) -> None:
    """A price change must be a config edit. The cost line she sees on every
    delivery is computed from these numbers."""
    config = tmp_path / "providers.json"
    config.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "id": "p",
                        "adapter": "fal",
                        "model": "m",
                        "api_key_env": "K",
                        "tiers": ["preview"],
                        "capabilities": ["t2i"],
                        "cost_per_call_usd": 0.0777,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    registry, _ = build_registry(
        config_path=config, output_dir=tmp_path, env={"K": "key"}
    )
    assert registry.get("p").descriptor.cost_per_call_usd == 0.0777


# -- data uri -----------------------------------------------------------------


def test_data_uri_round_trips(source_image: Path) -> None:
    import base64

    uri = to_data_uri(source_image)
    assert uri.startswith("data:image/png;base64,")
    assert base64.standard_b64decode(uri.split(",", 1)[1]) == PNG_1PX
