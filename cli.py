"""Command-line interface.

    python cli.py init-db
    python cli.py communities
    python cli.py sync-employers [--community SLUG ...] [--program FCIP|RCIP]
    python cli.py sync-employers --community sudbury --from-file saved_page.html
    python cli.py employers [--community SLUG] [--program FCIP|RCIP]
"""

from __future__ import annotations

import logging
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


if __name__ == "__main__":
    app()
