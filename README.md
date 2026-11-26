# Immigration Jobs Aggregator (FCIP / RCIP)

Deterministic (zero-LLM) pipeline that tracks designated employers in the 18
IRCC pilot communities (6 FCIP + 14 RCIP = 20 designations; Sudbury and
Timmins are in both) and aggregates their job postings.

## Setup (Windows / PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
copy .env.example .env          # optional; defaults to sqlite:///data/jobs.db
.\.venv\Scripts\alembic upgrade head
.\.venv\Scripts\python -m pytest
```

## Data model

```
Community (18) --< CommunityProgram (20) >-- EmployerCommunity --< Employer
                                                                  |
                                       JobPosting (employer_id nullable) >-+
ScrapeRun: audit log per task
```

* Designation `status` (`active` / `not_hiring` / `revoked`) is per
  community-program link; `Employer.status` is a roll-up.
* `JobPosting` is unique on `(source, external_id)`; `dedupe_hash` is indexed
  but intentionally **not** unique (cross-source duplicate detection only).
* Unmatched / borderline jobs are kept with `match_status` + `match_score`.

## Matching rules (`core/matcher.py`)

1. Normalize: lowercase, strip accents, punctuation, trailing legal suffixes
   (Inc, Ltd, Ltée, Limited, Corp, Co, S.E.N.C., Enr, ULC, LP...).
2. Split operating names (`o/a`, `DBA`, `faisant affaire sous`) and match on
   both legal and operating names. Numbered companies match only exactly.
3. `token_sort_ratio >= 85` **and** the job's city must be in one of the
   employer's designated communities (`config/communities.py` municipalities).
4. `75–84`, or a tie between employers → `needs_review` (logged), never auto-linked.

## Status

- [x] Milestone 1 — config, DB, models, schemas, matcher, Alembic, tests
- [x] Milestone 2 — employer ingestion (`sync-employers`)
- [ ] Milestone 3 — Job Bank + career-site job aggregation (`sync-jobs`)
- [ ] Milestone 4 — CSV / XLSX / PDF exports and full CLI

## Employer ingestion

```powershell
python cli.py sync-employers                         # all communities
python cli.py sync-employers -c timmins -p FCIP
python cli.py sync-employers -c sudbury --from-file saved.html   # manual import
python cli.py employers -c brandon
```

Each community's sources live in `config/communities.py` (`EmployerSource`);
the generic parsers are configured there, not coded per site:

| Parser | Used by | Key options |
|---|---|---|
| `html` | Sudbury, Kelowna, Superior East, SSM, Altona, Brandon, Claresholm, West Kootenay | `container`, `container_heading`, `row_selector`+`fields`, `item_selector`, `group_heading_selector`, `item_regex` |
| `pdf` | Acadian Peninsula, Timmins, St-Pierre-Jolys, Pictou, Steinbach, Moose Jaw, NOS | `mode` (tables/lines/columns), `columns`+`row_key`, `flow_columns`, `start_after`/`stop_at` |
| `json` | North Bay | `fields` |
| `csv` | any (`data/seeds/<slug>.csv`, auto-included when present) | columns `name, operating_name, program, website_url, careers_url, priority_sector, status` |

Shared behaviour: dated filenames are resolved from the landing page
(`link_text` / `link_href`); "not hiring" annotations set the designation to
`not_hiring`; sectors are canonicalised to NOC broad categories; trade names in
"Legal Ltd (Trade Name)" / "o/a" become operating names for job matching;
robots.txt is honoured (RFC 9309 longest-match); TLS uses the OS trust store.

Safety: a sync revokes designations missing from a list only when every source
for that (community, program) succeeded and returned at least 50% of the
previously active count. Every run is logged in `scrape_run`.

### Source status (verified 2026-11-26)

| Community | Status |
|---|---|
| Sudbury (FCIP+RCIP) | **Site returns 403 to the scraper.** Import a saved copy with `--from-file`. |
| Thunder Bay | **No public list** (old page 404). Use `data/seeds/thunder-bay.csv`. |
| Peace Liard | **No public list yet.** Use `data/seeds/peace-liard.csv`. |
| Moose Jaw | **Low fidelity**: names flow in text columns; wrapped names can't be split deterministically. Curate `data/seeds/moose-jaw.csv`. |
| All others | Automated. |

> IRCC closes the RCIP pilot on 2027-01-01 (replaced by the permanent Rural
> Community Immigration Program, same communities). Expect list URLs to change;
> a failing source is reported per run and never triggers revocations.

Golden tests in `tests/test_employer_fixtures.py` run each parser against the
real pages saved in `tests/fixtures/employers/` (`python -m tests.fixture_http`
prints a parse report).
