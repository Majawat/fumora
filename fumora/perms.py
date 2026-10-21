"""File permissions and folder checks for an Unraid-style setup.

Fumora runs as PUID:PGID (default 99:100, nobody:users) with UMASK (default
0002), so everything it creates is group-writable (664 files, 775 folders) and
usable by other containers and SMB users. Files it *moves* in (from SABnzbd,
Shelfmark, a manual import) or unpacks from an archive would otherwise keep the
other program's or the archive's permissions, so those are set the same way.
Hardlinked torrent files are never touched: they are the very file the torrent
client seeds.

`folder_problems` says plainly when a folder Fumora needs is missing or not
writable, instead of failing later or quietly doing less.
"""

import logging
import os
from pathlib import Path

from sqlalchemy.orm import Session

from . import settings_store

log = logging.getLogger("fumora.perms")


def _read_umask() -> int:
    # Read once at import time, before any threads start (os.umask sets as it reads).
    mask = os.umask(0)
    os.umask(mask)
    return mask


UMASK = _read_umask()
FILE_MODE = 0o666 & ~UMASK
DIR_MODE = 0o777 & ~UMASK


def normalize(path: Path) -> None:
    """Give a file or folder Fumora placed the permissions it would have created it with."""
    try:
        os.chmod(path, DIR_MODE if path.is_dir() else FILE_MODE)
    except OSError as exc:
        log.warning("couldn't set permissions on %s: %s", path, exc)


def normalize_tree(root: Path) -> None:
    normalize(root)
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            normalize(Path(dirpath) / name)


def _writable(path: str) -> bool:
    return os.access(path, os.W_OK | os.X_OK)


def folder_problems(db: Session, config_dir: Path) -> list[str]:
    """Folders Fumora needs that it can't use, in words for the dashboard and the log."""
    problems: list[str] = []
    if not _writable(str(config_dir)):
        problems.append(
            f"The config folder {config_dir} isn't writable: settings and the database can't be saved."
        )
    library = settings_store.get(db, "library.path")
    if library:
        if not os.path.isdir(library):
            problems.append(
                f"The library folder {library} doesn't exist (check the container's /data mount)."
            )
        elif not _writable(library):
            problems.append(
                f"The library folder {library} is read-only for Fumora: imports, renames and tag writes "
                "will fail. Mount /data read-write when you're ready for Fumora to change files."
            )
    recycle = settings_store.get(db, "library.recycle_path")
    if recycle:
        existing = recycle
        while existing and not os.path.exists(existing):
            existing = os.path.dirname(existing)
        if existing and not _writable(existing):
            problems.append(
                f"The recycle folder {recycle} can't be created or written: conversions keep no backup."
            )
    if os.getuid() == 0:
        problems.append("Fumora is running as root: files it writes will be owned by root. Set PUID/PGID.")
    return problems
