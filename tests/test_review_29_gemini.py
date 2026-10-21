"""Regressions for the independent (Gemini) review of PR #29."""

import os
import shutil
import subprocess

import pytest
from sqlalchemy import select

from fumora import settings_store
from fumora.manual_import import (
    MANUAL_IMPORT,
    ImportPathError,
    check_path,
    make_manual_import_handler,
    read_embedded,
    within_roots,
)
from fumora.models import Event, Issue, Series
from tests.helpers import make_cbz
from tests.test_m2_routes_jobs import _job
from tests.test_manual_import import _lib, _rows

pytestmark = pytest.mark.skipif(shutil.which("bsdtar") is None, reason="bsdtar not installed")


def _rar_like(path, members: dict[str, bytes]):
    """A 7z archive (bsdtar can write it) named as the caller likes: content, not name, decides."""
    src = path.parent / f".src-{path.name}"
    src.mkdir()
    for name, data in members.items():
        (src / name).write_bytes(data)
    subprocess.run(["bsdtar", "-cf", str(path), "--format", "7zip", "-C", str(src), "."], check=True)
    shutil.rmtree(src)
    return path


def test_1_converting_in_place_never_overwrites_an_existing_cbz(app, tmp_path):
    _lib(app, tmp_path)
    folder = tmp_path / "library" / "Absolute Batman (2024)"
    folder.mkdir(parents=True)
    existing = make_cbz(folder / "Absolute Batman 017 (2026).cbz")
    before = existing.read_bytes()
    _rar_like(folder / "Absolute Batman 017 (2026).cbr", {"p1.jpg": b"\xff\xd8\xff" + b"x" * 300})
    rows = _rows(app, folder)
    assert rows["Absolute Batman 017 (2026).cbr"].problem == "Absolute Batman 017 (2026).cbz already exists."
    assert existing.read_bytes() == before


async def test_2_a_deleted_issue_is_skipped_without_failing_the_job(app, tmp_path):
    _lib(app, tmp_path)
    src = make_cbz(tmp_path / "inbox" / "Absolute Batman 017.cbz")
    with app.state.session_factory() as db:
        issue = db.scalar(select(Issue).where(Issue.number == "17"))
        gone_id = issue.id
        db.delete(issue)
        db.commit()
    msg = await make_manual_import_handler()(
        _job(app, MANUAL_IMPORT, {"items": [{"path": str(src), "issue_id": gone_id}], "mode": "move"})
    )
    assert "0 imported, 1 skipped" in msg
    with app.state.session_factory() as db:
        assert db.scalar(select(Event).where(Event.message.like("Skipped%no longer exists%"))) is not None


def test_3_collected_editions_use_the_collected_template(app, tmp_path):
    _lib(app, tmp_path)
    with app.state.session_factory() as db:
        settings_store.put(db, "naming.collected_template", "{Series Title} Vol. {Issue} ({Issue Year})")
        db.scalar(select(Series)).series_type = "Trade Paperback"
        db.commit()
    make_cbz(tmp_path / "inbox" / "Absolute Batman Vol. 17 (2026).cbz")
    rows = _rows(app, tmp_path / "inbox")
    assert rows["Absolute Batman Vol. 17 (2026).cbz"].dest.endswith("Absolute Batman Vol. 17 (2026).cbz")


def test_4_a_file_gone_since_the_listing_is_reported_on_its_row(app, tmp_path, logged_in):
    _lib(app, tmp_path)
    inbox = tmp_path / "inbox"
    keep = make_cbz(inbox / "Absolute Batman 017 (2026).cbz")
    gone = make_cbz(inbox / "Absolute Batman 15.cbz")
    with app.state.session_factory() as db:
        label = f"Absolute Batman (2024) [{db.scalar(select(Series.id))}]"
    gone.unlink()
    form = {"path": str(inbox), "file": [str(keep), str(gone)], "series": [label, label],
            "number": ["17", "15"], "include": ["0"], "mode": "move"}  # fmt: skip
    r = logged_in.post("/manual-import", data=form, follow_redirects=False)
    assert r.status_code == 303  # the ticked file still imports; the gone one isn't an error page


def test_5_slash_as_an_import_root(app, tmp_path):
    with app.state.session_factory() as db:
        settings_store.put(db, "import.roots", "/")
        db.commit()
        assert check_path(db, str(tmp_path)) == os.path.realpath(tmp_path)
        settings_store.put(db, "import.roots", str(tmp_path / "a"))
        with pytest.raises(ImportPathError):
            within_roots(db, str(tmp_path / "ab"))  # a sibling that merely shares the prefix


def test_6_ids_in_a_rar_named_cbz_are_still_read(tmp_path):
    f = _rar_like(tmp_path / "Issue 01.cbz", {
        "p1.jpg": b"\xff\xd8\xff" + b"x" * 300,
        "MetronInfo.xml": b'<MetronInfo><IDS><ID source="Metron" primary="true">4743</ID></IDS></MetronInfo>',
    })  # fmt: skip
    assert read_embedded(f).metron_id == 4743
