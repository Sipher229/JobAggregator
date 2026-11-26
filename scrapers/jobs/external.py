"""Pluggable adapters for external job boards (Indeed, LinkedIn, ...).

Neither Indeed nor LinkedIn offers a public job-search API today (Indeed's
Publisher API is retired; LinkedIn's Jobs APIs are partner-only), and both
prohibit scraping in their terms. Note that Canada Job Bank already
re-publishes many Indeed / Talent.com / CareerBeacon postings, which
`job_bank.py` ingests with `source_detail` set.

To add a licensed feed (a partner API key, a paid aggregator, an XML feed
an employer gives you), implement `ExternalJobSource` and register it:

    class MyFeed:
        name = "my_feed"
        source = JobSource.INDEED
        async def fetch(self, community: Community, http: HttpClient) -> list[JobPostingIn]: ...

    register(MyFeed())

Registered adapters run in `sync-jobs --source external` and go through the
same matching (name + geography) as Job Bank postings.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from config.communities import Community
from core.models import JobSource
from core.schemas import JobPostingIn
from scrapers.base import HttpClient


@runtime_checkable
class ExternalJobSource(Protocol):
    name: str
    source: JobSource

    async def fetch(self, community: Community, http: HttpClient) -> list[JobPostingIn]:
        ...


EXTERNAL_SOURCES: dict[str, ExternalJobSource] = {}


def register(adapter: ExternalJobSource) -> None:
    if not isinstance(adapter, ExternalJobSource):
        raise TypeError("adapter must implement ExternalJobSource")
    EXTERNAL_SOURCES[adapter.name] = adapter
