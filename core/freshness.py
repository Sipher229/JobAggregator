"""How long a posting has been up, and whether it may be stale.

Flags, in priority order:
  expired         past its "Advertised until" date (closed by sync; never exported)
  unverified      not seen in a search for more than UNVERIFIED_DAYS (e.g. the
                  community's last search failed, so it may be gone)
  expiring soon   "Advertised until" is within EXPIRING_DAYS
  stale           posted more than STALE_DAYS ago (often filled but still listed)
  no expiry date  no "Advertised until" known yet (detail page not fetched, or
                  the source doesn't publish one)
  ok
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

STALE_DAYS = 60
UNVERIFIED_DAYS = 3
EXPIRING_DAYS = 7


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Freshness:
    days_up: int | None
    days_left: int | None
    flag: str

    @property
    def needs_attention(self) -> bool:
        return self.flag in ("unverified", "stale", "expired")


def assess(posted_at: datetime | None, first_seen_at: datetime | None, last_seen_at: datetime | None,
           expires_at: datetime | None, *, now: datetime | None = None,
           stale_days: int = STALE_DAYS, unverified_days: int = UNVERIFIED_DAYS,
           expiring_days: int = EXPIRING_DAYS) -> Freshness:
    now = _aware(now) or datetime.now(timezone.utc)
    start = _aware(posted_at) or _aware(first_seen_at)
    days_up = max(0, (now - start).days) if start else None
    expires = _aware(expires_at)
    # valid through the whole "advertised until" day
    days_left = (expires + timedelta(days=1) - now).days if expires else None
    seen = _aware(last_seen_at)

    if expires and now >= expires + timedelta(days=1):
        flag = "expired"
    elif seen and now - seen > timedelta(days=unverified_days):
        flag = "unverified"
    elif days_left is not None and days_left <= expiring_days:
        flag = "expiring soon"
    elif days_up is not None and days_up > stale_days:
        flag = "stale"
    elif expires is None:
        flag = "no expiry date"
    else:
        flag = "ok"
    return Freshness(days_up, days_left, flag)


def assess_posting(post, **kwargs) -> Freshness:
    return assess(post.posted_at, post.first_seen_at, post.last_seen_at, post.expires_at, **kwargs)
