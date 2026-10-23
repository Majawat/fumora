"""In-process scheduler: starts the daily Metron refresh at the configured time."""

import asyncio
import logging
import random
from datetime import UTC, datetime
from pathlib import Path

from . import backup, settings_store
from .auto_search import SEARCH
from .grabbing import check_downloads
from .models import Event, Setting
from .refresh import REFRESH
from .sources import CV_REFRESH

log = logging.getLogger("fumora.scheduler")
CHECK_EVERY = 300  # seconds


_FIRST_PICK = {
    "schedule.refresh_time": "Daily Metron refresh set to {at} UTC, picked at random so installs don't all "
    "hit Metron at once (Metron's request; the old default was 05:00). Change it in Settings.",
    "schedule.cv_refresh_time": "Daily ComicVine refresh set to {at} UTC, picked at random so installs "
    "don't all hit ComicVine at once. Change it in Settings.",
}


def ensure_refresh_time(
    db, rng: random.Random | None = None, key: str = "schedule.refresh_time"
) -> str | None:
    """Give this install its own random daily time (HH:MM UTC) for a refresh, once.

    Metron asked apps not to ship a fixed default time: every install would hit
    it at the same moment (ComicVine gets the same courtesy). Saved like a normal
    setting, so it shows on the Settings page and can be changed; an empty value
    (off) is left alone."""
    if db.get(Setting, key) is None:
        rng = rng or random.Random()
        at = f"{rng.randrange(24):02}:{rng.randrange(60):02}"
        settings_store.put(db, key, at)
        db.add(Event(kind="settings", message=_FIRST_PICK[key].format(at=at)))
        db.commit()
        log.info("%s set to %s UTC", key, at)
    return settings_store.get(db, key)


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


def backup_due(now: datetime, every_days: str | None, last: str | None) -> bool:
    try:
        days = float(every_days) if every_days else 0
    except ValueError:
        return False
    if days <= 0:
        return False
    return not last or (now - datetime.fromisoformat(last)).total_seconds() >= days * 86400


async def scheduled_backup(db, now: datetime) -> None:
    """Back up the live database into /config/backups and drop old scheduled ones."""
    path = Path(db.get_bind().url.database)
    folder = backup.backup_dir(path.parent)
    made = await asyncio.to_thread(backup.make_backup, path, folder, "scheduled")
    keep = int(settings_store.get(db, "backup.keep") or 5)
    gone = await asyncio.to_thread(backup.prune, folder, keep)
    settings_store.put_state(db, "schedule.backup.last", now.isoformat())
    db.add(Event(kind="backup", message=f"Backed up the database: {made.name}"
                 + (f" (removed {len(gone)} old)" if gone else "")))  # fmt: skip
    db.commit()


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


async def run_download_monitor(session_factory, jobs=None, every: int = 60, before_import=None) -> None:
    """Follow downloads every minute and import the finished ones; a failed one
    starts a new search for its issue (see grabbing.retry_candidates)."""

    pending: list[int] = []  # retries waiting for a running search to finish

    def search_again(issue_ids: list[int]) -> None:
        if jobs is None:
            return
        pending.extend(i for i in issue_ids if i not in pending)
        if jobs.active(SEARCH) is not None:
            log.info("download failed for %s; searching again once the running search ends", issue_ids)
            return
        jobs.start(SEARCH, {"issue_ids": list(pending), "retry": True})
        pending.clear()

    while True:
        try:
            if pending and jobs is not None and jobs.active(SEARCH) is None:
                jobs.start(SEARCH, {"issue_ids": list(pending), "retry": True})
                pending.clear()
            await check_downloads(session_factory, on_failed=search_again, before_import=before_import)
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
                cv_at = settings_store.get(db, "schedule.cv_refresh_time")
                cv_last = settings_store.get_state(db, "schedule.cv_refresh.last_day")
                if cv_last is None:
                    settings_store.put_state(db, "schedule.cv_refresh.last_day", now.date().isoformat())
                    db.commit()
                    cv_last = now.date().isoformat()
                if (
                    due(now, cv_at, cv_last)
                    and settings_store.is_set(db, "comicvine.api_key")
                    and jobs.active(CV_REFRESH) is None
                ):
                    settings_store.put_state(db, "schedule.cv_refresh.last_day", now.date().isoformat())
                    db.commit()
                    jobs.start(CV_REFRESH, {})
                    log.info("scheduled ComicVine refresh started")
                if backup_due(
                    now,
                    settings_store.get(db, "backup.interval_days"),
                    settings_store.get_state(db, "schedule.backup.last"),
                ):
                    try:
                        await scheduled_backup(db, now)
                    except Exception as exc:  # said out loud, retried next check
                        db.rollback()
                        db.add(Event(kind="backup", level="error", message=f"Scheduled backup failed: {exc}"))
                        db.commit()
                        log.exception("scheduled backup failed")
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
