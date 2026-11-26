"""Sync the static community registry into the database (idempotent)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from config.communities import COMMUNITIES
from core.models import Community, CommunityProgram


def sync_communities(session: Session) -> dict[str, int]:
    """Upsert the 18 communities and their 20 program designations."""
    created = updated = 0
    existing = {c.slug: c for c in session.scalars(select(Community))}

    for cfg in COMMUNITIES:
        row = existing.get(cfg.slug)
        if row is None:
            row = Community(slug=cfg.slug)
            session.add(row)
            created += 1
        elif (row.name, row.province, row.portal_url) != (cfg.name, cfg.province, cfg.portal_url):
            updated += 1
        row.name, row.province, row.portal_url = cfg.name, cfg.province, cfg.portal_url

        have = {p.program for p in row.programs}
        for program in cfg.programs:
            if program not in have:
                row.programs.append(CommunityProgram(program=program))

    session.flush()
    return {"created": created, "updated": updated}
