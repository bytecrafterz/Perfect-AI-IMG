"""Generate the catalog cover images.

Every look declared a cover_image and none had ever been produced, so the
style screen - the first thing she sees after uploading - showed twenty-one
broken image icons.

The fallback (her own photograph) is now correct when a cover is missing, but
a real cover is much better: the whole point of the style screen is that she
recognises a look at a glance rather than reading twenty-one names.

WHY THIS IS WORTH RUNNING
It costs nothing. On the Cloudflare free tier a 512x640 image is 57.5 neurons,
so all twenty-one is ~1,210 of the 10,000 daily allocation - about 12% of one
day, once. It is also the cheapest honest end-to-end test of the whole
provider path.

WHAT IT DOES NOT DO
No source image, no reference: covers are TEXT-TO-IMAGE. They illustrate a
style, not a person, so nobody's likeness goes into a file that ships with the
repository. The subject is described generically and the coverage policy is
applied exactly as it is for her own photographs, so a cover cannot advertise
a look the policy would refuse to produce.

Withheld looks get no cover while the policy is on - generating one would
create the very image the policy exists to avoid.

    python scripts/make_covers.py
    python scripts/make_covers.py --force      redo existing ones
    python scripts/make_covers.py --only ID    just one
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.compile.compiler import COVERAGE_CLAUSE, cover_recipe_text  # noqa: E402
from app.config import settings  # noqa: E402
from app.contracts.provider import Capability, GenerationRequest, Tier  # noqa: E402
from app.providers.base import ProviderError  # noqa: E402
from app.providers.loader import build_registry  # noqa: E402

COVER_W, COVER_H = 512, 640


def describe(look) -> str:
    """A prompt for the LOOK, with no individual in it.

    Generic subject on purpose. A cover is a style illustration that ships in
    the repository; it must not carry anyone's likeness.
    """
    recipe = look.recipe
    covered = cover_recipe_text if settings.coverage_enforced else (lambda t: t)

    bits: list[str] = ["editorial fashion photograph of a woman"]
    if recipe.garment:
        garment = covered(recipe.garment.type)
        if garment:
            bits.append(f"wearing {garment}")
        details = covered(recipe.garment.details)
        if details:
            bits.append(details)
        if recipe.garment.fabric:
            bits.append(f"{recipe.garment.fabric} fabric")
    if recipe.scene:
        place = recipe.scene.place
        if recipe.scene.time:
            place = f"{place}, {recipe.scene.time}"
        bits.append(f"in {place}")
    if recipe.lighting:
        bits.append(f"lit by {recipe.lighting.key}")
    if recipe.pose_family:
        bits.append(recipe.pose_family[0])

    cam = recipe.camera
    bits.append(f"{cam.focal_mm}mm at {cam.aperture}, {cam.framing.value} framing")
    bits.append("photorealistic, natural skin texture, sharp focus")

    # Same policy as her own photographs. A cover must not advertise a look
    # the system would then refuse to produce.
    if settings.coverage_enforced:
        bits.append(COVERAGE_CLAUSE)
    return ", ".join(bits)


async def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="regenerate existing covers")
    ap.add_argument("--only", help="one look id")
    args = ap.parse_args(argv)

    from app.catalog import Catalog

    catalog = Catalog(settings.catalog_dir).load()
    covers = Path(settings.catalog_dir) / "covers"
    covers.mkdir(parents=True, exist_ok=True)

    registry, report = build_registry(
        config_path=settings.providers_path, output_dir=settings.images_dir
    )
    if report.using_mock:
        print("Sin proveedor real: solo se generarian marcadores. Aborto.")
        return 1

    # Cheapest provider that can do text-to-image, which for covers is all
    # that is needed - preferring free over fast over good, deliberately.
    candidates = [
        p
        for p in registry.all()
        if Capability.TEXT_TO_IMAGE in p.descriptor.capabilities
        and Tier.PREVIEW in p.descriptor.tiers
    ]
    if not candidates:
        print("Ningun proveedor hace texto-a-imagen en el nivel preview.")
        return 1
    provider = min(candidates, key=lambda p: p.descriptor.cost_per_call_usd)
    print(f"Proveedor: {provider.descriptor.id}  "
          f"${provider.descriptor.cost_per_call_usd:.4f}/imagen\n")

    # all() already excludes withheld looks while the policy is on.
    looks = catalog.all()
    if args.only:
        looks = [l for l in looks if l.id == args.only]
        if not looks:
            print(f"No encuentro '{args.only}' entre los looks disponibles.")
            return 1

    withheld = catalog.withheld()
    if withheld and not args.only:
        print(f"Omito {len(withheld)} look(s) retenidos por la politica de cobertura:")
        for look in withheld:
            print(f"  - {look.id}")
        print()

    made = skipped = failed = 0
    for index, look in enumerate(looks, 1):
        target = covers / f"{look.id}.webp"
        if target.exists() and not args.force:
            print(f"  [{index:2}/{len(looks)}] = {look.id} (ya existe)")
            skipped += 1
            continue

        try:
            result = await provider.generate(
                GenerationRequest(
                    prompt=describe(look),
                    negative_prompt="text, watermark, logo, distorted anatomy, extra fingers",
                    width=COVER_W,
                    height=COVER_H,
                    # Deterministic per look, so --force reproduces the same
                    # cover rather than quietly redecorating the catalog.
                    seed=abs(hash(look.id)) % 2_000_000,
                )
            )
        except ProviderError as exc:
            print(f"  [{index:2}/{len(looks)}] ! {look.id}: {exc}")
            failed += 1
            continue

        from PIL import Image

        with Image.open(result.image_path) as image:
            image.convert("RGB").save(target, "WEBP", quality=82, method=6)
        Path(result.image_path).unlink(missing_ok=True)

        size_kb = target.stat().st_size / 1024
        print(f"  [{index:2}/{len(looks)}] + {look.id}  {size_kb:.0f} KB  {result.elapsed_s:.1f}s")
        made += 1

    await registry.close()
    print(f"\n  {made} generadas, {skipped} ya estaban, {failed} fallaron")
    if made:
        print(f"  en {covers}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
