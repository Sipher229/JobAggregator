from datetime import timedelta

from sqlalchemy import select

from config.communities import Program
from core.employer_sync import sync_designations
from core.job_sync import (
    CandidateIndex,
    deactivate_unseen,
    load_candidates,
    mark_expired,
    needs_detail,
    upsert_jobs,
)
from core.models import Employer, JobPosting, JobSource, MatchStatus, utcnow
from core.schemas import EmployerIn, JobPostingIn


def emp(name, slug, program=Program.RCIP, **kw):
    return EmployerIn(name=name, community_slug=slug, program=program, **kw)


def jb(ext, title, employer, city, prov="ON", **kw):
    return JobPostingIn(source=JobSource.JOB_BANK, external_id=ext, title=title,
                        employer_name=employer, city=city, province=prov, **kw)


def seed(s):
    sync_designations(s, "timmins", Program.RCIP, [
        emp("Dumas Contracting Ltd.", "timmins"),
        emp("MWB Contracting", "timmins"),
        emp("1887559 Ontario Inc. o/a Dark Enterprise", "timmins"),
        emp("Rubino Trucking", "timmins"),
    ])
    sync_designations(s, "brandon", Program.RCIP, [emp("Kal Tire", "brandon")])
    s.commit()
    return CandidateIndex(load_candidates(s))


def test_matching_rules(seeded_session):
    s = seeded_session
    index = seed(s)
    now = utcnow()
    stats = upsert_jobs(s, [
        jb("1", "Driller", "DUMAS CONTRACTING LTD", "Timmins"),               # match
        jb("2", "Labourer", "JMH Contracting Inc", "Kirkland Lake"),         # generic-word only -> unmatched
        jb("3", "Mechanic", "DARK Enterprise", "Hearst"),                   # trade name
        jb("4", "Driver", "ROBERT RUBINO TRUCKING LTD", "Porcupine"),       # review
        jb("5", "Tech", "Kal Tire", "Timmins"),                              # designated elsewhere -> unmatched
        jb("6", "Clerk", "Some Shop", "Toronto"),                            # outside communities -> not stored
    ], index, run_started=now)
    assert (stats.found, stats.in_scope, stats.created, stats.matched, stats.needs_review) == (6, 5, 5, 2, 1)
    rows = {p.external_id: p for p in s.scalars(select(JobPosting))}
    assert set(rows) == {"1", "2", "3", "4", "5"}
    assert rows["1"].match_status == MatchStatus.MATCHED and rows["1"].employer_id
    assert rows["2"].match_status == MatchStatus.UNMATCHED and rows["2"].employer_id is None
    assert rows["3"].match_status == MatchStatus.MATCHED
    assert rows["4"].match_status == MatchStatus.NEEDS_REVIEW and rows["4"].employer_id
    assert rows["5"].match_status == MatchStatus.UNMATCHED and rows["5"].match_reason in ("no_candidate", "geo_mismatch")
    assert all(p.community.slug == "timmins" for p in rows.values())
    assert rows["1"].dedupe_hash and rows["1"].is_active


def test_upsert_is_idempotent_and_deactivates(seeded_session):
    s = seeded_session
    index = seed(s)
    t1 = utcnow()
    upsert_jobs(s, [jb("1", "Driller", "Dumas Contracting", "Timmins"),
                    jb("2", "Welder", "Dumas Contracting", "Timmins")], index, run_started=t1)
    t2 = t1 + timedelta(hours=1)
    stats = upsert_jobs(s, [jb("1", "Driller", "Dumas Contracting", "Timmins")], index, run_started=t2)
    assert (stats.created, stats.updated) == (0, 1)
    assert deactivate_unseen(s, JobSource.JOB_BANK, t2, community_slugs=["brandon"]) == 0  # other scope
    assert deactivate_unseen(s, JobSource.JOB_BANK, t2, community_slugs=["timmins"]) == 1
    active = {p.external_id: p.is_active for p in s.scalars(select(JobPosting))}
    assert active == {"1": True, "2": False}
    # seen again later -> reactivated
    upsert_jobs(s, [jb("2", "Welder", "Dumas Contracting", "Timmins")], index, run_started=t2 + timedelta(hours=1))
    assert s.scalar(select(JobPosting).where(JobPosting.external_id == "2")).is_active


def test_details_tracking_and_expiry(seeded_session):
    s = seeded_session
    index = seed(s)
    now = utcnow()
    upsert_jobs(s, [jb("1", "Driller", "Dumas Contracting", "Timmins", details_fetched=True,
                       noc_code="73400", teer_category=3, employer_website="https://dumas.ca"),
                    jb("2", "Welder", "Dumas Contracting", "Timmins")], index, run_started=now)
    assert needs_detail(s, JobSource.JOB_BANK, ["1", "2", "3"]) == {"2", "3"}
    dumas = s.scalar(select(Employer).where(Employer.normalized_name == "dumas contracting"))
    assert dumas.website_url == "https://dumas.ca/"                     # enriched from posting (normalised)
    # a later listing without detail fields must not wipe stored NOC
    upsert_jobs(s, [jb("1", "Driller", "Dumas Contracting", "Timmins")], index, run_started=now)
    assert s.scalar(select(JobPosting).where(JobPosting.external_id == "1")).noc_code == "73400"
    assert mark_expired(s, JobSource.JOB_BANK, ["2"]) == 1


def test_rematch_after_new_designation(seeded_session):
    from core.job_sync import rematch_jobs
    s = seeded_session
    index = seed(s)
    upsert_jobs(s, [jb("1", "Cook", "Lucky's Diner", "Timmins")], index, run_started=utcnow())
    assert s.scalar(select(JobPosting)).match_status == MatchStatus.UNMATCHED
    sync_designations(s, "timmins", Program.RCIP, [emp("Lucky's Diner Inc.", "timmins")], complete=False)
    result = rematch_jobs(s)
    assert result["changed"] == 1
    post = s.scalar(select(JobPosting))
    assert post.match_status == MatchStatus.MATCHED and post.employer_id


def test_career_site_jobs(seeded_session):
    s = seeded_session
    index = seed(s)
    kal = s.scalar(select(Employer).where(Employer.normalized_name == "kal tire"))
    dumas = s.scalar(select(Employer).where(Employer.normalized_name == "dumas contracting"))
    now = utcnow()
    upsert_jobs(s, [jb("10", "Driller", "Dumas Contracting", "Timmins")], index, run_started=now)

    def cs(ext, title, employer, city, prov):
        return JobPostingIn(source=JobSource.CAREER_SITE, external_id=ext, title=title,
                            employer_name=employer.canonical_name, employer_id=employer.id,
                            city=city, province=prov)

    stats = upsert_jobs(s, [
        cs("lever:kal:1", "Tire Tech", kal, "Brandon", "MB"),          # in designated community
        cs("lever:kal:2", "Tire Tech", kal, "Toronto", "ON"),          # national chain elsewhere -> skip
        cs("lever:kal:3", "Manager", kal, None, None),                 # no location -> review
        cs("jsonld:dumas:1", "Driller", dumas, "Timmins", "ON"),       # duplicate of Job Bank posting
    ], index, run_started=now)
    assert stats.duplicates_skipped == 1
    rows = {p.external_id: p for p in s.scalars(select(JobPosting).where(JobPosting.source == JobSource.CAREER_SITE))}
    assert set(rows) == {"lever:kal:1", "lever:kal:3"}
    assert rows["lever:kal:1"].match_status == MatchStatus.MATCHED
    assert rows["lever:kal:3"].match_status == MatchStatus.NEEDS_REVIEW
    assert rows["lever:kal:3"].match_reason == "no_location"
