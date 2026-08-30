"""Three defects that would each have wasted real money on the first paid run.

None of them were visible from the outside. The app started, served, and
reported healthy through all three. They were found by tracing what WOULD
happen once the keys were real, and every one of them cost nothing to fix and
would have cost a paid session to discover.

  1. anthropic 0.42.0 has no messages.parse, so the visual judge raised
     AttributeError into a swallowing except and returned UNKNOWN forever.
     The $5 of credit was unspendable.

  2. fal serves text-to-image and image-to-image from DIFFERENT endpoints.
     Every look routes in_place_edit, so every preview posted her photo to a
     text-to-image path, which returns 200 and ignores the image - producing a
     convincing photograph of a stranger.

  3. The skin check compared three fixed rectangles against a real reference
     and returned a hard FAIL. On a clothed photo those rectangles sample coat
     and background. It failed 6 of the 13 photos its own reference was the
     mean of, and 10 of 10 other real photos - discarding every final she paid
     for, indistinguishable from the generator actually altering her skin.

The shape they share is worth naming: each one degraded into something that
looked like an answer. A swallowed exception, an HTTP 200, a confident FAIL.
That is what these tests are guarding - not the specific bugs, but the habit
of turning "could not" into "did".
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import numpy as np
import pytest

from app.contracts.common import Attribute
from app.contracts.provider import Capability, Tier
from app.contracts.qa_report import CheckOutcome
from app.gate import backends
from app.providers.base import ProviderError
from app.providers.fal import FalProvider

PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6360000002000100ffff03000006000"
    "557bfabd40000000049454e44ae426082"
)


# ---------------------------------------------------------------------------
# 1. The judge must be able to make its call at all
# ---------------------------------------------------------------------------


def test_installed_anthropic_sdk_has_structured_output() -> None:
    """app/gate/judge.py calls client.messages.parse(output_format=...).

    Pinning an SDK without it does not fail at import or at startup - the
    AttributeError is raised at attribute lookup inside a broad `except`, so
    the judge silently returns UNKNOWN on every call and the API credit is
    never touched. The only visible symptom is photos that are never verified,
    which looks exactly like the gate working in degraded mode.
    """
    from anthropic.resources.messages import Messages

    assert hasattr(Messages, "parse"), (
        "installed anthropic SDK has no messages.parse - the visual judge "
        "cannot run and the API credit cannot be spent"
    )


def test_parsed_output_is_readable_on_the_response() -> None:
    """judge.py reads response.parsed_output. It is a property, not a field,
    so a model_fields check would wrongly report it missing."""
    from anthropic.types import ParsedMessage

    assert isinstance(getattr(ParsedMessage, "parsed_output", None), property)


# ---------------------------------------------------------------------------
# 2. An image-to-image job must not be posted to a text-to-image endpoint
# ---------------------------------------------------------------------------


class _Recorder:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if request.url.host != "fal.run":
                return httpx.Response(200, content=PNG_1PX)
            return httpx.Response(
                200, json={"images": [{"url": "https://cdn.example/out.png"}], "seed": 1}
            )

        return httpx.MockTransport(handle)

    @property
    def path(self) -> str:
        return self.requests[0].url.path


def _provider(tmp_path: Path, recorder: _Recorder, **overrides) -> FalProvider:
    kwargs: dict = dict(
        provider_id="fal.test",
        model="fal-ai/flux/schnell",
        tiers=[Tier.PREVIEW],
        capabilities=[Capability.TEXT_TO_IMAGE, Capability.IMAGE_TO_IMAGE],
        cost_per_call_usd=0.003,
        output_dir=tmp_path / "out",
        api_key="test-key",
        transport=recorder.transport(),
    )
    kwargs.update(overrides)
    return FalProvider(**kwargs)  # type: ignore[arg-type]


def _request(**kwargs):
    from app.contracts.provider import GenerationRequest

    base = dict(prompt="una foto", width=512, height=512)
    base.update(kwargs)
    return GenerationRequest(**base)  # type: ignore[arg-type]


@pytest.fixture()
def source(tmp_path: Path) -> Path:
    p = tmp_path / "hers.png"
    p.write_bytes(PNG_1PX)
    return p


@pytest.mark.asyncio
async def test_a_source_image_goes_to_the_image_to_image_endpoint(
    tmp_path: Path, source: Path
) -> None:
    """The defect in one line: fal-ai/flux/schnell accepts an image_url and
    ignores it, so the call succeeds and returns somebody else."""
    rec = _Recorder()
    p = _provider(tmp_path, rec, i2i_model="fal-ai/flux/schnell/image-to-image")
    await p.generate(_request(source_image_path=str(source)))
    await p.close()

    assert rec.path == "/fal-ai/flux/schnell/image-to-image"


@pytest.mark.asyncio
async def test_no_source_image_still_uses_the_text_to_image_endpoint(
    tmp_path: Path,
) -> None:
    rec = _Recorder()
    p = _provider(tmp_path, rec, i2i_model="fal-ai/flux/schnell/image-to-image")
    await p.generate(_request())
    await p.close()

    assert rec.path == "/fal-ai/flux/schnell"


@pytest.mark.asyncio
async def test_a_model_serving_both_from_one_endpoint_is_left_alone(
    tmp_path: Path, source: Path
) -> None:
    """The edit models (kontext) genuinely do both on one path. Declaring no
    i2i_model must keep working rather than becoming an error."""
    rec = _Recorder()
    p = _provider(tmp_path, rec, model="fal-ai/flux-pro/kontext", i2i_model=None)
    await p.generate(_request(source_image_path=str(source)))
    await p.close()

    assert rec.path == "/fal-ai/flux-pro/kontext"


@pytest.mark.asyncio
async def test_text_to_image_only_provider_refuses_a_source_image(
    tmp_path: Path, source: Path
) -> None:
    """Loud beats plausible. A refusal costs a fraction of a cent to find; the
    silent version is found by the client noticing it is not her."""
    rec = _Recorder()
    p = _provider(
        tmp_path, rec, capabilities=[Capability.TEXT_TO_IMAGE], i2i_model=None
    )
    with pytest.raises(ProviderError):
        await p.generate(_request(source_image_path=str(source)))
    await p.close()

    assert not rec.requests, "refused calls must not reach the network or the bill"


def test_the_shipped_config_declares_an_image_to_image_endpoint() -> None:
    """Every catalog look routes in_place_edit, so the preview provider's i2i
    path is the one that actually runs in production."""
    root = Path(__file__).resolve().parent.parent
    entries = json.loads((root / "providers.json").read_text(encoding="utf-8"))["providers"]
    schnell = next(e for e in entries if e["id"] == "fal.flux-schnell")

    assert "i2i" in schnell["capabilities"]
    assert schnell.get("i2i_model"), (
        "fal.flux-schnell claims image-to-image but names no i2i endpoint - "
        "its model path is text-to-image only"
    )
    assert schnell["i2i_model"] != schnell["model"]


# ---------------------------------------------------------------------------
# 3. The skin check must not fail what it cannot measure
# ---------------------------------------------------------------------------


def test_skin_check_is_unknown_without_a_face_detector(monkeypatch) -> None:
    """A guess is not a measurement.

    With no detector, skin_patches samples three fixed rectangles. On a
    clothed or full-length photo they land on fabric and background. Returned
    as a FAIL that discarded every final; returned as UNKNOWN it is recorded,
    surfaced, and blocks only in strict mode - which is what it honestly is.
    """
    from app.config import Thresholds
    from app.gate.gate import Gate
    from app.profile.model import IdentityProfile

    caps = backends.CVCapabilities(
        onnxruntime=False,
        insightface=False,
        opencv=False,
        face_model=False,
        pose_model=False,
    )
    assert caps.face_detector_available is False

    profile = IdentityProfile(skin_lab=[49.1, 14.5, 15.8])
    assert profile.can_check_skin, "the reference exists - this is the FAIL path"

    gate = Gate(
        profile=profile,
        thresholds=Thresholds(),
        models_dir=Path("does-not-exist"),
        strict=False,
    )
    monkeypatch.setattr(gate, "capabilities", caps)

    rgb = np.full((256, 192, 3), 0.5, dtype=np.float32)
    check = gate._check_skin(rgb)

    assert check.outcome is CheckOutcome.UNKNOWN
    assert check.attribute is Attribute.SKIN_TONE
    assert check.outcome is not CheckOutcome.FAIL


def test_a_missing_reference_is_still_unknown() -> None:
    """Unchanged behaviour, asserted so the two UNKNOWN paths stay distinct:
    no reference, versus a reference we cannot measure against."""
    from app.config import Thresholds
    from app.gate.gate import Gate
    from app.profile.model import IdentityProfile

    gate = Gate(
        profile=IdentityProfile(),
        thresholds=Thresholds(),
        models_dir=Path("does-not-exist"),
        strict=False,
    )
    check = gate._check_skin(np.full((64, 64, 3), 0.5, dtype=np.float32))
    assert check.outcome is CheckOutcome.UNKNOWN


def test_face_detector_capability_is_reported_separately() -> None:
    """Same requirement as identity today, deliberately its own flag: the skin
    check needs a BOX, not an embedding."""
    caps = backends.CVCapabilities(
        onnxruntime=True,
        insightface=True,
        opencv=True,
        face_model=True,
        pose_model=False,
    )
    assert caps.face_detector_available is True
    assert caps.proportions_available is False


# ---------------------------------------------------------------------------
# 4. A key pasted with a trailing comment must be caught, not billed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "abc123          # or REPLICATE_API_TOKEN",
        "abc123 # comment",
        "abc123 trailing words",
        "abc 123",
    ],
)
def test_malformed_key_is_reported(monkeypatch, value: str) -> None:
    """load_dotenv strips quotes and whitespace but not a `#`.

    The result is not empty, so every "is it configured?" check passes, the
    provider registers, and the first paid call returns 401 - which reads as a
    rejected key and sends you to the billing page instead of the file. The
    deployment README taught this exact format.
    """
    from app.config import _malformed_key_warnings

    monkeypatch.setenv("FAL_API_KEY", value)
    warnings = _malformed_key_warnings()
    assert warnings, f"not caught: {value!r}"
    assert "FAL_API_KEY" in warnings[0]


@pytest.mark.parametrize("value", ["fal-abc123def456", "sk-ant-api03-XyZ_123"])
def test_a_well_formed_key_is_not_flagged(monkeypatch, value: str) -> None:
    from app.config import _malformed_key_warnings

    monkeypatch.setenv("FAL_API_KEY", value)
    assert _malformed_key_warnings() == []


def test_the_readme_no_longer_teaches_the_broken_format() -> None:
    """The warning above is the durable guard; this stops the documentation
    drifting back into contradicting it."""
    root = Path(__file__).resolve().parent.parent
    readme = (root / "deploy" / "windows" / "README.md").read_text(encoding="utf-8")
    previous = ""
    for line in readme.splitlines():
        stripped = line.strip()
        if stripped.startswith(("FAL_API_KEY=", "ANTHROPIC_API_KEY=", "REPLICATE_API_TOKEN=")):
            # The document deliberately shows the broken form once, to name it.
            # That line is labelled, and a labelled counter-example is the
            # opposite of the problem this guards against.
            if "WRONG" in previous.upper():
                continue
            assert "#" not in stripped, f"README teaches an inline comment: {stripped!r}"
        previous = line
