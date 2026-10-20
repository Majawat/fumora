"""Metron API client.

Metron allows 20 requests a minute ("burst") and a daily quota ("sustained",
5,000 to start), per account, reported in X-RateLimit-* headers on every
response. A 429 response carries Retry-After. Every request in the process
goes through one RateLimiter, which:

- spaces requests evenly: at most `per_minute` a minute (default 20);
- stops when the account's burst window is nearly used up (the headers are
  account-wide, so this also covers other apps on the same account);
- after a 429 blocks *every* request for Retry-After seconds, plus a little
  random jitter (Metron asks that installs don't all come back at once);
- refuses with MetronDailyLimitReached when the daily quota is spent.
"""

import asyncio
import logging
import random
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx

BASE_URL = "https://metron.cloud/api/"
USER_AGENT = "Fumora/0.1 (+https://github.com/Majawat/fumora)"
DEFAULT_PER_MINUTE = 20
# A 429 without Retry-After (Metron always sends one) waits this long.
DEFAULT_RETRY_AFTER = 60.0
# Give up on one request after this many 429s in a row; the job then pauses.
MAX_RATE_LIMITED = 20

log = logging.getLogger("fumora.metron")


class MetronError(Exception):
    pass


class MetronAuthError(MetronError):
    pass


class MetronPause(MetronError):
    """Metron says stop for a while. Jobs wait until `reset_at` and carry on
    with the same item (they never skip ahead and fire again)."""

    def __init__(self, message: str, reset_at: float):
        super().__init__(message)
        self.reset_at = reset_at


class MetronDailyLimitReached(MetronPause):
    def __init__(self, reset_at: float):
        super().__init__(f"Metron daily limit reached; resets at {time.ctime(reset_at)}", reset_at)


class MetronRateLimited(MetronPause):
    def __init__(self, reset_at: float):
        super().__init__("Metron keeps rate-limiting requests; pausing", reset_at)


class RateLimiter:
    """One per process: paces every Metron request (see the module docstring)."""

    def __init__(
        self,
        per_minute: int = DEFAULT_PER_MINUTE,
        headroom: int = 1,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Any] = asyncio.sleep,
        min_interval: float | None = None,
        jitter: float = 0.2,
        name: str = "Metron",
    ):
        self.name = name  # for log lines: the same class paces ComicVine too
        self.per_minute = per_minute
        # Waits after errors grow by up to this fraction, at random (0 in tests).
        self.jitter = jitter
        self.headroom = headroom
        # Tests pass min_interval=0 to skip the pacing delay.
        self.min_interval = min_interval
        self._clock = clock
        self.sleep = sleep
        self._lock = asyncio.Lock()
        self._last_sent = 0.0
        self.blocked_until = 0.0
        self.burst_limit: int | None = None
        self.burst_remaining: int | None = None
        self.burst_reset: float | None = None
        self.sustained_remaining: int | None = None
        self.sustained_limit: int | None = None
        self.sustained_reset: float | None = None
        self.rejected = 0  # 429s seen since start
        self.last_rejected: float | None = None

    @property
    def interval(self) -> float:
        if self.min_interval is not None:
            return self.min_interval
        limit = min(self.per_minute, self.burst_limit or self.per_minute)
        return 60.0 / max(limit, 1)

    def _wait(self, now: float) -> float:
        wait = max(self.blocked_until - now, self._last_sent + self.interval - now)
        if (
            self.burst_remaining is not None
            and self.burst_remaining <= self.headroom
            and self.burst_reset is not None
            and self.burst_reset > now
        ):
            wait = max(wait, self.burst_reset - now + 1)
        return wait

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = self._clock()
                if (
                    self.sustained_remaining is not None
                    and self.sustained_remaining <= 0
                    and self.sustained_reset is not None
                    and now < self.sustained_reset
                ):
                    raise MetronDailyLimitReached(self.sustained_reset)
                wait = self._wait(now)
                if wait <= 0:
                    break
                await self.sleep(wait)
            self._last_sent = self._clock()
            if self.burst_remaining is not None:
                self.burst_remaining -= 1  # until the response tells us the real number

    def block(self, seconds: float) -> None:
        """Hold every request for `seconds` (after a 429, an outage, ...), with jitter."""
        seconds *= 1 + random.uniform(0, self.jitter)
        self.blocked_until = max(self.blocked_until, self._clock() + seconds)

    def on_rate_limited(self, retry_after: float | None) -> None:
        self.rejected += 1
        self.last_rejected = self._clock()
        self.block(retry_after if retry_after and retry_after > 0 else DEFAULT_RETRY_AFTER)
        log.warning(
            "%s is rate-limiting; holding all its requests for %.0fs",
            self.name,
            self.blocked_until - self._clock(),
        )

    def update(self, headers: httpx.Headers) -> None:
        def num(name: str) -> int | None:
            value = headers.get(name)
            try:
                return int(float(value)) if value is not None else None
            except ValueError:
                return None

        if (v := num("x-ratelimit-burst-limit")) is not None:
            self.burst_limit = v
        if (v := num("x-ratelimit-burst-remaining")) is not None:
            self.burst_remaining = v
        if (v := num("x-ratelimit-burst-reset")) is not None:
            self.burst_reset = float(v)
        if (v := num("x-ratelimit-sustained-limit")) is not None:
            self.sustained_limit = v  # the account's tier (5,000 standard; more for supporters)
        if (v := num("x-ratelimit-sustained-remaining")) is not None:
            self.sustained_remaining = v
        if (v := num("x-ratelimit-sustained-reset")) is not None:
            self.sustained_reset = float(v)


def _open(make_client, user, password, token):
    """Jobs get a client factory; pass the token only when there is one."""
    return make_client(user, password, token=token) if token else make_client(user, password)


class MetronClient:
    def __init__(
        self,
        username: str,
        password: str,
        limiter: RateLimiter,
        transport: httpx.AsyncBaseTransport | None = None,
        max_retries: int = 3,
        token: str | None = None,
    ):
        """An API token (Authorization: Bearer) is used when given; Metron is
        phasing out username/password (Basic) for apps."""
        self._limiter = limiter
        self._max_retries = max_retries
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._http = httpx.AsyncClient(
            base_url=BASE_URL,
            auth=None if token else (username, password),
            headers=headers,
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
        failures = 0  # network errors and 5xx
        rate_limited = 0
        while True:
            await self._limiter.acquire()
            try:
                resp = await self._http.get(path, params=params)
            except httpx.TransportError as exc:
                failures += 1
                if failures > self._max_retries:
                    raise MetronError(f"Could not reach Metron ({type(exc).__name__})") from exc
                self._limiter.block(5.0 * failures)
                continue
            self._limiter.update(resp.headers)
            if resp.status_code == 429:
                rate_limited += 1
                retry_after = resp.headers.get("retry-after")
                try:
                    seconds = float(retry_after) if retry_after else None
                except ValueError:
                    seconds = None
                self._limiter.on_rate_limited(seconds)
                if rate_limited >= MAX_RATE_LIMITED:
                    raise MetronRateLimited(time.time() + 600)
                continue  # acquire() now waits for everyone
            if resp.status_code in (401, 403):
                raise MetronAuthError("Metron rejected the username or password")
            if resp.status_code >= 500:
                failures += 1
                if failures > self._max_retries:
                    raise MetronError(f"Metron {path} returned HTTP {resp.status_code}")
                self._limiter.block(10.0 * failures)
                continue
            if resp.status_code >= 400:
                raise MetronError(f"Metron {path} returned HTTP {resp.status_code}")
            return resp.json()

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

    def issues_modified_since(self, since_iso: str) -> AsyncIterator[dict]:
        return self._paged("issue/", {"modified_gt": since_iso})

    def releases_between(self, start: str, end: str) -> AsyncIterator[dict]:
        return self._paged("issue/", {"store_date_range_after": start, "store_date_range_before": end})
