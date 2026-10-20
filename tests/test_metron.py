import httpx
import pytest
import respx

from fumora.metron import (
    BASE_URL,
    MAX_RATE_LIMITED,
    MetronAuthError,
    MetronClient,
    MetronDailyLimitReached,
    MetronPause,
    RateLimiter,
)


class FakeTime:
    def __init__(self, now=1000.0):
        self.now = now
        self.slept = []

    def clock(self):
        return self.now

    async def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def make_limiter(t, min_interval=1.0):
    return RateLimiter(min_interval=min_interval, clock=t.clock, sleep=t.sleep, jitter=0)


async def test_limiter_spaces_requests_evenly_at_20_a_minute():
    t = FakeTime()
    limiter = RateLimiter(clock=t.clock, sleep=t.sleep, jitter=0)  # defaults: 20 a minute
    for _ in range(4):
        await limiter.acquire()
    assert t.slept == [pytest.approx(3.0)] * 3
    limiter.per_minute = 10  # e.g. lowered in Settings
    await limiter.acquire()
    assert t.slept[-1] == pytest.approx(6.0)


async def test_limiter_waits_for_burst_reset():
    t = FakeTime()
    limiter = make_limiter(t, min_interval=0)
    limiter.update(httpx.Headers({"x-ratelimit-burst-remaining": "0", "x-ratelimit-burst-reset": "1030"}))
    await limiter.acquire()
    assert t.slept == [pytest.approx(31.0)]  # until the reset, plus a second


async def test_limiter_refuses_when_daily_budget_spent():
    t = FakeTime()
    limiter = make_limiter(t)
    limiter.update(
        httpx.Headers({"x-ratelimit-sustained-remaining": "0", "x-ratelimit-sustained-reset": "90000"})
    )
    with pytest.raises(MetronDailyLimitReached):
        await limiter.acquire()


async def test_limiter_allows_after_daily_reset():
    t = FakeTime(now=95000)
    limiter = make_limiter(t)
    limiter.update(
        httpx.Headers({"x-ratelimit-sustained-remaining": "0", "x-ratelimit-sustained-reset": "90000"})
    )
    await limiter.acquire()


def page(results, next_url=None):
    return {"count": len(results), "next": next_url, "previous": None, "results": results}


@respx.mock
async def test_paging_follows_next():
    t = FakeTime()
    route = respx.get(f"{BASE_URL}issue/")
    route.side_effect = [
        httpx.Response(200, json=page([{"id": 1}], next_url="x")),
        httpx.Response(200, json=page([{"id": 2}])),
    ]
    async with MetronClient("u", "p", make_limiter(t, 0)) as client:
        ids = [i["id"] async for i in client.series_issues(17753)]
    assert ids == [1, 2]
    assert route.calls[0].request.url.params["series_id"] == "17753"
    assert route.calls[1].request.url.params["page"] == "2"


@respx.mock
async def test_series_by_cv_id():
    t = FakeTime()
    respx.get(f"{BASE_URL}series/", params={"cv_id": "162288"}).respond(
        200, json=page([{"id": 17753, "series": "Private Dance (2023)"}])
    )
    async with MetronClient("u", "p", make_limiter(t, 0)) as client:
        s = await client.series_by_cv_id(162288)
    assert s["id"] == 17753


@respx.mock
async def test_bad_login_raises_auth_error():
    t = FakeTime()
    respx.get(f"{BASE_URL}series_type/").respond(401)
    async with MetronClient("u", "bad", make_limiter(t, 0)) as client:
        with pytest.raises(MetronAuthError):
            await client.check_login()


@respx.mock
async def test_429_holds_every_request_for_retry_after():
    t = FakeTime()
    limiter = make_limiter(t, 0)
    route = respx.get(f"{BASE_URL}series/5/")
    route.side_effect = [
        httpx.Response(429, headers={"retry-after": "7"}),
        httpx.Response(200, json={"id": 5}),
    ]
    respx.get(f"{BASE_URL}series/6/").respond(200, json={"id": 6})
    async with MetronClient("u", "p", limiter) as client:
        assert (await client.series(5))["id"] == 5
        assert t.slept == [pytest.approx(7.0)]
        assert limiter.rejected == 1 and limiter.last_rejected is not None
        limiter.on_rate_limited(30)  # another request was told to wait 30s...
        await client.series(6)  # ...so this one waits too, instead of firing right away
    assert t.slept[-1] == pytest.approx(30.0)


@respx.mock
async def test_429_without_retry_after_waits_a_minute():
    t = FakeTime()
    respx.get(f"{BASE_URL}series/5/").side_effect = [httpx.Response(429), httpx.Response(200, json={"id": 5})]
    async with MetronClient("u", "p", make_limiter(t, 0)) as client:
        await client.series(5)
    assert t.slept == [pytest.approx(60.0)]


@respx.mock
async def test_endless_429s_pause_the_job_instead_of_moving_on():
    t = FakeTime()
    route = respx.get(f"{BASE_URL}series/5/").respond(429, headers={"retry-after": "2"})
    async with MetronClient("u", "p", make_limiter(t, 0)) as client:
        with pytest.raises(MetronPause):
            await client.series(5)
    assert route.call_count == MAX_RATE_LIMITED
    assert all(s == pytest.approx(2.0) for s in t.slept)


async def test_limiter_keeps_headroom_for_other_clients():
    t = FakeTime()
    limiter = make_limiter(t, 0)
    limiter.update(httpx.Headers({"x-ratelimit-burst-remaining": "1", "x-ratelimit-burst-reset": "1040"}))
    await limiter.acquire()
    assert t.slept == [pytest.approx(41.0)]


@respx.mock
async def test_headers_update_limiter():
    t = FakeTime()
    limiter = make_limiter(t, 0)
    respx.get(f"{BASE_URL}series/5/").respond(
        200, json={"id": 5}, headers={"x-ratelimit-sustained-remaining": "4992"}
    )
    async with MetronClient("u", "p", limiter) as client:
        await client.series(5)
    assert limiter.sustained_remaining == 4992


@respx.mock
async def test_api_token_is_sent_as_bearer_instead_of_basic():
    t = FakeTime()
    route = respx.get(f"{BASE_URL}series/1/").respond(200, json={"id": 1})
    async with MetronClient("u", "p", make_limiter(t, 0), token="tok123") as client:
        await client._get("series/1/")
    auth = route.calls[0].request.headers["authorization"]
    assert auth == "Bearer tok123"


def test_settings_prefer_the_token(app):
    from fumora import settings_store

    with app.state.session_factory() as db:
        assert not settings_store.metron_ready(db)
        settings_store.put(db, "metron.api_token", "tok")
        assert settings_store.metron_ready(db)
        assert settings_store.metron_login(db) == (None, None, "tok")


def test_waits_after_errors_are_jittered():
    t = FakeTime()
    waits = set()
    for _ in range(20):
        limiter = RateLimiter(clock=t.clock, sleep=t.sleep)
        limiter.on_rate_limited(100)
        wait = limiter.blocked_until - t.now
        assert 100 <= wait <= 120
        waits.add(round(wait, 3))
    assert len(waits) > 1
