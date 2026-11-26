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
- [ ] Milestone 2 — employer ingestion (`sync-employers`)
- [ ] Milestone 3 — Job Bank + career-site job aggregation (`sync-jobs`)
- [ ] Milestone 4 — CSV / XLSX / PDF exports and full CLI

> Community portal URLs and municipality lists in `config/communities.py` are
> best-effort and get verified in Milestone 2.
