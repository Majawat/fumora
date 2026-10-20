"""ComicVine client: the fallback source for series Metron doesn't have (M5b).

Only the series Fumora follows are fetched (their issue list, by ComicVine
volume id), never a site-wide feed. ComicVine allows about 200 requests an hour
per resource and blocks clients that go faster, so every request goes through
one RateLimiter (the Metron limiter's class, with its own instance): evenly
spaced, a rate-limit answer holds every request (with jitter).

The API key is a secret: it is sent as a query parameter (ComicVine's only
way), so request URLs are never logged.
"""

import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .metron import RateLimiter

BASE_URL = "https://comicvine.gamespot.com/api/"
USER_AGENT = "Fumora/0.1 (+https://github.com/Majawat/fumora)"
PER_MINUTE = 3  # 180 an hour: under ComicVine's 200 per resource
RATE_LIMITED_WAIT = 3600.0  # ComicVine's window is an hour
MAX_RETRIES = 3
PAGE = 100
VOLUME_PREFIX = "4050-"

log = logging.getLogger("fumora.comicvine")


class ComicVineError(Exception):
    pass


class ComicVineAuthError(ComicVineError):
    pass


class ComicVineRateLimited(ComicVineError):
    """ComicVine said slow down: every request is held for an hour."""


def make_limiter() -> RateLimiter:
    return RateLimiter(per_minute=PER_MINUTE, name="ComicVine")


def start_year(vol: dict) -> int | None:
    try:
        return int(vol.get("start_year") or 0) or None
    except (TypeError, ValueError):
        return None


class ComicVineClient:
    def __init__(
        self,
        api_key: str,
        limiter: RateLimiter,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._key = api_key
        self._limiter = limiter
        self._http = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=30.0,
            transport=transport,
        )

    async def __aenter__(self) -> "ComicVineClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self._http.aclose()

    async def _get(self, path: str, params: dict[str, Any]) -> dict:
        failures = 0
        while True:
            await self._limiter.acquire()
            try:
                resp = await self._http.get(path, params={**params, "api_key": self._key, "format": "json"})
            except httpx.TransportError as exc:
                failures += 1
                if failures > MAX_RETRIES:
                    raise ComicVineError(f"Could not reach ComicVine ({type(exc).__name__})") from exc
                self._limiter.block(10.0 * failures)
                continue
            if resp.status_code in (420, 429):
                self._limiter.on_rate_limited(RATE_LIMITED_WAIT)
                raise ComicVineRateLimited("ComicVine is rate-limiting; holding requests for an hour")
            if resp.status_code == 401:
                raise ComicVineAuthError("ComicVine rejected the API key")
            if resp.status_code >= 500:
                failures += 1
                if failures > MAX_RETRIES:
                    raise ComicVineError(f"ComicVine returned HTTP {resp.status_code}")
                self._limiter.block(30.0 * failures)
                continue
            try:
                data = resp.json()
            except ValueError as exc:
                raise ComicVineError(f"ComicVine's answer wasn't JSON (HTTP {resp.status_code})") from exc
            code = data.get("status_code")
            if code == 100:
                raise ComicVineAuthError("ComicVine rejected the API key")
            if code == 107:
                self._limiter.on_rate_limited(RATE_LIMITED_WAIT)
                raise ComicVineRateLimited("ComicVine is rate-limiting; holding requests for an hour")
            if code == 101:
                return {"results": None, "not_found": True}
            if code != 1:
                raise ComicVineError(f"ComicVine: {data.get('error') or f'status {code}'}")
            return data

    async def volume(self, cv_id: int) -> dict | None:
        """A volume (ComicVine's word for a series): name, start year, publisher."""
        data = await self._get(
            f"volume/{VOLUME_PREFIX}{cv_id}/",
            {"field_list": "id,name,start_year,publisher,count_of_issues,site_detail_url,deck"},
        )
        return data.get("results") or None

    async def volume_issues(self, cv_id: int) -> AsyncIterator[dict]:
        """Every issue of a volume, oldest first (a page of 100 per request)."""
        offset = 0
        while True:
            data = await self._get(
                "issues/",
                {
                    "filter": f"volume:{cv_id}",
                    "field_list": "id,issue_number,name,cover_date,store_date,date_last_updated",
                    "sort": "cover_date:asc",
                    "limit": PAGE,
                    "offset": offset,
                },
            )
            results = data.get("results") or []
            for r in results:
                yield r
            offset += len(results)
            if not results or offset >= (data.get("number_of_total_results") or 0):
                return

    async def search_volumes(self, query: str, limit: int = 20) -> list[dict]:
        data = await self._get(
            "search/",
            {
                "query": query,
                "resources": "volume",
                "limit": limit,
                "field_list": "id,name,start_year,publisher,count_of_issues,site_detail_url,image",
            },
        )
        return data.get("results") or []

    async def check(self) -> str:
        await self._get("types/", {"field_list": "id"})
        return "ok"
