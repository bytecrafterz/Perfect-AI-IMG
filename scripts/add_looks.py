"""Adds eleven looks to the catalog, taking it from 9 to 20.

WHY A SCRIPT AND NOT ELEVEN HAND-WRITTEN FILES
Every look must satisfy LookRecipe, and a stray comma in one JSON file makes
that look vanish silently at load - the catalog logs a skip and carries on,
which is right for production and terrible for authoring. Building them
through the model means a bad look fails here, loudly, instead of going
missing on her screen.

THE THREE HELD BACK
intimo_lenceria_editorial, intimo_bano_luz_suave and playa_bikini_verano are
authored in full and marked requires_coverage_off. While COVERAGE_POLICY is
enforced they are not shown at all.

That is deliberate: rendering a lingerie look through a coverage clause does
not produce a modest version of it, it produces a contradiction - a prompt
asking for a covered neck and a lingerie garment in the same breath. The
generator resolves that badly, the gate fails it, and a paid generation is
spent proving nothing. Hidden is honest; toned down is a bug.

Run once:  python scripts/add_looks.py
Idempotent: existing files are left alone unless --force.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.contracts.look_recipe import LookRecipe  # noqa: E402

CATALOG = ROOT / "catalog"


def look(
    id: str,
    name: str,
    category: str,
    *,
    garment: dict,
    scene: dict,
    lighting: dict,
    camera: dict,
    poses: list[str],
    applies: dict,
    chips: dict,
    axes: list[str],
    extra_locks: list[str] | None = None,
    gated: bool = False,
    note: str | None = None,
) -> dict:
    return {
        "version": "1.0",
        "id": id,
        "name": name,
        "category": category,
        "cover_image": f"covers/{id}.webp",
        "recipe": {
            "garment": garment,
            "scene": scene,
            "lighting": lighting,
            "camera": camera,
            "pose_family": poses,
        },
        "applies_to": applies,
        "variation_axes": axes,
        # hair is locked everywhere by default: it is close enough to identity
        # that changing it without being asked reads as the wrong person.
        "locks": ["face", "body_proportions", "skin_tone"] + (extra_locks or ["hair"]),
        "selectable": list(chips.keys()),
        "chips": chips,
        "chip_stats": {},
        "route_hint": "in_place_edit",
        "learned_params": {},
        "stats": {
            "first_try_rate": None,
            "avg_cost_usd": None,
            "keep_rate": None,
            "sessions": 0,
            "last_shown_at": None,
        },
        "enabled": True,
        "requires_coverage_off": gated,
        **({"_note": note} if note else {}),
    }


EXPR = ["neutra", "sonrisa suave", "seria", "mirada baja"]
EXPR_WARM = ["sonrisa abierta", "sonrisa suave", "neutra", "riendo"]

LOOKS: list[dict] = [
    # ---------------------------------------------------------------- retrato
    look(
        "retrato_primer_plano_belleza",
        "Primer plano belleza",
        "retrato",
        garment={
            "type": "top liso de cuello alto",
            "fabric": "punto fino",
            "colors": ["negro", "crema", "gris topo"],
            "details": "sin estampado, para que nada compita con la cara",
        },
        scene={
            "place": "fondo liso de estudio, gris medio",
            "time": None,
            "depth": "fondo completamente limpio, sin textura",
        },
        lighting={
            "key": "beauty dish frontal ligeramente elevado",
            "fill": "reflector plateado bajo la barbilla",
            "mood": "limpio, sombras suaves bajo los pomulos",
        },
        camera={
            "focal_mm": 105,
            "aperture": "f/4.0",
            "height": "ojos",
            "framing": "primer plano",
        },
        poses=["frontal a camara", "leve giro de tres cuartos", "barbilla adelantada"],
        applies={
            "framing": ["primer plano", "medio"],
            "needs_body": False,
            "replaces_background": True,
            "min_source_quality": 0.55,
        },
        chips={
            "garment_color": ["negro", "crema", "gris topo", "blanco"],
            "expression": EXPR,
            "light": ["suave frontal", "lateral marcada", "clave alta"],
            "framing": ["como esta", "primer plano"],
        },
        axes=["expression", "garment_color", "camera_angle"],
    ),
    look(
        "retrato_gimnasio_estudio",
        "Deportivo en estudio",
        "retrato",
        garment={
            "type": "conjunto deportivo de manga larga",
            "fabric": "tejido tecnico mate",
            "colors": ["negro", "gris jaspeado", "azul marino"],
            "details": "camiseta de manga larga y leggings largos, corte limpio",
        },
        scene={
            "place": "estudio industrial con suelo de cemento pulido",
            "time": None,
            "depth": "fondo oscuro degradado, algo de humo en el aire",
        },
        lighting={
            "key": "luz dura lateral desde la derecha",
            "fill": "rebote negro a la izquierda para hundir la sombra",
            "mood": "contrastado, definicion marcada",
        },
        camera={
            "focal_mm": 50,
            "aperture": "f/2.8",
            "height": "pecho",
            "framing": "medio",
        },
        poses=["de pie con brazos cruzados", "manos en las caderas", "estiramiento de hombro"],
        applies={
            "framing": ["medio", "cuerpo entero"],
            "needs_body": True,
            "replaces_background": True,
            "min_source_quality": 0.45,
        },
        chips={
            "garment_color": ["negro", "gris jaspeado", "azul marino", "burdeos"],
            "gesture": ["brazos cruzados", "manos en caderas", "de pie", "estirando"],
            "expression": ["seria", "neutra", "concentrada"],
            "framing": ["como esta", "medio", "cuerpo entero"],
        },
        axes=["gesture", "expression", "garment_color", "camera_angle"],
    ),
    # ------------------------------------------------------------------ moda
    look(
        "moda_denim_industrial",
        "Denim industrial",
        "moda",
        garment={
            "type": "camisa vaquera y pantalon vaquero",
            "fabric": "denim rigido",
            "colors": ["azul medio", "azul oscuro", "negro lavado"],
            "details": "camisa abotonada hasta arriba, manga remangada al codo",
        },
        scene={
            "place": "nave industrial con ventanales altos y estructura metalica",
            "time": "media tarde",
            "depth": "profundidad larga por el pasillo de la nave, muy desenfocada",
        },
        lighting={
            "key": "luz de ventanal difusa que entra lateral",
            "fill": "rebote del suelo claro",
            "mood": "neutro, algo frio",
        },
        camera={
            "focal_mm": 35,
            "aperture": "f/2.8",
            "height": "pecho",
            "framing": "cuerpo entero",
        },
        poses=["de pie de frente", "apoyada en una columna", "caminando de lado"],
        applies={
            "framing": ["medio", "cuerpo entero"],
            "needs_body": True,
            "replaces_background": True,
            "min_source_quality": 0.45,
        },
        chips={
            "garment": ["camisa vaquera", "cazadora vaquera", "mono vaquero"],
            "garment_color": ["azul medio", "azul oscuro", "negro lavado", "blanco roto"],
            "gesture": ["de pie", "apoyada", "caminando", "manos en bolsillos"],
            "expression": EXPR,
            "framing": ["como esta", "medio", "cuerpo entero"],
        },
        axes=["gesture", "expression", "garment_color", "camera_angle"],
    ),
    look(
        "moda_gala_noche",
        "Gala de noche",
        "moda",
        garment={
            "type": "vestido largo de gala",
            "fabric": "terciopelo con caida pesada",
            "colors": ["negro", "azul noche", "esmeralda"],
            "details": "manga larga, cuello alto, falda hasta el suelo",
        },
        scene={
            "place": "escalinata de marmol de un hotel clasico",
            "time": "noche",
            "depth": "lampara de arana desenfocada al fondo, puntos de luz calidos",
        },
        lighting={
            "key": "luz calida cenital de la lampara",
            "fill": "panel suave frontal a media potencia",
            "mood": "elegante, calido, sombras profundas",
        },
        camera={
            "focal_mm": 85,
            "aperture": "f/1.8",
            "height": "pecho",
            "framing": "cuerpo entero",
        },
        poses=["de pie en la escalinata", "mano en la barandilla", "girando hacia camara"],
        applies={
            "framing": ["medio", "cuerpo entero"],
            "needs_body": True,
            "replaces_background": True,
            "min_source_quality": 0.5,
        },
        chips={
            "garment": ["vestido largo de gala", "vestido de terciopelo", "traje de esmoquin"],
            "garment_color": ["negro", "azul noche", "esmeralda", "burdeos"],
            "gesture": ["de pie", "mano en barandilla", "girando", "mirando atras"],
            "expression": EXPR,
            "framing": ["como esta", "medio", "cuerpo entero"],
        },
        axes=["gesture", "expression", "garment_color", "camera_angle"],
    ),
    look(
        "moda_abrigo_invierno",
        "Abrigo de invierno",
        "moda",
        garment={
            "type": "abrigo largo de lana",
            "fabric": "lana gruesa",
            "colors": ["camel", "gris marengo", "negro"],
            "details": "cinturon anudado, cuello subido, bufanda de punto",
        },
        scene={
            "place": "calle adoquinada del centro historico con arboles sin hojas",
            "time": "manana de invierno, cielo cubierto",
            "depth": "fachadas antiguas desenfocadas, algo de niebla al fondo",
        },
        lighting={
            "key": "luz de dia difusa por el cielo cubierto",
            "fill": "rebote del adoquin humedo",
            "mood": "frio, plano, muy suave",
        },
        camera={
            "focal_mm": 50,
            "aperture": "f/2.0",
            "height": "pecho",
            "framing": "cuerpo entero",
        },
        poses=["caminando hacia camara", "ajustandose la bufanda", "de pie de tres cuartos"],
        applies={
            "framing": ["medio", "cuerpo entero"],
            "needs_body": True,
            "replaces_background": True,
            "min_source_quality": 0.45,
        },
        chips={
            "garment": ["abrigo largo de lana", "trenca", "abrigo acolchado"],
            "garment_color": ["camel", "gris marengo", "negro", "verde oliva"],
            "gesture": ["caminando", "ajustando bufanda", "de pie", "manos en bolsillos"],
            "expression": EXPR_WARM,
            "framing": ["como esta", "medio", "cuerpo entero"],
        },
        axes=["gesture", "expression", "garment_color", "camera_angle"],
    ),
    # ------------------------------------------------------------- escenarios
    look(
        "escenario_biblioteca_calida",
        "Biblioteca calida",
        "escenarios",
        garment={
            "type": "jersey de punto y pantalon de vestir",
            "fabric": "punto grueso",
            "colors": ["crema", "camel", "verde botella"],
            "details": "cuello alto, manga larga",
        },
        scene={
            "place": "biblioteca antigua con estanterias de madera hasta el techo",
            "time": "media tarde",
            "depth": "hileras de libros desenfocadas, escalera de madera al fondo",
        },
        lighting={
            "key": "ventanal alto a la izquierda",
            "fill": "lampara de mesa calida fuera de encuadre",
            "mood": "calido, intimo, contraste medio",
        },
        camera={
            "focal_mm": 50,
            "aperture": "f/1.8",
            "height": "pecho",
            "framing": "medio",
        },
        poses=["sentada leyendo", "de pie junto a la estanteria", "apoyada en la mesa"],
        applies={
            "framing": ["primer plano", "medio", "cuerpo entero"],
            "needs_body": False,
            "replaces_background": True,
            "min_source_quality": 0.4,
        },
        chips={
            "garment": ["jersey de punto", "camisa y chaleco", "blazer"],
            "garment_color": ["crema", "camel", "verde botella", "gris"],
            "gesture": ["sentada leyendo", "de pie", "apoyada", "sosteniendo un libro"],
            "expression": EXPR,
            "light": ["ventana lateral", "lampara calida", "contraluz suave"],
            "framing": ["como esta", "primer plano", "medio"],
        },
        axes=["gesture", "expression", "garment_color", "light"],
    ),
    look(
        "escenario_jardin_primavera",
        "Jardin en primavera",
        "escenarios",
        garment={
            "type": "vestido midi de manga larga",
            "fabric": "algodon ligero",
            "colors": ["blanco roto", "azul cielo", "verde salvia"],
            "details": "cuello cerrado, falda hasta media pierna",
        },
        scene={
            "place": "jardin con setos recortados y un arco de glicinias en flor",
            "time": "media manana",
            "depth": "vegetacion muy desenfocada, puntos de luz entre las hojas",
        },
        lighting={
            "key": "sol filtrado entre las hojas",
            "fill": "rebote verde suave de la vegetacion",
            "mood": "luminoso, fresco, alto en luz",
        },
        camera={
            "focal_mm": 85,
            "aperture": "f/1.8",
            "height": "pecho",
            "framing": "medio",
        },
        poses=["de pie bajo el arco", "caminando por el sendero", "oliendo una flor"],
        applies={
            "framing": ["medio", "cuerpo entero"],
            "needs_body": True,
            "replaces_background": True,
            "min_source_quality": 0.45,
        },
        chips={
            "garment": ["vestido midi", "blusa y falda", "mono largo"],
            "garment_color": ["blanco roto", "azul cielo", "verde salvia", "amarillo palido"],
            "gesture": ["de pie", "caminando", "girando", "mano en el pelo"],
            "expression": EXPR_WARM,
            "framing": ["como esta", "medio", "cuerpo entero"],
        },
        axes=["gesture", "expression", "garment_color", "camera_angle"],
    ),
    look(
        "escenario_cocina_luminosa",
        "Cocina luminosa",
        "escenarios",
        garment={
            "type": "camisa blanca y vaqueros",
            "fabric": "popelin de algodon",
            "colors": ["blanco", "azul claro", "rayas finas"],
            "details": "manga remangada, camisa por dentro",
        },
        scene={
            "place": "cocina moderna de encimera clara con ventana grande al lado",
            "time": "manana",
            "depth": "estanteria con vajilla desenfocada al fondo",
        },
        lighting={
            "key": "ventana grande lateral, luz de dia",
            "fill": "rebote de la encimera blanca",
            "mood": "limpio, luminoso, muy natural",
        },
        camera={
            "focal_mm": 35,
            "aperture": "f/2.2",
            "height": "pecho",
            "framing": "medio",
        },
        poses=["apoyada en la encimera", "sirviendo cafe", "de pie de tres cuartos"],
        applies={
            "framing": ["primer plano", "medio"],
            "needs_body": False,
            "replaces_background": True,
            "min_source_quality": 0.4,
        },
        chips={
            "garment": ["camisa blanca", "jersey fino", "blusa de lino"],
            "garment_color": ["blanco", "azul claro", "beige", "gris perla"],
            "gesture": ["apoyada en la encimera", "sirviendo cafe", "de pie", "sonriendo a camara"],
            "expression": EXPR_WARM,
            "framing": ["como esta", "primer plano", "medio"],
        },
        axes=["gesture", "expression", "garment_color", "camera_angle"],
    ),
    # --------------------------------------------------------------- producto
    look(
        "producto_belleza_cosmetica",
        "Producto de belleza",
        "producto",
        garment={
            "type": "top liso de cuello alto",
            "fabric": "punto fino mate",
            "colors": ["crema", "blanco", "negro"],
            "details": "sin adornos, para que el producto lleve la atencion",
        },
        scene={
            "place": "fondo de estudio color crema con una superficie de marmol",
            "time": None,
            "depth": "fondo liso, muy poca profundidad",
        },
        lighting={
            "key": "ventana difusa grande a 45 grados",
            "fill": "reflector blanco frontal",
            "mood": "limpio, publicitario, sombra unica y suave",
        },
        camera={
            "focal_mm": 85,
            "aperture": "f/4.0",
            "height": "pecho",
            "framing": "primer plano",
        },
        poses=[
            "sosteniendo el producto junto a la cara",
            "manos presentando el producto",
            "aplicandose el producto",
        ],
        applies={
            "framing": ["primer plano", "medio"],
            "needs_body": False,
            "replaces_background": True,
            "min_source_quality": 0.55,
        },
        chips={
            "garment_color": ["crema", "blanco", "negro", "gris topo"],
            "gesture": [
                "producto junto a la cara",
                "manos presentando",
                "aplicando el producto",
                "producto en la palma",
            ],
            "expression": ["sonrisa suave", "neutra", "mirada al producto"],
            "framing": ["como esta", "primer plano"],
        },
        axes=["gesture", "expression", "garment_color"],
    ),
    # ------------------------------------------------------------------------
    # Held back by the coverage policy. Authored in full, hidden until the
    # policy is relaxed - see the module docstring for why hidden and not
    # toned down.
    # ------------------------------------------------------------------------
    look(
        "playa_bikini_verano",
        "Bikini en la playa",
        "verano",
        garment={
            "type": "bikini de playa",
            "fabric": "lycra mate",
            "colors": ["negro", "blanco", "coral"],
            "details": "conjunto clasico de dos piezas, corte de catalogo",
        },
        scene={
            "place": "playa de arena clara con el mar al fondo",
            "time": "media tarde, sol alto pero ya cayendo",
            "depth": "rompiente desenfocada, horizonte limpio",
        },
        lighting={
            "key": "sol directo desde atras a la derecha",
            "fill": "rebote fuerte de la arena clara",
            "mood": "veraniego, alto contraste, muy luminoso",
        },
        camera={
            "focal_mm": 50,
            "aperture": "f/2.8",
            "height": "pecho",
            "framing": "cuerpo entero",
        },
        poses=["de pie en la orilla", "caminando por la arena", "sentada en la arena"],
        applies={
            "framing": ["cuerpo entero"],
            "needs_body": True,
            "replaces_background": True,
            "min_source_quality": 0.5,
        },
        chips={
            "garment_color": ["negro", "blanco", "coral", "azul marino"],
            "gesture": ["de pie", "caminando", "sentada", "mirando al mar"],
            "expression": EXPR_WARM,
            "framing": ["cuerpo entero"],
        },
        axes=["gesture", "expression", "garment_color", "camera_angle"],
        gated=True,
        note=(
            "Retenido mientras COVERAGE_POLICY este activa. No se muestra en el "
            "catalogo hasta que la politica se relaje en el traspaso."
        ),
    ),
    look(
        "intimo_lenceria_editorial",
        "Lenceria editorial",
        "intimo",
        garment={
            "type": "conjunto de lenceria de encaje",
            "fabric": "encaje y seda",
            "colors": ["negro", "marfil", "champan"],
            "details": "conjunto de catalogo, con bata de seda abierta encima",
        },
        scene={
            "place": "dormitorio de hotel con ropa de cama blanca y cortina de lino",
            "time": "primera hora de la manana",
            "depth": "cortina desenfocada al fondo, luz entrando por detras",
        },
        lighting={
            "key": "luz de ventana filtrada por la cortina de lino",
            "fill": "rebote blanco de la cama",
            "mood": "suave, envolvente, sin sombras duras",
        },
        camera={
            "focal_mm": 85,
            "aperture": "f/2.0",
            "height": "pecho",
            "framing": "medio",
        },
        poses=["sentada en el borde de la cama", "de pie junto a la ventana", "recostada de lado"],
        applies={
            "framing": ["medio", "cuerpo entero"],
            "needs_body": True,
            "replaces_background": True,
            "min_source_quality": 0.55,
        },
        chips={
            "garment": ["conjunto de encaje", "bata de seda", "camison largo"],
            "garment_color": ["negro", "marfil", "champan", "burdeos"],
            "gesture": ["sentada en la cama", "de pie junto a la ventana", "recostada", "mirando a camara"],
            "expression": EXPR,
            "framing": ["como esta", "medio"],
        },
        axes=["gesture", "expression", "garment_color", "light"],
        gated=True,
        note=(
            "Retenido mientras COVERAGE_POLICY este activa. No se muestra en el "
            "catalogo hasta que la politica se relaje en el traspaso."
        ),
    ),
    look(
        "intimo_bano_luz_suave",
        "Bano con luz suave",
        "intimo",
        garment={
            "type": "bata de seda",
            "fabric": "seda",
            "colors": ["marfil", "negro", "gris perla"],
            "details": "bata anudada, hombros descubiertos",
        },
        scene={
            "place": "bano de marmol claro con banera exenta y velas encendidas",
            "time": "atardecer",
            "depth": "vapor suave en el aire, espejo empanado al fondo",
        },
        lighting={
            "key": "luz calida y baja de las velas",
            "fill": "ventana pequena con luz fria de atardecer",
            "mood": "intimo, calido, contraste bajo",
        },
        camera={
            "focal_mm": 50,
            "aperture": "f/1.8",
            "height": "pecho",
            "framing": "medio",
        },
        poses=["sentada en el borde de la banera", "de pie junto al espejo", "apoyada en el marmol"],
        applies={
            "framing": ["primer plano", "medio"],
            "needs_body": False,
            "replaces_background": True,
            "min_source_quality": 0.55,
        },
        chips={
            "garment": ["bata de seda", "toalla anudada", "camison"],
            "garment_color": ["marfil", "negro", "gris perla"],
            "gesture": ["sentada en la banera", "junto al espejo", "apoyada", "mirando abajo"],
            "expression": EXPR,
            "light": ["velas", "ventana fria", "mixta"],
            "framing": ["como esta", "primer plano", "medio"],
        },
        axes=["gesture", "expression", "light", "camera_angle"],
        gated=True,
        note=(
            "Retenido mientras COVERAGE_POLICY este activa. No se muestra en el "
            "catalogo hasta que la politica se relaje en el traspaso."
        ),
    ),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="overwrite existing files")
    args = ap.parse_args()

    CATALOG.mkdir(parents=True, exist_ok=True)
    written = skipped = 0
    failed: list[str] = []

    for payload in LOOKS:
        path = CATALOG / f"{payload['id']}.json"
        if path.exists() and not args.force:
            print(f"  = {payload['id']} (exists)")
            skipped += 1
            continue
        try:
            # Validate through the model. A look that cannot load is a look
            # she never sees, and the catalog would swallow the error at
            # runtime rather than here.
            LookRecipe.model_validate(payload)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! {payload['id']}: {exc}")
            failed.append(payload["id"])
            continue
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        flag = "  [retenido por COVERAGE_POLICY]" if payload.get("requires_coverage_off") else ""
        print(f"  + {payload['id']}{flag}")
        written += 1

    print(f"\n  written {written}, skipped {skipped}, failed {len(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
