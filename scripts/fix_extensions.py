"""Rename stored images whose extension does not match their bytes.

The Cloudflare adapter hardcoded ".png" while the service returns JPEG, so
every photograph it produced was served as Content-Type: image/png containing
JPEG data. Browsers refuse to render that. The request was a clean 200, the
file was intact, and the picture was simply blank - a hard failure to read,
because nothing is missing and nothing errors.

The adapter now sniffs the format. This repairs what it wrote before that,
and updates the database rows and derivatives to match so nothing is orphaned.

    python scripts/fix_extensions.py            report only
    python scripts/fix_extensions.py --apply    rename and update
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.providers.cloudflare import _suffix_for  # noqa: E402
from app.store import Store  # noqa: E402


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually rename")
    args = ap.parse_args(argv)

    store = Store(settings.db_path)
    rows = {}
    for kind in ("final", "preview", "upload"):
        for row in store.gallery(limit=5000, kind=kind):
            rows[str(Path(row.path))] = row

    wrong: list[tuple[Path, Path]] = []
    for folder in (settings.images_dir, settings.uploads_dir):
        for path in sorted(Path(folder).glob("*")):
            if not path.is_file():
                continue
            try:
                head = path.open("rb").read(16)
            except OSError:
                continue
            correct = _suffix_for(head)
            if correct != path.suffix.lower():
                wrong.append((path, path.with_suffix(correct)))

    if not wrong:
        print("  Nothing to fix: every extension matches its bytes.")
        return 0

    print(f"  {len(wrong)} file(s) with the wrong extension:\n")
    for old, new in wrong:
        print(f"    {old.name}")
        print(f"      -> {new.name}")

    if not args.apply:
        print("\n  Report only. Re-run with --apply to rename.")
        return 0

    renamed = 0
    for old, new in wrong:
        if new.exists():
            print(f"  ! {new.name} already exists, skipping {old.name}")
            continue
        old.rename(new)

        # The database stores an absolute path, and the derivative is named
        # after the stem - which does not change - so only the row needs
        # updating. Left stale, the gallery would ask for a file that is no
        # longer there and we would have traded one blank photo for another.
        row = rows.get(str(old))
        if row is not None:
            with store.connect() as db:
                db.execute(
                    "UPDATE images SET path=? WHERE id=?", (str(new), row.id)
                )
        renamed += 1

    print(f"\n  {renamed} renamed, database rows updated.")
    print("  Derivatives are named by stem, which is unchanged, so they still match.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
