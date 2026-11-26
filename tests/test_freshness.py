from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from config.communities import Program
from core.employer_sync import sync_designations
from core.exporter import ReportFilter, build_report
from core.freshness import assess
from core.job_sync import (
    CandidateIndex,
    close_expired,
    deactivate_unseen,
    is_expired,
    load_candidates,
    mark_expired,
    needs_detail,
    upsert_jobs,
)
from core.models import JobPosting, JobSource
from core.schemas import EmployerIn, JobPostingIn
from scrapers.jobs.career_site import parse_jsonld_jobs

NOW = datetime(2026, 11, 26, 15, 0, tzinfo=timezone.utc)
DAY = timedelta(days=1)


def d(days_ago: float) -> datetime:
    return NOW - timedelta(days=days_ago)


@pytest.mark.parametrize("posted, seen, expires, flag, days_up", [
    (d(5), d(0), NOW + 20 * DAY, "ok", 5),
    (d(5), d(0), None, "no expiry date", 5),
    (d(70), d(0), NOW + 30 * DAY, "stale", 70),
    (d(10), d(0), NOW + 3 * DAY, "expiring soon", 10),
    (d(10), d(5), NOW + 20 * DAY, "unverified", 10),
    (d(40), d(0), d(2), "expired", 40),
])
def test_assess_flags(posted, seen, expires, flag, days_up):
    f = assess(posted, None, seen, expires, now=NOW)
    assert (f.flag, f.days_up) == (flag, days_up)


def test_assess_uses_first_seen_and_naive_datetimes():
    f = assess(None, d(3).replace(tzinfo=None), NOW.replace(tzinfo=None), None, now=NOW)
    assert f.days_up == 3


def test_expiry_is_end_of_day():
    until = datetime(2026, 11, 26, tzinfo=timezone.utc)            # "Advertised until 2026-11-26"
    assert not is_expired(until, datetime(2026, 11, 26, 23, 59, tzinfo=timezone.utc))
    assert is_expired(until, datetime(2026, 11, 27, 0, 0, tzinfo=timezone.utc))
    assert assess(None, None, None, until, now=datetime(2026, 11, 26, 12, tzinfo=timezone.utc)).days_left == 0


def test_jsonld_valid_through():
    html = '''<script type="application/ld+json">{"@type":"JobPosting","title":"Cook",
      "datePosted":"2026-11-01","validThrough":"2026-12-01T23:59:00Z"}</script>'''
    assert parse_jsonld_jobs(html, "https://x.ca/")[0]["expires_at"].date().isoformat() == "2026-12-01"


# --------------------------------------------------------------------------- #
def jb(ext, title="Driller", **kw):
    return JobPostingIn(source=JobSource.JOB_BANK, external_id=ext, title=title,
                        employer_name="Dumas Contracting", city="Timmins", province="ON", **kw)


@pytest.fixture()
def index(seeded_session):
    sync_designations(seeded_session, "timmins", Program.RCIP, [
        EmployerIn(name="Dumas Contracting Ltd.", community_slug="timmins", program=Program.RCIP)])
    seeded_session.commit()
    return CandidateIndex(load_candidates(seeded_session))


def rows(s):
    return {p.external_id: p for p in s.scalars(select(JobPosting))}


def test_closed_reasons(seeded_session, index):
    s = seeded_session
    t1 = NOW - 2 * DAY
    upsert_jobs(s, [jb("1"), jb("2"), jb("3", expires_at=NOW - 3 * DAY),       # 3: listed but expired
                    jb("4", expires_at=NOW - 3 * DAY + DAY * 0)], index, run_started=t1)
    r = rows(s)
    assert r["3"].is_active is False and r["3"].closed_reason == "expired"
    assert r["3"].closed_at.replace(tzinfo=None) == t1.replace(tzinfo=None)  # SQLite stores naive
    upsert_jobs(s, [jb("1")], index, run_started=NOW)
    assert deactivate_unseen(s, JobSource.JOB_BANK, NOW, community_slugs=["timmins"]) >= 1
    assert mark_expired(s, JobSource.JOB_BANK, ["1"]) == 1
    r = rows(s)
    assert (r["1"].closed_reason, r["2"].closed_reason) == ("removed", "not_seen")
    # seen again -> reopened, closure cleared
    upsert_jobs(s, [jb("2", expires_at=NOW + 10 * DAY)], index, run_started=NOW + DAY)
    r2 = rows(s)["2"]
    assert r2.is_active and r2.closed_reason is None and r2.closed_at is None


def test_close_expired_without_network(seeded_session, index):
    s = seeded_session
    upsert_jobs(s, [jb("1", expires_at=NOW + 2 * DAY), jb("2", expires_at=NOW + 10 * DAY), jb("3")],
                index, run_started=NOW)
    s.commit()
    assert close_expired(s, now=NOW + 3 * DAY + timedelta(hours=1)) == 1
    r = rows(s)
    assert (r["1"].is_active, r["1"].closed_reason) == (False, "expired")
    assert r["2"].is_active and r["3"].is_active       # no expiry date -> left alone


def test_needs_detail_backfills_missing_expiry(seeded_session, index):
    s = seeded_session
    from core.models import utcnow
    now = utcnow()
    upsert_jobs(s, [jb("1", details_fetched=True, expires_at=now + 10 * DAY),
                    jb("2", details_fetched=True),            # fetched, no expiry, recently
                    jb("3")], index, run_started=now)
    assert needs_detail(s, JobSource.JOB_BANK, ["1", "2", "3"]) == {"3"}
    rows(s)["2"].details_fetched_at = now - 8 * DAY          # old detail without expiry -> refetch
    s.flush()
    assert needs_detail(s, JobSource.JOB_BANK, ["1", "2", "3"]) == {"2", "3"}


def test_export_freshness(seeded_session, index):
    s = seeded_session
    from core.models import utcnow
    now = utcnow()
    upsert_jobs(s, [jb("fresh", "Welder", posted_at=now - 3 * DAY, expires_at=now + 20 * DAY),
                    jb("old", "Miner", posted_at=now - 90 * DAY, expires_at=now + 20 * DAY),
                    jb("soon", "Cook", posted_at=now - 20 * DAY, expires_at=now + 2 * DAY)],
                index, run_started=now)
    # expired but not yet closed by a sync: still excluded from exports
    upsert_jobs(s, [jb("gone", "Clerk", posted_at=now - 40 * DAY)], index, run_started=now)
    rows(s)["gone"].expires_at = now - 5 * DAY
    s.commit()

    rep = build_report(s, ReportFilter())
    by_title = {r["Job Title"]: r for r in rep.jobs}
    assert set(by_title) == {"Welder", "Miner", "Cook"}
    assert by_title["Welder"]["Freshness"] == "ok" and by_title["Welder"]["Days Up"] == 3
    assert by_title["Miner"]["Freshness"] == "stale"
    assert by_title["Cook"]["Freshness"] == "expiring soon"
    assert by_title["Cook"]["Advertised Until"] == (now + 2 * DAY).date().isoformat()
    total = rep.summary[-1]
    assert total["Active Jobs"] == 3 and total["Stale/Unverified Jobs"] == 1

    recent = build_report(s, ReportFilter(max_age=30))
    assert {r["Job Title"] for r in recent.jobs} == {"Welder", "Cook"}
    assert build_report(s, ReportFilter(stale_days=100)).jobs and all(
        r["Freshness"] != "stale" for r in build_report(s, ReportFilter(stale_days=100)).jobs)
    with pytest.raises(ValueError):
        ReportFilter(max_age=-1)
