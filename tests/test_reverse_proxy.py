"""Running behind a reverse proxy (#8): CSRF guard, Secure cookie, login limit, proxy notice."""

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from fumora.models import Event
from fumora.web_security import LoginLimiter, cross_origin_reason

SETUP = {"username": "admin", "password": "correct horse", "password2": "correct horse"}


def _scope(method="POST", scheme="http", host="fumora.local:8792", **headers):
    raw = [(b"host", host.encode())]
    raw += [(k.replace("_", "-").encode(), v.encode()) for k, v in headers.items()]
    return {"type": "http", "method": method, "scheme": scheme, "headers": raw, "path": "/x"}


# --- cross-site requests -------------------------------------------------------------


def test_cross_origin_rules():
    assert cross_origin_reason(_scope(method="GET", sec_fetch_site="cross-site")) is None
    assert cross_origin_reason(_scope(sec_fetch_site="same-origin")) is None
    assert cross_origin_reason(_scope(sec_fetch_site="none")) is None  # typed in / bookmark
    assert cross_origin_reason(_scope(sec_fetch_site="cross-site"))
    # Another *.majawat.com app is a different origin too.
    assert cross_origin_reason(_scope(sec_fetch_site="same-site"))
    # Older browsers: Origin only.
    assert cross_origin_reason(_scope(origin="http://fumora.local:8792")) is None
    assert cross_origin_reason(_scope(origin="http://evil.example"))
    assert cross_origin_reason(_scope(origin="null"))
    assert cross_origin_reason(_scope(origin="https://fumora.local:8792"))  # scheme differs
    # Default ports compare equal whether or not they are written.
    site = "https://comics.example.com"
    assert cross_origin_reason(_scope(scheme="https", host="comics.example.com", origin=site)) is None
    assert cross_origin_reason(_scope(scheme="https", host="comics.example.com:443", origin=site)) is None
    # No browser headers at all: not a browser, nothing to forge.
    assert cross_origin_reason(_scope()) is None
    # Malformed headers are refused, not a crash (Gemini review).
    assert cross_origin_reason(_scope(origin="http://[::1"))
    assert cross_origin_reason(_scope(host="[::1", origin="http://[::1]"))


def test_cross_site_post_is_refused_and_changes_nothing(logged_in):
    headers = {"Sec-Fetch-Site": "cross-site", "Origin": "https://evil.example"}
    r = logged_in.post("/logout", headers=headers, follow_redirects=False)
    assert r.status_code == 403
    assert "another site" in r.text
    assert logged_in.get("/", follow_redirects=False).status_code == 200  # still logged in


def test_cross_site_login_is_refused(client):
    client.post("/setup", data=SETUP)
    client.post("/logout")
    r = client.post(
        "/login",
        data={"username": "admin", "password": "correct horse"},
        headers={"Origin": "http://evil.example"},
        follow_redirects=False,
    )
    assert r.status_code == 403


def test_same_origin_post_works(logged_in):
    r = logged_in.post(
        "/logout",
        headers={"Sec-Fetch-Site": "same-origin", "Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert r.status_code == 303


# --- Secure cookie --------------------------------------------------------------------


def _session_cookie(r) -> str:
    return next(c for c in r.headers.get_list("set-cookie") if c.startswith("fumora_session="))


def test_cookie_is_secure_over_https(app):
    with TestClient(app, base_url="https://comics.example.com") as c:
        r = c.post("/setup", data=SETUP, follow_redirects=False)
        cookie = _session_cookie(r).lower()
        assert "; secure" in cookie and "httponly" in cookie and "samesite=lax" in cookie
        assert c.get("/", follow_redirects=False).status_code == 200


def test_cookie_is_not_secure_over_plain_http(client):
    r = client.post("/setup", data=SETUP, follow_redirects=False)
    assert "secure" not in _session_cookie(r).lower()


# --- login limit ------------------------------------------------------------------------


def test_limiter_locks_after_five_and_unlocks_after_window():
    now = [1000.0]
    lim = LoginLimiter(clock=lambda: now[0])
    for _ in range(4):
        assert lim.failed("1.2.3.4") is False
        now[0] += 10
    assert lim.locked_for("1.2.3.4") == 0
    assert lim.failed("1.2.3.4") is True
    assert lim.locked_for("1.2.3.4") > 14 * 60
    assert lim.locked_for("5.6.7.8") == 0  # other addresses unaffected
    now[0] += 15 * 60
    assert lim.locked_for("1.2.3.4") == 0
    lim.failed("1.2.3.4")
    lim.succeeded("1.2.3.4")
    assert lim.locked_for("1.2.3.4") == 0


def test_login_locks_out_after_wrong_passwords(client, app):
    client.post("/setup", data=SETUP)
    client.post("/logout")
    for _ in range(5):
        r = client.post("/login", data={"username": "admin", "password": "wrong"})
        assert r.status_code == 401
    # Even the right password waits now, and the page says why.
    r = client.post("/login", data={"username": "admin", "password": "correct horse"}, follow_redirects=False)
    assert r.status_code == 429
    assert "Too many wrong logins" in r.text
    with app.state.session_factory() as db:
        events = db.scalars(select(Event).where(Event.kind == "auth", Event.level == "warning")).all()
        assert len(events) == 1 and "locked" in events[0].message


def test_lockout_lasts_the_full_window_from_the_locking_failure():
    # Gemini review: failures spread over the window used to unlock when the first one aged out.
    now = [0.0]
    lim = LoginLimiter(clock=lambda: now[0])
    for t in (0, 180, 360, 540, 840):
        now[0] = t
        locked = lim.failed("1.2.3.4")
    assert locked
    now[0] = 840 + 61  # the failure at 0 is out of the window by now
    assert lim.locked_for("1.2.3.4") > 13 * 60
    now[0] = 840 + 15 * 60 - 1
    assert lim.locked_for("1.2.3.4") > 0
    now[0] = 840 + 15 * 60
    assert lim.locked_for("1.2.3.4") == 0
    assert lim.failed("1.2.3.4") is False  # a fresh count after the lockout


def test_right_password_resets_the_count(client):
    client.post("/setup", data=SETUP)
    client.post("/logout")
    for _ in range(4):
        client.post("/login", data={"username": "admin", "password": "wrong"})
    client.post("/login", data={"username": "admin", "password": "correct horse"})
    client.post("/logout")
    for _ in range(4):
        assert client.post("/login", data={"username": "admin", "password": "wrong"}).status_code == 401


# --- untrusted proxy -------------------------------------------------------------------


def test_untrusted_proxy_is_reported_on_the_dashboard(logged_in):
    # The TestClient is no trusted proxy: uvicorn would ignore its forwarded headers.
    assert "FUMORA_TRUSTED_PROXIES" not in logged_in.get("/").text
    logged_in.get("/", headers={"X-Forwarded-Proto": "https", "X-Forwarded-For": "203.0.113.9"})
    page = logged_in.get("/").text
    assert "FUMORA_TRUSTED_PROXIES" in page and "does not trust" in page


def _behind_proxy(app, trusted: str, peer: str) -> httpx.AsyncClient:
    """The app wrapped in uvicorn's own proxy-header middleware, as `--forwarded-allow-ips` runs it."""
    transport = httpx.ASGITransport(
        app=ProxyHeadersMiddleware(app, trusted_hosts=trusted), client=(peer, 5000)
    )
    return httpx.AsyncClient(transport=transport, base_url="http://comics.example.com")


FORWARDED = {"X-Forwarded-Proto": "https", "X-Forwarded-For": "203.0.113.9"}


@pytest.mark.parametrize("trusted", ["192.168.9.5", "192.168.9.0/24"])
async def test_trusted_proxy_gives_https_and_the_visitor_address(app, trusted):
    async with _behind_proxy(app, trusted, "192.168.9.5") as c:
        r = await c.post("/setup", data=SETUP, headers=FORWARDED)
        assert "; secure" in _session_cookie(r).lower()
        r = await c.post(
            "/logout", headers=FORWARDED | {"Origin": "https://comics.example.com"}
        )  # https origin matches once the proxy is believed
        assert r.status_code == 303
        for _ in range(5):
            await c.post("/login", data={"username": "admin", "password": "wrong"}, headers=FORWARDED)
    assert app.state.login_limiter.locked_for("203.0.113.9") > 0
    assert app.state.login_limiter.locked_for("192.168.9.5") == 0
    assert app.state.proxy_check.problem is None


async def test_untrusted_proxy_headers_are_ignored_and_reported(app):
    async with _behind_proxy(app, "127.0.0.1", "192.168.9.5") as c:
        r = await c.post("/setup", data=SETUP, headers=FORWARDED)
        assert "secure" not in _session_cookie(r).lower()
    assert "If 192.168.9.5 is your reverse proxy" in app.state.proxy_check.problem


def test_forwarded_proto_matching_the_scheme_is_fine(app):
    with TestClient(app, base_url="https://comics.example.com") as c:
        c.post("/setup", data=SETUP)
        page = c.get("/", headers={"X-Forwarded-Proto": "https"}).text
        assert "FUMORA_TRUSTED_PROXIES" not in page
