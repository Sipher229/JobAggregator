"""Golden tests: each community's configured parser against its real saved page.

Fixtures were captured 2026-11-26. When a community republishes its list, refresh
the fixture and these expectations together.
"""

from collections import Counter

import pytest

from config.communities import Program
from scrapers.employers.common import canonical_sector, finalize, parse_programs, RawEmployer
from tests.fixture_http import has_fixture, scrape_fixture

# slug: (expected counts per program (min, max), names that must be present)
EXPECTED = {
    "acadian-peninsula": ({"FCIP": (30, 45)}, ["Dixie Lee", "Université de Moncton Campus de Shippagan"]),
    "superior-east": ({"FCIP": (15, 25)}, ["Subway Wawa", "Pepco Corporation"]),
    "st-pierre-jolys": ({"FCIP": (6, 6)}, ["Boulangerie St Pierre Bakery",
                                           "Division Scolaire Franco-manitobaine – ECRB"]),
    "kelowna": ({"FCIP": (50, 60)}, ["Acera Insurance", "Westwood Cabinetry"]),
    "sudbury": ({"RCIP": (360, 380), "FCIP": (95, 110)}, ["Baker Tilly SNT LLP", "7055579 Canada Inc."]),
    "timmins": ({"RCIP": (160, 175), "FCIP": (33, 42)},
                ["Corporation of the Town of Kirkland Lake – Teck Pioneer Residence", "Caisse Alliance",
                 "YMCA of Northeastern Ontario", "The Bucket Shop"]),
    "north-bay": ({"RCIP": (250, 270)}, ["Beyond Blue Docks"]),
    "pictou-county": ({"RCIP": (60, 75)}, ["BKR Management Inc. (Burger King)", "Nova Scotia Health Authority"]),
    "steinbach": ({"RCIP": (45, 55)}, ["HyLife Ltd.", "WS Steel", "Barnala Driver Training Academy"]),
    "sault-ste-marie": ({"RCIP": (140, 160)}, ["ALGOMA TUBES INC (TENARIS CANADA)", "SHOGUN SUSHI"]),
    "altona-rhineland": ({"RCIP": (20, 26)}, ["Buffalo Creek Mills (2017) Inc."]),
    "brandon": ({"RCIP": (38, 46)}, ["Prairie Mountain Health (PMH)", "Glendale Industries Limited"]),
    "moose-jaw": ({"RCIP": (100, 170)}, ["Boston Pizza", "Briercrest College"]),
    "claresholm": ({"RCIP": (24, 24)}, ["Structural Precast", "Tim Hortons"]),
    "west-kootenay": ({"RCIP": (280, 310)}, ["Agilion Accounting & Advisory", "Doane Grant Thornton LLP"]),
    "north-okanagan-shuswap": ({"RCIP": (470, 510)}, ["0738736 BC Ltd.", "Wood Creek Construction Ltd."]),
}


@pytest.fixture(scope="module")
def parsed():
    return {slug: scrape_fixture(slug) for slug in EXPECTED if has_fixture(slug)}


@pytest.mark.parametrize("slug", sorted(EXPECTED))
def test_counts_and_known_names(parsed, slug):
    if slug not in parsed:
        pytest.skip("fixture missing")
    counts_expected, names = EXPECTED[slug]
    records = parsed[slug]
    counts = Counter(r.program.value for r in records)
    for program, (lo, hi) in counts_expected.items():
        assert lo <= counts[program] <= hi, f"{slug} {program}: {counts[program]}"
    assert set(counts) == set(counts_expected)
    got = {r.name for r in records}
    for name in names:
        assert name in got, f"{slug}: missing {name!r}"


def test_no_junk_names(parsed):
    junk = ("updated", "page ", "please", "priority sector", "designated employer", "nb :",
            "further notice", "employer name")
    for slug, records in parsed.items():
        for r in records:
            assert not any(j in r.name.lower() for j in junk), (slug, r.name)
            assert "not hiring" not in r.name.lower() and "ne recrute" not in r.name.lower()


def test_status_and_fields(parsed):
    by = {s: {r.name: r for r in recs} for s, recs in parsed.items()}
    assert by["kelowna"]["Acera Insurance"].not_hiring
    assert by["claresholm"]["El Molino"].not_hiring and not by["claresholm"]["Structural Precast"].not_hiring
    assert by["claresholm"]["Structural Precast"].priority_sector == "Manufacturing and utilities"
    assert by["brandon"]["Glendale Industries Limited"].not_hiring
    assert by["acadian-peninsula"]["Dixie Lee"].location
    assert by["west-kootenay"]["Agilion Accounting & Advisory"].location == "Nelson"
    assert by["sudbury"]["Baker Tilly SNT LLP"].website_url.host == "www.bakertilly.ca"
    nb = Counter(r.not_hiring for r in parsed["north-bay"])
    assert nb[True] > 200 and nb[False] >= 1


def test_timmins_program_split(parsed):
    recs = parsed["timmins"]
    fcip = {r.name for r in recs if r.program is Program.FCIP}
    rcip = {r.name for r in recs if r.program is Program.RCIP}
    assert "Extendicare (Timmins)" in fcip and "Extendicare (Timmins)" in rcip  # RCIP/FCIP
    assert "Columbia Forest Products" in fcip and "Columbia Forest Products" not in rcip
    assert "Anson General Hospital" in rcip and "Anson General Hospital" not in fcip


def test_sector_canonicalisation():
    assert canonical_sector("Education, Law and Social, Community") == canonical_sector(
        "and Government Services")
    assert canonical_sector("Trades and") == "Trades, transport and equipment operators"
    assert canonical_sector("6, Sales & Services") == "Sales and service"
    assert canonical_sector("Social, Community and Government Services").startswith("Education")
    assert canonical_sector("Widgets") is None


def test_finalize_annotations():
    cases = {
        "Foyer des Pionniers [not hiring]": ("Foyer des Pionniers", True),
        "Pepco Corporation - (Not Hiring at this time)": ("Pepco Corporation", True),
        "Access Credit Union Ltd. (Altona) – Not Currently Hiring": ("Access Credit Union Ltd. (Altona)", True),
        "Everest Plastik (ne recrute pas pour le moment)": ("Everest Plastik", True),
        "SHOGUN SUSHI(2025)": ("SHOGUN SUSHI", False),
        "Buffalo Creek Mills (2017) Inc.": ("Buffalo Creek Mills (2017) Inc.", False),
        "A&W Restaurant*": ("A&W Restaurant", False),
    }
    for raw, (name, nh) in cases.items():
        r = finalize(RawEmployer(name=raw))
        assert (r.name, r.not_hiring) == (name, nh), raw
    assert finalize(RawEmployer(name="A&W*")).flags == ["asterisk"]
    assert finalize(RawEmployer(name="X Co", status="Not Hiring")).not_hiring
    assert not finalize(RawEmployer(name="X Co", status="Hiring")).not_hiring


def test_parse_programs():
    assert parse_programs("RCIP/FCIP") == (Program.RCIP, Program.FCIP)
    assert parse_programs("PPICF") == (Program.FCIP,)
    assert parse_programs("n/a") == ()
