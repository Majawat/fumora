"""Running behind a reverse proxy (#8): one admin, one subdomain, https at the proxy.

- The proxy's X-Forwarded-* headers are believed only from `FUMORA_TRUSTED_PROXIES`
  (uvicorn `--forwarded-allow-ips`, set in `docker/entrypoint.sh`). Headers from anyone
  else are ignored by uvicorn and reported here (`ProxyCheck`), not silently accepted.
- `CrossOriginGuard` refuses state-changing requests a browser sends on behalf of another
  site (CSRF), using Sec-Fetch-Site with Origin as the fallback (the approach of Go's
  `http.CrossOriginProtection`). Requests without either header are not from a browser.
- `SecureSessionCookie` marks the session cookie Secure when the request came over https.
- `LoginLimiter` locks one address out after repeated wrong passwords.
"""

import logging
import time
from collections import deque
from urllib.parse import urlsplit

from starlette.datastructures import MutableHeaders
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger("fumora.security")

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
DEFAULT_PORTS = {"http": 80, "https": 443}


def _headers(scope: Scope) -> dict[str, str]:
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}


def _origin(scheme: str, host: str) -> tuple[str, str, int | None]:
    """(scheme, hostname, port) with the default port filled in, for comparing origins."""
    parts = urlsplit(f"{scheme}://{host}")
    try:
        port = parts.port
    except ValueError:
        port = None
    return parts.scheme, (parts.hostname or ""), port or DEFAULT_PORTS.get(parts.scheme)


def cross_origin_reason(scope: Scope) -> str | None:
    """Why this request looks like it came from another site, or None when it is fine."""
    if scope.get("method", "GET") in SAFE_METHODS:
        return None
    headers = _headers(scope)
    site = headers.get("sec-fetch-site")
    if site is not None:
        if site in ("same-origin", "none"):
            return None
        return f"Sec-Fetch-Site is {site!r}"
    origin = headers.get("origin")
    if origin is None:
        return None  # no browser sends a cross-site POST without Sec-Fetch-Site or Origin
    if origin == "null":
        return "Origin is 'null'"
    parts = urlsplit(origin)
    try:
        theirs = (parts.scheme, parts.hostname or "", parts.port or DEFAULT_PORTS.get(parts.scheme))
    except ValueError:
        return f"Origin {origin!r} is not valid"
    ours = _origin(scope.get("scheme", "http"), headers.get("host", ""))
    if theirs != ours:
        return f"Origin {origin!r} is not this site ({ours[0]}://{headers.get('host', '')})"
    return None


class CrossOriginGuard:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and (reason := cross_origin_reason(scope)):
            client = scope.get("client") or ("?", 0)
            log.warning(
                "Refused cross-site %s %s from %s: %s", scope["method"], scope["path"], client[0], reason
            )
            response = PlainTextResponse(
                f"Refused: this request came from another site ({reason}). Reload Fumora and try again.",
                status_code=403,
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


class SecureSessionCookie:
    """Adds `Secure` to the session cookie on https requests (the proxy's https, once trusted)."""

    def __init__(self, app: ASGIApp, cookie_name: str) -> None:
        self.app = app
        self.prefix = cookie_name + "="

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("scheme") != "https":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                cookies = headers.getlist("set-cookie")
                if any(c.startswith(self.prefix) for c in cookies):
                    del headers["set-cookie"]
                    for c in cookies:
                        if c.startswith(self.prefix) and "secure" not in c.lower().split("; "):
                            c += "; secure"
                        headers.append("set-cookie", c)
            await send(message)

        await self.app(scope, receive, send_wrapper)


class ProxyCheck:
    """Notices X-Forwarded-Proto that uvicorn did not apply: a proxy Fumora does not trust."""

    def __init__(self) -> None:
        self.problem: str | None = None

    def check(self, scope: Scope) -> None:
        proto = _headers(scope).get("x-forwarded-proto")
        if proto is None:
            return
        proto = proto.split(",")[0].strip().lower()
        if proto in ("http", "https") and proto != scope.get("scheme"):
            client = (scope.get("client") or ("?", 0))[0]
            problem = (
                f"A reverse proxy at {client} sends X-Forwarded-Proto: {proto}, but Fumora does not "
                "trust it, "
                "so it sees plain http and the proxy's address instead of the visitor's. "
                f"Add {client} to the FUMORA_TRUSTED_PROXIES variable of the container and restart it."
            )
            if problem != self.problem:
                log.warning(problem)
            self.problem = problem


class LoginLimiter:
    """At most `max_failures` wrong logins per address within `window` seconds."""

    def __init__(self, max_failures: int = 5, window: float = 15 * 60, clock=time.monotonic) -> None:
        self.max_failures = max_failures
        self.window = window
        self.clock = clock
        self._failures: dict[str, deque[float]] = {}

    def _recent(self, address: str) -> deque[float]:
        now = self.clock()
        q = self._failures.get(address, deque())
        while q and q[0] <= now - self.window:
            q.popleft()
        if q:
            self._failures[address] = q
        else:
            self._failures.pop(address, None)
        return q

    def locked_for(self, address: str) -> float:
        """Seconds until this address may try again (0 = it may now)."""
        q = self._recent(address)
        if len(q) < self.max_failures:
            return 0.0
        return max(0.0, q[-self.max_failures] + self.window - self.clock())

    def failed(self, address: str) -> bool:
        """Records a wrong login; True when this one locked the address out."""
        q = self._recent(address)
        q.append(self.clock())
        self._failures[address] = q
        return len(q) == self.max_failures

    def succeeded(self, address: str) -> None:
        self._failures.pop(address, None)
