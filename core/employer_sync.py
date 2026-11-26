"""Persist scraped employer records (EmployerIn) into the database.

Identity: an employer is the same row when its normalized legal or operating
name matches exactly (deterministic; no fuzzy merging). Near-duplicates
(token_sort_ratio >= 92) are logged for manual review instead of merged.

Revocation: after a successful sync of a (community, program), designations
that were not seen are marked `revoked`, unless the run looks broken (zero
records, or fewer than REVOCATION_GUARD of the previously active count), in
which case nothing is revoked and the run is marked `partial`.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field

from rapidfuzz import fuzz, process
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from config.communities import Program
from core.matcher import normalize_name, split_operating_name
from core.models import (
    Community,
    CommunityProgram,
    DesignationStatus,
    Employer,
    EmployerCommunity,
    RunStatus,
    ScrapeRun,
    utcnow,
)
from core.schemas import EmployerIn
from core.urls import clean_website

logger = logging.getLogger(__name__)

REVOCATION_GUARD = 0.5
DUPLICATE_HINT_THRESHOLD = 92


@dataclass
class SyncStats:
    found: int = 0
    created: int = 0          # new employers
    linked: int = 0           # new designations
    updated: int = 0          # refreshed designations
    revoked: int = 0
    possible_duplicates: list[tuple[str, str, float]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class EmployerIndex:
    """In-memory lookup of employers by normalized *legal* name.

    Operating/trade names are deliberately not identity keys: franchisees share
    them ("3091247 Nova Scotia Limited (Sam's Pizza)" and "OSP Restaurant Group
    Inc (Sam's Pizza)" are different employers). Trade names are used only for
    job matching (core.matcher.name_variants).
    """

    def __init__(self, session: Session):
        self.by_name: dict[str, Employer] = {}
        for emp in session.scalars(select(Employer)):
            self.add(emp)

    def add(self, emp: Employer) -> None:
        if emp.normalized_name:
            self.by_name.setdefault(emp.normalized_name, emp)

    def find(self, *keys: str | None) -> Employer | None:
        for key in keys:
            if key and key in self.by_name:
                return self.by_name[key]
        return None

    def near(self, key: str) -> tuple[str, float] | None:
        hit = process.extractOne(key, self.by_name.keys(), scorer=fuzz.token_sort_ratio,
                                 score_cutoff=DUPLICATE_HINT_THRESHOLD)
        return (hit[0], hit[1]) if hit else None


def _community_program(session: Session, slug: str, program: Program) -> CommunityProgram:
    cp = session.scalar(
        select(CommunityProgram).join(Community)
        .where(Community.slug == slug, CommunityProgram.program == program)
        .options(selectinload(CommunityProgram.employer_links))
    )
    if cp is None:
        raise LookupError(f"{slug}:{program} not in database; run `cli.py init-db` first")
    return cp


def upsert_employer(session: Session, index: EmployerIndex, rec: EmployerIn,
                    stats: SyncStats) -> Employer:
    legal, op_from_name = split_operating_name(rec.name)
    operating = rec.operating_name or op_from_name
    norm_legal = normalize_name(legal)
    norm_op = normalize_name(operating) if operating else None

    emp = index.find(norm_legal)
    if emp is None:
        hint = index.near(norm_legal)
        if hint:
            stats.possible_duplicates.append((rec.name, hint[0], hint[1]))
            logger.warning("Possible duplicate employer: %r ~ %r (%.0f)", rec.name, hint[0], hint[1])
        emp = Employer(
            canonical_name=legal,
            normalized_name=norm_legal,
            operating_name=operating,
            normalized_operating_name=norm_op,
        )
        session.add(emp)
        index.add(emp)
        stats.created += 1
    elif operating and not emp.operating_name:
        emp.operating_name, emp.normalized_operating_name = operating, norm_op
        index.add(emp)

    # Fill blanks (or replace unusable values); never overwrite a good URL.
    set_website(emp, str(rec.website_url) if rec.website_url else None)
    if rec.careers_url and not clean_website(emp.careers_url):
        emp.careers_url = clean_website(str(rec.careers_url))
    emp.last_seen_at = utcnow()
    return emp


def set_website(emp: Employer, candidate: str | None) -> bool:
    """Use `candidate` if the employer has no usable website yet. A new URL
    clears the previous career-site error, so it gets retried."""
    candidate = clean_website(candidate)
    if not candidate or clean_website(emp.website_url):
        return False
    emp.website_url = candidate
    emp.website_error = None
    return True


def clean_employer_websites(session: Session) -> dict[str, int]:
    """Normalise stored website/careers URLs; drop ones that can't be an employer site."""
    stats = {"fixed": 0, "removed": 0}
    for emp in session.scalars(select(Employer).where(
            (Employer.website_url.is_not(None)) | (Employer.careers_url.is_not(None)))):
        for attr in ("website_url", "careers_url"):
            old = getattr(emp, attr)
            if old is None:
                continue
            new = clean_website(old)
            if new != old:
                setattr(emp, attr, new)
                stats["removed" if new is None else "fixed"] += 1
                if attr == "website_url":
                    emp.website_error = None
    session.flush()
    return stats


def sync_designations(session: Session, slug: str, program: Program,
                      records: Iterable[EmployerIn], *, complete: bool = True,
                      index: EmployerIndex | None = None) -> SyncStats:
    """Upsert one community-program's employer list.

    complete=False (some source failed) disables revocation.
    """
    records = [r for r in records if r.community_slug == slug and r.program == program]
    stats = SyncStats(found=len(records))
    index = index or EmployerIndex(session)
    cp = _community_program(session, slug, program)
    links = {link.employer_id: link for link in cp.employer_links if link.employer_id}
    previously_active = sum(1 for link in links.values()
                            if link.status != DesignationStatus.REVOKED)
    seen: set[int] = set()
    now = utcnow()

    for rec in records:
        emp = upsert_employer(session, index, rec, stats)
        session.flush()  # assign emp.id
        link = links.get(emp.id)
        if link is None:
            link = EmployerCommunity(employer=emp, community_program=cp, designated_at=now)
            session.add(link)
            links[emp.id] = link
            stats.linked += 1
        else:
            stats.updated += 1
        link.status = DesignationStatus.NOT_HIRING if rec.not_hiring else DesignationStatus.ACTIVE
        link.revoked_at = None
        link.last_seen_at = now
        link.priority_sector = rec.priority_sector or link.priority_sector
        link.source_url = rec.source_url or link.source_url
        seen.add(emp.id)

    if not complete:
        stats.warnings.append("incomplete run: revocation skipped")
    elif not records:
        stats.warnings.append("0 records: revocation skipped")
    elif previously_active and len(seen) < REVOCATION_GUARD * previously_active:
        stats.warnings.append(
            f"found {len(seen)} < {REVOCATION_GUARD:.0%} of {previously_active} active: "
            "revocation skipped")
    else:
        for emp_id, link in links.items():
            if emp_id not in seen and link.status != DesignationStatus.REVOKED:
                link.status = DesignationStatus.REVOKED
                link.revoked_at = now
                stats.revoked += 1

    session.flush()
    for link in links.values():
        session.refresh(link.employer, ["community_links"])
        link.employer.refresh_status()
    for w in stats.warnings:
        logger.warning("%s:%s %s", slug, program, w)
    return stats


def record_run(session: Session, task_name: str, target: str) -> ScrapeRun:
    run = ScrapeRun(task_name=task_name, target=target, status=RunStatus.RUNNING)
    session.add(run)
    session.flush()
    return run


def finish_run(run: ScrapeRun, stats: SyncStats | None, *, error: str | None = None) -> None:
    run.finished_at = utcnow()
    if stats:
        run.records_found = stats.found
        run.records_created = stats.created + stats.linked
        run.records_updated = stats.updated
    if error:
        run.status = RunStatus.FAILED if not stats else RunStatus.PARTIAL
        run.error_message = error
    elif stats and stats.warnings:
        run.status = RunStatus.PARTIAL
        run.error_message = "; ".join(stats.warnings)
    else:
        run.status = RunStatus.SUCCESS
