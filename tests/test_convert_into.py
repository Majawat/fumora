"""#31: a CBR/CB7/CBT/PDF download is converted straight into the library: one write, no copy."""

import errno
import os
import shutil
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import select

from fumora import convert, settings_store
from fumora.cbz import ArchiveError
from fumora.grabbing import ImportFailed, _place, place_issue_file
from fumora.library_jobs import naming_settings
from fumora.models import Issue
from fumora.perms import FILE_MODE
from tests.helpers import make_cbz
from tests.test_downloads import _cbr_download, _setup

pytestmark = pytest.mark.skipif(shutil.which("bsdtar") is None, reason="bsdtar not installed")


def _no_copies(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("the download was copied")

    for name in ("copyfile", "copy", "copy2", "copytree", "move"):
        monkeypatch.setattr(shutil, name, refuse)


def test_convert_into_writes_a_new_cbz_and_only_reads_the_source(tmp_path, monkeypatch):
    src = _cbr_download(tmp_path / "dl", "x.cbr")
    raw = src.read_bytes()
    lib = tmp_path / "lib"
    lib.mkdir()
    _no_copies(monkeypatch)
    out = convert.convert_into(src, lib / "X 001.cbz")
    assert out == lib / "X 001.cbz"
    with zipfile.ZipFile(out) as z:
        assert sorted(n for n in z.namelist() if n.endswith(".jpg")) == ["p0.jpg", "p1.jpg", "p2.jpg"]
    assert src.read_bytes() == raw  # untouched
    assert sorted(p.name for p in lib.iterdir()) == ["X 001.cbz"]  # no work folder or temp zip left
    assert sorted(p.name for p in src.parent.iterdir()) == ["x.cbr"]  # nothing written next to the download
    assert (os.stat(out).st_mode & 0o777) == FILE_MODE


def test_convert_into_works_from_a_read_only_download_folder(tmp_path):
    src = _cbr_download(tmp_path / "dl", "x.cbr")
    lib = tmp_path / "lib"
    lib.mkdir()
    os.chmod(src.parent, 0o555)
    try:
        assert zipfile.is_zipfile(convert.convert_into(src, lib / "x.cbz"))
    finally:
        os.chmod(src.parent, 0o755)


def test_convert_into_never_overwrites(tmp_path):
    src = _cbr_download(tmp_path / "dl", "x.cbr")
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "x.cbz").write_bytes(b"keep me")
    with pytest.raises(ArchiveError, match="already exists"):
        convert.convert_into(src, lib / "x.cbz")
    assert (lib / "x.cbz").read_bytes() == b"keep me"


def test_convert_into_cleans_up_after_a_bad_archive(tmp_path):
    src = tmp_path / "dl" / "x.cbr"
    src.parent.mkdir()
    src.write_bytes(b"Rar!\x1a\x07\x00" + b"\x00" * 600)  # a RAR header, nothing inside
    lib = tmp_path / "lib"
    lib.mkdir()
    with pytest.raises(ArchiveError):
        convert.convert_into(src, lib / "x.cbz")
    assert list(lib.iterdir()) == [] and src.exists()


def test_convert_into_rebuilds_a_zip_with_the_wrong_name(tmp_path):
    (tmp_path / "dl").mkdir()
    src = make_cbz(tmp_path / "dl" / "x.cbr")
    out = convert.convert_into(src, tmp_path / "x.cbz")
    assert zipfile.is_zipfile(out) and src.exists()


def _lib(app, tmp_path):
    _setup(app, tmp_path, numbers=("17",))
    with app.state.session_factory() as db:
        settings_store.put(db, "import.tag", "off")
        db.commit()
    (tmp_path / "library" / "Absolute Batman (2024)").mkdir(parents=True, exist_ok=True)


@pytest.mark.parametrize("keep_original", [False, True])
def test_importing_a_cbr_download_writes_once_into_the_library(app, tmp_path, monkeypatch, keep_original):
    _lib(app, tmp_path)
    src = _cbr_download(tmp_path / "downloads", "Absolute Batman 017 (2026).cbr")
    _no_copies(monkeypatch)
    with app.state.session_factory() as db:
        issue = db.scalar(select(Issue).where(Issue.number == "17"))
        row, problem = place_issue_file(db, issue.series, issue, src, keep_original, naming_settings(db))
        assert problem is None
        placed = Path(row.path)
        assert placed.suffix == ".cbz" and zipfile.is_zipfile(placed)
        assert sorted(p.name for p in placed.parent.iterdir()) == [placed.name]
    assert src.exists() == keep_original  # a torrent keeps seeding; a usenet download is removed


def test_moves_and_hardlinks_still_refuse_another_mount(tmp_path, monkeypatch):
    def cross(*a, **k):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    (tmp_path / "dl").mkdir()
    src = make_cbz(tmp_path / "dl" / "x.cbz")
    monkeypatch.setattr(os, "rename", cross)
    monkeypatch.setattr(os, "link", cross)
    for protocol in ("usenet", "torrent"):
        with pytest.raises(ImportFailed, match="different mounts"):
            _place(src, tmp_path / "lib" / "x.cbz", protocol)
