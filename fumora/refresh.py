"""Keeping the catalog current from Metron (M3).

The daily refresh costs a predictable couple of hundred requests, whatever
Metron itself is doing (Metron's change feed can list 60,000+ edited issues in
a day after a bulk edit, so it isn't used):

1. Fetch Metron's release schedule (last week to three weeks ahead). Matched
   series with an issue in it are refreshed: that's how new issues arrive.
2. Refresh a rolling batch of other matched series, least recently checked
   first, to pick up corrections. Ended series are rechecked monthly.

It reads from Metron and writes Fumora's database; it never touches files.
"""

import asyncio
import time
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from . import settings_store
from .importer import _parse_date, apply_metron_series, log, sync_series_issues
from .jobs import JobContext
from .metron import MetronAuthError, MetronClient, MetronError, MetronPause, _open
from .models import Release, Series
from .sources import run_metron_checks

REFRESH = "refresh"
DEFAULT_BATCH = 50
ENDED_RECHECK = timedelta(days=30)
ACTIVE_RECHECK = timedelta(days=7)


def _utc(dt: datetime) -> datetime:
    """Comparable UTC datetime (SQLite returns naive values; Metron's carry an offset)."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


async def refresh_series(db: Session, client: MetronClient, series: Series) -> dict:
    """Re-read one matched series from Metron: its details and its issue list."""
    detail = await client.series(series.metron_id)
    apply_metron_series(db, series, detail)
    counts = await sync_series_issues(db, client, series)
    stale = 0
    for issue in series.issues:
        cached = (issue.metron_detail or {}).get("modified")
        if cached and issue.modified and _utc(datetime.fromisoformat(cached)) < _utc(issue.modified):
            # Metron changed this issue since we cached its record: fetch it
            # again the next time the file is tagged.
            issue.metron_detail = None
            issue.detail_fetched = None
            stale += 1
    series.issues_synced = datetime.now(UTC)
    db.flush()
    return {**counts, "stale": stale}


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def schedule_window(today: date | None = None) -> tuple[date, date]:
    """Last week's Monday to the Sunday three weeks ahead."""
    start = week_start(today or date.today()) - timedelta(days=7)
    return start, start + timedelta(days=34)


async def update_releases(db: Session, client: MetronClient, today: date | None = None) -> int:
    """Store Metron's schedule for the window; entries Metron no longer lists in
    it (moved or deleted) are removed."""
    start, end = schedule_window(today)
    n = 0
    seen: set[int] = set()
    async for mi in client.releases_between(start.isoformat(), end.isoformat()):
        seen.add(mi["id"])
        s = mi.get("series") or {}
        row = db.scalar(select(Release).where(Release.metron_issue_id == mi["id"]))
        if row is None:
            row = Release(metron_issue_id=mi["id"])
            db.add(row)
        row.metron_series_id = s.get("id")
        row.series_name = s.get("name") or mi.get("issue") or ""
        row.series_year = s.get("year_began")
        row.number = mi.get("number") or ""
        row.store_date = _parse_date(mi.get("store_date"))
        row.cover_date = _parse_date(mi.get("cover_date"))
        row.image = mi.get("image")
        row.fetched = datetime.now(UTC)
        n += 1
    # An empty answer is far likelier a Metron hiccup than an empty five weeks:
    # keep what we have rather than wipe the schedule.
    for gone in (
        []
        if not seen
        else db.scalars(
            select(Release).where(
                Release.store_date >= start, Release.store_date <= end, Release.metron_issue_id.not_in(seen)
            )
        )
    ):
        db.delete(gone)
    # Forget schedule entries older than eight weeks.
    cutoff = start - timedelta(days=49)
    for old in db.scalars(select(Release).where(Release.store_date < cutoff)):
        db.delete(old)
    db.flush()
    return n


async def _retrying(ctx: JobContext, call):
    while True:
        try:
            return await call()
        except MetronPause as exc:
            resume = datetime.fromtimestamp(exc.reset_at, UTC)
            ctx.update(message=f"{exc}; resuming {resume:%Y-%m-%d %H:%M} UTC")
            await asyncio.sleep(max(exc.reset_at - time.time(), 0) + 5)
            ctx.update(message=None)


def rolling_selection(db: Session, batch: int, now: datetime) -> list[int]:
    """Series to refresh today: those with an issue on the release schedule,
    then up to `batch` others that are due, least recently checked first."""
    start, end = schedule_window(now.date())
    on_schedule = set(
        db.scalars(
            select(Release.metron_series_id).where(Release.store_date >= start, Release.store_date <= end)
        )
    )
    matched = Series.match_status == "matched"
    releasing = db.scalars(
        select(Series.id).where(matched, Series.metron_id.in_(on_schedule)).order_by(Series.id)
    ).all()
    active_due = _naive(now - ACTIVE_RECHECK)
    ended_due = _naive(now - ENDED_RECHECK)
    due = db.scalars(
        select(Series.id)
        .where(
            matched,
            Series.id.not_in(releasing),
            or_(
                Series.issues_synced.is_(None),
                (Series.year_end.is_(None) & (Series.issues_synced < active_due)),
                (Series.year_end.is_not(None) & (Series.issues_synced < ended_due)),
            ),
        )
        .order_by(Series.issues_synced.is_not(None), Series.issues_synced)
        .limit(batch)
    ).all()
    return list(releasing) + list(due)


def _naive(dt: datetime) -> datetime:
    return dt.astimezone(UTC).replace(tzinfo=None)


def make_refresh_handler(make_client):
    async def run(ctx: JobContext) -> str:
        params = ctx.params()
        explicit = list(params.get("series_ids") or [])
        sf = ctx.session_factory
        started = datetime.now(UTC)
        with sf() as db:
            user, password, token = settings_store.metron_login(db)
            try:
                batch = int(settings_store.get(db, "schedule.refresh_batch") or DEFAULT_BATCH)
            except ValueError:
                batch = DEFAULT_BATCH
        if not settings_store.metron_usable(user, password, token):
            raise RuntimeError("No Metron API token (or username and password) is set")

        totals = {"series": 0, "new": 0, "stale": 0, "errors": 0}
        releases = 0
        async with _open(make_client, user, password, token) as client:
            if explicit:
                ids = explicit
            else:
                ctx.update(phase="releases", message="Fetching Metron's release schedule")
                with sf() as db:
                    releases = await _retrying(ctx, lambda d=db: update_releases(d, client))
                    db.commit()
                    ids = rolling_selection(db, batch, started)
                ctx.update(message=None)
            ctx.update(phase="series", progress=0, total=len(ids))
            for n, series_id in enumerate(ids, 1):
                with sf() as db:
                    series = db.get(Series, series_id)
                    if series is None or series.metron_id is None:
                        continue
                    try:
                        r = await _retrying(ctx, lambda s=series, d=db: refresh_series(d, client, s))
                        totals["series"] += 1
                        totals["new"] += r["new"]
                        totals["stale"] += r["stale"]
                        db.commit()
                    except MetronAuthError:
                        raise
                    except MetronError as exc:
                        db.rollback()
                        totals["errors"] += 1
                        log(db, f"Refresh failed for series {series_id}: {exc}", kind=REFRESH, level="error")
                        # Back of the queue, or a series that keeps failing (deleted on
                        # Metron, say) would take a slot of every daily batch for ever.
                        failed = db.get(Series, series_id)
                        if failed is not None:
                            failed.issues_synced = datetime.now(UTC)
                        db.commit()
                ctx.update(progress=n)
            checks = None
            if not explicit:
                ctx.update(phase="metron-check", message="Looking for ComicVine series on Metron")
                try:
                    checks = await run_metron_checks(ctx, client)
                except Exception as exc:  # noqa: BLE001 - the monthly check must not fail the refresh
                    with sf() as db:
                        log(db, f"Metron checks skipped today: {exc}", kind=REFRESH, level="error")
                        db.commit()
                ctx.update(message=None)

        summary = (
            f"Refreshed {totals['series']} series: {totals['new']} new issues, "
            f"{totals['stale']} issue records to re-fetch, {totals['errors']} errors"
            + (f"; {releases} scheduled releases" if not explicit else "")
        )
        if checks and any(checks.values()):
            summary += (
                f"; ComicVine series checked on Metron: {checks['moved']} moved, "
                f"{checks['review']} to review, {checks['absent'] + checks['rejected']} not there"
            )
        with sf() as db:
            if not explicit:
                settings_store.put_state(db, "refresh.last_success", started.isoformat())
            log(db, summary, kind=REFRESH)
            db.commit()
        return summary

    return run
