from sqlalchemy import func, select

from config.communities import Program
from core.employer_sync import sync_designations
from core.models import DesignationStatus, Employer, EmployerCommunity, RunStatus, ScrapeRun
from core.schemas import EmployerIn
from scrapers.employers.runner import CommunityResult, persist
from config.communities import get_community


def rec(name, slug="sudbury", program=Program.RCIP, **kw):
    return EmployerIn(name=name, community_slug=slug, program=program, **kw)


def links(session, slug="sudbury"):
    return session.scalars(select(EmployerCommunity)).all()


def test_insert_and_idempotent(seeded_session):
    s = seeded_session
    batch = [rec("Vale Canada Limited", website_url="https://vale.com"),
             rec("1234567 Ontario Inc. o/a Tim Hortons", priority_sector="Food")]
    st = sync_designations(s, "sudbury", Program.RCIP, batch)
    assert (st.found, st.created, st.linked, st.revoked) == (2, 2, 2, 0)

    tim = s.scalar(select(Employer).where(Employer.normalized_operating_name == "tim hortons"))
    assert tim.canonical_name == "1234567 Ontario Inc." and tim.operating_name == "Tim Hortons"

    st = sync_designations(s, "sudbury", Program.RCIP, batch)
    assert (st.created, st.linked, st.updated) == (0, 0, 2)
    assert s.scalar(select(func.count(Employer.id))) == 2


def test_same_employer_across_programs_and_communities(seeded_session):
    s = seeded_session
    sync_designations(s, "sudbury", Program.RCIP, [rec("Vale Canada Limited")])
    sync_designations(s, "sudbury", Program.FCIP, [rec("VALE CANADA LTD", program=Program.FCIP)])
    sync_designations(s, "thunder-bay", Program.RCIP, [rec("Vale Canada", slug="thunder-bay")])
    assert s.scalar(select(func.count(Employer.id))) == 1
    assert len(links(s)) == 3


def test_franchisees_sharing_trade_name_stay_separate(seeded_session):
    s = seeded_session
    st = sync_designations(s, "pictou-county", Program.RCIP, [
        rec("3091247 Nova Scotia Limited (Sam's Pizza, Trenton)", "pictou-county"),
        rec("OSP Restaurant Group Inc (Sam's Pizza - Stellarton)", "pictou-county"),
    ])
    assert (st.created, st.linked) == (2, 2)
    ops = {e.operating_name for e in s.scalars(select(Employer))}
    assert ops == {"Sam's Pizza"}


def test_not_hiring_status(seeded_session):
    s = seeded_session
    sync_designations(s, "kelowna", Program.FCIP,
                      [rec("Acera Insurance", "kelowna", Program.FCIP, not_hiring=True)])
    emp = s.scalar(select(Employer))
    assert emp.community_links[0].status == DesignationStatus.NOT_HIRING
    assert emp.status == DesignationStatus.NOT_HIRING
    sync_designations(s, "kelowna", Program.FCIP, [rec("Acera Insurance", "kelowna", Program.FCIP)])
    assert emp.status == DesignationStatus.ACTIVE


def test_revocation_and_reactivation(seeded_session):
    s = seeded_session
    sync_designations(s, "brandon", Program.RCIP,
                      [rec(n, "brandon") for n in ("A Co One", "B Two", "C Three")])
    st = sync_designations(s, "brandon", Program.RCIP, [rec(n, "brandon") for n in ("A Co One", "B Two")])
    assert st.revoked == 1
    c = s.scalar(select(Employer).where(Employer.normalized_name == "c three"))
    assert c.status == DesignationStatus.REVOKED and c.community_links[0].revoked_at

    st = sync_designations(s, "brandon", Program.RCIP,
                           [rec(n, "brandon") for n in ("A Co One", "B Two", "C Three")])
    assert st.revoked == 0 and c.status == DesignationStatus.ACTIVE
    assert c.community_links[0].revoked_at is None


def test_revocation_guards(seeded_session):
    s = seeded_session
    names = [f"Employer Number {i}" for i in range(10)]
    sync_designations(s, "brandon", Program.RCIP, [rec(n, "brandon") for n in names])

    st = sync_designations(s, "brandon", Program.RCIP, [])
    assert st.revoked == 0 and "0 records" in st.warnings[0]
    st = sync_designations(s, "brandon", Program.RCIP, [rec(names[0], "brandon")])
    assert st.revoked == 0 and "revocation skipped" in st.warnings[0]
    st = sync_designations(s, "brandon", Program.RCIP, [rec(names[0], "brandon")], complete=False)
    assert st.revoked == 0
    assert all(l.status == DesignationStatus.ACTIVE for l in links(s))


def test_curated_urls_not_overwritten(seeded_session):
    s = seeded_session
    sync_designations(s, "sudbury", Program.RCIP, [rec("Vale Canada", website_url="https://vale.com")])
    sync_designations(s, "sudbury", Program.RCIP, [rec("Vale Canada", website_url="https://other.com",
                                                       careers_url="https://vale.com/careers")])
    emp = s.scalar(select(Employer))
    assert emp.website_url == "https://vale.com/" or emp.website_url == "https://vale.com"
    assert emp.careers_url.startswith("https://vale.com/careers")


def test_persist_records_runs_and_failed_sources(seeded_session):
    s = seeded_session
    sudbury, brandon, moose = get_community("sudbury"), get_community("brandon"), get_community("moose-jaw")
    results = [
        CommunityResult(sudbury, [rec("Vale Canada")], {"sudbury:html:FCIP": "ScrapeError: 503"},
                        {Program.FCIP}, configured=True),
        CommunityResult(brandon, [rec("Kal Tire", "brandon")], {}, set(), configured=True),
        CommunityResult(moose, [], {}, set(), configured=False),
    ]
    summary = persist(s, results)
    assert set(summary) == {"sudbury:FCIP", "sudbury:RCIP", "brandon:RCIP", "moose-jaw:RCIP"}
    runs = {r.target: r for r in s.scalars(select(ScrapeRun))}
    assert runs["sudbury:RCIP"].status == RunStatus.SUCCESS
    assert runs["sudbury:FCIP"].status == RunStatus.PARTIAL and "503" in runs["sudbury:FCIP"].error_message
    assert runs["brandon:RCIP"].records_created == 2  # 1 employer + 1 designation
    assert runs["moose-jaw:RCIP"].status == RunStatus.PARTIAL
