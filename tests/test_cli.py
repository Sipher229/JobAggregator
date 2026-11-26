from datetime import datetime, timezone

import pytest
import typer
from typer.testing import CliRunner

import cli


@pytest.mark.parametrize("expr, weekday", [
    ("0 4 * * 1", "Mon"), ("0 4 * * 0", "Sun"), ("0 4 * * 7", "Sun"), ("0 6 * * 6", "Sat"),
    ("0 9 * * 5", "Fri"),
])
def test_cron_weekdays_follow_crontab(expr, weekday):
    thursday = datetime(2026, 11, 26, 12, tzinfo=timezone.utc)
    nxt = cli.cron_trigger(expr).get_next_fire_time(None, thursday)
    assert nxt.strftime("%a") == weekday


def test_cron_ranges_and_errors():
    trig = cli.cron_trigger("30 7 * * 1-5")
    assert "mon-fri" in str(trig)
    with pytest.raises(typer.BadParameter):
        cli.cron_trigger("0 4 * *")


def test_help_lists_all_commands():
    out = CliRunner().invoke(cli.app, ["--help"]).output
    for cmd in ("init-db", "sync-employers", "sync-jobs", "rematch", "export", "schedule", "status"):
        assert cmd in out


def test_export_rejects_unknown_community():
    result = CliRunner().invoke(cli.app, ["export", "-c", "atlantis"])
    assert result.exit_code != 0 and "unknown community" in result.output
