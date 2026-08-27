"""The whole journey, over HTTP, exactly as her phone would drive it.

  upload a photo -> style screen -> previews -> pick -> finals

Runs against the mock provider, so it proves the plumbing without spending
anything. What it is really guarding is the promises the product is sold on:
one button, no file-type instructions, options chosen for that photo, and
nothing expensive happening before she has chosen.
"""

from __future__ import annotations

import io
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.main import app, services


@pytest.fixture(scope="module")
def client() -> TestClient:
    with TestClient(app) as c:
        # ACCESS_TOKEN is unset in tests, so the magic link accepts anything
        # and sets the session cookie - the same path her private link takes.
        assert c.get("/e/whatever", follow_redirects=False).status_code == 303
        yield c


def photo_bytes(*, width: int = 900, height: int = 1400, color=(150, 120, 100)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, "JPEG", quality=92)
    return buffer.getvalue()


def wait_for(predicate, *, timeout: float = 20.0, interval: float = 0.15):
    """Poll until the background task has done its work.

    Preview generation is fired as a task so the HTTP response can return
    immediately - that is what lets tiles stream in one by one - so the test
    has to wait the same way the browser does.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


# -- auth ---------------------------------------------------------------------


def test_logged_out_is_sent_to_the_entrance() -> None:
    with TestClient(app) as anonymous:
        response = anonymous.get("/", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/entrar"


def test_photographs_are_never_public() -> None:
    """A public URL raises the stakes over a private chat. Every image route
    is behind the session cookie."""
    with TestClient(app) as anonymous:
        assert anonymous.get("/media/anything.png").status_code == 401
        assert anonymous.get("/media/thumb/anything.webp").status_code == 401


def test_static_assets_stay_public() -> None:
    with TestClient(app) as anonymous:
        assert anonymous.get("/static/app.css").status_code == 200
        assert anonymous.get("/manifest.json").status_code == 200
        assert anonymous.get("/sw.js").status_code == 200


# -- the journey --------------------------------------------------------------


def test_upload_accepts_a_normal_phone_photo(client: TestClient) -> None:
    """No instruction about file types: the web upload always sends the
    original, which is why the 'send as Archivo' problem disappeared."""
    response = client.post(
        "/upload", files={"photo": ("IMG_7458.jpg", photo_bytes(), "image/jpeg")}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["role"] == "source"
    assert data["next"].startswith("/estilo/")


def test_upload_rejects_a_non_image_with_a_readable_message(client: TestClient) -> None:
    response = client.post(
        "/upload", files={"photo": ("notes.txt", b"not an image", "text/plain")}
    )
    assert response.status_code == 415
    assert "no he podido leer" in response.json()["error"].lower()


def test_style_screen_offers_options_for_this_photo(client: TestClient) -> None:
    upload = client.post(
        "/upload", files={"photo": ("tall.jpg", photo_bytes(width=900, height=1500), "image/jpeg")}
    ).json()

    page = client.get(upload["next"])
    assert page.status_code == 200
    body = page.text

    # The multi-select rows, and the honest counter above the button.
    assert "puedes marcar varios" in body
    assert "ROPA" in body and "GESTO" in body and "EXPRESION" in body
    assert "Ver 6 opciones" in body


def test_full_journey_upload_to_finished_photos(client: TestClient) -> None:
    upload = client.post(
        "/upload", files={"photo": ("shoot.jpg", photo_bytes(), "image/jpeg")}
    ).json()
    image_id = upload["image_id"]

    # She marks two garments and one expression: SEVERAL -> vary across
    # exactly those, ONE -> identical in all six.
    started = client.post(
        "/previews",
        data={
            "image_id": image_id,
            "look_id": "moda_terraza_atardecer",
            "selections": '{"garment": ["vestido largo", "traje sastre"],'
                          ' "expression": ["sonrisa suave"]}',
        },
    )
    assert started.status_code == 200
    session_id = started.json()["session_id"]
    assert started.json()["expected"] == 6

    state = services.orchestrator.session(session_id)
    assert wait_for(lambda: len(state.candidates) >= 6), "previews did not arrive"

    # Her selection was honoured in every single preview.
    from app.contracts.common import Attribute

    for candidate in state.candidates.values():
        values = candidate.slot.values
        assert values[Attribute.GARMENT] in {"vestido largo", "traje sastre"}
        assert values[Attribute.EXPRESSION] == "sonrisa suave"

    # She picks two.
    chosen = list(state.candidates)[:2]
    finals = client.post(
        "/finals",
        data={"session_id": session_id, "chosen": f'["{chosen[0]}", "{chosen[1]}"]'},
    )
    assert finals.status_code == 200

    assert wait_for(lambda: len(state.finals) >= 2), "finals did not arrive"

    # THE STRUCTURAL SAVING: the expensive stage ran only on what she chose.
    kinds = [e.kind for e in services.ledger.entries if e.session_id == session_id]
    assert kinds.count("preview") >= 6
    assert kinds.count("final") == 2

    page = client.get(f"/resultado/{session_id}")
    assert page.status_code == 200


def test_gallery_serves_thumbnails_not_originals(client: TestClient) -> None:
    """Full-resolution photographs over mobile data would make this screen
    unusable, which is why derivatives are built on the way in."""
    client.post("/upload", files={"photo": ("g.jpg", photo_bytes(), "image/jpeg")})
    page = client.get("/galeria")
    assert page.status_code == 200
    assert "/media/thumb/" in page.text or "Todavía no hay nada" in page.text


def test_settings_reports_every_degraded_mode(client: TestClient) -> None:
    """A half-configured system announces itself. She should never be the one
    to discover that identity verification was off."""
    page = client.get("/ajustes")
    assert page.status_code == 200
    assert "Cosas que revisar" in page.text
    assert "identidad" in page.text.lower()


def test_health_is_honest_about_what_is_not_running(client: TestClient) -> None:
    payload = client.get("/health").json()
    assert payload["ok"] is True
    assert payload["catalog"] >= 1
    # No CV models in CI, so this must report False rather than quietly
    # claiming the gate is checking identity.
    assert payload["identity_verification"] is False
    assert payload["strict_gate"] is False
    assert payload["warnings"]
