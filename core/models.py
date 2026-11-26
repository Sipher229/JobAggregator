"""ORM models.

Community (18) --< CommunityProgram (20) >-- EmployerCommunity --< Employer
                                                                  |
                                                     JobPosting >-+  (employer_id nullable)
ScrapeRun: audit log of every pipeline task.

Designation status lives on EmployerCommunity because an employer is
designated per community *and* per program (e.g. active for Sudbury RCIP but
not Sudbury FCIP). Employer.status is a derived roll-up, refreshed by
Employer.refresh_status().
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from config.communities import Program
from core.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _enum(enum_cls: type[StrEnum], length: int = 20) -> Enum:
    # native_enum=False -> VARCHAR + CHECK, identical on SQLite and PostgreSQL
    return Enum(
        enum_cls,
        native_enum=False,
        length=length,
        values_callable=lambda e: [m.value for m in e],
        validate_strings=True,
    )


class DesignationStatus(StrEnum):
    ACTIVE = "active"
    NOT_HIRING = "not_hiring"
    REVOKED = "revoked"


class JobSource(StrEnum):
    JOB_BANK = "job_bank"
    CAREER_SITE = "career_site"
    COMMUNITY_BOARD = "community_board"
    INDEED = "indeed"
    LINKEDIN = "linkedin"


class MatchStatus(StrEnum):
    MATCHED = "matched"
    NEEDS_REVIEW = "needs_review"
    UNMATCHED = "unmatched"


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


# --------------------------------------------------------------------------- #
# Communities
# --------------------------------------------------------------------------- #
class Community(TimestampMixin, Base):
    __tablename__ = "community"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    province: Mapped[str] = mapped_column(String(2), index=True)
    portal_url: Mapped[str | None] = mapped_column(String(512))

    programs: Mapped[list[CommunityProgram]] = relationship(
        back_populates="community", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Community {self.slug}>"


class CommunityProgram(TimestampMixin, Base):
    """One designation of a community under a pilot (20 rows total)."""

    __tablename__ = "community_program"
    __table_args__ = (UniqueConstraint("community_id", "program"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    community_id: Mapped[int] = mapped_column(ForeignKey("community.id", ondelete="CASCADE"))
    program: Mapped[Program] = mapped_column(_enum(Program, 8), index=True)

    community: Mapped[Community] = relationship(back_populates="programs")
    employer_links: Mapped[list[EmployerCommunity]] = relationship(
        back_populates="community_program", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<CommunityProgram {self.community.slug if self.community else '?'}:{self.program}>"


# --------------------------------------------------------------------------- #
# Employers
# --------------------------------------------------------------------------- #
class Employer(TimestampMixin, Base):
    __tablename__ = "employer"

    id: Mapped[int] = mapped_column(primary_key=True)
    canonical_name: Mapped[str] = mapped_column(String(255))
    normalized_name: Mapped[str] = mapped_column(String(255), index=True)
    operating_name: Mapped[str | None] = mapped_column(String(255))
    normalized_operating_name: Mapped[str | None] = mapped_column(String(255), index=True)

    website_url: Mapped[str | None] = mapped_column(String(512))
    careers_url: Mapped[str | None] = mapped_column(String(512))
    careers_page_hash: Mapped[str | None] = mapped_column(String(64))
    careers_page_snapshot: Mapped[str | None] = mapped_column(Text)
    careers_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ats_type: Mapped[str | None] = mapped_column(String(32))
    needs_manual_review: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    status: Mapped[DesignationStatus] = mapped_column(
        _enum(DesignationStatus), default=DesignationStatus.ACTIVE, index=True
    )
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    community_links: Mapped[list[EmployerCommunity]] = relationship(
        back_populates="employer", cascade="all, delete-orphan"
    )
    jobs: Mapped[list[JobPosting]] = relationship(back_populates="employer")

    def refresh_status(self) -> DesignationStatus:
        """Roll up per-designation statuses: active > not_hiring > revoked."""
        statuses = {link.status for link in self.community_links}
        if DesignationStatus.ACTIVE in statuses:
            self.status = DesignationStatus.ACTIVE
        elif DesignationStatus.NOT_HIRING in statuses:
            self.status = DesignationStatus.NOT_HIRING
        elif statuses:
            self.status = DesignationStatus.REVOKED
        return self.status

    def __repr__(self) -> str:
        return f"<Employer {self.id} {self.canonical_name!r}>"


class EmployerCommunity(TimestampMixin, Base):
    """An employer's designation in one community under one program."""

    __tablename__ = "employer_community"
    __table_args__ = (UniqueConstraint("employer_id", "community_program_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    employer_id: Mapped[int] = mapped_column(ForeignKey("employer.id", ondelete="CASCADE"), index=True)
    community_program_id: Mapped[int] = mapped_column(
        ForeignKey("community_program.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[DesignationStatus] = mapped_column(
        _enum(DesignationStatus), default=DesignationStatus.ACTIVE, index=True
    )
    priority_sector: Mapped[str | None] = mapped_column(String(128))
    source_url: Mapped[str | None] = mapped_column(String(512))
    designated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    employer: Mapped[Employer] = relationship(back_populates="community_links")
    community_program: Mapped[CommunityProgram] = relationship(back_populates="employer_links")


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #
class JobPosting(TimestampMixin, Base):
    __tablename__ = "job_posting"
    __table_args__ = (
        UniqueConstraint("source", "external_id"),
        Index("ix_job_posting_active_employer", "is_active", "employer_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    employer_id: Mapped[int | None] = mapped_column(
        ForeignKey("employer.id", ondelete="SET NULL"), index=True
    )
    community_id: Mapped[int | None] = mapped_column(
        ForeignKey("community.id", ondelete="SET NULL"), index=True
    )
    external_id: Mapped[str] = mapped_column(String(128))
    source: Mapped[JobSource] = mapped_column(_enum(JobSource), index=True)

    title: Mapped[str] = mapped_column(String(255))
    employer_name_raw: Mapped[str | None] = mapped_column(String(255))
    noc_code: Mapped[str | None] = mapped_column(String(8))
    teer_category: Mapped[int | None] = mapped_column(Integer)
    city: Mapped[str | None] = mapped_column(String(128), index=True)
    province: Mapped[str | None] = mapped_column(String(2), index=True)
    wage_raw: Mapped[str | None] = mapped_column(String(128))
    employment_type: Mapped[str | None] = mapped_column(String(64))  # full/part time
    employment_term: Mapped[str | None] = mapped_column(String(64))  # permanent/seasonal
    language_req: Mapped[str | None] = mapped_column(String(32), index=True)
    url: Mapped[str | None] = mapped_column(String(1024))
    source_detail: Mapped[str | None] = mapped_column(String(64))   # e.g. "indeed.com" via Job Bank
    job_number: Mapped[str | None] = mapped_column(String(32))      # Job Bank "Job number"
    postal_code: Mapped[str | None] = mapped_column(String(10))
    vacancies: Mapped[int | None] = mapped_column(Integer)
    work_mode: Mapped[str | None] = mapped_column(String(32))       # On site / Hybrid / Remote
    match_reason: Mapped[str | None] = mapped_column(String(32))
    details_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    match_status: Mapped[MatchStatus] = mapped_column(
        _enum(MatchStatus), default=MatchStatus.UNMATCHED, index=True
    )
    match_score: Mapped[float | None] = mapped_column(Float)
    dedupe_hash: Mapped[str | None] = mapped_column(String(64), index=True)  # NOT unique

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    employer: Mapped[Employer | None] = relationship(back_populates="jobs")
    community: Mapped[Community | None] = relationship()

    def __repr__(self) -> str:
        return f"<JobPosting {self.source}:{self.external_id} {self.title!r}>"


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #
class ScrapeRun(Base):
    __tablename__ = "scrape_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_name: Mapped[str] = mapped_column(String(64), index=True)
    target: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[RunStatus] = mapped_column(_enum(RunStatus), default=RunStatus.RUNNING)
    records_found: Mapped[int] = mapped_column(Integer, default=0)
    records_created: Mapped[int] = mapped_column(Integer, default=0)
    records_updated: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
