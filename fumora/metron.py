"""Metron API client.

Metron allows a burst of 20 requests and 5,000 per day per user, and reports
both in X-Ratelimit-* headers. Every request goes through one RateLimiter so
the whole app shares the budget.
"""

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx

BASE_URL = "https://metron.cloud/api/"
USER_AGENT = "Fumora/0.1 (+https://github.com/Majawat/fumora)"


class MetronError(Exception):
    pass


class MetronAuthError(MetronError):
    pass


class MetronDailyLimitReached(MetronError):
    def __init__(self, reset_at: float):
        super().__init__(f"Metron daily limit reached; resets at {time.ctime(reset_at)}")
        self.reset_at = reset_at


class RateLimiter:
    """Spaces requests and obeys Metron's rate-limit headers.

    - Never sends faster than one request per `min_interval` seconds.
    - When the burst budget is spent, waits until the burst window resets.
    - When the daily budget is spent, refuses with MetronDailyLimitReached
      instead of blocking for hours.
    """

    def __init__(
        self,
        min_interval: float = 1.0,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ):
        self.min_interval = min_interval
        self._clock = clock
        self.sleep = sleep
        self._lock = asyncio.Lock()
        self._last_sent = 0.0
        self.burst_remaining: int | None = None
        self.burst_reset: float | None = None
        self.sustained_remaining: int | None = None
        self.sustained_reset: float | None = None

    async def acquire(self) -> None:
        async with self._lock:
            now = self._clock()
            if (
                self.sustained_remaining is not None
                and self.sustained_remaining <= 0
                and self.sustained_reset is not None
                and now < self.sustained_reset
            ):
                raise MetronDailyLimitReached(self.sustained_reset)

            wait = 0.0
            if (
                self.burst_remaining is not None
                and self.burst_remaining <= 0
                and self.burst_reset is not None
            ):
                wait = max(wait, self.burst_reset - now)
            wait = max(wait, self._last_sent + self.min_interval - now)
            if wait > 0:
                await self.sleep(wait)
            self._last_sent = self._clock()

    def update(self, headers: httpx.Headers) -> None:
        def num(name: str) -> int | None:
            value = headers.get(name)
            try:
                return int(value) if value is not None else None
            except ValueError:
                return None

        if (v := num("x-ratelimit-burst-remaining")) is not None:
            self.burst_remaining = v
        if (v := num("x-ratelimit-burst-reset")) is not None:
            self.burst_reset = float(v)
        if (v := num("x-ratelimit-sustained-remaining")) is not None:
            self.sustained_remaining = v
        if (v := num("x-ratelimit-sustained-reset")) is not None:
            self.sustained_reset = float(v)


class MetronClient:
    def __init__(
        self,
        username: str,
        password: str,
        limiter: RateLimiter,
        transport: httpx.AsyncBaseTransport | None = None,
        max_retries: int = 3,
    ):
        self._limiter = limiter
        self._max_retries = max_retries
        self._http = httpx.AsyncClient(
            base_url=BASE_URL,
            auth=(username, password),
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=30.0,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "MetronClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def _get(self, path: str, params: dict | None = None) -> dict:
        for attempt in range(self._max_retries + 1):
            await self._limiter.acquire()
            try:
                resp = await self._http.get(path, params=params)
            except httpx.TransportError as exc:
                if attempt < self._max_retries:
                    await self._limiter.sleep(5.0 * (attempt + 1))
                    continue
                raise MetronError(f"Could not reach Metron ({type(exc).__name__})") from exc
            self._limiter.update(resp.headers)
            if resp.status_code == 429 and attempt < self._max_retries:
                retry_after = resp.headers.get("retry-after")
                await self._limiter.sleep(float(retry_after) if retry_after else 60.0)
                continue
            if resp.status_code in (401, 403):
                raise MetronAuthError("Metron rejected the username or password")
            if resp.status_code >= 500 and attempt < self._max_retries:
                await self._limiter.sleep(5.0 * (attempt + 1))
                continue
            if resp.status_code >= 400:
                raise MetronError(f"Metron {path} returned HTTP {resp.status_code}")
            return resp.json()
        raise MetronError(f"Metron {path} failed after {self._max_retries} retries")

    async def _paged(
        self, path: str, params: dict | None = None, max_pages: int | None = None
    ) -> AsyncIterator[dict]:
        page = 1
        while True:
            data = await self._get(path, {**(params or {}), "page": page})
            for item in data.get("results", []):
                yield item
            if not data.get("next") or (max_pages is not None and page >= max_pages):
                return
            page += 1

    # --- endpoints -------------------------------------------------------

    async def check_login(self) -> None:
        """Cheap authenticated call; raises MetronAuthError on bad credentials."""
        await self._get("series_type/")

    async def search_series(self, name: str, year_began: int | None = None, max_pages: int = 2) -> list[dict]:
        params: dict = {"name": name}
        if year_began is not None:
            params["year_began"] = year_began
        return [s async for s in self._paged("series/", params, max_pages=max_pages)]

    async def series_by_cv_id(self, cv_id: int) -> dict | None:
        data = await self._get("series/", {"cv_id": cv_id})
        results = data.get("results", [])
        return results[0] if results else None

    async def series(self, metron_id: int) -> dict:
        return await self._get(f"series/{metron_id}/")

    def series_issues(self, metron_series_id: int) -> AsyncIterator[dict]:
        return self._paged("issue/", {"series_id": metron_series_id})

    async def issue(self, metron_id: int) -> dict:
        return await self._get(f"issue/{metron_id}/")

    def series_modified_since(self, since_iso: str) -> AsyncIterator[dict]:
        return self._paged("series/", {"modified_gt": since_iso})

    def releases_between(self, start: str, end: str) -> AsyncIterator[dict]:
        return self._paged("issue/", {"store_date_range_after": start, "store_date_range_before": end})
