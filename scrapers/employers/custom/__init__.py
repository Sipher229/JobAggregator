"""Custom per-community employer scrapers.

Register a subclass of scrapers.employers.common.EmployerScraper here when a
community portal cannot be handled by the generic HTML/PDF/CSV parsers, then
set `EmployerSource(SourceType.CUSTOM)` on that community in
config/communities.py.
"""

from __future__ import annotations

CUSTOM_SCRAPERS: dict[str, type] = {}
