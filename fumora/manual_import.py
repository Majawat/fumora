"""Importing files by hand, like Sonarr's manual import.

Point Fumora at a folder: every comic file in it gets a proposed series and
issue, from the strongest evidence first:

1. ids inside the file (MetronInfo.xml, or ComicInfo's Notes/Web as Fumora,
   ComicTagger and Mylar write them) -> the issue with that Metron or
   ComicVine id;
2. ComicInfo's Series + Number (+ Year);
3. the file name (the release parser), with the folder name as the series
   when the file name has none.

The owner checks or changes each row, then a job places the chosen files
exactly like a finished download (grabbing.place_issue_file): named, moved or
hardlinked (never copied across mounts), converted to CBZ, then tagged with the
issue's full record when tagging is on. Nothing is ever overwritten: an issue
that has a file, or a name that's taken, is skipped.
"""

import asyncio
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from . import cbz, convert, settings_store
from .catalog import folder_for
from .comicinfo import COLLECTED_TYPES
from .grabbing import IMPORT_EXTS, destination, place_issue_file, tags_on_import
from .importer import log
from .jobs import JobContext
from .library_jobs import naming_settings
from .models import File, Issue, Series
from .numbers import normalize_number
from .releases import parse_release, series_key
from .tagging import tag_file

MANUAL_IMPORT = "manual_import"
MAX_FILES = 500  # per folder listing
DEFAULT_ROOTS = "/data"

_METRON_NOTE = re.compile(r"\[Metron:(\d+)\]")
_CV_NOTE = re.compile(r"\[CVDB(\d+)\]|\[Issue ID (\d+)\]", re.I)
_CV_WEB = re.compile(r"comicvine\.gamespot\.com/[^\s]*?/4000-(\d+)", re.I)


class ImportPathError(Exception):
    pass


@dataclass
class Embedded:
    """What a file says about itself."""

    metron_id: int | None = None
    cv_id: int | None = None
    series: str | None = None
    number: str | None = None
    year: int | None = None


@dataclass
class Proposal:
    path: str
    name: str
    size: int
    series_id: int | None = None
    number: str | None = None
    issue_id: int | None = None
    evidence: str = ""  # "ids in the file", "ComicInfo", "file name", "folder name"
    problem: str | None = None
    dest: str | None = None

    @property
    def ready(self) -> bool:
        return self.issue_id is not None and self.problem is None


def roots(db: Session) -> list[str]:
    raw = settings_store.get(db, "import.roots") or DEFAULT_ROOTS
    return [os.path.realpath(r.strip()) for r in raw.split(",") if r.strip()]


def check_path(db: Session, path: str) -> str:
    """The real path, if it's under one of the import folders."""
    if not path:
        raise ImportPathError("Enter a folder to import from.")
    # normpath after realpath: symlinks resolved, then the form CodeQL's path check expects.
    real = os.path.normpath(os.path.realpath(path))
    for root in roots(db):
        if real == root or real.startswith(root.rstrip("/") + "/"):
            if not os.path.exists(real):
                raise ImportPathError(f"{path} doesn't exist.")
            return real
    raise ImportPathError(f"{path} isn't under an import folder ({', '.join(roots(db))}; see Settings).")


def list_files(path: str) -> list[Path]:
    p = Path(path)
    if p.is_file():
        return [p] if p.suffix.lower() in IMPORT_EXTS else []
    found: list[Path] = []
    for root, dirs, files in os.walk(p):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(files):
            if Path(name).suffix.lower() in IMPORT_EXTS and not name.startswith("."):
                found.append(Path(root) / name)
                if len(found) >= MAX_FILES:
                    return found
    return found


def _int(text: str | None) -> int | None:
    try:
        return int((text or "").strip()) or None
    except ValueError:
        return None


def read_embedded(path: Path) -> Embedded:
    """ComicInfo.xml / MetronInfo.xml inside the file, if it has them."""
    e = Embedded()
    try:
        if cbz.is_cbz(path):
            ci, mi, _pages = cbz.read_metadata(path)
        elif path.suffix.lower() != ".pdf" and convert.needs_conversion(path):
            ci, mi = convert.read_member(path, cbz.COMICINFO), convert.read_member(path, cbz.METRONINFO)
        else:
            return e
    except Exception:  # unreadable archive: the file name still helps
        return e
    if mi:
        try:
            for el in ET.fromstring(mi).iter("ID"):
                source = (el.get("source") or "").lower()
                if source == "metron":
                    e.metron_id = e.metron_id or _int(el.text)
                elif source == "comic vine":
                    e.cv_id = e.cv_id or _int(el.text)
        except ET.ParseError:
            pass
    if ci:
        try:
            root = ET.fromstring(ci)
        except ET.ParseError:
            return e
        text = {child.tag: (child.text or "").strip() for child in root}
        notes, web = text.get("Notes", ""), text.get("Web", "")
        if e.metron_id is None and (m := _METRON_NOTE.search(notes)):
            e.metron_id = int(m.group(1))
        if e.cv_id is None and (m := _CV_NOTE.search(notes) or _CV_WEB.search(web)):
            e.cv_id = int(next(g for g in m.groups() if g))
        e.series = text.get("Series") or None
        e.number = text.get("Number") or None
        e.year = _int(text.get("Year"))
    return e


class Matcher:
    """Series and issues by name, number and id, loaded once per listing."""

    def __init__(self, db: Session):
        self.db = db
        self.series = db.scalars(select(Series).options(selectinload(Series.issues))).all()
        self.by_key: dict[str, list[Series]] = {}
        for s in self.series:
            for name in {s.name, s.mylar_name} - {None, ""}:
                self.by_key.setdefault(series_key(name), []).append(s)

    def by_name(self, name: str | None, year: int | None, number: str | None) -> Series | None:
        options = self.by_key.get(series_key(name or ""), []) if name else []
        if len(options) <= 1:
            return options[0] if options else None
        # Several volumes share the name: the year decides, then who has the issue.
        if year:
            exact = [s for s in options if s.year_began == year]
            if len(exact) == 1:
                return exact[0]
            dated = [s for s in options if self.issue(s, number, year)]
            if len(dated) == 1:
                return dated[0]
        having = [s for s in options if number and self.issue(s, number)]
        return having[0] if len(having) == 1 else None

    @staticmethod
    def issue(series: Series, number: str | None, year: int | None = None) -> Issue | None:
        if not number:
            return None
        want = normalize_number(number)
        for i in series.issues:
            if normalize_number(i.number) == want and (
                year is None or (i.cover_date and abs(i.cover_date.year - year) <= 1)
            ):
                return i
        return None

    def by_id(self, e: Embedded) -> Issue | None:
        if e.metron_id:
            issue = self.db.scalar(select(Issue).where(Issue.metron_id == e.metron_id))
            if issue is not None:
                return issue
        if e.cv_id:
            return self.db.scalar(select(Issue).where(Issue.cv_id == e.cv_id).limit(1))
        return None


def _from_name(m: Matcher, path: Path) -> tuple[Series | None, str | None, str]:
    rel = parse_release(path.name)
    number = rel.number if rel.kind in ("issue", "volume") else None
    series = m.by_name(rel.series, rel.year, number)
    evidence = "file name"
    if series is None:
        # "Mieruko-chan/Vol. 12.cbz": the folder names the series.
        folder = parse_release(path.parent.name)
        series = m.by_name(folder.series or path.parent.name, rel.year or folder.year, number)
        evidence = "folder name"
    if series is not None and rel.kind == "volume":
        # "Vol. 12" is issue 12 only where issues are volumes (manga, collected editions).
        if not (series.issues_are_volumes or series.series_type in COLLECTED_TYPES):
            number = None
    return series, number, evidence


def propose(db: Session, path: str) -> list[Proposal]:
    m = Matcher(db)
    opts = naming_settings(db)
    out: list[Proposal] = []
    for f in list_files(path):
        p = Proposal(path=str(f), name=f.name, size=f.stat().st_size)
        e = read_embedded(f)
        issue = m.by_id(e)
        if issue is not None:
            series, number, p.evidence = issue.series, issue.number, "ids in the file"
        else:
            series = m.by_name(e.series, e.year, e.number) if e.series else None
            number, p.evidence = (e.number, "ComicInfo") if series else (None, "")
            if series is None:
                series, number, p.evidence = _from_name(m, f)
            issue = m.issue(series, number) if series else None
        if series is None:
            p.evidence = ""
        p.series_id = series.id if series else None
        p.number = issue.number if issue else number
        check(db, p, series, issue, opts)
        out.append(p)
    return out


def check(db: Session, p: Proposal, series: Series | None, issue: Issue | None, opts: dict) -> None:
    """Fill in where the file would go, or the problem that stops it."""
    p.issue_id = issue.id if issue else None
    linked = db.scalar(select(File).where(File.path == p.path, File.issue_id.is_not(None)))
    if linked is not None:
        # Moving it would leave its own issue pointing at nothing.
        p.problem = f"Already in the library as {linked.issue.series.name} #{linked.issue.number}."
    elif series is None:
        p.problem = "Which series? None matched."
    elif issue is None:
        p.problem = f"{series.name} has no #{p.number}." if p.number else "Which issue? No number found."
    elif issue.files:
        p.problem = f"#{issue.number} already has a file."
    else:
        if not series.path:
            series.path = folder_for(db, series)
        dest = destination(series, issue, Path(p.path), opts)
        p.dest = str(dest.with_suffix(".cbz"))
        same = dest.exists() and dest.resolve() == Path(p.path).resolve()
        if not same and (dest.exists() or dest.with_suffix(".cbz").exists()):
            p.problem = f"{dest.with_suffix('.cbz').name} already exists."


def resolve(db: Session, path: str, series_id: int | None, number: str | None) -> Proposal:
    """A row as the owner set it (series and number), checked again."""
    f = Path(path)
    p = Proposal(path=path, name=f.name, size=f.stat().st_size if f.exists() else 0)
    series = db.get(Series, series_id) if series_id else None
    p.series_id, p.number = series_id, (number or "").strip() or None
    if not f.is_file():
        p.problem = "The file is gone."
        return p
    issue = Matcher.issue(series, p.number) if series else None
    check(db, p, series, issue, naming_settings(db))
    return p


def make_manual_import_handler(before_import=None):
    """params: items [{path, issue_id}], mode "move" | "hardlink"."""

    async def run(ctx: JobContext) -> str:
        params = ctx.params()
        items = params.get("items") or []
        keep = params.get("mode") == "hardlink"
        protocol = "torrent" if keep else "usenet"  # the same tagging rules as downloads
        done = set(params.get("done") or [])
        sf = ctx.session_factory
        with sf() as db:
            tag_on = tags_on_import(db, protocol)
        todo = [it for it in items if it["path"] not in done]
        if before_import is not None and tag_on and todo:
            ctx.update(phase="fetching issue records", total=len(items), progress=len(done))
            await before_import(sf, [it["issue_id"] for it in todo])
        ctx.update(phase="importing", total=len(items), progress=len(done))
        counts = {"imported": 0, "skipped": 0}
        for n, it in enumerate(todo, len(done) + 1):
            msg = await asyncio.to_thread(_import_one, sf, it["path"], it["issue_id"], keep, tag_on)
            counts["imported" if msg is None else "skipped"] += 1
            done.add(it["path"])
            ctx.update(progress=n, params={**params, "done": sorted(done)})
        summary = f"Manual import: {counts['imported']} imported, {counts['skipped']} skipped (see the log)"
        with sf() as db:
            log(db, summary, kind="import")
            db.commit()
        return summary

    return run


def _import_one(sf, path: str, issue_id: int, keep: bool, tag_on: bool) -> str | None:
    """Import one file; returns why it was skipped, or None."""
    with sf() as db:
        issue = db.scalar(
            select(Issue)
            .where(Issue.id == issue_id)
            .options(
                selectinload(Issue.files),
                selectinload(Issue.tags),
                selectinload(Issue.series).selectinload(Series.issues),
                selectinload(Issue.series).selectinload(Series.tags),
            )
        )
        src = Path(path)
        problem = None
        if issue is None:
            problem = "the issue no longer exists"
        elif not src.is_file():
            problem = "the file is gone"
        elif db.scalar(select(File).where(File.path == path, File.issue_id.is_not(None))) is not None:
            problem = "it's already in the library as another issue's file"
        if problem is None:
            series = issue.series
            try:
                row, problem = place_issue_file(db, series, issue, src, keep, naming_settings(db))
            except Exception as exc:  # e.g. another mount: say so, carry on with the rest
                db.rollback()
                row, problem = None, str(exc)
            if row is not None:
                if series.writable and tag_on:
                    recycle = settings_store.get(db, "library.recycle_path") or "/data/media/fumora-recycle"
                    tag_file(issue, row, dry_run=False, recycle_dir=recycle)
                log(
                    db,
                    f"Imported {src.name} as {series.name} #{issue.number}",
                    kind="import",
                    issue_id=issue.id,
                )
                db.commit()
                return None
        log(db, f"Skipped {src.name}: {problem}", kind="import", level="warning", issue_id=issue_id)
        db.commit()
        return problem
