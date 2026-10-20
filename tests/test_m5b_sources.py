"""M5b: series following ComicVine, the monthly move to Metron, manga reading direction."""

from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
import respx
from sqlalchemy import select

from fumora import settings_store
from fumora.comicvine import BASE_URL as CV
from fumora.comicvine import ComicVineAuthError, ComicVineClient, ComicVineError
from fumora.metron import BASE_URL as METRON
from fumora.metron import RateLimiter
from fumora.models import Event, File, Issue, Publisher, Series
from fumora.sources import (
    add_cv_series,
    check_on_metron,
    cv_selection,
    make_cv_refresh_handler,
    metron_check_selection,
    sync_cv_issues,
)
from tests.test_m2_routes_jobs import _job
from tests.test_m3_refresh import client as metron_client
from tests.test_m3_refresh import page


def cv_client():
    return ComicVineClient("cvkey", RateLimiter(min_interval=0, jitter=0))


def cv_ok(results, total=None):
    return {"status_code": 1, "error": "OK", "number_of_total_results": total or len(results),
            "limit": 100, "offset": 0, "results": results}  # fmt: skip


def cv_issue(n, cover, store=None, cid=None):
    return {"id": cid or 1000 + n, "issue_number": str(n), "name": f"Volume {n}", "cover_date": cover,
            "store_date": store, "date_last_updated": "2026-01-01 00:00:00"}  # fmt: skip


def _mieruko(db, issues=11, with_files=True, monitored=True):
    s = Series(name="Mieruko-chan", year_began=2020, source="cv", cv_id=131898, match_status="not_found",
               monitored=monitored, monitor_new=True, locked_fields=[],
               added=datetime(2020, 1, 1, tzinfo=UTC), publisher=Publisher(name="Yen Press"))  # fmt: skip
    for n in range(1, issues + 1):
        i = Issue(series=s, number=str(n), title=f"Volume {n}", cover_date=date(2020 + n // 3, 1, 1),
                  monitored=True, locked_fields=[])  # fmt: skip
        if with_files:
            i.files = [File(path=f"/lib/Mieruko-chan {n:03}.cbz")]
    db.add(s)
    db.commit()
    return s


# --- ComicVine client ----------------------------------------------------------------


@respx.mock
async def test_volume_issues_pages_and_sends_the_key():
    route = respx.get(f"{CV}issues/")
    route.side_effect = [
        httpx.Response(200, json=cv_ok([cv_issue(n, "2020-01-01") for n in range(1, 101)], total=101)),
        httpx.Response(200, json=cv_ok([cv_issue(101, "2030-01-01")], total=101)),
    ]
    async with cv_client() as cv:
        got = [i async for i in cv.volume_issues(5)]
    assert len(got) == 101
    params = route.calls[1].request.url.params
    assert params["filter"] == "volume:5" and params["offset"] == "100" and params["api_key"] == "cvkey"
    assert "Fumora" in route.calls[0].request.headers["user-agent"]


@respx.mock
async def test_comicvine_errors():
    respx.get(f"{CV}types/").respond(200, json={"status_code": 100, "error": "Invalid API Key"})
    async with cv_client() as cv:
        with pytest.raises(ComicVineAuthError):
            await cv.check()
    limiter = RateLimiter(min_interval=0, jitter=0)
    respx.get(f"{CV}volume/4050-5/").respond(420, text="Slow down")
    async with ComicVineClient("k", limiter) as cv:
        with pytest.raises(ComicVineError, match="rate-limiting"):
            await cv.volume(5)
    assert limiter.blocked_until > 0  # every request now waits about an hour


# --- refresh -----------------------------------------------------------------------------


@respx.mock
async def test_sync_adds_new_volumes_and_updates_dates(app):
    with app.state.session_factory() as db:
        s = _mieruko(db)
        db.get(Series, s.id).issues[0].locked_fields = ["cover_date"]
        db.commit()
        respx.get(f"{CV}issues/").respond(200, json=cv_ok(
            [cv_issue(n, "2021-02-03") for n in range(1, 12)]
            + [cv_issue(12, "2026-04-27", "2026-04-28"), cv_issue(13, "2027-01-01")]))  # fmt: skip
        async with cv_client() as cv:
            counts = await sync_cv_issues(db, cv, db.get(Series, s.id))
        db.commit()
        issues = {i.number: i for i in db.get(Series, s.id).issues}
    assert counts["new"] == 2
    assert issues["12"].store_date == date(2026, 4, 28) and issues["12"].title == "Volume 12"
    assert issues["12"].monitored and issues["12"].cv_id == 1012
    assert issues["1"].cover_date == date(2020, 1, 1)  # locked by the user: kept
    assert issues["2"].cover_date == date(2021, 2, 3)


def test_cv_selection_monitored_weekly_others_monthly(app):
    now = datetime.now(UTC)
    with app.state.session_factory() as db:

        def cv_series(name, synced, monitored):
            return Series(name=name, source="cv", cv_id=hash(name) % 10**6, match_status="not_found",
                          monitored=monitored, locked_fields=[], issues_synced=synced)  # fmt: skip

        db.add_all([
            cv_series("never", None, False),
            cv_series("mon-old", (now - timedelta(days=8)).replace(tzinfo=None), True),
            cv_series("mon-fresh", (now - timedelta(days=2)).replace(tzinfo=None), True),
            cv_series("other-10d", (now - timedelta(days=10)).replace(tzinfo=None), False),
            cv_series("other-40d", (now - timedelta(days=40)).replace(tzinfo=None), False),
            Series(name="on-metron", source="metron", cv_id=1, metron_id=1, match_status="matched",
                   locked_fields=[]),
        ])  # fmt: skip
        db.commit()
        names = [db.get(Series, i).name for i in cv_selection(db, 10, now)]
    assert names == ["mon-old", "never", "other-40d"]


@respx.mock
async def test_refresh_job(app):
    with app.state.session_factory() as db:
        settings_store.put(db, "comicvine.api_key", "cvkey")
        db.commit()
        sid = _mieruko(db).id
    respx.get(f"{CV}issues/").respond(200, json=cv_ok([cv_issue(n, "2021-01-01") for n in range(1, 13)]))
    summary = await make_cv_refresh_handler(lambda k: cv_client())(_job(app, "cv_refresh", {}))
    assert "refreshed 1 series, 1 new issues" in summary
    with app.state.session_factory() as db:
        assert len(db.get(Series, sid).issues) == 12 and db.get(Series, sid).issues_synced is not None


# --- moving to Metron ------------------------------------------------------------------------

DETAIL = {"id": 900, "name": "Mieruko-chan", "year_began": 2020, "publisher": {"id": 7, "name": "Yen Press"},
          "series_type": {"name": "Single Issue"}, "status": "Ongoing", "genres": [], "cv_id": 131898,
          "modified": "2026-10-01T00:00:00+00:00"}  # fmt: skip


def metron_issues(numbers, year=None):
    return page([{"id": 9000 + int(n), "number": n,
                  "cover_date": f"{year or 2020 + int(n) // 3}-01-01"} for n in numbers])  # fmt: skip


def mock_metron(detail=DETAIL, numbers=range(1, 12), found=True, year=None):
    respx.get(f"{METRON}series/", params={"cv_id": "131898"}).respond(
        200, json=page([{"id": detail["id"]}] if found else [])
    )
    respx.get(f"{METRON}series/{detail['id']}/").respond(200, json=detail)
    respx.get(f"{METRON}issue/").respond(200, json=metron_issues([str(n) for n in numbers], year))


@respx.mock
async def test_everything_agrees_so_it_moves(app):
    mock_metron()
    with app.state.session_factory() as db:
        s = _mieruko(db)
        async with metron_client() as m:
            assert await check_on_metron(db, m, s) == "moved"
        db.commit()
        s = db.get(Series, s.id)
        assert (s.source, s.metron_id, s.match_status, s.confirmed_by) == ("metron", 900, "matched", "auto")
        assert all(i.metron_id for i in s.issues)


@respx.mock
async def test_not_on_metron_yet(app):
    mock_metron(found=False)
    with app.state.session_factory() as db:
        s = _mieruko(db)
        async with metron_client() as m:
            assert await check_on_metron(db, m, s) == "absent"
        assert s.source == "cv" and s.metron_checked is not None


@respx.mock
async def test_wrong_comicvine_id_on_metron_goes_to_review(app):
    mock_metron(detail={**DETAIL, "name": "Harbinger: Omegas", "year_began": 2011})
    with app.state.session_factory() as db:
        s = _mieruko(db)
        async with metron_client() as m:
            assert await check_on_metron(db, m, s) == "review"
        assert s.source == "cv" and s.suggested_metron_id == 900
        assert "Harbinger: Omegas" in s.match_note


@respx.mock
async def test_different_numbering_goes_to_review(app):
    mock_metron(numbers=range(1, 6))  # Metron splits the run: 6-11 are elsewhere
    with app.state.session_factory() as db:
        s = _mieruko(db)
        async with metron_client() as m:
            assert await check_on_metron(db, m, s) == "review"
        assert "numbering differs" in s.match_note and "#6 is not on Metron" in s.match_note


@respx.mock
async def test_dates_far_apart_go_to_review(app):
    mock_metron(year=1990)
    with app.state.session_factory() as db:
        s = _mieruko(db)
        async with metron_client() as m:
            assert await check_on_metron(db, m, s) == "review"
        assert "dated 1990-01 on Metron" in s.match_note


@respx.mock
async def test_rejected_match_is_never_suggested_again(app, logged_in):
    mock_metron(detail={**DETAIL, "name": "Harbinger: Omegas"})
    with app.state.session_factory() as db:
        s = _mieruko(db)
        async with metron_client() as m:
            await check_on_metron(db, m, s)
        db.commit()
        sid = s.id
    logged_in.post(f"/series/{sid}/not-on-metron")
    with app.state.session_factory() as db:
        s = db.get(Series, sid)
        assert s.rejected_metron_ids == [900] and s.suggested_metron_id is None
        s.metron_checked = None
        db.commit()
        async with metron_client() as m:
            assert await check_on_metron(db, m, s) == "rejected"


def test_monthly_check_selection(app):
    now = datetime.now(UTC)
    with app.state.session_factory() as db:
        a = _mieruko(db)
        b = Series(name="Checked", source="cv", cv_id=2, match_status="not_found", locked_fields=[],
                   metron_checked=(now - timedelta(days=3)).replace(tzinfo=None))  # fmt: skip
        c = Series(name="Waiting", source="cv", cv_id=3, match_status="review", suggested_metron_id=5,
                   locked_fields=[])  # fmt: skip
        db.add_all([b, c])
        db.commit()
        assert metron_check_selection(db, now) == [a.id]


# --- adding from ComicVine, gaps page, reading direction ---------------------------------------


@respx.mock
async def test_add_from_comicvine(app):
    vol = {"id": 131898, "name": "Mieruko-chan", "start_year": "2020", "count_of_issues": 1,
           "publisher": {"id": 2342, "name": "Yen Press"},
           "site_detail_url": "https://comicvine.gamespot.com/mieruko-chan/4050-131898/"}  # fmt: skip
    respx.get(f"{CV}volume/4050-131898/").respond(200, json={"status_code": 1, "error": "OK", "results": vol})
    respx.get(f"{CV}issues/").respond(200, json=cv_ok([cv_issue(1, "2020-12-01")]))
    with app.state.session_factory() as db:
        settings_store.put(db, "library.path", "/nonexistent")
        async with cv_client() as cv:
            s = await add_cv_series(db, cv, 131898, "all")
        db.commit()
        assert (s.source, s.match_status, s.year_began, s.publisher.name) == (
            "cv",
            "not_found",
            2020,
            "Yen Press",
        )
        assert [i.number for i in s.issues] == ["1"] and s.issues[0].monitored


@respx.mock
def test_add_page_prefers_metron(app, logged_in):
    with app.state.session_factory() as db:
        settings_store.put(db, "comicvine.api_key", "cvkey")
        settings_store.put(db, "metron.api_token", "tok")
        db.commit()
    respx.get(f"{METRON}series/", params={"cv_id": "131898"}).respond(200, json=page([{"id": 900}]))
    r = logged_in.get("/add/cv/131898", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/add/900"


def test_gaps_page_lists_comicvine_series(app, logged_in):
    with app.state.session_factory() as db:
        s = _mieruko(db)
        s.rejected_metron_ids = [900]
        db.commit()
    page_ = logged_in.get("/metron-gaps").text
    assert (
        "Mieruko-chan" in page_
        and "Rejected Metron matches (1)" in page_
        and "metron.cloud/series/900" in page_
    )


def test_reading_direction_is_written_as_comicinfo_manga(app, tmp_path, logged_in):
    from fumora.tagging import issue_meta

    with app.state.session_factory() as db:
        s = _mieruko(db, issues=1)
        sid = s.id
    logged_in.post(f"/series/{sid}/edit", data={"reading_direction": "rtl"})
    with app.state.session_factory() as db:
        issue = db.get(Series, sid).issues[0]
        meta = issue_meta(issue)
        from fumora.comicinfo import comicinfo_fields

        fields = comicinfo_fields(meta)
    assert fields["Manga"] == "YesAndRightToLeft"


def test_comicvine_refresh_time_is_random_per_install(app, client):
    with app.state.session_factory() as db:
        at = settings_store.get(db, "schedule.cv_refresh_time")
        events = [e.message for e in db.scalars(select(Event))]
    assert at and ":" in at and any("ComicVine refresh set to" in m for m in events)


# --- review regressions ----------------------------------------------------------------------


def test_not_on_metron_keeps_comicvine_issues(app, logged_in):
    with app.state.session_factory() as db:
        s = _mieruko(db, issues=3)
        db.add(Issue(series=s, number="4", match_status="new", locked_fields=[]))  # from ComicVine
        s.match_status, s.suggested_metron_id = "review", 900
        db.commit()
        sid = s.id
    logged_in.post(f"/series/{sid}/not-on-metron")
    with app.state.session_factory() as db:
        assert sorted(i.number for i in db.get(Series, sid).issues) == ["1", "2", "3", "4"]


@respx.mock
def test_add_from_comicvine_works_without_metron(app, logged_in):
    with app.state.session_factory() as db:
        settings_store.put(db, "comicvine.api_key", "cvkey")  # no Metron login at all
        db.commit()
    vol = {"id": 5, "name": "Indie", "start_year": "2024", "count_of_issues": 1, "publisher": None,
           "site_detail_url": "https://comicvine.gamespot.com/x/4050-5/"}  # fmt: skip
    respx.get(f"{CV}volume/4050-5/").respond(200, json={"status_code": 1, "error": "OK", "results": vol})
    r = logged_in.get("/add/cv/5")
    assert r.status_code == 200 and "check Metron first" in r.text and "Indie" in r.text


async def test_metron_check_errors_never_stop_the_rest(app, monkeypatch):
    from fumora import sources
    from fumora.metron import MetronDailyLimitReached

    with app.state.session_factory() as db:
        a = _mieruko(db).id
        b = Series(name="Other", source="cv", cv_id=7, match_status="not_found", locked_fields=[])
        db.add(b)
        db.commit()
        b = b.id
    calls = []

    async def boom(db, client, series):
        calls.append(series.id)
        if series.id == a:
            raise KeyError("odd answer")
        raise MetronDailyLimitReached(0)

    monkeypatch.setattr(sources, "check_on_metron", boom)
    counts = await sources.run_metron_checks(_job(app, "refresh", {}), None)
    assert counts["errors"] == 1 and calls == [a, b]  # error logged, then the pause ended it


@respx.mock
async def test_comicvine_refresh_skips_a_series_that_moved_meanwhile(app):
    with app.state.session_factory() as db:
        settings_store.put(db, "comicvine.api_key", "cvkey")
        db.commit()
        sid = _mieruko(db).id

    def moved_while_fetching(request):
        with app.state.session_factory() as db:
            s = db.get(Series, sid)
            s.source, s.metron_id, s.match_status = "metron", 900, "matched"
            db.commit()
        return httpx.Response(200, json=cv_ok([cv_issue(n, "1999-01-01") for n in range(1, 13)]))

    respx.get(f"{CV}issues/").mock(side_effect=moved_while_fetching)
    summary = await make_cv_refresh_handler(lambda k: cv_client())(
        _job(app, "cv_refresh", {"series_ids": [sid]})
    )
    assert "refreshed 0 series" in summary
    with app.state.session_factory() as db:
        assert len(db.get(Series, sid).issues) == 11  # nothing written over the Metron series


@respx.mock
def test_accepting_a_check_suggestion_moves_it_directly(app, logged_in):
    mock_metron(numbers=range(1, 6))
    with app.state.session_factory() as db:
        settings_store.put(db, "metron.api_token", "tok")
        s = _mieruko(db)
        s.match_status, s.suggested_metron_id = "review", 900
        s.match_note = (
            "Metron has this ComicVine id (series 900), but the numbering differs. Accept or reject."
        )
        db.commit()
        sid = s.id
    assert logged_in.post(f"/series/{sid}/accept-suggestion", follow_redirects=False).status_code == 303
    with app.state.session_factory() as db:
        s = db.get(Series, sid)
        assert (s.source, s.metron_id, s.confirmed_by) == ("metron", 900, "user")
        assert s.match_note.startswith("Moved to Metron by hand")
        assert app.state.jobs.active("mylar_import") is None


def test_comicvine_limiter_names_itself():
    from fumora.comicvine import make_limiter

    assert make_limiter().name == "ComicVine"
