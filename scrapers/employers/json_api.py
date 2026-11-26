"""JSON endpoint employer lists (e.g. a DataTables/AJAX feed behind a JS page).

Options:
  records_path: dot path to the list inside the payload ("" = payload itself)
  fields: {"name": "name", "priority_sector": "color", "status": "status"}
"""

from __future__ import annotations

import json
from typing import Any

from scrapers.employers.common import EmployerScraper, RawEmployer

_FIELDS = ("name", "operating_name", "website_url", "careers_url", "priority_sector",
           "location", "program", "status")


def _dig(payload: Any, path: str) -> Any:
    for key in filter(None, path.split(".")):
        payload = payload[int(key)] if isinstance(payload, list) else payload[key]
    return payload


def parse_json_employers(payload: Any, options: dict | None = None) -> list[RawEmployer]:
    options = options or {}
    items = _dig(payload, options.get("records_path", ""))
    fields = options.get("fields", {"name": "name"})
    out = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        values = {f: (str(item[k]) if item.get(k) not in (None, "") else None)
                  for f, k in fields.items() if f in _FIELDS}
        if values.get("name"):
            out.append(RawEmployer(**values))
    return out


class JsonApiScraper(EmployerScraper):
    name = "json_api"

    async def fetch_raw(self) -> list[RawEmployer]:
        url = await self.resolve_url()
        text = await self.http.get_text(url, headers={"Accept": "application/json",
                                                      "X-Requested-With": "XMLHttpRequest"})
        return parse_json_employers(json.loads(text), self.options)
