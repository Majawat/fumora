"""In-process scheduler: starts the daily Metron refresh at the configured time."""

import asyncio
import logging
from datetime import UTC, datetime

from . import settings_store
from .auto_search import SEARCH
from .grabbing import check_downloads
from .refresh import REFRESH

log = logging.getLogger("fumora.scheduler")
CHECK_EVERY = 300  # seconds


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


async def run_download_monitor(session_factory, every: int = 60) -> None:
    """Follow downloads every minute and import the finished ones."""
    while True:
        try:
            await check_downloads(session_factory)
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
