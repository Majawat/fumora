import os
import zipfile

import pytest

from fumora import cbz
from fumora.models import File, Issue, Series
from fumora.tagging import NotWritable, plan_file, tag_file
from tests.helpers import make_cbz, metron_issue
from tests.test_comicinfo import EXISTING


def test_write_metadata_keeps_pages_and_mode(tmp_path):
    p = make_cbz(tmp_path / "a.cbz", comicinfo=EXISTING)
    os.chmod(p, 0o666)
    before = {i.filename: i.CRC for i in zipfile.ZipFile(p).infolist() if i.filename.endswith(".jpg")}
    cbz.write_metadata(p, {"ComicInfo.xml": b"<ComicInfo/>", "MetronInfo.xml": b"<MetronInfo/>"})
    with zipfile.ZipFile(p) as zf:
        names = [i.filename for i in zf.infolist()]
        assert {i.filename: i.CRC for i in zf.infolist() if i.filename.endswith(".jpg")} == before
        assert names.count("ComicInfo.xml") == 1 and zf.read("ComicInfo.xml") == b"<ComicInfo/>"
        assert zf.read("MetronInfo.xml") == b"<MetronInfo/>"
    assert os.stat(p).st_mode & 0o777 == 0o666
    assert not list(tmp_path.glob(".*fumora-tmp"))


def test_corrupt_archive_is_left_untouched(tmp_path):
    p = make_cbz(tmp_path / "bad.cbz", comicinfo=EXISTING)
    data = bytearray(p.read_bytes())
    i = data.find(b"\xff\xd8\xff")
    data[i + 10] ^= 0xFF  # flip a byte inside a stored page: CRC now fails
    p.write_bytes(bytes(data))
    original = p.read_bytes()
    with pytest.raises(cbz.ArchiveError):
        cbz.write_metadata(p, {"ComicInfo.xml": b"<ComicInfo/>"})
    assert p.read_bytes() == original
    assert not list(tmp_path.glob(".*fumora-tmp"))


def test_cbr_is_refused(tmp_path):
    with pytest.raises(cbz.ArchiveError):
        cbz.write_metadata(tmp_path / "x.cbr", {"ComicInfo.xml": b""})


def _series_with_file(app, tmp_path, confirmed="auto", name="a.cbz"):
    d = metron_issue(186450)
    path = make_cbz(tmp_path / name, comicinfo=EXISTING) if name.endswith(".cbz") else tmp_path / name
    if not path.exists():
        path.write_bytes(b"rar")
    db = app.state.session_factory()
    s = Series(
        name="Private Dance", year_began=2023, metron_id=17753, match_status="matched",
        confirmed_by=confirmed, series_type="Single Issue", locked_fields=[],
    )  # fmt: skip
    i = Issue(series=s, number="1", metron_id=186450, metron_detail=d, locked_fields=[])
    f = File(issue=i, path=str(path))
    db.add_all([s, i, f])
    db.flush()
    return db, i, f


def test_dry_run_changes_nothing(app, tmp_path):
    db, issue, f = _series_with_file(app, tmp_path)
    before = open(f.path, "rb").read()
    result = tag_file(issue, f, dry_run=True)
    assert open(f.path, "rb").read() == before
    assert result["error"] is None and result["dry_run"]
    assert result["fields"]["Series"] == ["Old Name", "Private Dance"]
    assert result["fields"]["MetronInfo.xml"] == [None, "added"]
    assert f.tagged_at is None


def test_write_then_nothing_left_to_change(app, tmp_path):
    db, issue, f = _series_with_file(app, tmp_path)
    assert tag_file(issue, f, dry_run=False)["error"] is None
    assert f.tagged_at is not None
    with zipfile.ZipFile(f.path) as zf:
        assert b"<Series>Private Dance</Series>" in zf.read("ComicInfo.xml")
        assert b"<Tags>Explicit, Favorite</Tags>" in zf.read("ComicInfo.xml")
        assert b"<IDS>" in zf.read("MetronInfo.xml")
    assert tag_file(issue, f, dry_run=True)["fields"] == {}


def test_unconfirmed_match_is_not_written(app, tmp_path):
    db, issue, f = _series_with_file(app, tmp_path, confirmed=None)
    with pytest.raises(NotWritable):
        plan_file(issue, f)
    assert "not confirmed" in tag_file(issue, f, dry_run=False)["error"]


def test_cbr_file_reports_error(app, tmp_path):
    db, issue, f = _series_with_file(app, tmp_path, name="a.cbr")
    assert "CBZ only" in tag_file(issue, f, dry_run=True)["error"]
