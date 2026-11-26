import asyncio
import json

from scrapers.jobs.career_site import (
    CareerSiteScraper,
    EmployerTarget,
    detect_ats,
    find_careers_url,
    page_fingerprint,
    parse_ats_feed,
    parse_jsonld_jobs,
)

HOME = """<html><body><nav><a href="/about">About</a><a href="/careers">Careers</a></nav>
<p>Welcome</p><footer><a href="/jobs-old">jobs</a></footer></body></html>"""

JSONLD_PAGE = """<html><head>
<script type="application/ld+json">{"@context":"https://schema.org","@graph":[
 {"@type":"Organization","name":"Acme"},
 {"@type":"JobPosting","title":"Welder","datePosted":"2026-11-20","identifier":{"value":"W-1"},
  "url":"/careers/welder","employmentType":"FULL_TIME",
  "jobLocation":{"@type":"Place","address":{"addressLocality":"Brandon","addressRegion":"MB","postalCode":"R7A 1A1"}}}
]}</script>
<script type="application/ld+json">[{"@type":"JobPosting","title":"Cook","jobLocation":[{"address":{"addressLocality":"Brandon","addressRegion":"Manitoba"}}],}]</script>
</head><body><main>Careers</main></body></html>"""


def test_find_careers_url():
    assert find_careers_url(HOME, "https://acme.ca/") == "https://acme.ca/careers"
    ats_home = '<a href="https://jobs.lever.co/acme">Open roles</a><a href="/careers">Careers</a>'
    assert find_careers_url(ats_home, "https://acme.ca/") == "https://jobs.lever.co/acme"
    fr = '<a href="/fr/emplois">Emplois</a>'
    assert find_careers_url(fr, "https://acme.ca/") == "https://acme.ca/fr/emplois"
    assert find_careers_url("<a href='/contact'>Contact</a>", "https://acme.ca/") is None


def test_detect_ats():
    html = """
      <iframe src="https://boards.greenhouse.io/embed/job_board?for=acmeco"></iframe>
      <a href="https://acme.bamboohr.com/careers">x</a>
      <a href="https://acme.wd3.myworkdayjobs.com/External">y</a>
      <script src="https://static.smartrecruiters.com/js/widget.js"></script>"""
    boards = detect_ats(html)
    kinds = [(b.ats, b.token) for b in boards]
    assert ("greenhouse", "acmeco") in kinds and ("bamboohr", "acme") in kinds
    assert ("workday", "acme") in kinds
    assert kinds[-1][0] == "workday"            # parseable boards sorted first
    assert not any(b.ats == "smartrecruiters" for b in boards)  # static asset, not a board


def test_parse_jsonld_jobs():
    jobs = parse_jsonld_jobs(JSONLD_PAGE, "https://acme.ca/careers")
    assert [j["title"] for j in jobs] == ["Welder", "Cook"]
    welder, cook = jobs
    assert (welder["id"], welder["city"], welder["province"], welder["postal_code"]) == (
        "W-1", "Brandon", "MB", "R7A 1A1")
    assert welder["url"] == "https://acme.ca/careers/welder"
    assert welder["posted_at"].date().isoformat() == "2026-11-20"
    assert cook["province"] == "MB"            # full province name + trailing comma tolerated


def test_parse_ats_feeds():
    gh = parse_ats_feed("greenhouse", "acme", {"jobs": [
        {"id": 1, "title": "Millwright", "location": {"name": "Thunder Bay, Ontario"},
         "absolute_url": "https://boards.greenhouse.io/acme/jobs/1", "updated_at": "2026-11-01T10:00:00-04:00"}]})
    assert gh[0]["city"] == "Thunder Bay" and gh[0]["province"] == "ON"
    lever = parse_ats_feed("lever", "acme", [{"id": "abc", "text": "Driver", "hostedUrl": "https://jobs.lever.co/acme/abc",
                                              "createdAt": 1764000000000, "categories": {"location": "Vernon, BC", "commitment": "Full-time"}}])
    assert lever[0]["city"] == "Vernon" and lever[0]["province"] == "BC" and lever[0]["posted_at"].year == 2025
    sr = parse_ats_feed("smartrecruiters", "Acme", {"content": [
        {"id": "7", "name": "Nurse", "location": {"city": "Brandon", "region": "MB"}, "releasedDate": "2026-11-02T00:00:00.000Z"}]})
    assert sr[0]["url"] == "https://jobs.smartrecruiters.com/Acme/7"
    wk = parse_ats_feed("workable", "acme", {"jobs": [{"title": "Cook", "shortcode": "X1", "city": "Nelson",
                                                       "state": "British Columbia", "url": "https://apply.workable.com/j/X1"}]})
    assert wk[0]["province"] == "BC"
    bz = parse_ats_feed("breezy", "acme", [{"id": "b1", "name": "Clerk", "url": "u",
                                            "location": {"city": "Trail", "state": {"id": "BC"}}}])
    assert bz[0]["province"] == "BC"
    rc = parse_ats_feed("recruitee", "acme", {"offers": [{"id": 3, "title": "Baker", "city": "Altona",
                                                          "state_code": "MB", "careers_url": "u"}]})
    assert rc[0]["city"] == "Altona"
    bh = parse_ats_feed("bamboohr", "acme", {"result": [{"id": "12", "jobOpeningName": "Mechanic",
                                                          "location": {"city": "Steinbach", "state": "Manitoba"}}]})
    assert bh[0]["url"] == "https://acme.bamboohr.com/careers/12" and bh[0]["province"] == "MB"
    jz = parse_ats_feed("jazzhr", "acme", """<ul><li class="list-group-item">
        <h4><a href="/apply/AbC123/Line-Cook">Line Cook</a></h4>
        <ul class="list-inline"><li>Moose Jaw, SK</li></ul></li></ul>""")
    assert jz[0]["id"] == "AbC123" and jz[0]["title"] == "Line Cook"
    assert jz[0]["url"] == "https://acme.applytojob.com/apply/AbC123/Line-Cook"


def test_page_fingerprint_ignores_boilerplate_and_numbers():
    a = "<html><body><nav>Menu 1</nav><main>Join our team. Updated 2026-11-01. 3 openings</main><footer>(c) 2026</footer></body></html>"
    b = "<html><body><nav>Other menu</nav><main>Join our team. Updated 2026-11-25. 4 openings</main><footer>(c) 2027</footer></body></html>"
    c = "<html><body><main>Join our team. We are hiring a welder.</main></body></html>"
    assert page_fingerprint(a)[0] == page_fingerprint(b)[0]
    assert page_fingerprint(a)[0] != page_fingerprint(c)[0]
    assert "menu" not in page_fingerprint(a)[1]


class SiteHttp:
    def __init__(self, pages: dict[str, str]):
        self.pages = pages
        self.calls: list[str] = []

    async def get_text(self, url, **kwargs):
        self.calls.append(url)
        if url not in self.pages:
            from scrapers.base import ScrapeError
            raise ScrapeError(f"HTTP 404 for {url}", status_code=404)
        return self.pages[url]


def _target(**kw):
    base = dict(employer_id=1, name="Acme", website_url="https://acme.ca/", careers_url=None,
                careers_page_hash=None)
    base.update(kw)
    return EmployerTarget(**base)


def test_scraper_tier1_jsonld():
    http = SiteHttp({"https://acme.ca/": HOME, "https://acme.ca/careers": JSONLD_PAGE})
    res = asyncio.run(CareerSiteScraper(http).scrape(_target()))
    assert res.tier == "jsonld" and len(res.jobs) == 2 and res.careers_url == "https://acme.ca/careers"
    job = res.jobs[0]
    assert job.employer_id == 1 and job.source.value == "career_site" and job.external_id.startswith("jsonld:")


def test_scraper_tier2_ats():
    careers = '<html><body><a href="https://jobs.lever.co/acme">See openings</a></body></html>'
    feed = json.dumps([{"id": "p1", "text": "Driver", "hostedUrl": "https://jobs.lever.co/acme/p1",
                        "categories": {"location": "Brandon, MB"}}])
    http = SiteHttp({"https://acme.ca/careers": careers,
                     "https://api.lever.co/v0/postings/acme?mode=json": feed})
    res = asyncio.run(CareerSiteScraper(http).scrape(_target(careers_url="https://acme.ca/careers")))
    assert res.tier == "ats" and res.ats_type == "lever"
    assert [(j.title, j.city, j.external_id) for j in res.jobs] == [("Driver", "Brandon", "lever:acme:p1")]


def test_scraper_tier3_hash_change():
    page_v1 = "<html><body><main>No openings right now.</main></body></html>"
    page_v2 = "<html><body><main>We are hiring a heavy equipment operator!</main></body></html>"
    first = asyncio.run(CareerSiteScraper(SiteHttp({"https://acme.ca/careers": page_v1})).scrape(
        _target(careers_url="https://acme.ca/careers")))
    assert first.tier == "hash" and not first.changed and first.page_hash
    second = asyncio.run(CareerSiteScraper(SiteHttp({"https://acme.ca/careers": page_v2})).scrape(
        _target(careers_url="https://acme.ca/careers", careers_page_hash=first.page_hash)))
    assert second.changed


def test_scraper_unparseable_ats_falls_back_to_hash():
    careers = '<a href="https://acme.wd3.myworkdayjobs.com/External">Jobs</a>'
    res = asyncio.run(CareerSiteScraper(SiteHttp({"https://acme.ca/careers": careers})).scrape(
        _target(careers_url="https://acme.ca/careers")))
    assert res.tier == "hash" and res.ats_type == "workday"


def test_scraper_reports_errors():
    res = asyncio.run(CareerSiteScraper(SiteHttp({})).scrape(_target()))
    assert res.error and not res.jobs
