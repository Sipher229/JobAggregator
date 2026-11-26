import io

import pdfplumber
import pytest
from openpyxl import load_workbook

from config.communities import Program
from core.employer_sync import sync_designations
from core.exporter import ReportFilter, build_report, export
from core.job_sync import CandidateIndex, load_candidates, upsert_jobs
from core.models import JobSource, utcnow
from core.schemas import EmployerIn, JobPostingIn


def emp(name, slug, program, **kw):
    return EmployerIn(name=name, community_slug=slug, program=program, **kw)


def job(ext, title, employer, city, prov, lang=None, noc=None, **kw):
    return JobPostingIn(source=JobSource.JOB_BANK, external_id=ext, title=title, employer_name=employer,
                        city=city, province=prov, language_req=lang, noc_code=noc,
                        teer_category=int(noc[1]) if noc else None,
                        url=f"https://www.jobbank.gc.ca/jobsearch/jobposting/{ext}", **kw)


@pytest.fixture()
def db(seeded_session):
    s = seeded_session
    sync_designations(s, "sudbury", Program.FCIP, [emp("Collège Boréal", "sudbury", Program.FCIP,
                                                       priority_sector="Education")])
    sync_designations(s, "sudbury", Program.RCIP, [emp("Collège Boréal", "sudbury", Program.RCIP),
                                                   emp("Vale Canada Limited", "sudbury", Program.RCIP,
                                                       website_url="https://vale.com")])
    sync_designations(s, "brandon", Program.RCIP, [emp("Kal Tire", "brandon", Program.RCIP, not_hiring=True)])
    s.commit()
    index = CandidateIndex(load_candidates(s))
    upsert_jobs(s, [
        job("1", "Enseignant(e)", "Collège Boréal", "Sudbury", "ON", lang="French", noc="41210"),
        job("2", "Millwright", "Vale Canada Ltd", "Copper Cliff", "ON", lang="English", noc="72400"),
        job("3", "Agent bilingue", "College Boreal", "Sudbury", "ON", lang="Bilingual"),
        job("4", "Tire Technician", "Kal Tire", "Brandon", "MB", lang="English"),
        job("5", "Cashier", "Unrelated Shop", "Sudbury", "ON"),                 # unmatched -> not exported
    ], index, run_started=utcnow())
    s.commit()
    return s


def test_report_filters(db):
    all_ = build_report(db, ReportFilter())
    assert len(all_.jobs) == 4
    boreal = next(r for r in all_.jobs if r["Job Title"] == "Enseignant(e)")
    assert boreal["Programs"] == "FCIP/RCIP" and boreal["NOC"] == "41210" and boreal["TEER"] == 1
    assert boreal["Priority Sector"].startswith("Education") or boreal["Priority Sector"] == "Education"

    fcip = build_report(db, ReportFilter(program="FCIP"))
    assert {r["Job Title"] for r in fcip.jobs} == {"Enseignant(e)", "Agent bilingue"}
    assert all(r["Programs"] == "FCIP" for r in fcip.jobs)

    fr = build_report(db, ReportFilter(lang="fr"))
    assert {r["Language"] for r in fr.jobs} == {"French", "Bilingual"}
    en = build_report(db, ReportFilter(lang="en"))
    assert {r["Language"] for r in en.jobs} == {"English", "Bilingual"}

    brandon = build_report(db, ReportFilter(community="brandon"))
    assert [r["Job Title"] for r in brandon.jobs] == ["Tire Technician"]
    assert brandon.jobs[0]["Designation Status"] == "not hiring"


def test_employer_directory_and_summary(db):
    rep = build_report(db, ReportFilter())
    rows = {(r["Employer"], r["Program"]): r for r in rep.employers}
    assert rows[("Collège Boréal", "FCIP")]["Active Jobs"] == 2
    assert rows[("Vale Canada Limited", "RCIP")]["Website"].rstrip("/") == "https://vale.com"
    summary = {(r["Community"], r["Program"]): r for r in rep.summary}
    assert summary[("Greater Sudbury", "RCIP")]["Designated Employers"] == 2
    assert summary[("Greater Sudbury", "FCIP")]["French/Bilingual Jobs"] == 2
    assert summary[("Brandon", "RCIP")]["Not Hiring"] == 1
    total = rep.summary[-1]
    assert total["Community"] == "TOTAL" and total["Designated Employers"] == 4


def test_invalid_filters():
    with pytest.raises(ValueError):
        ReportFilter(program="XYZ")
    with pytest.raises(ValueError):
        ReportFilter(lang="de")
    with pytest.raises(ValueError):
        ReportFilter(community="atlantis")


def test_csv_export(db, tmp_path):
    (path,) = export(db, ["csv"], ReportFilter(), tmp_path)
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")                       # UTF-8 BOM for Excel
    text = raw.decode("utf-8-sig")
    assert "Collège Boréal" in text and "Unrelated Shop" not in text
    assert len(text.strip().splitlines()) == 5                  # header + 4


def test_xlsx_export(db, tmp_path):
    (path,) = export(db, ["xlsx"], ReportFilter(program="RCIP"), tmp_path)
    wb = load_workbook(path)
    assert wb.sheetnames == ["Active Job Postings", "Designated Employers", "Summary", "About"]
    jobs = wb["Active Job Postings"]
    assert jobs.freeze_panes == "A2" and jobs.auto_filter.ref.startswith("A1:")
    header = [c.value for c in jobs[1]]
    url_col = header.index("URL") + 1
    assert jobs.cell(row=2, column=url_col).hyperlink.target.startswith("https://www.jobbank.gc.ca/")
    assert jobs.column_dimensions["G"].width >= 10
    assert jobs.max_row == 5                                       # RCIP: 4 postings + header
    assert wb["Summary"].cell(row=wb["Summary"].max_row, column=2).value == "TOTAL"


def test_pdf_export(db, tmp_path):
    (path,) = export(db, ["pdf"], ReportFilter(), tmp_path)
    data = path.read_bytes()
    assert data.startswith(b"%PDF")
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        text = "\n".join(p.extract_text() or "" for p in pdf.pages)
        links = [a for p in pdf.pages for a in (p.hyperlinks or [])]
    assert "Designated-Employer Job Report" in text
    assert text.index("Manitoba") < text.index("Ontario")          # grouped by province
    assert "Greater Sudbury" in text and "Collège Boréal" in text and "41210 / 1" in text
    assert any("jobbank.gc.ca" in (l.get("uri") or "") for l in links)


def test_pdf_export_empty(db, tmp_path):
    (path,) = export(db, ["pdf"], ReportFilter(community="kelowna"), tmp_path)
    with pdfplumber.open(path) as pdf:
        assert "No active postings" in pdf.pages[0].extract_text()


def test_all_formats_share_one_report(db, tmp_path):
    paths = export(db, ["csv", "xlsx", "pdf"], ReportFilter(lang="fr"), tmp_path)
    assert [p.suffix for p in paths] == [".csv", ".xlsx", ".pdf"]
    assert all("all_all_fr" in p.name for p in paths)
