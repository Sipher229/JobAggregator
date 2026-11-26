"""Orchestrates job aggregation (python cli.py sync-jobs).

1. Job Bank: every community's search centres (sequential: Job Bank paging is
   session-based and robots.txt asks for a 5 s crawl delay). Postings that match
   (or nearly match) a designated employer get their detail page fetched once
   for NOC/TEER/language/terms, within a per-run budget.
2. Career sites: designated employers with a website and no active matched Job
   Bank posting, scraped concurrently across hosts (per-host throttling still
   applies).
3. External adapters registered in scrapers.jobs.external (none by default).
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from config.communities import COMMUNITIES, Community, get_community
from core.job_sync import (
    CandidateIndex,
    JobSyncStats,
    active_job_counts,
    close_expired,
    deactivate_unseen,
    load_candidates,
    mark_expired,
    needs_detail,
    upsert_jobs,
)
from core.models import (
    Community as CommunityRow,
    CommunityProgram,
    DesignationStatus,
    Employer,
    EmployerCommunity,
    JobPosting,
    JobSource,
    MatchStatus,
    RunStatus,
    ScrapeRun,
    utcnow,
)
from core.schemas import JobPostingIn
from scrapers.base import HttpClient, HttpConfig, ScrapeError
from core.employer_sync import clean_employer_websites
from scrapers.jobs.career_site import PERMANENT_ERRORS, CareerSiteScraper, EmployerTarget
from scrapers.jobs.external import EXTERNAL_SOURCES
from scrapers.jobs.job_bank import JobBankScraper

logger = logging.getLogger(__name__)

ALL_SOURCES = ("job_bank", "career_site", "external")


@dataclass
class JobSyncOptions:
    sources: tuple[str, ...] = ("job_bank", "career_site")
    fetch_details: bool = True
    max_details: int = 300
    career_limit: int | None = None
    career_concurrency: int = 8


def _start(session: Session, task: str, target: str) -> ScrapeRun:
    run = ScrapeRun(task_name=task, target=target, status=RunStatus.RUNNING)
    session.add(run)
    session.flush()
    return run


def _finish(run: ScrapeRun, stats: JobSyncStats, error: str | None = None) -> None:
    run.finished_at = utcnow()
    run.records_found = stats.found
    run.records_created = stats.created
    run.records_updated = stats.updated
    notes = [*stats.warnings] + ([error] if error else [])
    run.error_message = "; ".join(notes) or None
    run.status = RunStatus.FAILED if error and not stats.found else \
        RunStatus.PARTIAL if notes else RunStatus.SUCCESS


# --------------------------------------------------------------------------- #
# Job Bank
# --------------------------------------------------------------------------- #
async def sync_job_bank(session: Session, http: HttpClient, communities: list[Community],
                        opts: JobSyncOptions) -> dict[str, JobSyncStats]:
    index = CandidateIndex(load_candidates(session))
    jb = JobBankScraper(http)
    run_started = utcnow()   # one timestamp for the pass: overlapping radii stay consistent
    budget = opts.max_details if opts.fetch_details else 0
    summary: dict[str, JobSyncStats] = {}

    for community in communities:
        run = _start(session, "sync_jobs:job_bank", community.slug)
        stats = JobSyncStats()
        error = None
        listings, errors = await jb.search_community(community)
        complete = not errors
        if errors:
            error = "; ".join(errors)

        # Detail pages only for (near-)matches not fetched before.
        interesting = [j for j in listings if index.match(j).status in ("matched", "needs_review")]
        pending = needs_detail(session, JobSource.JOB_BANK, [j.external_id for j in interesting])
        enriched, expired = {}, []
        for job in interesting:
            if job.external_id not in pending:
                continue
            if budget <= 0:
                stats.warnings.append("detail budget exhausted")
                break
            budget -= 1
            try:
                detailed, gone = await jb.fetch_detail(job)
            except ScrapeError as exc:
                logger.warning("job_bank detail %s failed: %s", job.external_id, exc)
                continue
            if gone:
                expired.append(job.external_id)
            else:
                enriched[job.external_id] = detailed

        jobs = [enriched.get(j.external_id, j) for j in listings if j.external_id not in expired]
        upsert_jobs(session, jobs, index, run_started=run_started, stats=stats)
        stats.deactivated += mark_expired(session, JobSource.JOB_BANK, expired)
        if complete:
            stats.deactivated += deactivate_unseen(session, JobSource.JOB_BANK, run_started,
                                                   community_slugs=[community.slug])
        else:
            stats.warnings.append("search failed: deactivation skipped")
        _finish(run, stats, error)
        session.commit()
        summary[f"job_bank:{community.slug}"] = stats
        logger.info("job_bank %s: %d found, %d in scope, %d matched, %d review, %d details",
                    community.slug, stats.found, stats.in_scope, stats.matched,
                    stats.needs_review, len(enriched))
    return summary


# --------------------------------------------------------------------------- #
# Career sites
# --------------------------------------------------------------------------- #
def career_targets(session: Session, slugs: list[str] | None, limit: int | None) -> list[EmployerTarget]:
    counts = active_job_counts(session)
    q = (select(Employer).distinct()
         .join(EmployerCommunity, EmployerCommunity.employer_id == Employer.id)
         .join(CommunityProgram, CommunityProgram.id == EmployerCommunity.community_program_id)
         .join(CommunityRow, CommunityRow.id == CommunityProgram.community_id)
         .where(EmployerCommunity.status != DesignationStatus.REVOKED,
                (Employer.website_url.is_not(None)) | (Employer.careers_url.is_not(None)))
         .order_by(Employer.careers_checked_at.is_(None).desc(), Employer.careers_checked_at, Employer.id))
    if slugs:
        q = q.where(CommunityRow.slug.in_(slugs))
    targets = []
    for e in session.scalars(q):
        if counts.get(e.id, 0):
            continue  # already has active matched Job Bank postings
        if e.website_error and e.website_error.split(":", 1)[0] in PERMANENT_ERRORS:
            continue  # dead domain / robots refusal: wait until the URL changes
        targets.append(EmployerTarget(e.id, e.operating_name or e.canonical_name, e.website_url,
                                      e.careers_url, e.careers_page_hash, e.ats_type))
    return targets[:limit] if limit else targets


async def sync_career_sites(session: Session, http: HttpClient, slugs: list[str] | None,
                            opts: JobSyncOptions) -> dict[str, JobSyncStats]:
    cleaned = clean_employer_websites(session)
    if any(cleaned.values()):
        logger.info("career_site: website cleanup %s", cleaned)
    targets = career_targets(session, slugs, opts.career_limit)
    run = _start(session, "sync_jobs:career_site", ",".join(slugs) if slugs else "ALL")
    stats = JobSyncStats()
    run_started = utcnow()
    scraper = CareerSiteScraper(http)
    sem = asyncio.Semaphore(opts.career_concurrency)

    async def one(t: EmployerTarget):
        async with sem:
            return await scraper.scrape(t)

    results = await asyncio.gather(*(one(t) for t in targets))
    index = CandidateIndex(load_candidates(session))
    jobs, ok_ids, errors = [], [], 0
    tiers: dict[str, int] = {}
    error_kinds: dict[str, int] = {}
    for res in results:
        emp = session.get(Employer, res.employer_id)
        emp.careers_checked_at = run_started
        if res.error:
            errors += 1
            kind = res.error_kind or "network"
            error_kinds[kind] = error_kinds.get(kind, 0) + 1
            emp.website_error = f"{kind}: {res.error}"[:200]
            logger.info("career_site %s (%s): %s", emp.canonical_name, emp.website_url, emp.website_error)
            continue
        emp.website_error = None
        ok_ids.append(res.employer_id)
        tiers[res.tier] = tiers.get(res.tier, 0) + 1
        if res.careers_url and not emp.careers_url and res.careers_url != emp.website_url:
            emp.careers_url = res.careers_url
        if res.ats_type:
            emp.ats_type = res.ats_type
        if res.tier == "hash":
            if res.changed:
                emp.needs_manual_review = True
            emp.careers_page_hash = res.page_hash
            emp.careers_page_snapshot = res.snapshot
        elif res.tier == "no_careers_page" and not emp.careers_url:
            # earlier versions hashed homepages; drop those baselines and their alerts
            emp.careers_page_hash = emp.careers_page_snapshot = None
            emp.needs_manual_review = False
        jobs.extend(res.jobs)

    upsert_jobs(session, jobs, index, run_started=run_started, stats=stats)
    stats.deactivated += deactivate_unseen(session, JobSource.CAREER_SITE, run_started,
                                           employer_ids=ok_ids)
    changed = sum(1 for r in results if r.changed)
    stats.warnings.append(
        f"{len(targets)} employers: jsonld={tiers.get('jsonld', 0)} ats={tiers.get('ats', 0)} "
        f"hash={tiers.get('hash', 0)} no_careers_page={tiers.get('no_careers_page', 0)} "
        f"changed={changed} errors={errors}"
        + (f" ({', '.join(f'{k}: {v}' for k, v in sorted(error_kinds.items(), key=lambda kv: -kv[1]))})"
           if error_kinds else ""))
    _finish(run, stats)
    run.status = RunStatus.SUCCESS if errors < len(targets) or not targets else RunStatus.FAILED
    session.commit()
    return {"career_site": stats}


# --------------------------------------------------------------------------- #
# External adapters
# --------------------------------------------------------------------------- #
async def sync_external(session: Session, http: HttpClient, communities: list[Community]):
    summary = {}
    if not EXTERNAL_SOURCES:
        return summary
    index = CandidateIndex(load_candidates(session))
    for name, adapter in EXTERNAL_SOURCES.items():
        run_started = utcnow()
        stats = JobSyncStats()
        run = _start(session, f"sync_jobs:{name}", "ALL")
        for community in communities:
            jobs = await adapter.fetch(community, http)
            upsert_jobs(session, jobs, index, run_started=run_started, stats=stats)
        _finish(run, stats)
        session.commit()
        summary[name] = stats
    return summary


async def refresh_job_bank_details(session: Session, http: HttpClient, *, limit: int | None = None,
                                   only_missing_expiry: bool = True) -> JobSyncStats:
    """Re-read detail pages of stored, active matched/review Job Bank postings.

    Fills NOC/language/terms and the 'Advertised until' date without re-running
    searches, and closes postings whose page is gone (404 / 'no longer available').
    """
    q = (select(JobPosting)
         .where(JobPosting.source == JobSource.JOB_BANK, JobPosting.is_active.is_(True),
                JobPosting.match_status.in_([MatchStatus.MATCHED, MatchStatus.NEEDS_REVIEW]))
         .order_by(JobPosting.match_status, JobPosting.posted_at.desc()))
    if only_missing_expiry:
        q = q.where(JobPosting.expires_at.is_(None))
    posts = list(session.scalars(q.limit(limit) if limit else q))
    run = _start(session, "refresh_details:job_bank", f"{len(posts)} postings")
    stats = JobSyncStats(found=len(posts))
    jb = JobBankScraper(http)
    index = CandidateIndex(load_candidates(session))
    now = utcnow()
    expired: list[str] = []
    for post in posts:
        listing = JobPostingIn(source=JobSource.JOB_BANK, external_id=post.external_id, title=post.title,
                               employer_name=post.employer_name_raw or "", city=post.city,
                               province=post.province, url=post.url, posted_at=post.posted_at,
                               wage_raw=post.wage_raw, source_detail=post.source_detail,
                               job_number=post.job_number, work_mode=post.work_mode)
        try:
            detailed, gone = await jb.fetch_detail(listing)
        except ScrapeError as exc:
            logger.warning("refresh-details %s failed: %s", post.external_id, exc)
            continue
        if gone:
            expired.append(post.external_id)
            continue
        # keep the posting's original last_seen (a detail read isn't a listing sighting)
        last_seen = post.last_seen_at
        upsert_jobs(session, [detailed], index, run_started=now, stats=stats)
        post.last_seen_at = last_seen
        session.commit()
    stats.deactivated += mark_expired(session, JobSource.JOB_BANK, expired)
    stats.deactivated += close_expired(session)
    stats.found = len(posts)  # upsert_jobs also counts into `found`
    _finish(run, stats)
    session.commit()
    return stats


def run_refresh_details(session: Session, limit: int | None = None, all_postings: bool = False,
                        http_config: HttpConfig | None = None) -> JobSyncStats:
    async def main():
        async with HttpClient(http_config) as http:
            return await refresh_job_bank_details(session, http, limit=limit,
                                                  only_missing_expiry=not all_postings)
    return asyncio.run(main())


def run_job_sync(session: Session, slugs: list[str] | None = None,
                 opts: JobSyncOptions | None = None,
                 http_config: HttpConfig | None = None) -> dict[str, JobSyncStats]:
    opts = opts or JobSyncOptions()
    communities = [get_community(s) for s in slugs] if slugs else list(COMMUNITIES)

    async def main():
        summary: dict[str, JobSyncStats] = {}
        async with HttpClient(http_config) as http:
            if "job_bank" in opts.sources:
                summary |= await sync_job_bank(session, http, communities, opts)
            if "career_site" in opts.sources:
                summary |= await sync_career_sites(session, http, slugs, opts)
            if "external" in opts.sources:
                summary |= await sync_external(session, http, communities)
        closed = close_expired(session)
        session.commit()
        summary["expired"] = JobSyncStats(deactivated=closed)
        if closed:
            logger.info("closed %d postings past their 'Advertised until' date", closed)
        return summary

    return asyncio.run(main())
