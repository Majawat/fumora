"""Tagging: turn Fumora's data into ComicInfo.xml + MetronInfo.xml in each file."""

from datetime import UTC, date, datetime

from sqlalchemy.orm import Session

from . import __version__, cbz
from .comicinfo import IssueMeta, build_metroninfo, merge_comicinfo
from .metron import MetronClient
from .models import File, Issue, Series
from .ratings import TO_COMICINFO, issue_rating


class NotWritable(Exception):
    """The series or file may not be written (unconfirmed match, not a CBZ...)."""


def _date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value[:10]) if value else None
    except ValueError:
        return None


def _named(items, key: str = "name") -> list[tuple[str, int | None]]:
    return [(i.get(key), i.get("id")) for i in items or [] if i.get(key)]


def issue_meta(issue: Issue) -> IssueMeta:
    series: Series = issue.series
    d = issue.metron_detail or {}
    ds = d.get("series") or {}
    rating = issue_rating(issue)
    tags = sorted({t.name for t in series.tags} | {t.name for t in issue.tags})
    cv_id = issue.cv_id or d.get("cv_id")
    gcd_id = issue.gcd_id or d.get("gcd_id")
    ids = [
        f"[Metron:{issue.metron_id}]" if issue.metron_id else None,
        f"[CVDB{cv_id}]" if cv_id else None,
        f"[GCD:{gcd_id}]" if gcd_id else None,
    ]
    notes = f"Tagged by Fumora {__version__} on {date.today().isoformat()}. " + " ".join(i for i in ids if i)
    web = d.get("resource_url") or (
        f"https://metron.cloud/issue/{issue.metron_id}/" if issue.metron_id else None
    )
    return IssueMeta(
        series_name=series.name,
        series_year=series.year_began,
        series_sort_name=series.sort_name,
        series_volume=series.volume,
        series_type=series.series_type,
        series_language=ds.get("language"),
        series_metron_id=series.metron_id,
        publisher=series.publisher.name if series.publisher else None,
        publisher_metron_id=series.publisher.metron_id if series.publisher else None,
        imprint=series.imprint.name if series.imprint else None,
        imprint_metron_id=series.imprint.metron_id if series.imprint else None,
        genres=list(series.genres or []),
        number=issue.number,
        title=(d.get("title") or None) or issue.title,
        stories=[s for s in d.get("name") or [] if s],
        collection_title=d.get("title") or None,
        alt_number=d.get("alt_number") or issue.alt_number or None,
        summary=d.get("desc") or None,
        cover_date=issue.cover_date or _date(d.get("cover_date")),
        store_date=issue.store_date or _date(d.get("store_date")),
        page_count=d.get("page") or issue.page_count or None,
        price=d.get("price"),
        price_currency=d.get("price_currency"),
        isbn=d.get("isbn") or issue.isbn or None,
        upc=d.get("upc") or issue.upc or None,
        credits=[
            (c["creator"], c.get("id"), [(r["name"], r.get("id")) for r in c.get("role") or []])
            for c in d.get("credits") or []
            if c.get("creator")
        ],
        characters=_named(d.get("characters")),
        teams=_named(d.get("teams")),
        universes=_named(d.get("universes")),
        arcs=_named(d.get("arcs")),
        reprints=_named(d.get("reprints"), key="issue"),
        age_rating_metron=rating,
        age_rating_comicinfo=TO_COMICINFO.get(rating) if rating else None,
        tags=tags,
        metron_id=issue.metron_id,
        cv_id=cv_id,
        gcd_id=gcd_id,
        web=web,
        modified=d.get("modified"),
        notes=notes,
    )


async def ensure_detail(db: Session, client: MetronClient, issue: Issue) -> None:
    """Fetch and cache Metron's full issue record if we don't have it yet."""
    if issue.metron_id is None or issue.metron_detail is not None:
        return
    d = await client.issue(issue.metron_id)
    issue.metron_detail = d
    issue.detail_fetched = datetime.now(UTC)
    rating = (d.get("rating") or {}).get("name")
    if rating and "rating" not in (issue.locked_fields or []):
        issue.rating = rating
    for field, value in (("isbn", d.get("isbn")), ("upc", d.get("upc")), ("page_count", d.get("page"))):
        if value and field not in (issue.locked_fields or []):
            setattr(issue, field, value)
    if d.get("gcd_id") and issue.gcd_id is None:
        issue.gcd_id = d["gcd_id"]
    db.flush()


def check_writable(series: Series, file: File) -> None:
    if series.match_status == "matched" and series.confirmed_by is None:
        raise NotWritable("Metron match not confirmed")
    if not cbz.is_cbz(file.path):
        raise NotWritable(f"{file.path.rsplit('.', 1)[-1].upper()} files can't be tagged (CBZ only)")


def plan_file(issue: Issue, file: File) -> tuple[dict[str, bytes], dict[str, tuple]]:
    """New metadata files for `file` and the ComicInfo fields that would change."""
    check_writable(issue.series, file)
    comicinfo, metroninfo, _pages = cbz.read_metadata(file.path)
    meta = issue_meta(issue)
    new_ci, changes = merge_comicinfo(comicinfo, meta)
    files = {cbz.COMICINFO: new_ci}
    if issue.metron_id:
        files[cbz.METRONINFO] = build_metroninfo(meta, metroninfo)
        if metroninfo is None:
            changes["MetronInfo.xml"] = (None, "added")
    # "Notes" always changes (it carries the tagging date); not worth showing alone.
    meaningful = {k: v for k, v in changes.items() if k != "Notes"}
    return files, meaningful


def tag_file(issue: Issue, file: File, dry_run: bool) -> dict:
    """Preview (dry run) or write one file. Returns what is stored in file.tag_preview."""
    try:
        files, changes = plan_file(issue, file)
        if not dry_run:
            cbz.write_metadata(file.path, files)
            file.tagged_at = datetime.now(UTC)
        result = {"fields": {k: list(v) for k, v in changes.items()}, "error": None}
    except (NotWritable, cbz.ArchiveError, OSError) as exc:
        result = {"fields": {}, "error": str(exc)}
    result["dry_run"] = dry_run
    result["at"] = datetime.now(UTC).isoformat()
    file.tag_preview = result
    return result
