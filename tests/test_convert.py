import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from fumora import convert
from fumora.cbz import ArchiveError
from fumora.models import File, Issue, Series
from fumora.tagging import tag_file
from tests.helpers import FIXTURES, make_cbz, metron_issue

pytestmark = pytest.mark.skipif(shutil.which("bsdtar") is None, reason="bsdtar not installed")
ARCHIVES = FIXTURES / "archives"


def _pages(folder: Path, n: int = 3) -> list[str]:
    folder.mkdir(parents=True, exist_ok=True)
    names = []
    for i in range(1, n + 1):
        (folder / f"p{i:02}.jpg").write_bytes(b"\xff\xd8\xff" + bytes([i]) * 300)
        names.append(f"p{i:02}.jpg")
    (folder / "ComicInfo.xml").write_text("<ComicInfo><Series>From RAR</Series><Tags>Kept</Tags></ComicInfo>")
    return names + ["ComicInfo.xml"]


def _bsdtar_create(out: Path, src: Path, fmt: str) -> Path:
    subprocess.run(["bsdtar", "-cf", str(out), "--format", fmt, "-C", str(src), "."], check=True)
    return out


def test_sniff(tmp_path):
    assert convert.sniff(ARCHIVES / "rar.rar") == "rar"
    assert convert.sniff(ARCHIVES / "rar5_multiple_files.rar") == "rar"
    assert convert.sniff(make_cbz(tmp_path / "z.cbr")) == "zip"


def test_mislabelled_zip_is_just_renamed(tmp_path):
    p = make_cbz(tmp_path / "Issue 001.cbr")
    inode = os.stat(p).st_ino
    new = convert.convert_to_cbz(p, tmp_path / "recycle")
    assert new.name == "Issue 001.cbz" and os.stat(new).st_ino == inode
    assert not (tmp_path / "recycle").exists()


@pytest.mark.parametrize("fmt,ext", [("7zip", ".cb7"), ("ustar", ".cbt")])
def test_7z_and_tar_convert_with_original_recycled(tmp_path, fmt, ext):
    _pages(tmp_path / "src")
    src = _bsdtar_create(tmp_path / f"Issue 002{ext}", tmp_path / "src", fmt)
    new = convert.convert_to_cbz(src, tmp_path / "recycle")
    assert new.suffix == ".cbz" and not src.exists()
    with zipfile.ZipFile(new) as zf:
        names = sorted(n.removeprefix("./") for n in zf.namelist())
        assert names == ["ComicInfo.xml", "p01.jpg", "p02.jpg", "p03.jpg"]
        assert (
            zf.getinfo([n for n in zf.namelist() if n.endswith("p01.jpg")][0]).compress_type
            == zipfile.ZIP_STORED
        )
    recycled = list((tmp_path / "recycle").rglob(f"*{ext}"))
    assert len(recycled) == 1 and recycled[0].read_bytes()[:2] in (b"7z", b".\x00", b"./")
    assert not list(tmp_path.glob(".*fumora*"))


def test_real_rar_is_readable_and_imageless_rar_is_refused(tmp_path):
    for name in ("rar.rar", "rar5_multiple_files.rar"):
        p = tmp_path / name.replace(".rar", ".cbr")
        shutil.copy(ARCHIVES / name, p)
        before = p.read_bytes()
        with pytest.raises(ArchiveError, match="no images"):
            convert.convert_to_cbz(p, tmp_path / "recycle")
        assert p.read_bytes() == before  # untouched
    assert convert.read_member(ARCHIVES / "rar.rar", "TEST.TXT") is not None  # case-insensitive


def test_encrypted_rar_is_refused_untouched(tmp_path):
    p = tmp_path / "locked.cbr"
    shutil.copy(ARCHIVES / "rar4_encrypted.rar", p)
    before = p.read_bytes()
    with pytest.raises(ArchiveError):
        convert.convert_to_cbz(p, tmp_path / "recycle")
    assert p.read_bytes() == before
    assert not list(tmp_path.glob(".*fumora*"))


def test_symlinks_inside_archives_are_dropped(tmp_path):
    src = tmp_path / "src"
    _pages(src)
    os.symlink("/etc/passwd", src / "evil.jpg")
    arc = _bsdtar_create(tmp_path / "x.cbt", src, "ustar")
    new = convert.convert_to_cbz(arc, tmp_path / "recycle")
    with zipfile.ZipFile(new) as zf:
        assert not any("evil" in n for n in zf.namelist())


def test_cbz_that_is_really_7z_keeps_original_safe(tmp_path):
    _pages(tmp_path / "src")
    p = _bsdtar_create(tmp_path / "Issue 003.cbz", tmp_path / "src", "7zip")
    original = p.read_bytes()
    assert convert.needs_conversion(p)
    new = convert.convert_to_cbz(p, tmp_path / "recycle")
    assert new == p and zipfile.is_zipfile(new)
    assert list((tmp_path / "recycle").rglob("Issue 003.cbz"))[0].read_bytes() == original


def test_tagging_converts_then_tags(app, tmp_path):
    _pages(tmp_path / "src")
    cbt = _bsdtar_create(tmp_path / "Private Dance 001 (2023).cbt", tmp_path / "src", "ustar")
    with app.state.session_factory() as db:
        s = Series(name="Private Dance", year_began=2023, metron_id=17753, match_status="matched",
                   confirmed_by="auto", locked_fields=[])  # fmt: skip
        i = Issue(
            series=s, number="1", metron_id=186450, metron_detail=metron_issue(186450), locked_fields=[]
        )
        f = File(issue=i, path=str(cbt))
        db.add_all([s, i, f])
        db.flush()
        preview = tag_file(i, f, dry_run=True)
        assert preview["fields"]["File"] == ["CBT", "converted to CBZ"]
        assert preview["fields"]["Series"] == ["From RAR", "Private Dance"]
        assert cbt.exists()
        result = tag_file(i, f, dry_run=False, recycle_dir=str(tmp_path / "recycle"))
        assert result["error"] is None
        assert f.path.endswith("Private Dance 001 (2023).cbz") and not cbt.exists()
        with zipfile.ZipFile(f.path) as zf:
            ci = zf.read("ComicInfo.xml")
            assert b"<Series>Private Dance</Series>" in ci and b"<Tags>Kept</Tags>" in ci
            assert "MetronInfo.xml" in zf.namelist()
        assert tag_file(i, f, dry_run=True)["fields"] == {}
