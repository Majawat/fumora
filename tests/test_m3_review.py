"""Regression tests for the M3 code review."""

from datetime import UTC, date, datetime, timedelta

import httpx
import respx
from sqlalchemy import select

from fumora import settings_store
from fumora.catalog import add_series
from fumora.metron import BASE_URL
from fumora.models import Issue, Job, Release, Series, Setting
from fumora.refresh import rolling_selection, update_releases
from fumora.scheduler import due
from tests.test_m3_refresh import SERIES_DETAIL, client, page


def test_empty_refresh_time_turns_the_schedule_off(logged_in, app):
    logged_in.post("/settings", data={"schedule.refresh_time": ""})
    with app.state.session_factory() as db:
        at = settings_store.get(db, "schedule.refresh_time")
    assert at == "" and not due(datetime(2026, 10, 17, 23, 0, tzinfo=UTC), at, None)
    logged_in.post("/settings", data={"schedule.refresh_time": "06:30"})
    with app.state.session_factory() as db:
        assert settings_store.get(db, "schedule.refresh_time") == "06:30"


@respx.mock
async def test_added_series_keeps_its_synced_time(app, tmp_path):
    respx.get(f"{BASE_URL}series/500/").respond(200, json=SERIES_DETAIL)
    respx.get(f"{BASE_URL}issue/").respond(
        200, json=page([{"id": 1, "number": "1", "cover_date": "2025-01-01"}])
    )
    with app.state.session_factory() as db:
        settings_store.put(db, "library.path", str(tmp_path / "nope"))
        async with client() as c:
            sid = (await add_series(db, c, 500)).id
        db.commit()
    with app.state.session_factory() as db:
        assert db.get(Series, sid).issues_synced is not None


def test_only_current_releases_trigger_refreshes(app):
    today = date.today()
    with app.state.session_factory() as db:
        old = Series(name="Old", metron_id=1, match_status="matched", issues_synced=datetime.now(UTC),
                     locked_fields=[])  # fmt: skip
        cur = Series(name="Cur", metron_id=2, match_status="matched", issues_synced=datetime.now(UTC),
                     locked_fields=[])  # fmt: skip
        db.add_all([old, cur])
        db.add(Release(metron_issue_id=10, metron_series_id=1, series_name="Old", number="1",
                       store_date=today - timedelta(days=45)))  # fmt: skip
        db.add(Release(metron_issue_id=20, metron_series_id=2, series_name="Cur", number="1",
                       store_date=today + timedelta(days=2)))  # fmt: skip
        db.flush()
        assert rolling_selection(db, 10, datetime.now(UTC)) == [cur.id]


@respx.mock
async def test_releases_metron_no_longer_lists_are_removed(app):
    soon = (date.today() + timedelta(days=2)).isoformat()
    respx.get(f"{BASE_URL}issue/").mock(
        return_value=httpx.Response(
            200,
            json=page([{"id": 30, "number": "2", "store_date": soon, "series": {"id": 3, "name": "Kept"}}]),
        )  # fmt: skip
    )
    with app.state.session_factory() as db:
        db.add(Release(metron_issue_id=99, metron_series_id=9, series_name="Delayed", number="5",
                       store_date=date.today() + timedelta(days=3)))  # fmt: skip
        db.flush()
        async with client() as c:
            await update_releases(db, c)
        assert [r.series_name for r in db.scalars(select(Release))] == ["Kept"]


def test_add_form_folder_preview_uses_publisher(logged_in, app):
    with app.state.session_factory() as db:
        settings_store.put(db, "metron.username", "u")
        settings_store.put(db, "metron.password", "p")
        settings_store.put(db, "naming.folder_template", "{Publisher}/{Series Title} ({Series Year})")
        db.commit()
    with respx.mock:
        respx.get(f"{BASE_URL}series/500/").respond(200, json=SERIES_DETAIL)
        respx.get(f"{BASE_URL}series/").respond(
            200, json={"count": 0, "next": None, "results": []}
        )  # annuals
        page_html = logged_in.get("/add/500").text
    assert "Pub/Monthly (2025)" in page_html.replace("&#x2F;", "/")
    with app.state.session_factory() as db:
        assert db.scalar(select(Series)) is None  # the preview was not saved


def test_full_refresh_refused_while_one_series_refreshes(logged_in, app):
    with app.state.session_factory() as db:
        db.add(Job(kind="refresh", state="running", params={"series_ids": [1]}))
        db.commit()
    assert logged_in.post("/refresh").status_code == 409


def test_upcoming_includes_issues_with_only_a_cover_date(logged_in, app):
    with app.state.session_factory() as db:
        s = Series(name="Solicited", monitored=True, locked_fields=[])
        db.add_all([s, Issue(series=s, number="7", cover_date=date.today() + timedelta(days=20),
                             monitored=True, locked_fields=[])])  # fmt: skip
        db.commit()
    assert "Solicited" in logged_in.get("/releases").text


def test_each_install_gets_its_own_random_refresh_time(app):
    import random

    from fumora.scheduler import ensure_refresh_time

    with app.state.session_factory() as db:
        db.query(Setting).filter(Setting.key == "schedule.refresh_time").delete()
        db.commit()
        at = ensure_refresh_time(db, random.Random(7))
        hh, mm = at.split(":")
        assert 0 <= int(hh) < 24 and 0 <= int(mm) < 60
        assert ensure_refresh_time(db) == at  # kept, not re-rolled
        settings_store.put(db, "schedule.refresh_time", "")
        db.commit()
        assert ensure_refresh_time(db) == ""  # off stays off
    times = set()
    for seed in range(10):
        with app.state.session_factory() as db:
            db.query(Setting).filter(Setting.key == "schedule.refresh_time").delete()
            db.commit()
            times.add(ensure_refresh_time(db, random.Random(seed)))
    assert len(times) > 5
