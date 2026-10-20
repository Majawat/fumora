"""GetComics (own indexer + downloader) and Shelfmark (Anna's Archive hand-off)."""

import asyncio
import io
import json
import shutil
import zipfile

import httpx
import pytest
import respx

from fumora import ddl, getcomics, settings_store
from fumora.auto_search import make_search_handler
from fumora.ddl import DirectClient
from fumora.getcomics import GetComicsClient, GetComicsError, Post, parse_post, parse_search, query_text
from fumora.grabbing import check_downloads, grab
from fumora.indexers import Indexers
from fumora.models import File, Grab
from fumora.search import Profile, evaluate, wants
from fumora.shelfmark import ShelfmarkClient, to_release
from tests.helpers import FIXTURES, make_cbz
from tests.test_downloads import _issue, _setup
from tests.test_m2_routes_jobs import _job

pytestmark = pytest.mark.skipif(shutil.which("bsdtar") is None, reason="bsdtar not installed")

GC = "https://getcomics.org"
POST_24 = f"{GC}/dc/absolute-batman-24-2026/"
POST_TPB = f"{GC}/dc/absolute-batman-vol-3-devils-workshop-tpb-2026/"
SM = "http://shelfmark.test"


def _page(name: str) -> str:
    return (FIXTURES / "getcomics" / name).read_text()


@pytest.fixture(autouse=True)
def _no_pacing(monkeypatch):
    monkeypatch.setattr(getcomics, "PACE", 0)


def _cbz_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for n in range(3):
            zf.writestr(f"p{n:03}.jpg", b"\xff\xd8\xff" + bytes([n]) * 400)
    return buf.getvalue()


async def _finish_downloads():
    while ddl._tasks:
        await asyncio.gather(*list(ddl._tasks.values()))


# --- parsing -------------------------------------------------------------------


def test_parse_search_lists_posts_with_size_and_date():
    posts = parse_search(_page("search.html"))
    assert len(posts) == 12
    p = next(p for p in posts if p.url == POST_24)
    assert (p.title, p.size_mb, p.date) == ("Absolute Batman #24 (2026)", 47.0, "2026-09-23")
    assert posts[0].title == "Absolute Batman Vol. 3 – Devil’s Workshop (TPB) (2026)"


def test_parse_post_issue_uses_file_name_and_supported_hosts():
    [rel] = parse_post(
        _page("post_issue.html"), Post("Absolute Batman #24 (2026)", POST_24, 47, "2026-09-23")
    )
    assert rel["title"] == "Absolute Batman 024 (2026) (Digital) (Shan-Empire).cbz"
    assert (rel["protocol"], rel["client"], rel["guid"]) == ("ddl", "getcomics", f"{POST_24}#0")
    assert rel["size"] == 47 * 2**20 and rel["publishDate"] == "2026-09-23T00:00:00Z"
    spec = json.loads(rel["downloadUrl"])
    assert [h for h, _ in spec["links"]] == ["main", "pixeldrain"]  # TeraBox etc. skipped
    assert spec["post"] == POST_24 and spec["group"] == 0


def test_parse_post_with_two_releases():
    rels = parse_post(_page("post_tpb.html"), Post("x", POST_TPB, None, "2026-10-08"))
    assert [r["size"] // 2**20 for r in rels] == [762, 254]
    assert "Son of Ultron-Empire" in rels[0]["title"] and "LeDuch-DCP" in rels[1]["title"]
    assert [h for h, _ in json.loads(rels[0]["downloadUrl"])["links"]] == ["pixeldrain"]
    assert rels[1]["guid"] == f"{POST_TPB}#1"


def test_query_text_drops_zero_padding():
    assert query_text("Absolute Batman 024") == "Absolute Batman 24"
    assert query_text("Spider-Man 2099 001") == "Spider-Man 2099 1"


def test_en_dash_range_is_a_pack():
    from fumora.releases import parse_release

    r = parse_release("Absolute Batman #20 – 23 (2026)")
    assert (r.kind, r.range_start, r.range_end) == ("range", 20, 23)


# --- GetComics client ------------------------------------------------------------


@respx.mock
async def test_search_opens_only_wanted_posts(app, tmp_path):
    respx.get(f"{GC}/", params={"s": "Absolute Batman"}).respond(200, text=_page("search.html"))
    post = respx.get(POST_24).respond(200, text=_page("post_issue.html"))
    other = respx.get(url__startswith=f"{GC}/dc/").respond(404)
    _setup(app, tmp_path, numbers=("24",))
    with app.state.session_factory() as db:
        issue = _issue(db, "24")
        async with GetComicsClient() as gc:
            releases = await gc.search("Absolute Batman", wants([issue]))
        assert post.call_count == 1 and other.call_count == 0
        c = evaluate(releases[0], issue, Profile())
        assert c.accepted and c.parsed.source == "digital" and c.protocol == "ddl"


@respx.mock
async def test_cloudflare_challenge_needs_solver():
    respx.get(f"{GC}/").respond(503, text="<title>Just a moment...</title>")
    async with GetComicsClient() as gc:
        with pytest.raises(GetComicsError, match="FlareSolverr"):
            await gc.search_posts("x")


@respx.mock
async def test_cloudflare_challenge_solved_by_byparr():
    respx.get(f"{GC}/").respond(503, text="<title>Just a moment...</title>")
    solver = respx.post("http://byparr.test/v1").respond(200, json={"status": "ok", "solution": {
        "response": _page("search.html"), "userAgent": "UA",
        "cookies": [{"name": "cf_clearance", "value": "c", "domain": ".getcomics.org"}]}})  # fmt: skip
    async with GetComicsClient(flaresolverr_url="http://byparr.test") as gc:
        posts = await gc.search_posts("Absolute Batman")
    assert len(posts) == 12 and json.loads(solver.calls[0].request.content)["cmd"] == "request.get"


# --- Direct downloads --------------------------------------------------------------


def _spec(*links):
    return json.dumps({"post": POST_24, "group": 0, "links": [list(x) for x in links]})


@respx.mock
async def test_direct_download_follows_redirect_and_saves_file(tmp_path):
    respx.get(f"{GC}/dls/main").respond(
        302, headers={"location": "https://fs3.comicfiles.ru/a/Absolute%20Batman%20024.cbz"}
    )
    respx.get("https://fs3.comicfiles.ru/a/Absolute%20Batman%20024.cbz").respond(
        200, content=_cbz_bytes(), headers={"content-type": "application/octet-stream"}
    )
    client = DirectClient(str(tmp_path / "ddl"))
    job = await client.add(_spec(("main", f"{GC}/dls/main")), "Absolute Batman 024")
    await _finish_downloads()
    st = await client.status(job)
    assert st.state == "completed"
    assert [f.name for f in (tmp_path / "ddl" / job).iterdir()] == ["Absolute Batman 024.cbz"]


@respx.mock
async def test_direct_download_falls_back_to_pixeldrain(tmp_path):
    respx.get(f"{GC}/dls/main").respond(200, text="<html>gone</html>", headers={"content-type": "text/html"})
    respx.get(f"{GC}/dls/pd").respond(302, headers={"location": "https://pixeldrain.com/u/abc123"})
    pd = respx.get("https://pixeldrain.com/api/file/abc123").respond(
        200, content=_cbz_bytes(), headers={"content-disposition": 'inline; filename="AB 024 (2026).cbz"'}
    )
    client = DirectClient(str(tmp_path / "ddl"))
    job = await client.add(_spec(("main", f"{GC}/dls/main"), ("pixeldrain", f"{GC}/dls/pd")), "x")
    await _finish_downloads()
    assert (await client.status(job)).state == "completed" and pd.called
    assert (tmp_path / "ddl" / job / "AB 024 (2026).cbz").exists()


@respx.mock
async def test_direct_download_rereads_post_when_links_expire(tmp_path):
    respx.get(f"{GC}/dls/old").respond(404)
    respx.get(POST_24).respond(200, text=_page("post_issue.html"))
    fresh = respx.get(url__startswith=f"{GC}/dls/PrA0Ep").respond(
        200, content=_cbz_bytes(), headers={"content-disposition": 'attachment; filename="AB.cbz"'}
    )
    client = DirectClient(str(tmp_path / "ddl"))
    job = await client.add(_spec(("main", f"{GC}/dls/old")), "x")
    await _finish_downloads()
    assert (await client.status(job)).state == "completed" and fresh.called


@respx.mock
async def test_direct_download_failure_is_reported(tmp_path):
    respx.get(f"{GC}/dls/main").respond(500)
    respx.get(POST_24).respond(404)
    client = DirectClient(str(tmp_path / "ddl"))
    job = await client.add(_spec(("main", f"{GC}/dls/main")), "x")
    await _finish_downloads()
    st = await client.status(job)
    assert st.state == "failed" and "HTTP 500" in st.message


@respx.mock
async def test_pack_of_comics_is_unpacked(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Absolute Batman 020 (2026).cbz", _cbz_bytes())
        zf.writestr("Absolute Batman 021 (2026).cbz", _cbz_bytes())
    respx.get(f"{GC}/dls/pack").respond(
        200, content=buf.getvalue(), headers={"content-disposition": 'attachment; filename="AB 20-21.zip"'}
    )
    client = DirectClient(str(tmp_path / "ddl"))
    job = await client.add(_spec(("main", f"{GC}/dls/pack")), "x")
    await _finish_downloads()
    names = sorted(f.name for f in (tmp_path / "ddl" / job).iterdir())
    assert names == ["Absolute Batman 020 (2026).cbz", "Absolute Batman 021 (2026).cbz"]


async def test_restart_reports_missing(tmp_path):
    st = await DirectClient(str(tmp_path)).status("ddl-unknown")
    assert st.state == "missing"


# --- grab -> download -> import ---------------------------------------------------------


def _gc_settings(app, tmp_path):
    with app.state.session_factory() as db:
        settings_store.put(db, "getcomics.enabled", "on")
        settings_store.put(db, "ddl.path", str(tmp_path / "downloads" / "fumora"))
        db.commit()


@respx.mock
async def test_getcomics_grab_downloads_and_imports(app, tmp_path, logged_in):
    _setup(app, tmp_path, numbers=("24",))
    _gc_settings(app, tmp_path)
    respx.get(url__startswith=f"{GC}/dls/").respond(
        200, content=_cbz_bytes(), headers={"content-disposition": 'attachment; filename="AB 024.cbz"'}
    )
    sf = app.state.session_factory
    [rel] = parse_post(_page("post_issue.html"), Post("x", POST_24, 47, "2026-09-23"))
    with sf() as db:
        issue = _issue(db, "24")
        g = await grab(db, evaluate(rel, issue, Profile(min_mb=0)), issue)
        db.commit()
        assert (g.client, g.protocol) == ("getcomics", "ddl")
    await _finish_downloads()
    await check_downloads(sf)
    with sf() as db:
        g = db.query(Grab).one()
        assert g.state == "imported", g.message
        [f] = db.query(File).all()
        assert f.path.endswith("Absolute Batman (2024)/Absolute Batman 024 (2026).cbz")
    assert not any((tmp_path / "downloads" / "fumora").iterdir())  # job folder cleaned up


@respx.mock
async def test_getcomics_job_restarts_after_fumora_restart(app, tmp_path, logged_in):
    _setup(app, tmp_path, numbers=("24",))
    _gc_settings(app, tmp_path)
    respx.get(url__startswith=f"{GC}/dls/").respond(
        200, content=_cbz_bytes(), headers={"content-disposition": 'attachment; filename="AB 024.cbz"'}
    )
    sf = app.state.session_factory
    [rel] = parse_post(_page("post_issue.html"), Post("x", POST_24, 47, "2026-09-23"))
    with sf() as db:
        issue = _issue(db, "24")
        g = await grab(db, evaluate(rel, issue, Profile(min_mb=0)), issue)
        db.commit()
        job = g.client_job_id
    await _finish_downloads()
    ddl._jobs.pop(job)  # what a restart looks like
    await check_downloads(sf)
    with sf() as db:
        assert db.query(Grab).one().message == "Restarted after Fumora restarted"
    await _finish_downloads()
    await check_downloads(sf)
    with sf() as db:
        assert db.query(Grab).one().state == "imported"


# --- Shelfmark ------------------------------------------------------------------------

SM_RELEASE = {"source": "direct_download", "source_id": "0123abcd", "title": "Absolute Batman Vol. 3",
              "format": "cbz", "size": "254.0MB", "size_bytes": None, "extra": {"year": "2026"},
              "info_url": "https://annas-archive.org/md5/0123abcd"}  # fmt: skip


def test_shelfmark_release_shape():
    r = to_release(SM_RELEASE)
    assert r["title"] == "Absolute Batman Vol. 3 (2026).cbz"
    assert (r["protocol"], r["client"], r["indexer"]) == ("ddl", "shelfmark", "Anna's Archive")
    assert r["size"] == 254 * 2**20 and r["guid"] == "shelfmark:0123abcd"
    assert json.loads(r["downloadUrl"])["source_id"] == "0123abcd"
    epub = to_release({**SM_RELEASE, "format": "epub"})
    from fumora.releases import parse_release

    assert parse_release(epub["title"]).ebook


@respx.mock
async def test_shelfmark_search_no_results_and_errors():
    route = respx.get(f"{SM}/api/releases")
    route.side_effect = [
        httpx.Response(503, json={"error": "No books found. Please try another query."}),
        httpx.Response(200, json={"releases": [SM_RELEASE]}),
    ]
    async with ShelfmarkClient(SM, "key") as sm:
        found = await sm.search("Absolute Batman", ("direct_download", "libgen"))
    assert [r["guid"] for r in found] == ["shelfmark:0123abcd"]
    assert route.calls[0].request.headers["X-Api-Key"] == "key"
    assert route.calls[0].request.url.params["source"] == "direct_download"


@respx.mock
async def test_shelfmark_grab_status_and_import(app, tmp_path, logged_in):
    _setup(app, tmp_path, numbers=("24",))
    with app.state.session_factory() as db:
        settings_store.put(db, "shelfmark.url", SM)
        db.commit()
    done = tmp_path / "Shelfmark" / "Absolute Batman - 24 (2026).cbz"
    done.parent.mkdir()
    make_cbz(done)
    queued = respx.post(f"{SM}/api/releases/download").respond(200, json={"status": "queued"})
    respx.get(f"{SM}/api/status").side_effect = [
        httpx.Response(200, json={"downloading": {"abc": {"progress": 40}}, "complete": {}}),
        httpx.Response(200, json={"complete": {"abc": {"download_path": str(done)}}}),
    ]
    sf = app.state.session_factory
    rel = to_release({**SM_RELEASE, "source_id": "abc", "title": "Absolute Batman #24", "size": "40 MB"})
    with sf() as db:
        issue = _issue(db, "24")
        c = evaluate(rel, issue, Profile())
        assert c.accepted, c.rejections
        g = await grab(db, c, issue)
        db.commit()
        assert (g.client, g.client_job_id) == ("shelfmark", "abc")
    assert json.loads(queued.calls[0].request.content)["source_id"] == "abc"
    await check_downloads(sf)
    with sf() as db:
        assert db.query(Grab).one().progress == 40
    await check_downloads(sf)
    with sf() as db:
        g = db.query(Grab).one()
        assert g.state == "imported", g.message
    assert not done.exists()


# --- all sources together -------------------------------------------------------------------


@respx.mock
async def test_one_failing_source_does_not_hide_the_others(app, tmp_path):
    _setup(app, tmp_path, numbers=("24",))
    _gc_settings(app, tmp_path)
    with app.state.session_factory() as db:
        settings_store.put(db, "shelfmark.url", SM)
        db.commit()
        respx.get("http://prowlarr.test/api/v1/search").respond(500)
        respx.get(f"{GC}/", params={"s": "Absolute Batman"}).respond(200, text=_page("search.html"))
        respx.get(POST_24).respond(200, text=_page("post_issue.html"))
        respx.get(f"{SM}/api/releases").mock(side_effect=httpx.ConnectError("down"))
        async with Indexers(db) as idx:
            assert idx.names == ["Prowlarr", "GetComics", "Shelfmark"]
            found, errors = await idx.search("Absolute Batman", wants([_issue(db, "24")]))
    assert [r["indexer"] for r in found] == ["GetComics"]
    assert len(errors) == 2 and errors[0].startswith("Prowlarr:") and errors[1].startswith("Shelfmark:")


@respx.mock
async def test_auto_search_grabs_from_getcomics_without_prowlarr(app, tmp_path, logged_in):
    _setup(app, tmp_path, numbers=("24",))
    _gc_settings(app, tmp_path)
    with app.state.session_factory() as db:
        settings_store.put(db, "prowlarr.url", None)
        db.commit()
    respx.get(f"{GC}/", params={"s": "Absolute Batman"}).respond(200, text=_page("search.html"))
    respx.get(POST_24).respond(200, text=_page("post_issue.html"))
    respx.get(url__startswith=f"{GC}/dls/").respond(
        200, content=_cbz_bytes(), headers={"content-disposition": 'attachment; filename="AB 024.cbz"'}
    )
    summary = await make_search_handler()(_job(app, "search", {}))
    assert "grabbed 1" in summary
    await _finish_downloads()
    with app.state.session_factory() as db:
        assert db.query(Grab).one().client == "getcomics"


@respx.mock
async def test_cancel_stops_a_direct_download(tmp_path):
    gate = asyncio.Event()

    async def slow(request):
        await gate.wait()
        return httpx.Response(200, content=_cbz_bytes())

    respx.get(f"{GC}/dls/main").mock(side_effect=slow)
    client = DirectClient(str(tmp_path / "ddl"))
    job = await client.add(_spec(("main", f"{GC}/dls/main")), "x")
    await asyncio.sleep(0.05)
    task = ddl._tasks[job]
    client.cancel(job)
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled() and job not in ddl._tasks and job not in ddl._jobs
    assert not (tmp_path / "ddl" / job).exists()


def test_remove_and_retry_cancel_direct_downloads(app, logged_in, monkeypatch):
    cancelled = []
    monkeypatch.setattr(DirectClient, "cancel", lambda self, job: cancelled.append(job))
    with app.state.session_factory() as db:
        for n, client in enumerate(("getcomics", "sabnzbd", "getcomics")):
            db.add(Grab(indexer="GetComics", release_title=f"r{n}", protocol="ddl", client=client,
                        client_job_id=f"job{n}", state="downloading", guid=f"g{n}"))  # fmt: skip
        db.commit()
    assert logged_in.post("/queue/1/remove", follow_redirects=False).status_code == 303
    assert logged_in.post("/queue/2/remove", follow_redirects=False).status_code == 303
    logged_in.post("/queue/3/retry", follow_redirects=False)
    assert cancelled == ["job0", "job2"]


def test_migration_adds_ddl_to_a_saved_protocol_list(tmp_path):
    from alembic import command
    from alembic.config import Config as AlembicConfig
    from sqlalchemy import create_engine, text

    from fumora.db import MIGRATIONS_DIR

    url = f"sqlite:///{tmp_path / 'm.db'}"
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "0006")
    eng = create_engine(url)
    with eng.begin() as c:
        c.execute(text("INSERT INTO setting (key, value) VALUES ('quality.protocols', 'torrent, usenet')"))
    command.upgrade(cfg, "head")
    with eng.connect() as c:
        assert c.execute(text("SELECT value FROM setting")).scalar() == "torrent, usenet, ddl"


def test_query_text_keeps_decimals():
    assert query_text("Batman 1.05") == "Batman 1.05"


def test_refetch_finds_group_by_guid_when_earlier_groups_are_skipped():
    """A post whose first release has no usable host: group numbers still line up."""
    page = _page("post_tpb.html")
    rels = parse_post(page, Post("x", POST_TPB))
    # The fixture's first group has only Pixeldrain; drop it so the first group has no usable host.
    page = page.replace("PIXELDRAIN", "MEGA", 1)
    rels2 = parse_post(page, Post("x", POST_TPB))
    assert [r["guid"] for r in rels2] == [rels[1]["guid"]] == [f"{POST_TPB}#1"]


@respx.mock
async def test_fresh_links_match_the_grabbed_group(tmp_path):
    respx.get(POST_TPB).respond(200, text=_page("post_tpb.html").replace("PIXELDRAIN", "MEGA", 1))
    links = await DirectClient(str(tmp_path))._fresh_links({"post": POST_TPB, "group": 1})
    assert [h for h, _ in links] == ["main", "pixeldrain"]


@respx.mock
async def test_converted_direct_download_leaves_no_original(app, tmp_path, logged_in):
    from tests.test_downloads import _cbr_download

    _setup(app, tmp_path, numbers=("24",))
    _gc_settings(app, tmp_path)
    sf = app.state.session_factory
    with sf() as db:
        g = Grab(issue_id=_issue(db, "24").id, issue_ids=[_issue(db, "24").id], indexer="GetComics",
                 release_title="AB 024", protocol="ddl", client="getcomics", client_job_id="ddl-x",
                 state="importing")  # fmt: skip
        folder = tmp_path / "downloads" / "fumora" / "ddl-x"
        folder.mkdir(parents=True)
        _cbr_download(folder, "Absolute Batman 024 (2026).cbr")
        g.path = str(folder)
        db.add(g)
        db.commit()
    await check_downloads(sf)
    with sf() as db:
        assert db.query(Grab).one().state == "imported"
    assert not folder.exists()


def test_shelfmark_status_odd_shape_is_a_downloader_error():
    from fumora.downloaders import DownloaderError

    async def run():
        with respx.mock:
            respx.get(f"{SM}/api/status").respond(200, json={"queued": ["x"], "complete": None})
            async with ShelfmarkClient(SM) as sm:
                assert (await sm.status("abc")).state == "missing"
            respx.get(f"{SM}/api/status").respond(200, json=["x"])
            async with ShelfmarkClient(SM) as sm:
                with pytest.raises(DownloaderError):
                    await sm.status("abc")

    asyncio.run(run())


@respx.mock
async def test_direct_download_gets_cloudflare_clearance_for_main_server(tmp_path):
    challenge = httpx.Response(
        503, text="<title>Just a moment...</title>", headers={"content-type": "text/html"}
    )
    dls = respx.get(f"{GC}/dls/main")
    dls.side_effect = [challenge, httpx.Response(200, content=_cbz_bytes(),
                       headers={"content-disposition": 'attachment; filename="AB.cbz"'})]  # fmt: skip
    respx.post("http://byparr.test/v1").respond(200, json={"status": "ok", "solution": {
        "response": "<html></html>", "userAgent": "UA-solved",
        "cookies": [{"name": "cf_clearance", "value": "ok", "domain": "getcomics.org"}]}})  # fmt: skip
    client = DirectClient(str(tmp_path / "ddl"), flaresolverr_url="http://byparr.test")
    job = await client.add(_spec(("main", f"{GC}/dls/main")), "x")
    await _finish_downloads()
    assert (await client.status(job)).state == "completed"
    retry = dls.calls[1].request
    assert "cf_clearance=ok" in retry.headers["cookie"] and retry.headers["user-agent"] == "UA-solved"


async def test_cancel_while_unpacking_waits_for_the_thread(tmp_path, monkeypatch):
    import threading

    release = threading.Event()
    started = threading.Event()

    def slow_unpack(path):
        started.set()
        release.wait(5)

    monkeypatch.setattr(ddl, "_unpack", slow_unpack)
    folder = tmp_path / "ddl"
    client = DirectClient(str(folder))

    async def fake_links(self, job_id, f, links, page=None):
        (f / "AB.zip").write_bytes(b"x")
        return []

    monkeypatch.setattr(DirectClient, "_try_links", fake_links)
    job = await client.add(_spec(("main", "x")), "x")
    await asyncio.to_thread(started.wait, 5)
    client.cancel(job)
    assert (folder / job).exists()  # not deleted under the unpack thread
    release.set()
    await _finish_downloads()
    assert not (folder / job).exists() and (await client.status(job)).state == "missing"
