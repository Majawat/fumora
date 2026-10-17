"""Regression tests for code-review findings on M1."""

import time

import httpx
import pytest
import respx
from sqlalchemy import select

from fumora import settings_store
from fumora.import_job import make_handler
from fumora.importer import upsert_mylar
from fumora.jobs import JobContext
from fumora.metron import BASE_URL, MetronAuthError, MetronClient, MetronError, RateLimiter
from fumora.models import Issue, Job, Series
from fumora.mylar import MylarIssue, MylarSeries


def test_start_button_spawns_job(logged_in, app, tmp_path):
    """POST /import/start must actually start the job, not leave it 'queued'."""
    with app.state.session_factory() as db:
        settings_store.put(db, "mylar.db_path", str(tmp_path / "missing.db"))
        settings_store.put(db, "metron.username", "u")
        settings_store.put(db, "metron.password", "p")
        db.commit()
    r = logged_in.post("/import/start")
    assert r.status_code == 200
    deadline = time.time() + 5
    while time.time() < deadline:
        job = app.state.jobs.latest("mylar_import")
        if job.state not in ("queued", "running"):
            break
        logged_in.get("/import/status")  # lets the event loop run the task
    assert job.state == "failed" and "Mylar database not found" in job.message


@respx.mock
async def test_auth_error_stops_job_and_leaves_series_pending(app):
    respx.get(f"{BASE_URL}series/").respond(401)
    sf = app.state.session_factory
    with sf() as db:
        settings_store.put(db, "metron.username", "u")
        settings_store.put(db, "metron.password", "wrong")
        db.add(Series(name="A", cv_id=1, locked_fields=[]))
        job = Job(kind="mylar_import", params={"skip_read": True})
        db.add(job)
        db.commit()
        job_id = job.id
    limiter = RateLimiter(min_interval=0)
    handler = make_handler(app.state.config.config_dir, lambda u, p: MetronClient(u, p, limiter))
    with pytest.raises(MetronAuthError):
        await handler(JobContext(job_id, sf))
    with sf() as db:
        assert db.scalar(select(Series)).match_status == "pending"


@respx.mock
async def test_network_errors_retry_then_raise_metron_error():
    slept = []
    now = [1000.0]

    async def fake_sleep(s):
        slept.append(s)
        now[0] += s

    route = respx.get(f"{BASE_URL}series/5/")
    route.side_effect = [httpx.ReadTimeout("t"), httpx.Response(200, json={"id": 5})]
    limiter = RateLimiter(min_interval=0, sleep=fake_sleep, clock=lambda: now[0])
    async with MetronClient("u", "p", limiter) as client:
        assert (await client.series(5))["id"] == 5
        route.side_effect = httpx.ConnectError("down")
        with pytest.raises(MetronError):
            await client.series(5)
    assert slept


def test_same_number_different_cv_issue_is_not_folded(app):
    ms = MylarSeries(
        cv_id=10,
        name="S",
        sort_name=None,
        year=2000,
        publisher=None,
        path="/data/media/Comics/S",
        active=True,
        mylar_type="Print",
        age_rating=None,
        issues=[
            MylarIssue(101, "1", None, None, None, "Downloaded", "/data/media/Comics/S/a.cbz"),
            MylarIssue(102, "1", None, None, None, "Downloaded", "/data/media/Comics/S/b.cbz"),
        ],
    )
    with app.state.session_factory() as db:
        stats = upsert_mylar(db, [ms], exists=lambda p: True)
        db.commit()
        issues = db.scalars(select(Issue)).all()
        assert len(issues) == 1 and issues[0].cv_id == 101
        assert stats.issues_conflict == 1 and stats.files_linked == 1
