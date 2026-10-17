"""Regression tests for the M4 part 2 code review."""

import errno
import os
import shutil

import httpx
import pytest
import respx
from sqlalchemy import select

from fumora import settings_store
from fumora.downloaders import DownloaderError, QbittorrentClient
from fumora.grabbing import check_downloads, grab
from fumora.models import File, Grab
from fumora.search import Profile, evaluate
from tests.helpers import make_cbz
from tests.test_downloads import QBIT, SAB, _issue, _release, _setup

pytestmark = pytest.mark.skipif(shutil.which("bsdtar") is None, reason="bsdtar not installed")


def _sab_done(storage, nzo="n"):
    respx.get(f"{SAB}/api", params={"mode": "addurl"}).respond(200, json={"status": True, "nzo_ids": [nzo]})
    respx.get(f"{SAB}/api", params={"mode": "queue"}).respond(200, json={"queue": {"slots": []}})
    respx.get(f"{SAB}/api", params={"mode": "history"}).respond(
        200, json={"history": {"slots": [{"nzo_id": nzo, "status": "Completed", "storage": str(storage)}]}}
    )


async def _grab16(app, title="Absolute Batman 016 (2026) (Digital)", protocol="usenet"):
    with app.state.session_factory() as db:
        issue = _issue(db, "16")
        g = await grab(db, evaluate(_release(title, protocol), issue, Profile()), issue)
        db.commit()
        return g.id


@respx.mock
async def test_refused_release_leaves_no_grab(app, tmp_path):
    _setup(app, tmp_path)
    respx.get(f"{SAB}/api", params={"mode": "addurl"}).respond(200, json={"status": False, "nzo_ids": []})
    with app.state.session_factory() as db:
        issue = _issue(db, "16")
        with pytest.raises(DownloaderError):
            await grab(
                db, evaluate(_release("Absolute Batman 016 (2026) (Digital)"), issue, Profile()), issue
            )
        db.commit()
        assert db.scalar(select(Grab)) is None


@respx.mock
async def test_different_mounts_fail_loudly_without_copying(app, tmp_path, monkeypatch):
    _setup(app, tmp_path)
    dl = tmp_path / "usenet" / "fumora" / "Job"
    dl.mkdir(parents=True)
    src = make_cbz(dl / "Absolute Batman 016 (2026).cbz")
    _sab_done(dl)

    def exdev(a, b):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr("fumora.grabbing.os.rename", exdev)
    await _grab16(app)
    await check_downloads(app.state.session_factory)
    with app.state.session_factory() as db:
        g = db.scalar(select(Grab))
        assert g.state == "import_failed" and "different mounts" in g.message
        assert db.scalar(select(File)) is None
    assert src.exists()


@respx.mock
async def test_failed_conversion_leaves_download_and_library_untouched(app, tmp_path):
    sid = _setup(app, tmp_path)
    dl = tmp_path / "usenet" / "fumora" / "Job"
    dl.mkdir(parents=True)
    bad = dl / "Absolute Batman 016 (2026).cbr"
    bad.write_bytes(b"Rar!\x1a\x07\x00 this is not a real archive")
    _sab_done(dl)
    await _grab16(app)
    await check_downloads(app.state.session_factory)
    with app.state.session_factory() as db:
        assert db.scalar(select(Grab)).state == "import_failed"
        assert db.scalar(select(File)) is None
        from fumora.models import Series

        folder = db.get(Series, sid).path
    assert bad.exists()  # the download is still where SABnzbd put it
    leftovers = os.listdir(folder) if os.path.isdir(folder) else []
    assert leftovers == []  # no half-written CBZ and no temporary copy in the library


@respx.mock
async def test_single_file_download_never_removes_its_folder(app, tmp_path):
    _setup(app, tmp_path)
    category = tmp_path / "usenet" / "complete" / "fumora"
    category.mkdir(parents=True)
    other = category / "someone-elses-job.nfo"
    other.write_text("keep me")
    single = make_cbz(category / "Absolute Batman 016 (2026).cbz")
    _sab_done(single)
    with app.state.session_factory() as db:
        settings_store.put(db, "import.tag", "off")
        db.commit()
    await _grab16(app)
    await check_downloads(app.state.session_factory)
    assert category.is_dir() and other.exists()


@respx.mock
async def test_interrupted_import_is_resumed(app, tmp_path):
    _setup(app, tmp_path)
    dl = tmp_path / "usenet" / "fumora" / "Job"
    dl.mkdir(parents=True)
    make_cbz(dl / "Absolute Batman 016 (2026).cbz")
    _sab_done(dl)
    gid = await _grab16(app)
    with app.state.session_factory() as db:
        settings_store.put(db, "import.tag", "off")
        g = db.get(Grab, gid)
        g.state, g.path = "importing", str(dl)  # as if the app restarted mid-import
        db.commit()
    await check_downloads(app.state.session_factory)
    with app.state.session_factory() as db:
        assert db.get(Grab, gid).state == "imported"


@respx.mock
async def test_torrent_import_of_a_tagged_series_stays_a_hardlink(app, tmp_path):
    _setup(app, tmp_path)  # confirmed series: usenet imports would be tagged
    tdir = tmp_path / "torrents" / "fumora"
    tdir.mkdir(parents=True)
    src = make_cbz(tdir / "Absolute Batman #16.cbz")
    respx.post(f"{QBIT}/api/v2/auth/login").respond(200, text="Ok.")
    respx.post(f"{QBIT}/api/v2/torrents/add").respond(200, text="Ok.")
    respx.get(f"{QBIT}/api/v2/torrents/info").respond(
        200, json=[{"state": "uploading", "progress": 1.0, "content_path": str(src)}]
    )
    await _grab16(app, "Absolute Batman #16 by Snyder [ENG / CBZ]", "torrent")
    await check_downloads(app.state.session_factory)
    with app.state.session_factory() as db:
        f = db.scalar(select(File))
        assert os.stat(f.path).st_ino == os.stat(src).st_ino


@respx.mock
def test_double_grab_is_refused(logged_in, app, tmp_path):
    _setup(app, tmp_path)
    respx.get("http://prowlarr.test/api/v1/search").respond(
        200,
        json=[
            _release("Absolute Batman 016 [2026] [Digital]"),
            _release("Absolute Batman 016 (2026) (Webrip)"),
        ],
    )
    respx.get(f"{SAB}/api", params={"mode": "addurl"}).respond(200, json={"status": True, "nzo_ids": ["n"]})
    with app.state.session_factory() as db:
        iid = _issue(db, "16").id
    html = logged_in.get(f"/issues/{iid}/search").text
    token = html.split('name="token" value="')[1].split('"')[0]
    assert (
        logged_in.post(
            f"/issues/{iid}/grab", data={"token": token, "index": "0"}, follow_redirects=False
        ).status_code
        == 303
    )
    assert logged_in.post(f"/issues/{iid}/grab", data={"token": token, "index": "1"}).status_code == 409


@respx.mock
async def test_qbittorrent_down_is_a_downloader_error():
    respx.post(f"{QBIT}/api/v2/auth/login").respond(200, text="Ok.")
    respx.get(f"{QBIT}/api/v2/torrents/info").mock(side_effect=httpx.ConnectError("down"))
    async with QbittorrentClient(QBIT, "u", "p") as qb:
        with pytest.raises(DownloaderError):
            await qb.status("fumora-x")
