"""Background jobs that work on library files: scan, retag, organize."""

import asyncio
import contextlib
import time
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from . import naming, settings_store
from .comicvine import ComicVineAuthError, ComicVineError, ComicVineRateLimited
from .importer import log
from .jobs import JobContext
from .metron import MetronAuthError, MetronClient, MetronError, MetronPause, _open
from .models import Issue, Series
from .scanner import scan_folders
from .tagging import ensure_cv_detail, ensure_detail, tag_file

SCAN, RETAG, ORGANIZE = "scan", "retag", "organize"


def _series_ids(ctx: JobContext) -> list[int]:
    return list(ctx.params().get("series_ids") or [])


@contextlib.asynccontextmanager
async def _maybe(client):
    """`async with` a client that may be missing."""
    if client is None:
        yield None
        return
    async with client:
        yield client


def make_scan_handler():
    async def run(ctx: JobContext) -> str:
        ids = _series_ids(ctx)
        ctx.update(phase="scan", message="Scanning series folders")
        with ctx.session_factory() as db:
            r = scan_folders(db, ids or None)
            summary = (
                f"Scanned {r.folders} folders: {r.linked} files linked, {len(r.unlinked)} unmatched, "
                f"{len(r.duplicates)} extra copies, {len(r.missing_folders)} folders missing"
            )
            log(db, summary, kind="scan")
            db.commit()
        return summary

    return run


def make_retag_handler(
    make_client: Callable[[str, str], MetronClient], make_cv_client: Callable | None = None
):
    async def run(ctx: JobContext) -> str:
        params = ctx.params()
        dry_run = bool(params.get("dry_run", True))
        ids = _series_ids(ctx)
        sf = ctx.session_factory
        with sf() as db:
            user, password, token = settings_store.metron_login(db)
            recycle = settings_store.get(db, "library.recycle_path")
            cv_key = settings_store.get(db, "comicvine.api_key")
            q = select(Issue.id).join(Series).where(Issue.files.any()).order_by(Series.id, Issue.sort_number)
            if ids:
                q = q.where(Series.id.in_(ids))
            if not ids or not dry_run:
                # Library-wide runs and writes skip unconfirmed matches entirely
                # (no Metron requests spent on files that can't be written).
                q = q.where(or_(Series.match_status != "matched", Series.confirmed_by.is_not(None)))
            issue_ids = db.scalars(q).all()
        if not settings_store.metron_usable(user, password, token):
            raise RuntimeError("No Metron API token (or username and password) is set")
        done = params.get("done_issue_ids") or []
        done_set = set(done)
        todo = [i for i in issue_ids if i not in done_set]
        ctx.update(phase="preview" if dry_run else "write", progress=len(done), total=len(issue_ids))
        counts = {"changed": 0, "unchanged": 0, "errors": 0}
        # Series Metron doesn't have get ComicVine's issue record instead.
        cv = make_cv_client(cv_key) if cv_key and make_cv_client is not None else None
        async with _open(make_client, user, password, token) as client, _maybe(cv):
            for n, issue_id in enumerate(todo, len(done) + 1):
                with sf() as db:
                    issue = db.scalar(
                        select(Issue)
                        .where(Issue.id == issue_id)
                        .options(
                            selectinload(Issue.files),
                            selectinload(Issue.tags),
                            selectinload(Issue.series).selectinload(Series.issues),
                            selectinload(Issue.series).selectinload(Series.tags),
                            selectinload(Issue.series).selectinload(Series.publisher),
                            selectinload(Issue.series).selectinload(Series.imprint),
                        )
                    )
                    while True:
                        try:
                            await ensure_detail(db, client, issue)
                            break
                        except MetronPause as exc:
                            resume = datetime.fromtimestamp(exc.reset_at, UTC)
                            ctx.update(
                                message=f"Metron daily limit reached; resuming {resume:%Y-%m-%d %H:%M} UTC"
                            )
                            await asyncio.sleep(max(exc.reset_at - time.time(), 0) + 5)
                        except MetronAuthError:
                            raise
                        except MetronError as exc:
                            log(
                                db,
                                f"No Metron detail for issue {issue_id}: {exc}",
                                kind="retag",
                                level="error",
                            )
                            break
                    if cv is not None and issue.metron_id is None and issue.cv_id:
                        try:
                            await ensure_cv_detail(db, cv, issue)
                        except ComicVineAuthError:
                            raise
                        except ComicVineRateLimited as exc:
                            log(db, f"{exc}; the rest get basic tags", kind="retag", level="error")
                            cv = None
                        except ComicVineError as exc:
                            log(
                                db,
                                f"No ComicVine detail for issue {issue_id}: {exc}",
                                kind="retag",
                                level="error",
                            )
                    for f in issue.files:
                        # File I/O off the event loop: archives can be large.
                        result = await asyncio.to_thread(tag_file, issue, f, dry_run, recycle)
                        if result["error"]:
                            counts["errors"] += 1
                        elif result["fields"]:
                            counts["changed"] += 1
                        else:
                            counts["unchanged"] += 1
                    db.commit()
                done.append(issue_id)
                if n % 10 == 0 or n == len(issue_ids):
                    ctx.update(progress=n, params={**params, "done_issue_ids": done})
        verb = "would change" if dry_run else "changed"
        summary = (
            f"{'Preview' if dry_run else 'Tagging'}: {counts['changed']} files {verb}, "
            f"{counts['unchanged']} already up to date, {counts['errors']} could not be tagged"
        )
        with sf() as db:
            log(db, summary, kind="retag")
            db.commit()
        return summary

    return run


def naming_settings(db) -> dict:
    return {
        "folder_template": settings_store.get(db, "naming.folder_template") or naming.DEFAULT_FOLDER,
        "file_template": settings_store.get(db, "naming.file_template") or naming.DEFAULT_FILE,
        "collected_template": settings_store.get(db, "naming.collected_template") or None,
        "colon": settings_store.get(db, "naming.colon") or "delete",
        "apostrophes": settings_store.get(db, "naming.apostrophes") or "remove",
    }


def shared_folders(db) -> set[str]:
    paths = db.scalars(select(Series.path).where(Series.path.is_not(None))).all()
    return {p for p in paths if paths.count(p) > 1}


def make_organize_handler():
    async def run(ctx: JobContext) -> str:
        ids = _series_ids(ctx)
        renamed, problems = 0, []
        with ctx.session_factory() as db:
            opts = naming_settings(db)
            shared = shared_folders(db)
            series_list = db.scalars(select(Series).where(Series.id.in_(ids))).all()
            ctx.update(phase="rename", progress=0, total=len(series_list))
            for n, series in enumerate(series_list, 1):
                if not series.writable:
                    problems.append(f"{series.name}: match not confirmed")
                    continue
                plan = naming.plan_series(series, shared_folder=series.path in shared, **opts)
                # apply_plan updates the database after each successful rename and
                # reports the rest, so the database always matches the disk.
                done_count, skipped = await asyncio.to_thread(naming.apply_plan, db, series, plan)
                renamed += done_count
                problems += [f"{series.name}: {p}" for p in skipped]
                db.commit()
                ctx.update(progress=n)
            summary = f"Renamed {renamed} files/folders" + (f"; {len(problems)} skipped" if problems else "")
            log(db, summary + (": " + "; ".join(problems[:5]) if problems else ""), kind="organize")
            db.commit()
        return summary

    return run
