"""Make one real call to each configured provider and report exactly what
came back.

This exists because of an honest limitation. The adapters were written and
tested against a mock transport, which proves the request building, the
response parsing, the retry policy and the failure handling - but it cannot
prove that a live service accepts these bodies. Model input schemas differ
between models and change between versions.

So rather than discovering a mismatch when a real batch dies after paying for
every image, run this first. It spends one generation per provider - a few
cents - and prints the request, the response and the failure verbatim.

Run:  python scripts/check_provider.py
      python scripts/check_provider.py --tier preview
      python scripts/check_provider.py --id fal.flux-schnell
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.contracts.provider import Capability, GenerationRequest, Tier  # noqa: E402
from app.providers.base import ProviderError  # noqa: E402
from app.providers.loader import build_registry  # noqa: E402

PROMPT = (
    "a photograph of a woman standing by a window, natural light, "
    "photorealistic, sharp focus"
)


async def check_one(provider) -> bool:
    descriptor = provider.descriptor
    print(f"\n{'-' * 66}")
    print(f"{descriptor.id}")
    print(f"  {descriptor.notes}")
    print(f"  tiers={[t.value for t in descriptor.tiers]} "
          f"coste=${descriptor.cost_per_call_usd:.4f}/llamada "
          f"dialecto={descriptor.prompt_dialect.value}")

    request = GenerationRequest(
        prompt=PROMPT,
        negative_prompt="extra fingers, distorted anatomy, blurry",
        width=512,
        height=640,
        seed=12345,
        steps=4,
    )

    try:
        result = await provider.generate(request)
    except ProviderError as exc:
        print(f"  FALLO: {exc}")
        print(f"  reintentable: {exc.retryable}")
        print("\n  Que mirar:")
        print("   * si es 401/403 -> la clave no es valida para este servicio")
        print("   * si es 404     -> el 'model' de providers.json no existe")
        print("   * si es 422/400 -> el modelo espera otros campos de entrada;")
        print("     ajusta 'params' en providers.json o el _build_body del adaptador")
        return False
    except Exception as exc:  # noqa: BLE001
        print(f"  FALLO INESPERADO: {type(exc).__name__}: {exc}")
        return False

    size = Path(result.image_path).stat().st_size
    print(f"  OK  {result.elapsed_s:.1f}s  seed={result.seed}  {size / 1024:.0f} KB")
    print(f"  -> {result.image_path}")

    # The reproduction record is what carries a chosen preview into its final.
    # If it comes back empty, preview fidelity is broken and she would receive
    # a different photograph from the one she picked.
    missing = [k for k in ("prompt", "seed", "provider_id") if not result.reproduction.get(k)]
    if missing:
        print(f"  AVISO: al registro de reproduccion le falta {missing}")
        print("         sin eso, la foto final no puede derivarse de la elegida")

    return await _check_image_to_image(provider, descriptor, result.image_path)


async def _check_image_to_image(provider, descriptor, seed_image: str) -> bool:
    """The call that actually runs in production, and the one a text-only
    probe cannot see.

    Every look in the catalog routes in_place_edit, so with a source photo the
    router demands IMAGE_TO_IMAGE and every real generation takes this path.
    Checking only text-to-image proves the key and the account and nothing
    about the work.

    It matters because on fal these are separate endpoints, not one endpoint
    with a mode flag. Posting an image to the text-to-image path returns 200
    and ignores the image, so the failure is not an error - it is a convincing
    photograph of somebody who is not her.

    Reuses the image the previous call just produced, so this costs one
    generation and needs nothing prepared.
    """
    if Capability.IMAGE_TO_IMAGE not in descriptor.capabilities:
        print("  i2i: no declarado, no se prueba")
        return True

    print("  i2i: probando la ruta imagen-a-imagen (la que se usa de verdad)...")
    request = GenerationRequest(
        prompt="the same woman, wearing a dark wool coat, same face and body",
        negative_prompt="different person, distorted anatomy",
        width=512,
        height=640,
        seed=12345,
        steps=4,
        source_image_path=seed_image,
        strength=0.55,
    )
    try:
        result = await provider.generate(request)
    except ProviderError as exc:
        print(f"  i2i FALLO: {exc}")
        print("")
        print("  Que mirar:")
        print("   * 404 -> el endpoint i2i no existe con ese nombre.")
        print("     Corrige 'i2i_model' en providers.json. En fal el camino")
        print("     imagen-a-imagen es distinto del de texto-a-imagen.")
        print("   * 422 -> el endpoint existe pero espera otros campos")
        print("   * Si este proveedor no hace i2i, quita 'i2i' de sus")
        print("     capabilities para que el router deje de elegirlo.")
        return False
    except Exception as exc:  # noqa: BLE001
        print(f"  i2i FALLO INESPERADO: {type(exc).__name__}: {exc}")
        return False

    print(f"  i2i OK  {result.elapsed_s:.1f}s  -> {result.image_path}")
    print("  COMPRUEBALO A OJO: la salida debe parecerse a la entrada.")
    print("  Si es una persona distinta, el endpoint ha ignorado la imagen")
    print("  y esta generando desde cero - que es el fallo que esto busca.")
    return True


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tier", choices=["preview", "final"], help="solo este nivel")
    parser.add_argument("--id", help="solo este proveedor")
    parser.add_argument(
        "--yes", action="store_true", help="no preguntar antes de gastar"
    )
    args = parser.parse_args(argv)

    settings.ensure_dirs()
    registry, report = build_registry(
        config_path=settings.providers_path,
        output_dir=settings.images_dir,
    )

    if report.using_mock:
        print("No hay ningun proveedor real configurado.")
        print(f"Pon una clave en .env y revisa {settings.providers_path}.")
        for provider_id, reason in report.skipped:
            print(f"  {provider_id}: {reason}")
        return 1

    providers = registry.all()
    if args.id:
        providers = [p for p in providers if p.descriptor.id == args.id]
    if args.tier:
        tier = Tier(args.tier)
        providers = [p for p in providers if p.descriptor.serves(tier)]

    if not providers:
        print("Ningun proveedor coincide con ese filtro.")
        return 1

    estimated = sum(p.descriptor.cost_per_call_usd for p in providers)
    print(f"Voy a hacer {len(providers)} generacion(es) reales.")
    print(f"Coste estimado: ${estimated:.4f}")
    if report.skipped:
        print("\nOmitidos:")
        for provider_id, reason in report.skipped:
            print(f"  {provider_id}: {reason}")

    if not args.yes:
        # Spending her money is not something a script should do because
        # somebody ran it out of curiosity.
        answer = input("\nContinuar? [s/N] ").strip().lower()
        if answer not in {"s", "si", "sí", "y", "yes"}:
            print("Cancelado. No se ha gastado nada.")
            return 0

    results = [await check_one(p) for p in providers]
    await registry.close()

    ok = sum(results)
    print(f"\n{'=' * 66}")
    print(f"{ok} de {len(results)} proveedores funcionan.")
    if ok < len(results):
        print("Arregla los que fallan antes de generar un lote de verdad:")
        print("un fallo repetido cuesta una imagen cada vez que se intenta.")
        return 1
    print("Listo para producir.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
