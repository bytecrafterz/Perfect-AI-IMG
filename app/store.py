"""Persistence.

Plain sqlite3 from the standard library.  For one user this is not a
compromise, it is the correct engineering choice: no server, no connection
pool, no migration tool, and a backup is copying one file.

Deliberately no ORM.  The schema is eight columns wide and an ORM would add a
dependency, a wheel to build on the ARM target, and a layer of indirection
over queries that fit on one line.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS images (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,          -- upload | preview | final | profile
    path         TEXT NOT NULL,
    session_id   TEXT,
    look_id      TEXT,
    slot         TEXT,
    score        REAL,
    kept         INTEGER NOT NULL DEFAULT 0,
    report       TEXT,
    created_at   REAL NOT NULL,
    -- Soft delete. NULL means live; a timestamp means it is in the bin and
    -- will be destroyed for real once the retention window passes.
    --
    -- Nothing is ever deleted outright on her say-so. She is working on a
    -- phone, the delete control sits next to the image, and a mis-tap that
    -- permanently destroys a photograph she liked is not a mistake this
    -- system should be capable of making.
    deleted_at   REAL
);
CREATE INDEX IF NOT EXISTS images_session ON images(session_id);
CREATE INDEX IF NOT EXISTS images_kind_created ON images(kind, created_at DESC);
CREATE INDEX IF NOT EXISTS images_kept ON images(kept, created_at DESC);
-- NOTE: the index on deleted_at is created in _migrate(), not here.
-- CREATE TABLE IF NOT EXISTS is a no-op against an existing table, so on a
-- database made before that column existed this script would try to index a
-- column that is not there yet and fail before the migration could add it.

CREATE TABLE IF NOT EXISTS sessions (
    id           TEXT PRIMARY KEY,
    source_id    TEXT,
    look_id      TEXT,
    selections   TEXT,
    cost_usd     REAL NOT NULL DEFAULT 0,
    delivered    INTEGER NOT NULL DEFAULT 0,
    elapsed_s    REAL,
    created_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS costs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL,
    kind         TEXT NOT NULL,
    provider_id  TEXT NOT NULL,
    usd          REAL NOT NULL,
    detail       TEXT,
    at           REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS costs_at ON costs(at DESC);

-- The learning layer.  Chosen and unchosen previews are both recorded:
-- every session yields positive AND negative labels at no effort from her,
-- which is what makes case-based learning work from about the tenth session.
CREATE TABLE IF NOT EXISTS chip_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    look_id      TEXT NOT NULL,
    attribute    TEXT NOT NULL,
    value        TEXT NOT NULL,
    shown        INTEGER NOT NULL DEFAULT 0,
    kept         INTEGER NOT NULL DEFAULT 0,
    generated    INTEGER NOT NULL DEFAULT 0,
    passed_gate  INTEGER NOT NULL DEFAULT 0,
    at           REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS chip_lookup ON chip_events(look_id, attribute, value);

CREATE TABLE IF NOT EXISTS bandit (
    look_id      TEXT NOT NULL,
    provider_id  TEXT NOT NULL,
    successes    REAL NOT NULL DEFAULT 1,
    failures     REAL NOT NULL DEFAULT 1,
    PRIMARY KEY (look_id, provider_id)
);

CREATE TABLE IF NOT EXISTS settings (
    key          TEXT PRIMARY KEY,
    value        TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class ImageRow:
    id: str
    kind: str
    path: str
    session_id: str | None
    look_id: str | None
    slot: str | None
    score: float | None
    kept: bool
    created_at: float
    deleted_at: float | None = None

    @property
    def name(self) -> str:
        return Path(self.path).name

    @property
    def stem(self) -> str:
        return Path(self.path).stem

    @property
    def in_bin(self) -> bool:
        return self.deleted_at is not None

    def days_left(self, retention_days: float) -> float:
        """How long before this is destroyed for good."""
        if self.deleted_at is None:
            return retention_days
        elapsed = (time.time() - self.deleted_at) / 86_400
        return max(0.0, retention_days - elapsed)

    def expiry_es(self, retention_days: float) -> str:
        """Plain words, because "expires in 0.3 days" helps nobody decide
        whether to act now."""
        days = self.days_left(retention_days)
        if days <= 0:
            return "se borra en cualquier momento"
        if days < 1:
            hours = max(1, math.ceil(days * 24))
            return f"quedan {hours} hora{'s' if hours != 1 else ''}"
        # CEIL, not int(). Truncating tells someone with 1.99 days left that
        # they have 1, and tells someone who deleted a photo ten seconds ago
        # that they have 6 of their 7 days. Both under-report, consistently,
        # for the entire life of every item in the bin.
        whole = math.ceil(days)
        return f"quedan {whole} dia{'s' if whole != 1 else ''}"


class Store:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)
            # WAL so a long read (the gallery) never blocks a write (a batch
            # finishing).  On one box with one user this is the difference
            # between "instant" and "occasionally stuck".
            db.execute("PRAGMA journal_mode=WAL")
            self._migrate(db)

    def _migrate(self, db: sqlite3.Connection) -> None:
        """Bring an existing database up to the current schema.

        CREATE TABLE IF NOT EXISTS does nothing to a table that already
        exists, so a column added later never appears on a database created
        before it. Deployed installs already hold her photographs; dropping
        and recreating is not an option.
        """
        existing = {row["name"] for row in db.execute("PRAGMA table_info(images)")}
        if "deleted_at" not in existing:
            db.execute("ALTER TABLE images ADD COLUMN deleted_at REAL")
        # Created here rather than in SCHEMA, and unconditionally, so it exists
        # on both a fresh database and a migrated one.
        db.execute("CREATE INDEX IF NOT EXISTS images_deleted ON images(deleted_at)")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self._path, timeout=10.0)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        finally:
            db.close()

    # -- images ------------------------------------------------------------

    def add_image(
        self,
        *,
        image_id: str,
        kind: str,
        path: str,
        session_id: str | None = None,
        look_id: str | None = None,
        slot: str | None = None,
        score: float | None = None,
        report: dict | None = None,
    ) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO images"
                " (id, kind, path, session_id, look_id, slot, score, kept, report, created_at)"
                " VALUES (?,?,?,?,?,?,?,COALESCE((SELECT kept FROM images WHERE id=?),0),?,?)",
                (
                    image_id, kind, path, session_id, look_id, slot, score,
                    image_id,
                    json.dumps(report, ensure_ascii=False) if report else None,
                    time.time(),
                ),
            )

    def mark_kept(self, image_ids: list[str], kept: bool = True) -> None:
        if not image_ids:
            return
        with self.connect() as db:
            db.executemany(
                "UPDATE images SET kept=? WHERE id=?",
                [(1 if kept else 0, i) for i in image_ids],
            )

    def gallery(self, *, limit: int = 60, kind: str = "final", kept_only: bool = False) -> list[ImageRow]:
        # deleted_at IS NULL everywhere a live image is listed. A binned photo
        # that still shows up in the gallery would make "delete" a lie.
        query = "SELECT * FROM images WHERE kind=? AND deleted_at IS NULL"
        params: list[object] = [kind]
        if kept_only:
            query += " AND kept=1"
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self.connect() as db:
            return [_row_to_image(r) for r in db.execute(query, params)]

    # -- bin ---------------------------------------------------------------

    def move_to_bin(self, image_ids: list[str]) -> int:
        """Soft delete. Reversible for the retention window."""
        if not image_ids:
            return 0
        now = time.time()
        with self.connect() as db:
            cur = db.executemany(
                "UPDATE images SET deleted_at=? WHERE id=? AND deleted_at IS NULL",
                [(now, i) for i in image_ids],
            )
            return cur.rowcount if cur.rowcount and cur.rowcount > 0 else len(image_ids)

    def restore(self, image_ids: list[str]) -> int:
        """Bring images back out of the bin.

        Only touches rows still IN the bin: restoring something already purged
        is impossible, and this makes that explicit rather than silently
        resurrecting a row whose file is gone.
        """
        if not image_ids:
            return 0
        with self.connect() as db:
            cur = db.executemany(
                "UPDATE images SET deleted_at=NULL WHERE id=? AND deleted_at IS NOT NULL",
                [(i,) for i in image_ids],
            )
            return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    def bin_contents(self, *, limit: int = 200) -> list[ImageRow]:
        """Newest first, so what she just deleted is what she sees."""
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM images WHERE deleted_at IS NOT NULL"
                " ORDER BY deleted_at DESC LIMIT ?",
                (limit,),
            )
            return [_row_to_image(r) for r in rows]

    def expired(self, *, retention_days: float) -> list[ImageRow]:
        """Everything past the retention window - the purge candidates.

        Returned rather than deleted so the caller can remove the FILES first
        and only then the rows. Deleting rows first would orphan the files
        with nothing left pointing at them.
        """
        cutoff = time.time() - retention_days * 86_400
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM images WHERE deleted_at IS NOT NULL AND deleted_at < ?",
                (cutoff,),
            )
            return [_row_to_image(r) for r in rows]

    def forget(self, image_ids: list[str]) -> int:
        """Remove the rows. Call only after the files are gone."""
        if not image_ids:
            return 0
        with self.connect() as db:
            db.executemany("DELETE FROM images WHERE id=?", [(i,) for i in image_ids])
        return len(image_ids)

    def bin_count(self) -> int:
        with self.connect() as db:
            row = db.execute(
                "SELECT COUNT(*) AS n FROM images WHERE deleted_at IS NOT NULL"
            ).fetchone()
        return int(row["n"])

    def recent_sessions(self, limit: int = 8) -> list[sqlite3.Row]:
        with self.connect() as db:
            return list(
                db.execute(
                    "SELECT * FROM sessions ORDER BY created_at DESC LIMIT ?", (limit,)
                )
            )

    def image(self, image_id: str) -> ImageRow | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM images WHERE id=?", (image_id,)).fetchone()
        return _row_to_image(row) if row else None

    # -- sessions ----------------------------------------------------------

    def open_session(
        self, *, session_id: str, source_id: str | None, look_id: str | None, selections: dict
    ) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO sessions"
                " (id, source_id, look_id, selections, cost_usd, delivered, created_at)"
                " VALUES (?,?,?,?,0,0,?)",
                (
                    session_id, source_id, look_id,
                    json.dumps(selections, ensure_ascii=False), time.time(),
                ),
            )

    def close_session(self, *, session_id: str, cost_usd: float, delivered: int, elapsed_s: float) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE sessions SET cost_usd=?, delivered=?, elapsed_s=? WHERE id=?",
                (cost_usd, delivered, elapsed_s, session_id),
            )

    # -- costs -------------------------------------------------------------

    def add_cost(self, *, session_id: str, kind: str, provider_id: str, usd: float, detail: str, at: float) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO costs (session_id, kind, provider_id, usd, detail, at)"
                " VALUES (?,?,?,?,?,?)",
                (session_id, kind, provider_id, usd, detail, at),
            )

    def spend_since(self, since: float) -> float:
        with self.connect() as db:
            row = db.execute(
                "SELECT COALESCE(SUM(usd),0) AS total FROM costs WHERE at>=?", (since,)
            ).fetchone()
        return float(row["total"])

    def costs_since(self, since: float) -> list[dict]:
        """Every priced call since `since`, for rehydrating the ledger.

        The daily cap is only real if it survives a restart. The Ledger is an
        in-memory accumulator that starts empty, so without this the $10/day
        limit resets every time the process comes up - and the watchdog is
        configured to restart it up to 999 times.
        """
        with self.connect() as db:
            rows = db.execute(
                "SELECT session_id, kind, provider_id, usd, detail, at"
                " FROM costs WHERE at>=? ORDER BY at",
                (since,),
            ).fetchall()
        return [dict(r) for r in rows]

    # -- preferences -------------------------------------------------------

    def preference(self, key: str, default: str = "") -> str:
        """One saved setting.

        The Ajustes chips had a click handler that toggled a CSS class and no
        route behind them: they looked like controls, changed nothing, and
        reverted on reload. A setting that cannot be saved is worse than an
        absent one, because it invites her to believe she has chosen.
        """
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS preferences ("
                " key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            row = db.execute(
                "SELECT value FROM preferences WHERE key=?", (key,)
            ).fetchone()
        return str(row["value"]) if row else default

    def set_preference(self, key: str, value: str) -> None:
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS preferences ("
                " key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            db.execute(
                "INSERT INTO preferences (key, value) VALUES (?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )

    def spend_summary(self) -> dict[str, float]:
        now = time.time()
        return {
            "today": self.spend_since(now - 86_400),
            "week": self.spend_since(now - 7 * 86_400),
            "month": self.spend_since(now - 30 * 86_400),
            "all": self.spend_since(0),
        }

    # -- learning ----------------------------------------------------------

    def record_chip(
        self, *, look_id: str, attribute: str, value: str,
        shown: int = 0, kept: int = 0, generated: int = 0, passed_gate: int = 0,
    ) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO chip_events"
                " (look_id, attribute, value, shown, kept, generated, passed_gate, at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (look_id, attribute, value, shown, kept, generated, passed_gate, time.time()),
            )

    def chip_stats(self, look_id: str) -> dict[str, dict[str, int]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT attribute, value,"
                " SUM(shown) shown, SUM(kept) kept,"
                " SUM(generated) generated, SUM(passed_gate) passed_gate"
                " FROM chip_events WHERE look_id=? GROUP BY attribute, value",
                (look_id,),
            ).fetchall()
        return {
            f"{r['attribute']}:{r['value']}": {
                "shown": int(r["shown"] or 0),
                "kept": int(r["kept"] or 0),
                "generated": int(r["generated"] or 0),
                "passed_gate": int(r["passed_gate"] or 0),
            }
            for r in rows
        }

    def load_bandit(self) -> dict[str, dict[str, float]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM bandit").fetchall()
        return {
            f"{r['look_id']}|{r['provider_id']}": {
                "successes": float(r["successes"]),
                "failures": float(r["failures"]),
            }
            for r in rows
        }

    def save_bandit(self, snapshot: dict[str, dict[str, float]]) -> None:
        with self.connect() as db:
            for key, values in snapshot.items():
                look_id, _, provider_id = key.partition("|")
                db.execute(
                    "INSERT OR REPLACE INTO bandit (look_id, provider_id, successes, failures)"
                    " VALUES (?,?,?,?)",
                    (look_id, provider_id, values["successes"], values["failures"]),
                )

    # -- settings ----------------------------------------------------------

    def get_setting(self, key: str, default: str = "") -> str:
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, value)
            )


def _row_to_image(row: sqlite3.Row) -> ImageRow:
    keys = row.keys()
    return ImageRow(
        id=row["id"],
        kind=row["kind"],
        path=row["path"],
        session_id=row["session_id"],
        look_id=row["look_id"],
        slot=row["slot"],
        score=row["score"],
        kept=bool(row["kept"]),
        created_at=row["created_at"],
        # Tolerated as absent so a row read before the migration ran does not
        # raise; it simply reads as live.
        deleted_at=row["deleted_at"] if "deleted_at" in keys else None,
    )
