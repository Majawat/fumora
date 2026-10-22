"""Direct downloads run by Fumora itself (GetComics' file hosts).

Same interface as the SABnzbd and qBittorrent clients (add / status / check), so
grabbing and the download monitor treat it like any other client. Each job gets
its own folder under the direct-download folder (on the shared /data mount, so
the import is a rename). Jobs live in memory: after a restart the monitor sees
the job as missing and starts it again (`grabbing.check_downloads`).

A GetComics pack is often a .zip/.rar of CBZ/CBR files; it is unpacked so the
import can map each file to its issue. An archive of images is a comic and is
just given a comic extension.
"""

import asyncio
import json
import logging
import re
import shutil
import uuid
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx

from .convert import ArchiveError, _bsdtar, sniff
from .downloaders import DownloaderError, DownloadStatus
from .getcomics import USER_AGENT, GetComicsClient, GetComicsError, Post, is_challenge, parse_post
from .perms import normalize_tree

logger = logging.getLogger("fumora.ddl")
COMIC_INSIDE = (".cbz", ".cbr", ".cb7", ".cbt", ".pdf")
ARCHIVE_COMIC_EXT = {"zip": ".cbz", "rar": ".cbr", "7z": ".cb7", "tar": ".cbt"}
MAX_PARALLEL = 2

_jobs: dict[str, DownloadStatus] = {}
_tasks: dict[str, asyncio.Task] = {}
_folders: dict[str, Path] = {}  # each job's own folder (the setting may change meanwhile)
_unpacking: set[str] = set()  # jobs inside the unpack thread, which can't be interrupted
_cancelled: set[str] = set()  # cancelled while unpacking: cleaned up once the thread ends
_slots: dict[int, asyncio.Semaphore] = {}  # one per event loop (tests run several)


def _slot() -> asyncio.Semaphore:
    loop = id(asyncio.get_running_loop())
    if loop not in _slots:
        _slots[loop] = asyncio.Semaphore(MAX_PARALLEL)
    return _slots[loop]


class LinkFailed(Exception):
    pass


def _safe_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", unquote(name)).strip(" .")
    return name[:200] or "download"


def _filename(resp: httpx.Response) -> str:
    cd = resp.headers.get("content-disposition") or ""
    m = re.search(r"filename\*=UTF-8''([^;]+)", cd, re.I) or re.search(r'filename="?([^";]+)"?', cd, re.I)
    if m:
        return _safe_name(m.group(1))
    return _safe_name(urlsplit(str(resp.url)).path.rsplit("/", 1)[-1])


def _pixeldrain_api(url: str) -> str:
    """pixeldrain.com/u/<id> is a web page; /api/file/<id> is the file. (A /l/ list
    is an album, not a file: left alone, so it fails as a web page.)"""
    m = re.match(r"https?://(?:www\.)?pixeldrain\.com/u/([A-Za-z0-9]+)", url)
    return f"https://pixeldrain.com/api/file/{m.group(1)}?download" if m else url


class DirectClient:
    # What the download monitor and import ask of a client (SABnzbd sets some too).
    restartable = True  # jobs live in memory: start them again after a restart
    owns_job_folders = True  # an imported job's folder may be removed

    def __init__(
        self,
        folder: str,
        getcomics_url: str | None = None,
        flaresolverr_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.folder = Path(folder)
        self.category = self.folder.name
        self._gc_url = getcomics_url
        self._solver = flaresolverr_url
        self._transport = transport

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def check(self) -> str:
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            probe = self.folder / f".fumora-probe-{uuid.uuid4().hex[:6]}"
            probe.write_bytes(b"")
            probe.unlink()
        except OSError as exc:
            raise DownloaderError(f"Can't write to {self.folder}: {exc.strerror or exc}") from exc
        return str(self.folder)

    async def add(self, spec: str, name: str, job_id: str | None = None) -> str:
        try:
            data = json.loads(spec)
            if not data.get("links") and not data.get("post"):
                raise ValueError
        except (ValueError, AttributeError) as exc:
            raise DownloaderError("This release has no usable download links") from exc
        job_id = job_id or f"ddl-{uuid.uuid4().hex[:12]}"
        old = _tasks.get(job_id)
        if old is not None and not old.done():
            return job_id
        _jobs[job_id] = DownloadStatus("queued", 0.0)
        _folders[job_id] = self.folder / job_id
        _tasks[job_id] = asyncio.create_task(self._run(job_id, data, name))
        return job_id

    async def status(self, job_id: str) -> DownloadStatus:
        st = _jobs.get(job_id)
        if st is None:
            return DownloadStatus("missing", 0.0, None, "Not running (Fumora restarted?)")
        return st

    def forget(self, job_id: str) -> None:
        """The monitor has dealt with a finished or failed job."""
        if job_id not in _tasks:
            _jobs.pop(job_id, None)
            _folders.pop(job_id, None)

    def cancel(self, job_id: str) -> None:
        """Stop a download (the grab was removed or retried) and drop its folder.
        A pack being unpacked finishes first (a thread can't be stopped) and is
        cleaned up right after."""
        _jobs.pop(job_id, None)
        if job_id in _unpacking:
            _cancelled.add(job_id)
            return
        task = _tasks.pop(job_id, None)
        if task is not None:
            task.cancel()
        shutil.rmtree(_folders.pop(job_id, self.folder / job_id), ignore_errors=True)

    async def _run(self, job_id: str, data: dict, name: str) -> None:
        folder = _folders.get(job_id, self.folder / job_id)
        try:
            async with _slot():
                _jobs[job_id] = DownloadStatus("downloading", 0.0)
                if folder.exists():
                    shutil.rmtree(folder)  # leftovers of an interrupted attempt
                folder.mkdir(parents=True)
                post = data.get("post")
                errors = await self._try_links(job_id, folder, data.get("links") or [], post)
                if (errors or not data.get("links")) and post:
                    # The post's links may have expired: read the post again once.
                    errors += await self._try_links(job_id, folder, await self._fresh_links(data), post)
                files = [f for f in folder.iterdir() if f.is_file()]
                if not files:
                    raise LinkFailed("; ".join(errors) or "no download links")
                _unpacking.add(job_id)
                try:
                    await asyncio.to_thread(_unpack, files[0])
                finally:
                    _unpacking.discard(job_id)
                if job_id in _cancelled:
                    _cancelled.discard(job_id)
                    shutil.rmtree(folder, ignore_errors=True)
                    _folders.pop(job_id, None)
                    return
                _jobs[job_id] = DownloadStatus("completed", 100.0, str(folder))
        except LinkFailed as exc:
            _jobs[job_id] = DownloadStatus("failed", 0.0, None, str(exc)[:500])
        except Exception as exc:  # report it on the Queue page; never crash the loop
            logger.exception("direct download %s failed", job_id)
            _jobs[job_id] = DownloadStatus("failed", 0.0, None, f"{type(exc).__name__}: {exc}"[:500])
        finally:
            _tasks.pop(job_id, None)

    async def _fresh_links(self, data: dict) -> list:
        try:
            async with GetComicsClient(self._gc_url, self._solver, transport=self._transport) as gc:
                page = await gc.get(data["post"])
        except GetComicsError as exc:
            logger.info("re-reading %s failed: %s", data["post"], exc)
            return []
        guid = f"{data['post']}#{data.get('group') or 0}"
        for rel in parse_post(page, Post(title="", url=data["post"])):
            if rel["guid"] == guid:
                return json.loads(rel["downloadUrl"])["links"]
        return []

    async def _try_links(self, job_id: str, folder: Path, links: list, page: str | None = None) -> list[str]:
        """Download from the first host that works; returns the errors of those that didn't
        (an empty list means a file is in `folder`)."""
        errors = []
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, read=120.0),
            transport=self._transport,
            headers={"User-Agent": USER_AGENT},
        ) as http:
            for host, url in links:
                try:
                    await self._fetch(http, job_id, folder, url, page)
                    return []
                except LinkFailed as exc:
                    errors.append(f"{host}: {exc}")
                except httpx.HTTPError as exc:
                    errors.append(f"{host}: {type(exc).__name__}")
                for part in folder.iterdir():
                    part.unlink()
        return errors

    async def _fetch(
        self, http: httpx.AsyncClient, job_id: str, folder: Path, url: str, page: str | None = None
    ) -> None:
        # Follow redirects by hand to learn the real host (a Pixeldrain page link
        # becomes its file API) and stream the file from wherever it ends up.
        target = url
        solved = False
        for _ in range(7):
            target = _pixeldrain_api(target)
            async with http.stream("GET", target, follow_redirects=False) as resp:
                location = resp.headers.get("location")
                if resp.is_redirect and location:
                    target = str(resp.url.join(location))
                    continue
                if resp.status_code in (403, 429, 503) and self._solver and not solved:
                    await resp.aread()
                    if is_challenge(resp):
                        solved = True
                        await self._clearance(http, page or target)
                        continue  # same link again, now with the clearance cookies
                await self._save(resp, job_id, folder)
                return
        raise LinkFailed("too many redirects")

    async def _clearance(self, http: httpx.AsyncClient, page: str) -> None:
        """Cloudflare is challenging GetComics: let FlareSolverr/byparr open the
        post and copy its cookies and browser identity to the download client."""
        try:
            async with GetComicsClient(self._gc_url, self._solver, transport=self._transport) as gc:
                await gc._solve(page)
                http.cookies.update(gc._http.cookies)
                http.headers["User-Agent"] = gc._http.headers["User-Agent"]
        except GetComicsError as exc:
            raise LinkFailed(f"Cloudflare challenge: {exc}") from exc

    async def _save(self, resp: httpx.Response, job_id: str, folder: Path) -> None:
        if resp.status_code >= 400:
            raise LinkFailed(f"HTTP {resp.status_code}")
        if "text/html" in (resp.headers.get("content-type") or ""):
            raise LinkFailed("got a web page, not a file (link expired or host changed)")
        total = int(resp.headers.get("content-length") or 0)
        dest = folder / _filename(resp)
        part = dest.with_name(dest.name + ".part")
        done = 0
        with open(part, "wb") as fh:
            async for chunk in resp.aiter_bytes(1 << 20):
                fh.write(chunk)
                done += len(chunk)
                if total:
                    _jobs[job_id] = DownloadStatus("downloading", min(99.0, 100.0 * done / total))
        if total and done < total:
            raise LinkFailed(f"download cut short ({done} of {total} bytes)")
        if done == 0:
            raise LinkFailed("empty file")
        part.rename(dest)


def _unpack(path: Path) -> None:
    """A pack of comic files is unpacked next to itself; an archive of images is a
    comic and gets a comic extension. Anything else is left alone."""
    suffix = path.suffix.lower()
    if suffix not in (".zip", ".rar", ".7z", ".tar"):
        return
    kind = sniff(path)
    try:
        listing = _bsdtar("-tf", str(path))
    except ArchiveError:
        return
    if listing.returncode != 0:
        msg = listing.stderr.decode(errors="replace").strip()[:200]
        raise ArchiveError(f"couldn't read {path.name} (damaged or password-protected?): {msg}")
    names = [n for n in listing.stdout.decode(errors="replace").splitlines() if n and not n.endswith("/")]
    if any(n.lower().endswith(COMIC_INSIDE) for n in names):
        out = _bsdtar("-xf", str(path), "-C", str(path.parent))
        if out.returncode != 0:
            raise ArchiveError(f"couldn't unpack {path.name}: {out.stderr.decode(errors='replace')[:200]}")
        path.unlink()
        normalize_tree(path.parent)  # unpacked files would keep the archive's permissions
    elif kind in ARCHIVE_COMIC_EXT:
        path.rename(path.with_suffix(ARCHIVE_COMIC_EXT[kind]))
