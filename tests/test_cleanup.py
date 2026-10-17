import asyncio

import respx
from sqlalchemy import select

from fumora.library_jobs import make_retag_handler
from fumora.metron import BASE_URL, MetronClient, RateLimiter
from fumora.models import Issue, Job, Series
from fumora.ratings import issue_rating
from tests.helpers import metron_issue
from tests.test_m2_routes_jobs import _job, _setup


def test_library_and_jobs_pages(logged_in, app, tmp_path):
    _setup(app, tmp_path, confirmed=None)
    page = logged_in.get("/library").text
    assert "Matches to confirm" in page and "Files without full Metron tags" in page
    assert logged_in.post("/library/retag", data={"write": "true"}).status_code == 400  # no tick
    assert logged_in.get("/jobs").status_code == 200
    assert "No jobs yet" in logged_in.get("/jobs/table").text


def test_issue_edit_overrides_rating_and_adds_tags(logged_in, app, tmp_path):
    sid, _ = _setup(app, tmp_path)
    with app.state.session_factory() as db:
        iid = db.scalar(select(Issue.id))
    assert logged_in.get(f"/issues/{iid}").status_code == 200
    logged_in.post(f"/issues/{iid}/edit", data={"age_rating": "Explicit", "tags": "Explicit"})
    with app.state.session_factory() as db:
        i = db.get(Issue, iid)
        assert i.rating_override == "Explicit" and [t.name for t in i.tags] == ["Explicit"]
        assert issue_rating(i) == "Explicit"
    assert f'href="/issues/{iid}"' in logged_in.get(f"/series/{sid}").text


async def test_cancel_stops_a_running_job_and_it_does_not_resume(app):
    async def slow(ctx):
        await asyncio.sleep(30)
        return "finished"

    runner = app.state.jobs
    runner.register("slow", slow)
    job_id = runner.start("slow")
    await asyncio.sleep(0.05)
    assert runner.cancel(job_id)
    for _ in range(50):
        await asyncio.sleep(0.02)
        with app.state.session_factory() as db:
            if db.get(Job, job_id).state == "cancelled":
                break
    with app.state.session_factory() as db:
        assert db.get(Job, job_id).state == "cancelled"
    runner.resume_all()  # a cancelled job is not picked up again
    assert runner.active("slow") is None
    assert not runner.cancel(job_id)


@respx.mock
async def test_library_wide_preview_skips_unconfirmed(app, tmp_path):
    sid, _ = _setup(app, tmp_path, confirmed=None)
    route = respx.get(f"{BASE_URL}issue/186450/").respond(200, json=metron_issue(186450))
    handler = make_retag_handler(lambda u, p: MetronClient(u, p, RateLimiter(min_interval=0)))
    summary = await handler(_job(app, "retag", {"series_ids": [], "dry_run": True}))
    assert route.call_count == 0 and "0 files would change" in summary
    # Previewing that one series explicitly still works (it helps decide on the match).
    summary = await handler(_job(app, "retag", {"series_ids": [sid], "dry_run": True}))
    assert route.call_count == 1 and "1 files would change" in summary
    with app.state.session_factory() as db:
        assert db.get(Series, sid).confirmed_by is None
