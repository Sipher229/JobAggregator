"""Maps a community to the scraper instances that ingest its employers."""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path

from config.communities import Community, EmployerSource, SourceType
from core.database import PROJECT_ROOT
from scrapers.base import HttpClient
from scrapers.employers.common import EmployerScraper
from scrapers.employers.csv_seed import CsvSeedScraper, seed_path
from scrapers.employers.custom import CUSTOM_SCRAPERS
from scrapers.employers.html_table import HtmlTableScraper
from scrapers.employers.json_api import JsonApiScraper
from scrapers.employers.pdf_list import PdfListScraper

logger = logging.getLogger(__name__)

MANUAL_DIR = PROJECT_ROOT / "data" / "manual"
_EXT = {SourceType.HTML: ".html", SourceType.PDF: ".pdf", SourceType.JSON: ".json"}

SCRAPER_TYPES: dict[SourceType, type[EmployerScraper]] = {
    SourceType.HTML: HtmlTableScraper,
    SourceType.PDF: PdfListScraper,
    SourceType.CSV: CsvSeedScraper,
    SourceType.JSON: JsonApiScraper,
}


class LocalFileHttp:
    """Serves one saved file for every request (manual import of a blocked page)."""

    def __init__(self, path: Path):
        self.path = path

    async def get_text(self, url: str, **kwargs) -> str:
        return self.path.read_text(encoding="utf-8", errors="replace")

    async def get_bytes(self, url: str, **kwargs) -> bytes:
        return self.path.read_bytes()


class MissingManualCopy(Exception):
    pass


class _MissingManualScraper(EmployerScraper):
    """Placeholder that fails (-> no revocation) when a manual copy is absent."""

    name = "manual"

    async def fetch_raw(self):
        raise MissingManualCopy(
            f"{self.community.slug}: manual source but no saved copy at "
            f"{manual_path(self.community.slug, self.source.type)}; save the page there "
            f"or run `sync-employers -c {self.community.slug} --from-file <file>`")


def manual_path(slug: str, source_type: SourceType) -> Path:
    return MANUAL_DIR / f"{slug}{_EXT.get(source_type, '.html')}"


def scraper_class(community: Community, source: EmployerSource) -> type[EmployerScraper]:
    if source.type is SourceType.CUSTOM:
        try:
            return CUSTOM_SCRAPERS[community.slug]
        except KeyError as exc:
            raise KeyError(f"No custom scraper registered for {community.slug!r}") from exc
    return SCRAPER_TYPES[source.type]


def local_scraper(community: Community, source: EmployerSource, path: Path) -> EmployerScraper:
    """The configured parser for `source`, reading `path` instead of the network."""
    opts = {k: v for k, v in source.options.items() if k not in ("link_text", "link_href")}
    local = replace(source, options=opts, requires_js=False)
    scraper = scraper_class(community, local)(community, local, LocalFileHttp(path))
    scraper.resolved_url = source.url  # provenance stays the official page
    return scraper


def build_scrapers(community: Community, http: HttpClient) -> list[EmployerScraper]:
    """Configured sources (manual ones read data/manual/), plus data/seeds/<slug>.csv."""
    sources = list(community.employer_sources)
    has_default_seed = any(s.type is SourceType.CSV and not s.url for s in sources)
    if not has_default_seed and seed_path(community.slug).exists():
        sources.append(EmployerSource(SourceType.CSV))

    scrapers: list[EmployerScraper] = []
    for s in sources:
        if s.manual:
            path = manual_path(community.slug, s.type)
            if path.exists():
                logger.info("%s: using manual copy %s", community.slug, path)
                scrapers.append(local_scraper(community, s, path))
            else:
                scrapers.append(_MissingManualScraper(community, s, http))
        else:
            scrapers.append(scraper_class(community, s)(community, s, http))
    return scrapers
