import asyncio

import pytest
from sqlalchemy import select

from config.communities import Program
from core.employer_sync import clean_employer_websites, set_website, sync_designations
from core.models import Employer
from core.schemas import EmployerIn
from core.urls import clean_website
from scrapers.base import ScrapeError
from scrapers.jobs.career_site import CareerSiteScraper, EmployerTarget, classify_error


# Bad "websites" seen in the 2026-11-26 career-site run
@pytest.mark.parametrize("raw, expected", [
    ("https://http//www.thesocialsoulpreneur.com", "http://www.thesocialsoulpreneur.com/"),
    ("http//acme.ca", "http://acme.ca/"),
    ("vale.com", "https://vale.com/"),
    ("HTTPS://WWW.Acme.CA", "https://www.acme.ca/"),
    ("https://705painting.ca/home-8623#form-cZq81BpDqR", "https://705painting.ca/home-8623"),
    ("https://x.ca/p?countryRedir=1&utm_source=google&utm_medium=organic", "https://x.ca/p?countryRedir=1"),
    ("http://www.walfordonthepark/", None),                                   # no TLD
    ("https://investsudbury.ca/cdn-cgi/l/email-protection#9af4fe", None),      # Cloudflare email link
    ("https://www.facebook.com/share/1CJNKD3vWx/?mibextid=wwXIfr", None),
    ("https://www.linkedin.com/company/codalio/jobs", None),
    ("https://sudburychamber.ca/membershipdirectory/#/action/Listing/value/142756", None),
    ("mailto:jobs@acme.ca", None),
    ("n/a", None),
    (None, None),
])
def test_clean_website(raw, expected):
    assert clean_website(raw) == expected


def test_exclude_list_site():
    assert clean_website("https://investsudbury.ca/x", exclude_site="https://investsudbury.ca/rcip") is None
    assert clean_website("https://acme.ca", exclude_site="https://investsudbury.ca/rcip") == "https://acme.ca/"


def test_set_website_and_cleanup(seeded_session):
    s = seeded_session
    sync_designations(s, "sudbury", Program.RCIP, [EmployerIn(name="Acme", community_slug="sudbury",
                                                              program=Program.RCIP)])
    emp = s.scalar(select(Employer))
    emp.website_url = "https://investsudbury.ca/cdn-cgi/l/email-protection#abc"   # legacy bad value
    emp.website_error = "not_found: HTTP 404"
    assert clean_employer_websites(s) == {"fixed": 0, "removed": 1}
    assert emp.website_url is None and emp.website_error is None
    assert set_website(emp, "https://www.facebook.com/acme") is False
    assert set_website(emp, "acme.ca") is True and emp.website_url == "https://acme.ca/"
    assert set_website(emp, "https://other.ca") is False          # never overwrite a good URL


@pytest.mark.parametrize("err, kind", [
    (ScrapeError("HTTP 403 for https://x.ca/", status_code=403), "blocked"),
    (ScrapeError("HTTP 404 for https://x.ca/", status_code=404), "not_found"),
    (ScrapeError("HTTP 429 for https://x.ca/", status_code=429), "rate_limited"),
    (ScrapeError("HTTP 502 for https://x.ca/", status_code=502), "server"),
    (ScrapeError("robots.txt disallows https://x.ca/"), "robots"),
    (ScrapeError("ConnectError fetching https://x.ca/: [Errno 11001] getaddrinfo failed"), "dns"),
    (ScrapeError("ConnectError fetching https://x.ca/: [SSL: SSLV3_ALERT_HANDSHAKE_FAILURE]"), "tls"),
    (ScrapeError("ConnectError fetching https://x.ca/: (\"The certificate's CN name does not match\")"), "tls"),
    (ScrapeError("ConnectError fetching https://x.ca/: All connection attempts failed"), "network"),
])
def test_classify_error(err, kind):
    assert classify_error(err) == kind


class FlakyHttp:
    def __init__(self, pages, tls_fail=(), blocked=()):
        self.pages, self.tls_fail, self.blocked = pages, set(tls_fail), set(blocked)
        self.calls = []

    async def get_text(self, url, **kw):
        self.calls.append(url)
        if url in self.tls_fail:
            raise ScrapeError(f"ConnectError fetching {url}: [SSL: WRONG_VERSION_NUMBER] wrong version number")
        if url in self.blocked:
            raise ScrapeError(f"HTTP 403 for {url}", status_code=403)
        if url not in self.pages:
            raise ScrapeError(f"HTTP 404 for {url}", status_code=404)
        return self.pages[url]


def target(eid=1, site="https://acme.ca/", careers=None, h=None):
    return EmployerTarget(eid, "Acme", site, careers, h)


def test_http_fallback_after_tls_failure():
    http = FlakyHttp({"http://acme.ca/": '<a href="/careers">Careers</a>',
                      "https://acme.ca/careers": "<main>Join us</main>"},
                     tls_fail={"https://acme.ca/"})
    res = asyncio.run(CareerSiteScraper(http).scrape(target()))
    assert res.error is None and res.tier == "hash" and res.careers_url == "https://acme.ca/careers"
    assert "http://acme.ca/" in http.calls


def test_failures_memoised_per_run():
    http = FlakyHttp({}, blocked={"https://group.ca/"})
    scraper = CareerSiteScraper(http)

    async def go():
        return [await scraper.scrape(target(i, "https://group.ca/")) for i in range(4)]

    results = asyncio.run(go())
    assert all(r.error_kind == "blocked" for r in results)
    assert http.calls.count("https://group.ca/") == 1


def test_homepage_is_not_hashed():
    http = FlakyHttp({"https://acme.ca/": "<main>Spring sale! Visit our store.</main>"})
    res = asyncio.run(CareerSiteScraper(http).scrape(target()))
    assert res.tier == "no_careers_page" and res.page_hash is None and not res.changed
