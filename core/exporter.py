"""Report generation: CSV, multi-tab Excel and printable PDF.

All three formats come from one filtered dataset (`build_report`):

* program:   FCIP | RCIP | ALL   (employer designated under that pilot in the
             posting's community)
* community: slug or None
* lang:      fr | en | all       ("fr" keeps French, Bilingual and
             "English or French" postings; "en" the English-capable ones)
* include_review: also export postings whose employer match needs review

CSV   - UTF-8 with BOM so Excel renders accents correctly.
XLSX  - Jobs / Employers / Summary tabs: frozen headers, filters, fitted
        columns, clickable links.
PDF   - Province -> Community -> Employer, landscape, clickable links.
"""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from config.communities import COMMUNITIES_BY_SLUG, Program
from core.database import PROJECT_ROOT
from core.freshness import STALE_DAYS, assess_posting
from core.models import (
    Community,
    CommunityProgram,
    DesignationStatus,
    Employer,
    EmployerCommunity,
    JobPosting,
    MatchStatus,
    utcnow,
)

EXPORT_DIR = PROJECT_ROOT / "exports"
FORMATS = ("csv", "xlsx", "pdf")

PROVINCE_NAMES = {"NB": "New Brunswick", "NS": "Nova Scotia", "ON": "Ontario", "MB": "Manitoba",
                  "SK": "Saskatchewan", "AB": "Alberta", "BC": "British Columbia"}
_LANG_OK = {
    "fr": {"French", "Bilingual", "English or French"},
    "en": {"English", "Bilingual", "English or French"},
}


@dataclass(frozen=True)
class ReportFilter:
    program: str = "ALL"          # FCIP | RCIP | ALL
    community: str | None = None
    lang: str = "all"             # fr | en | all
    include_review: bool = False
    max_age: int | None = None    # only postings up at most this many days
    stale_days: int = STALE_DAYS  # "stale" flag threshold

    def __post_init__(self):
        if self.program.upper() not in ("FCIP", "RCIP", "ALL"):
            raise ValueError(f"program must be FCIP, RCIP or ALL, not {self.program!r}")
        if self.lang not in ("fr", "en", "all"):
            raise ValueError(f"lang must be fr, en or all, not {self.lang!r}")
        if self.community and self.community not in COMMUNITIES_BY_SLUG:
            raise ValueError(f"unknown community {self.community!r}")
        if self.max_age is not None and self.max_age < 0:
            raise ValueError("max_age must be >= 0")

    @property
    def programs(self) -> set[Program]:
        return {Program.FCIP, Program.RCIP} if self.program.upper() == "ALL" else {Program(self.program.upper())}

    def describe(self) -> str:
        parts = [f"Program: {self.program.upper()}",
                 f"Community: {COMMUNITIES_BY_SLUG[self.community].name if self.community else 'All'}",
                 f"Language: {self.lang}"]
        if self.include_review:
            parts.append("includes matches needing review")
        if self.max_age is not None:
            parts.append(f"posted within {self.max_age} days")
        return " | ".join(parts)

    def slug(self) -> str:
        return "_".join([self.program.lower(), self.community or "all", self.lang]
                        + (["review"] if self.include_review else [])
                        + ([f"max{self.max_age}d"] if self.max_age is not None else []))


JOB_COLUMNS = [
    "Province", "Community", "Programs", "Employer", "Designation Status", "Priority Sector",
    "Job Title", "NOC", "TEER", "Language", "Employment Type", "Employment Term", "City",
    "Wage", "Vacancies", "Posted", "Days Up", "Advertised Until", "Freshness", "Source",
    "Original Board", "Match", "Match Score", "URL", "Last Verified",
]
EMPLOYER_COLUMNS = [
    "Province", "Community", "Program", "Employer", "Operating Name", "Status", "Priority Sector",
    "Active Jobs", "Jobs Needing Review", "Website", "Careers Page", "ATS",
    "Careers Page Changed", "Website Problem", "First Seen", "Last Seen",
]
SUMMARY_COLUMNS = [
    "Province", "Community", "Program", "Designated Employers", "Hiring", "Not Hiring",
    "Employers With Active Jobs", "Active Jobs", "French/Bilingual Jobs", "Stale/Unverified Jobs",
    "Jobs Needing Review",
]


@dataclass
class Report:
    filter: ReportFilter
    generated_at: datetime
    jobs: list[dict] = field(default_factory=list)
    employers: list[dict] = field(default_factory=list)
    summary: list[dict] = field(default_factory=list)


def _date(dt: datetime | None) -> str:
    return dt.date().isoformat() if dt else ""


def _lang_ok(lang: str | None, wanted: str) -> bool:
    return wanted == "all" or (lang in _LANG_OK[wanted])


def build_report(session: Session, flt: ReportFilter) -> Report:
    report = Report(flt, utcnow())
    programs = flt.programs

    # Designations in scope: (employer_id, community_id) -> [(program, status, sector)]
    q = (select(EmployerCommunity, CommunityProgram.program, Community, Employer)
         .join(CommunityProgram, CommunityProgram.id == EmployerCommunity.community_program_id)
         .join(Community, Community.id == CommunityProgram.community_id)
         .join(Employer, Employer.id == EmployerCommunity.employer_id)
         .where(CommunityProgram.program.in_(programs)))
    if flt.community:
        q = q.where(Community.slug == flt.community)
    designations: dict[tuple[int, int], list[tuple[Program, DesignationStatus, str | None]]] = defaultdict(list)
    designation_rows = session.execute(q).all()
    for link, program, community, employer in designation_rows:
        designations[(employer.id, community.id)].append((program, link.status, link.priority_sector))

    # Jobs
    statuses = [MatchStatus.MATCHED] + ([MatchStatus.NEEDS_REVIEW] if flt.include_review else [])
    jq = (select(JobPosting, Employer, Community)
          .join(Employer, Employer.id == JobPosting.employer_id)
          .join(Community, Community.id == JobPosting.community_id)
          .where(JobPosting.is_active.is_(True), JobPosting.match_status.in_(statuses)))
    if flt.community:
        jq = jq.where(Community.slug == flt.community)
    # (employer, community) -> (matched ids, review ids, french/bilingual matched ids,
    # stale/unverified matched ids). Sets, so totals count distinct postings even
    # when an employer is designated under both pilots.
    job_ids: dict[tuple[int, int], tuple[set, set, set, set]] = defaultdict(
        lambda: (set(), set(), set(), set()))
    for post, employer, community in session.execute(jq).all():
        des = [d for d in designations.get((employer.id, community.id), [])
               if d[1] != DesignationStatus.REVOKED]
        if not des:
            continue  # employer not designated under the requested program(s) here
        fresh = assess_posting(post, now=report.generated_at, stale_days=flt.stale_days)
        if fresh.flag == "expired":
            continue  # past "Advertised until" even if no sync has closed it yet
        if flt.max_age is not None and fresh.days_up is not None and fresh.days_up > flt.max_age:
            continue
        matched, review, french, stale = job_ids[(employer.id, community.id)]
        if post.match_status == MatchStatus.MATCHED:
            matched.add(post.id)
            if post.language_req in _LANG_OK["fr"]:
                french.add(post.id)
            if fresh.flag in ("stale", "unverified"):
                stale.add(post.id)
        else:
            review.add(post.id)
        if not _lang_ok(post.language_req, flt.lang):
            continue
        report.jobs.append({
            "Province": community.province,
            "Community": community.name,
            "Programs": "/".join(sorted({d[0].value for d in des})),
            "Employer": employer.operating_name or employer.canonical_name,
            "Designation Status": "not hiring" if all(d[1] == DesignationStatus.NOT_HIRING for d in des) else "active",
            "Priority Sector": next((d[2] for d in des if d[2]), ""),
            "Job Title": post.title,
            "NOC": post.noc_code or "",
            "TEER": "" if post.teer_category is None else post.teer_category,
            "Language": post.language_req or "",
            "Employment Type": post.employment_type or "",
            "Employment Term": post.employment_term or "",
            "City": post.city or "",
            "Wage": post.wage_raw or "",
            "Vacancies": post.vacancies or "",
            "Posted": _date(post.posted_at or post.first_seen_at),
            "Days Up": "" if fresh.days_up is None else fresh.days_up,
            "Advertised Until": _date(post.expires_at),
            "Freshness": fresh.flag,
            "Source": post.source.value,
            "Original Board": post.source_detail or "",
            "Match": post.match_status.value,
            "Match Score": post.match_score or "",
            "URL": post.url or "",
            "Last Verified": _date(post.last_seen_at),
        })
    report.jobs.sort(key=lambda r: (r["Province"], r["Community"], r["Employer"].lower(),
                                    r["Posted"] or "0"), reverse=False)

    # Employers directory (one row per designation)
    summary: dict[tuple[str, str, str], dict] = {}
    totals_ids: tuple[set, set, set, set] = (set(), set(), set(), set())
    empty: tuple[set, set, set, set] = (set(), set(), set(), set())
    for link, program, community, employer in designation_rows:
        ids = job_ids.get((employer.id, community.id), empty)
        active, review, french, stale = (len(x) for x in ids)
        report.employers.append({
            "Province": community.province,
            "Community": community.name,
            "Program": program.value,
            "Employer": employer.canonical_name,
            "Operating Name": employer.operating_name or "",
            "Status": link.status.value,
            "Priority Sector": link.priority_sector or "",
            "Active Jobs": active,
            "Jobs Needing Review": review,
            "Website": employer.website_url or "",
            "Careers Page": employer.careers_url or "",
            "ATS": employer.ats_type or "",
            "Careers Page Changed": "yes" if employer.needs_manual_review else "",
            "Website Problem": employer.website_error or "",
            "First Seen": _date(employer.first_seen_at),
            "Last Seen": _date(link.last_seen_at),
        })
        key = (community.province, community.name, program.value)
        s = summary.setdefault(key, {c: 0 for c in SUMMARY_COLUMNS[3:]})
        if link.status == DesignationStatus.REVOKED:
            continue
        s["Designated Employers"] += 1
        s["Hiring" if link.status == DesignationStatus.ACTIVE else "Not Hiring"] += 1
        if active:
            s["Employers With Active Jobs"] += 1
        s["Active Jobs"] += active
        s["French/Bilingual Jobs"] += french
        s["Stale/Unverified Jobs"] += stale
        s["Jobs Needing Review"] += review
        for total_set, these in zip(totals_ids, ids):
            total_set.update(these)
    report.employers.sort(key=lambda r: (r["Province"], r["Community"], r["Program"], r["Employer"].lower()))

    for (prov, name, program), values in sorted(summary.items()):
        report.summary.append({"Province": prov, "Community": name, "Program": program, **values})
    if report.summary:
        total = {"Province": "", "Community": "TOTAL", "Program": ""}
        for col in SUMMARY_COLUMNS[3:]:
            total[col] = sum(r[col] for r in report.summary)
        # Postings are distinct in the total (a dual-designated employer's job
        # appears under both FCIP and RCIP rows above).
        (total["Active Jobs"], total["Jobs Needing Review"], total["French/Bilingual Jobs"],
         total["Stale/Unverified Jobs"]) = (len(x) for x in (totals_ids[0], totals_ids[1],
                                                             totals_ids[2], totals_ids[3]))
        report.summary.append(total)
    return report


# --------------------------------------------------------------------------- #
# Writers
# --------------------------------------------------------------------------- #
def _out_path(report: Report, ext: str, output_dir: Path | None) -> Path:
    directory = Path(output_dir or EXPORT_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = report.generated_at.strftime("%Y%m%d-%H%M%S")
    return directory / f"immigration_jobs_{report.filter.slug()}_{stamp}.{ext}"


def write_csv(report: Report, output_dir: Path | None = None) -> Path:
    path = _out_path(report, "csv", output_dir)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=JOB_COLUMNS)
        writer.writeheader()
        writer.writerows(report.jobs)
    return path


def write_xlsx(report: Report, output_dir: Path | None = None) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    path = _out_path(report, "xlsx", output_dir)
    wb = Workbook()
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="1F4E79")
    link_font = Font(color="0563C1", underline="single")
    url_columns = {"URL", "Website", "Careers Page"}
    widths_cap = {"URL": 45, "Website": 40, "Careers Page": 40, "Job Title": 45, "Employer": 45}

    def sheet(ws, title: str, columns: list[str], rows: list[dict]):
        ws.title = title
        ws.append(columns)
        for cell in ws[1]:
            cell.font, cell.fill = header_font, header_fill
            cell.alignment = Alignment(vertical="center", wrap_text=True)
        for row in rows:
            ws.append([row.get(c, "") for c in columns])
        for idx, col in enumerate(columns, start=1):
            letter = get_column_letter(idx)
            if col in url_columns:
                for cell in ws[letter][1:]:
                    if isinstance(cell.value, str) and cell.value.startswith("http"):
                        cell.hyperlink = cell.value
                        cell.font = link_font
            longest = max([len(str(col))] + [len(str(r.get(col, ""))) for r in rows[:2000]])
            ws.column_dimensions[letter].width = min(max(10, longest + 2), widths_cap.get(col, 35))
        ws.freeze_panes = "A2"
        if rows:
            ws.auto_filter.ref = f"A1:{get_column_letter(len(columns))}{len(rows) + 1}"
        return ws

    sheet(wb.active, "Active Job Postings", JOB_COLUMNS, report.jobs)
    sheet(wb.create_sheet(), "Designated Employers", EMPLOYER_COLUMNS, report.employers)
    summary_ws = sheet(wb.create_sheet(), "Summary", SUMMARY_COLUMNS, report.summary)
    if report.summary:
        for cell in summary_ws[summary_ws.max_row]:
            cell.font = Font(bold=True)
    info = wb.create_sheet("About")
    for line in (["Immigration Jobs Report"], ["Generated (UTC)", report.generated_at.strftime("%Y-%m-%d %H:%M")],
                 ["Filters", report.filter.describe()], ["Active postings", len(report.jobs)],
                 ["Designations", len(report.employers)],
                 ["Note", "Postings are linked to IRCC FCIP/RCIP designated employers by deterministic "
                          "name + location matching. Verify designation and eligibility with the community."]):
        info.append(line)
    info["A1"].font = Font(bold=True, size=14)
    info.column_dimensions["A"].width, info.column_dimensions["B"].width = 18, 110
    wb.save(path)
    return path


_PDF_SAFE = re.compile(r"[^\x20-\x7E -ÿ–—‘’“”…€]")


def _pdf_text(value) -> str:
    """Escape for reportlab Paragraph markup and drop glyphs Helvetica can't render."""
    text = _PDF_SAFE.sub("", "" if value is None else str(value))
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def write_pdf(report: Report, output_dir: Path | None = None) -> Path:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import landscape, letter
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import (KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table,
                                    TableStyle)

    path = _out_path(report, "pdf", output_dir)
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Title"], fontSize=18, spaceAfter=4)
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], textColor=colors.HexColor("#1F4E79"), spaceBefore=10,
                        keepWithNext=1)
    h3 = ParagraphStyle("h3", parent=styles["Heading3"], spaceBefore=8, spaceAfter=2, keepWithNext=1)
    h4 = ParagraphStyle("h4", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=9.5,
                        spaceBefore=6, spaceAfter=2, keepWithNext=1)
    small = ParagraphStyle("small", parent=styles["Normal"], fontSize=8, leading=10)
    meta = ParagraphStyle("meta", parent=styles["Normal"], fontSize=9, textColor=colors.HexColor("#555555"))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.HexColor("#777777"))
        canvas.drawString(0.5 * inch, 0.35 * inch,
                          f"FCIP/RCIP designated-employer jobs - {report.filter.describe()}")
        canvas.drawRightString(10.5 * inch, 0.35 * inch, f"Page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(str(path), pagesize=landscape(letter), leftMargin=0.5 * inch,
                            rightMargin=0.5 * inch, topMargin=0.5 * inch, bottomMargin=0.6 * inch,
                            title="Immigration Jobs Report", author="immigration_jobs")
    story = [
        Paragraph("Designated-Employer Job Report", h1),
        Paragraph(_pdf_text(f"{report.filter.describe()} | Generated "
                            f"{report.generated_at:%Y-%m-%d %H:%M} UTC | "
                            f"{len(report.jobs)} active postings"), meta),
        Spacer(1, 8),
    ]

    # Summary table
    if report.summary:
        cols = ["Province", "Community", "Program", "Designated Employers", "Hiring",
                "Employers With Active Jobs", "Active Jobs", "French/Bilingual Jobs",
                "Stale/Unverified Jobs"]
        data = [[Paragraph(f"<b>{c}</b>", small) for c in cols]]
        data += [[Paragraph(_pdf_text(r[c]), small) for c in cols] for r in report.summary]
        t = Table(data, repeatRows=1, colWidths=[0.7 * inch, 2.3 * inch] + [0.95 * inch] * 7)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#DCE6F1")),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#BBBBBB")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F5F8FB")]),
            ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        story += [Paragraph("Summary", h2), t]

    # Province -> Community -> Employer
    grouped: dict[str, dict[str, dict[str, list[dict]]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for row in report.jobs:
        grouped[row["Province"]][row["Community"]][row["Employer"]].append(row)

    col_widths = [2.9 * inch, 0.85 * inch, 1.0 * inch, 1.25 * inch, 1.4 * inch, 1.1 * inch, 1.5 * inch]
    header = ["Job title", "NOC / TEER", "Language", "City", "Posted (days up)", "Source", "Link"]
    flag_colors = {"stale": "#B45F06", "unverified": "#B45F06", "expiring soon": "#7F6000"}
    for prov in sorted(grouped):
        story.append(Paragraph(_pdf_text(PROVINCE_NAMES.get(prov, prov)), h2))
        for community in sorted(grouped[prov]):
            employers = grouped[prov][community]
            n = sum(len(v) for v in employers.values())
            story.append(Paragraph(_pdf_text(f"{community} - {n} posting{'s' if n != 1 else ''}, "
                                             f"{len(employers)} employer{'s' if len(employers) != 1 else ''}"), h3))
            for employer in sorted(employers, key=str.lower):
                rows = employers[employer]
                first = rows[0]
                label = f"{employer}  ({first['Programs']}"
                label += f", {first['Priority Sector']})" if first["Priority Sector"] else ")"
                data = [[Paragraph(f"<b>{h}</b>", small) for h in header]]
                for r in rows:
                    noc = f"{r['NOC']} / {r['TEER']}" if r["NOC"] else ""
                    src = r["Source"].replace("_", " ") + (f" ({r['Original Board']})" if r["Original Board"] else "")
                    link = (f'<link href="{_pdf_text(r["URL"])}" color="#0563C1">view posting</link>'
                            if r["URL"] else "")
                    title = _pdf_text(r["Job Title"]) + (" <i>(review)</i>" if r["Match"] != "matched" else "")
                    if r["Freshness"] in flag_colors:
                        title += f' <font color="{flag_colors[r["Freshness"]]}"><b>[{r["Freshness"]}]</b></font>'
                    posted = r["Posted"] + (f" ({r['Days Up']} d)" if r["Days Up"] != "" else "")
                    if r["Advertised Until"]:
                        posted += f"<br/>until {r['Advertised Until']}"
                    data.append([Paragraph(title, small), Paragraph(_pdf_text(noc), small),
                                 Paragraph(_pdf_text(r["Language"]), small), Paragraph(_pdf_text(r["City"]), small),
                                 Paragraph(posted, small), Paragraph(_pdf_text(src), small),
                                 Paragraph(link, small)])
                t = Table(data, colWidths=col_widths, repeatRows=1)
                t.setStyle(TableStyle([
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EEF2F6")),
                    ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#DDDDDD")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                ]))
                story.append(KeepTogether([Paragraph(_pdf_text(label), h4), t]) if len(rows) < 15
                             else Paragraph(_pdf_text(label), h4))
                if len(rows) >= 15:
                    story.append(t)
    if not report.jobs:
        story.append(Paragraph("No active postings match these filters.", styles["Normal"]))

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return path


WRITERS = {"csv": write_csv, "xlsx": write_xlsx, "pdf": write_pdf}


def export(session: Session, formats: list[str], flt: ReportFilter,
           output_dir: Path | None = None) -> list[Path]:
    unknown = set(formats) - set(FORMATS)
    if unknown:
        raise ValueError(f"unknown format(s): {', '.join(sorted(unknown))}")
    report = build_report(session, flt)
    return [WRITERS[f](report, output_dir) for f in formats]
