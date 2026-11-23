"""Watchdog windows and expiry warnings."""
import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from kit import expiry, profile, watchdog


def test_window_covers_the_whole_gap_between_slots():
    """With recaps at 07:30 and 21:00 the day gap is 13.5 h; a fixed 13 h window would miss runs."""
    p = profile.load()
    assert watchdog.window_hours(p, "evening") == 14.0
    assert watchdog.window_hours(p, "morning") == 11.0


def at(hhmm, day="2026-01-14"):
    return datetime.fromisoformat(f"{day}T{hhmm}").replace(tzinfo=ZoneInfo("America/Denver"))


def receipt(**slots):
    profile.data_dir().mkdir(parents=True, exist_ok=True)
    (profile.data_dir() / "receipts.json").write_text(json.dumps(slots))


def test_missing_receipt_is_a_problem():
    """No receipt for today's slot is reported in Alerts with where to look."""
    problem = watchdog.receipt_problem("morning", at("08:15"), profile.load())
    assert problem and "No morning recap sent today" in problem
    receipt(morning="2026-01-14")
    assert watchdog.receipt_problem("morning", at("08:15"), profile.load()) is None


def test_late_evening_watchdog_after_midnight_accepts_last_nights_receipt(monkeypatch, kit_env):
    """Evening at 23:30 puts its watchdog at 00:15 the next day; last night's recap must count."""
    from conftest import write_profile
    p = profile.load(write_profile(kit_env / "late.toml", **{'evening = "21:00"': 'evening = "23:30"'}))
    receipt(evening="2026-01-14")
    assert watchdog.receipt_problem("evening", at("00:15", "2026-01-15"), p) is None
    # Once the evening time has passed, yesterday's receipt no longer counts.
    assert watchdog.receipt_problem("evening", at("23:50", "2026-01-15"), p)


def test_cancelled_runs_are_not_failures():
    """GitHub replaces a waiting run in the kit-data group; that's not a failure to alert about."""
    assert "cancelled" in watchdog.OK and "failure" not in watchdog.OK


def test_no_setup_json_exits_quietly(monkeypatch, kit_env, sent, capsys):
    """The template and fresh copies must not fail (and email) every day from the GitHub cron."""
    monkeypatch.setattr(profile, "SETUP", kit_env / "missing.json")
    for slot in ("backstop", "morning", "evening"):
        assert watchdog.run(slot) == 0
    assert sent == []
    assert "isn't set up yet" in capsys.readouterr().out


@pytest.mark.parametrize("days,warn", [(31, False), (30, True), (29, False), (14, True), (7, True),
                                       (5, False), (3, True), (1, True), (0, True), (-2, True)])
def test_expiry_warning_days(days, warn):
    """Warn 30/14/7 days out and every day from 3 days, not every day for a month."""
    today = date(2026, 1, 14)
    exp = date.fromordinal(today.toordinal() + days).isoformat()
    setup = {"expiries": [{"name": "Claude token", "expires": exp, "breaks": "recaps", "fix": "new token"}]}
    assert bool(expiry.warnings(today, setup)) == warn
