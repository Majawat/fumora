import httpx
import pytest
import respx

from fumora.metron import (
    BASE_URL,
    MetronAuthError,
    MetronClient,
    MetronDailyLimitReached,
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
    return RateLimiter(min_interval=min_interval, clock=t.clock, sleep=t.sleep)


async def test_limiter_spaces_requests():
    t = FakeTime()
    limiter = make_limiter(t)
    await limiter.acquire()
    await limiter.acquire()
    assert t.slept == [pytest.approx(1.0)]


async def test_limiter_waits_for_burst_reset():
    t = FakeTime()
    limiter = make_limiter(t, min_interval=0)
    limiter.update(httpx.Headers({"x-ratelimit-burst-remaining": "0", "x-ratelimit-burst-reset": "1030"}))
    await limiter.acquire()
    assert t.slept == [pytest.approx(30.0)]


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
async def test_429_is_retried_after_retry_after():
    t = FakeTime()
    route = respx.get(f"{BASE_URL}series/5/")
    route.side_effect = [
        httpx.Response(429, headers={"retry-after": "7"}),
        httpx.Response(200, json={"id": 5}),
    ]
    async with MetronClient("u", "p", make_limiter(t, 0)) as client:
        assert (await client.series(5))["id"] == 5
    assert 7.0 in t.slept


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
