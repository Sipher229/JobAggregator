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
- [x] Milestone 3 — Job Bank + career-site job aggregation (`sync-jobs`)
- [x] Milestone 4 — CSV / XLSX / PDF exports and full CLI

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

## Job aggregation

```powershell
python cli.py sync-jobs                               # Job Bank + career sites, all communities
python cli.py sync-jobs -c timmins -s job_bank --max-details 50
python cli.py sync-jobs -s career_site --career-limit 100
python cli.py jobs -c brandon --status matched        # or needs_review / unmatched / all
```

**Job Bank** (`scrapers/jobs/job_bank.py`): each community's
`job_search_cities` are searched with a 50 km radius. Paging is session-based,
so searches run sequentially, and robots.txt asks for `Crawl-delay: 5`, which
the client honours. A full pass is about 40 searches, roughly 10 minutes. Postings
Job Bank re-publishes from partner boards (indeed.com, Talent.com, CareerBeacon...)
are kept with `source_detail` set. Detail pages (NOC, TEER, language,
full/part time, permanent/seasonal, vacancies, postal code, employer website)
are fetched only for matched / needs-review postings, once, within
`--max-details` per run.

**Matching** (`core/matcher.py`): `token_sort_ratio >= 85` and the job's city
must be inside one of the employer's designated communities. In addition, the
names' distinctive cores (generic words like "Contracting", "Services",
"Corporation of the Town of" and place names removed) must agree. Otherwise
"JMH Contracting" would match "MWB Contracting". Cores that are somewhat similar are
capped into the review band (75-84), so a person checks them.

**Career sites** (`scrapers/jobs/career_site.py`), for designated employers
with a website and no active matched Job Bank posting:
1. JSON-LD `JobPosting`
2. ATS feeds: Greenhouse, Lever, SmartRecruiters, Workable, Breezy,
   Recruitee, BambooHR (JSON), JazzHR (HTML). Workday, UKG, Dayforce, ADP,
   Taleo and iCIMS are detected and recorded in `ats_type` only.
3. SHA-256 of the careers page's main text (boilerplate and numbers removed).
   A change sets `Employer.needs_manual_review`.
Only a dedicated careers page is hashed; employers whose site has no careers
link are recorded as `no_careers_page` (homepages change too often to diff).
Website URLs are cleaned on the way in (`core/urls.py`): Cloudflare
email-protection links, Facebook/LinkedIn/directory pages and hosts without a
TLD are rejected; `http//` typos and tracking parameters are fixed.
Failures are stored in `Employer.website_error` as `<kind>: <detail>` and
shown in the Employers tab. `dns`, `robots` and `invalid_url` are permanent,
so those employers are skipped until their website changes. `blocked` (403),
`rate_limited`, `server`, `tls` and `network` are retried on the next run.
After an HTTPS certificate or protocol failure, plain `http://` is tried once.
Career postings must also be located in the employer's community (a national
chain's Toronto opening is skipped). Postings without a location are marked `needs_review`,
and duplicates of an active Job Bank posting (same `dedupe_hash`) are skipped.

**Lifecycle**: postings are upserted on `(source, external_id)`. After a fully
successful community search, Job Bank postings in that community that weren't
seen are set `is_active = False`. A failed search centre disables this for that community.
Expired detail pages (404 / "no longer available") are closed immediately.

**External boards** (`scrapers/jobs/external.py`): Indeed and LinkedIn have no
public job-search APIs and prohibit scraping. The module is a typed adapter
interface for licensed feeds, and nothing is registered by default.

**Transport note**: www.jobbank.gc.ca never answers an OpenSSL ClientHello
offering both TLS 1.2 and 1.3, but connects instantly with TLS 1.3 only.
`HttpConfig.tls13_hosts` pins that per host. It is a compatibility setting, not
client impersonation.

## Reports

```powershell
python cli.py export                                   # xlsx, everything
python cli.py export -f csv -f xlsx -f pdf -p FCIP -l fr
python cli.py export -f pdf -c sudbury --include-review -o C:\reports
```

| Option | Values |
|---|---|
| `--format/-f` | `csv`, `xlsx`, `pdf` (repeatable; default `xlsx`) |
| `--program/-p` | `FCIP`, `RCIP`, `ALL`: the employer must be designated under it *in the posting's community* |
| `--community/-c` | community slug (`python cli.py communities`) |
| `--lang/-l` | `fr` (French, Bilingual, English or French), `en`, `all` |
| `--include-review` | also include postings whose employer match is borderline |

* **CSV**: active postings, UTF-8 with BOM (accents render in Excel).
* **XLSX**: *Active Job Postings*, *Designated Employers* (one row per
  designation, with active-job counts and careers-page change flags), *Summary*
  (per community × program), *About*. Headers are frozen, columns have filters
  and fitted widths, and URLs are clickable.
* **PDF**: landscape report with a summary table, then Province → Community →
  Employer sections listing each posting's title, NOC/TEER, language, city,
  source and a link.

Totals count distinct postings. Under a single program, a job at an employer designated under both
FCIP and RCIP appears in both rows. Language is known only for postings whose Job Bank
detail page has been fetched (see `--max-details`), so `--lang` filters may
grow as later runs fill in details.

## Scheduling

```powershell
python cli.py schedule                       # employers Mon 04:00, jobs + xlsx/pdf export daily 06:00
python cli.py schedule --jobs-cron "30 7 * * 1-5" --formats xlsx --run-now
python cli.py schedule --dry-run             # print next run times and exit
```

`schedule` is a blocking APScheduler process. Cron strings use standard crontab
weekday numbering (1 = Monday). APScheduler 3's own `from_crontab` treats 0 as Monday, so the CLI
translates numbers to day names. A run is never started twice at once.
On Windows you can instead register `python cli.py sync-jobs` and `python cli.py export`
with Task Scheduler.

## Typical cycle

```powershell
python cli.py sync-employers     # refresh designations (Sudbury: update data/manual/sudbury.html first)
python cli.py rematch            # re-link stored postings to the new designations
python cli.py sync-jobs          # Job Bank + career sites
python cli.py export -f xlsx -f pdf
python cli.py status             # recent runs and totals
```
