"""Series that follow a source other than Metron (M5b; design doc "Other metadata
sources" and "Moving a series to Metron").

- ComicVine refresh: a series Metron doesn't have keeps its issue list current
  from ComicVine (new volumes of a manga, corrected dates). Only followed series,
  one request per series (100 issues a page), at the install's own random time.
- Move to Metron: once a month each ComicVine series is looked up on Metron by
  its ComicVine id. It moves only when the id, the series (name, start year,
  publisher) and every issue that has a file agree; anything less becomes a
  suggestion for the owner. A Metron series the owner rejected is never
  suggested again.

Both only read the services and write Fumora's database; never files.
"""

import os
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from . import monitoring, settings_store
from .comicvine import ComicVineAuthError, ComicVineClient, ComicVineError, ComicVineRateLimited, start_year
from .importer import (
    _name_key,
    _parse_date,
    _taken_by,
    _upsert_publisher,
    apply_listed_values,
    apply_metron_series,
    listed_issue,
    log,
    sync_series_issues,
)
from .jobs import JobContext
from .metron import MetronAuthError, MetronClient, MetronPause
from .models import File, Issue, Series
from .numbers import normalize_number

CV_REFRESH = "cv_refresh"
DEFAULT_CV_BATCH = 30
MONITORED_RECHECK = timedelta(days=7)
OTHER_RECHECK = timedelta(days=30)
METRON_RECHECK = timedelta(days=30)
METRON_CHECK_BATCH = 10  # ComicVine series looked up on Metron per daily refresh
COVER_SLACK_DAYS = 92  # ComicVine and Metron cover dates may differ by a month or two
CHECK_NOTE = "Metron has this ComicVine id"


def _naive(dt: datetime) -> datetime:
    return dt.astimezone(UTC).replace(tzinfo=None)


def follows_comicvine():
    return (Series.source == "cv") & Series.cv_id.is_not(None) & (Series.match_status != "matched")


# --- ComicVine refresh -------------------------------------------------------------


def apply_cv_issues(db: Session, series: Series, cv_issues: list[dict]) -> dict[str, int]:
    """Pair our issues with ComicVine's by number; add issues only ComicVine has.
    Fields the user locked are left alone."""
    counts = {"new": 0, "updated": 0}
    by_number: dict[str, Issue] = {}
    for issue in series.issues:
        by_number.setdefault(normalize_number(issue.number), issue)
    for ci in cv_issues:
        number = (ci.get("issue_number") or "").strip()
        if not number:
            continue
        cover, store = _parse_date(ci.get("cover_date")), _parse_date(ci.get("store_date"))
        issue, created = listed_issue(db, series, by_number, number, store or cover)
        if issue.cv_id is None:
            issue.cv_id = ci.get("id")
        updated = ci.get("date_last_updated")
        if issue.cv_detail and issue.cv_id == ci.get("id") and updated:
            if updated != issue.cv_detail.get("date_last_updated"):
                issue.cv_detail = None  # ComicVine changed it: fetched again before the next tagging
        changed = apply_listed_values(
            issue, {"cover_date": cover, "store_date": store, "title": ci.get("name")}
        )
        counts["new" if created else "updated"] += int(created or changed)
    series.issues_synced = datetime.now(UTC)
    db.flush()
    return counts


async def sync_cv_issues(db: Session, cv: ComicVineClient, series: Series) -> dict[str, int]:
    return apply_cv_issues(db, series, [ci async for ci in cv.volume_issues(series.cv_id)])


def cv_selection(db: Session, batch: int, now: datetime) -> list[int]:
    """ComicVine series due a refresh: monitored ones weekly, others monthly,
    never-synced first, at most `batch`."""
    due = or_(
        Series.issues_synced.is_(None),
        Series.monitored.is_(True) & (Series.issues_synced < _naive(now - MONITORED_RECHECK)),
        Series.issues_synced < _naive(now - OTHER_RECHECK),
    )
    return list(
        db.scalars(
            select(Series.id)
            .where(follows_comicvine(), due)
            .order_by(Series.monitored.desc(), Series.issues_synced.is_not(None), Series.issues_synced)
            .limit(batch)
        )
    )


def make_cv_refresh_handler(make_cv_client):
    async def run(ctx: JobContext) -> str:
        params = ctx.params()
        explicit = list(params.get("series_ids") or [])
        sf = ctx.session_factory
        with sf() as db:
            key = settings_store.get(db, "comicvine.api_key")
            try:
                batch = int(settings_store.get(db, "schedule.cv_refresh_batch") or DEFAULT_CV_BATCH)
            except ValueError:
                batch = DEFAULT_CV_BATCH
            ids = explicit or cv_selection(db, batch, datetime.now(UTC))
            targets = {
                s.id: (s.cv_id, s.name)
                for s in db.scalars(select(Series).where(Series.id.in_(ids), follows_comicvine()))
            }
        if not key:
            raise RuntimeError("No ComicVine API key is set (Settings)")
        totals = {"series": 0, "new": 0, "updated": 0, "errors": 0}
        ctx.update(phase="series", progress=0, total=len(ids))
        async with make_cv_client(key) as cv:
            for n, series_id in enumerate(ids, 1):
                if series_id not in targets:
                    continue
                cv_id, name = targets[series_id]
                try:
                    # No database session while waiting on ComicVine (20 s apart):
                    # the series may move to Metron meanwhile, checked below.
                    cv_issues = [ci async for ci in cv.volume_issues(cv_id)]
                except ComicVineAuthError:
                    raise
                except ComicVineError as exc:
                    totals["errors"] += 1
                    with sf() as db:
                        log(db, f"ComicVine refresh failed for '{name}': {exc}", kind=CV_REFRESH,
                            level="error", series_id=series_id)  # fmt: skip
                        db.commit()
                    if isinstance(exc, ComicVineRateLimited):
                        break  # every request is held for an hour: stop here
                    continue
                with sf() as db:
                    series = db.get(Series, series_id)
                    if series is None or series.source != "cv" or series.match_status == "matched":
                        continue  # moved to Metron while we waited
                    r = apply_cv_issues(db, series, cv_issues)
                    totals["series"] += 1
                    totals["new"] += r["new"]
                    totals["updated"] += r["updated"]
                    if r["new"]:
                        log(
                            db, f"ComicVine lists {r['new']} new issues", kind=CV_REFRESH, series_id=series_id
                        )
                    db.commit()
                ctx.update(progress=n)
        summary = (
            f"ComicVine: refreshed {totals['series']} series, {totals['new']} new issues, "
            f"{totals['updated']} updated, {totals['errors']} errors"
        )
        with sf() as db:
            log(db, summary, kind=CV_REFRESH)
            db.commit()
        return summary

    return run


# --- Moving a series to Metron ---------------------------------------------------------


def metron_check_selection(db: Session, now: datetime, batch: int = METRON_CHECK_BATCH) -> list[int]:
    """ComicVine series due their monthly look on Metron, monitored first."""
    due = or_(Series.metron_checked.is_(None), Series.metron_checked < _naive(now - METRON_RECHECK))
    return list(
        db.scalars(
            select(Series.id)
            .where(follows_comicvine(), Series.suggested_metron_id.is_(None), due)
            .order_by(Series.monitored.desc(), Series.metron_checked.is_not(None), Series.metron_checked)
            .limit(batch)
        )
    )


def series_disagreement(series: Series, detail: dict) -> str | None:
    """Why Metron's series isn't this one (name, start year, publisher), or None."""
    ours = {_name_key(n) for n in (series.name, series.mylar_name) if n}
    if _name_key(detail.get("name")) not in ours:
        return f"Metron's name is '{detail.get('name')}', ours '{series.name}'"
    year, theirs = series.year_began or series.mylar_year, detail.get("year_began")
    if year and theirs and abs(year - theirs) > 1:
        return f"Metron's series starts in {theirs}, ours in {year}"
    mine = series.publisher.name if series.publisher else None
    other = (detail.get("publisher") or {}).get("name")
    if mine and other and _name_key(mine) not in _name_key(other) and _name_key(other) not in _name_key(mine):
        return f"Metron's publisher is {other}, ours {mine}"
    return None


def pairing_problems(series: Series, metron_issues: list[dict]) -> list[str]:
    """Issues with a file that don't pair with exactly one Metron issue (by number,
    cover date within a few months)."""
    by_number: dict[str, list[dict]] = {}
    for mi in metron_issues:
        by_number.setdefault(normalize_number(mi.get("number")), []).append(mi)
    problems = []
    for issue in sorted(series.issues, key=lambda i: i.sort_number or 0):
        if not issue.files:
            continue
        found = by_number.get(normalize_number(issue.number), [])
        if len(found) != 1:
            problems.append(f"#{issue.number} {'is not on Metron' if not found else 'is there twice'}")
            continue
        theirs = _parse_date(found[0].get("cover_date"))
        if issue.cover_date and theirs and abs((issue.cover_date - theirs).days) > COVER_SLACK_DAYS:
            problems.append(
                f"#{issue.number} is dated {theirs:%Y-%m} on Metron, {issue.cover_date:%Y-%m} here"
            )
    return problems


async def move_to_metron(db: Session, client: MetronClient, series: Series, detail: dict, note: str) -> None:
    """Make Metron the series' source: its details, its issue list (paired by number)."""
    apply_metron_series(db, series, detail)
    series.match_status, series.suggested_metron_id, series.match_note = "matched", None, note
    await sync_series_issues(db, client, series)
    series.issues_synced = datetime.now(UTC)


async def check_on_metron(db: Session, client: MetronClient, series: Series) -> str:
    """One ComicVine series' monthly look on Metron. Returns absent, rejected,
    moved or review."""
    series.metron_checked = datetime.now(UTC)
    found = await client.series_by_cv_id(series.cv_id)
    if found is None:
        return "absent"
    metron_id = found["id"]
    if metron_id in (series.rejected_metron_ids or []):
        return "rejected"

    def review(reason: str) -> str:
        series.match_status = "review"
        series.suggested_metron_id = metron_id
        series.match_note = f"{CHECK_NOTE} (series {metron_id}), but {reason}. Accept or reject."
        log(db, f"'{series.name}' may be on Metron now: {reason}", kind="metron_check", series_id=series.id)
        return "review"

    if (taken := _taken_by(db, metron_id, series)) is not None:
        return review(f"that Metron series is already '{taken.name}' here")
    detail = await client.series(metron_id)
    if reason := series_disagreement(series, detail):
        return review(reason)
    issues = [mi async for mi in client.series_issues(metron_id)]
    if problems := pairing_problems(series, issues):
        shown = "; ".join(problems[:3]) + (f" (+{len(problems) - 3} more)" if len(problems) > 3 else "")
        return review(f"the numbering differs: {shown}")
    await move_to_metron(
        db,
        client,
        series,
        detail,
        "Moved from ComicVine: Metron has its id, and the series and every issue agree",
    )
    series.confirmed_by = "auto"
    log(db, f"'{series.name}' moved to Metron (series {metron_id}); preview its tags when you like",
        kind="metron_check", series_id=series.id)  # fmt: skip
    return "moved"


async def accept_suggestion(db: Session, client: MetronClient, series: Series) -> None:
    """The owner accepts a Metron series suggested for a ComicVine one: move it
    now (the owner decided, so the checks that held it back don't apply)."""
    metron_id = series.suggested_metron_id
    if (taken := _taken_by(db, metron_id, series)) is not None:
        raise ValueError(f"Metron series {metron_id} is already '{taken.name}' here")
    detail = await client.series(metron_id)
    await move_to_metron(
        db, client, series, detail, f"Moved to Metron by hand (was: {series.match_note})"[:500]
    )
    series.confirmed_by = "user"
    log(db, f"'{series.name}' moved to Metron by hand (series {metron_id})", series_id=series.id)


def reject_metron_match(series: Series) -> None:
    """The owner says the Metron series isn't this one: remember it."""
    for metron_id in (series.metron_id, series.suggested_metron_id):
        if metron_id and metron_id not in (series.rejected_metron_ids or []):
            series.rejected_metron_ids = [*(series.rejected_metron_ids or []), metron_id]


async def run_metron_checks(ctx: JobContext, client: MetronClient) -> dict[str, int]:
    """The monthly look for a batch of ComicVine series (part of the daily refresh).

    Low priority: a Metron pause or daily limit ends it for today instead of
    waiting, and one series' error never stops the rest (or the refresh)."""
    sf = ctx.session_factory
    with sf() as db:
        ids = metron_check_selection(db, datetime.now(UTC))
    counts = {"absent": 0, "rejected": 0, "moved": 0, "review": 0, "errors": 0}
    for series_id in ids:
        with sf() as db:
            series = db.get(Series, series_id)
            if series is None:
                continue
            try:
                counts[await check_on_metron(db, client, series)] += 1
                db.commit()
            except (MetronAuthError, MetronPause):
                db.rollback()
                break
            except Exception as exc:  # noqa: BLE001 - logged; the refresh carries on
                db.rollback()
                counts["errors"] += 1
                log(
                    db,
                    f"Metron check failed for series {series_id}: {exc}",
                    kind="metron_check",
                    level="error",
                )
                db.commit()
    return counts


# --- Adding a series from ComicVine ------------------------------------------------------------


async def add_cv_series(
    db: Session, cv: ComicVineClient, cv_id: int, option: str = "future", monitor_new: bool = True
) -> Series:
    """Create a series that follows ComicVine (one Metron doesn't have), list its
    issues, set monitoring, link files if its folder exists. Touches no files."""
    from .catalog import folder_for
    from .scanner import scan_folders

    existing = db.scalar(select(Series).where(Series.cv_id == cv_id))
    if existing is not None:
        return existing
    vol = await cv.volume(cv_id)
    if vol is None:
        raise ComicVineError(f"ComicVine has no volume {cv_id}")
    series = Series(
        name=vol.get("name") or "",
        year_began=start_year(vol),
        source="cv",
        cv_id=cv_id,
        match_status="not_found",
        match_note="Added from ComicVine (not on Metron)",
        monitored=option != "none",
        monitor_new=monitor_new,
        locked_fields=[],
    )
    pub = _upsert_publisher(db, vol.get("publisher"), id_field="cv_id")
    if pub is not None:
        series.publisher_id = pub.id
    db.add(series)
    db.flush()
    db.refresh(series)
    series.path = folder_for(db, series)
    await sync_cv_issues(db, cv, series)
    if os.path.isdir(series.path):
        scan_folders(db, [series.id])
    db.flush()
    db.refresh(series)
    monitoring.apply_option(series, option)
    series.monitor_new = monitor_new
    log(db, f"Added '{series.name}' from ComicVine ({monitoring.OPTIONS[option]})", kind="add",
        series_id=series.id)  # fmt: skip
    db.flush()
    return series


def gap_counts(db: Session) -> dict[int, tuple[int, int]]:
    """Per ComicVine series: (issues, issues with a file), counted in SQL."""
    issues = dict(
        db.execute(
            select(Issue.series_id, func.count(Issue.id))
            .join(Series)
            .where(Series.source == "cv")
            .group_by(Issue.series_id)
        ).all()
    )
    with_files = dict(
        db.execute(
            select(Issue.series_id, func.count(func.distinct(Issue.id)))
            .join(Series)
            .join(File, File.issue_id == Issue.id)
            .where(Series.source == "cv")
            .group_by(Issue.series_id)
        ).all()
    )
    return {sid: (n, with_files.get(sid, 0)) for sid, n in issues.items()}
