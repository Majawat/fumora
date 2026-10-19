"""The 'mylar_import' background job (see importer.py for the steps)."""

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from . import settings_store
from .importer import (
    import_report,
    log,
    match_series,
    review_confirmations,
    snapshot_path,
    sync_series_issues,
    upsert_mylar,
)
from .jobs import JobContext
from .metron import MetronAuthError, MetronClient, MetronError, MetronPause, _open
from .models import Series
from .mylar import read_mylar, snapshot

KIND = "mylar_import"


async def _with_daily_limit(ctx: JobContext, call):
    """Run `call()`; if Metron's daily budget is spent, wait for the reset and retry."""
    while True:
        try:
            return await call()
        except MetronPause as exc:
            resume = datetime.fromtimestamp(exc.reset_at, UTC)
            ctx.update(message=f"{exc}; resuming {resume:%Y-%m-%d %H:%M} UTC")
            await asyncio.sleep(max(exc.reset_at - time.time(), 0) + 5)
            ctx.update(message=None)


def make_handler(config_dir: Path, make_client: Callable[[str, str], MetronClient]):
    async def run(ctx: JobContext) -> str:
        sf = ctx.session_factory
        with sf() as db:
            mylar_db = Path(settings_store.get(db, "mylar.db_path") or "")
            user, password, token = settings_store.metron_login(db)
        if not settings_store.metron_usable(user, password, token):
            raise RuntimeError("No Metron API token (or username and password) is set")

        # Phase 1: read Mylar (skipped for jobs that only apply accepted suggestions)
        params = ctx.params()
        if not params.get("read_done") and not params.get("skip_read"):
            if not mylar_db.is_file():
                raise RuntimeError(f"Mylar database not found at {mylar_db}")
            ctx.update(phase="read", message="Copying and reading Mylar's database")
            snap = snapshot_path(config_dir)
            snapshot(mylar_db, snap)
            mylar_series = read_mylar(snap)
            with sf() as db:
                stats = upsert_mylar(db, mylar_series)
                log(
                    db,
                    f"Read Mylar: {stats.series_seen} series ({stats.series_new} new), "
                    f"{stats.issues_new} new issues, {stats.files_linked} files linked, "
                    f"{stats.files_missing} recorded files missing on disk, "
                    f"{stats.issues_conflict} issues skipped (number already used by another issue)",
                )
                db.commit()
            ctx.update(params={**ctx.params(), "read_done": True}, message=None)

        async with _open(make_client, user, password, token) as client:
            # Phases 2 and 3 repeat until nothing is left, so a suggestion
            # accepted while this job runs is still applied by it.
            while True:
                with sf() as db:
                    to_match = db.scalars(
                        select(Series.id)
                        .where(Series.match_status.in_(("pending", "accepted")))
                        .order_by(Series.id)
                    ).all()
                    to_sync = db.scalars(
                        select(Series.id)
                        .where(Series.match_status == "matched", Series.issues_synced.is_(None))
                        .order_by(Series.id)
                    ).all()
                if not to_match and not to_sync:
                    break

                # Phase 2: find each series on Metron
                ctx.update(phase="match", progress=0, total=len(to_match))
                for n, series_id in enumerate(to_match, 1):
                    with sf() as db:
                        series = db.get(Series, series_id)
                        try:
                            await _with_daily_limit(ctx, lambda s=series, d=db: match_series(d, client, s))
                        except MetronAuthError:
                            # Wrong credentials affect every series: stop, leave them pending.
                            raise
                        except MetronError as exc:
                            series.match_status, series.match_note = "review", f"Metron error: {exc}"
                        db.commit()
                    ctx.update(progress=n)

                # Phase 3: pair issues for matched series
                with sf() as db:
                    to_sync = db.scalars(
                        select(Series.id)
                        .where(Series.match_status == "matched", Series.issues_synced.is_(None))
                        .order_by(Series.id)
                    ).all()
                ctx.update(phase="issues", progress=0, total=len(to_sync))
                for n, series_id in enumerate(to_sync, 1):
                    with sf() as db:
                        series = db.get(Series, series_id)
                        try:
                            await _with_daily_limit(
                                ctx, lambda s=series, d=db: sync_series_issues(d, client, s)
                            )
                            series.issues_synced = datetime.now(UTC)
                            db.commit()
                        except MetronAuthError:
                            raise
                        except MetronError as exc:
                            db.rollback()
                            series = db.get(Series, series_id)
                            # Mark it synced-with-error so the loop does not retry it forever.
                            series.issues_synced = datetime.now(UTC)
                            series.match_note = f"Issue sync failed: {exc}"
                            db.commit()
                    ctx.update(progress=n)

        with sf() as db:
            review_confirmations(db)
            db.commit()
            r = import_report(db)
            summary = (
                f"{r['series_matched']}/{r['series_total']} series matched, "
                f"{r['series_review']} need review, {r['series_not_found']} not on Metron; "
                f"{r['issues_matched']} issues matched, {r['issues_unmatched']} unmatched, "
                f"{r['issues_new']} new from Metron; "
                f"{r['series_unconfirmed']} matches need confirming"
            )
            log(db, f"Mylar import finished: {summary}")
            db.commit()
        return summary

    return run
