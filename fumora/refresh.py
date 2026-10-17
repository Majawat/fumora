"""Keeping the catalog current from Metron (M3).

The daily refresh asks Metron what changed since the last run (series and
issues with `modified_gt`), refreshes only the matched series among them, and
updates the release schedule. It reads from Metron and writes Fumora's
database; it never touches files.
"""

import asyncio
import time
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import settings_store
from .importer import _parse_date, apply_metron_series, log, sync_series_issues
from .jobs import JobContext
from .metron import MetronAuthError, MetronClient, MetronDailyLimitReached, MetronError
from .models import Release, Series

REFRESH = "refresh"
FIRST_LOOKBACK = timedelta(days=7)


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


async def changed_series_ids(client: MetronClient, since: datetime) -> set[int]:
    """Metron series that changed, or had an issue change, since `since`."""
    iso = since.astimezone(UTC).isoformat(timespec="seconds")
    ids = {s["id"] async for s in client.series_modified_since(iso)}
    async for issue in client.issues_modified_since(iso):
        sid = (issue.get("series") or {}).get("id")
        if sid:
            ids.add(sid)
    return ids


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


async def update_releases(db: Session, client: MetronClient, today: date | None = None) -> int:
    """Store Metron's schedule from last week to three weeks ahead."""
    start = week_start(today or date.today()) - timedelta(days=7)
    end = start + timedelta(days=34)
    n = 0
    async for mi in client.releases_between(start.isoformat(), end.isoformat()):
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
        except MetronDailyLimitReached as exc:
            resume = datetime.fromtimestamp(exc.reset_at, UTC)
            ctx.update(message=f"Metron daily limit reached; resuming {resume:%Y-%m-%d %H:%M} UTC")
            await asyncio.sleep(max(exc.reset_at - time.time(), 0) + 5)
            ctx.update(message=None)


def make_refresh_handler(make_client):
    async def run(ctx: JobContext) -> str:
        params = ctx.params()
        explicit = list(params.get("series_ids") or [])
        sf = ctx.session_factory
        started = datetime.now(UTC)
        with sf() as db:
            user = settings_store.get(db, "metron.username")
            password = settings_store.get(db, "metron.password")
            last = settings_store.get_state(db, "refresh.last_success")
        if not user or not password:
            raise RuntimeError("Metron username and password are not set")
        since = _utc(datetime.fromisoformat(last)) if last else started - FIRST_LOOKBACK

        totals = {"series": 0, "new": 0, "stale": 0, "errors": 0}
        async with make_client(user, password) as client:
            if explicit:
                ids = explicit
            else:
                ctx.update(
                    phase="changes", message=f"Asking Metron what changed since {since:%Y-%m-%d %H:%M} UTC"
                )
                changed = await _retrying(ctx, lambda: changed_series_ids(client, since))
                with sf() as db:
                    ids = db.scalars(
                        select(Series.id).where(
                            Series.match_status == "matched", Series.metron_id.in_(changed)
                        )
                    ).all()
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
                        db.commit()
                ctx.update(progress=n)
            releases = 0
            if not explicit:
                ctx.update(phase="releases")
                with sf() as db:
                    releases = await _retrying(ctx, lambda d=db: update_releases(d, client))
                    db.commit()

        summary = (
            f"Refreshed {totals['series']} series: {totals['new']} new issues, "
            f"{totals['stale']} issue records to re-fetch, {totals['errors']} errors"
            + (f"; {releases} scheduled releases" if not explicit else "")
        )
        with sf() as db:
            if not explicit:
                settings_store.put_state(db, "refresh.last_success", started.isoformat())
            log(db, summary, kind=REFRESH)
            db.commit()
        return summary

    return run
