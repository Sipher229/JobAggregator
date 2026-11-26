"""Generic, configurable HTML employer-list parser.

Modes (chosen from `EmployerSource.options`):

1. Explicit rows::

       {"row_selector": ".employer_box_container",
        "fields": {"name": "h5", "website_url": "h5 a@href", "location": "p"}}

   A field spec is a CSS selector relative to the row, optionally suffixed with
   `@attr`. A spec of "" means the row's own text.

2. Auto table (default): every <table> whose header row maps to a "name" column
   via HEADER_KEYWORDS.

3. List items::

       {"item_selector": "ul li"}

   Item text is the name; the first external link is the website.

Scoping / enrichment options:
  container            CSS selector; rows are collected from *every* match.
  container_heading    regex on heading text (h1-h6); the container is the
                       nearest ancestor of that heading that contains rows.
  group_heading_selector  CSS for section headings (e.g. "h4"); each row gets
                       the nearest preceding heading as its priority_sector.
  item_regex           named groups split the name text into fields, e.g.
                       r"^(?P<name>.+?) - (?P<priority_sector>.+?) - (?P<status>.+)$"
  skip_patterns, min_name_length, max_name_length
"""

from __future__ import annotations

import re

from selectolax.lexbor import LexborHTMLParser as HTMLParser
from selectolax.lexbor import LexborNode as Node

from scrapers.employers.common import (
    EmployerScraper,
    RawEmployer,
    clean_text,
    fold,
    map_header,
    normalize_url,
    same_site,
)

_FIELDS = ("name", "operating_name", "website_url", "careers_url", "priority_sector",
           "location", "program", "status")
_HEADINGS = "h1, h2, h3, h4, h5, h6"


def _text(node: Node | None) -> str | None:
    return clean_text(node.text(separator=" ", strip=True)) if node is not None else None


def _extract(row: Node, spec: str, base_url: str | None) -> str | None:
    selector, _, attr = spec.partition("@")
    node = row.css_first(selector) if selector else row
    if node is None:
        return None
    if attr:
        value = node.attributes.get(attr)
        return normalize_url(value, base_url) if attr in ("href", "src") else clean_text(value)
    return _text(node)


def _first_external_link(node: Node, base_url: str | None) -> str | None:
    for a in node.css("a[href]"):
        url = normalize_url(a.attributes.get("href"), base_url)
        if url and (not base_url or not same_site(url, base_url)):
            return url
    return None


def _cells(row: Node) -> list[Node]:
    return [c for c in row.iter() if c.tag in ("td", "th")]


# -- row collectors: each returns list[(row_node, RawEmployer)] ----------------
def _rows_fields(root: Node, options: dict, base_url: str | None):
    out = []
    for row in root.css(options["row_selector"]):
        values = {f: _extract(row, spec, base_url) for f, spec in options["fields"].items()
                  if f in _FIELDS}
        if values.get("name"):
            out.append((row, RawEmployer(**values)))
    return out


def _rows_tables(root: Node, base_url: str | None):
    out = []
    for table in root.css("table"):
        rows = table.css("tr")
        mapping, start = {}, 0
        for i, row in enumerate(rows[:3]):  # header may follow a caption-like row
            mapping = map_header([c.text(strip=True) for c in _cells(row)])
            if mapping:
                start = i + 1
                break
        if not mapping:
            continue
        for row in rows[start:]:
            cells = _cells(row)

            def cell(field_name: str) -> Node | None:
                idx = mapping.get(field_name)
                return cells[idx] if idx is not None and idx < len(cells) else None

            name_cell = cell("name")
            if name_cell is None:
                continue
            values = {f: _text(c) for f in _FIELDS if (c := cell(f)) is not None}
            for url_field in ("website_url", "careers_url"):
                if (c := cell(url_field)) is not None:
                    link = c.css_first("a[href]")
                    values[url_field] = normalize_url(
                        link.attributes.get("href") if link else values.get(url_field), base_url)
            values["website_url"] = values.get("website_url") or _first_external_link(name_cell, base_url)
            if values.get("name"):
                out.append((row, RawEmployer(**values)))
    return out


def _rows_items(root: Node, options: dict, base_url: str | None):
    out = []
    name_sel = options.get("name_selector")
    for item in root.css(options["item_selector"]):
        name = _text(item.css_first(name_sel) if name_sel else item)
        if name:
            out.append((item, RawEmployer(name=name, website_url=_first_external_link(item, base_url))))
    return out


def _collect(root: Node, options: dict, base_url: str | None):
    if options.get("row_selector") and options.get("fields"):
        return _rows_fields(root, options, base_url)
    if options.get("item_selector"):
        return _rows_items(root, options, base_url)
    return _rows_tables(root, base_url)


def _containers(tree: HTMLParser, options: dict) -> list[Node]:
    if options.get("container"):
        return tree.css(options["container"])
    if options.get("container_heading"):
        pattern = re.compile(options["container_heading"], re.I)
        probe = options.get("row_selector") or options.get("item_selector") or "table"
        for heading in tree.css(_HEADINGS):
            if not pattern.search(fold(heading.text(strip=True))):
                continue
            node = heading.parent
            for _ in range(6):
                if node is None:
                    break
                if node.css_first(probe) is not None:
                    return [node]
                node = node.parent
        return []
    return [tree.body] if tree.body is not None else []


def _assign_group_headings(root: Node, rows, selector: str) -> None:
    """Set priority_sector from the nearest heading preceding each row (document order)."""
    headings = {h.mem_id for h in root.css(selector)}
    by_id = {node.mem_id: raw for node, raw in rows}
    current = None
    for node in root.traverse():
        if node.mem_id in headings:
            current = _text(node)
        elif node.mem_id in by_id and current and not by_id[node.mem_id].priority_sector:
            by_id[node.mem_id].priority_sector = current


def parse_html_employers(html: str, options: dict | None = None,
                         base_url: str | None = None) -> list[RawEmployer]:
    """Raw rows (not yet post-processed; EmployerScraper.scrape applies postprocess)."""
    options = options or {}
    tree = HTMLParser(html)
    for junk in tree.css("script, style, noscript, nav, header, footer, "
                         ".elementor-hidden-desktop.elementor-hidden-tablet.elementor-hidden-mobile"):
        junk.decompose()
    records: list[RawEmployer] = []
    for root in _containers(tree, options):
        rows = _collect(root, options, base_url)
        if options.get("group_heading_selector"):
            _assign_group_headings(root, rows, options["group_heading_selector"])
        records.extend(raw for _, raw in rows)
    return records


class HtmlTableScraper(EmployerScraper):
    name = "html_table"

    async def fetch_raw(self) -> list[RawEmployer]:
        url = await self.resolve_url()
        return parse_html_employers(await self.fetch_html(url), self.options, base_url=url)
