"""Database backups (Sonarr's System -> Backup): scheduled, by hand, downloaded, restored.

Backups are consistent copies made with SQLite's online backup API (safe while
Fumora runs), in `<config>/backups/fumora-<when>-<label>.db`. A restore is staged as
`<config>/restore-pending.db` and swapped in at the next start, before migrations,
after the current database is itself backed up ("pre-restore"). The running app
never has its database replaced under it.

The database holds the settings, secrets included (API keys, passwords): backups
are as private as /config.
"""

import os
import re
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from .perms import normalize

LABELS = ("scheduled", "manual", "pre-restore", "uploaded")
_NAME = re.compile(r"^fumora-(\d{8}-\d{6})-(" + "|".join(LABELS) + r")(?:-\d+)?\.db$")
PENDING = "restore-pending.db"


class BackupError(Exception):
    pass


@dataclass
class BackupInfo:
    name: str
    path: Path
    size: int
    created: datetime
    label: str


def backup_dir(config_dir: Path) -> Path:
    return Path(config_dir) / "backups"


def _new_path(folder: Path, label: str) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    path, n = folder / f"fumora-{stamp}-{label}.db", 1
    while path.exists():
        path = folder / f"fumora-{stamp}-{label}-{n}.db"
        n += 1
    return path


def make_backup(db_path: Path, folder: Path, label: str = "manual") -> Path:
    """A consistent copy of the live database (WAL included)."""
    if label not in LABELS:
        raise ValueError(label)
    folder.mkdir(parents=True, exist_ok=True)
    normalize(folder)
    target = _new_path(folder, label)
    tmp = target.with_name(f".{target.name}.tmp")
    src = sqlite3.connect(db_path)  # read only by the backup; a WAL database needs a normal open
    try:
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)
            # A copy of a WAL database is in WAL mode too: then merely opening it leaves
            # -wal/-shm files next to it. A backup is one self-contained file.
            dst.execute("PRAGMA journal_mode=DELETE")
        finally:
            dst.close()
    finally:
        src.close()
    normalize(tmp)
    os.replace(tmp, target)
    return target


def list_backups(folder: Path) -> list[BackupInfo]:
    out = []
    if folder.is_dir():
        for p in folder.iterdir():
            m = _NAME.match(p.name)
            if m and p.is_file():
                created = datetime.strptime(m.group(1), "%Y%m%d-%H%M%S").replace(tzinfo=UTC)
                out.append(BackupInfo(p.name, p, p.stat().st_size, created, m.group(2)))
    return sorted(out, key=lambda b: (b.created, b.name), reverse=True)


def find(folder: Path, name: str) -> BackupInfo:
    """A backup by its file name (only names Fumora gave: no paths)."""
    for b in list_backups(folder):
        if b.name == name:
            return b
    raise BackupError(f"No backup called {name}")


def prune(folder: Path, keep: int) -> list[str]:
    """Delete scheduled backups beyond the newest `keep`. Others stay until deleted by hand."""
    scheduled = [b for b in list_backups(folder) if b.label == "scheduled"]
    gone = []
    for b in scheduled[max(keep, 1) :]:
        b.path.unlink(missing_ok=True)
        gone.append(b.name)
    return gone


def check(path: Path, known_revisions: set[str]) -> str:
    """Is this a sound Fumora database this version can use? Returns its schema revision."""
    try:
        # immutable: read the file as it is and write nothing next to it (no -wal/-shm).
        con = sqlite3.connect(f"file:{quote(str(path))}?mode=ro&immutable=1", uri=True)
    except sqlite3.Error as exc:
        raise BackupError(f"Not a database: {exc}") from exc
    try:
        if con.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise BackupError("The database is damaged (SQLite's check failed).")
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"alembic_version", "series", "issue", "setting"} <= tables:
            raise BackupError("Not a Fumora database.")
        rev = con.execute("SELECT version_num FROM alembic_version").fetchone()
    except sqlite3.DatabaseError as exc:
        raise BackupError(f"Not a database: {exc}") from exc
    finally:
        con.close()
    if not rev or rev[0] not in known_revisions:
        raise BackupError(
            f"It is from another Fumora version (schema {rev[0] if rev else '?'}): "
            "update Fumora before restoring it."
        )
    return rev[0]


def known_revisions() -> set[str]:
    from .db import MIGRATIONS_DIR

    return {p.name.split("_", 1)[0] for p in (MIGRATIONS_DIR / "versions").glob("[0-9]*_*.py")}


def stage_restore(config_dir: Path, backup: Path) -> None:
    check(backup, known_revisions())
    pending = Path(config_dir) / PENDING
    tmp = pending.with_name(f".{PENDING}.tmp")
    shutil.copyfile(backup, tmp)
    normalize(tmp)
    os.replace(tmp, pending)


def cancel_restore(config_dir: Path) -> bool:
    pending = Path(config_dir) / PENDING
    existed = pending.exists()
    pending.unlink(missing_ok=True)
    return existed


def restore_pending(config_dir: Path) -> bool:
    return (Path(config_dir) / PENDING).exists()


def apply_pending_restore(config_dir: Path, db_path: Path) -> str | None:
    """At start-up, before anything opens the database: swap in a staged restore.
    Returns a message for the log, or None when nothing was staged."""
    pending = Path(config_dir) / PENDING
    if not pending.exists():
        return None
    try:
        check(pending, known_revisions())
    except BackupError as exc:
        bad = pending.with_name(f"restore-rejected-{datetime.now(UTC):%Y%m%d-%H%M%S}.db")
        os.replace(pending, bad)
        return f"Restore not applied: {exc} (kept as {bad.name})"
    saved = None
    if db_path.exists():
        saved = make_backup(db_path, backup_dir(config_dir), "pre-restore")
    for base in (db_path, pending):
        for f in (base.with_name(base.name + "-wal"), base.with_name(base.name + "-shm")):
            f.unlink(missing_ok=True)
    os.replace(pending, db_path)
    normalize(db_path)
    return "Database restored from a backup" + (f"; the one it replaced is {saved.name}" if saved else "")
