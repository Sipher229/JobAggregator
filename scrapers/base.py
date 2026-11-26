"""Shared scraping infrastructure.

* HttpClient: async httpx wrapper with per-host rate limiting, retries with
  exponential backoff (429/5xx/transport errors, honours Retry-After) and
  robots.txt enforcement.
* BaseScraper: abstract base every scraper derives from. Scrapers only fetch
  and parse; they return Pydantic records and never touch the database, which
  keeps them unit-testable against saved fixtures.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Generic, TypeVar
from urllib.parse import urlsplit
from protego import Protego

import httpx
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)

# Generic, contact-free identifier. Override with SCRAPER_USER_AGENT if needed.
DEFAULT_USER_AGENT = "ImmigrationJobsAggregator/0.1 (+deterministic research crawler)"
RETRY_STATUS = {429, 500, 502, 503, 504}


def _ssl_context() -> ssl.SSLContext | bool:
    """Use the OS trust store when available: it completes missing intermediate
    certificates (AIA fetching on Windows), as browsers and curl do. Some
    municipal sites serve incomplete chains that certifi-only verification rejects."""
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except ImportError:  # pragma: no cover
        return True


class ScrapeError(Exception):
    """Raised when a page cannot be fetched or parsed."""


class RobotsDisallowed(ScrapeError):
    pass


@dataclass
class HttpConfig:
    user_agent: str = DEFAULT_USER_AGENT
    timeout: float = 30.0
    min_interval: float = 2.0       # seconds between requests to the same host
    max_attempts: int = 4
    backoff_max: float = 60.0
    respect_robots: bool = True


class _RetryableStatus(Exception):
    def __init__(self, response: httpx.Response):
        super().__init__(f"HTTP {response.status_code} for {response.url}")
        self.response = response


def _is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, (_RetryableStatus, httpx.TransportError))


class HttpClient:
    """Polite async HTTP client. Use as `async with HttpClient() as http:`."""

    def __init__(self, config: HttpConfig | None = None, *,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.config = config or HttpConfig()
        self._client = httpx.AsyncClient(
            headers={"User-Agent": self.config.user_agent,
                     "Accept-Language": "en-CA,en;q=0.9,fr-CA;q=0.8"},
            timeout=self.config.timeout,
            follow_redirects=True,
            verify=_ssl_context(),
            transport=transport,
        )
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._last_request: dict[str, float] = {}
        self._robots: dict[str, Protego | None] = {}
        self._text_cache: dict[str, str] = {}  # same page used by several sources

    async def __aenter__(self) -> HttpClient:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- politeness -----------------------------------------------------------
    async def _throttle(self, host: str) -> None:
        lock = self._host_locks.setdefault(host, asyncio.Lock())
        async with lock:
            elapsed = time.monotonic() - self._last_request.get(host, 0.0)
            if elapsed < self.config.min_interval:
                await asyncio.sleep(self.config.min_interval - elapsed)
            self._last_request[host] = time.monotonic()

    async def _robots_for(self, url: str) -> Protego | None:
        """RFC 9309 robots.txt (longest-match Allow/Disallow, wildcards).

        urllib.robotparser applies first-match semantics, which wrongly blocks the
        common WordPress pattern `Disallow: /wp-admin/` + `Allow: /wp-admin/admin-ajax.php`.
        """
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            parser: Protego | None
            try:
                await self._throttle(parts.netloc)
                resp = await self._client.get(f"{origin}/robots.txt")
                if resp.status_code == 200:
                    parser = Protego.parse(resp.text)
                elif resp.status_code in (401, 403):
                    parser = Protego.parse("User-agent: *\nDisallow: /")
                else:
                    parser = None  # 404 etc. -> everything allowed
            except httpx.HTTPError:
                parser = None
            self._robots[origin] = parser
        return self._robots[origin]

    async def allowed(self, url: str) -> bool:
        if not self.config.respect_robots:
            return True
        robots = await self._robots_for(url)
        return robots is None or robots.can_fetch(url, self.config.user_agent)  # protego: (url, ua)

    # -- requests -------------------------------------------------------------
    def _wait(self, state: RetryCallState) -> float:
        exc = state.outcome.exception() if state.outcome else None
        if isinstance(exc, _RetryableStatus):
            retry_after = exc.response.headers.get("Retry-After", "")
            if retry_after.isdigit():
                return min(float(retry_after), self.config.backoff_max)
        return wait_exponential(multiplier=1, max=self.config.backoff_max)(state)

    async def get(self, url: str, **kwargs) -> httpx.Response:
        if not await self.allowed(url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")
        host = urlsplit(url).netloc

        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self.config.max_attempts),
                wait=self._wait,
                retry=retry_if_exception(_is_retryable),
                reraise=True,
            ):
                with attempt:
                    await self._throttle(host)
                    resp = await self._client.get(url, **kwargs)
                    if resp.status_code in RETRY_STATUS:
                        logger.warning("Retryable %s from %s (attempt %d)",
                                       resp.status_code, url, attempt.retry_state.attempt_number)
                        raise _RetryableStatus(resp)
        except _RetryableStatus as exc:
            raise ScrapeError(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise ScrapeError(f"{type(exc).__name__} fetching {url}: {exc}") from exc

        if resp.status_code >= 400:
            raise ScrapeError(f"HTTP {resp.status_code} for {url}")
        return resp

    async def get_text(self, url: str, **kwargs) -> str:
        if kwargs:
            return (await self.get(url, **kwargs)).text
        if url not in self._text_cache:
            self._text_cache[url] = (await self.get(url)).text
        return self._text_cache[url]

    async def get_bytes(self, url: str, **kwargs) -> bytes:
        return (await self.get(url, **kwargs)).content


async def render_with_browser(url: str, *, wait_selector: str | None = None,
                              timeout_ms: int = 30_000) -> str:
    """Playwright fallback for JS-rendered pages (optional dependency)."""
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover - optional
        raise ScrapeError(
            "Page requires JavaScript; install with `pip install playwright && "
            "playwright install chromium`"
        ) from exc

    async with async_playwright() as pw:  # pragma: no cover - network
        browser = await pw.chromium.launch()
        try:
            page = await browser.new_page(user_agent=DEFAULT_USER_AGENT)
            await page.goto(url, timeout=timeout_ms, wait_until="networkidle")
            if wait_selector:
                await page.wait_for_selector(wait_selector, timeout=timeout_ms)
            return await page.content()
        finally:
            await browser.close()


T = TypeVar("T")


class BaseScraper(ABC, Generic[T]):
    """Fetch + parse. Subclasses implement `scrape()`; parsing helpers should be
    pure functions of the downloaded content so they can be tested offline."""

    name: str = "base"

    def __init__(self, http: HttpClient):
        self.http = http
        self.log = logging.getLogger(f"scrapers.{self.name}")

    @property
    def target(self) -> str:
        return self.name

    @abstractmethod
    async def scrape(self) -> list[T]:
        ...
