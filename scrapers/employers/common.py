"""Shared pieces for employer-list scrapers."""

from __future__ import annotations

import re
from abc import abstractmethod
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

from pydantic import HttpUrl, TypeAdapter, ValidationError
from selectolax.lexbor import LexborHTMLParser

from config.communities import Community, EmployerSource, Program
from core.matcher import strip_accents
from core.urls import clean_website
from core.schemas import EmployerIn
from scrapers.base import BaseScraper, HttpClient, ScrapeError, render_with_browser


@dataclass
class RawEmployer:
    name: str
    operating_name: str | None = None
    website_url: str | None = None
    careers_url: str | None = None
    priority_sector: str | None = None
    location: str | None = None
    program: str | None = None   # "RCIP", "FCIP", "RCIP/FCIP"; None = source default
    status: str | None = None    # free text ("Not Hiring", notes column, ...)
    not_hiring: bool = False
    flags: list[str] = field(default_factory=list)


# Header text -> field. Checked in order, so specific fields win over "name"
# ("Employer Website" -> website_url, not name).
HEADER_KEYWORDS: dict[str, tuple[str, ...]] = {
    "operating_name": ("operating name", "operating as", "dba", "trade name", "nom commercial"),
    "website_url": ("website", "web site", "site web", "url", "site internet", "link", "lien"),
    "careers_url": ("careers", "jobs page", "carrieres"),
    "priority_sector": ("sector", "industry", "secteur", "industrie", "naics"),
    "location": ("location", "city", "town", "municipality", "ville", "municipalite",
                 "address", "adresse"),
    "program": ("pilot", "program", "programme"),
    "status": ("status", "statut", "notes", "hiring", "recrut"),
    "name": ("employer", "business", "company", "organization", "organisation",
             "entreprise", "employeur", "name", "nom"),
}

DEFAULT_SKIP_PATTERNS: tuple[str, ...] = (
    r"^page \d+( of \d+)?$",
    r"^\d+$",
    r"^\W*$",
    r"^(list of )?(rcip |fcip )?(designated )?employers?( list)?:?$",
    r"^(liste des )?employeurs( designes)?$",
    r"^(last )?updated",
    r"^mis a jour",
    r"^(rural|francophone) community immigration pilot",
    r"^(employer|business|company)('?s)?( legal)?( business)? name$",
    r"^business legal name$",
    r"^priority sectors?$",
)

# --------------------------------------------------------------------------- #
# Text helpers
# --------------------------------------------------------------------------- #
_WS_RE = re.compile(r"\s+")
_BULLET_RE = re.compile(r"^[\s•·\-–—*▪●◦]+|[\s•·\-–—▪●◦]+$")
_NOT_HIRING_PHRASE = (r"not\s+(?:currently\s+)?(?:hiring|recruiting)(?:\s+at\s+(?:this|the)\s+"
                      r"(?:time|moment))?|ne\s+recrute\s+pas(?:\s+pour\s+le\s+moment)?")
# Suffix forms on a name: "X [not hiring]", "X - (Not Hiring at this time)", "X – Not Currently Hiring"
_NAME_NOT_HIRING_RE = re.compile(
    rf"\s*[-–—]?\s*(?:[\[\(]\s*(?:{_NOT_HIRING_PHRASE})[^\]\)]*[\]\)]|(?:{_NOT_HIRING_PHRASE}))\s*$",
    re.I)
_NAME_HIRING_RE = re.compile(r"\s*[-–—]\s*(?:currently\s+)?hiring\s*$", re.I)
_STATUS_NOT_HIRING_RE = re.compile(rf"\b(?:{_NOT_HIRING_PHRASE})\b|^\s*no\s*$", re.I)
_YEAR_TAG_RE = re.compile(r"\s*\((?:19|20)\d{2}\)\s*$")
_PROGRAM_RE = re.compile(r"\b(FCIP|RCIP|PPICF|PICR)\b", re.I)


def fold(text: str) -> str:
    return _WS_RE.sub(" ", strip_accents(text)).strip().lower()


def clean_text(text: str | None) -> str | None:
    if text is None:
        return None
    text = _WS_RE.sub(" ", text.replace("\xa0", " ").replace("​", "")).strip()
    text = _BULLET_RE.sub("", text).strip()
    return text or None


def parse_programs(text: str | None) -> tuple[Program, ...]:
    found = []
    for token in _PROGRAM_RE.findall(text or ""):
        token = token.upper()
        program = Program.FCIP if token in ("FCIP", "PPICF") else Program.RCIP
        if program not in found:
            found.append(program)
    return tuple(found)


# NOC 2021 broad occupational categories; first match wins, so the more
# specific / overlapping labels come first ("Social, Community and Government
# Services" must not land in "Sales and service").
SECTOR_CANON: tuple[tuple[str, str], ...] = (
    (r"educat|law\b|social|community|government|enseignement|droit|gouvern", "Education, law, social, community and government services"),
    (r"health|sante", "Health"),
    (r"trade|transport|equipment operat|metiers", "Trades, transport and equipment operators"),
    (r"manufactur|utilit|fabrication|services publics", "Manufacturing and utilities"),
    (r"natural resources|agricultur|forestry|mining|ressources naturelles", "Natural resources, agriculture and related production"),
    (r"applied science|natural and applied|engineering|sciences naturelles", "Natural and applied sciences"),
    (r"business|financ|admin|affaires", "Business, finance and administration"),
    (r"art|culture|recreation|sport|loisir", "Art, culture, recreation and sport"),
    (r"sales|service|vente", "Sales and service"),
    (r"management|gestion", "Legislative and senior management"),
)


def canonical_sector(text: str | None) -> str | None:
    if not text:
        return None
    folded = fold(text)
    for pattern, label in SECTOR_CANON:
        if re.search(pattern, folded):
            return label
    return None


def finalize(r: RawEmployer) -> RawEmployer | None:
    """Strip status/year annotations from the name and derive flags."""
    name = clean_text(r.name) or ""
    if _NAME_NOT_HIRING_RE.search(name):
        r.not_hiring = True
        name = _NAME_NOT_HIRING_RE.sub("", name)
    name = _NAME_HIRING_RE.sub("", name)
    name = _YEAR_TAG_RE.sub("", name)
    if name.endswith("*"):
        r.flags.append("asterisk")  # e.g. NOS: fast food / gas station no longer eligible
        name = name.rstrip("* ")
    if r.status and _STATUS_NOT_HIRING_RE.search(r.status):
        r.not_hiring = True
    name = re.sub(r"(\w)- ([a-z])", r"\1-\2", name)  # PDF line-wrap hyphen: "Franco- manitobaine"
    r.name = clean_text(name) or ""
    r.priority_sector = canonical_sector(r.priority_sector) or clean_text(r.priority_sector)
    return r if r.name else None


def map_header(cells: list[str | None]) -> dict[str, int]:
    """Map field names to column indexes using HEADER_KEYWORDS."""
    mapping: dict[str, int] = {}
    for idx, cell in enumerate(cells):
        label = fold(cell or "")
        if not label or len(label) > 60:
            continue
        for field_name, keywords in HEADER_KEYWORDS.items():
            if field_name in mapping:
                continue
            if any(kw in label for kw in keywords):
                mapping[field_name] = idx
                break
    return mapping if "name" in mapping else {}


def normalize_url(url: str | None, base_url: str | None = None) -> str | None:
    url = clean_text(url)
    if not url or url.lower().startswith(("mailto:", "tel:", "javascript:", "#")):
        return None
    if base_url and not re.match(r"^[a-z]+://", url, re.I) and url.startswith("/"):
        url = urljoin(base_url, url)
    if not re.match(r"^https?://", url, re.I):
        if re.match(r"^(www\.)?[a-z0-9-]+(\.[a-z0-9-]+)+(/.*)?$", url, re.I):
            url = "https://" + url
        else:
            return None
    return url


_URL_ADAPTER = TypeAdapter(HttpUrl)


def valid_url(url: str | None, *, exclude_site: str | None = None) -> str | None:
    """A usable employer website (core.urls.clean_website) that also passes strict
    URL validation; unusable URLs are dropped, never the row."""
    url = clean_website(url, exclude_site=exclude_site)
    if not url:
        return None
    try:
        _URL_ADAPTER.validate_python(url)
        return url
    except ValidationError:
        return None


def is_skippable(name: str, patterns: tuple[str, ...] | list[str]) -> bool:
    folded = fold(name)
    return any(re.search(p, folded) for p in patterns)


def apply_regex(r: RawEmployer, pattern: re.Pattern | None) -> RawEmployer | None:
    """Split a raw name into fields with named groups; non-matching rows are kept as-is."""
    if pattern is None:
        return r
    m = pattern.match(r.name)
    if not m:
        return r
    for key, value in m.groupdict().items():
        if value is not None and hasattr(r, key):
            setattr(r, key, clean_text(value))
    return r


def postprocess(records: list[RawEmployer], options: dict) -> list[RawEmployer]:
    """Shared filtering for all parsers: regex split, annotation stripping, skip rules."""
    pattern = re.compile(options["item_regex"], re.I) if options.get("item_regex") else None
    skip = (*DEFAULT_SKIP_PATTERNS, *options.get("skip_patterns", ()))
    min_len = options.get("min_name_length", 2)
    max_len = options.get("max_name_length", 150)
    out = []
    for r in records:
        r = apply_regex(r, pattern)
        r = finalize(r) if r else None
        if r and min_len <= len(r.name) <= max_len and not is_skippable(r.name, skip):
            out.append(r)
    return out


def dedupe_raw(records: list[RawEmployer]) -> list[RawEmployer]:
    """Merge rows with the same name (same employer listed under several sectors)."""
    merged: dict[str, RawEmployer] = {}
    for r in records:
        key = fold(r.name)
        if key not in merged:
            merged[key] = r
            continue
        keep = merged[key]
        keep.website_url = keep.website_url or r.website_url
        keep.priority_sector = keep.priority_sector or r.priority_sector
        keep.location = keep.location or r.location
        keep.not_hiring = keep.not_hiring and r.not_hiring
        if r.program and keep.program and r.program != keep.program:
            keep.program = f"{keep.program}/{r.program}"
    return list(merged.values())


def find_link(html: str, base_url: str, *, text: str | None = None,
              href: str | None = None) -> str | None:
    """First <a> whose (accent-folded) text and/or href match the given regexes."""
    tree = LexborHTMLParser(html)
    for a in tree.css("a[href]"):
        url = a.attributes.get("href") or ""
        if text and not re.search(text, fold(a.text(separator=" ", strip=True)), re.I):
            continue
        if href and not re.search(href, url, re.I):
            continue
        return urljoin(base_url, url)
    return None


def same_site(url: str, base_url: str) -> bool:
    return urlsplit(url).netloc.removeprefix("www.") == urlsplit(base_url).netloc.removeprefix("www.")


# --------------------------------------------------------------------------- #
# Base scraper
# --------------------------------------------------------------------------- #
class EmployerScraper(BaseScraper[EmployerIn]):
    """Base for one employer source of one community.

    Options shared by all scrapers:
      link_text / link_href: regexes; `source.url` is then a landing page and the
          real document is the first matching link (for dated PDF filenames).
    """

    name = "employer"

    def __init__(self, community: Community, source: EmployerSource, http: HttpClient):
        super().__init__(http)
        self.community = community
        self.source = source
        self.options = dict(source.options)
        self.resolved_url: str | None = source.url

    @property
    def target(self) -> str:
        return f"{self.community.slug}:{self.source.type}:{self.source.program or 'ALL'}"

    @property
    def programs(self) -> tuple[Program, ...]:
        return (self.source.program,) if self.source.program else self.community.programs

    async def fetch_html(self, url: str) -> str:
        if self.source.requires_js:
            return await render_with_browser(url, wait_selector=self.options.get("wait_selector"))
        return await self.http.get_text(url)

    async def resolve_url(self) -> str:
        if not self.source.url:
            raise ValueError(f"{self.target}: source requires a url")
        link_text, link_href = self.options.get("link_text"), self.options.get("link_href")
        if not (link_text or link_href):
            return self.source.url
        page = await self.fetch_html(self.source.url)
        url = find_link(page, self.source.url, text=link_text, href=link_href)
        if not url:
            raise ScrapeError(f"{self.target}: no link matching text={link_text!r} "
                              f"href={link_href!r} on {self.source.url}")
        self.log.info("%s: resolved document %s", self.target, url)
        self.resolved_url = url
        return url

    @abstractmethod
    async def fetch_raw(self) -> list[RawEmployer]:
        ...

    async def scrape(self) -> list[EmployerIn]:
        raw = dedupe_raw(postprocess(await self.fetch_raw(), self.options))
        if not raw:
            self.log.warning("%s: 0 employers parsed (layout change?)", self.target)
        return self.to_records(raw)

    def to_records(self, raw: list[RawEmployer]) -> list[EmployerIn]:
        records: list[EmployerIn] = []
        for r in raw:
            programs = parse_programs(r.program) if r.program else self.programs
            if r.program and not programs:
                self.log.warning("%s: row %r has unknown program %r", self.target, r.name, r.program)
                continue
            for program in programs:
                if program not in self.community.programs:
                    self.log.warning("%s: row %r has program %s not run by community",
                                     self.target, r.name, program)
                    continue
                if self.source.program and program != self.source.program:
                    continue
                try:
                    records.append(EmployerIn(
                        name=r.name,
                        operating_name=r.operating_name,
                        community_slug=self.community.slug,
                        program=program,
                        website_url=valid_url(r.website_url, exclude_site=self.resolved_url),
                        careers_url=valid_url(r.careers_url, exclude_site=self.resolved_url),
                        priority_sector=r.priority_sector,
                        location=r.location,
                        not_hiring=r.not_hiring,
                        flags=r.flags,
                        source_url=self.resolved_url,
                    ))
                except ValidationError as exc:
                    self.log.warning("%s: dropping invalid row %r: %s", self.target, r.name,
                                     exc.errors()[0]["msg"])
        self.log.info("%s: parsed %d employer records", self.target, len(records))
        return records
