"""Canada Job Bank (www.jobbank.gc.ca) search + posting-detail scraper.

How the site works (verified 2026-11-26):

* Location search needs a city id from the typeahead endpoint
  `/core/ta-cityprovsuggest_en/select?q=<city>&wt=json` -> docs[].city_id.
* `/jobsearch/jobsearch?searchstring=&locationstring=<City, PR>&locationparam=<id>&sort=D&d=50`
  returns the first 25 results (50 km radius). Further pages are served by
  `/jobsearch/job_search_loader.xhtml` for the *session's* current search
  (JSESSIONID cookie), 25 at a time, until a page adds nothing new. Searches
  must therefore run sequentially on one client.
* Results include postings Job Bank aggregates from partner boards
  (indeed.com, Talent.com, CareerBeacon...); `source_detail` records that.
* Posting pages (`/jobsearch/jobposting/<id>`) carry schema.org RDFa plus a
  hidden NOC code (`span.aa_jobbank_job_noccode`).
* robots.txt: everything allowed, `Crawl-delay: 5` (honoured by HttpClient).

Parsing functions are pure (HTML in, records out) and tested against saved pages.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from urllib.parse import quote_plus

from selectolax.lexbor import LexborHTMLParser as HTMLParser
from selectolax.lexbor import LexborNode as Node

from config.communities import Community
from core.models import JobSource
from core.schemas import JobPostingIn
from scrapers.base import HttpClient, ScrapeError

logger = logging.getLogger(__name__)

BASE = "https://www.jobbank.gc.ca"
SUGGEST_URL = BASE + "/core/ta-cityprovsuggest_en/select?q={q}&fq=NOT%20postalcode_cnt:0&wt=json&rows=10"
SEARCH_URL = BASE + "/jobsearch/jobsearch?searchstring=&locationstring={loc}&locationparam={cid}&sort=D&d={radius}"
LOADER_URL = BASE + "/jobsearch/job_search_loader.xhtml"
POSTING_URL = BASE + "/jobsearch/jobposting/{id}"

_WS = re.compile(r"\s+")
_LOC_RE = re.compile(r"^(?P<city>.+?)\s*\((?P<prov>[A-Z]{2})\)$")
_EXPIRED_RE = re.compile(r"no longer (?:available|advertised)|has expired|n'est plus (?:disponible|affich)", re.I)


def _text(node: Node | None) -> str | None:
    if node is None:
        return None
    text = _WS.sub(" ", node.text(separator=" ", strip=True)).strip()
    return text or None


def _strip_label(text: str | None, *labels: str) -> str | None:
    if not text:
        return None
    for label in labels:
        text = re.sub(rf"^\s*{label}\s*:?\s*", "", text, flags=re.I)
    return text.strip() or None


def parse_date(text: str | None) -> datetime | None:
    if not text:
        return None
    text = re.sub(r"^\s*posted on\s*", "", _WS.sub(" ", text), flags=re.I).strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def split_location(text: str | None) -> tuple[str | None, str | None]:
    text = _strip_label(text, "Location")
    if not text:
        return None, None
    m = _LOC_RE.match(text)
    if m:
        return m.group("city").strip(), m.group("prov")
    return (None, None) if "various" in text.lower() else (text, None)


def teer_from_noc(noc: str | None) -> int | None:
    """NOC 2021: 5 digits, the second digit is the TEER category (0-5)."""
    if noc and re.fullmatch(r"\d{5}", noc):
        teer = int(noc[1])
        return teer if teer <= 5 else None
    return None


def normalize_language(text: str | None) -> str | None:
    if not text:
        return None
    t = text.lower()
    en = "english" in t or "anglais" in t
    fr = "french" in t or "français" in t or "francais" in t
    if en and fr:
        return "Bilingual" if (" and " in t or " et " in t) else "English or French"
    return "French" if fr else "English" if en else text.strip()


# --------------------------------------------------------------------------- #
# Search results
# --------------------------------------------------------------------------- #
def parse_search_results(html: str) -> list[JobPostingIn]:
    tree = HTMLParser(html)
    out: list[JobPostingIn] = []
    for art in tree.css("article[id^='article-']"):
        posting_id = (art.attributes.get("id") or "").removeprefix("article-")
        title = _text(art.css_first(".noctitle"))
        employer = _text(art.css_first("li.business"))
        if not (posting_id.isdigit() and title and employer):
            continue
        city, prov = split_location(_text(art.css_first("li.location")))
        source_li = art.css_first("li.source")
        source_name = _text(source_li.css_first(".job-source .wb-inv")) if source_li else None
        job_number = None
        if source_li is not None:
            nums = re.findall(r"\d{4,}", source_li.text())
            job_number = nums[-1] if nums else None
        out.append(JobPostingIn(
            source=JobSource.JOB_BANK,
            external_id=posting_id,
            title=title,
            employer_name=employer,
            city=city,
            province=prov,
            wage_raw=_strip_label(_text(art.css_first("li.salary")), "Salary"),
            url=POSTING_URL.format(id=posting_id),
            posted_at=parse_date(_text(art.css_first("li.date"))),
            source_detail=None if (source_name or "").lower() == "job bank" else source_name,
            job_number=job_number,
            work_mode=_text(art.css_first("span.flag .telework")),
        ))
    return out


def has_more_results(html: str) -> bool:
    return 'id="moreresultbutton"' in html


def parse_result_count(html: str) -> int | None:
    m = re.search(r"(\d[\d,]*)\s*</span>\s*<span[^>]*>\s*results?\b", html) or \
        re.search(r"\b(\d[\d,]*)\s*results?\b", HTMLParser(html).body.text() if html else "")
    return int(m.group(1).replace(",", "")) if m else None


# --------------------------------------------------------------------------- #
# Posting detail
# --------------------------------------------------------------------------- #
def parse_posting(html: str) -> dict:
    """Fields from a posting page. Returns {"expired": True} for removed postings."""
    tree = HTMLParser(html)
    main = tree.css_first("main") or tree.body
    if main is None or (tree.css_first("[property='title']") is None and _EXPIRED_RE.search(main.text())):
        return {"expired": True}

    def prop(name: str) -> str | None:
        return _text(tree.css_first(f"[property='{name}']"))

    noc = _text(tree.css_first(".aa_jobbank_job_noccode"))
    org = tree.css_first("[property='hiringOrganization']")
    org_link = org.css_first("a[href]") if org else None
    employer = _text(org.css_first("[property='name']")) if org else None
    if employer:
        employer = re.sub(r"\s*This link will open in a new window\s*$", "", employer).strip()

    # <span property="employmentType">Permanent employment<span class="attribute-value">Full time</span></span>
    terms_text = _text(tree.css_first("[property='employmentType']")) or ""
    m_term = re.search(r"\b(permanent|term or contract|temporary|seasonal|casual)\b", terms_text, re.I)
    m_type = re.search(r"\b(full|part)[ -]time\b", terms_text, re.I)
    employment_term = m_term.group(1).capitalize() if m_term else None
    employment_type = f"{m_type.group(1).capitalize()} time" if m_type else None

    language = None
    for h4 in tree.css("#comparisonchart h4, .job-posting-detail-requirements h4"):
        if (_text(h4) or "").lower().startswith(("language", "langue")):
            nxt = h4.next
            while nxt is not None and nxt.tag not in ("p", "ul", "h4"):
                nxt = nxt.next
            if nxt is not None and nxt.tag != "h4":
                language = _text(nxt)
            break

    vacancies = None
    m = re.search(r"(\d+)\s+vacanc", main.text())
    if m:
        vacancies = int(m.group(1))

    work_mode = None
    for li in tree.css("li"):
        inv = _text(li.css_first(".wb-inv"))
        if inv and inv.lower() == "work location":
            work_mode = _strip_label(_text(li), "Work location")
            break

    return {
        "expired": False,
        "title": prop("title"),
        "noc_code": noc if noc and noc.isdigit() else None,
        "teer_category": teer_from_noc(noc),
        "employer_name": employer,
        "employer_website": org_link.attributes.get("href") if org_link else None,
        "city": prop("addressLocality"),
        "province": prop("addressRegion"),
        "postal_code": prop("postalCode"),
        "posted_at": parse_date(prop("datePosted")),
        "employment_type": employment_type,
        "employment_term": employment_term,
        "language_req": normalize_language(language),
        "vacancies": vacancies,
        "work_mode": work_mode,
        "wage_raw": _clean_wage(_text(tree.css_first("[property='baseSalary']"))),
    }


def _clean_wage(text: str | None) -> str | None:
    """'$ 25.00 to $ 30.00 HOUR hourly (To be negotiated)' -> '$25.00 to $30.00 hourly (To be negotiated)'"""
    if not text:
        return None
    text = re.sub(r"\$\s+", "$", text)
    text = re.sub(r"\b(?:HOUR|YEAR|WEEK|MONTH|DAY)\b\s*", "", text)
    return _WS.sub(" ", text).strip() or None


def apply_detail(job: JobPostingIn, detail: dict) -> JobPostingIn:
    if detail.get("expired"):
        return job
    updates = {k: v for k, v in detail.items()
               if k != "expired" and v not in (None, "") and k in JobPostingIn.model_fields}
    if job.wage_raw:
        updates.pop("wage_raw", None)  # listing salary text is cleaner
    if "employer_name" in detail and detail["employer_name"]:
        updates["employer_name"] = detail["employer_name"]
    updates["details_fetched"] = True
    return job.model_copy(update=updates)


# --------------------------------------------------------------------------- #
# Network client
# --------------------------------------------------------------------------- #
class JobBankScraper:
    """Sequential Job Bank client (session-based paging)."""

    name = "job_bank"

    def __init__(self, http: HttpClient, *, radius_km: int = 50, max_pages: int = 40):
        self.http = http
        self.radius_km = radius_km
        self.max_pages = max_pages
        self._city_ids: dict[tuple[str, str], str | None] = {}

    async def city_id(self, city: str, province: str) -> str | None:
        key = (city.lower(), province)
        if key not in self._city_ids:
            data = json.loads(await self.http.get_text(SUGGEST_URL.format(q=quote_plus(city))))
            docs = [d for d in data.get("response", {}).get("docs", [])
                    if d.get("province_cd") == province and d.get("city_id")]
            exact = [d for d in docs if d.get("name", "").lower() == city.lower()]
            pick = (exact or docs or [None])[0]
            self._city_ids[key] = pick["city_id"] if pick else None
            if not pick:
                logger.warning("job_bank: no city id for %s, %s", city, province)
        return self._city_ids[key]

    async def search(self, city: str, province: str) -> list[JobPostingIn]:
        cid = await self.city_id(city, province)
        if not cid:
            raise ScrapeError(f"Job Bank has no location id for {city}, {province}")
        url = SEARCH_URL.format(loc=quote_plus(f"{city}, {province}"), cid=cid, radius=self.radius_km)
        html = await self.http.get_text(url, cache=False)
        jobs = parse_search_results(html)
        seen = {j.external_id for j in jobs}
        total = parse_result_count(html)
        more = has_more_results(html)
        pages = 1
        while more and pages < self.max_pages:
            page = await self.http.get_text(LOADER_URL, cache=False,
                                            headers={"X-Requested-With": "XMLHttpRequest"})
            pages += 1
            new = [j for j in parse_search_results(page) if j.external_id not in seen]
            if not new:
                break
            jobs.extend(new)
            seen.update(j.external_id for j in new)
            more = has_more_results(page)
        logger.info("job_bank: %s, %s -> %d postings (%d pages, site reports %s)",
                    city, province, len(jobs), pages, total)
        if total and len(jobs) < total and pages >= self.max_pages:
            logger.warning("job_bank: %s truncated at %d pages", city, pages)
        return jobs

    async def search_community(self, community: Community) -> tuple[list[JobPostingIn], list[str]]:
        """All postings around the community's search centres, plus per-city errors.

        A failing centre does not discard the others; the caller treats any error
        as an incomplete sync (no deactivation)."""
        jobs: dict[str, JobPostingIn] = {}
        errors: list[str] = []
        for city in community.search_cities:
            try:
                for job in await self.search(city, community.province):
                    jobs.setdefault(job.external_id, job)
            except ScrapeError as exc:
                errors.append(f"{city}: {exc}")
                logger.error("job_bank %s / %s failed: %s", community.slug, city, exc)
        return list(jobs.values()), errors

    async def fetch_detail(self, job: JobPostingIn) -> tuple[JobPostingIn, bool]:
        """Returns (enriched job, expired?)."""
        try:
            html = await self.http.get_text(POSTING_URL.format(id=job.external_id), cache=False)
        except ScrapeError as exc:
            if exc.status_code in (404, 410):
                return job, True
            raise
        detail = parse_posting(html)
        return apply_detail(job, detail), bool(detail.get("expired"))
