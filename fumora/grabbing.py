"""Grabbing releases, following downloads, importing finished ones (M4 part 2).

Import rules (TRaSH layout: downloads and library share one /data mount):
- usenet downloads are moved into the library (a rename on the same disk);
- torrent downloads are hardlinked, so they keep seeding (private trackers);
- never a silent copy across mounts: if a rename/hardlink isn't possible, the
  import fails with a message saying the mounts need fixing;
- CBR/CB7/CBT are converted into a new CBZ in the series folder; the download
  itself is left untouched until the CBZ is safely in place;
- the file is named by the naming template and, for usenet imports, tagged
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
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from . import convert, naming, settings_store
from .catalog import folder_for
from .downloaders import DownloaderError, QbittorrentClient, SabnzbdClient
from .importer import log
from .library_jobs import naming_settings
from .models import Blocklist, File, Grab, Issue, Series
from .numbers import normalize_number
from .scanner import COMIC_EXTS, parse_issue_number
from .search import Candidate
from .tagging import tag_file

logger = logging.getLogger("fumora.grabbing")
ACTIVE = ("queued", "downloading", "importing")
IMPORT_EXTS = COMIC_EXTS - {".pdf"}
# Folder names a cleanup must never remove, whatever the client reports.
PROTECTED_DIRS = {"complete", "completed", "incomplete", "downloads", "usenet", "torrents", "data", "fumora"}

_importing: set[int] = set()  # grabs being imported by this process right now


class ImportFailed(Exception):
    """An import that can't go ahead (explained in the message)."""


def client_for(db: Session, protocol: str):
    """The configured download client for a protocol, or None."""
    if protocol == "usenet":
        url, key = settings_store.get(db, "sabnzbd.url"), settings_store.get(db, "sabnzbd.api_key")
        if url and key:
            return SabnzbdClient(url, key, settings_store.get(db, "sabnzbd.category") or "fumora")
    elif protocol == "torrent":
        url = settings_store.get(db, "qbittorrent.url")
        user = settings_store.get(db, "qbittorrent.username")
        pw = settings_store.get(db, "qbittorrent.password")
        if url and user and pw:
            return QbittorrentClient(
                url, user, pw, settings_store.get(db, "qbittorrent.category") or "fumora"
            )
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
    for g in db.scalars(select(Grab).where(Grab.state.in_(ACTIVE))):
        if issue_id in (g.issue_ids or []) or g.issue_id == issue_id:
            return g
    return None


async def grab(db: Session, c: Candidate, issue: Issue) -> Grab:
    """Send a release to its download client and start following it.

    Nothing is recorded unless the client accepted it."""
    client = client_for(db, c.protocol)
    if client is None:
        raise DownloaderError(f"No {c.protocol} download client is set up (Settings)")
    url = c.release.get("downloadUrl") or c.release.get("magnetUrl")
    if not url:
        raise DownloaderError("This release has no download link")
    async with client:
        if c.protocol == "usenet":
            job_id = await client.add(url, c.parsed.title)
        else:
            job_id = await client.add(url, f"fumora-{uuid.uuid4().hex[:12]}")
    g = Grab(
        issue_id=issue.id,
        issue_ids=covered_issues(c, issue),
        series_id=issue.series_id,
        indexer=c.release.get("indexer") or "",
        release_title=_clean_title(c.release.get("title")),
        guid=c.release.get("guid"),
        download_url=url,
        protocol=c.protocol,
        size=c.release.get("size"),
        score=c.score,
        source=c.parsed.source,
        client="sabnzbd" if c.protocol == "usenet" else "qbittorrent",
        client_job_id=job_id,
        state="downloading",
    )
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


def _remove_download_folder(path: str, files_moved: list[Path]) -> None:
    """After a usenet import, remove the job's own folder if nothing useful is left.

    Only a folder that the client reported as this download's location, never a
    category or root folder, and never when the client reported a single file."""
    folder = Path(path)
    if not folder.is_dir() or folder.name.lower() in PROTECTED_DIRS or len(folder.parts) < 4:
        return
    try:
        if not any(p.suffix.lower() in IMPORT_EXTS for p in folder.rglob("*") if p.is_file()):
            shutil.rmtree(folder)
    except OSError:
        pass


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
    files = _comic_files(path)
    if not files:
        raise ImportFailed(f"No comic files found in {path}")

    opts = naming_settings(db)
    recycle = settings_store.get(db, "library.recycle_path") or "/data/media/fumora-recycle"
    tag_usenet = (settings_store.get(db, "import.tag") or "on") == "on"
    tag_torrent = (settings_store.get(db, "import.tag_torrents") or "off") == "on"
    imported, skipped, moved = [], [], []
    for f in files:
        if len(files) == 1 and len(issues) == 1:
            issue = next(iter(issues.values()))
        else:
            num = parse_issue_number(f.name, series.name) or parse_issue_number(f.name, series.mylar_name)
            want = normalize_number(num) if num else None
            issue = next((i for i in issues.values() if normalize_number(i.number) == want), None)
        if issue is None:
            skipped.append(f"{f.name}: no matching issue")
            continue
        if issue.files:
            skipped.append(f"{f.name}: #{issue.number} already has a file")
            continue
        name = naming.render(
            opts["file_template"], naming.context(series, issue), opts["colon"], opts["apostrophes"]
        )
        dest = Path(series.path) / f"{name}{f.suffix.lower()}"
        if dest.exists() or dest.with_suffix(".cbz").exists():
            skipped.append(f"{f.name}: {dest.with_suffix('.cbz').name} already exists")
            continue
        if convert.needs_conversion(f):
            placed = _place_converted(f, dest)
            if g.protocol == "usenet":
                f.unlink(missing_ok=True)  # the download's own copy, now converted
        else:
            placed = _place(f, dest, g.protocol)
        moved.append(f)
        row = File(path=str(placed), size=placed.stat().st_size)
        issue.files.append(row)
        db.commit()  # the file is in the library now: record it whatever happens next
        if series.writable and (tag_torrent if g.protocol == "torrent" else tag_usenet):
            tag_file(issue, row, dry_run=False, recycle_dir=recycle)
            db.commit()
        imported.append(f"#{issue.number}")
    if g.protocol == "usenet" and imported:
        _remove_download_folder(path, moved)
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


async def check_downloads(session_factory) -> int:
    """Poll active grabs; import finished ones. Returns how many changed state."""
    changed = 0
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
                        await asyncio.to_thread(_import_in_thread, session_factory, g.id, g.path or "")
                    finally:
                        _importing.discard(g.id)
                    changed += 1
                    continue
                client = clients.get(g.protocol)
                if client is None:
                    client = client_for(db, g.protocol)
                    if client is None or not g.client_job_id:
                        continue
                    clients[g.protocol] = client
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
                        await asyncio.to_thread(_import_in_thread, session_factory, g.id, st.path or "")
                    finally:
                        _importing.discard(g.id)
                    changed += 1
                elif st.state in ("failed", "missing"):
                    g.state, g.message, g.finished = "failed", st.message, datetime.now(UTC)
                    blocklist(db, g, st.message or "download failed")
                    log(
                        db, f"Download failed: '{g.release_title}' ({st.message})", kind="grab", level="error"
                    )
                    db.commit()
                    changed += 1
        finally:
            for client in clients.values():
                await client.__aexit__(None, None, None)
    return changed
