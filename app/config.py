"""Settings, read from the environment once at import.

Deliberately plain: no pydantic-settings dependency, because every dependency
is one more wheel that has to build on the aarch64 target.

Nothing here has a secret as a default.  A missing key produces a clearly
degraded mode that says so, never a silent fallback that looks like it works.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = APP_ROOT.parent


def load_dotenv(path: Path | None = None) -> int:
    """Read .env into os.environ, if it is there.

    Under docker-compose this is unnecessary - `env_file` does it. Running
    directly (which is how the Windows deployment works, and how anyone runs
    it locally) nothing does, so the keys sit in the file being silently
    ignored and every call falls back to a degraded mode. The symptom is a
    system that looks configured and behaves as though it is not.

    Two details that are not fussiness:

      * A REAL environment variable always wins. A service wrapper or a shell
        export must be able to override the file, or there is no way to run a
        one-off with different settings.
      * The BOM is stripped. PowerShell's Set-Content -Encoding utf8 and
        Windows Notepad both write one, and it would otherwise become part of
        the first key's NAME - so the first line of the file, silently, does
        nothing at all.
    """
    path = path or (PROJECT_ROOT / ".env")
    if not path.exists():
        return 0

    loaded = 0
    try:
        text = path.read_text(encoding="utf-8-sig")  # -sig: tolerate the BOM
    except OSError:
        return 0

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded


#: Must run before Settings is constructed at the bottom of this module.
DOTENV_LOADED = load_dotenv()


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name) or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name) or default)
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Thresholds:
    """The quality gate's cutoffs.

    THESE ARE PLACEHOLDERS UNTIL STAGE 5 CALIBRATION.  They are deliberately
    conservative: over-rejecting costs money, under-rejecting costs trust, and
    trust is the thing that cannot be bought back.  Calibration replaces every
    number here with one fitted to her own photos and her own judgement.
    """

    #: ArcFace cosine against the profile centroid.
    identity_accept: float = 0.62
    identity_repair: float = 0.50

    #: Largest permitted relative drift in any body measurement.
    #:
    #: 6% was chosen perceptually - "roughly the point at which a change
    #: becomes visible side by side" - and never checked against the
    #: measurement's own repeatability. Measured against her 24 photographs,
    #: her OWN drift from her OWN baseline ranges 3.7% to 13.3%, median 7.0%.
    #: At 6% the check rejected 6 of her 10 measurable photographs; at 15% it
    #: rejects none.
    #:
    #: A threshold below the noise floor of the thing it measures does not
    #: make a check strict, it makes it wrong - and here it would have blamed
    #: the generator for the angle she stood at.
    #:
    #: 15% is therefore a COARSE BACKSTOP against gross reshaping, not a
    #: precision instrument. Say so when reporting it. Tightening it needs
    #: framing normalisation that does not exist yet.
    proportion_drift: float = 0.15

    #: CIE76 deltaE on cheek and arm patches.
    skin_delta_e: float = 4.0

    #: Eyes. Geometry only - the pose model gives eye CENTRES, so this catches
    #: an eye placed independently of the face, not an iris that looks wrong.
    #:
    #: eye_tilt_deg      how far the eye line may disagree with the ear line.
    #:                   Heads tilt; eyes tilt WITH them. 12 degrees is well
    #:                   past any real head pose and well inside the detector's
    #:                   own noise on a clear face.
    #: eye_spacing_ratio inter-ocular distance over ear width. Human faces
    #:                   cluster tightly; this band is deliberately generous
    #:                   because head rotation compresses the ear line.
    #: eye_confidence    below this the detector has stopped seeing an eye,
    #:                   which is itself the signal - the same reasoning as the
    #:                   wrist check.
    eye_tilt_deg: float = 12.0
    eye_spacing_ratio_min: float = 0.30
    eye_spacing_ratio_max: float = 0.85
    eye_confidence: float = 0.50

    #: SSIM outside the region the request was allowed to touch.  1.0 is
    #: identical; anything below this means the generator changed something
    #: it was not asked to.
    containment_ssim: float = 0.92

    #: Minimum mean hand-keypoint confidence before a hand is called suspect.
    hand_confidence: float = 0.55

    #: Below this the image is called technically poor regardless of identity.
    min_sharpness: float = 0.25


@dataclass(frozen=True)
class Caps:
    """Spending limits, enforced in the orchestrator.

    Mandatory, not optional: the system spends money without her watching, so
    it must refuse to run rather than overspend.
    """

    per_session_usd: float = 1.50
    per_day_usd: float = 10.00
    balance_floor_usd: float = 0.00


def _malformed_key_warnings() -> list[str]:
    """Catch an API key that was pasted with a trailing comment.

    load_dotenv strips whitespace and quotes but not a `#`, so

        FAL_API_KEY=abc123      # or REPLICATE_API_TOKEN

    yields the key "abc123      # or REPLICATE_API_TOKEN". It is not empty, so
    every "is the key set?" check passes, the provider registers as configured,
    and the failure arrives as a 401 on the first paid call - which reads as a
    rejected key rather than a malformed one, and sends you to the billing page
    instead of the file.

    Cheap to detect, so detect it. A real key from either service is one token
    with no spaces.
    """
    # What each key looks like, so a value in the WRONG SLOT is caught here
    # rather than as an HTTP 401 an hour later.
    #
    # This is not hypothetical. The Cloudflare account id and API token were
    # pasted into ANTHROPIC_API_KEY and FAL_API_KEY. Every "is it configured?"
    # check passed, both providers registered as ready, and every call to
    # either returned 401 - which reads as a billing problem, not a
    # copy-and-paste one. The gate reported the judge unavailable; the finals
    # silently produced nothing.
    shapes: dict[str, tuple[str, str]] = {
        # name: (test, what it should look like)
        "ANTHROPIC_API_KEY": ("prefix:sk-ant-", "empieza por 'sk-ant-'"),
        "FAL_API_KEY": ("contains::", "lleva dos puntos, con el formato id:secreto"),
        "CF_API_TOKEN": ("minlen:30", "es una cadena larga del panel de Cloudflare"),
        "CF_ACCOUNT_ID": ("hex:32", "son 32 caracteres hexadecimales"),
    }

    def wrong_shape(name: str, value: str) -> str | None:
        rule = shapes.get(name)
        if not rule:
            return None
        test, described = rule
        kind, _, arg = test.partition(":")
        if kind == "prefix" and not value.startswith(arg):
            return described
        if kind == "contains" and arg not in value:
            return described
        if kind == "minlen" and len(value) < int(arg):
            return described
        if kind == "hex" and (
            len(value) != int(arg) or any(c not in "0123456789abcdefABCDEF" for c in value)
        ):
            return described
        return None

    out: list[str] = []
    seen: dict[str, str] = {}
    for name in (
        "ANTHROPIC_API_KEY", "FAL_API_KEY", "REPLICATE_API_TOKEN",
        "CF_API_TOKEN", "CF_ACCOUNT_ID",
    ):
        value = os.environ.get(name, "")
        if not value:
            continue

        expected = wrong_shape(name, value)
        if expected:
            out.append(f"{name} no tiene el formato correcto: {expected}")

        # Two different services never share a credential. If they match, one
        # of them was pasted into the wrong line.
        if value in seen:
            out.append(
                f"{name} y {seen[value]} tienen el MISMO valor: uno de los dos "
                "esta en la linea equivocada del .env"
            )
        seen[value] = name
        if "#" in value:
            out.append(
                f"{name} contiene '#': parece que se pego un comentario en la "
                "misma linea. Los comentarios van en su propia linea del .env"
            )
        elif value != value.strip() or " " in value or "	" in value:
            out.append(
                f"{name} contiene espacios: probablemente sobra texto al final "
                "de la linea en el .env"
            )
    return out


@dataclass(frozen=True)
class Settings:
    # -- identity ----------------------------------------------------------
    app_name: str = "Estudio"
    owner_name: str = field(default_factory=lambda: _env("OWNER_NAME", "Nayane"))

    # -- security ----------------------------------------------------------
    secret_key: str = field(default_factory=lambda: _env("SECRET_KEY"))
    access_token: str = field(default_factory=lambda: _env("ACCESS_TOKEN"))
    session_days: int = field(default_factory=lambda: _env_int("SESSION_DAYS", 365))

    # -- storage -----------------------------------------------------------
    data_dir: Path = field(
        default_factory=lambda: Path(_env("DATA_DIR", str(PROJECT_ROOT / "data")))
    )
    catalog_dir: Path = field(
        default_factory=lambda: Path(_env("CATALOG_DIR", str(PROJECT_ROOT / "catalog")))
    )
    #: Which providers exist, what they cost, what they can do.  Config, so a
    #: price change or a provider swap never touches code.
    providers_path: Path = field(
        default_factory=lambda: Path(
            _env("PROVIDERS_CONFIG", str(PROJECT_ROOT / "providers.json"))
        )
    )

    # -- language models ---------------------------------------------------
    anthropic_api_key: str = field(
        default_factory=lambda: _env("ANTHROPIC_API_KEY")
    )
    analyser_model: str = field(
        default_factory=lambda: _env("ANALYSER_MODEL", "claude-opus-5")
    )
    judge_model: str = field(
        default_factory=lambda: _env("JUDGE_MODEL", "claude-haiku-4-5")
    )

    # -- image providers ---------------------------------------------------
    #: Empty means the mock provider is used, which generates locally and
    #: costs nothing.  That is the default so the app runs end to end with no
    #: keys and no spend.
    fal_api_key: str = field(default_factory=lambda: _env("FAL_API_KEY"))
    replicate_api_token: str = field(
        default_factory=lambda: _env("REPLICATE_API_TOKEN")
    )

    # -- batch shape -------------------------------------------------------
    preview_count: int = field(default_factory=lambda: _env_int("PREVIEW_COUNT", 6))
    preview_width: int = field(default_factory=lambda: _env_int("PREVIEW_WIDTH", 512))
    preview_height: int = field(default_factory=lambda: _env_int("PREVIEW_HEIGHT", 640))
    final_width: int = field(default_factory=lambda: _env_int("FINAL_WIDTH", 1024))
    final_height: int = field(default_factory=lambda: _env_int("FINAL_HEIGHT", 1280))

    # -- concurrency -------------------------------------------------------
    #: Generation is I/O bound (waiting on a provider) so it fans out wide.
    generation_concurrency: int = field(
        default_factory=lambda: _env_int("GENERATION_CONCURRENCY", 6)
    )
    #: CV is CPU bound on a 4-core box, so it is capped separately to stop it
    #: starving the event loop and blowing the latency budget.
    cv_concurrency: int = field(default_factory=lambda: _env_int("CV_CONCURRENCY", 2))

    # -- limits ------------------------------------------------------------
    max_upload_mb: int = field(default_factory=lambda: _env_int("MAX_UPLOAD_MB", 30))
    max_repair_attempts: int = field(
        default_factory=lambda: _env_int("MAX_REPAIR_ATTEMPTS", 1)
    )

    #: How long a deleted photo stays recoverable before it is destroyed.
    #:
    #: Seven days is chosen for a person, not for a disk: long enough to cover
    #: "I deleted that on Friday and want it back on Monday", short enough
    #: that deleting something actually means something. Disk is not the
    #: constraint - the box has 150 GB free.
    trash_retention_days: float = field(
        default_factory=lambda: _env_float("TRASH_RETENTION_DAYS", 7.0)
    )
    max_refill_rounds: int = field(
        default_factory=lambda: _env_int("MAX_REFILL_ROUNDS", 2)
    )

    # -- coverage policy ---------------------------------------------------
    #: How much of the body generated images must keep covered.
    #:
    #:   "full"  closed neckline, covered shoulders/arms/midriff/back, legs to
    #:           the ankle, nothing sheer. Stated on every call AND verified by
    #:           the judge.
    #:   "off"   no coverage constraint; looks render exactly as authored.
    #:
    #: A SETTING rather than a hard rule on purpose. It is on now because the
    #: POC is being tested under a restricted brief, and the intention is that
    #: the subject eventually gets the full range she asked for. Making it
    #: switchable means turning it off is one line in .env rather than
    #: unpicking it from the compiler, the judge and twenty catalog files.
    #:
    #: Note this is separate from, and does not substitute for, the consent
    #: procedure already agreed for intimate work. Turning this off widens the
    #: styling range; it does not settle that question.
    #: Whether an unmeasurable check blocks the image.
    #:
    #:   auto  block only when every check we hold a reference for can
    #:         actually be measured (the safe default - see resolve_strict)
    #:   on    always block on unknowns.  Correct for production once the CV
    #:         stack is complete; discards everything before that.
    #:   off   never block on unknowns.  Records them and shows a banner.
    strict_gate: str = field(
        default_factory=lambda: (_env("STRICT_GATE", "auto") or "auto").lower()
    )

    coverage_policy: str = field(
        default_factory=lambda: (_env("COVERAGE_POLICY", "full") or "full").lower()
    )

    thresholds: Thresholds = field(default_factory=Thresholds)
    caps: Caps = field(default_factory=Caps)

    @property
    def coverage_enforced(self) -> bool:
        return self.coverage_policy not in {"off", "none", "0", "false"}

    debug: bool = field(default_factory=lambda: _env_bool("DEBUG", False))

    # -- derived paths -----------------------------------------------------

    @property
    def db_path(self) -> Path:
        return self.data_dir / "estudio.sqlite3"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def images_dir(self) -> Path:
        return self.data_dir / "images"

    @property
    def derivatives_dir(self) -> Path:
        return self.data_dir / "derivatives"

    @property
    def profile_dir(self) -> Path:
        return self.data_dir / "profile"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    def ensure_dirs(self) -> None:
        for path in (
            self.data_dir,
            self.uploads_dir,
            self.images_dir,
            self.derivatives_dir,
            self.profile_dir,
            self.models_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    # -- capability reporting ---------------------------------------------

    @property
    def has_language_model(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def has_real_provider(self) -> bool:
        return bool(self.fal_api_key or self.replicate_api_token)

    def degraded_modes(self) -> list[str]:
        """What is missing, in plain words.

        Shown on the settings screen and logged at startup.  The point is that
        a half-configured system announces itself instead of quietly producing
        worse results.
        """
        missing: list[str] = []
        missing.extend(_malformed_key_warnings())
        if not self.has_language_model:
            missing.append(
                "ANTHROPIC_API_KEY no configurada: el analisis de foto y el "
                "juez visual usan reglas basicas en vez de un modelo"
            )
        # Providers are reported by the loader, not here: it knows what
        # actually registered, which is the honest answer.  A key being set is
        # not the same as a provider working.
        if not self.secret_key:
            missing.append(
                "SECRET_KEY no configurada: se genera una efimera y las "
                "sesiones se pierden al reiniciar"
            )
        return missing


settings = Settings()
