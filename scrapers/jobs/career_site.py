"""Employer career-site scraper (tiered, deterministic).

For a designated employer with a website (and no active Job Bank postings):

0. Careers page discovery: `Employer.careers_url`, else the first homepage link
   whose text/href looks like careers/jobs/emplois (or a direct ATS link).
1. Tier 1 - JSON-LD: `<script type="application/ld+json">` objects with
   `@type: JobPosting` (also inside `@graph` / lists).
2. Tier 2 - ATS: detect a hosted job board from links/iframes/scripts and read
   its public feed:
       greenhouse, lever, smartrecruiters, workable, breezy, recruitee,
       bamboohr (JSON) and jazzhr (HTML).
   ukg/ultipro, workday, dayforce, adp, taleo, icims are detected (ats_type
   recorded) but not parsed -> they fall through to tier 3.
3. Tier 3 - change detection: SHA-256 of the careers page's main text (nav,
   header, footer, scripts removed; digits-only tokens dropped to ignore dates
   and counters). A changed hash sets `needs_manual_review`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from core.models import JobSource
from core.schemas import JobPostingIn
from scrapers.base import HttpClient, ScrapeError

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
_CAREERS_TEXT = re.compile(
    r"\b(careers?|jobs?|job openings|employment|join (?:our|the) team|work (?:with|for) us|"
    r"we'?re hiring|now hiring|job opportunities|carri[eè]res?|emplois?|offres d'emploi|"
    r"recrutement|travailler (?:avec|chez) nous)\b", re.I)
_CAREERS_HREF = re.compile(r"/(careers?|jobs?|employment|join-us|work-with-us|carrieres?|emplois?)(/|$|\?|\.)", re.I)


@dataclass(frozen=True)
class AtsBoard:
    ats: str
    token: str
    url: str | None = None   # page that embedded it


ATS_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("greenhouse", re.compile(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9_-]+)")),
    ("lever", re.compile(r"jobs\.lever\.co/([A-Za-z0-9_.-]+)")),
    ("smartrecruiters", re.compile(r"(?:careers|jobs)\.smartrecruiters\.com/([A-Za-z0-9_-]+)")),
    ("workable", re.compile(r"apply\.workable\.com/([A-Za-z0-9_-]+)")),
    ("breezy", re.compile(r"([a-z0-9-]+)\.breezy\.hr")),
    ("recruitee", re.compile(r"([a-z0-9-]+)\.recruitee\.com")),
    ("bamboohr", re.compile(r"([a-z0-9-]+)\.bamboohr\.com")),
    ("jazzhr", re.compile(r"([a-z0-9-]+)\.applytojob\.com")),
    ("ukg", re.compile(r"recruiting2?\.ultipro\.(?:com|ca)/([A-Za-z0-9]+)")),
    ("workday", re.compile(r"([a-z0-9-]+)\.wd\d+\.myworkdayjobs\.com")),
    ("dayforce", re.compile(r"jobs\.dayforcehcm\.com/(?:[a-z-]+/)?([A-Za-z0-9_-]+)", re.I)),
    ("adp", re.compile(r"workforcenow\.adp\.com/.*?cid=([A-Za-z0-9-]+)")),
    ("taleo", re.compile(r"([a-z0-9-]+)\.taleo\.net")),
    ("icims", re.compile(r"careers-([a-z0-9-]+)\.icims\.com")),
)
PARSEABLE_ATS = {"greenhouse", "lever", "smartrecruiters", "workable", "breezy", "recruitee",
                 "bamboohr", "jazzhr"}
_ATS_IGNORE_TOKENS = {"www", "api", "app", "embed", "js", "static", "assets", "cdn", "careers", "jobs"}


def detect_ats(html: str, page_url: str | None = None) -> list[AtsBoard]:
    found: dict[tuple[str, str], AtsBoard] = {}
    for ats, pattern in ATS_PATTERNS:
        for m in pattern.finditer(html):
            token = m.group(1).strip("/")
            if token.lower() in _ATS_IGNORE_TOKENS:
                continue
            found.setdefault((ats, token.lower()), AtsBoard(ats, token, page_url))
    return sorted(found.values(), key=lambda b: (b.ats not in PARSEABLE_ATS, b.ats))


def find_careers_url(html: str, base_url: str) -> str | None:
    tree = HTMLParser(html)
    base_host = (urlsplit(base_url).hostname or "").removeprefix("www.")
    best: tuple[int, str] | None = None
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        url = urljoin(base_url, href)
        text = a.text(separator=" ", strip=True)
        host = (urlsplit(url).hostname or "").removeprefix("www.")
        score = 0
        if detect_ats(url):
            score = 3
        elif _CAREERS_TEXT.search(text) and len(text) < 40:
            score = 2 if host == base_host else 1
        elif _CAREERS_HREF.search(url) and host == base_host:
            score = 1
        if score and (best is None or score > best[0]):
            best = (score, url)
    return best[1] if best else None


# --------------------------------------------------------------------------- #
# Tier 1: JSON-LD
# --------------------------------------------------------------------------- #
def _iter_ld(obj):
    if isinstance(obj, list):
        for item in obj:
            yield from _iter_ld(item)
    elif isinstance(obj, dict):
        yield obj
        if "@graph" in obj:
            yield from _iter_ld(obj["@graph"])
        if "itemListElement" in obj:
            for el in obj["itemListElement"] or []:
                yield from _iter_ld(el.get("item", el) if isinstance(el, dict) else el)


def _is_job(obj: dict) -> bool:
    t = obj.get("@type")
    return t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t)


def _ld_location(obj: dict) -> tuple[str | None, str | None, str | None]:
    loc = obj.get("jobLocation")
    if isinstance(loc, list):
        loc = loc[0] if loc else None
    addr = (loc or {}).get("address") if isinstance(loc, dict) else None
    if isinstance(addr, str):
        return addr, None, None
    if isinstance(addr, dict):
        region = addr.get("addressRegion")
        return addr.get("addressLocality"), (region[:2].upper() if isinstance(region, str) and len(region) == 2 else _province_code(region)), addr.get("postalCode")
    return None, None, None


_PROVINCES = {
    "alberta": "AB", "british columbia": "BC", "manitoba": "MB", "new brunswick": "NB",
    "newfoundland and labrador": "NL", "nova scotia": "NS", "ontario": "ON",
    "prince edward island": "PE", "quebec": "QC", "québec": "QC", "saskatchewan": "SK",
    "northwest territories": "NT", "nunavut": "NU", "yukon": "YT",
}


def _province_code(value) -> str | None:
    if not isinstance(value, str):
        return None
    v = value.strip()
    if len(v) == 2 and v.isalpha():
        return v.upper()
    return _PROVINCES.get(v.lower())


def parse_date_any(value) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):  # epoch seconds or ms
        ts = value / 1000 if value > 1e11 else value
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    text = str(value).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text.replace("Z", "+0000") if fmt.endswith("%z") else text, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def parse_jsonld_jobs(html: str, page_url: str) -> list[dict]:
    tree = HTMLParser(html)
    jobs = []
    for script in tree.css('script[type="application/ld+json"]'):
        raw = script.text() or ""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            try:
                data = json.loads(re.sub(r",\s*([}\]])", r"\1", raw.strip()))  # trailing commas
            except json.JSONDecodeError:
                continue
        for obj in _iter_ld(data):
            if not _is_job(obj) or not obj.get("title"):
                continue
            city, prov, postal = _ld_location(obj)
            ident = obj.get("identifier")
            if isinstance(ident, dict):
                ident = ident.get("value")
            jobs.append({
                "id": str(ident) if ident else None,
                "title": obj.get("title"),
                "city": city, "province": prov, "postal_code": postal,
                "url": urljoin(page_url, obj.get("url") or "") or page_url,
                "posted_at": parse_date_any(obj.get("datePosted")),
                "employment_type": obj.get("employmentType") if isinstance(obj.get("employmentType"), str) else None,
            })
    return jobs


# --------------------------------------------------------------------------- #
# Tier 2: ATS feeds
# --------------------------------------------------------------------------- #
def _split_city_region(text: str | None) -> tuple[str | None, str | None]:
    if not text:
        return None, None
    parts = [p.strip() for p in re.split(r",|\s-\s", text) if p.strip()]
    city = parts[0] if parts else None
    prov = next((_province_code(p) for p in parts[1:] if _province_code(p)), None)
    return city, prov


def parse_ats_feed(ats: str, token: str, payload) -> list[dict]:
    """Normalise a public ATS feed (already decoded JSON, or HTML for jazzhr)."""
    out: list[dict] = []
    if ats == "greenhouse":
        for j in payload.get("jobs", []):
            city, prov = _split_city_region((j.get("location") or {}).get("name"))
            out.append({"id": j.get("id"), "title": j.get("title"), "city": city, "province": prov,
                        "url": j.get("absolute_url"), "posted_at": parse_date_any(j.get("updated_at"))})
    elif ats == "lever":
        for j in payload or []:
            city, prov = _split_city_region((j.get("categories") or {}).get("location"))
            out.append({"id": j.get("id"), "title": j.get("text"), "city": city, "province": prov,
                        "url": j.get("hostedUrl"), "posted_at": parse_date_any(j.get("createdAt")),
                        "employment_type": (j.get("categories") or {}).get("commitment")})
    elif ats == "smartrecruiters":
        for j in payload.get("content", []):
            loc = j.get("location") or {}
            out.append({"id": j.get("id"), "title": j.get("name"), "city": loc.get("city"),
                        "province": _province_code(loc.get("region")),
                        "url": f"https://jobs.smartrecruiters.com/{token}/{j.get('id')}",
                        "posted_at": parse_date_any(j.get("releasedDate"))})
    elif ats == "workable":
        for j in payload.get("jobs", []):
            out.append({"id": j.get("shortcode"), "title": j.get("title"), "city": j.get("city"),
                        "province": _province_code(j.get("state")), "url": j.get("url") or j.get("shortlink"),
                        "posted_at": parse_date_any(j.get("published_on"))})
    elif ats == "breezy":
        for j in payload or []:
            loc = j.get("location") or {}
            state = loc.get("state")
            out.append({"id": j.get("id"), "title": j.get("name"), "city": loc.get("city"),
                        "province": _province_code(state.get("id") if isinstance(state, dict) else state),
                        "url": j.get("url"), "posted_at": parse_date_any(j.get("published_date"))})
    elif ats == "recruitee":
        for j in payload.get("offers", []):
            out.append({"id": j.get("id"), "title": j.get("title"), "city": j.get("city"),
                        "province": _province_code(j.get("state_code") or j.get("state_name")),
                        "url": j.get("careers_url"), "posted_at": parse_date_any(j.get("published_at"))})
    elif ats == "bamboohr":
        for j in payload.get("result", []):
            loc = j.get("location") or {}
            out.append({"id": j.get("id"), "title": j.get("jobOpeningName"), "city": loc.get("city"),
                        "province": _province_code(loc.get("state")),
                        "url": f"https://{token}.bamboohr.com/careers/{j.get('id')}",
                        "employment_type": j.get("employmentStatusLabel")})
    elif ats == "jazzhr":
        tree = HTMLParser(payload)
        for a in tree.css("a[href*='/apply/']"):
            href = a.attributes.get("href") or ""
            m = re.search(r"/apply/([A-Za-z0-9]+)", href)
            title = a.text(separator=" ", strip=True)
            if not m or not title or title.lower() in ("apply", "apply now"):
                continue
            li = a.parent
            for _ in range(3):
                if li is None or li.tag == "li":
                    break
                li = li.parent
            loc_text = None
            if li is not None:
                loc_node = li.css_first(".list-group-item-text, .location, li")
                loc_text = loc_node.text(separator=" ", strip=True) if loc_node else None
            city, prov = _split_city_region(loc_text)
            out.append({"id": m.group(1), "title": title, "city": city, "province": prov,
                        "url": urljoin(f"https://{token}.applytojob.com/", href)})
    return [j for j in out if j.get("title")]


ATS_FEEDS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{t}/jobs",
    "lever": "https://api.lever.co/v0/postings/{t}?mode=json",
    "smartrecruiters": "https://api.smartrecruiters.com/v1/companies/{t}/postings?limit=100",
    "workable": "https://apply.workable.com/api/v1/widget/accounts/{t}",
    "breezy": "https://{t}.breezy.hr/json",
    "recruitee": "https://{t}.recruitee.com/api/offers/",
    "bamboohr": "https://{t}.bamboohr.com/careers/list",
    "jazzhr": "https://{t}.applytojob.com/apply",
}


# --------------------------------------------------------------------------- #
# Tier 3: change detection
# --------------------------------------------------------------------------- #
def page_fingerprint(html: str) -> tuple[str, str]:
    """(sha256, snapshot) of the page's main text, ignoring boilerplate and numbers."""
    tree = HTMLParser(html)
    for junk in tree.css("script, style, noscript, nav, header, footer, form, iframe, svg, "
                         "[role=navigation], [aria-hidden=true], .cookie, #cookie-notice"):
        junk.decompose()
    root = tree.css_first("main") or tree.css_first("[role=main]") or tree.body
    text = root.text(separator=" ", strip=True) if root is not None else ""
    tokens = [t for t in re.sub(r"\s+", " ", text).lower().split(" ") if t and not re.search(r"\d", t)]
    normalized = " ".join(tokens)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest(), normalized[:2000]


# --------------------------------------------------------------------------- #
# Scraper
# --------------------------------------------------------------------------- #
@dataclass
class EmployerTarget:
    employer_id: int
    name: str
    website_url: str | None
    careers_url: str | None
    careers_page_hash: str | None
    ats_type: str | None = None


@dataclass
class CareerResult:
    employer_id: int
    jobs: list[JobPostingIn] = field(default_factory=list)
    careers_url: str | None = None
    ats_type: str | None = None
    tier: str | None = None              # jsonld | ats | hash | None
    page_hash: str | None = None
    snapshot: str | None = None
    changed: bool = False
    error: str | None = None


def _to_job(target: EmployerTarget, raw: dict, ats: str, token: str) -> JobPostingIn:
    ident = raw.get("id") or hashlib.sha1((raw.get("url") or raw["title"]).encode()).hexdigest()[:16]
    return JobPostingIn(
        source=JobSource.CAREER_SITE,
        external_id=f"{ats}:{token}:{ident}"[:128],
        title=str(raw["title"]).strip()[:255],
        employer_name=target.name,
        employer_id=target.employer_id,
        city=raw.get("city"),
        province=raw.get("province"),
        postal_code=raw.get("postal_code"),
        url=raw.get("url"),
        posted_at=raw.get("posted_at"),
        employment_type=raw.get("employment_type"),
        source_detail=ats,
    )


class CareerSiteScraper:
    name = "career_site"

    def __init__(self, http: HttpClient):
        self.http = http

    async def _ats_jobs(self, target: EmployerTarget, board: AtsBoard) -> list[JobPostingIn]:
        url = ATS_FEEDS[board.ats].format(t=board.token)
        text = await self.http.get_text(url, headers={"Accept": "application/json, text/html"})
        payload = text if board.ats == "jazzhr" else json.loads(text)
        return [_to_job(target, raw, board.ats, board.token)
                for raw in parse_ats_feed(board.ats, board.token, payload)]

    async def scrape(self, target: EmployerTarget) -> CareerResult:
        result = CareerResult(target.employer_id)
        try:
            careers_url = target.careers_url
            home_html = None
            if not careers_url:
                if not target.website_url:
                    result.error = "no website"
                    return result
                home_html = await self.http.get_text(target.website_url)
                careers_url = find_careers_url(home_html, target.website_url) or target.website_url
            result.careers_url = careers_url

            page_html = home_html if careers_url == target.website_url and home_html else \
                await self.http.get_text(careers_url)

            # Tier 1: JSON-LD
            ld = parse_jsonld_jobs(page_html, careers_url)
            if ld:
                result.tier = "jsonld"
                result.jobs = [_to_job(target, raw, "jsonld", urlsplit(careers_url).hostname or "site")
                               for raw in ld]
                return result

            # Tier 2: ATS (careers page, then homepage, then the URL itself)
            boards = detect_ats(page_html, careers_url) or detect_ats(careers_url) or \
                (detect_ats(home_html, target.website_url) if home_html else [])
            if boards:
                result.ats_type = boards[0].ats
                for board in boards:
                    if board.ats in PARSEABLE_ATS:
                        try:
                            result.jobs = await self._ats_jobs(target, board)
                            result.tier = "ats"
                            result.ats_type = board.ats
                            return result
                        except (ScrapeError, json.JSONDecodeError, ValueError) as exc:
                            logger.info("career_site: %s feed %s failed: %s", board.ats, board.token, exc)

            # Tier 3: hash
            result.page_hash, result.snapshot = page_fingerprint(page_html)
            result.tier = "hash"
            result.changed = bool(target.careers_page_hash) and result.page_hash != target.careers_page_hash
            return result
        except ScrapeError as exc:
            result.error = str(exc)
            return result
