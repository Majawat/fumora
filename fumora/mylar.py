"""Read-only access to a Mylar3 database.

Fumora never opens Mylar's live database for writing: it takes a consistent
snapshot with SQLite's backup API (from a read-only connection) and reads
the copy.
"""

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

# Mylar's free-text age ratings -> Metron vocabulary.
MYLAR_AGE_RATING = {
    "All": "Everyone",
    "All Ages": "Everyone",
    "9+": "Everyone",
    "12+": "Teen",
    "15+": "Teen Plus",
    "17+": "Mature",
    "Adult": "Adult",
}


@dataclass
class MylarIssue:
    cv_id: int
    number: str
    title: str | None
    cover_date: date | None
    store_date: date | None
    status: str  # Fumora status
    path: str | None  # full path of the file Mylar recorded


@dataclass
class MylarSeries:
    cv_id: int
    name: str
    sort_name: str | None
    year: int | None
    publisher: str | None
    path: str | None
    active: bool
    mylar_type: str | None
    age_rating: str | None  # Metron vocabulary
    is_annual: bool = False
    issues: list[MylarIssue] = field(default_factory=list)


def snapshot(src: Path, dest: Path) -> None:
    """Copy Mylar's DB to `dest` without writing to the source."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(tmp)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    tmp.replace(dest)


def _date(value: str | None) -> date | None:
    """Mylar dates are 'YYYY-MM-DD', sometimes 'YYYY-MM-00' or '0000-00-00'."""
    if not value:
        return None
    parts = value.strip()[:10].split("-")
    try:
        y, m, d = (int(p) for p in parts)
    except ValueError:
        return None
    if y == 0:
        return None
    try:
        return date(y, m or 1, d or 1)
    except ValueError:
        return None


def _int(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _status(mylar_status: str | None, has_file: bool) -> str:
    if has_file:
        return "Downloaded"
    if mylar_status in ("Wanted", "Snatched", "Failed"):
        return "Wanted"
    if mylar_status == "Ignored":
        return "Ignored"
    return "Skipped"


def _file_path(folder: str | None, location: str | None) -> str | None:
    if not location:
        return None
    if location.startswith("/"):
        return location
    if not folder:
        return None
    return f"{folder.rstrip('/')}/{location}"


def read_mylar(db_path: Path) -> list[MylarSeries]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        comic_cols = {r[1] for r in conn.execute("PRAGMA table_info(comics)")}
        age_col = "AgeRating" if "AgeRating" in comic_cols else "NULL"
        type_col = "Type" if "Type" in comic_cols else "NULL"
        series_by_cv: dict[int, MylarSeries] = {}
        for row in conn.execute(
            f"SELECT ComicID, ComicName, ComicSortName, ComicYear, ComicPublisher, ComicLocation, "
            f"Status, {type_col} AS Type, {age_col} AS AgeRating FROM comics"
        ):
            cv_id = _int(row["ComicID"])
            if cv_id is None:
                continue
            series_by_cv[cv_id] = MylarSeries(
                cv_id=cv_id,
                name=row["ComicName"],
                sort_name=row["ComicSortName"],
                year=_int(row["ComicYear"]),
                publisher=row["ComicPublisher"],
                path=row["ComicLocation"],
                active=row["Status"] == "Active",
                mylar_type=row["Type"],
                age_rating=MYLAR_AGE_RATING.get((row["AgeRating"] or "").strip()),
            )

        for row in conn.execute(
            "SELECT IssueID, ComicID, Issue_Number, IssueName, IssueDate, ReleaseDate, Status, Location "
            "FROM issues"
        ):
            parent = series_by_cv.get(_int(row["ComicID"]))
            issue_cv = _int(row["IssueID"])
            if parent is None or issue_cv is None:
                continue
            path = _file_path(parent.path, row["Location"])
            parent.issues.append(
                MylarIssue(
                    cv_id=issue_cv,
                    number=str(row["Issue_Number"] or "").strip(),
                    title=row["IssueName"],
                    cover_date=_date(row["IssueDate"]),
                    store_date=_date(row["ReleaseDate"]),
                    status=_status(row["Status"], path is not None),
                    path=path,
                )
            )

        # Annuals hang off their parent in Mylar but are their own series on
        # ComicVine and Metron (ReleaseComicID), so they become separate series.
        annuals: dict[int, MylarSeries] = {}
        for row in conn.execute(
            "SELECT IssueID, ComicID, Issue_Number, IssueName, IssueDate, ReleaseDate, Status, "
            "Location, ReleaseComicID, ReleaseComicName FROM annuals"
        ):
            parent = series_by_cv.get(_int(row["ComicID"]))
            annual_cv = _int(row["ReleaseComicID"])
            issue_cv = _int(row["IssueID"])
            if parent is None or annual_cv is None or issue_cv is None:
                continue
            series = annuals.get(annual_cv) or series_by_cv.get(annual_cv)
            if series is None:
                series = annuals[annual_cv] = MylarSeries(
                    cv_id=annual_cv,
                    name=row["ReleaseComicName"] or f"{parent.name} Annual",
                    sort_name=None,
                    year=None,
                    publisher=parent.publisher,
                    path=parent.path,
                    active=parent.active,
                    mylar_type="Annual",
                    age_rating=parent.age_rating,
                    is_annual=True,
                )
            path = _file_path(parent.path, row["Location"])
            series.issues.append(
                MylarIssue(
                    cv_id=issue_cv,
                    number=str(row["Issue_Number"] or "").strip(),
                    title=row["IssueName"],
                    cover_date=_date(row["IssueDate"]),
                    store_date=_date(row["ReleaseDate"]),
                    status=_status(row["Status"], path is not None),
                    path=path,
                )
            )
        for annual in annuals.values():
            dates = [i.cover_date for i in annual.issues if i.cover_date]
            annual.year = min(dates).year if dates else None
        return list(series_by_cv.values()) + list(annuals.values())
    finally:
        conn.close()
