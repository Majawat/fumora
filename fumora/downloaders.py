"""Download clients: SABnzbd (usenet) and qBittorrent (torrents).

Both are given Prowlarr's download link (which carries the Prowlarr API key;
it is passed through, never shown or logged) and a category ("fumora"), and
are then polled for progress. Paths they report are under the shared /data
mount (TRaSH layout), the same paths Fumora sees.
"""

from dataclasses import dataclass

import httpx


class DownloaderError(Exception):
    pass


@dataclass
class DownloadStatus:
    state: str  # queued, downloading, completed, failed, missing
    progress: float = 0.0  # 0-100
    path: str | None = None  # where the finished download is (file or folder)
    message: str | None = None


class SabnzbdClient:
    owns_job_folders = True  # an imported job's folder may be removed (see grabbing)

    def __init__(self, url: str, api_key: str, category: str = "fumora", transport=None):
        self.category = category
        self._key = api_key
        self._http = httpx.AsyncClient(base_url=url.rstrip("/"), timeout=60.0, transport=transport)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self._http.aclose()

    async def _api(self, **params) -> dict:
        try:
            resp = await self._http.get("/api", params={**params, "apikey": self._key, "output": "json"})
        except httpx.TransportError as exc:
            raise DownloaderError(f"Could not reach SABnzbd ({type(exc).__name__})") from exc
        if resp.status_code >= 400:
            raise DownloaderError(f"SABnzbd returned HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise DownloaderError("SABnzbd's answer wasn't JSON (wrong URL?)") from exc
        if isinstance(data, dict) and data.get("error"):
            raise DownloaderError(f"SABnzbd: {data['error']}")
        return data

    async def check(self) -> str:
        return (await self._api(mode="version")).get("version", "")

    async def add(self, url: str, name: str) -> str:
        """Queue an NZB by URL; returns SABnzbd's job id (nzo_id)."""
        data = await self._api(mode="addurl", name=url, nzbname=name, cat=self.category)
        ids = data.get("nzo_ids") or []
        if not data.get("status") or not ids:
            raise DownloaderError("SABnzbd didn't accept the NZB")
        return ids[0]

    async def status(self, nzo_id: str) -> DownloadStatus:
        queue = (await self._api(mode="queue", nzo_ids=nzo_id)).get("queue", {})
        for slot in queue.get("slots") or []:
            if slot.get("nzo_id") == nzo_id:
                try:
                    pct = float(slot.get("percentage") or 0)
                except ValueError:
                    pct = 0.0
                state = (
                    "queued" if (slot.get("status") or "").lower() in ("queued", "paused") else "downloading"
                )
                return DownloadStatus(state, pct)
        history = (await self._api(mode="history", nzo_ids=nzo_id)).get("history", {})
        for slot in history.get("slots") or []:
            if slot.get("nzo_id") != nzo_id:
                continue
            status = (slot.get("status") or "").lower()
            if status == "completed":
                return DownloadStatus("completed", 100.0, slot.get("storage"))
            if status == "failed":
                return DownloadStatus("failed", 0.0, None, slot.get("fail_message") or "Download failed")
            return DownloadStatus("downloading", 99.0, None, f"SABnzbd: {slot.get('status')}")
        return DownloadStatus("missing", 0.0, None, "Not in SABnzbd's queue or history")


class QbittorrentClient:
    """qBittorrent Web API v2. Each grab is tagged 'fumora-<grab id>' so it can be
    found again (adding by URL doesn't return the torrent's hash)."""

    def __init__(self, url: str, username: str, password: str, category: str = "fumora", transport=None):
        self.category = category
        self._user, self._pw = username, password
        self._http = httpx.AsyncClient(base_url=url.rstrip("/"), timeout=60.0, transport=transport)
        self._logged_in = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self._http.aclose()

    async def _login(self) -> None:
        if self._logged_in:
            return
        try:
            resp = await self._http.post(
                "/api/v2/auth/login",
                data={"username": self._user, "password": self._pw},
                headers={"Referer": str(self._http.base_url)},
            )
        except httpx.TransportError as exc:
            raise DownloaderError(f"Could not reach qBittorrent ({type(exc).__name__})") from exc
        if resp.status_code != 200 or resp.text.strip() != "Ok.":
            raise DownloaderError("qBittorrent rejected the username or password")
        self._logged_in = True

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        await self._login()
        try:
            resp = await self._http.request(method, path, **kwargs)
        except httpx.TransportError as exc:
            raise DownloaderError(f"Could not reach qBittorrent ({type(exc).__name__})") from exc
        if resp.status_code == 403:  # session expired: log in again once
            self._logged_in = False
            await self._login()
            try:
                resp = await self._http.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                raise DownloaderError(f"Could not reach qBittorrent ({type(exc).__name__})") from exc
        return resp

    async def check(self) -> str:
        return (await self._request("GET", "/api/v2/app/version")).text

    async def add(self, url: str, tag: str) -> str:
        resp = await self._request(
            "POST", "/api/v2/torrents/add", data={"urls": url, "category": self.category, "tags": tag}
        )
        if resp.status_code != 200 or resp.text.strip() not in ("Ok.", ""):
            raise DownloaderError(f"qBittorrent didn't accept the torrent ({resp.text.strip()[:80]})")
        return tag

    async def status(self, tag: str) -> DownloadStatus:
        resp = await self._request("GET", "/api/v2/torrents/info", params={"tag": tag})
        if resp.status_code != 200:
            raise DownloaderError(f"qBittorrent returned HTTP {resp.status_code}")
        try:
            torrents = resp.json()
        except ValueError as exc:
            raise DownloaderError("qBittorrent's answer wasn't JSON") from exc
        if not torrents:
            return DownloadStatus("missing", 0.0, None, "Not in qBittorrent")
        t = torrents[0]
        state = t.get("state") or ""
        progress = float(t.get("progress") or 0) * 100
        if state in ("error", "missingFiles"):
            return DownloadStatus("failed", progress, None, f"qBittorrent: {state}")
        if progress >= 100 or state in (
            "uploading",
            "stalledUP",
            "pausedUP",
            "stoppedUP",
            "queuedUP",
            "forcedUP",
        ):
            return DownloadStatus("completed", 100.0, t.get("content_path"))
        return DownloadStatus("queued" if state.startswith("queued") else "downloading", progress)
