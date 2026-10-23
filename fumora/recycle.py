"""The recycle folder: where replaced or deleted library files go, if the owner wants one.

Settings: `library.recycle` ("on": move to the folder, "off": delete for good) and
`library.recycle_path`. Callers get `recycle_dir(db)`: a folder, or None for "delete"."""

import os
import shutil
from datetime import date
from pathlib import Path

from sqlalchemy.orm import Session

from . import settings_store

DEFAULT_RECYCLE = "/data/media/fumora-recycle"


def recycle_dir(db: Session) -> Path | None:
    if (settings_store.get(db, "library.recycle") or "on") == "off":
        return None
    return Path(settings_store.get(db, "library.recycle_path") or DEFAULT_RECYCLE)


def discard(path: Path, recycle: Path | None) -> Path | None:
    """Move a file or folder into today's subfolder of `recycle` (never over another
    one), or delete it for good when `recycle` is None. Returns where it went."""
    path = Path(path)
    if recycle is None:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
        return None
    target_dir = Path(recycle) / date.today().isoformat()
    target_dir.mkdir(parents=True, exist_ok=True)
    is_dir = path.is_dir()
    target = target_dir / path.name
    n = 1
    while target.exists():
        target = target_dir / (f"{path.name} ({n})" if is_dir else f"{path.stem} ({n}){path.suffix}")
        n += 1
    try:
        os.rename(path, target)
    except OSError:
        shutil.move(str(path), str(target))  # recycle folder on another mount
    return target
