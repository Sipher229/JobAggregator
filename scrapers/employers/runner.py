"""Orchestrates employer ingestion: scrape every source of each community
concurrently, then persist per (community, program) with a ScrapeRun audit row."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, replace
from pathlib import Path

from sqlalchemy.orm import Session

from config.communities import COMMUNITIES, Community, Program, SourceType, get_community
from core.employer_sync import (
    EmployerIndex,
    SyncStats,
    finish_run,
    record_run,
    sync_designations,
)
from core.schemas import EmployerIn
from scrapers.base import HttpClient, HttpConfig
from scrapers.employers.registry import build_scrapers, scraper_class

logger = logging.getLogger(__name__)
TASK_NAME = "sync_employers"


@dataclass
class CommunityResult:
    community: Community
    records: list[EmployerIn]
    errors: dict[str, str]            # scraper target -> message
    failed_programs: set[Program]
    configured: bool


async def scrape_community(community: Community, http: HttpClient) -> CommunityResult:
    scrapers = build_scrapers(community, http)
    if not scrapers:
        return CommunityResult(community, [], {}, set(), configured=False)

    results = await asyncio.gather(*(s.scrape() for s in scrapers), return_exceptions=True)
    records: list[EmployerIn] = []
    errors: dict[str, str] = {}
    failed: set[Program] = set()
    for scraper, result in zip(scrapers, results):
        if isinstance(result, BaseException):
            errors[scraper.target] = f"{type(result).__name__}: {result}"
            failed.update(scraper.programs)
            logger.error("%s failed: %s", scraper.target, result)
        else:
            records.extend(result)
    return CommunityResult(community, records, errors, failed, configured=True)


class LocalFileHttp:
    """Serves one saved file for every request (manual import of a blocked page)."""

    def __init__(self, path: Path):
        self.path = path

    async def get_text(self, url: str, **kwargs) -> str:
        return self.path.read_text(encoding="utf-8", errors="replace")

    async def get_bytes(self, url: str, **kwargs) -> bytes:
        return self.path.read_bytes()


async def scrape_community_from_file(community: Community, path: Path) -> CommunityResult:
    """Run the community's configured (non-CSV) parsers against a locally saved copy."""
    http = LocalFileHttp(path)
    want = SourceType.PDF if path.suffix.lower() == ".pdf" else None
    scrapers = []
    for source in community.employer_sources:
        if source.type is SourceType.CSV or (want and source.type is not want):
            continue
        opts = {k: v for k, v in source.options.items() if k not in ("link_text", "link_href")}
        local = replace(source, options=opts, requires_js=False)
        scraper = scraper_class(community, local)(community, local, http)
        scraper.resolved_url = source.url  # keep provenance pointing at the official page
        scrapers.append(scraper)
    if not scrapers:
        raise ValueError(f"{community.slug}: no configured parser for {path.name}")
    results = await asyncio.gather(*(s.scrape() for s in scrapers))
    records = [r for batch in results for r in batch]
    return CommunityResult(community, records, {}, set(), configured=True)


async def scrape_all(communities: list[Community], http_config: HttpConfig | None = None,
                     concurrency: int = 4) -> list[CommunityResult]:
    sem = asyncio.Semaphore(concurrency)
    async with HttpClient(http_config) as http:
        async def bounded(c: Community) -> CommunityResult:
            async with sem:
                return await scrape_community(c, http)
        return await asyncio.gather(*(bounded(c) for c in communities))


def persist(session: Session, results: list[CommunityResult],
            programs: set[Program] | None = None) -> dict[str, SyncStats]:
    index = EmployerIndex(session)
    summary: dict[str, SyncStats] = {}
    for res in results:
        by_program: dict[Program, list[EmployerIn]] = defaultdict(list)
        for rec in res.records:
            by_program[rec.program].append(rec)

        for program in res.community.programs:
            if programs and program not in programs:
                continue
            target = f"{res.community.slug}:{program}"
            run = record_run(session, TASK_NAME, target)
            if not res.configured:
                stats = SyncStats(warnings=["no employer source configured"])
                finish_run(run, stats)
                summary[target] = stats
                continue
            error = "; ".join(res.errors.values()) or None
            stats = sync_designations(
                session, res.community.slug, program, by_program.get(program, []),
                complete=program not in res.failed_programs, index=index,
            )
            finish_run(run, stats, error=error if program in res.failed_programs else None)
            summary[target] = stats
        session.commit()
    return summary


def run_employer_sync(session: Session, slugs: list[str] | None = None,
                      program: Program | None = None,
                      http_config: HttpConfig | None = None,
                      from_file: Path | None = None) -> dict[str, SyncStats]:
    if from_file is not None:
        if not slugs or len(slugs) != 1:
            raise ValueError("--from-file requires exactly one --community")
        result = asyncio.run(scrape_community_from_file(get_community(slugs[0]), from_file))
        return persist(session, [result], {program} if program else None)

    communities = [get_community(s) for s in slugs] if slugs else list(COMMUNITIES)
    if program:
        communities = [c for c in communities if program in c.programs]
    results = asyncio.run(scrape_all(communities, http_config))
    return persist(session, results, {program} if program else None)
