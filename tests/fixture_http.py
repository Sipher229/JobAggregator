"""Offline stand-in for HttpClient that serves saved pages from tests/fixtures/employers.

For a community slug:
  <slug>-page.html  landing page (link discovery), served by get_text when present
  <slug>.html / <slug>.json  the list itself
  <slug>.pdf        served by get_bytes
"""

from __future__ import annotations

import asyncio
from collections import Counter
from pathlib import Path

from config.communities import COMMUNITIES_BY_SLUG

FIXTURES = Path(__file__).parent / "fixtures" / "employers"


class FixtureHttp:
    def __init__(self, slug: str):
        self.slug = slug
        self.requested: list[str] = []

    def _path(self, *names: str) -> Path:
        for name in names:
            path = FIXTURES / name
            if path.exists():
                return path
        raise FileNotFoundError(f"no fixture among {names}")

    async def get_text(self, url: str, **kwargs) -> str:
        self.requested.append(url)
        if url.lower().endswith(".pdf") or "fileName=" in url:
            raise AssertionError("PDF requested as text")
        page = FIXTURES / f"{self.slug}-page.html"
        if page.exists() and not self.requested[:-1]:
            return page.read_text(encoding="utf-8")
        return self._path(f"{self.slug}.html", f"{self.slug}.json").read_text(encoding="utf-8")

    async def get_bytes(self, url: str, **kwargs) -> bytes:
        self.requested.append(url)
        return self._path(f"{self.slug}.pdf").read_bytes()


def has_fixture(slug: str) -> bool:
    return any((FIXTURES / f"{slug}{ext}").exists() for ext in (".html", ".json", ".pdf"))


def scrape_fixture(slug: str):
    """Run every configured (non-CSV) source of a community against its fixtures."""
    from scrapers.employers.registry import scraper_class

    community = COMMUNITIES_BY_SLUG[slug]
    records = []
    for source in community.employer_sources:
        http = FixtureHttp(slug)
        scraper = scraper_class(community, source)(community, source, http)
        records.extend(asyncio.run(scraper.scrape()))
    return records


if __name__ == "__main__":  # python -m tests.fixture_http  -> parse report
    import sys

    slugs = sys.argv[1:] or [s for s in COMMUNITIES_BY_SLUG if has_fixture(s)]
    for slug in slugs:
        try:
            recs = scrape_fixture(slug)
        except FileNotFoundError as exc:
            print(f"== {slug}: missing fixture ({exc})")
            continue
        by_prog = Counter(r.program.value for r in recs)
        nh = sum(r.not_hiring for r in recs)
        sectors = Counter(r.priority_sector for r in recs)
        print(f"== {slug}: {dict(by_prog)} not_hiring={nh} sectors={dict(sectors.most_common(4))}")
        for r in recs[:4] + recs[-3:]:
            print(f"   {r.program.value} {'NH' if r.not_hiring else '  '} {r.name!r}"
                  f" | {r.location or ''} | {r.website_url or ''} {r.flags or ''}")
