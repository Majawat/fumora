"""Unraid permissions: what Fumora places is group-writable; folder problems are said out loud."""

import os
import shutil
import stat
import zipfile

import pytest
from sqlalchemy import select

from fumora import settings_store
from fumora.ddl import _unpack
from fumora.manual_import import MANUAL_IMPORT, make_manual_import_handler
from fumora.models import File, Issue
from fumora.perms import DIR_MODE, FILE_MODE, folder_problems
from tests.helpers import make_cbz
from tests.test_m2_routes_jobs import _job
from tests.test_manual_import import _lib

pytestmark = pytest.mark.skipif(shutil.which("bsdtar") is None, reason="bsdtar not installed")


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


async def _import(app, src, mode):
    with app.state.session_factory() as db:
        settings_store.put(db, "import.tag", "off")
        db.commit()
        issue_id = db.scalar(select(Issue.id).where(Issue.number == "17"))
    await make_manual_import_handler()(
        _job(app, MANUAL_IMPORT, {"items": [{"path": str(src), "issue_id": issue_id}], "mode": mode})
    )
    with app.state.session_factory() as db:
        return db.scalar(select(File)).path


async def test_moved_in_files_get_fumoras_permissions(app, tmp_path):
    _lib(app, tmp_path)
    src = make_cbz(tmp_path / "inbox" / "Absolute Batman 017.cbz")
    os.chmod(src, 0o600)  # e.g. another container's owner-only file
    placed = await _import(app, src, "move")
    assert _mode(placed) == FILE_MODE
    assert _mode(os.path.dirname(placed)) & DIR_MODE == DIR_MODE


async def test_hardlinked_torrent_files_are_left_alone(app, tmp_path):
    _lib(app, tmp_path)
    src = make_cbz(tmp_path / "inbox" / "Absolute Batman 017.cbz")
    os.chmod(src, 0o644)
    placed = await _import(app, src, "hardlink")
    assert os.stat(placed).st_ino == os.stat(src).st_ino and _mode(src) == 0o644


def test_unpacked_packs_get_fumoras_permissions(tmp_path):
    inner = make_cbz(tmp_path / "Batman 001.cbz")
    os.chmod(inner, 0o600)
    pack = tmp_path / "job" / "pack.zip"
    pack.parent.mkdir()
    with zipfile.ZipFile(pack, "w") as zf:
        zf.write(inner, "Batman 001.cbz")
    _unpack(pack)
    assert _mode(pack.parent / "Batman 001.cbz") == FILE_MODE


@pytest.mark.skipif(os.getuid() == 0, reason="root can write anywhere")
def test_folder_problems_are_reported(app, tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    with app.state.session_factory() as db:
        settings_store.put(db, "library.path", str(lib))
        settings_store.put(db, "library.recycle_path", str(lib / ".fumora-recycle"))
        db.commit()
        assert folder_problems(db, tmp_path) == []
        os.chmod(lib, 0o555)
        try:
            problems = folder_problems(db, tmp_path)
        finally:
            os.chmod(lib, 0o755)
        assert any("read-only" in p for p in problems) and any("recycle" in p for p in problems)
        settings_store.put(db, "library.path", str(tmp_path / "missing"))
        assert any("doesn't exist" in p for p in folder_problems(db, tmp_path))


@pytest.mark.skipif(os.getuid() == 0, reason="root can write anywhere")
def test_dashboard_shows_folder_problems(app, tmp_path, logged_in):
    with app.state.session_factory() as db:
        settings_store.put(db, "library.path", str(tmp_path / "nowhere"))
        db.commit()
    assert "doesn&#39;t exist" in logged_in.get("/").text


def test_new_database_is_group_writable(tmp_path):
    from fumora.db import make_engine, upgrade_db

    url = f"sqlite:///{tmp_path / 'fresh.db'}"
    upgrade_db(url)
    engine = make_engine(url)
    with engine.connect():
        pass
    engine.dispose()
    assert _mode(tmp_path / "fresh.db") == FILE_MODE  # SQLite alone would make it 0644


def test_existing_database_is_corrected(tmp_path):
    from fumora.db import make_engine

    db = tmp_path / "old.db"
    db.touch()
    os.chmod(db, 0o644)
    make_engine(f"sqlite:///{db}").dispose()
    assert _mode(db) == FILE_MODE
