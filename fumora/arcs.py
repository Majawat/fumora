"""Story arcs as reading lists (Mylar's Story Arcs), from Metron.

An arc is Metron's list of issues in reading order, across series. Fumora stores the
list and matches each entry to its own issues by Metron id when the arc is shown, so
a series added later fills in by itself. From an arc: monitor the issues of series
you follow, add the series you don't (only the arc's issues monitored), and export
a ComicRack reading list (.cbl), which Komga and Kavita import.
"""

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from . import monitoring
from .importer import log
from .metron import MetronClient
from .models import Issue, StoryArc, StoryArcEntry


def _date(v) -> date | None:
    try:
        return date.fromisoformat(v) if v else None
    except ValueError:
        return None


async def sync_arc(db: Session, client: MetronClient, metron_id: int) -> StoryArc:
    """Add the arc, or bring its issue list up to date (Metron's order)."""
    detail = await client.arc(metron_id)
    arc = db.scalar(select(StoryArc).where(StoryArc.metron_id == metron_id))
    if arc is None:
        arc = StoryArc(metron_id=metron_id, name=detail.get("name") or f"Arc {metron_id}")
        db.add(arc)
    arc.name = detail.get("name") or arc.name
    arc.desc = detail.get("desc") or None
    arc.cv_id = detail.get("cv_id")
    rows = [r async for r in client.arc_issues(metron_id)]
    old = {e.metron_issue_id: e for e in arc.entries}
    keep = []
    for pos, r in enumerate(rows, 1):
        if not r.get("id") or r["id"] in {e.metron_issue_id for e in keep}:
            continue
        e = old.pop(r["id"], None) or StoryArcEntry(metron_issue_id=r["id"])
        s = r.get("series") or {}
        e.position = pos
        e.metron_series_id = s.get("id")
        e.series_name = s.get("name") or ""
        e.series_year = s.get("year_began")
        e.volume = s.get("volume")
        e.number = str(r.get("number") or "")[:20]
        e.cover_date = _date(r.get("cover_date"))
        e.store_date = _date(r.get("store_date"))
        keep.append(e)
    arc.entries = keep  # entries Metron no longer lists go
    arc.synced = datetime.now(UTC)
    db.flush()
    return arc


@dataclass
class Row:
    entry: StoryArcEntry
    issue: Issue | None  # Fumora's, if its series is followed

    @property
    def state(self) -> str:
        if self.issue is None:
            return "Not followed"
        return monitoring.state(self.issue)


def rows(db: Session, arc: StoryArc) -> list[Row]:
    ids = [e.metron_issue_id for e in arc.entries]
    issues = {
        i.metron_id: i
        for i in db.scalars(
            select(Issue)
            .where(Issue.metron_id.in_(ids))
            .options(selectinload(Issue.files), selectinload(Issue.series))
        )
    }
    return [Row(e, issues.get(e.metron_issue_id)) for e in arc.entries]


def summary(rs: list[Row]) -> dict:
    out = {"total": len(rs), "have": 0, "wanted": 0, "not_followed": 0}
    for r in rs:
        if r.issue is None:
            out["not_followed"] += 1
        elif r.issue.files:
            out["have"] += 1
        elif r.state == "Wanted":
            out["wanted"] += 1
    return out


def monitor_followed(db: Session, arc: StoryArc) -> int:
    """Monitor the arc's issues in followed series (and the series, so they're Wanted)."""
    n = 0
    for r in rows(db, arc):
        if r.issue is not None and not r.issue.files and not (r.issue.monitored and r.issue.series.monitored):
            r.issue.monitored = True
            r.issue.series.monitored = True
            n += 1
    if n:
        log(db, f"Monitored {n} issues of the story arc '{arc.name}'", kind="monitor")
    return n


def missing_series(db: Session, arc: StoryArc) -> list[dict]:
    """The arc's series Fumora doesn't follow: Metron id, name, year, how many arc issues."""
    out: dict[int, dict] = {}
    for r in rows(db, arc):
        e = r.entry
        if r.issue is None and e.metron_series_id:
            d = out.setdefault(
                e.metron_series_id,
                {"id": e.metron_series_id, "name": e.series_name, "year": e.series_year, "count": 0},
            )
            d["count"] += 1
    return sorted(out.values(), key=lambda d: (-d["count"], d["name"]))


def monitor_arc_issues_only(db: Session, arc: StoryArc, metron_series_id: int) -> int:
    """After adding a series for an arc: monitor just the arc's issues in it."""
    ids = {e.metron_issue_id for e in arc.entries if e.metron_series_id == metron_series_id}
    n = 0
    for i in db.scalars(select(Issue).where(Issue.metron_id.in_(ids))):
        i.monitored = True
        i.series.monitored = True
        n += 1
    return n


def reading_list(db: Session, arc: StoryArc) -> bytes:
    """ComicRack reading list (.cbl): series, number, volume, year, and the Metron and
    ComicVine ids where known (Komga and Kavita match on series + number)."""
    root = ET.Element("ReadingList")
    ET.SubElement(root, "Name").text = arc.name
    books = ET.SubElement(root, "Books")
    for r in rows(db, arc):
        e = r.entry
        series = r.issue.series if r.issue is not None else None
        attrs = {
            "Series": series.name if series is not None else e.series_name,
            "Number": e.number,
            "Volume": str(
                series.year_began if series is not None and series.year_began else e.series_year or ""
            ),
            "Year": str((e.cover_date or e.store_date).year) if (e.cover_date or e.store_date) else "",
        }
        book = ET.SubElement(books, "Book", {k: v for k, v in attrs.items() if v})
        ET.SubElement(book, "Database", {"Name": "metron", "Series": str(e.metron_series_id or ""),
                                         "Issue": str(e.metron_issue_id)})  # fmt: skip
        if r.issue is not None and r.issue.cv_id:
            cv_series = series.cv_id if series is not None and series.cv_id else ""
            ET.SubElement(
                book, "Database", {"Name": "cv", "Series": str(cv_series), "Issue": str(r.issue.cv_id)}
            )
    ET.indent(root)
    return b'<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="utf-8")


def arcs_of(db: Session, issue: Issue) -> list[StoryArc]:
    if not issue.metron_id:
        return []
    return list(
        db.scalars(
            select(StoryArc)
            .join(StoryArcEntry)
            .where(StoryArcEntry.metron_issue_id == issue.metron_id)
            .order_by(StoryArc.name)
        )
    )
