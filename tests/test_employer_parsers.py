import asyncio
import io

import pytest
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Table
from reportlab.pdfgen import canvas

from config.communities import EmployerSource, Program, SourceType, get_community
from scrapers.employers.common import map_header, normalize_url, postprocess
from scrapers.employers.csv_seed import CsvSeedScraper, parse_csv_employers
from scrapers.employers.html_table import HtmlTableScraper, parse_html_employers
from scrapers.employers.pdf_list import parse_pdf_employers

BASE = "https://example-community.ca/rcip/employers"

TABLE_HTML = """
<html><body>
<nav><a href="/">Home</a><table><tr><th>Employer</th></tr><tr><td>Nav Junk</td></tr></table></nav>
<h1>Designated Employers</h1>
<table class="employers">
  <thead><tr><th>Employer Name</th><th>Industry / Sector</th><th>Website</th></tr></thead>
  <tbody>
    <tr><td>Vale Canada Limited</td><td>Mining</td><td><a href="https://www.vale.com">vale.com</a></td></tr>
    <tr><td>Boulangerie Côté Ltée</td><td>Food</td><td>boulangeriecote.ca</td></tr>
    <tr><td>  1234567 Ontario Inc. o/a Tim Hortons </td><td>Food Services</td><td></td></tr>
    <tr><td>Page 2</td><td></td><td></td></tr>
    <tr><td>Vale Canada Limited</td><td>Mining</td><td></td></tr>
  </tbody>
</table>
<footer><table><tr><th>Business</th></tr><tr><td>Footer Junk</td></tr></table></footer>
</body></html>
"""


def test_map_header():
    assert map_header(["Employer Name", "Sector", "Website"]) == {
        "name": 0, "priority_sector": 1, "website_url": 2}
    assert map_header(["Nom de l'entreprise", "Secteur", "Site Web"]) == {
        "name": 0, "priority_sector": 1, "website_url": 2}
    assert map_header(["Foo", "Bar"]) == {}


def test_normalize_url():
    assert normalize_url("vale.com") == "https://vale.com"
    assert normalize_url("/x", BASE) == "https://example-community.ca/x"
    assert normalize_url("mailto:a@b.ca") is None
    assert normalize_url("n/a") is None


def test_html_auto_table():
    rows = postprocess(parse_html_employers(TABLE_HTML, base_url=BASE), {})
    names = [r.name for r in rows]
    assert names[:3] == ["Vale Canada Limited", "Boulangerie Côté Ltée",
                         "1234567 Ontario Inc. o/a Tim Hortons"]
    assert "Page 2" not in names and "Nav Junk" not in names and "Footer Junk" not in names
    assert rows[0].website_url == "https://www.vale.com"
    assert rows[0].priority_sector == "Natural resources, agriculture and related production"
    assert rows[1].website_url == "https://boulangeriecote.ca"


def test_html_explicit_fields():
    html = """<div class="grid">
      <div class="card"><h3>Kal Tire</h3><span class="sec">Automotive</span>
        <a href="https://kaltire.com">Visit</a></div>
      <div class="card"><h3>Northern Heating</h3><span class="sec">Trades</span></div>
    </div>"""
    rows = parse_html_employers(html, {
        "row_selector": ".card",
        "fields": {"name": "h3", "priority_sector": ".sec", "website_url": "a@href"},
    }, BASE)
    assert [(r.name, r.priority_sector, r.website_url) for r in rows] == [
        ("Kal Tire", "Automotive", "https://kaltire.com"),
        ("Northern Heating", "Trades", None),
    ]


def test_html_list_items_skip_internal_links():
    html = """<ul class="employers">
      <li><a href="https://acme.ca">ACME Ltd.</a></li>
      <li><a href="/employers/beta">Beta Corp</a></li>
      <li>• Gamma Inc.</li></ul>"""
    rows = parse_html_employers(html, {"item_selector": "ul.employers li"}, BASE)
    assert [(r.name, r.website_url) for r in rows] == [
        ("ACME Ltd.", "https://acme.ca"), ("Beta Corp", None), ("Gamma Inc.", None)]


def _table_pdf(rows: list[list[str]], split_at: int | None = None) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter)
    from reportlab.platypus import PageBreak
    story = []
    if split_at:
        story += [Table(rows[:split_at], style=[("GRID", (0, 0), (-1, -1), 0.5, "black")]),
                  PageBreak(),
                  Table(rows[split_at:], style=[("GRID", (0, 0), (-1, -1), 0.5, "black")])]
    else:
        story.append(Table(rows, style=[("GRID", (0, 0), (-1, -1), 0.5, "black")]))
    doc.build(story)
    return buf.getvalue()


def test_pdf_tables_across_pages():
    rows = [["Business Name", "Sector"], ["Vale Canada Limited", "Mining"],
            ["Kal Tire Ltd.", "Automotive"], ["Gamma Inc.", "Retail"]]
    data = _table_pdf(rows, split_at=3)  # page 2 continues without header
    parsed = parse_pdf_employers(data)
    assert [(r.name, r.priority_sector) for r in parsed] == [
        ("Vale Canada Limited", "Mining"), ("Kal Tire Ltd.", "Automotive"), ("Gamma Inc.", "Retail")]
    # canonicalisation happens in postprocess
    assert postprocess(parsed, {})[0].priority_sector == "Natural resources, agriculture and related production"


def test_pdf_line_mode_with_regex():
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    y = 750
    for line in ["Designated Employers", "Updated March 2026", "ACME Ltd. - Manufacturing",
                 "Northern Heating - Trades", "Page 1 of 1"]:
        c.drawString(72, y, line)
        y -= 20
    c.save()
    plain = postprocess(parse_pdf_employers(buf.getvalue(), {"mode": "lines"}), {})
    assert [r.name for r in plain] == ["ACME Ltd. - Manufacturing", "Northern Heating - Trades"]
    opts = {"mode": "lines", "item_regex": r"^(?P<name>.+?) - (?P<priority_sector>.+)$"}
    split = postprocess(parse_pdf_employers(buf.getvalue(), opts), opts)
    assert [(r.name, r.priority_sector) for r in split] == [
        ("ACME Ltd.", "Manufacturing and utilities"),
        ("Northern Heating", "Trades, transport and equipment operators")]


def test_csv_seed(tmp_path):
    path = tmp_path / "sudbury.csv"
    path.write_text("name,program,website_url,priority_sector\n"
                    "Vale Canada Limited,RCIP,vale.com,Mining\n"
                    "Collège Boréal,FCIP,,Education\n"
                    "Both Pilots Inc.,,,\n"
                    "Bad Program Co,XYZ,,\n", encoding="utf-8-sig")
    assert len(parse_csv_employers(path)) == 4

    scraper = CsvSeedScraper(get_community("sudbury"), EmployerSource(SourceType.CSV, url=str(path)),
                             http=None)
    records = asyncio.run(scraper.scrape())
    got = sorted((r.name, r.program.value) for r in records)
    assert got == [("Both Pilots Inc.", "FCIP"), ("Both Pilots Inc.", "RCIP"),
                   ("Collège Boréal", "FCIP"), ("Vale Canada Limited", "RCIP")]
    assert next(r for r in records if r.name.startswith("Vale")).website_url.host == "vale.com"


def test_scraper_program_scoping():
    """A program-specific source on a dual-program community emits only that program."""
    class FakeHttp:
        async def get_text(self, url):
            return TABLE_HTML

    src = EmployerSource(SourceType.HTML, url=BASE, program=Program.FCIP)
    records = asyncio.run(HtmlTableScraper(get_community("timmins"), src, FakeHttp()).scrape())
    assert records and {r.program for r in records} == {Program.FCIP}
    assert len(records) == 3  # duplicate Vale row removed
