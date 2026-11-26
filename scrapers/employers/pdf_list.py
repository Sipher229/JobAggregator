"""Generic pdfplumber parser for municipal PDF employer lists.

Modes (`options["mode"]`):

* "tables" / "auto" (default): `page.extract_tables()` with header detection
  (HEADER_KEYWORDS); header-less continuation tables reuse the last mapping.
  "auto" falls back to "lines" if no table rows are found.
* "lines": each text line is a name. `item_regex` (named groups) can split
  structured lines like "Tim Hortons - Caraquet".
* "columns": words are assigned to fields by x-position::

      {"mode": "columns",
       "columns": {"priority_sector": [0, 170], "name": [170, 445], "program": [445, 9999]},
       "row_key": "program"}

  A line whose `row_key` column is non-empty (and matches `row_key_pattern`,
  if given) starts a new row; a line with only a name continues the previous
  row's name (wrapped text). Sector text is
  canonicalised and carried down to following rows.
  `flow_columns: [[0, 320], [320, 9999]]` instead reads newspaper-style name
  columns top-to-bottom, left-to-right (one name per line).

Common options: pages ("1-3" or [1, 2]), start_after (regex; lines before the
first match are ignored), stop_at (regex; that line and everything after is ignored), y_tolerance (default 3), skip_patterns.
"""

from __future__ import annotations

import io
import re

import pdfplumber

from scrapers.employers.common import (
    DEFAULT_SKIP_PATTERNS,
    HEADER_KEYWORDS,
    is_skippable,
    EmployerScraper,
    RawEmployer,
    canonical_sector,
    clean_text,
    fold,
    map_header,
)


def _page_numbers(spec, total: int) -> list[int]:
    if not spec:
        return list(range(total))
    if isinstance(spec, str):
        start, _, end = spec.partition("-")
        return list(range(int(start) - 1, min(int(end or start), total)))
    return [p - 1 for p in spec if 0 < p <= total]


def _lines(page, y_tolerance: float, x_range: tuple[float, float] | None = None):
    """Words grouped into visual lines: list[list[word]] sorted top-to-bottom."""
    words = page.extract_words(x_tolerance=1.5, y_tolerance=2, keep_blank_chars=False)
    if x_range:
        words = [w for w in words if x_range[0] <= w["x0"] < x_range[1]]
    words.sort(key=lambda w: (w["top"], w["x0"]))
    lines: list[list[dict]] = []
    for w in words:
        if lines and w["top"] - lines[-1][0]["top"] <= y_tolerance:
            lines[-1].append(w)
        else:
            lines.append([w])
    return [sorted(line, key=lambda w: w["x0"]) for line in lines]


def _line_text(line) -> str:
    return " ".join(w["text"] for w in line)


class _StartGate:
    """Ignore lines before `start_after` matches and from `stop_at` onwards."""

    def __init__(self, options: dict):
        start, stop = options.get("start_after"), options.get("stop_at")
        self.pattern = re.compile(start, re.I) if start else None
        self.stop = re.compile(stop, re.I) if stop else None
        self.open = start is None
        self.closed = False

    def passes(self, text: str) -> bool:
        if self.closed:
            return False
        folded = fold(text)
        if self.stop and self.stop.search(folded):
            self.closed = True
            return False
        if self.open:
            return True
        if self.pattern.search(folded):
            self.open = True
        return False


def _rows_from_tables(pages) -> list[RawEmployer]:
    out: list[RawEmployer] = []
    mapping: dict[str, int] = {}
    for page in pages:
        for table in page.extract_tables():
            for row in table:
                header = map_header(row)
                if header:
                    mapping = header
                    continue
                if not mapping:
                    continue
                values = {f: clean_text(row[i]) for f, i in mapping.items() if i < len(row)}
                if values.get("name"):
                    out.append(RawEmployer(**values))
    return out


def _rows_from_lines(pages, options) -> list[RawEmployer]:
    gate = _StartGate(options)
    out = []
    for page in pages:
        for line in _lines(page, options.get("y_tolerance", 3)):
            text = clean_text(_line_text(line))
            if text and gate.passes(text):
                out.append(RawEmployer(name=text))
    return out


def _rows_from_flow(pages, options) -> list[RawEmployer]:
    gate = _StartGate(options)
    out = []
    for page in pages:
        for x0, x1 in options["flow_columns"]:
            for line in _lines(page, options.get("y_tolerance", 3), (x0, x1)):
                text = clean_text(_line_text(line))
                if text and gate.passes(text):
                    out.append(RawEmployer(name=text))
    return out


def _is_header_line(raw_cells: dict[str, str]) -> bool:
    """A header line labels *every* column with its own field ("Priority Sector |
    Business Legal Name"); a data row like "Business, Finance | Caisse Alliance"
    does not, even though "Business" is a header keyword."""
    if "name" not in raw_cells or len(raw_cells) < 2:
        return False
    for field_name, text in raw_cells.items():
        if field_name == "marker":
            continue
        mapping = map_header([text]) if field_name == "name" else _field_of(text)
        if field_name == "name" and mapping != {"name": 0}:
            return False
        if field_name != "name" and mapping != field_name:
            return False
    return True


def _field_of(text: str) -> str | None:
    label = fold(text)
    for field_name, keywords in HEADER_KEYWORDS.items():
        if any(kw in label for kw in keywords):
            return field_name
    return None


def _rows_from_columns(pages, options) -> list[RawEmployer]:
    columns: dict[str, tuple[float, float]] = {k: tuple(v) for k, v in options["columns"].items()}
    row_key = options.get("row_key", "name")
    row_key_re = re.compile(options["row_key_pattern"], re.I) if options.get("row_key_pattern") else None
    skip = (*DEFAULT_SKIP_PATTERNS, *options.get("skip_patterns", ()))
    gate = _StartGate(options)
    out: list[RawEmployer] = []
    current: RawEmployer | None = None
    sector: str | None = None

    for page in pages:
        for line in _lines(page, options.get("y_tolerance", 3)):
            if not gate.passes(_line_text(line)):
                continue
            if is_skippable(_line_text(line), skip):
                current = None  # footer/header line: never continue a name across it
                continue
            raw_cells: dict[str, str] = {}
            for field_name, (x0, x1) in columns.items():
                text = " ".join(w["text"] for w in line if x0 <= w["x0"] < x1).strip()
                if text:
                    raw_cells[field_name] = text
            if not raw_cells:
                continue
            if _is_header_line(raw_cells):
                continue  # repeated header row
            if (s := canonical_sector(raw_cells.get("priority_sector"))):
                sector = s
            key_text = raw_cells.get(row_key, "")
            starts_row = bool(key_text) and (row_key_re is None or bool(row_key_re.search(fold(key_text))))
            name = clean_text(raw_cells.get("name"))
            if starts_row and name:
                current = RawEmployer(name=name, priority_sector=sector)
                for f, v in raw_cells.items():
                    if f not in ("name", "priority_sector") and hasattr(current, f):
                        setattr(current, f, clean_text(v))
                out.append(current)
            elif starts_row and current is not None and row_key != "name":
                # marker/program on a line of its own: belongs to the previous name
                for f, v in raw_cells.items():
                    if f not in ("name", "priority_sector") and not getattr(current, f, None):
                        setattr(current, f, clean_text(v))
            elif name and current is not None:
                current.name = f"{current.name} {name}"   # wrapped name
    return out


def parse_pdf_employers(data: bytes, options: dict | None = None) -> list[RawEmployer]:
    """Raw rows (not yet post-processed; EmployerScraper.scrape applies postprocess)."""
    options = options or {}
    mode = options.get("mode", "auto")
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        pages = [pdf.pages[i] for i in _page_numbers(options.get("pages"), len(pdf.pages))]
        if options.get("flow_columns"):
            return _rows_from_flow(pages, options)
        if mode == "columns":
            return _rows_from_columns(pages, options)
        records: list[RawEmployer] = []
        if mode in ("auto", "tables"):
            records = _rows_from_tables(pages)
        if not records and mode in ("auto", "lines"):
            records = _rows_from_lines(pages, options)
        return records


class PdfListScraper(EmployerScraper):
    name = "pdf_list"

    async def fetch_raw(self) -> list[RawEmployer]:
        url = await self.resolve_url()
        data = await self.http.get_bytes(url)
        if not data.startswith(b"%PDF"):
            raise ValueError(f"{self.target}: {url} did not return a PDF")
        return parse_pdf_employers(data, self.options)
