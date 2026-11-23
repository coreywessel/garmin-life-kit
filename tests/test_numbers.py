"""Numbers the recaps show: sleep target across midnight, units, and missing data left out."""
from datetime import date, datetime, timezone

import pytest

from kit import garmin, fetch, numbers, profile


@pytest.mark.parametrize("bed,target,minutes,text", [
    ("00:15", "22:30", 105, "1h 45m after target"),
    ("22:00", "22:30", -30, "30m before target"),
    ("23:45", "00:30", -45, "45m before target"),
    ("01:00", "23:00", 120, "2h 00m after target"),
    ("22:30", "22:30", 0, "at target"),
])
def test_sleep_target_distance_across_midnight(bed, target, minutes, text):
    """Going to bed after midnight is later than a 22:30 target, not 22 hours early."""
    diff = numbers.minutes_vs_target(numbers.hhmm(bed), numbers.hhmm(target))
    assert diff == minutes
    assert numbers.vs_target_text(diff) == text


def sleep_rec(day, bed_day, bed, wake):
    ms = lambda d, t: int(datetime(d.year, d.month, d.day, int(t[:2]), int(t[3:]), tzinfo=timezone.utc).timestamp() * 1000)
    return {"sleepStartTimestampLocal": ms(bed_day, bed), "sleepEndTimestampLocal": ms(day, wake),
            "sleepTimeSeconds": 25200}


def test_average_bed_time_across_midnight():
    """11:30 pm and 12:30 am average to midnight, not noon."""
    p = profile.load()
    d14, d13, d12 = date(2026, 1, 14), date(2026, 1, 13), date(2026, 1, 12)
    data = {"days": {"2026-01-14": {"sleep": sleep_rec(d14, d14, "00:30", "07:30")},
                     "2026-01-13": {"sleep": sleep_rec(d13, d12, "23:30", "06:30")}}}
    s = numbers.sleep_block(data, p, d14)
    assert s["avg_bed_7"] == "12:00 am"
    assert s["bed"] == "12:30 am" and s["vs_target"] == "2h 00m after target"
    assert s["nights_in_avg"] == 2


@pytest.mark.parametrize("fn,arg,imperial,metric", [
    (profile.dist, 5000, "3.11 mi", "5.00 km"),
    (profile.elev, 100, "328 ft", "100 m"),
    (profile.speed, 10, "22 mph", "36 km/h"),
    (profile.temp, 0, "32°F", "0°C"),
    (profile.precip, 25.4, "1.00 in", "25 mm"),
])
def test_units_imperial_vs_metric(fn, arg, imperial, metric):
    """Every shown number follows the profile's units."""
    assert fn(arg, "imperial") == imperial
    assert fn(arg, "metric") == metric


def test_pace_per_mile_and_per_km():
    """Pace is per mile in imperial and per km in metric; a standstill has no pace."""
    assert profile.pace(1609.344 / 480, "imperial") == "8:00/mi"
    assert profile.pace(1000 / 300, "metric") == "5:00/km"
    assert profile.pace(0, "metric") is None


def test_units_come_from_the_profile(monkeypatch, kit_env):
    """With no units passed, the profile decides."""
    from conftest import write_profile
    monkeypatch.setenv("KIT_PROFILE", str(write_profile(kit_env / "m.toml", **{'"imperial"': '"metric"'})))
    assert profile.dist(1000) == "1.00 km"


def test_missing_garmin_means_keys_absent_not_guessed():
    """No Garmin data must give no sleep/HRV numbers at all, plus the plan hook as None."""
    ctx = numbers.morning({}, profile.load(), date(2026, 1, 14))
    assert set(ctx.get("sleep", {})) == {"target_bed"}         # the target is the profile's, not Garmin's
    assert "hrv" not in ctx and "resting_hr" not in ctx and "week" not in ctx
    assert ctx["garmin"] == "unavailable today"
    assert "plan" in ctx and ctx["plan"] is None


def test_garmin_minus_one_is_missing():
    """Garmin writes -1 for 'not enough data'; it must never show as a number."""
    data = {"days": {"2026-01-13": {"summary": {"averageStressLevel": -1, "totalSteps": 4000}}}, "activities": []}
    ctx = numbers.morning(data, profile.load(), date(2026, 1, 14))
    assert "stress_avg_yesterday" not in ctx
    assert ctx["steps_yesterday"] == "4,000"


def test_morning_numbers_from_fixtures():
    """The fixture morning: HRV and resting HR against their 7-day averages, yesterday's activities."""
    data = fetch.collect(garmin.DryRunClient(), date(2026, 1, 14))
    ctx = numbers.morning(data, profile.load(), date(2026, 1, 14))
    assert ctx["hrv"] == {"last_night_ms": 52, "avg_7_ms": 48, "garmin_status": "balanced", "baseline_ms": "44-58"}
    assert ctx["resting_hr"] == {"today": 51, "avg_7": 53}
    assert [a["name"] for a in ctx["yesterday_activities"]] == ["Strength", "Evening Ride"]
    assert ctx["week"]["count"] == 4 and ctx["week"]["distance_by_sport"]["running"] == "8.20 mi"


def test_week_starts_sunday_or_monday():
    """/week and week totals follow week_starts."""
    wed = date(2026, 1, 14)
    assert numbers.week_start(wed, {"week_starts": "monday"}) == date(2026, 1, 12)
    assert numbers.week_start(wed, {"week_starts": "sunday"}) == date(2026, 1, 11)
    sun = date(2026, 1, 18)
    assert numbers.week_start(sun, {"week_starts": "sunday"}) == sun
