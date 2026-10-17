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
async def test_incremental_refresh_updates_only_our_changed_series(app):
    future = (date.today() + timedelta(days=10)).isoformat()
    sf = app.state.session_factory
    with sf() as db:
        settings_store.put(db, "metron.username", "u")
        settings_store.put(db, "metron.password", "p")
        s = Series(name="Monthly", metron_id=500, match_status="matched", confirmed_by="auto",
                   monitored=True, monitor_new=True, locked_fields=[],
                   added=datetime(2026, 1, 1, tzinfo=UTC))  # fmt: skip
        i1 = Issue(series=s, number="1", metron_id=5001, match_status="matched", monitored=True,
                   metron_detail={"modified": "2026-10-16T09:00:00+00:00"}, locked_fields=[])  # fmt: skip
        i2 = Issue(series=s, number="2", metron_id=5002, match_status="matched", monitored=True,
                   metron_detail={"modified": "2026-10-01T09:00:00+00:00"}, locked_fields=[])  # fmt: skip
        i1.files = [File(path="/x/1.cbz")]
        db.add_all([s, i1, i2, Series(name="Not ours on Metron", locked_fields=[])])
        db.commit()

    respx.get(f"{BASE_URL}series/").side_effect = lambda request: httpx.Response(
        200, json=page([{"id": 500}, {"id": 999}]) if "modified_gt" in request.url.params else page([])
    )
    respx.get(f"{BASE_URL}series/500/").respond(200, json=SERIES_DETAIL)
    issues = respx.get(f"{BASE_URL}issue/")

    def issue_route(request):
        p = request.url.params
        if "modified_gt" in p:
            return httpx.Response(200, json=page([{"id": 7, "series": {"id": 777}}]))
        if "store_date_range_after" in p:
            monthly = {"id": 500, "name": "Monthly", "year_began": 2025}
            other = {"id": 800, "name": "Other", "year_began": 2020}
            return httpx.Response(200, json=page([
                {"id": 5003, "number": "3", "store_date": future, "series": monthly},
                {"id": 8001, "number": "12", "store_date": future, "series": other},
            ]))  # fmt: skip
        return httpx.Response(200, json=page([
            {"id": 5001, "number": "1", "cover_date": "2025-01-01", "modified": "2026-10-16T09:00:00+00:00"},
            {"id": 5002, "number": "2", "cover_date": "2025-02-01", "modified": "2026-10-17T09:00:00+00:00"},
            {"id": 5003, "number": "3", "store_date": future, "modified": "2026-10-17T09:00:00+00:00"},
        ]))  # fmt: skip

    issues.side_effect = issue_route
    handler = make_refresh_handler(lambda u, p: client())
    summary = await handler(_job(app, "refresh", {}))
    assert "Refreshed 1 series: 1 new issues, 1 issue records to re-fetch" in summary
    assert "2 scheduled releases" in summary
    with sf() as db:
        s = db.scalar(select(Series).where(Series.metron_id == 500))
        by = {i.number: i for i in s.issues}
        assert by["3"].monitored and by["3"].store_date.isoformat() == future  # new, future: monitored
        assert by["2"].metron_detail is None  # Metron changed it since we cached it
        assert by["1"].metron_detail is not None
        assert len(db.scalars(select(Release)).all()) == 2
        assert settings_store.get_state(db, "refresh.last_success")
    # Only the series (500) and its issue list/detail were fetched, nothing for 999 or 777.
    assert not any("/series/999/" in str(c.request.url) for c in respx.calls)


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
