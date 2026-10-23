"""Deleting a series, and the recycle folder setting that decides where deleted files go."""

import os
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import func, select

from fumora import convert, settings_store
from fumora.models import Event, File, Grab, Issue, Job, Series
from fumora.recycle import discard, recycle_dir
from fumora.series_delete import DeleteFailed, delete_series, plan_delete
from tests.helpers import make_cbz
from tests.test_downloads import _cbr_download


def _setup(app, tmp_path, recycle="on"):
    lib, bin_ = tmp_path / "library", tmp_path / "recycle"
    with app.state.session_factory() as db:
        settings_store.put(db, "library.path", str(lib))
        settings_store.put(db, "library.recycle_path", str(bin_))
        settings_store.put(db, "library.recycle", recycle)
        settings_store.put(db, "import.roots", str(tmp_path / "downloads"))
        db.commit()
    return lib, bin_


def _series(db, folder: Path, name="Saga", numbers=("1", "2")) -> Series:
    folder.mkdir(parents=True, exist_ok=True)
    s = Series(name=name, year_began=2012, path=str(folder), locked_fields=[])
    db.add(s)
    for n in numbers:
        i = Issue(series=s, number=n, locked_fields=[])
        f = make_cbz(folder / f"{name} {n.zfill(3)} (2012).cbz")
        i.files.append(File(path=str(f), size=f.stat().st_size))
        db.add(i)
    db.commit()
    return s


def test_delete_keeps_files_unless_asked(app, tmp_path):
    lib, _ = _setup(app, tmp_path)
    with app.state.session_factory() as db:
        s = _series(db, lib / "Saga (2012)")
        delete_series(db, s, delete_files=False, recycle=recycle_dir(db))
        assert db.scalar(select(func.count(Series.id))) == 0
        assert db.scalar(select(func.count(Issue.id))) == 0
        assert db.scalar(select(func.count(File.id))) == 0
        assert "files were kept" in db.scalar(select(Event.message).order_by(Event.id.desc()))
    assert len(list((lib / "Saga (2012)").iterdir())) == 2


def test_delete_moves_the_folder_to_the_recycle_folder(app, tmp_path):
    lib, bin_ = _setup(app, tmp_path)
    with app.state.session_factory() as db:
        s = _series(db, lib / "Saga (2012)")
        delete_series(db, s, delete_files=True, recycle=recycle_dir(db))
    assert not (lib / "Saga (2012)").exists() and lib.exists()
    moved = list(bin_.glob("*/Saga (2012)"))
    assert len(moved) == 1 and len(list(moved[0].iterdir())) == 2


def test_recycle_off_deletes_for_good(app, tmp_path):
    lib, bin_ = _setup(app, tmp_path, recycle="off")
    with app.state.session_factory() as db:
        assert recycle_dir(db) is None
        s = _series(db, lib / "Saga (2012)")
        delete_series(db, s, delete_files=True, recycle=recycle_dir(db))
        assert "deleted for good" in db.scalar(select(Event.message).order_by(Event.id.desc()))
    assert not (lib / "Saga (2012)").exists() and not bin_.exists()


def test_a_shared_folder_loses_only_this_series_files(app, tmp_path):
    lib, bin_ = _setup(app, tmp_path)
    with app.state.session_factory() as db:
        a = _series(db, lib / "Mixed", name="Saga")
        _series(db, lib / "Mixed", name="Paper Girls")
        plan = plan_delete(db, a)
        assert plan.folder is None and "Another series" in plan.folder_note and len(plan.files) == 2
        delete_series(db, a, delete_files=True, recycle=recycle_dir(db))
    assert sorted(p.name for p in (lib / "Mixed").iterdir()) == [
        "Paper Girls 001 (2012).cbz",
        "Paper Girls 002 (2012).cbz",
    ]
    assert len(list(bin_.rglob("Saga*.cbz"))) == 2


@pytest.mark.parametrize("where", ["library", "parent", "recycle", "import"])
def test_never_removes_a_protected_folder(app, tmp_path, where):
    lib, bin_ = _setup(app, tmp_path)
    folder = {"library": lib, "parent": tmp_path, "recycle": bin_, "import": tmp_path / "downloads"}[where]
    with app.state.session_factory() as db:
        s = _series(db, folder)
        plan = plan_delete(db, s)
        assert plan.folder is None and len(plan.files) == 2
        delete_series(db, s, delete_files=True, recycle=None)
    assert folder.is_dir()


def test_files_outside_the_folder_go_too(app, tmp_path):
    lib, _ = _setup(app, tmp_path)
    with app.state.session_factory() as db:
        s = _series(db, lib / "Saga (2012)")
        stray = make_cbz(lib / "Saga 003 (2012).cbz")
        i = Issue(series=s, number="3", locked_fields=[])
        i.files.append(File(path=str(stray), size=1))
        db.add(i)
        db.commit()
        plan = plan_delete(db, s)
        assert plan.folder == str(lib / "Saga (2012)") and plan.files == [str(stray)]
        delete_series(db, s, delete_files=True, recycle=None)
    assert not stray.exists() and lib.is_dir()


def test_a_download_in_progress_blocks_the_delete(app, tmp_path):
    lib, _ = _setup(app, tmp_path)
    with app.state.session_factory() as db:
        s = _series(db, lib / "Saga (2012)")
        db.add(Grab(issue_id=s.issues[0].id, issue_ids=[s.issues[0].id], state="downloading",
                    release_title="Saga 001", protocol="usenet", client="sabnzbd", indexer="x"))  # fmt: skip
        db.commit()
        with pytest.raises(DeleteFailed, match="Queue page"):
            delete_series(db, s, delete_files=True, recycle=None)
        assert db.scalar(select(func.count(Series.id))) == 1
    assert (lib / "Saga (2012)").is_dir()


def test_a_running_library_job_blocks_the_delete_but_another_series_job_does_not(app, tmp_path):
    lib, _ = _setup(app, tmp_path)
    with app.state.session_factory() as db:
        s = _series(db, lib / "Saga (2012)")
        db.add(Job(kind="scan", state="running", params={"series_ids": [s.id + 100]}))
        db.commit()
        assert plan_delete(db, s).blockers == []
        db.add(Job(kind="retag", state="running", params={}))
        db.commit()
        assert "retag" in plan_delete(db, s).blockers[0]


def test_a_failed_removal_keeps_the_series(app, tmp_path, monkeypatch):
    lib, _ = _setup(app, tmp_path)
    with app.state.session_factory() as db:
        s = _series(db, lib / "Saga (2012)")

        def refuse(*a, **k):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr("fumora.series_delete.discard", refuse)
        with pytest.raises(DeleteFailed, match="Nothing was taken out"):
            delete_series(db, s, delete_files=True, recycle=None)
        db.rollback()
        assert db.scalar(select(func.count(Series.id))) == 1


def test_delete_pages(app, tmp_path, logged_in):
    lib, bin_ = _setup(app, tmp_path)
    with app.state.session_factory() as db:
        sid = _series(db, lib / "Saga (2012)").id
    page = logged_in.get(f"/series/{sid}/delete")
    assert page.status_code == 200 and "recycle folder" in page.text and "Saga (2012)" in page.text
    r = logged_in.post(f"/series/{sid}/delete", data={"delete_files": "true"}, follow_redirects=False)
    assert r.status_code == 303
    assert logged_in.get(f"/series/{sid}").status_code == 404
    assert len(list(bin_.glob("*/Saga (2012)"))) == 1


def test_recycle_off_converts_without_keeping_the_original(tmp_path):
    src = _cbr_download(tmp_path / "lib", "x.cbr")
    out = convert.convert_to_cbz(src, None)
    assert zipfile.is_zipfile(out) and not src.exists()


def test_discard_never_overwrites_in_the_recycle_folder(tmp_path):
    bin_ = tmp_path / "bin"
    for _ in range(2):
        d = tmp_path / "Saga (2012)"
        d.mkdir()
        (d / "a.cbz").write_bytes(b"x")
        discard(d, bin_)
    assert sorted(p.name for p in next(bin_.iterdir()).iterdir()) == ["Saga (2012)", "Saga (2012) (1)"]
    assert os.path.isdir(tmp_path)


def test_the_settings_page_turns_the_recycle_folder_off(app, logged_in):
    page = logged_in.get("/settings")
    assert "library.recycle" in page.text
    logged_in.post("/settings", data={"library.recycle": "off"})
    with app.state.session_factory() as db:
        assert recycle_dir(db) is None
