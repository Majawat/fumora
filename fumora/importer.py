"""Mylar import (M1): copy Mylar's catalog into Fumora and match it to Metron.

Nothing here writes to Mylar or to the library. Phases:

1. read   - snapshot Mylar's DB, upsert series/issues/files keyed by ComicVine id
2. match  - find each series on Metron by its ComicVine id
3. issues - pair our issues with Metron's by issue number; add issues only Metron has

Every phase is idempotent, so a restarted job simply continues.
"""

import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .metron import MetronClient
from .models import Event, File, Imprint, Issue, Publisher, Series
from .monitoring import new_issue_monitored
from .mylar import MylarSeries
from .numbers import normalize_number, sort_number
from .releases import series_key


@dataclass
class ReadStats:
    series_new: int = 0
    series_seen: int = 0
    issues_new: int = 0
    files_linked: int = 0
    files_missing: int = 0
    issues_conflict: int = 0


# Module-level so tests can stand in for the filesystem.
file_exists: Callable[[str], bool] = os.path.exists


def upsert_mylar(
    db: Session, mylar_series: list[MylarSeries], exists: Callable[[str], bool] | None = None
) -> ReadStats:
    exists = exists or file_exists
    stats = ReadStats()
    for ms in mylar_series:
        stats.series_seen += 1
        series = db.scalar(select(Series).where(Series.cv_id == ms.cv_id))
        if series is None:
            series = Series(
                cv_id=ms.cv_id,
                source="cv",
                name=ms.name,
                sort_name=ms.sort_name,
                year_began=ms.year,
                path=ms.path,
                monitored=ms.active,
                monitor_new=ms.active,
                mylar_type=ms.mylar_type,
                series_type="Annual" if ms.is_annual else None,
                age_rating_override=ms.age_rating,
                match_status="pending",
                locked_fields=[],
            )
            db.add(series)
            db.flush()
            stats.series_new += 1
        elif series.path is None:
            series.path = ms.path
        series.mylar_name = ms.name
        series.mylar_year = ms.year

        by_cv = {i.cv_id: i for i in series.issues if i.cv_id is not None}
        by_number = {normalize_number(i.number): i for i in series.issues}
        for mi in ms.issues:
            issue = by_cv.get(mi.cv_id)
            if issue is None:
                same_number = by_number.get(normalize_number(mi.number))
                if same_number is not None and same_number.cv_id is not None:
                    # A different ComicVine issue already has this number in this
                    # series; don't fold the two together.
                    stats.issues_conflict += 1
                    continue
                issue = same_number
            if issue is None:
                issue = Issue(
                    series=series,
                    cv_id=mi.cv_id,
                    number=mi.number,
                    sort_number=sort_number(mi.number),
                    title=mi.title,
                    cover_date=mi.cover_date,
                    store_date=mi.store_date,
                    status=mi.status,
                    # Monitored if Mylar wanted it or had it (a lost file then shows as Wanted).
                    monitored=ms.active and mi.status in ("Wanted", "Downloaded"),
                    locked_fields=[],
                )
                db.add(issue)
                db.flush()
                by_number[normalize_number(mi.number)] = issue
                stats.issues_new += 1
            elif issue.cv_id is None:
                issue.cv_id = mi.cv_id

            if mi.path:
                if exists(mi.path):
                    file = db.scalar(select(File).where(File.path == mi.path))
                    if file is None:
                        db.add(File(issue_id=issue.id, path=mi.path))
                        stats.files_linked += 1
                    elif file.issue_id is None:
                        file.issue_id = issue.id
                    if issue.status != "Downloaded":
                        issue.status = "Downloaded"
                else:
                    stats.files_missing += 1
    db.flush()
    # Mylar's annuals belong to their series: keep that link (unless set by hand since).
    for ms in mylar_series:
        if ms.parent_cv_id is None:
            continue
        annual = db.scalar(select(Series).where(Series.cv_id == ms.cv_id))
        parent = db.scalar(select(Series).where(Series.cv_id == ms.parent_cv_id))
        if annual is not None and parent is not None and annual.parent_id is None and annual.id != parent.id:
            annual.parent_id = parent.id
    db.flush()
    return stats


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _upsert_publisher(db: Session, data: dict | None, id_field: str = "metron_id") -> Publisher | None:
    """The publisher with this Metron (or ComicVine: id_field='cv_id') id; a
    same-name publisher without that id gets it."""
    if not data or data.get("id") is None:
        return None
    column = getattr(Publisher, id_field)
    pub = db.scalar(select(Publisher).where(column == data["id"]))
    if pub is None and data.get("name"):
        pub = db.scalar(select(Publisher).where(Publisher.name == data["name"], column.is_(None)))
        if pub is not None:
            setattr(pub, id_field, data["id"])
    if pub is None:
        pub = Publisher(name=data.get("name") or "", **{id_field: data["id"]})
        db.add(pub)
        db.flush()
    return pub


def _upsert_imprint(db: Session, data: dict | None, publisher: Publisher | None) -> Imprint | None:
    if not data or data.get("id") is None:
        return None
    imp = db.scalar(select(Imprint).where(Imprint.metron_id == data["id"]))
    if imp is None:
        imp = Imprint(
            metron_id=data["id"], name=data.get("name") or "", publisher_id=publisher and publisher.id
        )
        db.add(imp)
        db.flush()
    return imp


def apply_metron_series(db: Session, series: Series, data: dict) -> None:
    """Copy Metron's series detail onto our row, skipping fields the user locked."""
    locked = set(series.locked_fields or [])

    def put(field: str, value) -> None:
        if field not in locked and value is not None:
            setattr(series, field, value)

    publisher = _upsert_publisher(db, data.get("publisher"))
    imprint = _upsert_imprint(db, data.get("imprint"), publisher)
    series.metron_id = data["id"]
    series.source = "metron"
    put("name", data.get("name"))
    put("sort_name", data.get("sort_name"))
    put("volume", data.get("volume"))
    put("year_began", data.get("year_began"))
    put("year_end", data.get("year_end"))
    put("series_type", (data.get("series_type") or {}).get("name"))
    put("status", data.get("status"))
    put("desc", data.get("desc"))
    put("genres", [g["name"] for g in data.get("genres") or []] or None)
    put("gcd_id", data.get("gcd_id"))
    if publisher and "publisher_id" not in locked:
        series.publisher_id = publisher.id
    if imprint and "imprint_id" not in locked:
        series.imprint_id = imprint.id
    series.modified = _parse_dt(data.get("modified"))
    series.last_synced = datetime.now(UTC)


# One way to compare series names everywhere (dots and underscores count as spaces).
_name_key = series_key


def _taken_by(db: Session, metron_id: int, series: Series) -> Series | None:
    return db.scalar(select(Series).where(Series.metron_id == metron_id, Series.id != series.id))


async def suggest_by_name(db: Session, client: MetronClient, series: Series) -> int | None:
    """A Metron series with the same name and a start year within one year.

    ComicVine and Metron often disagree on a start year by one (cover date vs
    on-sale date), so +/-1 is allowed; an exact-year candidate wins a tie.
    Only an unambiguous candidate that isn't matched elsewhere is returned.
    """
    if not series.year_began:
        return None
    key = _name_key(series.name)
    # One query per candidate year: name-only searches for common titles
    # ("Batman") span many pages.
    results: dict[int, dict] = {}
    for year in (series.year_began, series.year_began - 1, series.year_began + 1):
        for r in await client.search_series(series.name, year_began=year):
            results[r["id"]] = r
    candidates = [
        r
        for r in results.values()
        if r.get("year_began") is not None
        and abs(r["year_began"] - series.year_began) <= 1
        and _name_key(re.sub(r"\s*\(\d{4}\)$", "", r.get("series") or "")) == key
        and _taken_by(db, r["id"], series) is None
        and r["id"] not in (series.rejected_metron_ids or [])
    ]
    exact = [r for r in candidates if r["year_began"] == series.year_began]
    for group in (exact, candidates):
        if len(group) == 1:
            return group[0]["id"]
        if len(group) > 1:
            return None
    return None


async def match_series(db: Session, client: MetronClient, series: Series) -> str:
    """Find `series` on Metron. Returns the new match_status.

    By ComicVine id first. If that finds nothing, a unique same-name,
    same-year Metron series is stored as a suggestion for the user to accept.
    An accepted suggestion ('accepted') is applied directly.
    """
    if series.match_status == "accepted" and series.suggested_metron_id:
        metron_id = series.suggested_metron_id
        note = "Matched by name and year (accepted)"
    else:
        found = await client.series_by_cv_id(series.cv_id) if series.cv_id is not None else None
        if found is not None and found["id"] in (series.rejected_metron_ids or []):
            found = None  # the owner said that Metron series isn't this one
        if found is None:
            series.suggested_metron_id = await suggest_by_name(db, client, series)
            if series.suggested_metron_id:
                series.match_status = "review"
                series.match_note = "No ComicVine link on Metron; suggested match by name and year"
            else:
                series.match_status = "not_found"
                series.match_note = (
                    f"No Metron series has ComicVine id {series.cv_id}, or this name and year"
                    if series.cv_id is not None
                    else "No ComicVine id, and no Metron series with this name and year"
                )
            return series.match_status
        metron_id, note = found["id"], None

    taken = _taken_by(db, metron_id, series)
    if taken is not None:
        series.match_status = "review"
        series.match_note = f"Same Metron series ({metron_id}) as '{taken.name}' (#{taken.id})"
        return series.match_status
    detail = await client.series(metron_id)
    apply_metron_series(db, series, detail)
    series.match_status, series.match_note, series.suggested_metron_id = "matched", note, None
    if series.match_note and "accepted" in series.match_note:
        series.confirmed_by = "user"
    else:
        decide_confirmation(series)
    return series.match_status


def names_agree(series: Series) -> bool:
    """Mylar's name/year and Metron's name/year describe the same series."""
    if series.mylar_name is None:
        return True
    same_name = _name_key(series.mylar_name) == _name_key(series.name)
    years = (series.mylar_year, series.year_began)
    same_year = None in years or abs(years[0] - years[1]) <= 1
    return same_name and same_year


def decide_confirmation(series: Series) -> None:
    """Auto-confirm a ComicVine-id match whose name and year agree with Mylar's;
    otherwise leave it for the user and say why."""
    if series.confirmed_by is not None or series.match_status != "matched":
        return
    if names_agree(series):
        series.confirmed_by = "auto"
    else:
        mylar = series.mylar_name + (f" ({series.mylar_year})" if series.mylar_year else "")
        metron = series.name + (f" ({series.year_began})" if series.year_began else "")
        series.match_note = (
            f"Metron's name differs from Mylar's: '{mylar}' vs '{metron}'. Confirm or pick another."
        )


def review_confirmations(db: Session) -> int:
    """Settle confirmation for matched series that have none yet (e.g. matched
    before confirmation existed). Returns how many still need the user."""
    for series in db.scalars(
        select(Series).where(Series.match_status == "matched", Series.confirmed_by.is_(None))
    ):
        note = series.match_note or ""
        if "by hand" in note or "accepted" in note:
            series.confirmed_by = "user"
        else:
            decide_confirmation(series)
    db.flush()
    return db.scalar(
        select(func.count())
        .select_from(Series)
        .where(Series.match_status == "matched", Series.confirmed_by.is_(None))
    )


METRON_DERIVED = ("rating", "isbn", "upc", "page_count", "gcd_id")


def clear_metron_values(issue: Issue) -> None:
    """Forget an issue's Metron match and the values that came from it (except
    ones the user locked), so a wrong match leaves nothing behind."""
    issue.metron_id = None
    issue.match_status = None
    issue.metron_detail = None
    issue.detail_fetched = None
    for name in METRON_DERIVED:
        if name not in (issue.locked_fields or []):
            setattr(issue, name, None)


def unmatch_series(db: Session, series: Series, note: str) -> None:
    """Forget a Metron match: back to Mylar's name/year, drop Metron-only issues
    without files, clear Metron ids."""
    for issue in list(series.issues):
        # Only issues a Metron sync created (a ComicVine series' own new issues stay).
        if issue.match_status == "new" and not issue.files and issue.metron_id is not None:
            series.issues.remove(issue)
            db.delete(issue)
        else:
            clear_metron_values(issue)
    locked = set(series.locked_fields or [])
    if series.mylar_name and "name" not in locked:
        series.name = series.mylar_name
    if series.mylar_name and series.mylar_year and "year_began" not in locked:
        series.year_began = series.mylar_year
    series.metron_id = None
    series.source = "cv"
    series.confirmed_by = None
    series.issues_synced = None
    series.suggested_metron_id = None
    series.match_status = "not_found"
    series.match_note = note
    db.flush()


def listed_issue(
    db: Session, series: Series, by_number: dict[str, Issue], number: str, release: date | None
) -> tuple[Issue, bool]:
    """Our issue with this number, or a new one (monitored by the series' "monitor
    new issues" rule) when a source lists one we don't have. Returns (issue, created)."""
    key = normalize_number(number)
    issue = by_number.get(key)
    if issue is not None:
        return issue, False
    added = series.added.date() if series.added else date.today()
    issue = Issue(
        series=series,
        number=number or "",
        monitored=new_issue_monitored(series, release, added),
        match_status="new",
        locked_fields=[],
    )
    db.add(issue)
    by_number[key] = issue
    return issue, True


def apply_listed_values(issue: Issue, values: dict) -> bool:
    """Copy a source's values onto an issue, skipping fields the user locked and
    empty values. Returns whether anything changed."""
    locked = set(issue.locked_fields or [])
    changed = False
    for field, value in values.items():
        if field not in locked and value and getattr(issue, field) != value:
            setattr(issue, field, value)
            changed = True
    issue.sort_number = sort_number(issue.number)
    issue.last_synced = datetime.now(UTC)
    return changed


async def sync_series_issues(db: Session, client: MetronClient, series: Series) -> dict[str, int]:
    """Pair our issues with Metron's by number; add issues only Metron has."""
    counts = {"matched": 0, "new": 0, "unmatched": 0}
    by_number: dict[str, Issue] = {}
    for issue in series.issues:
        by_number.setdefault(normalize_number(issue.number), issue)

    async for mi in client.series_issues(series.metron_id):
        key = normalize_number(mi.get("number"))
        issue = by_number.get(key)
        if issue is not None and issue.metron_id not in (None, mi["id"]):
            continue
        clash = db.scalar(select(Issue).where(Issue.metron_id == mi["id"]))
        if clash is not None and (issue is None or clash.id != issue.id):
            continue
        cover, store = _parse_date(mi.get("cover_date")), _parse_date(mi.get("store_date"))
        issue, created = listed_issue(db, series, by_number, mi.get("number") or "", store or cover)
        if created:
            counts["new"] += 1
        else:
            issue.match_status = "matched"
            counts["matched"] += 1
        issue.metron_id = mi["id"]
        apply_listed_values(issue, {"cover_date": cover, "store_date": store})
        issue.modified = _parse_dt(mi.get("modified"))
        db.flush()

    for issue in series.issues:
        if issue.metron_id is None:
            issue.match_status = "unmatched"
            counts["unmatched"] += 1
    series.last_synced = datetime.now(UTC)
    db.flush()
    return counts


def import_report(db: Session) -> dict:
    def count(model, *where) -> int:
        return db.scalar(select(func.count()).select_from(model).where(*where)) or 0

    return {
        "series_total": count(Series),
        "series_matched": count(Series, Series.match_status == "matched"),
        "series_review": count(Series, Series.match_status == "review"),
        "series_not_found": count(Series, Series.match_status == "not_found"),
        "series_pending": count(Series, Series.match_status.in_(("pending", "accepted"))),
        "series_suggested": count(Series, Series.suggested_metron_id.is_not(None)),
        "series_unconfirmed": count(Series, Series.match_status == "matched", Series.confirmed_by.is_(None)),
        "issues_total": count(Issue),
        "issues_matched": count(Issue, Issue.match_status == "matched"),
        "issues_unmatched": count(Issue, Issue.match_status == "unmatched"),
        "issues_new": count(Issue, Issue.match_status == "new"),
        "files": count(File),
    }


def log(db: Session, message: str, kind: str = "import", level: str = "info", **ids) -> None:
    if ids.get("issue_id") and not ids.get("series_id"):
        # Every issue event is also its series' (the series page's History).
        ids["series_id"] = db.scalar(select(Issue.series_id).where(Issue.id == ids["issue_id"]))
    db.add(Event(kind=kind, level=level, message=message, **ids))


def snapshot_path(config_dir: Path) -> Path:
    return config_dir / "import" / "mylar-snapshot.db"
