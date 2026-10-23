"""RSS sync: Prowlarr's newest releases matched against Wanted issues, each release once."""

from datetime import UTC, datetime, timedelta

import respx
from sqlalchemy import func, select

from fumora import settings_store
from fumora.models import Grab, Job
from fumora.rss import make_rss_handler
from fumora.scheduler import rss_due
from tests.test_downloads import PROWLARR, SAB, _issue, _release, _setup
from tests.test_m2_routes_jobs import _job

FEED = [
    _release("Absolute Batman 016 [2026] [Digital] [Shan-Empire]", guid="ab16"),
    _release("Absolute Batman 018 [2026] [Digital] [Shan-Empire]", guid="ab18"),  # not wanted
    _release("Saga 070 (2026) (Digital) (Zone-Empire)", guid="saga70"),  # not followed
    _release("Some Novel by Someone [ENG / EPUB]", guid="book"),
]


@respx.mock
async def test_rss_grabs_wanted_issues_from_the_newest_releases(app, tmp_path):
    _setup(app, tmp_path, numbers=("16", "17"))
    feed = respx.get(f"{PROWLARR}/api/v1/search", params={"query": ""}).respond(200, json=FEED)
    add = respx.get(f"{SAB}/api", params={"mode": "addurl"}).respond(
        200, json={"status": True, "nzo_ids": ["n"]}
    )
    summary = await make_rss_handler()(_job(app, "rss", {}))
    assert "4 new releases" in summary and "grabbed 1" in summary
    assert feed.call_count == 1 and add.call_count == 1
    with app.state.session_factory() as db:
        g = db.scalar(select(Grab))
        assert g.guid == "ab16" and g.issue_id == _issue(db, "16").id
    # Each release is looked at once: the next run sees nothing new.
    summary = await make_rss_handler()(_job(app, "rss", {}))
    assert "none new" in summary and add.call_count == 1


@respx.mock
async def test_rss_follows_the_same_rules_as_the_search(app, tmp_path):
    _setup(app, tmp_path, numbers=("16",))
    with app.state.session_factory() as db:
        settings_store.put(db, "search.delay_hours", "48")
        _issue(db, "16").cover_date = datetime.now(UTC).date()  # inside the post-release delay
        db.commit()
    respx.get(f"{PROWLARR}/api/v1/search").respond(200, json=FEED)
    add = respx.get(f"{SAB}/api", params={"mode": "addurl"}).respond(
        200, json={"status": True, "nzo_ids": ["n"]}
    )
    summary = await make_rss_handler()(_job(app, "rss", {}))
    assert "grabbed 0" in summary and add.call_count == 0
    with app.state.session_factory() as db:
        assert db.scalar(select(func.count(Grab.id))) == 0


def test_rss_schedule():
    now = datetime(2026, 10, 23, 12, tzinfo=UTC)
    assert rss_due(now, "on", "30", None)
    assert not rss_due(now, "on", "30", (now - timedelta(minutes=20)).isoformat())
    assert rss_due(now, "on", "30", (now - timedelta(minutes=31)).isoformat())
    assert not rss_due(now, "on", "", None)  # off
    assert not rss_due(now, "off", "30", None)  # automatic search off: no RSS either


def test_jobs_page_keeps_only_the_latest_rss_run(app):
    with app.state.session_factory() as db:
        db.add(Job(kind="scan", state="done"))
        for _ in range(40):
            db.add(Job(kind="rss", state="done"))
        db.commit()
    kinds = [j.kind for j in app.state.jobs.recent()]
    assert kinds.count("rss") == 1 and "scan" in kinds


class _Jobs:
    def __init__(self):
        self.started = []

    def active(self, kind):
        return None

    def start(self, kind, params=None):
        self.started.append(kind)
        return 1


async def test_one_scheduler_pass_starts_rss_and_backs_up(app, tmp_path, config):
    import asyncio

    from fumora import backup
    from fumora.scheduler import run_scheduler

    _setup(app, tmp_path)
    with app.state.session_factory() as db:
        settings_store.put(db, "search.auto", "on")
        db.commit()
    jobs = _Jobs()
    task = asyncio.create_task(run_scheduler(app.state.session_factory, jobs, check_every=3600))
    for _ in range(100):
        await asyncio.sleep(0.02)
        if "rss" in jobs.started and backup.list_backups(backup.backup_dir(config.config_dir)):
            break
    task.cancel()
    assert "rss" in jobs.started
    assert [b.label for b in backup.list_backups(backup.backup_dir(config.config_dir))] == ["scheduled"]
    with app.state.session_factory() as db:
        settings_store.put(db, "search.rss_minutes", "")  # off
        settings_store.put_state(db, "schedule.rss.last", None)
        db.commit()
    jobs = _Jobs()
    task = asyncio.create_task(run_scheduler(app.state.session_factory, jobs, check_every=3600))
    await asyncio.sleep(0.3)
    task.cancel()
    assert "rss" not in jobs.started
