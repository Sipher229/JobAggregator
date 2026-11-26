"""Pydantic v2 DTOs (shared by CLI, exporters and a future FastAPI layer)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from config.communities import Program
from core.models import DesignationStatus, JobSource, MatchStatus, RunStatus


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- Communities -------------------------------------------------------------
class CommunityRead(ORMModel):
    id: int
    slug: str
    name: str
    province: str
    portal_url: str | None = None


class CommunityProgramRead(ORMModel):
    id: int
    program: Program
    community: CommunityRead


# --- Employers ---------------------------------------------------------------
class EmployerIn(BaseModel):
    """Raw employer record emitted by an employer scraper (pre-resolution)."""

    name: str = Field(min_length=1, max_length=255)
    community_slug: str
    program: Program
    operating_name: str | None = None
    website_url: HttpUrl | None = None
    careers_url: HttpUrl | None = None
    priority_sector: str | None = None
    location: str | None = None
    not_hiring: bool = False
    flags: list[str] = []
    source_url: str | None = None


class EmployerDesignationRead(ORMModel):
    community_program: CommunityProgramRead
    status: DesignationStatus
    priority_sector: str | None = None
    designated_at: datetime | None = None
    revoked_at: datetime | None = None


class EmployerRead(ORMModel):
    id: int
    canonical_name: str
    operating_name: str | None = None
    website_url: str | None = None
    careers_url: str | None = None
    ats_type: str | None = None
    status: DesignationStatus
    needs_manual_review: bool
    first_seen_at: datetime
    last_seen_at: datetime


class EmployerDetail(EmployerRead):
    community_links: list[EmployerDesignationRead] = []


# --- Jobs --------------------------------------------------------------------
class JobPostingIn(BaseModel):
    """Raw job record emitted by a job scraper (pre-matching)."""

    source: JobSource
    external_id: str
    title: str
    employer_name: str
    city: str | None = None
    province: str | None = Field(default=None, max_length=2)
    postal_code: str | None = None
    noc_code: str | None = None
    teer_category: int | None = Field(default=None, ge=0, le=5)
    wage_raw: str | None = None
    employment_type: str | None = None
    employment_term: str | None = None
    language_req: str | None = None
    url: str | None = None
    posted_at: datetime | None = None
    expires_at: datetime | None = None      # "Advertised until" / validThrough
    source_detail: str | None = None     # original board when aggregated (indeed.com, ...)
    job_number: str | None = None
    vacancies: int | None = None
    work_mode: str | None = None
    employer_website: str | None = None  # from the posting, used to enrich Employer
    employer_id: int | None = None       # known up front for career-site jobs
    details_fetched: bool = False


class JobPostingRead(ORMModel):
    id: int
    employer_id: int | None
    source: JobSource
    external_id: str
    title: str
    employer_name_raw: str | None = None
    noc_code: str | None = None
    teer_category: int | None = None
    city: str | None = None
    province: str | None = None
    wage_raw: str | None = None
    language_req: str | None = None
    url: str | None = None
    match_status: MatchStatus
    match_score: float | None = None
    is_active: bool
    posted_at: datetime | None = None
    last_seen_at: datetime


# --- Runs --------------------------------------------------------------------
class ScrapeRunRead(ORMModel):
    id: int
    task_name: str
    target: str | None = None
    status: RunStatus
    records_found: int
    records_created: int
    records_updated: int
    error_message: str | None = None
    started_at: datetime
    finished_at: datetime | None = None
