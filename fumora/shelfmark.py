"""Shelfmark: Anna's Archive (and Libgen) searches and downloads, handed off (M5).

Shelfmark already runs on the server with mirrors and challenge solving for
Anna's Archive, so Fumora doesn't scrape AA itself: it asks Shelfmark's API for
releases (`GET /api/releases?source=...&query=...`), queues the chosen one
(`POST /api/releases/download`), and follows it in `/api/status`. Shelfmark
saves the file under the shared /data mount; Fumora imports it from there.

The API key is optional (Shelfmark with AUTH_METHOD=none needs none); when set
it is sent as a header and never shown or logged.
"""

import json
import re

import httpx

from .downloaders import DownloaderError, DownloadStatus

DEFAULT_SOURCES = ("direct_download",)  # Anna's Archive; "libgen" too if enabled in Shelfmark
SOURCE_NAMES = {"direct_download": "Anna's Archive", "libgen": "Libgen"}
_UNITS = {"b": 1, "kb": 2**10, "mb": 2**20, "gb": 2**30}


class ShelfmarkError(Exception):
    pass


def _bytes(size: str | None) -> int | None:
    m = re.match(r"\s*([\d.,]+)\s*([KMG]?B)\b", size or "", re.I)
    if not m:
        return None
    try:
        return int(float(m.group(1).replace(",", "")) * _UNITS[m.group(2).lower()])
    except ValueError:
        return None


def clean_libgen_title(title: str) -> str:
    """Libgen comics titles are catalogue data, then ' c c <id> ', then the file name:
    'Black Hole #1 1995-mar 1 Biology 101 c c 1013165 Black Hole 01 (Charles Burns) 1024x1569px 300dpi'.
    Keep the catalogue's 'Series #N', its year, and the file name's bracketed tags
    (source, group); drop story titles, ISBNs and image sizes, whose numbers would
    confuse the release parser."""
    # Some rows end in the uploader's folder path: "... s:\\scene\\2026-04\\Yen.Press...".
    title = re.sub(r"\s+\S*\\\S.*$", "", title)
    # Libgen's catalogue writes the volume before the issue: "Walking Dead (The) 1 #100" is
    # volume 1, issue 100 of The Walking Dead, not a series called "Walking Dead 1".
    title = re.sub(r"^(.*?\S)\s(\d{1,2})\s(?=#\s?[\d½.-])", r"\1 v\2 ", title)
    m = re.match(r"^(?P<meta>.*?)\s+c c \d+\s+(?P<file>.*)$", title)
    if not m:
        return title
    meta, fname = m.group("meta"), m.group("file")
    year = re.search(r"\b((?:19|20)\d{2})-[a-z]{3}\b", meta)
    head = re.match(r"^(.*?#\s?[\d.]+)", meta)
    base = head.group(1) if head else re.sub(r"\s*\b\d{10,13}\b;?", "", meta).strip()
    tags = [
        t.strip()
        for t in re.findall(r"[\[(]([^\])]+)[\])]", fname)
        if not re.fullmatch(r"\s*\d+x\d+px|\d+dpi\s*", t)
    ]
    out = base
    if year and year.group(1) not in tags:
        out += f" ({year.group(1)})"
    return out + "".join(f" ({t})" for t in tags)


def to_release(r: dict) -> dict:
    """A Shelfmark release as a Prowlarr-shaped one. The format goes on the end of
    the title so the release parser sees EPUBs and PDFs for what they are."""
    extra = r.get("extra") or {}
    fmt = (r.get("format") or "").lower().strip(".")
    title = r.get("title") or ""
    if r.get("source") == "libgen":
        title = clean_libgen_title(title)
    if extra.get("year") and str(extra["year"]) not in title:
        title += f" ({extra['year']})"
    if fmt:
        title += f".{fmt}"
    source = r.get("source") or ""
    return {
        "title": title,
        "size": r.get("size_bytes") or _bytes(r.get("size")),
        "protocol": "ddl",
        "client": "shelfmark",
        "indexer": SOURCE_NAMES.get(source, r.get("indexer") or source or "Shelfmark"),
        "guid": f"shelfmark:{r.get('source_id')}",
        "infoUrl": r.get("info_url"),
        "language": r.get("language") or extra.get("language"),
        # Shelfmark needs the release back to queue it.
        "downloadUrl": json.dumps(r),
    }


class ShelfmarkClient:
    def __init__(
        self, url: str, api_key: str | None = None, transport: httpx.AsyncBaseTransport | None = None
    ):
        headers = {"Accept": "application/json"}
        if api_key:
            headers["X-Api-Key"] = api_key
        self._http = httpx.AsyncClient(
            base_url=url.rstrip("/"),
            headers=headers,
            timeout=httpx.Timeout(30.0, read=320.0),  # Shelfmark's own search budget is 300 s
            transport=transport,
        )
        self.category = "Shelfmark"

    async def __aenter__(self) -> "ShelfmarkClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kw) -> httpx.Response:
        try:
            resp = await self._http.request(method, path, **kw)
        except httpx.TransportError as exc:
            raise ShelfmarkError(f"Could not reach Shelfmark ({type(exc).__name__})") from exc
        if resp.status_code in (401, 403):
            raise ShelfmarkError("Shelfmark wants an API key (set SHELFMARK_API_KEY there and here)")
        return resp

    @staticmethod
    def _json(resp: httpx.Response):
        try:
            return resp.json()
        except ValueError as exc:
            raise ShelfmarkError("Shelfmark's answer wasn't JSON (wrong URL?)") from exc

    async def search(self, query: str, sources: tuple[str, ...] = DEFAULT_SOURCES) -> list[dict]:
        out = []
        for source in sources:
            resp = await self._request(
                "GET", "/api/releases", params={"source": source, "query": query, "content_type": "ebook"}
            )
            if resp.status_code >= 400:
                try:
                    data = resp.json()
                except ValueError:
                    data = None
                msg = (data or {}).get("error") if isinstance(data, dict) else None
                if resp.status_code == 503 and msg and "no books found" in msg.lower():
                    continue  # Shelfmark's way of saying "nothing"
                raise ShelfmarkError(f"Shelfmark ({source}): {msg or f'HTTP {resp.status_code}'}")
            data = self._json(resp)
            out += [to_release(r) for r in (data.get("releases") or []) if r.get("source_id")]
        return out

    async def check(self) -> str:
        resp = await self._request("GET", "/api/status")
        if resp.status_code >= 400:
            raise ShelfmarkError(f"Shelfmark returned HTTP {resp.status_code}")
        if not isinstance(self._json(resp), dict):
            raise ShelfmarkError("Unexpected answer from Shelfmark")
        return "ok"

    async def add(self, spec: str, name: str) -> str:
        try:
            release = json.loads(spec)
            task_id = release["source_id"]
        except (ValueError, KeyError, TypeError) as exc:
            raise DownloaderError("This Shelfmark release can't be queued (no source id)") from exc
        try:
            resp = await self._request("POST", "/api/releases/download", json=release)
        except ShelfmarkError as exc:
            raise DownloaderError(str(exc)) from exc
        if resp.status_code >= 400:
            try:
                msg = resp.json().get("error")
            except (ValueError, AttributeError):
                msg = None
            raise DownloaderError(f"Shelfmark didn't queue it: {msg or f'HTTP {resp.status_code}'}")
        return str(task_id)  # Shelfmark's task id is the release's source_id

    async def status(self, task_id: str) -> DownloadStatus:
        try:
            resp = await self._request("GET", "/api/status")
        except ShelfmarkError as exc:
            raise DownloaderError(str(exc)) from exc
        if resp.status_code >= 400:
            raise DownloaderError(f"Shelfmark returned HTTP {resp.status_code}")
        try:
            queues = resp.json()
        except ValueError as exc:
            raise DownloaderError("Shelfmark's answer wasn't JSON") from exc
        if not isinstance(queues, dict):
            raise DownloaderError("Unexpected answer from Shelfmark")
        for state, tasks in queues.items():
            task = tasks.get(task_id) if isinstance(tasks, dict) else None
            if not isinstance(task, dict):
                continue
            progress = float(task.get("progress") or 0)
            msg = task.get("status_message")
            if state == "complete":
                if not task.get("download_path"):
                    return DownloadStatus("failed", 100.0, None, "Shelfmark finished but the file is gone")
                return DownloadStatus("completed", 100.0, task["download_path"])
            if state in ("error", "cancelled"):
                return DownloadStatus("failed", progress, None, f"Shelfmark: {msg or state}")
            if state == "downloading":
                return DownloadStatus("downloading", progress, None, msg)
            return DownloadStatus("queued", progress, None, msg)  # queued, resolving, locating
        return DownloadStatus("missing", 0.0, None, "Not in Shelfmark's queue")
