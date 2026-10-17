from datetime import UTC, date, datetime, timedelta

import httpx
import respx
from sqlalchemy import select

from fumora import settings_store
from fumora.catalog import add_series
from fumora.metron import BASE_URL, MetronClient, RateLimiter
from fumora.models import File, Issue, Release, Series
from fumora.refresh import make_refresh_handler
from fumora.scheduler import due
from tests.test_m2_routes_jobs import _job


def page(results):
    return {"count": len(results), "next": None, "previous": None, "results": results}


def client():
    return MetronClient("u", "p", RateLimiter(min_interval=0))


SERIES_DETAIL = {
    "id": 500, "name": "Monthly", "sort_name": "Monthly", "volume": 1, "year_began": 2025,
    "series_type": {"id": 13, "name": "Single Issue"}, "status": "Ongoing",
    "publisher": {"id": 1, "name": "Pub"}, "imprint": None, "genres": [], "cv_id": 9, "gcd_id": None,
    "desc": "", "modified": "2026-10-16T10:00:00-04:00", "issue_count": 3,
}  # fmt: skip


@respx.mock
async def test_daily_refresh_uses_releases_then_a_rolling_batch(app):
    future = (date.today() + timedelta(days=3)).isoformat()
    long_ago = datetime(2026, 1, 1)
    recent = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)
    sf = app.state.session_factory
    with sf() as db:
        settings_store.put(db, "metron.username", "u")
        settings_store.put(db, "metron.password", "p")
        settings_store.put(db, "schedule.refresh_batch", "1")

        def matched(name, mid, synced, year_end=None):
            return Series(name=name, metron_id=mid, match_status="matched", confirmed_by="auto",
                          monitored=True, monitor_new=True, locked_fields=[], issues_synced=synced,
                          year_end=year_end, added=datetime(2026, 1, 1, tzinfo=UTC))  # fmt: skip

        monthly = matched("Monthly", 500, recent)  # checked an hour ago, but has a release
        i1 = Issue(series=monthly, number="1", metron_id=5001, match_status="matched", monitored=True,
                   metron_detail={"modified": "2026-10-16T09:00:00+00:00"}, locked_fields=[])  # fmt: skip
        i2 = Issue(series=monthly, number="2", metron_id=5002, match_status="matched", monitored=True,
                   metron_detail={"modified": "2026-10-01T09:00:00+00:00"}, locked_fields=[])  # fmt: skip
        i1.files = [File(path="/x/1.cbz")]
        old_ongoing = matched("Old ongoing", 600, long_ago)  # due: picked by the batch of 1
        # Checked 10 days ago: an ongoing series would be due (weekly), an ended one isn't (monthly).
        older_ended = matched("Ended", 700, recent - timedelta(days=10), year_end=2020)
        fresh = matched("Fresh", 800, recent)  # not due
        db.add_all([monthly, i1, i2, old_ongoing, older_ended, fresh])
        db.commit()

    def series_detail(request):
        sid = int(str(request.url).rstrip("/").rsplit("/", 1)[-1])
        return httpx.Response(200, json={**SERIES_DETAIL, "id": sid})

    respx.get(url__regex=rf"{BASE_URL}series/\d+/$").mock(side_effect=series_detail)

    def issue_route(request):
        p = request.url.params
        if "store_date_range_after" in p:
            monthly = {"id": 500, "name": "Monthly", "year_began": 2025}
            other = {"id": 900, "name": "Not ours", "year_began": 2020}
            return httpx.Response(200, json=page([
                {"id": 5003, "number": "3", "store_date": future, "series": monthly},
                {"id": 9001, "number": "12", "store_date": future, "series": other},
            ]))  # fmt: skip
        if p.get("series_id") == "500":
            m16, m17 = "2026-10-16T09:00:00+00:00", "2026-10-17T09:00:00+00:00"
            return httpx.Response(200, json=page([
                {"id": 5001, "number": "1", "cover_date": "2025-01-01", "modified": m16},
                {"id": 5002, "number": "2", "cover_date": "2025-02-01", "modified": m17},
                {"id": 5003, "number": "3", "store_date": future, "modified": m17},
            ]))  # fmt: skip
        return httpx.Response(200, json=page([]))

    respx.get(f"{BASE_URL}issue/").mock(side_effect=issue_route)
    handler = make_refresh_handler(lambda u, p: client())
    summary = await handler(_job(app, "refresh", {}))
    assert "Refreshed 2 series: 1 new issues, 1 issue records to re-fetch" in summary
    assert "2 scheduled releases" in summary
    fetched = {str(c.request.url) for c in respx.calls}
    assert any(u.endswith("/series/500/") for u in fetched)  # release this week
    assert any(u.endswith("/series/600/") for u in fetched)  # oldest due, batch of 1
    assert not any(u.endswith("/series/700/") or u.endswith("/series/800/") for u in fetched)
    assert not any("modified_gt" in u for u in fetched)  # no change feed
    with sf() as db:
        s = db.scalar(select(Series).where(Series.metron_id == 500))
        by = {i.number: i for i in s.issues}
        assert by["3"].monitored and by["3"].store_date.isoformat() == future
        assert by["2"].metron_detail is None and by["1"].metron_detail is not None
        assert len(db.scalars(select(Release)).all()) == 2


@respx.mock
async def test_add_series_applies_option_and_links_existing_folder(app, tmp_path):
    folder = tmp_path / "Monthly (2025)"
    folder.mkdir()
    (folder / "Monthly 001 (2025).cbz").write_bytes(b"x")
    future = (date.today() + timedelta(days=10)).isoformat()
    respx.get(f"{BASE_URL}series/500/").respond(200, json=SERIES_DETAIL)
    respx.get(f"{BASE_URL}issue/", params={"series_id": "500"}).respond(200, json=page([
        {"id": 5001, "number": "1", "cover_date": "2025-01-01"},
        {"id": 5002, "number": "2", "cover_date": "2025-02-01"},
        {"id": 5003, "number": "3", "store_date": future},
    ]))  # fmt: skip
    with app.state.session_factory() as db:
        settings_store.put(db, "library.path", str(tmp_path))
        async with client() as c:
            s = await add_series(db, c, 500, option="missing", monitor_new=True)
        db.commit()
        assert s.path == str(folder) and s.confirmed_by == "user" and s.match_status == "matched"
        by = {i.number: i for i in s.issues}
        assert by["1"].files  # linked from the existing folder
        assert [n for n, i in sorted(by.items()) if i.monitored] == ["2", "3"]  # missing + future
        async with client() as c:
            assert (await add_series(db, c, 500)).id == s.id  # already there: no duplicate


def test_scheduler_due():
    now = datetime(2026, 10, 17, 5, 30, tzinfo=UTC)
    assert due(now, "05:00", None)
    assert not due(now, "05:00", "2026-10-17")  # already ran today
    assert not due(now, "06:00", None)
    assert not due(now, "", None) and not due(now, "garbage", None)


def test_m3_pages_render(logged_in, app):
    with app.state.session_factory() as db:
        s = Series(name="Monthly", metron_id=500, match_status="matched", confirmed_by="auto",
                   monitored=True, locked_fields=[])  # fmt: skip
        i = Issue(series=s, number="2", cover_date=date(2025, 2, 1), monitored=True, locked_fields=[])
        db.add_all([s, i])
        db.add(Release(metron_issue_id=1, metron_series_id=500, series_name="Monthly", number="9",
                       store_date=date.today()))  # fmt: skip
        db.commit()
        sid, iid = s.id, i.id
    assert "Monthly" in logged_in.get("/wanted").text
    assert "Missing, not monitored" in logged_in.get("/wanted?show=missing").text
    r = logged_in.get("/releases").text
    assert "Your series this week (1)" in r
    assert logged_in.post(f"/issues/{iid}/monitor", headers={"HX-Request": "true"}).text.count("○") == 1
    logged_in.post(f"/series/{sid}/monitor-option", data={"option": "none"})
    with app.state.session_factory() as db:
        assert not db.get(Series, sid).monitored
    assert logged_in.get("/add").status_code == 200
