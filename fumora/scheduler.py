"""In-process scheduler: starts the daily Metron refresh at the configured time."""

import asyncio
import logging
import random
from datetime import UTC, datetime

from . import settings_store
from .auto_search import SEARCH
from .grabbing import check_downloads
from .models import Event, Setting
from .refresh import REFRESH

log = logging.getLogger("fumora.scheduler")
CHECK_EVERY = 300  # seconds


def ensure_refresh_time(db, rng: random.Random | None = None) -> str | None:
    """Give this install its own random daily refresh time (HH:MM UTC), once.

    Metron asked apps not to ship a fixed default time: every install would hit
    it at the same moment. Saved like a normal setting, so it shows on the
    Settings page and can be changed; an empty value (off) is left alone."""
    if db.get(Setting, "schedule.refresh_time") is None:
        rng = rng or random.Random()
        at = f"{rng.randrange(24):02}:{rng.randrange(60):02}"
        settings_store.put(db, "schedule.refresh_time", at)
        db.add(
            Event(
                kind="settings",
                message=f"Daily Metron refresh set to {at} UTC, picked at random so installs don't all "
                "hit Metron at once (Metron's request; the old default was 05:00). Change it in Settings.",
            )
        )
        db.commit()
        log.info("daily Metron refresh time set to %s UTC", at)
    return settings_store.get(db, "schedule.refresh_time")


def due(now: datetime, at: str | None, last_run_day: str | None) -> bool:
    """True when today's run time (UTC, 'HH:MM') has passed and it hasn't run today."""
    if not at:
        return False
    try:
        hour, minute = (int(x) for x in at.strip().split(":"))
    except ValueError:
        return False
    if last_run_day == now.date().isoformat():
        return False
    return (now.hour, now.minute) >= (hour, minute)


def search_due(now: datetime, enabled: str | None, every_hours: str | None, last: str | None) -> bool:
    if enabled != "on":
        return False
    try:
        hours = max(1.0, float(every_hours or 6))
    except ValueError:
        hours = 6.0
    if not last:
        return True
    return (now - datetime.fromisoformat(last)).total_seconds() >= hours * 3600


async def run_download_monitor(session_factory, jobs=None, every: int = 60) -> None:
    """Follow downloads every minute and import the finished ones; a failed one
    starts a new search for its issue (see grabbing.retry_candidates)."""

    def search_again(issue_ids: list[int]) -> None:
        if jobs is None:
            return
        if jobs.active(SEARCH) is not None:
            log.info("download failed for %s; a search is running, so not searching again now", issue_ids)
            return
        jobs.start(SEARCH, {"issue_ids": issue_ids, "retry": True})

    while True:
        try:
            await check_downloads(session_factory, on_failed=search_again)
        except Exception:
            log.exception("download check failed")
        await asyncio.sleep(every)


async def run_scheduler(session_factory, jobs, check_every: int = CHECK_EVERY) -> None:
    while True:
        try:
            now = datetime.now(UTC)
            with session_factory() as db:
                at = settings_store.get(db, "schedule.refresh_time")
                last = settings_store.get_state(db, "schedule.refresh.last_day")
                if last is None:
                    # First start: begin with the next scheduled time rather than
                    # spending Metron requests right away ("Refresh now" exists).
                    settings_store.put_state(db, "schedule.refresh.last_day", now.date().isoformat())
                    db.commit()
                    last = now.date().isoformat()
                if due(now, at, last) and jobs.active(REFRESH) is None:
                    settings_store.put_state(db, "schedule.refresh.last_day", now.date().isoformat())
                    db.commit()
                    jobs.start(REFRESH, {})
                    log.info("scheduled refresh started")
                last_search = settings_store.get_state(db, "schedule.search.last")
                if (
                    search_due(
                        now,
                        settings_store.get(db, "search.auto"),
                        settings_store.get(db, "search.interval_hours"),
                        last_search,
                    )
                    and jobs.active(SEARCH) is None
                ):
                    settings_store.put_state(db, "schedule.search.last", now.isoformat())
                    db.commit()
                    jobs.start(SEARCH, {})
                    log.info("scheduled Wanted search started")
        except Exception:  # never let the scheduler die
            log.exception("scheduler check failed")
        await asyncio.sleep(check_every)
