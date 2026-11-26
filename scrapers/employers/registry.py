"""Maps a community to the scraper instances that ingest its employers."""

from __future__ import annotations

from config.communities import Community, EmployerSource, SourceType
from scrapers.base import HttpClient
from scrapers.employers.common import EmployerScraper
from scrapers.employers.csv_seed import CsvSeedScraper, seed_path
from scrapers.employers.custom import CUSTOM_SCRAPERS
from scrapers.employers.html_table import HtmlTableScraper
from scrapers.employers.json_api import JsonApiScraper
from scrapers.employers.pdf_list import PdfListScraper

SCRAPER_TYPES: dict[SourceType, type[EmployerScraper]] = {
    SourceType.HTML: HtmlTableScraper,
    SourceType.PDF: PdfListScraper,
    SourceType.CSV: CsvSeedScraper,
    SourceType.JSON: JsonApiScraper,
}


def scraper_class(community: Community, source: EmployerSource) -> type[EmployerScraper]:
    if source.type is SourceType.CUSTOM:
        try:
            return CUSTOM_SCRAPERS[community.slug]
        except KeyError as exc:
            raise KeyError(f"No custom scraper registered for {community.slug!r}") from exc
    return SCRAPER_TYPES[source.type]


def build_scrapers(community: Community, http: HttpClient) -> list[EmployerScraper]:
    """Configured sources, plus data/seeds/<slug>.csv whenever it exists."""
    sources = list(community.employer_sources)
    has_default_seed = any(s.type is SourceType.CSV and not s.url for s in sources)
    if not has_default_seed and seed_path(community.slug).exists():
        sources.append(EmployerSource(SourceType.CSV))
    return [scraper_class(community, s)(community, s, http) for s in sources]
