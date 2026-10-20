"""Import files by hand (manual_import.py, /manual-import)."""

import os
import shutil
import zipfile
from datetime import date

import pytest
from sqlalchemy import select

from fumora import settings_store
from fumora.manual_import import (
    MANUAL_IMPORT,
    ImportPathError,
    check_path,
    make_manual_import_handler,
    propose,
)
from fumora.models import File, Issue, Job, Series
from tests.helpers import make_cbz
from tests.test_downloads import _cbr_download, _setup
from tests.test_m2_routes_jobs import _job

pytestmark = pytest.mark.skipif(shutil.which("bsdtar") is None, reason="bsdtar not installed")


def _lib(app, tmp_path, numbers=("15", "16", "17")):
    sid = _setup(app, tmp_path, numbers=numbers)
    with app.state.session_factory() as db:
        settings_store.put(db, "import.roots", str(tmp_path))
        db.scalar(select(Issue).where(Issue.number == "16")).metron_id = 4743
        db.commit()
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    return sid, inbox


def _with_metroninfo(path, metron_id):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("page001.jpg", b"\xff\xd8\xff" + b"x" * 500)
        zf.writestr(
            "MetronInfo.xml",
            f'<?xml version="1.0"?><MetronInfo><IDS><ID source="Metron" primary="true">{metron_id}</ID>'
            "</IDS></MetronInfo>",
        )
    return path


def _rows(app, path):
    with app.state.session_factory() as db:
        rows = propose(db, check_path(db, str(path)))
        db.rollback()
        return {r.name: r for r in rows}


def test_proposals_from_ids_comicinfo_and_names(app, tmp_path):
    _lib(app, tmp_path)
    inbox = tmp_path / "inbox"
    _with_metroninfo(inbox / "scan0042.cbz", 4743)  # name says nothing; the id does
    make_cbz(
        inbox / "whatever.cbz", "<ComicInfo><Series>Absolute Batman</Series><Number>15</Number></ComicInfo>"
    )
    make_cbz(inbox / "Absolute Batman 017 (2026) (Digital).cbz")
    make_cbz(inbox / "Absolute Batman 099 (2026).cbz")
    make_cbz(inbox / "Some Other Comic 001.cbz")
    rows = _rows(app, inbox)
    assert (rows["scan0042.cbz"].number, rows["scan0042.cbz"].evidence) == ("16", "ids in the file")
    assert (rows["whatever.cbz"].number, rows["whatever.cbz"].evidence) == ("15", "ComicInfo")
    r17 = rows["Absolute Batman 017 (2026) (Digital).cbz"]
    assert r17.ready and r17.evidence == "file name"
    assert r17.dest.endswith("Absolute Batman (2024)/Absolute Batman 017 (2026).cbz")
    assert rows["Absolute Batman 099 (2026).cbz"].problem == "Absolute Batman has no #99."
    assert rows["Some Other Comic 001.cbz"].problem == "Which series? None matched."


def test_never_overwrites_or_steals_a_library_file(app, tmp_path):
    _lib(app, tmp_path)
    inbox = tmp_path / "inbox"
    lib_file = tmp_path / "library" / "Absolute Batman (2024)" / "Absolute Batman 015 (2026).cbz"
    lib_file.parent.mkdir(parents=True)
    make_cbz(lib_file)
    with app.state.session_factory() as db:
        db.scalar(select(Issue).where(Issue.number == "15")).files = [File(path=str(lib_file))]
        db.commit()
    make_cbz(inbox / "Absolute Batman 015.cbz")
    taken = lib_file.parent / "Absolute Batman 017 (2026).cbz"
    make_cbz(taken)  # an unlinked file already has #17's name
    make_cbz(inbox / "Absolute Batman 017.cbz")
    rows = _rows(app, inbox)
    assert rows["Absolute Batman 015.cbz"].problem == "#15 already has a file."
    assert rows["Absolute Batman 017.cbz"].problem == "Absolute Batman 017 (2026).cbz already exists."
    lib_rows = _rows(app, lib_file.parent)
    assert lib_rows["Absolute Batman 015 (2026).cbz"].problem.startswith("Already in the library as")
    # #17's file sitting at its own name is simply linked where it is.
    assert lib_rows["Absolute Batman 017 (2026).cbz"].ready


def test_only_under_import_folders(app, tmp_path):
    _lib(app, tmp_path)
    with app.state.session_factory() as db:
        with pytest.raises(ImportPathError):
            check_path(db, "/etc")
        with pytest.raises(ImportPathError):
            check_path(db, str(tmp_path / "inbox" / ".." / ".." / "elsewhere"))


def test_same_name_series_year_decides(app, tmp_path):
    _lib(app, tmp_path)
    with app.state.session_factory() as db:
        old = Series(name="Absolute Batman", year_began=1990, locked_fields=[], path=str(tmp_path / "old"))
        db.add(Issue(series=old, number="17", cover_date=date(1991, 1, 1), locked_fields=[]))
        db.commit()
    make_cbz(tmp_path / "inbox" / "Absolute Batman 017 (2026).cbz")
    make_cbz(tmp_path / "inbox" / "Absolute Batman 017 (1991).cbz")
    rows = _rows(app, tmp_path / "inbox")
    with app.state.session_factory() as db:
        new_id = db.scalar(select(Series.id).where(Series.year_began == 2024))
        old_id = db.scalar(select(Series.id).where(Series.year_began == 1990))
    assert rows["Absolute Batman 017 (2026).cbz"].series_id == new_id
    assert rows["Absolute Batman 017 (1991).cbz"].series_id == old_id


def test_manga_volume_from_folder_name(app, tmp_path):
    _lib(app, tmp_path)
    with app.state.session_factory() as db:
        s = Series(name="Mieruko-chan", year_began=2020, numbering="volumes", locked_fields=[],
                   path=str(tmp_path / "library" / "Mieruko-chan"))  # fmt: skip
        db.add(Issue(series=s, number="12", title="Volume 12", locked_fields=[]))
        db.commit()
    folder = tmp_path / "inbox" / "Mieruko-chan"
    folder.mkdir()
    make_cbz(folder / "Vol. 12 (2025).cbz")
    make_cbz(tmp_path / "inbox" / "Absolute Batman Vol. 2 (2026).cbz")  # a volume isn't issue #2 here
    rows = _rows(app, tmp_path / "inbox")
    assert rows["Vol. 12 (2025).cbz"].ready and rows["Vol. 12 (2025).cbz"].evidence == "folder name"
    assert not rows["Absolute Batman Vol. 2 (2026).cbz"].ready


async def test_move_imports_converts_and_tags(app, tmp_path):
    _lib(app, tmp_path)
    inbox = tmp_path / "inbox"
    cbr = _cbr_download(inbox, "Absolute Batman 017 (2026).cbr")
    with app.state.session_factory() as db:
        issue = db.scalar(select(Issue).where(Issue.number == "17"))
        items = [{"path": str(cbr), "issue_id": issue.id}]
    msg = await make_manual_import_handler()(_job(app, MANUAL_IMPORT, {"items": items, "mode": "move"}))
    assert "1 imported" in msg
    with app.state.session_factory() as db:
        f = db.scalar(select(File))
        assert f.path.endswith("Absolute Batman (2024)/Absolute Batman 017 (2026).cbz")
        with zipfile.ZipFile(f.path) as zf:
            assert b"<Series>Absolute Batman</Series>" in zf.read("ComicInfo.xml")
    assert not cbr.exists()  # moved (converted, original removed)


async def test_hardlink_keeps_the_original(app, tmp_path):
    _lib(app, tmp_path)
    src = make_cbz(tmp_path / "inbox" / "Absolute Batman 017.cbz")
    with app.state.session_factory() as db:
        issue_id = db.scalar(select(Issue.id).where(Issue.number == "17"))
    await make_manual_import_handler()(
        _job(app, MANUAL_IMPORT, {"items": [{"path": str(src), "issue_id": issue_id}], "mode": "hardlink"})
    )
    with app.state.session_factory() as db:
        f = db.scalar(select(File))
    assert src.exists() and os.stat(f.path).st_ino == os.stat(src).st_ino


def test_page_and_start(app, tmp_path, logged_in):
    _lib(app, tmp_path)
    inbox = tmp_path / "inbox"
    make_cbz(inbox / "Absolute Batman 017 (2026).cbz")
    make_cbz(inbox / "Absolute Batman 15.cbz")
    page = logged_in.get("/manual-import", params={"path": str(inbox)}).text
    assert "Absolute Batman 017 (2026).cbz" in page and "matched by file name" in page
    assert logged_in.get("/manual-import", params={"path": "/etc"}).text.count("isn&#39;t under") == 1
    with app.state.session_factory() as db:
        sid = db.scalar(select(Series.id))
    label = f"Absolute Batman (2024) [{sid}]"
    files = [str(inbox / "Absolute Batman 017 (2026).cbz"), str(inbox / "Absolute Batman 15.cbz")]
    # Both rows set to #17: the second can't also be #17.
    form = {
        "path": str(inbox),
        "file": files,
        "series": [label, label],
        "include": ["0", "1"],
        "mode": "move",
    }
    r = logged_in.post("/manual-import", data={**form, "number": ["17", "17"]})
    assert "Another row is already importing as this issue." in r.text
    with app.state.session_factory() as db:
        assert db.scalar(select(Job).where(Job.kind == MANUAL_IMPORT)) is None
    app.state.jobs.start = lambda kind, params: setattr(app.state, "started", (kind, params))
    r = logged_in.post("/manual-import", data={**form, "number": ["17", "15"]}, follow_redirects=False)
    assert r.status_code == 303
    kind, params = app.state.started
    assert kind == MANUAL_IMPORT and len(params["items"]) == 2 and params["mode"] == "move"
