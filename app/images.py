"""Image intake and delivery.

Two jobs the chat surface used to do for free, and which now have to be done
properly:

  INTAKE     accept whatever her phone sends - including HEIC from an iPhone -
             without silently losing quality.  The web upload is the reason
             the "send as Archivo, not as Foto" instruction disappeared, so it
             must never reintroduce compression by the back door.

  DELIVERY   full-resolution photographs are slow and expensive over mobile
             data.  Grids get small WebP thumbnails, the viewer gets a medium,
             and the original is sent only when she asks to download it.
             Without this the gallery is unusable on a phone, which is why it
             is built in from the start rather than optimised in later.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

# iPhone photos arrive as HEIC.  Registering the opener is a no-op when the
# package is absent, and the upload then fails with a clear message rather
# than a corrupt file.
try:  # pragma: no cover - depends on the install
    import pillow_heif  # type: ignore[import-not-found]

    pillow_heif.register_heif_opener()
    HEIC_SUPPORTED = True
except Exception:  # noqa: BLE001
    HEIC_SUPPORTED = False


THUMB_WIDTH = 400
MEDIUM_WIDTH = 1080
#: Above this the source is downscaled on the way in.  Set well above what any
#: model consumes, so this trims phone-camera excess without ever touching the
#: quality the profile and the gate depend on.
MAX_STORED_SIDE = 3000


class UnsupportedImage(ValueError):
    """The file is not an image we can read.  Says why, in her language."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True)
class StoredImage:
    id: str
    path: Path
    width: int
    height: int
    original_name: str

    @property
    def url(self) -> str:
        return f"/media/{self.path.name}"

    @property
    def thumb_url(self) -> str:
        return f"/media/thumb/{self.path.stem}.webp"

    @property
    def medium_url(self) -> str:
        return f"/media/medium/{self.path.stem}.webp"


def store_upload(
    data: bytes, *, original_name: str, directory: Path, derivatives: Path
) -> StoredImage:
    """Persist an upload and build its derivatives.

    EXIF is stripped on the way in - it carries GPS.  Orientation is applied
    first, so a photo taken sideways is stored upright rather than being shown
    rotated everywhere downstream.
    """
    directory.mkdir(parents=True, exist_ok=True)

    import io

    try:
        with Image.open(io.BytesIO(data)) as opened:
            image = ImageOps.exif_transpose(opened)
            image = image.convert("RGB")
    except Exception as exc:  # noqa: BLE001
        hint = ""
        if original_name.lower().endswith((".heic", ".heif")) and not HEIC_SUPPORTED:
            hint = " (falta pillow-heif para leer fotos de iPhone)"
        raise UnsupportedImage(f"no he podido leer esta imagen{hint}: {exc}") from exc

    if max(image.size) > MAX_STORED_SIDE:
        scale = MAX_STORED_SIDE / max(image.size)
        image = image.resize(
            (int(image.width * scale), int(image.height * scale)), Image.LANCZOS
        )

    digest = hashlib.sha256(data).hexdigest()[:16]
    image_id = f"{digest}_{uuid.uuid4().hex[:6]}"
    path = directory / f"{image_id}.png"
    # PNG, so nothing is lost between upload and the profile that is built
    # from it.  Storage is 200 GB and free; a re-compressed source is not
    # recoverable.
    image.save(path, "PNG")

    build_derivatives(path, derivatives)
    return StoredImage(
        id=image_id,
        path=path,
        width=image.width,
        height=image.height,
        original_name=original_name,
    )


def build_derivatives(
    path: Path, derivatives: Path, *, stem: str | None = None
) -> tuple[Path, Path]:
    """Small and medium WebP, written once and cached forever.

    URLs are immutable, so these can be served with a long cache header and
    never revalidated.

    ``stem`` names the output. It defaults to the SOURCE filename's stem,
    which is correct only because an upload is stored under its own image id.
    A generated photograph is not: it keeps the provider's filename, so the
    gallery asked for /media/thumb/<image_id>.webp while the derivative had
    been written as <provider-filename>.webp. Same picture, two names, and a
    broken icon for every photograph the system produced.
    """
    thumb_dir = derivatives / "thumb"
    medium_dir = derivatives / "medium"
    thumb_dir.mkdir(parents=True, exist_ok=True)
    medium_dir.mkdir(parents=True, exist_ok=True)

    name = stem or path.stem
    thumb_path = thumb_dir / f"{name}.webp"
    medium_path = medium_dir / f"{name}.webp"

    if thumb_path.exists() and medium_path.exists():
        return thumb_path, medium_path

    with Image.open(path) as image:
        image = image.convert("RGB")

        thumb = image.copy()
        thumb.thumbnail((THUMB_WIDTH, THUMB_WIDTH * 4), Image.LANCZOS)
        thumb.save(thumb_path, "WEBP", quality=80, method=4)

        medium = image.copy()
        medium.thumbnail((MEDIUM_WIDTH, MEDIUM_WIDTH * 4), Image.LANCZOS)
        medium.save(medium_path, "WEBP", quality=88, method=4)

    return thumb_path, medium_path


def destroy(path: str | Path, derivatives: Path) -> list[str]:
    """Delete an image and every derivative of it, permanently.

    Called only by the purge, once the retention window has passed.

    The derivatives matter as much as the original. A thumbnail is a small
    picture of her, not a cache artefact - leaving `thumb/<stem>.webp` behind
    after "permanently deleted" means the photograph is still on the disk, and
    still servable to anyone who knows the URL.

    Never raises. A purge that aborts halfway leaves half-deleted images with
    rows already gone, which is harder to reason about than a file that failed
    to unlink and gets retried tomorrow.
    """
    path = Path(path)
    removed: list[str] = []

    candidates = [
        path,
        derivatives / "thumb" / f"{path.stem}.webp",
        derivatives / "medium" / f"{path.stem}.webp",
    ]
    for candidate in candidates:
        try:
            if candidate.exists():
                candidate.unlink()
                removed.append(str(candidate))
        except OSError:
            # Locked by a reader, or already gone. Either way the row stays,
            # and the next purge picks it up.
            continue
    return removed


def comparison_strip(source: Path, result: Path, output_dir: Path) -> Path:
    """Source and result side by side, as one image.

    For the "comparar con la original" view, and for the POC report where her
    manual attempts sit next to the robot's.  Composed server-side so it is a
    single file she can save or send to someone.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    with Image.open(source) as a, Image.open(result) as b:
        a, b = a.convert("RGB"), b.convert("RGB")
        height = min(a.height, b.height, 1400)
        a = a.resize((int(a.width * height / a.height), height), Image.LANCZOS)
        b = b.resize((int(b.width * height / b.height), height), Image.LANCZOS)

        gap = 16
        canvas = Image.new("RGB", (a.width + gap + b.width, height), (18, 18, 20))
        canvas.paste(a, (0, 0))
        canvas.paste(b, (a.width + gap, 0))

    path = output_dir / f"compare_{uuid.uuid4().hex[:10]}.webp"
    canvas.save(path, "WEBP", quality=90, method=4)
    return path
