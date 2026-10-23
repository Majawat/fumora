"""Annuals and specials (Mylar's "annuals integration").

On Metron and ComicVine an annual is a series of its own ("Batman Annual", 2016).
Fumora keeps it that way (its own issues, monitoring and searches) and links it to its
main series (`Series.parent_id`): the main series' page lists it, and it shares the main
series' folder, as Mylar does. The link comes from Mylar (an import keeps Mylar's), from
"Add series" (which offers the annuals Metron has), or is set on the series page.
Metron's own `associated` list is used when it has one; it is usually empty.
"""

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Series
from .releases import series_key

# "Annual", "2021 Annual", "Annual 2021", "Special": what makes a series another's annual.
_SUFFIX = re.compile(
    r"\s+(?:(?:19|20)\d{2}\s+)?(?:annual|annuals|special|specials)(?:\s+(?:19|20)\d{2})?$", re.I
)
KINDS = ("Annual",)  # Metron series types that belong to another series


def is_annual(series: Series) -> bool:
    return series.series_type in KINDS or bool(_SUFFIX.search(series.name or ""))


def base_key(name: str) -> str:
    """'Batman Annual' -> key of 'Batman'."""
    return series_key(_SUFFIX.sub("", name or ""))


def parent_candidates(db: Session, series: Series) -> list[Series]:
    """Series this one could belong to: those whose name starts its name ("Batman" for
    "Batman Annual", "Batman '66 Special"), most likely first (same publisher, the
    latest one that began by the time this did)."""
    key = series_key(series.name)
    rows = db.scalars(select(Series).where(Series.id != series.id, Series.parent_id.is_(None))).all()
    out = [
        s for s in rows if not is_annual(s) and (k := series_key(s.name)) and key.startswith(k) and k != key
    ]

    def rank(s: Series):
        same_base = series_key(s.name) == base_key(series.name)
        same_pub = s.publisher_id is not None and s.publisher_id == series.publisher_id
        began = s.year_began or 0
        fits = not series.year_began or not s.year_began or s.year_began <= series.year_began + 1
        return (not same_base, not fits, not same_pub, -began)

    return sorted(out, key=rank)


def guess_parent(db: Session, series: Series) -> Series | None:
    """The main series an annual most likely belongs to, or None when unsure: same base
    name, began by the time the annual did (a year's slack), not ended before it, and
    the same publisher when both are known."""
    if not is_annual(series):
        return None
    fits = []
    for s in parent_candidates(db, series):
        if series_key(s.name) != base_key(series.name):
            continue
        if series.publisher_id and s.publisher_id and s.publisher_id != series.publisher_id:
            continue
        y = series.year_began
        if y and s.year_began and s.year_began > y + 1:
            continue
        if y and s.year_end and s.year_end < y - 1:
            continue
        fits.append(s)
    if not fits:
        return None
    if series.year_began:
        # The run that was going when the annual began: the latest start not after it.
        return max(fits, key=lambda s: s.year_began or 0)
    return fits[0] if len(fits) == 1 else None


def link(series: Series, parent: Series | None) -> None:
    """Make `series` belong to `parent` (None: to nothing). A series with no files yet
    moves into the parent's folder; one with files keeps its folder (Organize moves it)."""
    if parent is not None and (
        parent is series or (series.id is not None and series.id in (parent.id, parent.parent_id))
    ):
        raise ValueError("A series can't belong to itself or to its own annual")
    series.parent_id = parent.id if parent is not None else None
    if parent is not None and parent.path and not any(i.files for i in series.issues):
        series.path = parent.path


def link_once(db: Session) -> int:
    """After the upgrade that added the link: link the annuals already in the database once."""
    from . import settings_store

    if settings_store.get_state(db, "annuals.linked_once"):
        return 0
    n = link_unlinked(db)
    settings_store.put_state(db, "annuals.linked_once", "1")
    db.commit()
    return n


def link_unlinked(db: Session) -> int:
    """Give annuals without a main series the one guess_parent is sure of."""
    n = 0
    for s in db.scalars(select(Series).where(Series.parent_id.is_(None))).all():
        if is_annual(s) and (p := guess_parent(db, s)) is not None:
            s.parent_id = p.id
            n += 1
    return n


async def metron_annuals(client, db: Session, detail: dict) -> list[dict]:
    """Metron series that are annuals/specials of the series `detail` describes and aren't
    followed yet: Metron's `associated` list, else a search for "<name> Annual" by name
    and years. One or two Metron requests."""
    found: dict[int, dict] = {}
    for a in detail.get("associated") or []:
        if a.get("id"):
            found[a["id"]] = {"id": a["id"], "series": a.get("series") or a.get("name") or ""}
    if not found:
        name = detail.get("name") or ""
        began, ended = detail.get("year_began"), detail.get("year_end")
        for r in await client.search_series(f"{name} Annual", max_pages=1):
            label = re.sub(r"\s*\(\d{4}\)$", "", r.get("series") or r.get("name") or "")
            y = r.get("year_began")
            if base_key(label) != series_key(name) or not _SUFFIX.search(label):
                continue
            if began and y and not (began - 1 <= y <= (ended or 9999) + 1):
                continue
            found[r["id"]] = r
    have = set(db.scalars(select(Series.metron_id).where(Series.metron_id.in_(found))).all())
    return [r for i, r in found.items() if i not in have and i != detail.get("id")]
