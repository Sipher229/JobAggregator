"""Command-line interface.

    python cli.py init-db
    python cli.py communities
    python cli.py sync-employers [--community SLUG ...] [--program FCIP|RCIP]
    python cli.py sync-employers --community sudbury --from-file saved_page.html
    python cli.py employers [--community SLUG] [--program FCIP|RCIP]
    python cli.py sync-jobs [--community SLUG ...] [--source job_bank|career_site|external|all]
                            [--max-details N] [--no-details] [--career-limit N]
    python cli.py jobs [--community SLUG] [--status matched|needs_review|unmatched|all]
    python cli.py rematch
    python cli.py refresh-details [--limit N] [--all]
    python cli.py export --format csv|xlsx|pdf [--program FCIP|RCIP|ALL] [--community SLUG]
                         [--lang fr|en|all] [--include-review] [--output-dir DIR]
    python cli.py schedule [--jobs-cron "0 6 * * *"] [--employers-cron "0 4 * * 1"] [--run-now]
    python cli.py status
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from sqlalchemy import func, select

from config.communities import COMMUNITIES, Program
from core.database import init_db, session_scope
from core.models import Community, CommunityProgram, Employer, EmployerCommunity
from core.seed import sync_communities

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="FCIP/RCIP designated-employer job aggregator")


class ProgramOpt(StrEnum):
    FCIP = "FCIP"
    RCIP = "RCIP"
    ALL = "ALL"


def _program(p: ProgramOpt) -> Program | None:
    return None if p is ProgramOpt.ALL else Program(p.value)


@app.callback()
def main(verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)


@app.command("init-db")
def init_db_cmd() -> None:
    """Create tables (if missing) and load the community registry."""
    init_db()
    with session_scope() as s:
        result = sync_communities(s)
    typer.echo(f"Communities: {result['created']} created, {result['updated']} updated")


@app.command()
def communities() -> None:
    """List configured communities and their employer sources."""
    for c in COMMUNITIES:
        sources = ", ".join(f"{s.type}{'/' + s.program if s.program else ''}"
                            for s in c.employer_sources) or "-"
        typer.echo(f"{c.slug:<24} {c.province}  {'+'.join(c.programs):<10} sources: {sources}")


@app.command("sync-employers")
def sync_employers(
    community: Annotated[list[str] | None, typer.Option("--community", "-c")] = None,
    program: Annotated[ProgramOpt, typer.Option("--program", "-p", case_sensitive=False)] = ProgramOpt.ALL,
    from_file: Annotated[Path | None, typer.Option(
        "--from-file", exists=True, dir_okay=False,
        help="Parse a manually saved copy of the community's list (HTML/PDF/JSON) "
             "instead of fetching it, e.g. when the site blocks automated access.")] = None,
) -> None:
    """Scrape designated-employer lists and upsert them into the database."""
    from scrapers.employers.runner import run_employer_sync

    init_db()
    with session_scope() as s:
        sync_communities(s)
        summary = run_employer_sync(s, community, _program(program), from_file=from_file)

    typer.echo(f"\n{'target':<32}{'found':>7}{'new':>6}{'linked':>8}{'revoked':>9}  notes")
    for target, st in summary.items():
        notes = "; ".join(st.warnings)
        typer.echo(f"{target:<32}{st.found:>7}{st.created:>6}{st.linked:>8}{st.revoked:>9}  {notes}")


@app.command()
def employers(
    community: Annotated[str | None, typer.Option("--community", "-c")] = None,
    program: Annotated[ProgramOpt, typer.Option("--program", "-p", case_sensitive=False)] = ProgramOpt.ALL,
) -> None:
    """Show designated employers stored in the database."""
    with session_scope() as s:
        q = (select(Employer.canonical_name, Community.slug, CommunityProgram.program,
                    EmployerCommunity.status)
             .join(EmployerCommunity, EmployerCommunity.employer_id == Employer.id)
             .join(CommunityProgram).join(Community)
             .order_by(Community.slug, CommunityProgram.program, Employer.canonical_name))
        if community:
            q = q.where(Community.slug == community)
        if (p := _program(program)) is not None:
            q = q.where(CommunityProgram.program == p)
        rows = s.execute(q).all()
        for name, slug, prog, status in rows:
            typer.echo(f"{slug:<24} {prog:<5} {status:<10} {name}")
        total = s.scalar(select(func.count(Employer.id)))
    typer.echo(f"\n{len(rows)} designations shown, {total} employers in database")


class JobSourceOpt(StrEnum):
    JOB_BANK = "job_bank"
    CAREER_SITE = "career_site"
    EXTERNAL = "external"
    ALL = "all"


@app.command("sync-jobs")
def sync_jobs(
    community: Annotated[list[str] | None, typer.Option("--community", "-c")] = None,
    source: Annotated[list[JobSourceOpt] | None, typer.Option(
        "--source", "-s", help="job_bank, career_site, external or all (repeatable)")] = None,
    max_details: Annotated[int, typer.Option(
        help="Max Job Bank detail pages per run (5 s crawl delay each)")] = 300,
    no_details: Annotated[bool, typer.Option("--no-details", help="Skip Job Bank detail pages")] = False,
    career_limit: Annotated[int | None, typer.Option(help="Max employer career sites to check")] = None,
) -> None:
    """Aggregate postings from Job Bank and employer career sites, linked to designated employers."""
    from scrapers.jobs.runner import ALL_SOURCES, JobSyncOptions, run_job_sync

    chosen = {s.value for s in (source or [JobSourceOpt.JOB_BANK, JobSourceOpt.CAREER_SITE])}
    sources = ALL_SOURCES if "all" in chosen else tuple(s for s in ALL_SOURCES if s in chosen)
    opts = JobSyncOptions(sources=sources, fetch_details=not no_details,
                          max_details=max_details, career_limit=career_limit)
    init_db()
    with session_scope() as s:
        sync_communities(s)
        summary = run_job_sync(s, community, opts)

    typer.echo(f"\n{'target':<34}{'found':>7}{'local':>7}{'new':>6}{'match':>7}{'review':>8}{'closed':>8}  notes")
    for target, st in summary.items():
        typer.echo(f"{target:<34}{st.found:>7}{st.in_scope:>7}{st.created:>6}{st.matched:>7}"
                   f"{st.needs_review:>8}{st.deactivated:>8}  {'; '.join(st.warnings)}")


@app.command("refresh-details")
def refresh_details(
    limit: Annotated[int | None, typer.Option(help="Max detail pages (5 s each)")] = None,
    all_postings: Annotated[bool, typer.Option(
        "--all", help="Re-read every active matched/review posting, not only those without an expiry date")] = False,
) -> None:
    """Re-read Job Bank detail pages of stored postings: fills 'Advertised until',
    NOC/language/terms, and closes postings that were removed. No searches."""
    from scrapers.jobs.runner import run_refresh_details

    with session_scope() as s:
        st = run_refresh_details(s, limit=limit, all_postings=all_postings)
    typer.echo(f"checked {st.found}, updated {st.updated}, closed {st.deactivated}")


@app.command()
def rematch() -> None:
    """Re-link stored postings to designated employers (no network; run after sync-employers)."""
    from core.job_sync import rematch_jobs

    with session_scope() as s:
        result = rematch_jobs(s)
    typer.echo(", ".join(f"{k}: {v}" for k, v in result.items()))


@app.command()
def jobs(
    community: Annotated[str | None, typer.Option("--community", "-c")] = None,
    status: Annotated[str, typer.Option(help="matched, needs_review, unmatched or all")] = "matched",
    limit: Annotated[int, typer.Option()] = 50,
) -> None:
    """Show active job postings stored in the database (with days up and freshness flags)."""
    from core.freshness import assess_posting
    from core.models import JobPosting, MatchStatus

    with session_scope() as s:
        q = (select(JobPosting, Employer.canonical_name, Community.slug)
             .outerjoin(Employer, Employer.id == JobPosting.employer_id)
             .outerjoin(Community, Community.id == JobPosting.community_id)
             .where(JobPosting.is_active.is_(True))
             .order_by(JobPosting.posted_at.desc().nulls_last()))
        if community:
            q = q.where(Community.slug == community)
        if status != "all":
            q = q.where(JobPosting.match_status == MatchStatus(status))
        rows = s.execute(q.limit(limit)).all()
        for post, emp, slug in rows:
            fresh = assess_posting(post)
            date = post.posted_at.date().isoformat() if post.posted_at else "          "
            age = f"{fresh.days_up:>3}d" if fresh.days_up is not None else "   ?"
            flag = "" if fresh.flag in ("ok", "no expiry date") else f" [{fresh.flag}]"
            noc = f"NOC {post.noc_code}/T{post.teer_category}" if post.noc_code else ""
            typer.echo(f"{date} {age} {slug or '-':<22} {post.title[:36]:<36} | {(emp or post.employer_name_raw or '')[:28]:<28}"
                       f" | {post.city or ''} {noc} {post.language_req or ''} [{post.source}]{flag}")
    typer.echo(f"\n{len(rows)} postings shown")


class FormatOpt(StrEnum):
    CSV = "csv"
    XLSX = "xlsx"
    PDF = "pdf"


class LangOpt(StrEnum):
    FR = "fr"
    EN = "en"
    ALL = "all"


@app.command()
def export(
    fmt: Annotated[list[FormatOpt] | None, typer.Option(
        "--format", "-f", case_sensitive=False, help="csv, xlsx or pdf (repeatable)")] = None,
    program: Annotated[ProgramOpt, typer.Option("--program", "-p", case_sensitive=False)] = ProgramOpt.ALL,
    community: Annotated[str | None, typer.Option("--community", "-c")] = None,
    lang: Annotated[LangOpt, typer.Option("--lang", "-l", case_sensitive=False)] = LangOpt.ALL,
    include_review: Annotated[bool, typer.Option(
        "--include-review", help="Also export postings whose employer match needs review")] = False,
    max_age: Annotated[int | None, typer.Option(
        "--max-age", help="Only postings up for at most this many days")] = None,
    stale_days: Annotated[int, typer.Option(
        "--stale-days", help="Flag postings older than this as stale")] = 60,
    output_dir: Annotated[Path | None, typer.Option("--output-dir", "-o", file_okay=False)] = None,
) -> None:
    """Export active postings + employer directory + summary as CSV / XLSX / PDF.

    Postings past their 'Advertised until' date are never exported; the
    Freshness column flags stale / unverified / expiring-soon postings."""
    from core.exporter import ReportFilter
    from core.exporter import export as run_export

    try:
        flt = ReportFilter(program=program.value, community=community, lang=lang.value,
                           include_review=include_review, max_age=max_age, stale_days=stale_days)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    formats = [f.value for f in (fmt or [FormatOpt.XLSX])]
    with session_scope() as s:
        paths = run_export(s, formats, flt, output_dir)
    for path in paths:
        typer.echo(f"wrote {path}")


_CRON_DOW = {"0": "sun", "1": "mon", "2": "tue", "3": "wed", "4": "thu", "5": "fri", "6": "sat", "7": "sun"}


def cron_trigger(expr: str):
    """Standard 5-field crontab -> APScheduler CronTrigger.

    APScheduler 3's `CronTrigger.from_crontab` numbers weekdays 0=Monday, unlike
    cron's 0/7=Sunday, 1=Monday ("0 4 * * 1" would fire on Tuesdays). Numeric
    day-of-week values are therefore translated to names first.
    """
    import re as _re

    from apscheduler.triggers.cron import CronTrigger

    fields = expr.split()
    if len(fields) != 5:
        raise typer.BadParameter(f"expected 5 crontab fields, got {expr!r}")
    minute, hour, day, month, dow = fields
    dow = _re.sub(r"\d", lambda m: _CRON_DOW[m.group(0)], dow)
    return CronTrigger(minute=minute, hour=hour, day=day, month=month, day_of_week=dow)


@app.command()
def schedule(
    jobs_cron: Annotated[str, typer.Option(help="Crontab for sync-jobs + export")] = "0 6 * * *",
    employers_cron: Annotated[str, typer.Option(help="Crontab for sync-employers")] = "0 4 * * 1",
    formats: Annotated[str, typer.Option(help="Comma-separated export formats after each job sync")] = "xlsx,pdf",
    max_details: Annotated[int, typer.Option()] = 300,
    run_now: Annotated[bool, typer.Option("--run-now", help="Run both tasks once at startup")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print the schedule and exit")] = False,
) -> None:
    """Run syncs and exports on a schedule (APScheduler, blocking; Ctrl+C to stop).

    Defaults: employers every Monday 04:00, jobs + export daily 06:00 (local time).
    """
    from apscheduler.schedulers.blocking import BlockingScheduler

    from core.exporter import ReportFilter
    from core.exporter import export as run_export
    from scrapers.employers.runner import run_employer_sync
    from scrapers.jobs.runner import JobSyncOptions, run_job_sync

    log = logging.getLogger("scheduler")
    fmts = [f.strip() for f in formats.split(",") if f.strip()]

    def employers_task():
        log.info("scheduled sync-employers starting")
        init_db()
        with session_scope() as s:
            sync_communities(s)
            run_employer_sync(s)
        log.info("scheduled sync-employers done")

    def jobs_task():
        log.info("scheduled sync-jobs starting")
        init_db()
        with session_scope() as s:
            run_job_sync(s, None, JobSyncOptions(max_details=max_details))
        with session_scope() as s:
            for path in run_export(s, fmts, ReportFilter()):
                log.info("exported %s", path)
        log.info("scheduled sync-jobs done")

    sched = BlockingScheduler()
    # max_instances=1 + coalesce: a slow run is never started twice concurrently.
    sched.add_job(employers_task, cron_trigger(employers_cron), id="employers",
                  max_instances=1, coalesce=True, misfire_grace_time=3600)
    sched.add_job(jobs_task, cron_trigger(jobs_cron), id="jobs",
                  max_instances=1, coalesce=True, misfire_grace_time=3600)
    if run_now:
        employers_task()
        jobs_task()
    for job in sched.get_jobs():
        typer.echo(f"scheduled {job.id}: next run {job.trigger.get_next_fire_time(None, datetime.now(timezone.utc).astimezone())}")
    if dry_run:
        return
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        typer.echo("scheduler stopped")


@app.command()
def status() -> None:
    """Recent pipeline runs and database totals."""
    from core.models import JobPosting, MatchStatus, ScrapeRun

    with session_scope() as s:
        typer.echo(f"employers: {s.scalar(select(func.count(Employer.id)))}  "
                   f"designations: {s.scalar(select(func.count(EmployerCommunity.id)))}")
        for st in MatchStatus:
            n = s.scalar(select(func.count(JobPosting.id)).where(
                JobPosting.is_active.is_(True), JobPosting.match_status == st))
            typer.echo(f"active postings {st.value:<13} {n}")
        typer.echo("\nlast runs:")
        for run in s.scalars(select(ScrapeRun).order_by(ScrapeRun.id.desc()).limit(15)):
            typer.echo(f"  {run.started_at:%Y-%m-%d %H:%M} {run.task_name:<24} {run.target or '':<26} "
                       f"{run.status.value:<8} found={run.records_found} {(run.error_message or '')[:70]}")


if __name__ == "__main__":
    app()
