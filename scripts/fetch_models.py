"""Download the quality gate's CV models.

Two models, both ONNX, both CPU:

  buffalo_l           face detection + ArcFace recognition -> identity
  yolov8n-pose.onnx   17 COCO body keypoints -> proportions AND hand location

The second one is the anti-slimming check. Without it a generator can narrow
her and nothing in the system notices, which is why its absence is reported in
capitals rather than tolerated.

Two rather than three: a dedicated hand-landmark model would allow finger
COUNTING, but pose already gives the wrists, and a located hand is what the
repair loop actually needs. Whether a hand is broken is the visual judge's
call on finals - a pixel metric cannot tell six fingers from five.

Nothing is vendored into the repo: the weights are ~200 MB and would make
every clone slow for no benefit.

HONEST NOTE. buffalo_l downloads itself through insightface, which is a
maintained path. The pose export has no equally stable canonical URL - export
locations move and checksums change - so this script verifies what is present
and says exactly what is missing and where it goes, rather than hardcoding a
link that will rot and fail silently at 3am.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402

EXPECTED = {
    "buffalo_l": "carpeta - deteccion facial + ArcFace (identidad)",
    "yolov8n-pose.onnx": "keypoints de cuerpo - proporciones (anti-adelgazamiento) Y manos",
}

POSE_EXPORT = """\
  Exportado desde ultralytics. Se exporta UNA vez y se copia el .onnx: ni
  ultralytics ni torch tienen que quedarse en el servidor, que es lo que
  mantiene la imagen pequena en ARM.

  No hace falta otra maquina. Se puede instalar en un directorio aparte y
  borrarlo despues, sin tocar el interprete de la aplicacion. Windows con el
  Python portable (que no tiene venv) - probado, tarda unos minutos y ocupa
  ~1.1 GB temporales:

    set TMP=C:\\temp\\poseexport
    python -m pip install --target %TMP%\\lib ultralytics
    python -c "import sys,os; sys.path.insert(0, r'%TMP%\\lib'); os.chdir(r'%TMP%'); \\
               from ultralytics import YOLO; YOLO('yolov8n-pose.pt').export(format='onnx', opset=12)"
    copy %TMP%\\yolov8n-pose.onnx {target}
    rmdir /s /q %TMP%

  El sys.path.insert es necesario: el Python embebido usa un ._pth en modo
  aislado e ignora PYTHONPATH, asi que --target por si solo no basta.

  Resultado esperado: 12.9 MB, opset 12.
"""


def fetch_buffalo(models_dir: Path) -> bool:
    target = models_dir / "buffalo_l"
    if target.exists():
        print(f"  buffalo_l          ya esta en {target}")
        return True
    try:
        from insightface.app import FaceAnalysis  # type: ignore[import-not-found]
    except ImportError:
        print("  buffalo_l          FALTA - instala primero requirements-cv.txt")
        return False

    print("  buffalo_l          descargando (~300 MB, una sola vez)...")
    try:
        app = FaceAnalysis(name="buffalo_l", root=str(models_dir))
        app.prepare(ctx_id=-1, det_size=(640, 640))  # ctx_id=-1 -> CPU
    except Exception as exc:  # noqa: BLE001
        print(f"  buffalo_l          ERROR: {exc}")
        return False
    print("  buffalo_l          listo")
    return True


def check(models_dir: Path, name: str) -> bool:
    path = models_dir / name
    if path.exists():
        size = (
            sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
            if path.is_dir()
            else path.stat().st_size
        )
        print(f"  {name:19s} ok ({size / 1_048_576:.0f} MB)")
        return True
    print(f"  {name:19s} FALTA")
    return False


def main() -> int:
    models_dir = settings.models_dir
    models_dir.mkdir(parents=True, exist_ok=True)
    print(f"modelos en {models_dir}\n")

    fetch_buffalo(models_dir)
    pose_ok = check(models_dir, "yolov8n-pose.onnx")

    if not pose_ok:
        print("\nPara el modelo de pose:")
        print(POSE_EXPORT.format(target=models_dir / "yolov8n-pose.onnx"))

    # Re-detect with the cache cleared, so the summary reflects what is on
    # disk right now rather than what was true when the app last booted.
    from app.gate.backends import detect_capabilities

    detect_capabilities.cache_clear()
    capabilities = detect_capabilities(models_dir)

    print("=" * 62)
    if capabilities.fully_available:
        print("Todas las comprobaciones del gate estan disponibles.")
    else:
        print("El gate arrancara en modo degradado:")
        for missing in capabilities.missing_es():
            print(f"  - {missing}")
        print()
        print("El sistema funciona igual, pero lo dira en cada pantalla.")
        print("Una comprobacion que no se puede hacer NUNCA se da por buena.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
