import asyncio

import httpx
import pytest

from scrapers.base import HttpClient, HttpConfig, RobotsDisallowed, ScrapeError

FAST = HttpConfig(min_interval=0, max_attempts=3, backoff_max=0.01)


def run(coro):
    return asyncio.run(coro)


def _client(handler, config=FAST):
    return HttpClient(config, transport=httpx.MockTransport(handler))


def test_retries_then_succeeds():
    calls = {"n": 0}

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] < 3 else httpx.Response(200, text="ok")

    async def go():
        async with _client(handler) as http:
            return await http.get_text("https://x.ca/page")

    assert run(go()) == "ok" and calls["n"] == 3


def test_gives_up_after_max_attempts():
    def handler(req):
        return httpx.Response(404) if req.url.path == "/robots.txt" else httpx.Response(500)

    async def go():
        async with _client(handler) as http:
            await http.get("https://x.ca/page")

    with pytest.raises(ScrapeError, match="500"):
        run(go())


def test_non_retryable_4xx():
    def handler(req):
        return httpx.Response(404)

    async def go():
        async with _client(handler) as http:
            await http.get("https://x.ca/missing")

    with pytest.raises(ScrapeError, match="404"):
        run(go())


def test_robots_disallow():
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
        return httpx.Response(200, text="ok")

    async def go():
        async with _client(handler) as http:
            assert await http.get_text("https://x.ca/public") == "ok"
            await http.get("https://x.ca/private/list")

    with pytest.raises(RobotsDisallowed):
        run(go())


def test_robots_longest_match_allow():
    """RFC 9309: the more specific Allow beats a shorter Disallow (WordPress pattern)."""
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /wp-admin/\n"
                                            "Allow: /wp-admin/admin-ajax.php\n")
        return httpx.Response(200, text="[]")

    async def go():
        async with _client(handler) as http:
            assert await http.allowed("https://x.ca/wp-admin/admin-ajax.php?action=load")
            assert not await http.allowed("https://x.ca/wp-admin/options.php")

    run(go())


def test_rate_limit_spacing():
    import time
    stamps = []

    def handler(req):
        if req.url.path != "/robots.txt":
            stamps.append(time.monotonic())
        return httpx.Response(200, text="ok")

    async def go():
        async with _client(handler, HttpConfig(min_interval=0.2, respect_robots=False)) as http:
            await asyncio.gather(*(http.get("https://x.ca/p") for _ in range(3)))

    run(go())
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert all(g >= 0.18 for g in gaps)
