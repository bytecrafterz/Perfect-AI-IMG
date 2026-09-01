"""The web app.

One button, a few taps, under two minutes.  The route list is deliberately
short, because every screen she does not have to learn is a screen that
cannot confuse her:

    /e/{token}         the private link, once.  Sets the cookie.
    /                  Crear - the upload button and recent sessions
    /estilo/{sid}      the style screen: quick styles + multi-select rows
    /opciones/{sid}    the six previews, filling in one by one
    /resultado/{sid}   the finished photographs
    /galeria           everything ever made
    /ajustes           four items only

Everything else is API or media.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
import json
import time
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

from app.analysis.analyser import build_analyser
from app.analysis.photo_router import REFERENCE_TARGETS, PhotoRole, PhotoRouter
from app.auth import COOKIE_NAME, Auth
from app.balance import BalanceBook
from app.catalog import Catalog, ProposalEngine, default_chip_rows
from app.compile.compiler import PromptCompiler
from app.config import settings
from app.contracts.common import Attribute
from app.contracts.provider import Tier
from app.contracts.selections import Selections
from app.gate import backends
from app.gate.backends import FaceBackend
from app.gate.gate import Gate, resolve_strict
from app.gate.judge import VisualJudge
from app.images import UnsupportedImage, build_derivatives, destroy, store_upload
from app.ledger import BudgetExceeded, Ledger
from app.orchestrator.engine import Orchestrator
from app.orchestrator.events import EventKind, bus
from app.profile.model import IdentityProfile
from app.providers.base import ProviderError
from app.providers.loader import build_registry
from app.providers.router import Bandit, Router
from app.store import Store

APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

app = FastAPI(title="Estudio", docs_url=None, redoc_url=None)
# Static assets are public: stylesheet, icons, service worker. No photograph
# is ever served from here - those go through /media, which is behind auth.
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


class Services:
    """Everything assembled once at startup.

    Built here rather than as module globals so tests can construct a parallel
    set against a temporary directory without touching real data.
    """

    def __init__(self) -> None:
        settings.ensure_dirs()

        self.store = Store(settings.db_path)
        self.auth = Auth(
            secret_key=settings.secret_key,
            access_token=settings.access_token,
            max_age_days=settings.session_days,
        )
        self.catalog = Catalog(settings.catalog_dir).load()
        self.proposals = ProposalEngine()

        self.profile = IdentityProfile.load(settings.profile_dir) or IdentityProfile(
            owner=settings.owner_name
        )

        # Built from providers.json.  Entries whose API key is unset are
        # skipped, so the file can list everything and the machine runs
        # whatever it has keys for.  With none, the local mock is used and
        # says so.  Nothing upstream changes either way - the router selects
        # on declared capability, never on a provider's name.
        self.registry, self.provider_report = build_registry(
            config_path=settings.providers_path,
            output_dir=settings.images_dir,
        )

        bandit = Bandit()
        bandit.load(self.store.load_bandit())
        self.router = Router(self.registry, bandit=bandit)

        judge = (
            VisualJudge(api_key=settings.anthropic_api_key, model=settings.judge_model)
            if settings.has_language_model
            else None
        )
        # Strict means an unmeasurable check blocks the image.  Turning it on
        # while a check genuinely cannot run discards everything, so it is
        # resolved from the profile AND the installed capabilities together -
        # see resolve_strict.  Overridable with STRICT_GATE for the day this
        # judgement is wrong.
        strict, self.strict_blocked_by = resolve_strict(
            self.profile,
            backends.detect_capabilities(settings.models_dir),
            settings.strict_gate,
        )
        self.gate = Gate(
            profile=self.profile,
            thresholds=settings.thresholds,
            models_dir=settings.models_dir,
            judge=judge,
            strict=strict,
        )
        self.analyser = build_analyser(
            api_key=settings.anthropic_api_key, model=settings.analyser_model
        )
        self.face = FaceBackend(settings.models_dir)
        self.photo_router = PhotoRouter(profile=self.profile, face=self.face)

        self.ledger = Ledger(
            per_session_usd=settings.caps.per_session_usd,
            per_day_usd=settings.caps.per_day_usd,
            balance_floor_usd=settings.caps.balance_floor_usd,
        )
        # Load today's spend back in, or the daily cap is fiction: the ledger
        # is an in-memory accumulator, so without this it starts at zero on
        # every boot - under a watchdog configured to restart 999 times, which
        # means the "$10 per day" limit could be re-earned all afternoon.
        self.ledger.rehydrate(self.store.costs_since(time.time() - 86_400))
        self.orchestrator = Orchestrator(
            settings=settings,
            router=self.router,
            gate=self.gate,
            compiler=PromptCompiler(),
            ledger=self.ledger,
            bus=bus,
        )
        self.balances = BalanceBook(self.store)
        self.uploads: dict[str, str] = {}

    def warnings(self) -> list[str]:
        """Everything currently degraded, in her language.

        Shown on the settings screen and logged at boot.  A half-configured
        system should announce itself rather than quietly produce worse work.
        """
        out = list(settings.degraded_modes())
        out.extend(self.provider_report.messages_es())
        out.extend(self.gate.status_es())
        # Why unknowns are not blocking. Without this the gate silently runs
        # permissive and the only clue is a line in every report.
        for reason in self.strict_blocked_by:
            out.append(f"Modo no estricto: {reason}")
        if self.auth.is_open:
            out.append(
                "ACCESS_TOKEN no configurada: cualquiera con la direccion puede entrar"
            )
        if len(self.catalog) == 0:
            out.append("El catalogo esta vacio: no hay estilos que ofrecer")
        return out

    def warnings_for_her(self) -> list[str]:
        """What SHE needs to know, in her words. Usually nothing.

        The full list is for whoever maintains this and belongs on Ajustes.
        Putting it on her home screen produced seven lines of jargon under a
        red heading - insightface, buffalo_l, centroide, MODO NO ESTRICTO -
        none of which she can act on and all of which read as "it is broken".
        The app was working perfectly at the time.

        Two things genuinely change what she receives, and both are said
        without naming a single library:

          the pictures are not real     she must not send a placeholder to a
                                        client believing it is a photograph
          they are not fully checked    the promise of this product is that
                                        the result respects her body, and
                                        right now that cannot be confirmed

        Everything else - which package is missing, which flag is off - is our
        problem, not hers.
        """
        out: list[str] = []

        if self.provider_report.using_mock:
            out.append(
                "Ahora mismo no estoy generando fotos de verdad, solo ejemplos "
                "de prueba. No las uses como si fueran tuyas."
            )

        unverified = [
            name
            for name, ready in (
                ("que salgas tu", self.profile.can_check_identity),
                ("tus proporciones", self.profile.can_check_proportions),
            )
            if not ready
        ]
        # Money first. She asked to be warned BEFORE the credit runs out and
        # to be stopped rather than have the robot keep trying - so this
        # outranks anything about verification.
        out.extend(self.balances.warnings_es())

        if unverified and not self.gate.strict:
            out.append(
                "Todavia no puedo comprobar automaticamente "
                + " ni ".join(unverified)
                + ". Las fotos se generan igual, pero revisalas tu antes de usarlas."
            )
        return out


services = Services()


@asynccontextmanager
async def _lifespan(_: FastAPI):
    print(f"[estudio] catalogo: {len(services.catalog)} estilos")
    for warning in services.warnings():
        print(f"[estudio] AVISO: {warning}")

    # Sweep the bin daily, and once at startup. The startup pass matters on a
    # box where nothing keeps the app running across a reboot: without it, a
    # machine that is off for a fortnight would keep photos she deleted three
    # weeks ago until someone happened to leave it running past 04:00.
    scheduler = AsyncIOScheduler()
    scheduler.add_job(purge_expired, "cron", hour=4, minute=0, id="purge_bin")
    scheduler.start()
    try:
        purge_expired()
    except Exception as exc:  # noqa: BLE001 - a failed sweep must not block boot
        print(f"[estudio] AVISO: no se pudo limpiar la papelera: {exc}")

    yield

    scheduler.shutdown(wait=False)
    await services.registry.close()


app.router.lifespan_context = _lifespan


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def require_session(request: Request):
    session = services.auth.read(request.cookies.get(COOKIE_NAME))
    if session is None:
        raise HTTPException(status_code=401, detail="sin sesion")
    return session


def _guard(request: Request) -> RedirectResponse | None:
    if services.auth.read(request.cookies.get(COOKIE_NAME)) is None:
        return RedirectResponse("/entrar", status_code=303)
    return None


def _is_https(request: Request) -> bool:
    """Whether the browser actually reached us over TLS.

    Behind Caddy the app itself is spoken to over plain HTTP, so the scheme on
    the request is not the whole story - the forwarded header is.  Getting
    this wrong in either direction is bad: marking the cookie Secure on a
    plain-HTTP origin means the browser silently drops it and she can never
    log in, and omitting it on a real HTTPS deployment leaks the session over
    any downgraded request.
    """
    forwarded = request.headers.get("x-forwarded-proto", "")
    if forwarded:
        return forwarded.split(",")[0].strip().lower() == "https"
    return request.url.scheme == "https"


@app.get("/e/{token}")
async def magic_link(request: Request, token: str) -> Response:
    """The private link.  Opened once, never needed again on that phone."""
    if not services.auth.verify_token(token):
        return HTMLResponse(
            "<h1>Enlace no valido</h1><p>Pide un enlace nuevo.</p>", status_code=403
        )
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        COOKIE_NAME,
        services.auth.issue(settings.owner_name),
        max_age=settings.session_days * 24 * 3600,
        httponly=True,
        samesite="lax",
        secure=_is_https(request),
    )
    return response


@app.get("/entrar", response_class=HTMLResponse)
async def entrar(request: Request) -> Response:
    return templates.TemplateResponse(
        request, "entrar.html", {"app_name": settings.app_name}
    )


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def crear(request: Request) -> Response:
    if (redirect := _guard(request)) is not None:
        return redirect
    return templates.TemplateResponse(
        request,
        "crear.html",
        {
            "owner": settings.owner_name,
            "recent": services.store.gallery(limit=8, kind="final"),
            # Hers, not the diagnostic dump - see warnings_for_her.
            "warnings": services.warnings_for_her(),
            "tab": "crear",
        },
    )


@app.post("/upload")
async def upload(request: Request, photo: UploadFile = File(...)) -> Response:
    """One button.  Her camera roll opens, she picks, and that is the whole
    entry - no instruction about file types, because the web upload always
    sends the original."""
    require_session(request)

    data = await photo.read()
    if len(data) > settings.max_upload_mb * 1024 * 1024:
        return JSONResponse(
            {"error": f"La foto pesa mas de {settings.max_upload_mb} MB"}, status_code=413
        )

    try:
        stored = store_upload(
            data,
            original_name=photo.filename or "foto.jpg",
            directory=settings.uploads_dir,
            derivatives=settings.derivatives_dir,
        )
    except UnsupportedImage as exc:
        return JSONResponse({"error": exc.detail}, status_code=415)

    services.store.add_image(image_id=stored.id, kind="upload", path=str(stored.path))
    services.uploads[stored.id] = str(stored.path)

    # Same gesture, two meanings, resolved by the robot.
    decision = await asyncio.to_thread(services.photo_router.classify, stored.path)
    if decision.role is PhotoRole.REFERENCE:
        return JSONResponse(
            {
                "role": "reference",
                "image_id": stored.id,
                "url": stored.thumb_url,
                "targets": [{"key": k, "label": l} for k, l in REFERENCE_TARGETS],
            }
        )

    return JSONResponse({"role": "source", "image_id": stored.id, "next": f"/estilo/{stored.id}"})


@app.get("/estilo/{image_id}", response_class=HTMLResponse)
async def estilo(request: Request, image_id: str) -> Response:
    """Style options, chosen for THIS photo, plus the multi-select rows."""
    if (redirect := _guard(request)) is not None:
        return redirect

    path = services.uploads.get(image_id)
    if path is None:
        row = services.store.image(image_id)
        if row is None:
            raise HTTPException(status_code=404, detail="foto no encontrada")
        path = row.path

    ir = await _analyse(image_id, path)
    proposals = services.proposals.rank(services.catalog.all(), ir, limit=6)
    rows = default_chip_rows(proposals[0].look if proposals else None)

    return templates.TemplateResponse(
        request,
        "estilo.html",
        {
            "preview_count": _preview_count(),
            "image_id": image_id,
            "photo_url": f"/media/thumb/{Path(path).stem}.webp",
            "proposals": [p.public() for p in proposals],
            "rows": [
                {
                    "attribute": attribute.value,
                    "label": _ROW_LABELS.get(attribute, attribute.value),
                    "chips": chips,
                }
                for attribute, chips in rows.items()
            ],
            "framing": ir.capture.framing.value,
            "tab": "crear",
        },
    )


_ROW_LABELS: dict[Attribute, str] = {
    Attribute.GARMENT: "ROPA",
    Attribute.GARMENT_COLOR: "COLOR",
    Attribute.HAIR: "PELO",
    Attribute.GESTURE: "GESTO",
    Attribute.EXPRESSION: "EXPRESION",
    Attribute.SCENE: "ESCENARIO",
    Attribute.LIGHT: "LUZ",
    Attribute.FRAMING: "ENCUADRE",
}


@app.post("/previews")
async def start_previews(
    request: Request,
    image_id: str = Form(...),
    look_id: str = Form(""),
    selections: str = Form("{}"),
    count: int = Form(0),
) -> Response:
    """Stage A.  Returns immediately with a session id; the tiles arrive over
    SSE one by one as they land."""
    require_session(request)

    path = services.uploads.get(image_id) or (
        services.store.image(image_id).path if services.store.image(image_id) else None
    )
    if path is None:
        raise HTTPException(status_code=404, detail="foto no encontrada")

    parsed = _parse_selections(selections)
    look = services.catalog.get(look_id) if look_id else None
    if look is not None and not parsed.values:
        # A quick style is just a saved selection - one mechanism underneath,
        # with shortcuts on top.
        parsed = look.as_selections()

    ir = await _analyse(image_id, path)
    # Read once, at session start. A preference changed mid-generation must
    # not produce a batch made by two different models.
    state = services.orchestrator.open_session(
        source_path=path,
        ir=ir,
        look=look,
        selections=parsed,
        prefer_quality=_prefer_quality(),
    )
    services.store.open_session(
        session_id=state.id,
        source_id=image_id,
        look_id=look_id or None,
        selections={a.value: v for a, v in parsed.values.items()},
    )

    empty = services.balances.exhausted()
    if empty:
        # Stop, and say so. She asked for exactly this: "que el bot se detenga
        # y me avise inmediatamente, en lugar de continuar intentando generar
        # imagenes."
        return JSONResponse(
            {"detail": services.balances.warnings_es()[0]}, status_code=402
        )

    wanted = count or _preview_count()
    try:
        estimate = services.orchestrator.estimate(state, count=wanted)
    except ProviderError as exc:
        # A configuration fault, not a transient one. Returning 500 gave her
        # "No he podido empezar. Intentalo otra vez" - advice that could never
        # work, for a problem retrying cannot touch.
        print(f"[estudio] no se pudo estimar la sesion: {exc}")
        return JSONResponse(
            {"detail": f"No hay proveedor de imagen configurado: {exc}"},
            status_code=503,
        )

    async def run() -> None:
        try:
            candidates = await services.orchestrator.run_previews(state, count=wanted)
        except BudgetExceeded as exc:
            bus.publish(state.id, EventKind.ERROR, detail=exc.message_es())
            return
        for candidate in candidates:
            _record_image(
                image_id=candidate.id,
                kind="preview",
                path=candidate.image_path,
                session_id=state.id,
                look_id=look_id or None,
                slot=candidate.slot.describe(),
                score=candidate.score,
                report=candidate.report.model_dump(mode="json"),
            )
        _record_chip_exposure(state, candidates)

    asyncio.create_task(run())

    return JSONResponse(
        {
            "session_id": state.id,
            "expected": wanted,
            "estimate_usd": round(estimate.preview_stage_usd, 3),
            "message": estimate.message_es(),
        }
    )


@app.post("/finals")
async def start_finals(
    request: Request, session_id: str = Form(...), chosen: str = Form(...)
) -> Response:
    """Stage B.  Only her choices, at full quality, through the full gate."""
    require_session(request)
    try:
        state = services.orchestrator.session(session_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="sesion no encontrada") from None

    chosen_ids = [c for c in json.loads(chosen or "[]") if c]
    services.store.mark_kept(chosen_ids, kept=True)

    async def run() -> None:
        try:
            finals = await services.orchestrator.run_finals(state, chosen_ids)
        except BudgetExceeded as exc:
            bus.publish(state.id, EventKind.ERROR, detail=exc.message_es())
            return
        for final in finals:
            _record_image(
                image_id=final.id,
                kind="final",
                path=final.image_path,
                session_id=state.id,
                look_id=state.look.id if state.look else None,
                slot=final.report.summary_line(),
                score=final.report.score,
                report=final.report.model_dump(mode="json"),
            )
        for entry in services.ledger.entries:
            if entry.session_id == state.id:
                services.store.add_cost(
                    session_id=entry.session_id,
                    kind=entry.kind,
                    provider_id=entry.provider_id,
                    usd=entry.usd,
                    detail=entry.detail,
                    at=entry.at,
                )
        services.store.close_session(
            session_id=state.id,
            cost_usd=services.ledger.session_total(state.id),
            delivered=len(finals),
            elapsed_s=time.time() - state.started_at,
        )
        _learn_from_choices(state, chosen_ids)

    asyncio.create_task(run())
    return JSONResponse({"session_id": session_id, "count": len(chosen_ids)})


@app.get("/events/{session_id}")
async def events(request: Request, session_id: str) -> EventSourceResponse:
    """Progressive delivery.  Each tile is pushed the moment it lands, which
    is why the same wait feels roughly half as long as an album that arrives
    whole."""
    require_session(request)

    async def stream():
        async for event in bus.subscribe(session_id):
            if await request.is_disconnected():
                break
            yield event.to_sse()

    return EventSourceResponse(stream())


@app.get("/opciones/{session_id}", response_class=HTMLResponse)
async def opciones(request: Request, session_id: str) -> Response:
    if (redirect := _guard(request)) is not None:
        return redirect
    return templates.TemplateResponse(
        request,
        "opciones.html",
        {"session_id": session_id, "expected": _preview_count(), "tab": "crear"},
    )


@app.get("/resultado/{session_id}", response_class=HTMLResponse)
async def resultado(request: Request, session_id: str) -> Response:
    if (redirect := _guard(request)) is not None:
        return redirect
    try:
        state = services.orchestrator.session(session_id)
        finals = [f.public() for f in state.finals]
        cost = services.ledger.session_total(session_id)
        elapsed = time.time() - state.started_at
    except LookupError:
        finals, cost, elapsed = [], 0.0, 0.0
    return templates.TemplateResponse(
        request,
        "resultado.html",
        {
            "session_id": session_id,
            "finals": finals,
            "cost": round(cost, 3),
            "elapsed": int(elapsed),
            "tab": "crear",
        },
    )


@app.get("/galeria", response_class=HTMLResponse)
async def galeria(request: Request, kept: int = 0, tipo: str = "final") -> Response:
    if (redirect := _guard(request)) is not None:
        return redirect

    # `tipo=upload` lists the photographs SHE sent, not the ones the system
    # made. Without it there is no screen anywhere that shows her own
    # originals, and no way to delete them - which is precisely the thing she
    # asked for ("que las fotos originales puedan eliminarse despues").
    kind = "upload" if tipo == "upload" else "final"
    return templates.TemplateResponse(
        request,
        "galeria.html",
        {
            "images": services.store.gallery(limit=90, kind=kind, kept_only=bool(kept)),
            "kept_only": bool(kept),
            "showing_uploads": kind == "upload",
            "bin_count": services.store.bin_count(),
            "tab": "favoritos" if kept else "galeria",
        },
    )


# ---------------------------------------------------------------------------
# Deleting, and undeleting
#
# Nothing she deletes is destroyed on the spot. She is working on a phone with
# the delete control next to the image, and a mis-tap that permanently loses a
# photograph is not a mistake this system should be able to make. Deleted
# photos sit in the bin for a week and can be brought straight back.
# ---------------------------------------------------------------------------


@app.post("/borrar")
async def borrar(request: Request, ids: str = Form(...)) -> Response:
    require_session(request)
    try:
        image_ids = [i for i in json.loads(ids or "[]") if i]
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="seleccion no valida") from None

    moved = services.store.move_to_bin(image_ids)
    days = int(settings.trash_retention_days)
    return JSONResponse(
        {
            "moved": moved,
            "bin_count": services.store.bin_count(),
            "message": (
                f"{moved} foto{'s' if moved != 1 else ''} a la papelera. "
                f"Puedes recuperarla{'s' if moved != 1 else ''} durante {days} dias."
            ),
        }
    )


@app.post("/restaurar")
async def restaurar(request: Request, ids: str = Form(...)) -> Response:
    require_session(request)
    try:
        image_ids = [i for i in json.loads(ids or "[]") if i]
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="seleccion no valida") from None

    restored = services.store.restore(image_ids)
    return JSONResponse(
        {
            "restored": restored,
            "bin_count": services.store.bin_count(),
            "message": f"{restored} foto{'s' if restored != 1 else ''} recuperada"
            f"{'s' if restored != 1 else ''}",
        }
    )


@app.get("/papelera", response_class=HTMLResponse)
async def papelera(request: Request) -> Response:
    if (redirect := _guard(request)) is not None:
        return redirect
    rows = services.store.bin_contents(limit=200)
    days = settings.trash_retention_days
    return templates.TemplateResponse(
        request,
        "papelera.html",
        {
            "images": [
                {
                    "id": r.id,
                    "stem": r.stem,
                    "name": r.name,
                    "expiry": r.expiry_es(days),
                    "urgent": r.days_left(days) < 1,
                }
                for r in rows
            ],
            "retention_days": int(days),
            "tab": "galeria",
        },
    )


@app.post("/papelera/vaciar")
async def vaciar_papelera(request: Request, confirm: str = Form("")) -> Response:
    """Destroy everything in the bin now, without waiting out the week.

    Guarded by an explicit confirmation string rather than a boolean: this is
    the one irreversible action in the product, and it should not be reachable
    by a stray POST.
    """
    require_session(request)
    if confirm != "BORRAR":
        raise HTTPException(status_code=400, detail="confirmacion requerida")

    rows = services.store.bin_contents(limit=10_000)
    purged = _purge(rows)
    return JSONResponse(
        {
            "purged": purged,
            "message": f"{purged} foto{'s' if purged != 1 else ''} borrada"
            f"{'s' if purged != 1 else ''} definitivamente",
        }
    )


def _purge(rows) -> int:
    """Delete the FILES first, then the rows.

    That order matters: rows first would orphan the files with nothing left
    pointing at them, and they would sit on disk forever - which for
    photographs of a real person is the opposite of what "deleted" should mean.
    """
    if not rows:
        return 0
    for row in rows:
        destroy(row.path, settings.derivatives_dir)
    services.store.forget([r.id for r in rows])
    return len(rows)


def purge_expired() -> int:
    """The scheduled sweep. Anything past the retention window goes."""
    rows = services.store.expired(retention_days=settings.trash_retention_days)
    count = _purge(rows)
    if count:
        print(f"[estudio] papelera: {count} foto(s) borradas definitivamente")
    return count


@app.post("/ajustes")
async def guardar_ajustes(
    request: Request,
    preview_count: int = Form(None),
    final_quality: str = Form(None),
) -> JSONResponse:
    """Save a setting. The chips had no route behind them at all."""
    require_session(request)
    saved: dict[str, str] = {}

    if preview_count is not None:
        # Bounded rather than trusted: this multiplies directly into what a
        # session costs, and it arrives from a form.
        count = max(2, min(12, int(preview_count)))
        services.store.set_preference("preview_count", str(count))
        saved["preview_count"] = str(count)

    if final_quality is not None:
        choice = "best" if str(final_quality).lower() == "best" else "free"
        services.store.set_preference("final_quality", choice)
        saved["final_quality"] = choice

    return JSONResponse({"ok": True, "saved": saved})


@app.post("/ajustes/saldo")
async def registrar_saldo(
    request: Request, service: str = Form(...), amount: float = Form(...)
) -> JSONResponse:
    """Record a top-up she has made on the provider's own website.

    Neither service exposes a remaining-credit endpoint, so this is the only
    honest way to count down: she states what she added, and we subtract what
    we have spent - which is the same figure her cost line comes from.

    This records money she has ALREADY paid. It cannot take any.
    """
    require_session(request)
    if service not in services.balances.SERVICES:
        return JSONResponse({"detail": "servicio desconocido"}, status_code=400)
    if not (0 < amount <= 1000):
        return JSONResponse({"detail": "importe fuera de rango"}, status_code=400)
    services.balances.set_topped_up(service, amount)
    return JSONResponse(
        {"ok": True, "balance": services.balances.balance(service).remaining_usd}
    )


@app.get("/ajustes", response_class=HTMLResponse)
async def ajustes(request: Request) -> Response:
    if (redirect := _guard(request)) is not None:
        return redirect
    return templates.TemplateResponse(
        request,
        "ajustes.html",
        {
            "spend": services.store.spend_summary(),
            "balances": services.balances.all(),
            "caps": settings.caps,
            "preview_count": _preview_count(),
            "final_quality": services.store.preference("final_quality", "best"),
            "quality_available": any(
                p.descriptor.cost_per_call_usd > 0
                for p in services.registry.all()
                if Tier.FINAL in p.descriptor.tiers
            ),
            "warnings": services.warnings(),
            # Available, not the raw file count. They differ whenever the
            # coverage policy is holding looks back, and a settings page that
            # says 21 while she is offered 18 gets reported as a loading bug.
            "catalog_size": len(services.catalog.all()),
            "catalog_withheld": len(services.catalog.withheld())
            if settings.coverage_enforced
            else 0,
            "tab": "ajustes",
        },
    )


# ---------------------------------------------------------------------------
# Media
# ---------------------------------------------------------------------------


def _derivative(kind: str, name: str) -> Path:
    """The requested thumbnail, built from the original if it is missing.

    Belt and braces alongside _record_image. Sixty-one images already existed
    with no derivative when this was found, and an image that displays as a
    broken icon is indistinguishable, to her, from one that was lost - so it
    is worth healing rather than only preventing.

    Returns the path either way; _serve raises the 404 if there is genuinely
    nothing to serve.
    """
    safe = Path(name).name
    path = settings.derivatives_dir / kind / safe
    if path.exists():
        return path

    # The derivative is named after the source file, so the source is found by
    # that same stem - NOT by image id, which is a different string entirely
    # for anything the system generated rather than received.
    stem = Path(safe).stem
    for folder in (settings.images_dir, settings.uploads_dir):
        matches = sorted(Path(folder).glob(f"{stem}.*"))
        original = next((m for m in matches if m.suffix.lower() != ".webp"), None)
        if original is None:
            continue
        try:
            build_derivatives(original, settings.derivatives_dir)
        except Exception as exc:  # noqa: BLE001
            print(f"[estudio] AVISO: no pude reconstruir {safe}: {exc}")
        break
    return path


_ANALYSIS_CACHE: dict[str, object] = {}


async def _analyse(image_id: str, path: str):
    """Read a photograph once, record what it cost, and cache the answer.

    THREE PROBLEMS THIS CLOSES, all on the same call.

    It is a paid vision call fired from GET /estilo/{id}. It had no cap check,
    so it could spend past the limits; no ledger entry, so the spend was
    invisible to the totals SHE is shown; and no cache, so every refresh of
    the style screen was another charge for reading the same photograph.

    The client asked, in her own words, to know what she is spending and to
    have nothing charged she did not decide on. An uncapped, unrecorded,
    uncached call is the exact opposite of that.

    A photograph does not change, so the reading is cached by image id.
    """
    cached = _ANALYSIS_CACHE.get(image_id)
    if cached is not None:
        return cached

    from app.analysis.analyser import ANALYSIS_COST_USD

    if settings.has_language_model:
        try:
            services.ledger.check(
                session_id=f"analysis:{image_id}", additional_usd=ANALYSIS_COST_USD
            )
        except BudgetExceeded as exc:
            # Fall back to the free arithmetic reading rather than refusing the
            # screen. She still gets her styles; they are simply ranked without
            # a model's help.
            print(f"[estudio] analisis omitido por tope: {exc}")
            return await asyncio.to_thread(services.analyser._fallback.analyse, path)

    reading = await asyncio.to_thread(services.analyser.analyse, path)

    spent = float(getattr(reading, "cost_usd", 0.0) or 0.0)
    if spent:
        services.store.add_cost(
            session_id=f"analysis:{image_id}", kind="analysis",
            provider_id=settings.analyser_model, usd=spent,
            detail="lectura de la foto", at=time.time(),
        )
        services.ledger.record(
            session_id=f"analysis:{image_id}", kind="analysis",
            provider_id=settings.analyser_model, usd=spent,
        )

    _ANALYSIS_CACHE[image_id] = reading
    if len(_ANALYSIS_CACHE) > 200:
        _ANALYSIS_CACHE.pop(next(iter(_ANALYSIS_CACHE)))
    return reading


def _preview_count() -> int:
    """How many options to make, honouring what she actually chose.

    The saved preference was written by POST /ajustes and read back to paint
    the chips - and then generation used settings.preview_count from the
    environment instead. She selected 9, the screen showed 9 selected, and six
    photographs arrived.

    Resolved in ONE place because the same preference is needed by /previews
    and by the page that draws the waiting slots, and those two disagreeing is
    how a grid ends up with three placeholders that never fill.
    """
    try:
        return max(2, min(12, int(services.store.preference(
            "preview_count", str(settings.preview_count)
        ))))
    except (TypeError, ValueError):
        return settings.preview_count


def _prefer_quality() -> bool:
    """Whether finals go to the paid tier. Same reasoning: one reader."""
    return services.store.preference("final_quality", "best") == "best"


def _record_image(**fields) -> None:
    """Record a generated image AND build its thumbnails.

    These were two separate steps and only uploads ever did both, so every
    photograph the system produced was stored correctly and displayed as a
    broken icon: the gallery asks for /media/thumb/<id>.webp and nothing had
    ever written one.

    Kept together in one function precisely so they cannot drift apart again -
    the previous arrangement was not a missing call so much as an invitation
    to forget one.
    """
    services.store.add_image(**fields)
    path = fields.get("path")
    if not path:
        return
    try:
        # Named after the SOURCE file, which is what the templates request:
        # galeria.html asks for /media/thumb/{{ image.stem }}.webp, and
        # image.stem is the stem of the stored file. Naming these by image_id
        # instead produced the same mismatch in the opposite direction.
        build_derivatives(Path(path), settings.derivatives_dir)
    except Exception as exc:  # noqa: BLE001
        # A thumbnail is a convenience; the photograph is the deliverable.
        # Failing to shrink it must never lose it.
        print(f"[estudio] AVISO: sin miniatura para {fields.get('image_id')}: {exc}")


def _serve(path: Path) -> FileResponse:
    if not path.exists():
        raise HTTPException(status_code=404, detail="no encontrado")
    # Immutable URLs: content never changes for a given name, so it can be
    # cached hard.  This is what keeps the gallery usable on mobile data.
    return FileResponse(path, headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.get("/media/thumb/{name}")
async def media_thumb(request: Request, name: str) -> FileResponse:
    require_session(request)
    return _serve(_derivative("thumb", name))


@app.get("/media/medium/{name}")
async def media_medium(request: Request, name: str) -> FileResponse:
    require_session(request)
    return _serve(_derivative("medium", name))


def _is_binned(stem: str) -> bool:
    """Whether this file belongs to an image she has deleted.

    A binned photo must stop being servable immediately, not in a week. The
    file still exists so it can be restored, but a URL that keeps working
    after "delete" would make the whole feature dishonest - and these URLs get
    shared into chats and left in browser history.
    """
    for row in services.store.bin_contents(limit=10_000):
        if row.stem == stem:
            return True
    return False


@app.get("/media/{name}")
async def media(request: Request, name: str) -> FileResponse:
    """Every image route is behind auth.

    A public URL raises the stakes over a private chat, and these are
    photographs of a real person.  Filenames are unguessable and there is no
    directory listing.
    """
    require_session(request)
    safe = Path(name).name
    if _is_binned(Path(safe).stem):
        raise HTTPException(status_code=404, detail="no encontrado")
    for directory in (settings.images_dir, settings.uploads_dir):
        candidate = directory / safe
        if candidate.exists():
            return _serve(candidate)
    raise HTTPException(status_code=404, detail="no encontrado")


@app.get("/covers/{name}")
async def covers(request: Request, name: str) -> FileResponse:
    require_session(request)
    return _serve(settings.catalog_dir / "covers" / Path(name).name)


# ---------------------------------------------------------------------------
# PWA plumbing and health
# ---------------------------------------------------------------------------


@app.get("/manifest.json")
async def manifest() -> JSONResponse:
    return JSONResponse(
        {
            "name": f"{settings.app_name} - {settings.owner_name}",
            "short_name": settings.app_name,
            "start_url": "/",
            "display": "standalone",
            "background_color": "#111113",
            "theme_color": "#111113",
            "icons": [
                {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
                {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
            ],
        }
    )


@app.get("/sw.js")
async def service_worker() -> FileResponse:
    # Served from the root so its scope covers the whole app.
    return FileResponse(
        APP_DIR / "static" / "sw.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/health")
async def health() -> JSONResponse:
    """For the uptime monitor.

    Self-hosting introduced a failure mode a chat bot never had: the site can
    go down and nobody notices.
    """
    return JSONResponse(
        {
            "ok": True,
            "catalog": len(services.catalog),
            "identity_verification": services.profile.can_check_identity,
            "strict_gate": services.gate.strict,
            "warnings": services.warnings(),
        }
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_selections(raw: str) -> Selections:
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return Selections()
    values: dict[Attribute, list[str]] = {}
    for key, chosen in payload.items():
        try:
            attribute = Attribute(key)
        except ValueError:
            continue
        if isinstance(chosen, list) and chosen:
            values[attribute] = [str(c) for c in chosen]
    try:
        return Selections(values=values)
    except Exception:  # noqa: BLE001 - a bad row must not lose the whole request
        return Selections()


def _record_chip_exposure(state, candidates) -> None:
    if state.look is None:
        return
    for candidate in candidates:
        for attribute, value in candidate.slot.values.items():
            services.store.record_chip(
                look_id=state.look.id,
                attribute=attribute.value,
                value=value,
                shown=1,
                generated=1,
                passed_gate=1,
            )


def _learn_from_choices(state, chosen_ids: list[str]) -> None:
    """Her taps, turned into evidence.

    Both halves count: what she picked is a positive label and what she
    ignored is a negative one, so every session produces real preference data
    at no effort from her.
    """
    chosen = set(chosen_ids)
    kept_by_provider: dict[str, tuple[int, int]] = {}

    for candidate_id, candidate in state.candidates.items():
        was_kept = candidate_id in chosen
        shown, kept = kept_by_provider.get(candidate.provider_id, (0, 0))
        kept_by_provider[candidate.provider_id] = (shown + 1, kept + int(was_kept))

        if state.look is not None:
            for attribute, value in candidate.slot.values.items():
                services.store.record_chip(
                    look_id=state.look.id,
                    attribute=attribute.value,
                    value=value,
                    kept=int(was_kept),
                )

    look_id = state.look.id if state.look else "_global"
    for provider_id, (shown, kept) in kept_by_provider.items():
        services.router.bandit.observe(
            look_id=look_id, provider_id=provider_id, shown=shown, kept=kept
        )
    services.store.save_bandit(services.router.bandit.snapshot())
