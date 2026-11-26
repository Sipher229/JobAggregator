"""Persist scraped job postings and link them to designated employers.

Rules
* Only postings located in one of the 18 communities are stored.
* Job Bank postings are matched with core.matcher.match_employer (name >= 85
  AND the job's city inside one of the employer's designated communities);
  75-84 / ties are stored as `needs_review`, everything else `unmatched`.
* Career-site postings already know their employer; the geographic check still
  applies (a national chain's Toronto opening is not stored). Postings without a
  location are kept as `needs_review`.
* Upsert key: (source, external_id). dedupe_hash = sha256(employer|title|city);
  a career-site posting that duplicates an active Job Bank posting is skipped.
* Deactivation: after a *complete* sync of a scope, active postings of that
  source in that scope not seen in this run become `is_active=False`.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload

from config.communities import COMMUNITIES_BY_SLUG
from core.matcher import (
    EmployerCandidate,
    MatchResult,
    community_for_place,
    dedupe_hash,
    match_employer,
)
from core.models import (
    Community,
    CommunityProgram,
    DesignationStatus,
    Employer,
    EmployerCommunity,
    JobPosting,
    JobSource,
    MatchStatus,
    utcnow,
)
from core.schemas import JobPostingIn

logger = logging.getLogger(__name__)


@dataclass
class JobSyncStats:
    found: int = 0
    in_scope: int = 0
    created: int = 0
    updated: int = 0
    matched: int = 0
    needs_review: int = 0
    deactivated: int = 0
    duplicates_skipped: int = 0
    websites_added: int = 0
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Candidates
# --------------------------------------------------------------------------- #
def load_candidates(session: Session) -> list[EmployerCandidate]:
    """Designated (non-revoked) employers with the communities they're designated in."""
    rows = session.execute(
        select(Employer.id, Employer.canonical_name, Employer.operating_name, Community.slug)
        .join(EmployerCommunity, EmployerCommunity.employer_id == Employer.id)
        .join(CommunityProgram, CommunityProgram.id == EmployerCommunity.community_program_id)
        .join(Community, Community.id == CommunityProgram.community_id)
        .where(EmployerCommunity.status != DesignationStatus.REVOKED)
    ).all()
    acc: dict[int, tuple[str, str | None, set[str]]] = {}
    for emp_id, name, op, slug in rows:
        acc.setdefault(emp_id, (name, op, set()))[2].add(slug)
    return [EmployerCandidate.build(emp_id, name, slugs, operating_name=op)
            for emp_id, (name, op, slugs) in acc.items()]


class CandidateIndex:
    """Candidates bucketed by community, so matching only scores local employers."""

    def __init__(self, candidates: Iterable[EmployerCandidate]):
        self.all = list(candidates)
        self.by_id = {c.employer_id: c for c in self.all}
        self.by_community: dict[str, list[EmployerCandidate]] = {}
        for c in self.all:
            for slug in c.community_slugs:
                self.by_community.setdefault(slug, []).append(c)

    def match(self, job: JobPostingIn) -> MatchResult:
        slug = community_for_place(job.city, job.province, job.postal_code)
        if job.employer_id is not None:
            cand = self.by_id.get(job.employer_id)
            if cand is None:
                return MatchResult("unmatched", community_slug=slug, reason="employer_not_designated")
            if not job.city and not job.postal_code:
                only = next(iter(cand.community_slugs)) if len(cand.community_slugs) == 1 else None
                return MatchResult("needs_review", job.employer_id, 100.0, only, "no_location")
            if slug in cand.community_slugs:
                return MatchResult("matched", job.employer_id, 100.0, slug, "career_site")
            return MatchResult("unmatched", community_slug=slug, reason="geo_mismatch")
        if slug is None:
            return MatchResult("unmatched", reason="outside_communities")
        # Score against local employers only, but keep geo-mismatch logging useful.
        local = self.by_community.get(slug, [])
        return match_employer(job.employer_name, job.city, local,
                              province=job.province, postal_code=job.postal_code)


# --------------------------------------------------------------------------- #
# Upsert
# --------------------------------------------------------------------------- #
_COPY_FIELDS = ("title", "noc_code", "teer_category", "city", "province", "wage_raw",
                "employment_type", "employment_term", "language_req", "url", "posted_at",
                "source_detail", "job_number", "postal_code", "vacancies", "work_mode")


def upsert_jobs(session: Session, jobs: Iterable[JobPostingIn], index: CandidateIndex,
                *, run_started: datetime, stats: JobSyncStats | None = None) -> JobSyncStats:
    stats = stats or JobSyncStats()
    jobs = list(jobs)
    stats.found += len(jobs)
    if not jobs:
        return stats

    community_ids = {c.slug: c.id for c in session.scalars(select(Community))}
    source_values = {j.source for j in jobs}
    existing = {
        (p.source, p.external_id): p
        for p in session.scalars(select(JobPosting).where(
            JobPosting.source.in_(source_values),
            JobPosting.external_id.in_([j.external_id for j in jobs])))
    }
    employers: dict[int, Employer] = {}

    for job in jobs:
        result = index.match(job)
        if result.community_slug is None:
            continue
        stats.in_scope += 1
        employer_id = result.employer_id if result.status in ("matched", "needs_review") else None
        h = dedupe_hash(employer_id, job.title, job.city)

        if job.source is JobSource.CAREER_SITE and employer_id is not None:
            dup = session.scalar(select(JobPosting.id).where(
                JobPosting.dedupe_hash == h, JobPosting.is_active.is_(True),
                JobPosting.source != JobSource.CAREER_SITE))
            if dup:
                stats.duplicates_skipped += 1
                continue

        post = existing.get((job.source, job.external_id))
        if post is None:
            post = JobPosting(source=job.source, external_id=job.external_id, first_seen_at=run_started)
            session.add(post)
            existing[(job.source, job.external_id)] = post
            stats.created += 1
        else:
            stats.updated += 1

        for name in _COPY_FIELDS:
            value = getattr(job, name)
            if value is not None or getattr(post, name) is None:
                setattr(post, name, value)
        post.employer_name_raw = job.employer_name
        post.employer_id = employer_id
        post.community_id = community_ids.get(result.community_slug)
        post.match_status = MatchStatus(result.status)
        post.match_score = round(result.score, 1) if result.score else None
        post.match_reason = result.reason
        post.dedupe_hash = h
        post.is_active = True
        post.last_seen_at = run_started
        if job.details_fetched:
            post.details_fetched_at = run_started

        if result.status == "matched":
            stats.matched += 1
        elif result.status == "needs_review":
            stats.needs_review += 1

        # Enrich the employer with the website shown on its own posting.
        if result.status == "matched" and job.employer_website:
            emp = employers.get(employer_id) or session.get(Employer, employer_id)
            employers[employer_id] = emp
            if emp is not None and not emp.website_url:
                emp.website_url = job.employer_website
                stats.websites_added += 1

    session.flush()
    return stats


def deactivate_unseen(session: Session, source: JobSource, run_started: datetime, *,
                      community_slugs: Iterable[str] | None = None,
                      employer_ids: Iterable[int] | None = None) -> int:
    """Mark postings of `source` in the given scope not seen since run_started inactive."""
    q = update(JobPosting).where(
        JobPosting.source == source,
        JobPosting.is_active.is_(True),
        JobPosting.last_seen_at < run_started,
    )
    if community_slugs is not None:
        ids = list(session.scalars(select(Community.id).where(Community.slug.in_(list(community_slugs)))))
        q = q.where(JobPosting.community_id.in_(ids))
    if employer_ids is not None:
        q = q.where(JobPosting.employer_id.in_(list(employer_ids)))
    result = session.execute(q.values(is_active=False).execution_options(synchronize_session=False))
    session.expire_all()
    return result.rowcount or 0


def mark_expired(session: Session, source: JobSource, external_ids: Iterable[str]) -> int:
    ids = list(external_ids)
    if not ids:
        return 0
    result = session.execute(
        update(JobPosting).where(JobPosting.source == source, JobPosting.external_id.in_(ids))
        .values(is_active=False).execution_options(synchronize_session=False))
    return result.rowcount or 0


def needs_detail(session: Session, source: JobSource, external_ids: Iterable[str]) -> set[str]:
    """External ids that have never had their detail page fetched."""
    ids = list(external_ids)
    if not ids:
        return set()
    done = set(session.scalars(select(JobPosting.external_id).where(
        JobPosting.source == source, JobPosting.external_id.in_(ids),
        JobPosting.details_fetched_at.is_not(None))))
    return set(ids) - done


def active_job_counts(session: Session) -> dict[int, int]:
    from sqlalchemy import func
    return dict(session.execute(
        select(JobPosting.employer_id, func.count(JobPosting.id))
        .where(JobPosting.is_active.is_(True), JobPosting.employer_id.is_not(None),
               JobPosting.match_status == MatchStatus.MATCHED)
        .group_by(JobPosting.employer_id)).all())


def community_slugs() -> list[str]:
    return list(COMMUNITIES_BY_SLUG)


def rematch_jobs(session: Session, *, active_only: bool = True) -> dict[str, int]:
    """Recompute employer links for stored postings (no network).

    Run after `sync-employers` (new / revoked designations) or matcher changes.
    Career-site postings keep their employer; only their status is re-derived.
    """
    index = CandidateIndex(load_candidates(session))
    community_ids = {c.slug: c.id for c in session.scalars(select(Community))}
    q = select(JobPosting)
    if active_only:
        q = q.where(JobPosting.is_active.is_(True))
    changed = {"matched": 0, "needs_review": 0, "unmatched": 0, "changed": 0}
    for post in session.scalars(q):
        job = JobPostingIn(
            source=post.source, external_id=post.external_id, title=post.title,
            employer_name=post.employer_name_raw or "", city=post.city, province=post.province,
            postal_code=post.postal_code,
            employer_id=post.employer_id if post.source is JobSource.CAREER_SITE else None,
        )
        result = index.match(job)
        employer_id = result.employer_id if result.status in ("matched", "needs_review") else None
        if (post.employer_id, post.match_status.value) != (employer_id, result.status):
            changed["changed"] += 1
        post.employer_id = employer_id
        post.match_status = MatchStatus(result.status)
        post.match_score = round(result.score, 1) if result.score else None
        post.match_reason = result.reason
        post.dedupe_hash = dedupe_hash(employer_id, post.title, post.city)
        post.community_id = community_ids.get(result.community_slug) if result.community_slug else None
        if post.community_id is None:
            post.is_active = False  # no longer inside any pilot boundary
        changed[result.status] += 1
    session.flush()
    return changed
