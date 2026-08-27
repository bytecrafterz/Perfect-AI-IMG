"""Seed the look catalog.

IMPORTANT, AND NOT A DETAIL: these are STARTER looks. The spec budgets a full
day for 15-20 authored looks and calls it the deliverable she is actually
buying - weak recipes make a weak product no matter how good the pipeline is.
What is here is enough to run and demonstrate the system end to end, and it is
the shape a good recipe takes, but it is not the finished catalog.

The output is plain JSON in catalog/. After this runs, the files are the
source of truth: edit them by hand, add more, delete ones that stop earning
their place. Re-running this script does NOT overwrite an existing file, so
hand edits are safe.

Run:  python scripts/seed_catalog.py
"""

from __future__ import annotations

import json
from pathlib import Path

CATALOG = Path(__file__).resolve().parent.parent / "catalog"

BASE_LOCKS = ["face", "body_proportions", "skin_tone", "hair"]


def look(
    *,
    id: str,
    name: str,
    category: str,
    garment: dict,
    scene: dict,
    lighting: dict,
    camera: dict,
    poses: list[str],
    applies: dict,
    chips: dict,
    axes: list[str],
    selectable: list[str],
    route: str = "in_place_edit",
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
        "locks": BASE_LOCKS,
        "selectable": selectable,
        "chips": chips,
        "chip_stats": {},
        "route_hint": route,
        "learned_params": {},
        "stats": {
            "first_try_rate": None,
            "avg_cost_usd": None,
            "keep_rate": None,
            "sessions": 0,
            "last_shown_at": None,
        },
        "enabled": True,
    }


COMMON_EXPRESSION = ["neutra", "sonrisa suave", "sonrisa amplia", "seria", "mirada baja"]
COMMON_HAIR = ["el mio", "suelto", "recogido", "coleta", "ondas"]

LOOKS: list[dict] = [
    look(
        id="moda_editorial_estudio",
        name="Editorial de estudio",
        category="moda",
        garment={
            "type": "conjunto sastre",
            "fabric": "lana fria con caida estructurada",
            "colors": ["negro", "hueso", "camel"],
            "details": "hombro marcado, pantalon recto",
        },
        scene={
            "place": "estudio de fondo infinito gris medio",
            "time": None,
            "depth": "fondo limpio sin textura, separacion por luz",
        },
        lighting={
            "key": "ventana de luz grande a 45 grados",
            "fill": "panel blanco al lado opuesto",
            "mood": "editorial, sombra controlada",
        },
        camera={"focal_mm": 105, "aperture": "f/4.0", "height": "pecho", "framing": "medio"},
        poses=["de pie de frente", "tres cuartos", "manos en los bolsillos", "brazos cruzados"],
        applies={
            "framing": ["medio", "cuerpo entero"],
            "needs_body": True,
            "replaces_background": True,
            "min_source_quality": 0.5,
        },
        axes=["gesture", "expression", "garment_color"],
        selectable=["garment", "garment_color", "gesture", "expression", "framing"],
        chips={
            "garment": ["conjunto sastre", "blazer y vaquero", "vestido midi", "camisa blanca"],
            "garment_color": ["negro", "hueso", "camel", "azul marino"],
            "gesture": ["de pie de frente", "tres cuartos", "manos en los bolsillos",
                        "brazos cruzados", "manos en la cintura"],
            "expression": COMMON_EXPRESSION,
            "framing": ["como esta", "medio", "cuerpo entero"],
        },
    ),
    look(
        id="moda_calle_urbana",
        name="Calle urbana",
        category="moda",
        garment={
            "type": "abrigo largo",
            "fabric": "pano de lana",
            "colors": ["camel", "gris marengo", "negro"],
            "details": "cinturon anudado, cuello subido",
        },
        scene={
            "place": "calle de ciudad con fachadas de piedra y escaparates",
            "time": "media manana, dia nublado",
            "depth": "peatones desenfocados al fondo",
        },
        lighting={
            "key": "luz difusa de dia nublado",
            "fill": "rebote del pavimento humedo",
            "mood": "natural, sin sombras duras",
        },
        camera={"focal_mm": 50, "aperture": "f/2.8", "height": "pecho", "framing": "cuerpo entero"},
        poses=["caminando", "parada mirando al lado", "cruzando la calle", "apoyada en una pared"],
        applies={
            "framing": ["cuerpo entero", "medio"],
            "needs_body": True,
            "replaces_background": True,
            "min_source_quality": 0.5,
        },
        axes=["gesture", "expression", "garment_color", "light"],
        selectable=["garment", "garment_color", "gesture", "expression", "light"],
        chips={
            "garment": ["abrigo largo", "cazadora de cuero", "gabardina", "punto oversize"],
            "garment_color": ["camel", "negro", "gris marengo", "verde oliva"],
            "gesture": ["caminando", "parada", "cruzando la calle", "apoyada en la pared",
                        "mirando el escaparate"],
            "expression": COMMON_EXPRESSION,
            "light": ["como esta", "nublado", "dorada", "dramatica"],
        },
    ),
    look(
        id="retrato_corporativo",
        name="Retrato corporativo",
        category="retrato",
        garment={
            "type": "camisa lisa",
            "fabric": "algodon mate",
            "colors": ["blanco", "azul claro", "negro"],
            "details": "cuello abierto, sin estampado",
        },
        scene={
            "place": "fondo neutro gris claro degradado",
            "time": None,
            "depth": "fondo liso, sujeto separado del fondo",
        },
        lighting={
            "key": "softbox frontal ligeramente elevado",
            "fill": "reflector bajo para suavizar el menton",
            "mood": "limpio, profesional, sombra minima",
        },
        camera={"focal_mm": 85, "aperture": "f/4.0", "height": "ojos", "framing": "primer plano"},
        poses=["de frente", "tres cuartos suave", "hombros girados"],
        applies={
            "framing": ["primer plano", "medio"],
            "needs_body": False,
            "replaces_background": True,
            "min_source_quality": 0.55,
        },
        axes=["expression", "garment_color", "hair"],
        selectable=["garment", "garment_color", "expression", "hair"],
        chips={
            "garment": ["camisa lisa", "blazer", "jersey fino", "blusa"],
            "garment_color": ["blanco", "azul claro", "negro", "gris"],
            "expression": ["neutra", "sonrisa suave", "seria", "sonrisa amplia"],
            "hair": COMMON_HAIR,
        },
    ),
    look(
        id="retrato_luz_natural",
        name="Retrato con luz de ventana",
        category="retrato",
        garment={
            "type": "prenda sencilla",
            "fabric": "punto suave",
            "colors": ["crema", "gris", "verde salvia"],
            "details": "sin estampados que compitan con la cara",
        },
        scene={
            "place": "interior junto a una ventana grande",
            "time": "media tarde",
            "depth": "habitacion en penumbra al fondo",
        },
        lighting={
            "key": "luz de ventana lateral, suave y direccional",
            "fill": "caida natural a sombra, sin relleno artificial",
            "mood": "intimo, contraste medio",
        },
        camera={"focal_mm": 85, "aperture": "f/1.8", "height": "ojos", "framing": "primer plano"},
        poses=["mirando a camara", "mirando por la ventana", "cabeza ligeramente inclinada"],
        applies={
            "framing": ["primer plano", "medio"],
            "needs_body": False,
            "replaces_background": False,
            "min_source_quality": 0.45,
        },
        axes=["expression", "light", "hair"],
        selectable=["expression", "light", "hair", "framing"],
        chips={
            "expression": COMMON_EXPRESSION,
            "light": ["como esta", "ventana lateral", "contraluz suave", "dorada"],
            "hair": COMMON_HAIR,
            "framing": ["como esta", "primer plano", "medio"],
        },
    ),
    look(
        id="retrato_blanco_negro",
        name="Blanco y negro",
        category="retrato",
        garment={
            "type": "prenda lisa oscura",
            "fabric": "algodon mate",
            "colors": ["negro", "gris marengo"],
            "details": "sin brillos ni logos",
        },
        scene={
            "place": "fondo oscuro liso",
            "time": None,
            "depth": "fondo que cae a negro",
        },
        lighting={
            "key": "luz lateral dura con caida rapida",
            "fill": "sin relleno, sombra profunda",
            "mood": "dramatico, alto contraste, grano fino de pelicula",
        },
        camera={"focal_mm": 85, "aperture": "f/2.8", "height": "ojos", "framing": "primer plano"},
        poses=["de frente", "perfil", "tres cuartos", "mirada baja"],
        applies={
            "framing": ["primer plano", "medio"],
            "needs_body": False,
            "replaces_background": True,
            "min_source_quality": 0.5,
        },
        axes=["expression", "gesture"],
        selectable=["expression", "gesture", "framing"],
        chips={
            "expression": ["neutra", "seria", "mirada baja", "sonrisa suave"],
            "gesture": ["de frente", "perfil", "tres cuartos", "mano en la cara"],
            "framing": ["como esta", "primer plano", "medio"],
        },
    ),
    look(
        id="escenario_playa_dorada",
        name="Playa al atardecer",
        category="escenarios",
        garment={
            "type": "vestido ligero",
            "fabric": "lino o gasa que se mueve con el viento",
            "colors": ["blanco", "crema", "azul claro"],
            "details": "vuelo amplio",
        },
        scene={
            "place": "playa de arena clara con orilla visible",
            "time": "ultima hora antes del atardecer",
            "depth": "mar y horizonte desenfocados",
        },
        lighting={
            "key": "sol bajo a contraluz",
            "fill": "rebote de la arena en la cara",
            "mood": "calido, luz dorada, algo de destello",
        },
        camera={"focal_mm": 70, "aperture": "f/2.2", "height": "pecho", "framing": "cuerpo entero"},
        poses=["caminando por la orilla", "de pie mirando al mar", "girando", "sentada en la arena"],
        applies={
            "framing": ["cuerpo entero", "medio"],
            "needs_body": True,
            "replaces_background": True,
            "min_source_quality": 0.5,
        },
        axes=["gesture", "expression", "garment_color"],
        selectable=["garment", "garment_color", "gesture", "expression"],
        chips={
            "garment": ["vestido ligero", "pareo", "camisa y pantalon de lino"],
            "garment_color": ["blanco", "crema", "azul claro", "coral"],
            "gesture": ["caminando por la orilla", "de pie mirando al mar", "girando",
                        "sentada en la arena", "mano en el pelo"],
            "expression": COMMON_EXPRESSION,
        },
    ),
    look(
        id="escenario_cafe_invierno",
        name="Café en invierno",
        category="escenarios",
        garment={
            "type": "jersey de punto grueso",
            "fabric": "lana trenzada",
            "colors": ["crema", "camel", "gris"],
            "details": "cuello alto, mangas amplias",
        },
        scene={
            "place": "interior de cafeteria con madera y ventana empanada",
            "time": "manana de invierno",
            "depth": "luces calidas desenfocadas al fondo",
        },
        lighting={
            "key": "luz de ventana fria por un lado",
            "fill": "lamparas calidas del interior por el otro",
            "mood": "acogedor, mezcla de temperaturas",
        },
        camera={"focal_mm": 50, "aperture": "f/1.8", "height": "pecho", "framing": "medio"},
        poses=["sentada con la taza", "mirando por la ventana", "apoyada en la mesa"],
        applies={
            "framing": ["medio", "primer plano"],
            "needs_body": False,
            "replaces_background": True,
            "min_source_quality": 0.45,
        },
        axes=["gesture", "expression", "garment_color"],
        selectable=["garment", "garment_color", "gesture", "expression"],
        chips={
            "garment": ["jersey de punto", "camisa de franela", "abrigo abierto"],
            "garment_color": ["crema", "camel", "gris", "verde oliva"],
            "gesture": ["sentada con la taza", "mirando por la ventana", "apoyada en la mesa",
                        "manos alrededor de la taza"],
            "expression": COMMON_EXPRESSION,
        },
    ),
    look(
        id="producto_en_mano",
        name="Producto en mano",
        category="producto",
        garment={
            "type": "manga lisa neutra",
            "fabric": "algodon mate",
            "colors": ["blanco", "negro"],
            "details": "sin estampado, para no competir con el producto",
        },
        scene={
            "place": "superficie lisa clara con fondo neutro",
            "time": None,
            "depth": "fondo liso, poca profundidad",
        },
        lighting={
            "key": "luz suave cenital ligeramente frontal",
            "fill": "rebote blanco para eliminar sombra dura",
            "mood": "limpio, comercial, sombra suave bajo el objeto",
        },
        camera={"focal_mm": 60, "aperture": "f/5.6", "height": "pecho", "framing": "primer plano"},
        poses=["sujetando el objeto a la altura del pecho", "mostrando el objeto en la palma"],
        applies={
            "framing": ["primer plano", "medio"],
            "needs_body": False,
            "replaces_background": True,
            "min_source_quality": 0.55,
        },
        # Hands are the most common generator failure and this look is
        # entirely about hands, so the gesture chips matter more here than
        # anywhere else - the reliability ordering will earn its keep.
        axes=["gesture", "light"],
        selectable=["gesture", "light", "garment_color"],
        chips={
            "gesture": ["sujetando a la altura del pecho", "en la palma abierta",
                        "entre las dos manos", "sujetando por el borde"],
            "light": ["como esta", "suave cenital", "estudio", "natural"],
            "garment_color": ["blanco", "negro"],
        },
    ),
]


def main() -> None:
    CATALOG.mkdir(parents=True, exist_ok=True)
    (CATALOG / "covers").mkdir(parents=True, exist_ok=True)

    written, skipped = 0, 0
    for entry in LOOKS:
        path = CATALOG / f"{entry['id']}.json"
        if path.exists():
            # Never clobber a hand edit. The catalog is the authored artefact;
            # this script only ever fills gaps.
            skipped += 1
            continue
        path.write_text(json.dumps(entry, indent=2, ensure_ascii=False), encoding="utf-8")
        written += 1

    total = len(list(CATALOG.glob("*.json")))
    print(f"escritos {written}, respetados {skipped}, total en el catalogo {total}")
    if total < 15:
        print(
            f"AVISO: {total} estilos. El objetivo de entrega son 15-20 "
            "escritos a mano; los recetarios flojos hacen un producto flojo "
            "por muy bueno que sea el resto."
        )


if __name__ == "__main__":
    main()
