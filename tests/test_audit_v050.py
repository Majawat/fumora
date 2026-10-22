"""Regressions for the independent (Gemini) audit of v0.5.0, one test per real finding."""

import os
import re
import shutil
import zipfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import select

from fumora import naming, settings_store
from fumora.auto_search import make_search_handler
from fumora.ddl import _unpack
from fumora.grabbing import APPROVAL, ImportFailed, _place_converted, place_issue_file
from fumora.importer import unmatch_series
from fumora.library_jobs import RETAG, make_retag_handler, naming_settings
from fumora.metron import BASE_URL, MetronClient
from fumora.models import Event, File, Grab, Issue, Release, Series
from fumora.perms import FILE_MODE
from fumora.refresh import rolling_selection, update_releases
from fumora.sources import apply_cv_issues
from tests.helpers import make_cbz
from tests.test_downloads import SAB, _cbr_download, _release, _setup
from tests.test_m2_routes_jobs import _job
from tests.test_m3_refresh import client, page
from tests.test_m5b_sources import cv_issue
from tests.test_review_29_gemini import _rar_like

pytestmark = pytest.mark.skipif(shutil.which("bsdtar") is None, reason="bsdtar not installed")

FUMORA = Path(__file__).parents[1] / "fumora"


def _abs(db, number="17"):
    return db.scalar(select(Issue).where(Issue.number == number))


def _lib(app, tmp_path, numbers=("17",)):
    _setup(app, tmp_path, numbers=numbers)
    with app.state.session_factory() as db:
        settings_store.put(db, "import.tag", "off")
        db.commit()
    folder = tmp_path / "library" / "Absolute Batman (2024)"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def test_1_converting_a_library_file_in_place_recycles_the_original(app, tmp_path):
    folder = _lib(app, tmp_path)
    original = _rar_like(folder / "Absolute Batman 017 (2026).cbz", {"p1.jpg": b"\xff\xd8\xff" + b"x" * 300})
    raw = original.read_bytes()
    with app.state.session_factory() as db:
        issue = _abs(db)
        row, problem = place_issue_file(db, issue.series, issue, original, False, naming_settings(db))
        assert problem is None and row.path == str(original)
    assert zipfile.is_zipfile(original)  # converted where it was, not deleted
    recycled = list((tmp_path / "recycle").rglob("*.cbz"))
    assert len(recycled) == 1 and recycled[0].read_bytes() == raw  # the original is kept


def test_2_a_converted_download_obeys_the_same_mount_rule(tmp_path, monkeypatch):
    src = _cbr_download(tmp_path / "dl", "x.cbr")
    dest = tmp_path / "lib" / "x.cbr"
    dest.parent.mkdir()
    real_stat = Path.stat

    def fake_stat(self, *a, **k):
        st = real_stat(self, *a, **k)
        if self == src:
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev + 1, *tuple(st)[3:]))
        return st

    monkeypatch.setattr(Path, "stat", fake_stat)
    with pytest.raises(ImportFailed, match="different mounts"):
        _place_converted(src, dest)


def test_3_organize_never_nests_a_series_deeper(app, tmp_path):
    lib = tmp_path / "lib"
    with app.state.session_factory() as db:
        from tests.test_scanner_naming import _series

        s = _series(db, lib / "Dark Horse" / "Umbrella Academy Dallas (2008)")
        plan = naming.plan_series(
            s, folder_template="{Publisher}/{Series Title} ({Series Year})", library_root=str(lib)
        )
        assert plan.folder is None  # already where the template puts it


def test_4_an_unlinked_row_at_the_destination_is_reused(app, tmp_path):
    folder = _lib(app, tmp_path)
    dest = folder / "Absolute Batman 017 (2026).cbz"
    with app.state.session_factory() as db:
        db.add(File(path=str(dest)))  # scanned earlier; the file was then removed outside Fumora
        db.commit()
        src = make_cbz(tmp_path / "Absolute Batman 017.cbz")
        issue = _abs(db)
        row, problem = place_issue_file(db, issue.series, issue, src, False, naming_settings(db))
        assert problem is None and row.path == str(dest)
        assert len(db.scalars(select(File)).all()) == 1


@pytest.mark.parametrize("status", ["review", "accepted", "pending"])
def test_5_a_series_awaiting_its_match_is_not_written(status):
    assert not Series(name="x", match_status=status).writable
    assert Series(name="x", match_status="not_found").writable
    assert not Series(name="x", match_status="matched").writable
    assert Series(name="x", match_status="matched", confirmed_by="auto").writable


def test_6_an_unreadable_pack_is_an_error_not_a_comic(tmp_path):
    from fumora.cbz import ArchiveError

    bad = tmp_path / "job" / "pack.zip"
    bad.parent.mkdir()
    bad.write_bytes(b"PK\x03\x04" + b"\x00" * 64)  # looks like a zip, isn't one
    with pytest.raises(ArchiveError):
        _unpack(bad)
    assert bad.exists() and not bad.with_suffix(".cbz").exists()


async def test_7_retag_leaves_hardlinked_torrent_files_alone(app, tmp_path):
    folder = _lib(app, tmp_path)
    seeding = make_cbz(tmp_path / "seeding.cbz")
    linked = folder / "Absolute Batman 017 (2026).cbz"
    os.link(seeding, linked)
    with app.state.session_factory() as db:
        settings_store.put(db, "metron.api_token", "t")
        _abs(db).files = [File(path=str(linked))]
        db.commit()
    msg = await make_retag_handler(app.state.make_metron_client)(_job(app, RETAG, {"dry_run": False}))
    assert "1 left alone (hardlinked" in msg
    assert os.stat(seeding).st_ino == os.stat(linked).st_ino


def test_8_converted_files_get_fumoras_permissions(tmp_path):
    from fumora.convert import convert_to_cbz

    src = _cbr_download(tmp_path / "x", "a.cbr")
    os.chmod(src, 0o600)
    assert (os.stat(convert_to_cbz(src, tmp_path / "recycle")).st_mode & 0o777) == FILE_MODE


def test_9_a_second_comicvine_issue_with_the_same_number_changes_nothing(app):
    with app.state.session_factory() as db:
        s = Series(name="Mieruko-chan", source="cv", cv_id=1, locked_fields=[])
        db.add(s)
        db.flush()
        apply_cv_issues(db, s, [cv_issue(1, "2020-01-01", cid=100)])
        apply_cv_issues(db, s, [cv_issue(1, "2021-06-01", cid=200) | {"name": "Variant"}])
        i = s.issues[0]
        assert (i.cv_id, i.title, i.cover_date) == (100, "Volume 1", date(2020, 1, 1))


def test_10_unmatching_keeps_a_locked_name(app):
    with app.state.session_factory() as db:
        s = Series(name="My Name", year_began=2001, mylar_name="Mylar Name", mylar_year=1999,
                   metron_id=5, match_status="matched", locked_fields=["name"])  # fmt: skip
        db.add(s)
        db.flush()
        unmatch_series(db, s, "wrong match")
        assert (s.name, s.year_began) == ("My Name", 1999)


def test_11_19_every_setting_the_code_reads_exists():
    """A key read with settings_store.get that isn't in SETTINGS can never be set:
    its feature silently does nothing (this was the case for two settings)."""
    keys = set()
    for path in FUMORA.rglob("*.py"):
        keys |= set(re.findall(r"settings_store\.get\(\s*db\s*,\s*\"([a-z_.]+)\"", path.read_text()))
    missing = sorted(k for k in keys if k not in settings_store.SETTINGS_BY_KEY)
    assert missing == [], f"settings read but never defined: {missing}"


def test_12_no_code_can_page_metrons_change_feed():
    assert not hasattr(MetronClient, "issues_modified_since")
    assert not hasattr(MetronClient, "series_modified_since")
    assert "modified_gt" not in (FUMORA / "metron.py").read_text()


@respx.mock
async def test_13_a_failing_series_goes_to_the_back_of_the_queue(app):
    from fumora.refresh import REFRESH, make_refresh_handler

    respx.get(url__regex=rf"{BASE_URL}issue/.*").respond(200, json=page([]))
    respx.get(f"{BASE_URL}series/7/").respond(404, json={"detail": "Not found."})
    with app.state.session_factory() as db:
        settings_store.put(db, "metron.api_token", "t")
        db.add(Series(name="Gone", metron_id=7, match_status="matched", confirmed_by="auto", monitored=True,
                      locked_fields=[]))  # fmt: skip
        db.commit()
    await make_refresh_handler(app.state.make_metron_client)(_job(app, REFRESH, {}))
    with app.state.session_factory() as db:
        gone = db.scalar(select(Series))
        assert gone.issues_synced is not None
        assert gone.id not in rolling_selection(db, 1, datetime.now(UTC))


async def test_14_a_missing_comicvine_issue_is_remembered(app):
    from fumora.tagging import ensure_cv_detail

    class NoCV:
        calls = 0

        async def issue(self, _id):
            NoCV.calls += 1
            return None

    with app.state.session_factory() as db:
        s = Series(name="x", source="cv", locked_fields=[])
        i = Issue(series=s, number="1", cv_id=9, locked_fields=[])
        db.add(s)
        db.flush()
        await ensure_cv_detail(db, NoCV(), i)
        await ensure_cv_detail(db, NoCV(), i)
        assert NoCV.calls == 1 and i.cv_detail == {}


@respx.mock
async def test_15_an_empty_answer_keeps_the_release_schedule(app):
    respx.get(f"{BASE_URL}issue/").mock(return_value=httpx.Response(200, json=page([])))
    with app.state.session_factory() as db:
        db.add(Release(metron_issue_id=99, metron_series_id=9, series_name="Kept", number="5",
                       store_date=date.today() + timedelta(days=3)))  # fmt: skip
        db.flush()
        async with client() as c:
            await update_releases(db, c)
        assert [r.series_name for r in db.scalars(select(Release))] == ["Kept"]


async def test_16_comicvine_series_retag_without_metron(app, tmp_path):
    folder = tmp_path / "cv"
    folder.mkdir()
    f = make_cbz(folder / "Mieruko-chan 001.cbz")
    with app.state.session_factory() as db:
        s = Series(name="Mieruko-chan", source="cv", match_status="not_found", path=str(folder),
                   locked_fields=[])  # fmt: skip
        s.issues = [Issue(number="1", locked_fields=[], files=[File(path=str(f))])]
        db.add(s)
        db.commit()
    msg = await make_retag_handler(app.state.make_metron_client)(_job(app, RETAG, {"dry_run": True}))
    assert msg.startswith("Preview:")


def test_17_add_search_without_a_login_shows_the_message(logged_in):
    r = logged_in.get("/add", params={"q": "batman"})
    assert r.status_code == 200 and "Metron API token" in r.text


def test_20_setup_needs_a_username(client):
    r = client.post("/setup", data={"username": "  ", "password": "longenough", "password2": "longenough"})
    assert r.status_code == 400 and "Enter a username." in r.text


@respx.mock
async def test_22_a_held_release_stays_held_if_its_replacement_fails(app, tmp_path):
    _setup(app, tmp_path, numbers=("16",))
    search = respx.get("http://prowlarr.test/api/v1/search")
    search.respond(200, json=[_release("Absolute Batman 016 (2026) FRENCH")])
    sab = respx.get(f"{SAB}/api", params={"mode": "addurl"})
    sab.respond(200, json={"status": True, "nzo_ids": ["n"]})
    await make_search_handler()(_job(app, "search", {}))
    search.respond(200, json=[_release("Absolute Batman 016 (2026) (Digital) (Shan-Empire)")])
    sab.respond(500)  # SABnzbd down: the better release can't be sent
    await make_search_handler()(_job(app, "search", {}))
    with app.state.session_factory() as db:
        states = {g.release_title: g.state for g in db.scalars(select(Grab))}
        assert states == {"Absolute Batman 016 (2026) FRENCH": APPROVAL}
        assert db.scalar(select(Event).where(Event.message.like("Couldn't send%"))) is not None


# --- download pipeline ------------------------------------------------------------------


def _grab_row(db, issue_ids, title="x", state="downloading", **kw):
    g = Grab(issue_id=issue_ids[0], issue_ids=issue_ids, indexer="t", release_title=title, protocol="usenet",
             client="sabnzbd", state=state, **kw)  # fmt: skip
    db.add(g)
    db.commit()
    return g


def test_24_a_pack_of_manga_volumes_is_matched_by_volume(app, tmp_path):
    from fumora.grabbing import import_grab

    _setup(app, tmp_path, numbers=("1", "2"))
    dl = tmp_path / "pack"
    dl.mkdir()
    for n in (1, 2):
        make_cbz(dl / f"Absolute Batman Vol. {n:02} (2024).cbz")
    with app.state.session_factory() as db:
        s = db.scalar(select(Series))
        s.numbering = "volumes"
        settings_store.put(db, "import.tag", "off")
        g = _grab_row(db, [_abs(db, "1").id, _abs(db, "2").id])
        assert import_grab(db, g, str(dl)).startswith("Imported #1, #2")


def test_26_a_single_file_that_is_another_issue_is_not_imported(app, tmp_path):
    from fumora.grabbing import import_grab

    _setup(app, tmp_path, numbers=("16",))
    dl = tmp_path / "one"
    dl.mkdir()
    make_cbz(dl / "Absolute Batman 017 (2026).cbz")
    with app.state.session_factory() as db:
        g = _grab_row(db, [_abs(db, "16").id])
        with pytest.raises(ImportFailed, match="the file is #17, not #16"):
            import_grab(db, g, str(dl))


@respx.mock
async def test_27_a_failed_import_is_not_grabbed_again_automatically(app, tmp_path):
    _setup(app, tmp_path, numbers=("16",))
    search = respx.get("http://prowlarr.test/api/v1/search").respond(
        200, json=[_release("Absolute Batman 016 (2026) (Digital)")]
    )
    with app.state.session_factory() as db:
        _grab_row(db, [_abs(db, "16").id], state="import_failed")
    summary = await make_search_handler()(_job(app, "search", {}))
    assert summary.startswith("Searched 0 wanted issues") and not search.called


@respx.mock
async def test_28_an_issue_imported_during_the_search_is_not_grabbed(app, tmp_path):
    _setup(app, tmp_path, numbers=("16",))
    sf = app.state.session_factory

    def imported_meanwhile(request):
        with sf() as db:  # the download monitor imports #16 while the indexer answers
            issue = _abs(db, "16")
            if not issue.files:
                issue.files.append(File(path=str(tmp_path / "x.cbz")))
                db.commit()
        return httpx.Response(200, json=[_release("Absolute Batman 016 (2026) (Digital)")])

    respx.get("http://prowlarr.test/api/v1/search").mock(side_effect=imported_meanwhile)
    sab = respx.get(f"{SAB}/api", params={"mode": "addurl"}).respond(
        200, json={"status": True, "nzo_ids": ["n"]}
    )
    summary = await make_search_handler()(_job(app, "search", {}))
    assert not sab.called
    # Found (the import could write while the search waited: #29) and still not grabbed.
    assert "grabbed 0, nothing found for 0" in summary and "errors" not in summary


def test_25_30_release_names(app):
    from fumora.releases import parse_release

    r = parse_release("DC.Comics-Absolute.Batman.No.02.2024.HYBRID.COMIC [Digital]")
    assert (r.series, r.number) == ("Absolute Batman", "2")  # de-dotted, publisher prefix dropped
    assert parse_release("Mr. Miracle 001 (2017)").series == "Mr. Miracle"
    r = parse_release("X-Men.001.2024 [Digital-Empire]")  # a tag longer than the dotted name
    assert (r.series, r.number, r.year) == ("X-Men", "1", 2024)
    # Abbreviations keep their dots (a trailing "." is trimmed, as it always was on main).
    for title, series in (("Captain America U.S.A. 001 (2024)", "Captain America U.S.A"),
                          ("G.I. Joe - A Real American Hero 001 (2024)", "G.I. Joe - A Real American Hero"),
                          ("B.P.R.D. 001 (2002)", "B.P.R.D")):  # fmt: skip
        r = parse_release(title)
        assert (r.series, r.number) == (series, "1"), title
    from fumora.releases import matches_issue, series_key

    r = parse_release("The.Walking.Dead 001 (2003) (Digital) (Zone-Empire)")  # dotted name, spaced rest
    assert series_key(r.series) == series_key("The Walking Dead") and r.number == "1"
    assert matches_issue(r, ["The Walking Dead"], "1", (2003, 2003))
    r = parse_release("X-Men.Volume.1.Issue.1.through.100.CBZ [ENG / CBZ]")  # a 100-issue pack, not #1
    assert (r.series, r.volume, r.kind, r.range_start, r.range_end) == ("X-Men", "1", "range", 1, 100)
    r = parse_release("Batman Vol 1 through 5 (2020)")  # volumes stay volumes
    assert r.kind != "range"

    from fumora.scanner import strip_series_prefix

    assert strip_series_prefix("The.Walking.Dead.001", "The Walking Dead") == ".001"
    r = parse_release("Batman Vol. 3 016 (2016) (Digital)")
    assert (r.series, r.number, r.volume) == ("Batman", "16", "3")


def test_31_a_new_issue_without_a_cover_date_matches_its_year(app, tmp_path):
    from fumora.search import Profile, evaluate

    _setup(app, tmp_path, numbers=("30",))
    with app.state.session_factory() as db:
        issue = _abs(db, "30")
        issue.series.year_began = 2022
        issue.cover_date, issue.store_date = None, date(2026, 3, 1)
        c = evaluate(_release("Absolute Batman 030 (2026) (Digital)"), issue, Profile())
        assert c.identified and "wrong year" not in " ".join(c.rejections)


async def test_32_a_retry_asked_for_during_a_search_runs_afterwards(app, monkeypatch):
    import asyncio

    from fumora import scheduler

    started = []

    class Jobs:
        running = True

        def active(self, kind):
            return object() if self.running else None

        def start(self, kind, params):
            started.append(params)

    jobs = Jobs()
    calls = {"n": 0}

    async def fake_check(sf, on_failed=None, before_import=None):
        calls["n"] += 1
        if calls["n"] == 1:
            on_failed([5])  # a download failed while a search runs
        elif calls["n"] == 2:
            jobs.running = False  # the search ended
        else:
            raise asyncio.CancelledError

    monkeypatch.setattr(scheduler, "check_downloads", fake_check)
    with pytest.raises(asyncio.CancelledError):
        await scheduler.run_download_monitor(app.state.session_factory, jobs, every=0)
    assert started == [{"issue_ids": [5], "retry": True}]


@respx.mock
async def test_33_shelfmark_http_errors_are_reported_as_such():
    from fumora.shelfmark import ShelfmarkClient, ShelfmarkError

    respx.get("http://shelfmark.test/api/releases").respond(502, text="<html>Bad Gateway</html>")
    async with ShelfmarkClient("http://shelfmark.test") as sm:
        with pytest.raises(ShelfmarkError, match="HTTP 502"):
            await sm.search("x")


def test_34_no_download_folder_is_an_error(app, tmp_path):
    from fumora.grabbing import import_grab

    _setup(app, tmp_path, numbers=("16",))
    with app.state.session_factory() as db:
        g = _grab_row(db, [_abs(db, "16").id])
        with pytest.raises(ImportFailed, match="gave no folder"):
            import_grab(db, g, "")


async def test_35_a_download_whose_client_is_gone_fails_instead_of_hanging(app, tmp_path):
    from fumora.grabbing import check_downloads

    _setup(app, tmp_path, numbers=("16",))
    with app.state.session_factory() as db:
        settings_store.put(db, "sabnzbd.url", "")
        g = _grab_row(db, [_abs(db, "16").id], client_job_id="n1")
        gid = g.id
    await check_downloads(app.state.session_factory)
    with app.state.session_factory() as db:
        g = db.get(Grab, gid)
        assert g.state == "failed" and "isn't set up" in g.message


def test_37_grab_for_a_deleted_issue_is_a_404(app, logged_in):
    import time as _t

    app.state.search_results["tok"] = (_t.time() + 60, 999, ["c"])
    assert logged_in.post("/issues/999/grab", data={"token": "tok", "index": 0}).status_code == 404


def test_38_try_another_on_a_pack_searches_every_issue(app, tmp_path, logged_in):
    _setup(app, tmp_path, numbers=("1", "2", "3"))
    started = []
    app.state.jobs.start = lambda kind, params: started.append(params)
    with app.state.session_factory() as db:
        ids = [_abs(db, n).id for n in ("1", "2", "3")]
        g = _grab_row(db, ids, state="import_failed")
        gid = g.id
    logged_in.post(f"/queue/{gid}/retry")
    assert started == [{"issue_ids": ids}]
    with app.state.session_factory() as db:
        assert db.get(Grab, gid).state == "removed"


# --- structure and tests ------------------------------------------------------------------


async def test_41_a_second_job_of_a_kind_is_refused_not_dropped(app, logged_in):
    import asyncio

    from fumora.jobs import JobBusy, JobRunner

    runner = JobRunner(app.state.session_factory)
    gate = asyncio.Event()

    async def slow(ctx):
        await gate.wait()
        return "ok"

    runner.register("slow", slow)
    runner.start("slow", {"series_ids": [1]})
    with pytest.raises(JobBusy):
        runner.start("slow", {"series_ids": [2]})  # was silently folded into job 1
    gate.set()
    await runner.shutdown()

    app.state.jobs.start = lambda kind, params=None: (_ for _ in ()).throw(JobBusy(kind, 7))
    # A route that does start a job while one runs gets the 409 page, not a silent no-op.
    r = logged_in.post("/library/scan", follow_redirects=False)
    assert r.status_code == 409 and "already running" in r.text


def test_44_only_the_newest_job_of_a_kind_resumes(app):
    from fumora.jobs import JobRunner
    from fumora.models import Job

    with app.state.session_factory() as db:
        db.add_all([Job(kind="k", state="running", params={}), Job(kind="k", state="queued", params={})])
        db.commit()
    runner = JobRunner(app.state.session_factory)
    spawned = []
    runner._spawn = lambda job_id, kind: spawned.append(job_id)
    runner.resume_all()
    with app.state.session_factory() as db:
        jobs = db.scalars(select(Job).order_by(Job.id)).all()
        assert spawned == [jobs[1].id] and jobs[0].state == "cancelled"


async def test_45_a_cancelled_job_counts_as_stopped_only_after_its_file_work(app):
    import asyncio
    import threading

    from fumora.jobs import JobRunner

    runner = JobRunner(app.state.session_factory)
    release, finished = threading.Event(), []

    def file_work():
        release.wait(5)
        finished.append(True)

    async def job(ctx):
        await ctx.thread(file_work)

    runner.register("files", job)
    job_id = runner.start("files")
    await asyncio.sleep(0.05)
    runner.cancel(job_id)
    await asyncio.sleep(0.05)
    assert runner.active("files") is not None  # still running: its thread hasn't finished
    release.set()
    for _ in range(100):
        if runner.active("files") is None:
            break
        await asyncio.sleep(0.02)
    assert finished and runner.active("files") is None


@respx.mock
async def test_57_a_failed_download_asks_for_a_new_search(app, tmp_path):
    from fumora.grabbing import check_downloads

    _setup(app, tmp_path, numbers=("16",))
    respx.get(f"{SAB}/api", params={"mode": "queue"}).respond(200, json={"queue": {"slots": []}})
    respx.get(f"{SAB}/api", params={"mode": "history"}).respond(200, json={"history": {"slots": [
        {"nzo_id": "n1", "status": "Failed", "fail_message": "CRC error"}]}})  # fmt: skip
    asked = []
    with app.state.session_factory() as db:
        issue_id = _abs(db, "16").id
        _grab_row(db, [issue_id], client_job_id="n1")
    await check_downloads(app.state.session_factory, on_failed=asked.extend)
    assert asked == [issue_id]


def test_59_organize_moves_the_files_recorded_in_a_renamed_folder(app, tmp_path):
    from tests.test_scanner_naming import _series

    old = tmp_path / "Dallas"
    old.mkdir()
    f = make_cbz(old / "Umbrella Academy Dallas 001 (2008).cbz")
    with app.state.session_factory() as db:
        s = _series(db, old)
        s.issues = [
            Issue(number="1", cover_date=date(2008, 11, 1), locked_fields=[], files=[File(path=str(f))])
        ]
        db.flush()
        plan = naming.plan_series(s)
        assert naming.apply_plan(db, s, plan)[0] >= 1
        moved = s.issues[0].files[0].path
        assert moved == str(
            tmp_path / "Umbrella Academy Dallas (2008)" / "Umbrella Academy Dallas 001 (2008).cbz"
        )
        assert os.path.exists(moved) and s.path == str(tmp_path / "Umbrella Academy Dallas (2008)")


# --- review of PR #30 --------------------------------------------------------------------


def test_pr30_1_a_file_of_another_series_is_not_trusted_for_its_number(app, tmp_path):
    from fumora.grabbing import _file_number

    _setup(app, tmp_path, numbers=("16",))
    with app.state.session_factory() as db:
        s = db.scalar(select(Series))
        assert _file_number(Path("Action Comics 017 (2026).cbz"), s, confident=True) is None
        assert _file_number(Path("Absolute Batman 017 (2026).cbz"), s, confident=True) == "17"


async def test_pr30_2_a_cancelled_jobs_file_work_ends_before_its_session(app):
    import asyncio
    import threading

    from fumora.jobs import JobRunner

    runner = JobRunner(app.state.session_factory)
    release, order = threading.Event(), []

    def rename():
        release.wait(5)
        order.append("thread done")

    async def job(ctx):
        try:
            await ctx.thread(rename)
        finally:
            order.append("session closed")

    runner.register("organize", job)
    job_id = runner.start("organize")
    await asyncio.sleep(0.05)
    runner.cancel(job_id)
    await asyncio.sleep(0.05)
    release.set()
    for _ in range(100):
        if runner.active("organize") is None:
            break
        await asyncio.sleep(0.02)
    assert order == ["thread done", "session closed"]


async def test_pr30_3_library_wide_retag_handles_a_big_library(app, tmp_path):
    """Every issue needs Metron and no login is saved: the job stops right after counting
    them. That count used an IN list of every id, which SQLite refuses past its limit."""
    from sqlalchemy import insert

    with app.state.session_factory() as db:
        s = Series(name="Big", match_status="not_found", locked_fields=[])
        db.add(s)
        db.flush()
        n = 33_000  # more than SQLite's limit of variables in one statement
        rows = [{"series_id": s.id, "number": str(i), "metron_id": i + 1, "locked_fields": [],
                 "status": "Skipped", "monitored": False} for i in range(n)]  # fmt: skip
        db.execute(insert(Issue), rows)
        ids = db.scalars(select(Issue.id)).all()
        db.execute(insert(File), [{"issue_id": i, "path": f"/nowhere/{i}.cbz"} for i in ids])
        db.commit()
    with pytest.raises(RuntimeError, match="No Metron API token"):
        await make_retag_handler(app.state.make_metron_client)(_job(app, RETAG, {"dry_run": True}))


def test_pr30_4_try_another_during_a_search_keeps_the_button(app, tmp_path, logged_in):
    _setup(app, tmp_path, numbers=("16",))
    app.state.jobs.active = lambda kind: object()  # a search is running
    with app.state.session_factory() as db:
        gid = _grab_row(db, [_abs(db, "16").id], state="import_failed").id
    logged_in.post(f"/queue/{gid}/retry")
    with app.state.session_factory() as db:
        assert db.get(Grab, gid).state == "import_failed"  # still offered; nothing searched yet


@respx.mock
async def test_pr30_5_a_release_approved_during_the_search_is_not_superseded(app, tmp_path):
    _setup(app, tmp_path, numbers=("16",))
    sf = app.state.session_factory
    search = respx.get("http://prowlarr.test/api/v1/search")
    search.respond(200, json=[_release("Absolute Batman 016 (2026) FRENCH")])
    sab = respx.get(f"{SAB}/api", params={"mode": "addurl"}).respond(
        200, json={"status": True, "nzo_ids": ["n"]}
    )
    await make_search_handler()(_job(app, "search", {}))  # the French one is held for an OK

    def approved_meanwhile(request):
        if "016" not in request.url.params.get("query", ""):  # the series search: still French only
            return httpx.Response(200, json=[_release("Absolute Batman 016 (2026) FRENCH")])
        with sf() as db:  # the issue-specific follow-up search, after the held one was looked up
            held = db.scalar(select(Grab).where(Grab.state == APPROVAL))
            if held is not None:
                held.state = "downloading"  # the owner clicked OK on the Queue page
                db.commit()
        return httpx.Response(200, json=[_release("Absolute Batman 016 (2026) (Digital)")])

    search.mock(side_effect=approved_meanwhile)
    sab.reset()
    await make_search_handler()(_job(app, "search", {}))
    with sf() as db:
        assert [g.state for g in db.scalars(select(Grab))] == ["downloading"]
    assert not sab.called


def test_pr30_6_accepting_suggestions_during_an_import_is_fine(app, logged_in):
    started = []
    app.state.jobs.active = lambda kind: object()  # an import is running: it applies them itself
    app.state.jobs.start = lambda kind, params=None: started.append(kind)
    r = logged_in.post("/import/accept-suggestions", follow_redirects=False)
    assert r.status_code == 303 and started == []


def test_settings_only_save_offered_choices(app):
    with app.state.session_factory() as db:
        assert settings_store.apply_form(db, {"metron.requests_per_minute": "50"}) == []
        assert settings_store.get(db, "metron.requests_per_minute") == "20"
        assert settings_store.apply_form(db, {"metron.requests_per_minute": "10"}) == [
            "metron.requests_per_minute"
        ]
        assert settings_store.get(db, "metron.requests_per_minute") == "10"


def test_organize_twice_outside_the_library_stays_put(app, tmp_path):
    """A series kept outside the library folder: a second Organize must not nest it again."""
    from tests.test_scanner_naming import _series

    tpl = "{Publisher}/{Series Title} ({Series Year})"
    old = tmp_path / "elsewhere" / "Dallas"
    old.mkdir(parents=True)
    with app.state.session_factory() as db:
        s = _series(db, old)
        plan = naming.plan_series(s, folder_template=tpl, library_root=str(tmp_path / "library"))
        assert naming.apply_plan(db, s, plan)[0] == 1
        assert s.path == str(tmp_path / "elsewhere" / "Dark Horse" / "Umbrella Academy Dallas (2008)")
        again = naming.plan_series(s, folder_template=tpl, library_root=str(tmp_path / "library"))
        assert again.folder is None


def test_organize_outside_the_library_under_a_matching_publisher_folder(app, tmp_path):
    from tests.test_scanner_naming import _series

    tpl = "{Publisher}/{Series Title} ({Series Year})"
    with app.state.session_factory() as db:
        s = _series(db, tmp_path / "outside" / "Dark Horse" / "Dallas")
        plan = naming.plan_series(s, folder_template=tpl, library_root=str(tmp_path / "library"))
        assert plan.folder.new == str(tmp_path / "outside" / "Dark Horse" / "Umbrella Academy Dallas (2008)")


def test_organize_a_series_at_the_filesystem_root(app):
    with app.state.session_factory() as db:
        from tests.test_scanner_naming import _series

        s = _series(db, Path("/Umbrella Academy Dallas (2008)"))
        assert naming.plan_series(s).folder is None
