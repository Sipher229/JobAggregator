import asyncio
import json
from pathlib import Path

import pytest

from config.communities import get_community
from scrapers.base import ScrapeError
from scrapers.jobs.job_bank import (
    JobBankScraper,
    apply_detail,
    has_more_results,
    normalize_language,
    parse_posting,
    parse_result_count,
    parse_search_results,
    split_location,
    teer_from_noc,
)

FIX = Path(__file__).parent / "fixtures" / "jobs"
SEARCH = (FIX / "jobbank-search-timmins.html").read_text(encoding="utf-8")
LOADER = (FIX / "jobbank-loader.html").read_text(encoding="utf-8")
DETAIL = (FIX / "jobbank-detail.html").read_text(encoding="utf-8")


def test_parse_search_page():
    jobs = parse_search_results(SEARCH)
    assert len(jobs) == 25
    assert parse_result_count(SEARCH) == 109
    assert has_more_results(SEARCH)
    first = jobs[0]
    assert (first.external_id, first.title, first.employer_name) == (
        "50802546", "auto detailer", "Enterprise Rent A Car")
    assert (first.city, first.province) == ("Timmins", "ON")
    assert first.wage_raw == "$19.20 hourly"
    assert first.posted_at.date().isoformat() == "2026-11-25"
    assert first.url == "https://www.jobbank.gc.ca/jobsearch/jobposting/50802546"
    assert first.source_detail is None and first.job_number == "3728166"   # posted on Job Bank
    assert any(j.source_detail == "indeed.com" for j in jobs)
    assert {j.city for j in jobs} >= {"Timmins", "South Porcupine"}


def test_parse_loader_page():
    jobs = parse_search_results(LOADER)
    assert len(jobs) == 25 and has_more_results(LOADER)
    assert not {j.external_id for j in jobs} & {j.external_id for j in parse_search_results(SEARCH)}
    assert parse_search_results("") == [] and not has_more_results("")


def test_parse_posting_detail():
    d = parse_posting(DETAIL)
    assert d["expired"] is False
    assert d["noc_code"] == "73100" and d["teer_category"] == 3
    assert d["employer_name"] == "Antex Western Ltd"
    assert d["employer_website"] == "http://www.antexwestern.com"
    assert (d["city"], d["province"], d["postal_code"]) == ("Winnipeg", "MB", "R2X 1G4")
    assert (d["employment_type"], d["employment_term"]) == ("Full time", "Permanent")
    assert d["language_req"] == "English" and d["vacancies"] == 5 and d["work_mode"] == "On site"
    assert d["wage_raw"].startswith("$25.00 to $30.00 hourly")


def test_expired_posting():
    html = "<html><body><main><h1>This job posting is no longer available.</h1></main></body></html>"
    assert parse_posting(html) == {"expired": True}


def test_apply_detail_keeps_listing_wage():
    job = parse_search_results(SEARCH)[0]
    merged = apply_detail(job, parse_posting(DETAIL))
    assert merged.details_fetched and merged.noc_code == "73100"
    assert merged.wage_raw == "$19.20 hourly"           # listing salary kept
    assert merged.employer_website == "http://www.antexwestern.com"


@pytest.mark.parametrize("noc, teer", [("73100", 3), ("31301", 1), ("00012", 0), ("1234", None),
                                       ("x", None), (None, None), ("16000", None)])
def test_teer_from_noc(noc, teer):
    assert teer_from_noc(noc) == teer


def test_language_and_location_helpers():
    assert normalize_language("English") == "English"
    assert normalize_language("French") == "French"
    assert normalize_language("English and French") == "Bilingual"
    assert normalize_language("English or French") == "English or French"
    assert split_location("Location Timmins (ON)") == ("Timmins", "ON")
    assert split_location("Various locations") == (None, None)


class FakeJobBankHttp:
    """Serves the Timmins search page, then the loader page, then an empty page."""

    def __init__(self, fail_city: str | None = None):
        self.calls: list[str] = []
        self.loader_pages = [LOADER, ""]
        self.fail_city = fail_city

    async def get_text(self, url, cache=True, **kwargs):
        self.calls.append(url)
        if "cityprovsuggest" in url:
            q = url.split("q=")[1].split("&")[0].replace("+", " ")
            if self.fail_city and q.lower() == self.fail_city.lower():
                return json.dumps({"response": {"docs": []}})
            return json.dumps({"response": {"docs": [
                {"name": "Other", "province_cd": "ON", "city_id": "1"},
                {"name": q.title(), "province_cd": "ON", "city_id": "22423"},
            ]}})
        if "job_search_loader" in url:
            assert cache is False  # stateful endpoint must bypass the cache
            return self.loader_pages.pop(0) if self.loader_pages else ""
        if "jobsearch/jobsearch" in url:
            assert "locationparam=22423" in url and "d=50" in url
            return SEARCH
        raise AssertionError(url)


def test_search_pages_until_exhausted():
    http = FakeJobBankHttp()
    jobs = asyncio.run(JobBankScraper(http).search("Timmins", "ON"))
    assert len(jobs) == 50                                   # 25 + 25, then nothing new
    assert sum("job_search_loader" in c for c in http.calls) == 2


def test_search_community_continues_after_failed_centre():
    community = get_community("timmins")
    http = FakeJobBankHttp(fail_city="Kapuskasing")

    async def go():
        scraper = JobBankScraper(http)
        return await scraper.search_community(community)

    jobs, errors = asyncio.run(go())
    assert len(errors) == 1 and "Kapuskasing" in errors[0]
    assert len(jobs) == 50   # same postings from every centre are de-duplicated


def test_fetch_detail_404_is_expired():
    class Gone:
        async def get_text(self, url, **kw):
            raise ScrapeError("HTTP 404", status_code=404)

    job = parse_search_results(SEARCH)[0]
    _, expired = asyncio.run(JobBankScraper(Gone()).fetch_detail(job))
    assert expired
