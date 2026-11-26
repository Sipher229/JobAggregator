import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from config.communities import Program
from core.matcher import dedupe_hash, normalize_name
from core.models import (
    Community,
    CommunityProgram,
    DesignationStatus,
    Employer,
    EmployerCommunity,
    JobPosting,
    JobSource,
    MatchStatus,
)
from core.schemas import EmployerDetail, JobPostingRead
from core.seed import sync_communities


def _cp(session, slug, program):
    return session.scalar(
        select(CommunityProgram).join(Community)
        .where(Community.slug == slug, CommunityProgram.program == program)
    )


def test_seed_is_idempotent(seeded_session):
    s = seeded_session
    assert s.scalar(select(func.count(Community.id))) == 18
    assert s.scalar(select(func.count(CommunityProgram.id))) == 20
    assert sync_communities(s) == {"created": 0, "updated": 0}
    s.commit()
    assert s.scalar(select(func.count(CommunityProgram.id))) == 20


def test_employer_dual_designation_and_status_rollup(seeded_session):
    s = seeded_session
    rcip, fcip = _cp(s, "sudbury", Program.RCIP), _cp(s, "sudbury", Program.FCIP)
    emp = Employer(canonical_name="Vale Canada Limited", normalized_name=normalize_name("Vale Canada Limited"))
    emp.community_links = [
        EmployerCommunity(community_program=rcip),
        EmployerCommunity(community_program=fcip, status=DesignationStatus.REVOKED),
    ]
    s.add(emp)
    s.commit()

    assert emp.normalized_name == "vale canada"
    assert {l.community_program.program for l in emp.community_links} == {Program.FCIP, Program.RCIP}
    assert emp.refresh_status() == DesignationStatus.ACTIVE
    for link in emp.community_links:
        link.status = DesignationStatus.REVOKED
    assert emp.refresh_status() == DesignationStatus.REVOKED

    detail = EmployerDetail.model_validate(emp)
    assert len(detail.community_links) == 2


def test_duplicate_designation_rejected(seeded_session):
    s = seeded_session
    cp = _cp(s, "timmins", Program.RCIP)
    emp = Employer(canonical_name="X", normalized_name="x")
    emp.community_links = [EmployerCommunity(community_program=cp), EmployerCommunity(community_program=cp)]
    s.add(emp)
    with pytest.raises(IntegrityError):
        s.commit()


def test_job_posting_constraints(seeded_session):
    s = seeded_session
    emp = Employer(canonical_name="Acme", normalized_name="acme")
    s.add(emp)
    s.flush()

    h = dedupe_hash(emp.id, "Cook", "Brandon")
    # Same title/city twice from different postings: dedupe_hash is NOT unique.
    s.add_all([
        JobPosting(employer=emp, source=JobSource.JOB_BANK, external_id="1", title="Cook",
                   city="Brandon", province="MB", dedupe_hash=h, match_status=MatchStatus.MATCHED),
        JobPosting(employer=emp, source=JobSource.JOB_BANK, external_id="2", title="Cook",
                   city="Brandon", province="MB", dedupe_hash=h),
        # Unmatched job (no employer) is allowed.
        JobPosting(source=JobSource.CAREER_SITE, external_id="1", title="Welder"),
    ])
    s.commit()
    assert JobPostingRead.model_validate(emp.jobs[0]).match_status == MatchStatus.MATCHED

    s.add(JobPosting(source=JobSource.JOB_BANK, external_id="1", title="Dup"))
    with pytest.raises(IntegrityError):
        s.commit()


def test_enum_rejects_invalid_value(seeded_session):
    s = seeded_session
    s.add(JobPosting(source="monster", external_id="9", title="Bad"))
    with pytest.raises(Exception):
        s.commit()
