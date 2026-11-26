"""Deterministic entity resolution (no LLMs).

* normalize_name(): lowercase, strip accents, punctuation and trailing legal
  suffixes ("Ltée", "Inc.", "Limited", ...), collapse whitespace.
* name_variants(): legal name + operating name ("1234567 Ontario Inc. o/a Tim
  Hortons" -> {"1234567 ontario", "tim hortons"}).
* match_employer(): RapidFuzz token_sort_ratio >= 85 AND the job's city must be
  inside one of the employer's designated communities. 75-84 is flagged for
  manual review (logged + returned as NEEDS_REVIEW), never auto-linked.
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from config.communities import COMMUNITIES_BY_SLUG

logger = logging.getLogger(__name__)

MATCH_THRESHOLD = 85.0
REVIEW_THRESHOLD = 75.0

# Legal suffixes, already accent-stripped/lowercased. Removed only at the END of
# a name (repeatedly), so "Co-op Bakery" or "Inco Mining" are left intact.
LEGAL_SUFFIXES: tuple[str, ...] = (
    "incorporated", "incorporee", "inc",
    "limited", "limitee", "ltd", "ltee", "lte",
    "corporation", "corp",
    "company", "compagnie", "cie", "co",
    "llc", "llp", "lp", "ulc", "plc",
    "senc", "s e n c", "senc rl", "sencrl", "s e n c r l",
    "enr", "enregistree", "sa", "s a",
)
_SUFFIX_RE = re.compile(r"(?:\s+(?:%s))+$" % "|".join(re.escape(s) for s in
                        sorted(LEGAL_SUFFIXES, key=len, reverse=True)))

# "operating as" markers; split on the first one found.
_OPERATING_RE = re.compile(
    r"\s+(?:o/a|o\.a\.|dba|d/b/a|d\.b\.a\.|operating as|faisant affaire sous|f/a/s|"
    r"s/n|sous le nom)\s+",
    re.IGNORECASE,
)
# Applied to already-normalized text (punctuation -> spaces), e.g. "1234567 b c".
_NUMBERED_RE = re.compile(r"^\d{6,10}(?:\s+(?:canada|ontario|alberta|b ?c|british columbia|"
                          r"manitoba|saskatchewan|nova scotia|new brunswick|n ?b|n ?s|quebec))?$")
_LEADING_THE_RE = re.compile(r"^(?:the|le|la|les)\s+")


def strip_accents(text: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch)
    )


def _basic_clean(text: str) -> str:
    text = strip_accents(text).lower()
    text = text.replace("&", " and ").replace("’", "'")
    text = re.sub(r"'s\b", "s", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_name(name: str | None) -> str:
    """Canonical comparison key for a company name."""
    if not name:
        return ""
    text = _basic_clean(name)
    text = _LEADING_THE_RE.sub("", text)
    previous = None
    while previous != text:  # strip stacked suffixes, e.g. "Corp. Ltd."
        previous = text
        text = _SUFFIX_RE.sub("", text).strip()
    return text


# "3091247 Nova Scotia Limited (Sam's Pizza, Trenton)", "BKR Management Inc. (Burger King)"
_PAREN_TRADE_RE = re.compile(
    r"^(?P<legal>.+?\b(?:limited|ltd|inc|incorporated|corp|corporation|ltee|limitee|ulc|"
    r"llp|lp)\.?)(?:'s|’s)?\s*\((?P<trade>[^()]+)\)\s*$",
    re.IGNORECASE,
)


def split_operating_name(name: str) -> tuple[str, str | None]:
    """Return (legal_name, operating_name|None).

    Handles 'X o/a Y' / 'X DBA Y' / 'X faisant affaire sous Y', and a trailing
    parenthesised trade name after a legal suffix ('X Ltd (Y)'). Parentheses
    after a non-legal name ('Prairie Mountain Health (PMH)') are left alone.
    """
    parts = _OPERATING_RE.split(name, maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    m = _PAREN_TRADE_RE.match(name.strip())
    if m:
        trade = m.group("trade").strip()
        # "Sam's Pizza, Trenton" / "Sam's Pizza - Stellarton": drop a trailing known town
        tail = re.search(r"\s*(?:,|\s[-–]\s)\s*([^,–-]+)$", trade)
        if tail and normalize_place(tail.group(1)) in ALL_PLACES:
            trade = trade[: tail.start()].strip()
        if not _SUFFIX_RE.search(" " + _basic_clean(trade)) and len(trade) > 3:
            return m.group("legal").strip(), trade
    return name.strip(), None


def is_numbered_company(normalized: str) -> bool:
    return bool(_NUMBERED_RE.match(normalized))


def name_variants(name: str | None, operating_name: str | None = None) -> set[str]:
    """All normalized names an employer may appear under."""
    variants: set[str] = set()
    if name:
        legal, op = split_operating_name(name)
        variants.add(normalize_name(legal))
        if op:
            variants.add(normalize_name(op))
    if operating_name:
        variants.add(normalize_name(operating_name))
    variants.discard("")
    return variants


# Words that carry little identity. Two names that agree only on these
# ("JMH Contracting" / "MWB Contracting") must not match.
GENERIC_WORDS: frozenset[str] = frozenset("""
    the of and et de du des la le les a an
    services service contracting contractors contractor construction constructions trucking
    transport transportation logistics mechanical electric electrical enterprises enterprise
    group groupe solutions solution holdings management consulting consultants industries
    industrial systems technologies technology international partners associates agency
    corporation town city municipality township county district regional
    limited ltd inc incorporated co lp llp ltee
    partnership restaurant restaurants pizza pizzeria sushi grill bar cafe food foods
    store stores shop
    canada canadian ontario manitoba saskatchewan alberta columbia british nova scotia
    brunswick new north northern south east west
""".split())

CORE_REJECT = 50.0   # distinctive cores this different -> no match at all
CORE_CAP = 70.0      # cores in [50, 70) -> capped into the review band


def distinctive_core(normalized: str) -> str:
    """Name minus generic words and (when something else remains) place names."""
    tokens = [t for t in normalized.split() if t not in GENERIC_WORDS]
    without_places = [t for t in tokens if t not in _PLACE_TOKENS]
    return " ".join(without_places or tokens)


def _pair_score(x: str, y: str) -> float:
    if is_numbered_company(x) or is_numbered_company(y):
        return 100.0 if x == y else 0.0
    if x.replace(" ", "") == y.replace(" ", ""):
        return 100.0  # "north49 foods" / "north 49 foods", "sb 2" / "sb2"
    full = fuzz.token_sort_ratio(x, y)
    cx, cy = distinctive_core(x), distinctive_core(y)
    if not cx or not cy:
        return full
    core = max(fuzz.ratio(cx, cy), fuzz.token_sort_ratio(cx, cy))
    if core < CORE_REJECT:
        return min(full, core)
    if core < CORE_CAP:
        return min(full, MATCH_THRESHOLD - 5)  # human review, never auto-link
    return full


def name_score(a: Iterable[str], b: Iterable[str]) -> float:
    """Best pair score across name variants.

    * token_sort_ratio on the normalized names, then
    * the distinctive cores (generic words / place names removed) must agree too;
    * numbered-company names only count on an exact match ("1234567 ontario" vs
      "1234568 ontario" would otherwise score ~93).
    """
    return max((_pair_score(x, y) for x in a for y in b), default=0.0)


# --------------------------------------------------------------------------- #
# Geography
# --------------------------------------------------------------------------- #
_PLACE_ABBREVIATIONS = {
    "st": "saint", "ste": "sainte", "mt": "mount", "ft": "fort",
}


def normalize_place(place: str | None) -> str:
    if not place:
        return ""
    text = _basic_clean(place)
    tokens = [_PLACE_ABBREVIATIONS.get(t, t) for t in text.split()]
    return " ".join(tokens)


def _build_place_index() -> dict[str, frozenset[str]]:
    return {
        slug: frozenset(normalize_place(m) for m in (c.name, *c.municipalities))
        for slug, c in COMMUNITIES_BY_SLUG.items()
    }


COMMUNITY_PLACES: dict[str, frozenset[str]] = _build_place_index()
ALL_PLACES: frozenset[str] = frozenset().union(*COMMUNITY_PLACES.values())
_PLACE_TOKENS: frozenset[str] = frozenset(t for p in ALL_PLACES for t in p.split()) - {"saint", "sainte"}


def community_for_place(city: str | None, province: str | None = None,
                        postal_code: str | None = None) -> str | None:
    """Resolve a city (or postal code) to a community slug, if any."""
    place = normalize_place(city)
    prov = (province or "").upper()[:2] or None
    for slug, places in COMMUNITY_PLACES.items():
        community = COMMUNITIES_BY_SLUG[slug]
        if prov and community.province != prov:
            continue
        if place and place in places:
            return slug
    if postal_code:
        fsa = postal_code.replace(" ", "").upper()[:3]
        for slug, community in COMMUNITIES_BY_SLUG.items():
            if prov and community.province != prov:
                continue
            # Rural FSAs (e.g. P0M) are shared by many towns; only trust urban ones.
            if fsa in community.fsa_prefixes and fsa[1] != "0":
                return slug
    return None


def place_in_communities(city: str | None, community_slugs: Iterable[str],
                         province: str | None = None, postal_code: str | None = None) -> bool:
    slug = community_for_place(city, province, postal_code)
    return slug is not None and slug in set(community_slugs)


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class EmployerCandidate:
    """DB-independent view of an Employer for matching."""

    employer_id: int
    names: frozenset[str]
    community_slugs: frozenset[str]

    @classmethod
    def build(cls, employer_id: int, name: str, community_slugs: Iterable[str],
              operating_name: str | None = None) -> EmployerCandidate:
        return cls(employer_id, frozenset(name_variants(name, operating_name)),
                   frozenset(community_slugs))


@dataclass
class MatchResult:
    status: str  # "matched" | "needs_review" | "unmatched" (core.models.MatchStatus values)
    employer_id: int | None = None
    score: float = 0.0
    community_slug: str | None = None
    reason: str = ""
    alternatives: list[tuple[int, float]] = field(default_factory=list)

    @property
    def is_match(self) -> bool:
        return self.status == "matched"


def match_employer(
    employer_name: str,
    city: str | None,
    candidates: Sequence[EmployerCandidate],
    *,
    province: str | None = None,
    postal_code: str | None = None,
    threshold: float = MATCH_THRESHOLD,
    review_threshold: float = REVIEW_THRESHOLD,
) -> MatchResult:
    """Link a scraped job's employer name + location to a designated employer."""
    job_names = name_variants(employer_name)
    community_slug = community_for_place(city, province, postal_code)
    if not job_names:
        return MatchResult("unmatched", community_slug=community_slug, reason="empty_name")

    scored = sorted(
        ((name_score(job_names, c.names), c) for c in candidates),
        key=lambda pair: pair[0],
        reverse=True,
    )
    scored = [(s, c) for s, c in scored if s >= review_threshold]
    if not scored:
        return MatchResult("unmatched", community_slug=community_slug, reason="no_candidate")

    # Geographic check is mandatory: only candidates designated where the job is.
    geo_ok = [(s, c) for s, c in scored if community_slug and community_slug in c.community_slugs]
    alternatives = [(c.employer_id, s) for s, c in scored[:5]]

    if not geo_ok:
        best_score, best = scored[0]
        if best_score >= threshold:
            logger.info(
                "Geo mismatch: %r (%s) ~ employer %s score=%.1f, job community=%s, designated=%s",
                employer_name, city, best.employer_id, best_score, community_slug,
                sorted(best.community_slugs),
            )
        return MatchResult("unmatched", score=best_score, community_slug=community_slug,
                           reason="geo_mismatch", alternatives=alternatives)

    best_score, best = geo_ok[0]
    # Ambiguity: two different employers tie at the top -> human decides.
    tied = [c for s, c in geo_ok if s == best_score and c.employer_id != best.employer_id]

    if best_score >= threshold and not tied:
        return MatchResult("matched", best.employer_id, best_score, community_slug,
                           "name_and_geo", alternatives)

    reason = "ambiguous" if tied else "borderline_score"
    logger.warning(
        "Manual review: %r (%s) ~ employer %s score=%.1f reason=%s",
        employer_name, city, best.employer_id, best_score, reason,
    )
    return MatchResult("needs_review", best.employer_id, best_score, community_slug,
                       reason, alternatives)


# --------------------------------------------------------------------------- #
# Job de-duplication
# --------------------------------------------------------------------------- #
_TITLE_NOISE_RE = re.compile(
    r"\b(?:full time|part time|temps plein|temps partiel|permanent|temporary|"
    r"contract|urgent|immediate start|hiring)\b"
)


def normalize_title(title: str | None) -> str:
    if not title:
        return ""
    text = _basic_clean(title)
    text = _TITLE_NOISE_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def dedupe_hash(employer_id: int | None, title: str | None, city: str | None) -> str:
    """sha256(employer_id | normalized_title | normalized_city). Not unique by design."""
    key = f"{employer_id or ''}|{normalize_title(title)}|{normalize_place(city)}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()
