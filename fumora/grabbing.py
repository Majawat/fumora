"""Grabbing releases, following downloads, importing finished ones (M4 part 2).

Import rules (TRaSH layout: downloads and library share one /data mount):
- usenet and direct downloads (GetComics, Shelfmark) are moved into the library
  (a rename on the same disk);
- torrent downloads are hardlinked, so they keep seeding (private trackers);
- never a silent copy across mounts: if a rename/hardlink isn't possible, the
  import fails with a message saying the mounts need fixing;
- CBR/CB7/CBT/PDF are converted into a new CBZ in the series folder; the download
  itself is left untouched until the CBZ is safely in place;
- the file is named by the naming template and, unless it's a torrent, tagged
  (confirmed series only). Tagging a torrent import would rewrite the file and
  break its hardlink, doubling disk use, so that is a separate setting, off;
- an issue that already has a file is never overwritten.
"""

import asyncio
import errno
import html
import logging
import os
import shutil
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from . import convert, naming, settings_store
from .catalog import folder_for
from .comicinfo import COLLECTED_TYPES
from .ddl import DirectClient
from .downloaders import DownloaderError, QbittorrentClient, SabnzbdClient
from .importer import log
from .library_jobs import naming_settings
from .models import Blocklist, File, Grab, Issue, Series
from .numbers import normalize_number
from .perms import normalize
from .releases import parse_release, series_key
from .scanner import COMIC_EXTS, parse_issue_number
from .search import Candidate
from .shelfmark import ShelfmarkClient
from .tagging import DEFAULT_RECYCLE, tag_file

logger = logging.getLogger("fumora.grabbing")
ACTIVE = ("queued", "downloading", "importing")
APPROVAL = "approval"  # found, but waiting for the owner's OK (e.g. another language)
IMPORT_EXTS = COMIC_EXTS  # PDFs included: converted to CBZ on import
# Folder names a cleanup must never remove, whatever the client reports.
PROTECTED_DIRS = {"complete", "completed", "incomplete", "downloads", "usenet", "torrents", "data", "fumora"}

_importing: set[int] = set()  # grabs being imported by this process right now


class ImportFailed(Exception):
    """An import that can't go ahead (explained in the message)."""


DEFAULT_CLIENT = {"usenet": "sabnzbd", "torrent": "qbittorrent", "ddl": "getcomics"}


def client_for(db: Session, protocol: str, name: str | None = None):
    """The configured download client for a release (by its client name or protocol), or None."""
    get = lambda k: settings_store.get(db, k)  # noqa: E731
    name = name or DEFAULT_CLIENT.get(protocol)
    if name == "sabnzbd":
        if get("sabnzbd.url") and get("sabnzbd.api_key"):
            return SabnzbdClient(
                get("sabnzbd.url"), get("sabnzbd.api_key"), get("sabnzbd.category") or "fumora"
            )
    elif name == "qbittorrent":
        url, user, pw = get("qbittorrent.url"), get("qbittorrent.username"), get("qbittorrent.password")
        if url and user and pw:
            return QbittorrentClient(url, user, pw, get("qbittorrent.category") or "fumora")
    elif name == "getcomics":
        return DirectClient(get("ddl.path"), get("getcomics.url"), get("flaresolverr.url"))
    elif name == "shelfmark":
        if get("shelfmark.url"):
            return ShelfmarkClient(get("shelfmark.url"), get("shelfmark.api_key"))
    return None


def _clean_title(title: str | None) -> str:
    return html.unescape(title or "").strip()


def is_blocklisted(db: Session, release: dict) -> bool:
    guid = release.get("guid")
    if guid and db.scalar(select(Blocklist.id).where(Blocklist.guid == guid).limit(1)) is not None:
        return True
    title = _clean_title(release.get("title"))
    return db.scalar(select(Blocklist.id).where(Blocklist.title == title).limit(1)) is not None


def blocklist(db: Session, grab: Grab, reason: str) -> None:
    exists = db.scalar(
        select(Blocklist.id).where(
            (Blocklist.guid == grab.guid) if grab.guid else (Blocklist.title == grab.release_title)
        )
    )
    if exists is None:
        db.add(Blocklist(guid=grab.guid, title=grab.release_title, reason=(reason or "")[:500]))


def covered_issues(c: Candidate, issue: Issue) -> list[int]:
    """The issues a release contains: one, or every issue a pack's range covers."""
    if c.parsed.kind != "range":
        return [issue.id]
    lo, hi = c.parsed.range_start, c.parsed.range_end
    ids = []
    for i in issue.series.issues:
        try:
            n = float(normalize_number(i.number))
        except ValueError:
            continue
        if lo <= n <= hi:
            ids.append(i.id)
    return ids or [issue.id]


def active_grab(db: Session, issue_id: int) -> Grab | None:
    """A download under way for this issue, or a release waiting for the owner's OK."""
    for g in db.scalars(select(Grab).where(Grab.state.in_((*ACTIVE, APPROVAL)))):
        if issue_id in (g.issue_ids or []) or g.issue_id == issue_id:
            return g
    return None


async def send(db: Session, g: Grab) -> None:
    """Hand a grab's release to its download client; raises DownloaderError if it won't take it."""
    client = client_for(db, g.protocol, g.client)
    if client is None:
        raise DownloaderError(f"No {g.protocol} download client is set up (Settings)")
    if not g.download_url:
        raise DownloaderError("This release has no download link")
    async with client:
        if g.protocol == "torrent":
            g.client_job_id = await client.add(g.download_url, f"fumora-{uuid.uuid4().hex[:12]}")
        else:
            g.client_job_id = await client.add(g.download_url, g.release_title)
    g.state = "downloading"


async def grab(db: Session, c: Candidate, issue: Issue, approval: bool = False) -> Grab:
    """Send a release to its download client and start following it; nothing is
    recorded unless the client accepted it. With approval=True (a release only
    held back by the language setting) it waits on the Queue page for an OK."""
    g = Grab(
        issue_id=issue.id,
        issue_ids=covered_issues(c, issue),
        series_id=issue.series_id,
        indexer=c.release.get("indexer") or "",
        release_title=_clean_title(c.release.get("title")),
        guid=c.release.get("guid"),
        download_url=c.release.get("downloadUrl") or c.release.get("magnetUrl"),
        protocol=c.protocol,
        size=c.release.get("size"),
        score=c.score,
        source=c.parsed.source,
        client=c.release.get("client") or DEFAULT_CLIENT.get(c.protocol),
        state=APPROVAL,
    )
    if approval:
        g.message = f"Needs your OK ({c.hold})"
        db.add(g)
        log(db, f"Holding '{g.release_title}' for your OK: {c.hold}", kind="grab", issue_id=issue.id)
    else:
        await send(db, g)
        db.add(g)
        log(db, f"Grabbed '{g.release_title}' ({c.protocol}, {g.indexer})", kind="grab", issue_id=issue.id)
    db.flush()
    return g


def _comic_files(path: str) -> list[Path]:
    p = Path(path)
    if p.is_file():
        return [p] if p.suffix.lower() in IMPORT_EXTS else []
    found = []
    for root, _dirs, files in os.walk(p):
        for name in sorted(files):
            f = Path(root) / name
            if f.suffix.lower() in IMPORT_EXTS and not name.startswith("."):
                found.append(f)
    return found


def _same_mount_error(exc: OSError) -> ImportFailed:
    if exc.errno == errno.EXDEV:
        return ImportFailed(
            "Downloads and the library are on different mounts, so Fumora can't move or hardlink. "
            "Mount the whole data share once at /data (TRaSH layout), as the download clients see it."
        )
    return ImportFailed(f"Couldn't place the file: {exc}")


def _place(src: Path, dest: Path, protocol: str) -> Path:
    """Hardlink (torrents keep seeding) or move (usenet). Never a copy."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        if protocol == "torrent":
            os.link(src, dest)
        else:
            os.rename(src, dest)
    except OSError as exc:
        raise _same_mount_error(exc) from exc
    return dest


def _place_converted(src: Path, dest: Path) -> Path:
    """Convert a CBR/CB7/CBT download into a new CBZ at `dest` (.cbz). The
    download is only read; it stays where it is until the CBZ is in place."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.stat().st_dev != dest.parent.stat().st_dev:
        # Same rule as every other import: no silent copy across mounts.
        raise _same_mount_error(OSError(errno.EXDEV, "different mounts"))
    tmp = dest.parent / f".fumora-import-{uuid.uuid4().hex[:8]}{src.suffix.lower()}"
    try:
        shutil.copyfile(src, tmp)
        converted = convert.convert_to_cbz(tmp, None)  # removes the temporary copy
        final = dest.with_suffix(".cbz")
        os.replace(converted, final)
        return final
    finally:
        tmp.unlink(missing_ok=True)
        tmp.with_suffix(".cbz").unlink(missing_ok=True)


def _remove_download_folder(path: str, category: str) -> None:
    """After a usenet import, remove the job's own folder if nothing useful is left.

    The client may report the job folder or the file inside it. Only a folder
    sitting directly inside the category folder (".../fumora/<job>") is removed;
    the category folder and anything above it never are."""
    p = Path(path)
    folder = p if p.is_dir() else p.parent
    if (
        folder.name.lower() in PROTECTED_DIRS
        or folder.name.lower() == category.lower()
        or folder.parent.name.lower() != category.lower()
    ):
        return
    try:
        if not any(x.suffix.lower() in IMPORT_EXTS for x in folder.rglob("*") if x.is_file()):
            shutil.rmtree(folder)
    except OSError:
        pass


def destination(series: Series, issue: Issue, src: Path, opts: dict) -> Path:
    """Where `src` goes in the library as `issue`'s file (named by the naming settings;
    collected editions use the collected-edition template when one is set)."""
    template = opts["file_template"]
    if opts.get("collected_template") and series.series_type in COLLECTED_TYPES:
        template = opts["collected_template"]
    name = naming.render(template, naming.context(series, issue), opts["colon"], opts["apostrophes"])
    return Path(series.path) / f"{name}{src.suffix.lower()}"


def name_taken(dest: Path, src: Path) -> bool:
    """True if placing `src` at `dest` (or its .cbz once converted) would overwrite
    another file. `src` already sitting at `dest` is not a clash with itself."""
    for target in {dest, dest.with_suffix(".cbz")}:
        if target.exists() and target.resolve() != src.resolve():
            return True
    return False


def place_issue_file(
    db: Session, series: Series, issue: Issue, src: Path, keep_original: bool, opts: dict
) -> tuple[File | None, str | None]:
    """Name and place `src` as `issue`'s file: hardlink (keep_original) or move,
    converting CBR/CB7/CBT/PDF to CBZ. Returns the new File, or None and why not.
    Never overwrites: an issue with a file, or a taken name, is skipped."""
    if issue.files:
        return None, f"#{issue.number} already has a file"
    if not series.path:
        series.path = folder_for(db, series)
    dest = destination(series, issue, src, opts)
    # A file already sitting at its own name (e.g. unlinked in the series folder) is just linked.
    if name_taken(dest, src):
        return None, f"{dest.with_suffix('.cbz').name} already exists"
    in_place = dest.exists() and dest.resolve() == src.resolve()
    if in_place and not convert.needs_conversion(src):
        placed = src
    elif in_place:
        # Already in the library at its own name: converted where it is, and the
        # original goes to the recycle folder (never deleted, never overwritten).
        recycle = settings_store.get(db, "library.recycle_path") or DEFAULT_RECYCLE
        placed = convert.convert_to_cbz(src, recycle)
    elif convert.needs_conversion(src):
        placed = _place_converted(src, dest)
        if not keep_original:
            src.unlink(missing_ok=True)  # the download's own copy, now converted
    else:
        placed = _place(src, dest, "torrent" if keep_original else "usenet")
    if not (keep_original and placed != src and not convert.needs_conversion(src)):
        # Moved in or newly converted: Fumora's own permissions, not the other
        # program's. A hardlink is the torrent client's seeding file: left alone.
        normalize(placed)
    # The folder scan may know the file already (unlinked), at its old or its new path.
    known = db.scalar(
        select(File).where(File.path.in_([str(src), str(placed)]), File.issue_id.is_(None)).limit(1)
    )
    if known is not None and (known.path == str(placed) or placed == src or not keep_original):
        known.path, known.size = str(placed), placed.stat().st_size
        row = known
    else:
        row = File(path=str(placed), size=placed.stat().st_size)
    issue.files.append(row)
    db.commit()  # the file is in the library now: record it whatever happens next
    return row, None


def tags_on_import(db: Session, protocol: str) -> bool:
    """Imports are tagged (torrents only when asked: tagging rewrites the seeded file)."""
    if protocol == "torrent":
        return (settings_store.get(db, "import.tag_torrents") or "off") == "on"
    return (settings_store.get(db, "import.tag") or "on") == "on"


def _wants_records(db: Session, g: Grab) -> list[int]:
    """Issues of this grab whose import will write tags (see details.DetailFetcher)."""
    if not tags_on_import(db, g.protocol):
        return []
    series = db.scalar(select(Series).join(Issue).where(Issue.id == g.issue_id)) if g.issue_id else None
    return list(dict.fromkeys(g.issue_ids or [g.issue_id])) if series is not None and series.writable else []


def _file_number(f: Path, series: Series) -> str | None:
    """The issue number a downloaded file's name gives, if it clearly names this series.
    "Vol. 12" counts where the series' issues are volumes (manga, collected editions)."""
    rel = parse_release(f.name)
    names = {series_key(n) for n in (series.name, series.mylar_name) if n}
    if rel.number is None or (rel.series and series_key(rel.series) not in names):
        num = parse_issue_number(f.name, series.name) or parse_issue_number(f.name, series.mylar_name)
        return normalize_number(num) if num else None
    if rel.kind == "issue":
        return normalize_number(rel.number)
    if rel.kind == "volume" and (series.issues_are_volumes or series.series_type in COLLECTED_TYPES):
        return normalize_number(rel.number)
    return None


def import_grab(db: Session, g: Grab, path: str) -> str:
    """Import a finished download. Returns a one-line summary."""
    issues = {
        i.id: i
        for i in db.scalars(
            select(Issue)
            .where(Issue.id.in_(g.issue_ids or [g.issue_id]))
            .options(selectinload(Issue.files), selectinload(Issue.series).selectinload(Series.issues))
        )
    }
    if not issues:
        raise ImportFailed("The issues for this download no longer exist")
    series = next(iter(issues.values())).series
    if not series.path:
        series.path = folder_for(db, series)
    if not path:
        # Path("") is the current folder: never go looking there.
        raise ImportFailed("The download client said it finished but gave no folder for it")
    files = _comic_files(path)
    if not files:
        raise ImportFailed(f"No comic files found in {path}")

    opts = naming_settings(db)
    recycle = settings_store.get(db, "library.recycle_path") or "/data/media/fumora-recycle"
    tags_on = tags_on_import(db, g.protocol)
    imported, skipped = [], []
    for f in files:
        if len(files) == 1 and len(issues) == 1:
            issue = next(iter(issues.values()))
            said = _file_number(f, series)
            if said is not None and said != normalize_number(issue.number):
                # The file says it's another issue: never import it under this one's name.
                skipped.append(f"{f.name}: the file is #{said}, not #{issue.number}")
                continue
        else:
            want = _file_number(f, series)
            issue = next((i for i in issues.values() if normalize_number(i.number) == want), None)
        if issue is None:
            skipped.append(f"{f.name}: no matching issue")
            continue
        # A torrent keeps seeding its original: hardlink it (or convert a copy).
        row, problem = place_issue_file(
            db, series, issue, f, keep_original=g.protocol == "torrent", opts=opts
        )
        if row is None:
            skipped.append(f"{f.name}: {problem}")
            continue
        if series.writable and tags_on:
            tag_file(issue, row, dry_run=False, recycle_dir=recycle)
            db.commit()
        imported.append(f"#{issue.number}")
    client = client_for(db, g.protocol, g.client)
    if imported and getattr(client, "owns_job_folders", False):
        _remove_download_folder(path, client.category)
    summary = f"Imported {', '.join(imported) or 'nothing'}" + (
        f"; skipped: {'; '.join(skipped)}" if skipped else ""
    )
    if not imported:
        raise ImportFailed(summary)
    return summary


def _import_in_thread(session_factory, grab_id: int, path: str) -> None:
    """Runs off the event loop (file moves and conversions can take a while)."""
    with session_factory() as db:
        g = db.get(Grab, grab_id)
        try:
            g.message = import_grab(db, g, path)
            g.state = "imported"
            log(db, f"{g.message} from '{g.release_title}'", kind="import", issue_id=g.issue_id)
        except Exception as exc:  # report it and keep the download for a look
            db.rollback()
            g = db.get(Grab, grab_id)
            g.state, g.message = "import_failed", str(exc)[:500]
            log(db, f"Import failed for '{g.release_title}': {exc}", kind="import", level="error")
        g.finished = datetime.now(UTC)
        db.commit()


MAX_RETRIES_PER_DAY = 5  # failed downloads of one issue that start a new search, per day


def retry_candidates(db: Session, g: Grab) -> list[int]:
    """Issues of a failed grab that should be searched again right away: the next
    best release (the failed one is blocklisted) may well work, e.g. Libgen has
    many dead copies. Capped per issue and day so a run of failures can't loop."""
    if (settings_store.get(db, "search.retry_failed") or "on") != "on" or not g.issue_id:
        return []
    since = datetime.now(UTC) - timedelta(days=1)
    failures = db.scalar(
        select(func.count())
        .select_from(Grab)
        .where(Grab.issue_id == g.issue_id, Grab.state == "failed", Grab.created >= since)
    )
    return list(dict.fromkeys(g.issue_ids or [g.issue_id])) if failures <= MAX_RETRIES_PER_DAY else []


async def check_downloads(session_factory, on_failed=None, before_import=None) -> int:
    """Poll active grabs; import finished ones. Returns how many changed state.
    on_failed(issue_ids) is called with issues whose download failed and that
    should be searched again (see retry_candidates). before_import(session_factory,
    issue_ids) is awaited before an import that writes tags: it fetches the
    issues' full records (details.DetailFetcher.fetch)."""

    async def records(g: Grab) -> None:
        if before_import is not None:
            ids = _wants_records(db, g)
            if ids:
                await before_import(session_factory, ids)

    changed = 0
    retry: list[int] = []
    with session_factory() as db:
        grabs = db.scalars(select(Grab).where(Grab.state.in_(ACTIVE))).all()
        clients: dict[str, object] = {}
        try:
            for g in grabs:
                if g.id in _importing:
                    continue
                if g.state == "importing":
                    # An import cut short (e.g. a restart): run it again from the saved path.
                    _importing.add(g.id)
                    try:
                        await records(g)
                        await asyncio.to_thread(_import_in_thread, session_factory, g.id, g.path or "")
                    finally:
                        _importing.discard(g.id)
                    changed += 1
                    continue
                client = clients.get(g.client)
                if client is None:
                    client = client_for(db, g.protocol, g.client)
                    if client is None:
                        # Said, not skipped for ever: a stuck "downloading" would block the issue.
                        g.state, g.finished = "failed", datetime.now(UTC)
                        g.message = f"{g.client or g.protocol} isn't set up in Settings any more"
                        log(
                            db,
                            f"Download '{g.release_title}' failed: {g.message}",
                            kind="grab",
                            level="error",
                        )
                        db.commit()
                        changed += 1
                        continue
                    clients[g.client] = client
                if not g.client_job_id:
                    continue
                try:
                    st = await client.status(g.client_job_id)
                except DownloaderError as exc:
                    g.message = str(exc)
                    db.commit()
                    continue
                if st.state in ("queued", "downloading"):
                    g.state, g.progress, g.message = st.state, st.progress, st.message
                    db.commit()
                elif st.state == "completed":
                    g.state, g.progress, g.path = "importing", 100.0, st.path
                    db.commit()
                    _importing.add(g.id)
                    try:
                        await records(g)
                        await asyncio.to_thread(_import_in_thread, session_factory, g.id, st.path or "")
                    finally:
                        _importing.discard(g.id)
                    if hasattr(client, "forget"):
                        client.forget(g.client_job_id)
                    changed += 1
                elif st.state == "missing" and getattr(client, "restartable", False) and g.download_url:
                    # Fumora's own downloads stop when it restarts: start it again.
                    try:
                        await client.add(g.download_url, g.release_title, job_id=g.client_job_id)
                        g.state, g.progress, g.message = "queued", 0.0, "Restarted after Fumora restarted"
                    except DownloaderError as exc:
                        g.state, g.message, g.finished = "failed", str(exc), datetime.now(UTC)
                    db.commit()
                elif st.state in ("failed", "missing"):
                    g.state, g.message, g.finished = "failed", st.message, datetime.now(UTC)
                    blocklist(db, g, st.message or "download failed")
                    if hasattr(client, "forget"):
                        client.forget(g.client_job_id)
                    retry += retry_candidates(db, g)
                    log(
                        db, f"Download failed: '{g.release_title}' ({st.message})", kind="grab", level="error"
                    )
                    db.commit()
                    changed += 1
        finally:
            for client in clients.values():
                await client.__aexit__(None, None, None)
    if retry and on_failed is not None:
        on_failed(sorted(set(retry)))
    return changed
