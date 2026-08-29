"""Delete, bin, restore, purge.

This is the only feature in the product that destroys her work, so the tests
are about guarantees rather than mechanics:

  * deleting is REVERSIBLE for a week - a mis-tap on a phone must not be able
    to lose a photograph
  * a binned photo stops being visible AND stops being servable immediately,
    not in a week, or "deleted" is a lie
  * purging removes the DERIVATIVES too - a leftover thumbnail is still a
    picture of her on the disk
  * purging her source photos does NOT break her profile, which is exactly
    what she asked for: keep the measurements, drop the originals
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.images import build_derivatives, destroy, store_upload
from app.store import Store

RETENTION = 7.0


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "test.sqlite3")


@pytest.fixture
def dirs(tmp_path: Path):
    images = tmp_path / "images"
    derivatives = tmp_path / "derivatives"
    images.mkdir(parents=True, exist_ok=True)
    return images, derivatives


def make_image(images: Path, derivatives: Path, name: str = "shot") -> Path:
    from PIL import Image

    path = images / f"{name}.png"
    Image.new("RGB", (400, 500), (150, 120, 100)).save(path)
    build_derivatives(path, derivatives)
    return path


def add(store: Store, path: Path, image_id: str) -> None:
    store.add_image(image_id=image_id, kind="final", path=str(path))


# -- deleting is reversible --------------------------------------------------


def test_a_deleted_photo_leaves_the_gallery(store: Store, dirs) -> None:
    images, derivatives = dirs
    add(store, make_image(images, derivatives, "a"), "id-a")
    assert len(store.gallery()) == 1

    store.move_to_bin(["id-a"])
    assert store.gallery() == []
    assert store.bin_count() == 1


def test_the_file_survives_deletion(store: Store, dirs) -> None:
    """THE POINT OF A BIN. The row is flagged; the photograph is untouched
    until the retention window passes."""
    images, derivatives = dirs
    path = make_image(images, derivatives, "b")
    add(store, path, "id-b")

    store.move_to_bin(["id-b"])
    assert path.exists(), "a binned photo must still be on disk to be restorable"


def test_restore_brings_it_back(store: Store, dirs) -> None:
    images, derivatives = dirs
    path = make_image(images, derivatives, "c")
    add(store, path, "id-c")

    store.move_to_bin(["id-c"])
    assert store.restore(["id-c"]) == 1

    assert len(store.gallery()) == 1
    assert store.bin_count() == 0
    assert path.exists()


def test_restoring_something_not_in_the_bin_does_nothing(store: Store, dirs) -> None:
    """Not an error, but not a silent success either - it reports zero, so a
    caller cannot mistake it for having recovered something."""
    images, derivatives = dirs
    add(store, make_image(images, derivatives, "d"), "id-d")
    assert store.restore(["id-d"]) == 0
    assert store.restore(["never-existed"]) == 0


def test_deleting_twice_is_harmless(store: Store, dirs) -> None:
    images, derivatives = dirs
    add(store, make_image(images, derivatives, "e"), "id-e")
    store.move_to_bin(["id-e"])
    first = store.bin_contents()[0].deleted_at
    store.move_to_bin(["id-e"])
    # The clock must not restart, or repeated taps would keep a photo alive
    # in the bin forever.
    assert store.bin_contents()[0].deleted_at == first


# -- the retention window ----------------------------------------------------


def test_nothing_expires_before_the_window(store: Store, dirs) -> None:
    images, derivatives = dirs
    add(store, make_image(images, derivatives, "f"), "id-f")
    store.move_to_bin(["id-f"])
    assert store.expired(retention_days=RETENTION) == []


def test_expiry_after_the_window(store: Store, dirs) -> None:
    images, derivatives = dirs
    add(store, make_image(images, derivatives, "g"), "id-g")
    store.move_to_bin(["id-g"])

    # Backdate the deletion rather than waiting a week.
    with store.connect() as db:
        db.execute(
            "UPDATE images SET deleted_at=? WHERE id=?",
            (time.time() - (RETENTION + 1) * 86_400, "id-g"),
        )

    expired = store.expired(retention_days=RETENTION)
    assert [r.id for r in expired] == ["id-g"]


def test_days_left_counts_down(store: Store, dirs) -> None:
    images, derivatives = dirs
    add(store, make_image(images, derivatives, "h"), "id-h")
    store.move_to_bin(["id-h"])

    with store.connect() as db:
        db.execute(
            "UPDATE images SET deleted_at=? WHERE id=?",
            (time.time() - 5 * 86_400, "id-h"),
        )

    row = store.bin_contents()[0]
    assert 1.9 < row.days_left(RETENTION) < 2.1
    assert "2 dias" in row.expiry_es(RETENTION)


def test_expiry_wording_is_readable_near_the_end(store: Store, dirs) -> None:
    """'quedan 0 dias' is useless for deciding whether to act now."""
    images, derivatives = dirs
    add(store, make_image(images, derivatives, "i"), "id-i")
    store.move_to_bin(["id-i"])
    with store.connect() as db:
        db.execute(
            "UPDATE images SET deleted_at=? WHERE id=?",
            (time.time() - 6.8 * 86_400, "id-i"),
        )
    assert "hora" in store.bin_contents()[0].expiry_es(RETENTION)


# -- purging really destroys -------------------------------------------------


def test_purge_removes_the_original_and_every_derivative(dirs) -> None:
    """A leftover thumbnail is still a photograph of her on the disk, and
    still servable to anyone who knows the URL."""
    images, derivatives = dirs
    path = make_image(images, derivatives, "j")
    thumb = derivatives / "thumb" / "j.webp"
    medium = derivatives / "medium" / "j.webp"
    assert path.exists() and thumb.exists() and medium.exists()

    removed = destroy(path, derivatives)

    assert not path.exists()
    assert not thumb.exists(), "the thumbnail is still her photograph"
    assert not medium.exists()
    assert len(removed) == 3


def test_destroy_never_raises_on_missing_files(dirs) -> None:
    """A purge that aborts halfway leaves rows gone and files behind, which is
    harder to reason about than a file that failed and gets retried."""
    images, derivatives = dirs
    assert destroy(images / "does-not-exist.png", derivatives) == []


def test_forget_removes_the_row(store: Store, dirs) -> None:
    images, derivatives = dirs
    add(store, make_image(images, derivatives, "k"), "id-k")
    store.move_to_bin(["id-k"])
    store.forget(["id-k"])

    assert store.bin_count() == 0
    assert store.gallery() == []
    assert store.image("id-k") is None


# -- her actual request ------------------------------------------------------


def test_deleting_source_photos_does_not_break_the_profile(tmp_path: Path) -> None:
    """What she asked for: keep the measurements, drop the originals.

    The centroid and the proportion baseline are DERIVED. Once built, the
    source photographs can be destroyed and every downstream check still
    works - which is a privacy win, not a compromise.
    """
    import numpy as np

    from app.contracts.attribute_ir import BodyProportions
    from app.profile.model import Coverage, IdentityProfile

    profile_dir = tmp_path / "profile"
    sources = tmp_path / "sources"
    sources.mkdir()

    originals = []
    for i in range(3):
        p = sources / f"src{i}.png"
        p.write_bytes(b"pretend photo")
        originals.append(p)

    profile = IdentityProfile(
        owner="Nayane",
        centroid=np.ones(512, dtype=np.float32) / np.sqrt(512),
        dispersion=0.04,
        proportions=BodyProportions(shoulder_torso_ratio=0.72, hip_torso_ratio=0.64),
        skin_lab=(62.0, 12.0, 18.0),
        coverage=Coverage(full_body=6),
    )
    profile.save(profile_dir)

    # Destroy every source photograph.
    for p in originals:
        p.unlink()
    assert not any(p.exists() for p in originals)

    reloaded = IdentityProfile.load(profile_dir)
    assert reloaded is not None
    assert reloaded.can_check_identity, "identity must survive losing the sources"
    assert reloaded.can_check_proportions, "the anti-slimming baseline must survive"
    assert reloaded.skin_lab == (62.0, 12.0, 18.0)


# -- migration ---------------------------------------------------------------


def test_an_existing_database_gains_the_column(tmp_path: Path) -> None:
    """Deployed installs already hold her photographs. CREATE TABLE IF NOT
    EXISTS does nothing to a table that exists, so the column has to be added
    by migration or the feature simply fails on every real install."""
    import sqlite3

    path = tmp_path / "old.sqlite3"
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE images (id TEXT PRIMARY KEY, kind TEXT NOT NULL,"
        " path TEXT NOT NULL, session_id TEXT, look_id TEXT, slot TEXT,"
        " score REAL, kept INTEGER NOT NULL DEFAULT 0, report TEXT,"
        " created_at REAL NOT NULL)"
    )
    old.execute(
        "INSERT INTO images (id,kind,path,kept,created_at) VALUES ('old','final','x.png',1,1)"
    )
    old.commit()
    old.close()

    store = Store(path)  # migrates on open

    assert len(store.gallery()) == 1, "the pre-existing photo must survive"
    assert store.bin_count() == 0
    store.move_to_bin(["old"])
    assert store.bin_count() == 1
    assert store.restore(["old"]) == 1
