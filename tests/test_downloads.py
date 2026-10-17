import os
import shutil
import subprocess
import zipfile
from datetime import date

import pytest
import respx
from sqlalchemy import select

from fumora import settings_store
from fumora.auto_search import make_search_handler
from fumora.downloaders import QbittorrentClient, SabnzbdClient
from fumora.grabbing import check_downloads, grab
from fumora.models import Blocklist, File, Grab, Issue, Series
from fumora.search import Profile, evaluate
from tests.helpers import make_cbz
from tests.test_m2_routes_jobs import _job

pytestmark = pytest.mark.skipif(shutil.which("bsdtar") is None, reason="bsdtar not installed")

SAB = "http://sab.test"
QBIT = "http://qbit.test"
PROWLARR = "http://prowlarr.test"
SECRET_URL = "http://prowlarr.test/1/download?apikey=SECRET&link=x"


def _release(title, protocol="usenet", size=45, guid=None, seeders=50):
    return {"title": title, "size": size * 2**20, "protocol": protocol, "indexer": "NZBgeek",
            "guid": guid or title, "downloadUrl": SECRET_URL, "seeders": seeders,
            "publishDate": "2026-10-01T00:00:00Z"}  # fmt: skip


def _setup(app, tmp_path, numbers=("16",), cover=date(2026, 3, 1)):
    lib = tmp_path / "library"
    with app.state.session_factory() as db:
        for k, v in {"sabnzbd.url": SAB, "sabnzbd.api_key": "sabkey", "qbittorrent.url": QBIT,
                     "qbittorrent.username": "u", "qbittorrent.password": "p", "prowlarr.url": PROWLARR,
                     "prowlarr.api_key": "pk", "library.path": str(lib),
                     "library.recycle_path": str(tmp_path / "recycle")}.items():  # fmt: skip
            settings_store.put(db, k, v)
        s = Series(name="Absolute Batman", year_began=2024, series_type="Single Issue", monitored=True,
                   match_status="matched", confirmed_by="auto", metron_id=1, locked_fields=[],
                   path=str(lib / "Absolute Batman (2024)"))  # fmt: skip
        for n in numbers:
            db.add(Issue(series=s, number=n, cover_date=cover, monitored=True, locked_fields=[]))
        db.add(s)
        db.commit()
        return s.id


def _issue(db, number):
    return db.scalar(select(Issue).where(Issue.number == number))


def _cbr_download(folder, name):
    """A real 7z archive named .cbr, like a downloaded comic needing conversion."""
    src = folder / "pages"
    src.mkdir(parents=True)
    for n in range(3):
        (src / f"p{n}.jpg").write_bytes(b"\xff\xd8\xff" + bytes([n]) * 300)
    out = folder / name
    subprocess.run(["bsdtar", "-cf", str(out), "--format", "7zip", "-C", str(src), "."], check=True)
    shutil.rmtree(src)
    return out


@respx.mock
async def test_sab_client():
    respx.get(f"{SAB}/api", params={"mode": "addurl"}).respond(
        200, json={"status": True, "nzo_ids": ["SABnzbd_nzo_1"]}
    )
    respx.get(f"{SAB}/api", params={"mode": "queue"}).respond(200, json={"queue": {"slots": []}})
    respx.get(f"{SAB}/api", params={"mode": "history"}).respond(200, json={"history": {"slots": [
        {"nzo_id": "SABnzbd_nzo_1", "status": "Completed",
         "storage": "/data/usenet/complete/fumora/x"}]}})  # fmt: skip
    async with SabnzbdClient(SAB, "k") as sab:
        assert await sab.add(SECRET_URL, "Absolute Batman 016") == "SABnzbd_nzo_1"
        st = await sab.status("SABnzbd_nzo_1")
    assert (st.state, st.path) == ("completed", "/data/usenet/complete/fumora/x")
    add = respx.calls[0].request.url.params
    assert add["cat"] == "fumora" and add["name"] == SECRET_URL


@respx.mock
async def test_qbit_client():
    respx.post(f"{QBIT}/api/v2/auth/login").respond(200, text="Ok.")
    respx.post(f"{QBIT}/api/v2/torrents/add").respond(200, text="Ok.")
    respx.get(f"{QBIT}/api/v2/torrents/info").respond(200, json=[
        {"state": "stalledUP", "progress": 1.0, "content_path": "/data/torrents/fumora/x.cbz"}])  # fmt: skip
    async with QbittorrentClient(QBIT, "u", "p") as qb:
        assert await qb.add("magnet:?x", "fumora-7") == "fumora-7"
        st = await qb.status("fumora-7")
    assert (st.state, st.path) == ("completed", "/data/torrents/fumora/x.cbz")
    assert (
        b"tags=fumora-7" in respx.calls[1].request.content
        and b"category=fumora" in respx.calls[1].request.content
    )


@respx.mock
async def test_usenet_grab_then_import_moves_converts_names_and_tags(app, tmp_path, logged_in):
    _setup(app, tmp_path)
    dl = tmp_path / "usenet" / "fumora" / "Absolute.Batman.016"
    dl.mkdir(parents=True)
    _cbr_download(dl, "Absolute Batman 016 (2026) (Digital).cbr")
    (dl / "release.nfo").write_text("nfo")
    respx.get(f"{SAB}/api", params={"mode": "addurl"}).respond(
        200, json={"status": True, "nzo_ids": ["nzo_1"]}
    )
    respx.get(f"{SAB}/api", params={"mode": "queue"}).respond(200, json={"queue": {"slots": []}})
    respx.get(f"{SAB}/api", params={"mode": "history"}).respond(200, json={"history": {"slots": [
        {"nzo_id": "nzo_1", "status": "Completed", "storage": str(dl)}]}})  # fmt: skip
    sf = app.state.session_factory
    with sf() as db:
        issue = _issue(db, "16")
        c = evaluate(_release("Absolute Batman 016 [2026] [Digital] [Shan-Empire]"), issue, Profile())
        g = await grab(db, c, issue)
        db.commit()
        assert g.state == "downloading" and g.client_job_id == "nzo_1"
    assert "SECRET" not in logged_in.get("/queue").text
    assert await check_downloads(sf) == 1
    with sf() as db:
        g = db.scalar(select(Grab))
        assert g.state == "imported", g.message
        f = db.scalar(select(File))
        assert f.path.endswith("Absolute Batman (2024)/Absolute Batman 016 (2026).cbz")
        with zipfile.ZipFile(f.path) as zf:
            assert b"<Series>Absolute Batman</Series>" in zf.read("ComicInfo.xml")  # tagged on import
    assert not dl.exists()  # usenet download folder cleaned up (only junk was left)


@respx.mock
async def test_torrent_import_hardlinks_and_keeps_seeding(app, tmp_path):
    _setup(app, tmp_path)
    tdir = tmp_path / "torrents" / "fumora"
    tdir.mkdir(parents=True)
    src = make_cbz(tdir / "Absolute Batman #16.cbz")
    respx.post(f"{QBIT}/api/v2/auth/login").respond(200, text="Ok.")
    respx.post(f"{QBIT}/api/v2/torrents/add").respond(200, text="Ok.")
    respx.get(f"{QBIT}/api/v2/torrents/info").respond(200, json=[
        {"state": "uploading", "progress": 1.0, "content_path": str(src)}])  # fmt: skip
    sf = app.state.session_factory
    with sf() as db:
        settings_store.put(db, "import.tag", "off")
        issue = _issue(db, "16")
        await grab(db, evaluate(_release("Absolute Batman #16 by Snyder [ENG / CBZ]", "torrent"), issue,
                                Profile()), issue)  # fmt: skip
        db.commit()
    await check_downloads(sf)
    with sf() as db:
        f = db.scalar(select(File))
        assert os.path.exists(src), "the torrent's own file must stay for seeding"
        assert os.stat(f.path).st_ino == os.stat(src).st_ino  # a hardlink, not a copy


@respx.mock
async def test_pack_import_maps_files_and_never_overwrites(app, tmp_path):
    _setup(app, tmp_path, numbers=("1", "2", "3"), cover=date(2024, 12, 1))
    dl = tmp_path / "usenet" / "pack"
    dl.mkdir(parents=True)
    for n in (1, 2, 3):
        make_cbz(dl / f"Absolute Batman {n:03} (2024) (Digital).cbz")
    respx.get(f"{SAB}/api", params={"mode": "addurl"}).respond(200, json={"status": True, "nzo_ids": ["p"]})
    respx.get(f"{SAB}/api", params={"mode": "queue"}).respond(200, json={"queue": {"slots": []}})
    respx.get(f"{SAB}/api", params={"mode": "history"}).respond(200, json={"history": {"slots": [
        {"nzo_id": "p", "status": "Completed", "storage": str(dl)}]}})  # fmt: skip
    sf = app.state.session_factory
    with sf() as db:
        settings_store.put(db, "import.tag", "off")
        two = _issue(db, "2")
        existing = tmp_path / "existing-2.cbz"
        existing.write_bytes(b"mine")
        two.files = [File(path=str(existing))]
        one = _issue(db, "1")
        c = evaluate(_release("Absolute Batman 001-003 (2024) (Digital)", size=150), one, Profile())
        g = await grab(db, c, one)
        db.commit()
        assert len(g.issue_ids) == 3
    await check_downloads(sf)
    with sf() as db:
        g = db.scalar(select(Grab))
        assert g.state == "imported" and "#1, #3" in g.message and "#2 already has a file" in g.message
        assert existing.read_bytes() == b"mine"


@respx.mock
async def test_failed_download_is_blocklisted(app, tmp_path):
    _setup(app, tmp_path)
    respx.get(f"{SAB}/api", params={"mode": "addurl"}).respond(200, json={"status": True, "nzo_ids": ["f"]})
    respx.get(f"{SAB}/api", params={"mode": "queue"}).respond(200, json={"queue": {"slots": []}})
    respx.get(f"{SAB}/api", params={"mode": "history"}).respond(200, json={"history": {"slots": [
        {"nzo_id": "f", "status": "Failed", "fail_message": "Out of retention"}]}})  # fmt: skip
    sf = app.state.session_factory
    with sf() as db:
        issue = _issue(db, "16")
        await grab(db, evaluate(_release("Absolute Batman 016 (2026) (Digital)"), issue, Profile()), issue)
        db.commit()
    await check_downloads(sf)
    with sf() as db:
        assert db.scalar(select(Grab)).state == "failed"
        assert db.scalar(select(Blocklist)).reason == "Out of retention"


@respx.mock
async def test_wanted_search_grabs_best_and_skips_blocklisted_and_delayed(app, tmp_path):
    _setup(app, tmp_path, numbers=("16", "17"))
    sf = app.state.session_factory
    with sf() as db:
        _issue(db, "17").cover_date = date.today()  # just released
        settings_store.put(db, "search.delay_hours", "48")
        db.add(Blocklist(guid="best", title="x", reason="bad"))
        db.commit()
    respx.get(f"{PROWLARR}/api/v1/search").respond(200, json=[
        _release("Absolute Batman 016 [2026] [Digital] [Shan-Empire]", guid="best"),
        _release("Absolute Batman 016 [2026] [Webrip] [The Last Kryptonian-DCP]", guid="second"),
        _release("Absolute Batman 017 [2026] [Digital] [Shan-Empire]", guid="seventeen"),
    ])  # fmt: skip
    add = respx.get(f"{SAB}/api", params={"mode": "addurl"}).respond(
        200, json={"status": True, "nzo_ids": ["n"]}
    )
    summary = await make_search_handler()(_job(app, "search", {}))
    assert "grabbed 1" in summary
    assert add.call_count == 1
    with sf() as db:
        g = db.scalar(select(Grab))
        assert "Kryptonian" in g.release_title  # the best one is blocklisted; #17 is inside the delay
    # A second run doesn't grab #16 again while it's downloading.
    summary = await make_search_handler()(_job(app, "search", {}))
    assert add.call_count == 1


@respx.mock
def test_interactive_grab_uses_server_side_results(logged_in, app, tmp_path):
    _setup(app, tmp_path)
    respx.get(f"{PROWLARR}/api/v1/search").respond(
        200, json=[_release("Absolute Batman 016 [2026] [Digital]")]
    )
    respx.get(f"{SAB}/api", params={"mode": "addurl"}).respond(200, json={"status": True, "nzo_ids": ["n"]})
    with app.state.session_factory() as db:
        iid = _issue(db, "16").id
    html = logged_in.get(f"/issues/{iid}/search").text
    assert "SECRET" not in html and 'name="token"' in html
    token = html.split('name="token" value="')[1].split('"')[0]
    r = logged_in.post(f"/issues/{iid}/grab", data={"token": token, "index": "0"}, follow_redirects=False)
    assert r.status_code == 303
    assert logged_in.post(f"/issues/{iid}/grab", data={"token": "nope", "index": "0"}).status_code == 410
