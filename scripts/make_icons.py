"""Generate the PWA icons.

Small enough to keep as code rather than as binary blobs in the repo: the
icons are regenerable, reviewable in a diff, and cannot drift from the theme
colour in app.css.

Run:  python scripts/make_icons.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"

BACKGROUND = (17, 17, 19)
ACCENT = (217, 164, 65)


def make_icon(size: int) -> Image.Image:
    image = Image.new("RGB", (size, size), BACKGROUND)
    draw = ImageDraw.Draw(image)

    unit = size / 16

    # An aperture-ish mark: a ring with a gap, plus a solid centre. Reads as a
    # camera at 192px and is still legible at favicon size, which a detailed
    # glyph would not be.
    inset = unit * 3
    draw.ellipse(
        [inset, inset, size - inset, size - inset],
        outline=ACCENT,
        width=max(2, int(unit * 0.9)),
    )
    centre = unit * 6
    draw.ellipse(
        [centre, centre, size - centre, size - centre],
        fill=ACCENT,
    )
    return image


def main() -> None:
    STATIC.mkdir(parents=True, exist_ok=True)
    for size in (192, 512):
        path = STATIC / f"icon-{size}.png"
        make_icon(size).save(path, "PNG")
        print(f"wrote {path.relative_to(STATIC.parent.parent)}")


if __name__ == "__main__":
    main()
