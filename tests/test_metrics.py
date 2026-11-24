"""Metric switches and sparse accounts: missing data is never an error, a switched-off metric never shows."""
import json
import re

import pytest

from kit import cards, commands, fetch, garmin, metrics, numbers, profile, recap

OFF = ["sleep_stages", "hrv", "body_battery", "stress", "steps", "spo2", "vo2max",
       "training_load", "training_effect", "hr_zones"]
NEVER = re.compile(r"\bNone\b|no data|n/a|null", re.I)


def test_metrics_table_names():
    """setup/kit.py writes [metrics] with these exact names; renaming one breaks every profile."""
    assert metrics.NAMES == ("sleep", "sleep_stages", "sleep_score", "hrv", "resting_hr", "body_battery", "stress",
                             "steps", "respiration", "spo2", "vo2max", "training_load", "training_effect", "hr_zones")
    for name, (label, source, extract) in metrics.METRICS.items():
        assert label and source and callable(extract)


@pytest.mark.parametrize("folder", ["garmin-sparse", "garmin-empty"])
def test_sparse_and_empty_dry_runs_are_clean(folder, monkeypatch, kit_env, capsys, no_sockets):
    """A basic watch or a brand-new account still gets short recaps, cards and answers: no crash, no 'None'."""
    monkeypatch.setenv("KIT_DRY_RUN", "1")
    monkeypatch.setenv("KIT_DRY_FIXTURES", folder)
    assert recap.main(["morning"]) == 0 and recap.main(["evening"]) == 0
    assert cards.run() == 0
    for c in ("today", "week", "last", "status"):
        assert commands.main([c, ""]) == 0
    out = capsys.readouterr().out
    assert not NEVER.search(out), NEVER.search(out)
    for absent in ("HRV", "Body Battery", "stress", "SpO2", "Zones", "Effect", "load", "VO2"):
        assert absent not in out, absent
    assert "<b>Morning, Alex.</b>" in out and "<b>Evening, Alex.</b>" in out


@pytest.fixture
def trimmed(monkeypatch, kit_env):
    """A profile with ten metrics switched off, over the full fixtures (where all of them have data)."""
    text = (kit_env / "profile.toml").read_text(encoding="utf-8")
    text += "\n[metrics]\n" + "".join(f"{n} = false\n" for n in OFF)
    path = kit_env / "trimmed.toml"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setenv("KIT_PROFILE", str(path))
    p = profile.load()
    assert metrics.enabled(p) == set(metrics.NAMES) - set(OFF)
    return p


def test_switched_off_metrics_never_reach_the_recap(trimmed):
    """'Drop stress' must mean Claude never even sees stress."""
    data = fetch.collect(garmin.DryRunClient(), profile.today())
    m = numbers.morning(data, trimmed, profile.today())
    e = numbers.evening(data, trimmed, profile.today())
    for key in ("hrv", "body_battery", "stress_avg_yesterday", "steps_yesterday", "vo2max"):
        assert key not in m, key
    assert "steps_today" not in e
    assert "stages" not in m["sleep"] and "spo2_pct" not in m["sleep"]
    assert m["sleep"]["score"] == 81 and m["resting_hr"]["today"] == 51       # what's on still shows
    text = recap.fallback("morning", m) + recap.fallback("evening", e)
    assert not re.search(r"HRV|Body Battery|stress|steps|SpO2|VO2|deep", text, re.I)


def test_switched_off_metrics_never_reach_cards_or_commands(trimmed, sent, monkeypatch):
    """Cards and command answers leave out what was switched off."""
    monkeypatch.setattr(garmin, "connect", lambda: garmin.DryRunClient())
    newest = fetch.slim(garmin.DryRunClient().latest_activities(1)[0])
    card = cards.card(newest, trimmed["units"], metrics.enabled(trimmed))
    assert "HR 144 avg" in card
    assert not re.search(r"Zones|Effect|load|VO2", card)
    for c in ("today", "last"):
        commands.main([c, ""])
    answers = " ".join(t for _, t in sent)
    assert "Last night" in answers and not re.search(r"Steps|Zones|Effect|load|VO2", answers)


def test_switched_off_metrics_never_reach_mcp(trimmed, monkeypatch):
    """The MCP tools leave switched-off metrics out of raw Garmin responses too."""
    mcp_server = pytest.importorskip("kit.mcp_server")
    monkeypatch.setattr(mcp_server, "_client", garmin.DryRunClient())
    d = "2026-01-14"
    assert "switched off" in json.loads(mcp_server.get_hrv(d))["note"]
    summary = mcp_server.get_daily_summary(d)
    assert "restingHeartRate" in summary
    assert not re.search(r"bodyBattery|averageStressLevel|totalSteps", summary)
    sleep = mcp_server.get_sleep(d)
    assert "sleepScores" in sleep and not re.search(r"deepSleepSeconds|SpO2|sleepLevels", sleep)
    acts = mcp_server.list_activities("2026-01-08", d)
    assert "Morning Run" in acts
    assert not re.search(r"hrTimeInZone|TrainingEffect|activityTrainingLoad|vO2Max|\"zones\"|\"effect\"|\"load\"", acts)
    week = json.loads(mcp_server.weekly_rollup("2026-01-12"))
    assert "avg_hrv_ms" not in week and "avg_resting_hr" in week


def test_mcp_without_profile_shows_everything(monkeypatch, kit_env):
    """No profile (uvx) means all metrics on, not none."""
    mcp_server = pytest.importorskip("kit.mcp_server")
    monkeypatch.delenv("KIT_PROFILE")
    monkeypatch.setattr(profile, "PROFILE", kit_env / "missing.toml")
    monkeypatch.setattr(mcp_server, "_client", garmin.DryRunClient())
    assert "lastNightAvg" in mcp_server.get_hrv("2026-01-14")


def test_full_morning_has_the_new_sleep_and_vo2_lines():
    """With everything on and present, stages, breathing, SpO2 and VO2 max reach the context."""
    data = fetch.collect(garmin.DryRunClient(), profile.today())
    m = numbers.morning(data, profile.load(), profile.today())
    assert m["sleep"]["stages"]["deep"] == "1h 26m"
    assert m["sleep"]["breathing_per_min"] == 14.5 and m["sleep"]["spo2_pct"] == 95
    assert m["vo2max"] == 52


def test_sleep_off_strips_raw_sleep_times_from_mcp(monkeypatch, kit_env):
    """sleep = false must remove bed/wake timestamps and hours from the raw MCP record too."""
    mcp_server = pytest.importorskip("kit.mcp_server")
    path = kit_env / "nosleep.toml"
    path.write_text((kit_env / "profile.toml").read_text(encoding="utf-8") + "\n[metrics]\nsleep = false\n",
                    encoding="utf-8")
    monkeypatch.setenv("KIT_PROFILE", str(path))
    monkeypatch.setattr(mcp_server, "_client", garmin.DryRunClient())
    out = mcp_server.get_sleep("2026-01-14")
    assert "sleepScores" in out                                   # score is still on
    assert not re.search(r"sleepStartTimestamp|sleepEndTimestamp|sleepTimeSeconds|\"summary\"", out)
