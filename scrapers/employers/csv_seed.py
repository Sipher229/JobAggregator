"""Hand-maintained employer seed files: data/seeds/<community_slug>.csv

Columns (header required, UTF-8 or UTF-8-SIG, extra columns ignored):
    name, operating_name, program, website_url, careers_url, priority_sector, status

`program` may be FCIP, RCIP or blank (= every program of the community, or the
EmployerSource.program if set).
"""

from __future__ import annotations

import csv
from pathlib import Path

from core.database import PROJECT_ROOT
from scrapers.employers.common import EmployerScraper, RawEmployer, clean_text

SEED_DIR = PROJECT_ROOT / "data" / "seeds"


def seed_path(slug: str) -> Path:
    return SEED_DIR / f"{slug}.csv"


def parse_csv_employers(path: Path) -> list[RawEmployer]:
    out = []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            row = {(k or "").strip().lower(): clean_text(v) for k, v in row.items()}
            if not row.get("name"):
                continue
            out.append(RawEmployer(
                name=row["name"],
                operating_name=row.get("operating_name"),
                website_url=row.get("website_url"),
                careers_url=row.get("careers_url"),
                priority_sector=row.get("priority_sector"),
                program=row.get("program"),
                status=row.get("status"),
            ))
    return out


class CsvSeedScraper(EmployerScraper):
    name = "csv_seed"

    @property
    def path(self) -> Path:
        return Path(self.source.url) if self.source.url else seed_path(self.community.slug)

    async def fetch_raw(self) -> list[RawEmployer]:
        if not self.path.exists():
            self.log.info("%s: no seed file at %s", self.target, self.path)
            return []
        return parse_csv_employers(self.path)
