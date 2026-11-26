"""Employer website URL hygiene.

Community lists and job postings carry many unusable "websites": Cloudflare
email-protection links, Facebook/LinkedIn pages, chamber-of-commerce directory
entries, scheme typos ("https://http//x.ca"), hosts without a TLD. None of
those can lead to a careers page, so they are normalised or rejected here.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

# Hosts that are never an employer's own website (suffix match).
NON_EMPLOYER_HOSTS: tuple[str, ...] = (
    "facebook.com", "fb.com", "fb.me", "instagram.com", "linkedin.com", "lnkd.in",
    "twitter.com", "x.com", "youtube.com", "youtu.be", "tiktok.com", "pinterest.com",
    "yelp.ca", "yelp.com", "canada411.ca", "yellowpages.ca", "411.ca", "goo.gl",
    "maps.app.goo.gl", "g.page", "linktr.ee", "wa.me", "indeed.com", "indeed.ca",
    "jobbank.gc.ca", "google.com", "google.ca", "bit.ly",
)
NON_EMPLOYER_PATHS = re.compile(
    r"/cdn-cgi/l/email-protection|membershipdirectory|/business-directory|/directory/listing", re.I)
_SCHEME_TYPO = re.compile(r"^(?:https?://)?(https?)[:/]*//?", re.I)   # "https://http//x" / "http//x"
_HOST_OK = re.compile(r"^(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$", re.I)


def host_of(url: str | None) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.") if url else ""


def same_site(a: str | None, b: str | None) -> bool:
    ha, hb = host_of(a), host_of(b)
    return bool(ha) and ha == hb


def clean_website(url: str | None, *, exclude_site: str | None = None) -> str | None:
    """A usable employer website URL, or None.

    exclude_site: URL of the page the link was found on; links back to that site
    (e.g. the community's own pages) are not the employer's website.
    """
    if not url:
        return None
    url = url.strip().strip("\"'<>").replace(" ", "")
    if not url or url.lower().startswith(("mailto:", "tel:", "javascript:", "#")):
        return None
    m = _SCHEME_TYPO.match(url)
    if m and not re.match(r"^https?://[^/]", url, re.I) or re.match(r"^https?://https?[:/]", url, re.I):
        url = f"{m.group(1).lower()}://" + url[m.end():]
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    bare = host.removeprefix("www.")
    if not _HOST_OK.match(host) or "." not in bare:   # "www.walfordonthepark" has no real TLD
        return None
    if any(bare == h or bare.endswith("." + h) for h in NON_EMPLOYER_HOSTS):
        return None
    if NON_EMPLOYER_PATHS.search(parts.path or "") or NON_EMPLOYER_PATHS.search(parts.fragment or ""):
        return None
    if exclude_site and same_site(url, exclude_site):
        return None
    # drop fragments and tracking parameters
    query = "&".join(q for q in (parts.query or "").split("&")
                     if q and not q.lower().startswith(("utm_", "fbclid", "gclid", "mibextid")))
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", query, ""))
