"""profile.toml validation: each mistake gets one line naming the key and the fix."""
import pytest

from conftest import TEMPLATE, write_profile
from kit import profile, weather


@pytest.mark.parametrize("find,replace,key", [
    ('name = "Alex"', 'name = ""', "name"),
    ('"America/Denver"', '"Mars/Olympus"', "timezone"),
    ('units = "imperial"', 'units = "furlongs"', "units"),
    ('morning = "07:30"', 'morning = "7:30"', "times.morning"),
    ('evening = "21:00"', 'evening = "21:03"', "times.evening"),
    ("lat = 40.0", "lat = 91.0", "location.lat"),
    ("lon = -105.0", "lon = -181.0", "location.lon"),
    ('sleep_target_bed = "22:30"', 'sleep_target_bed = "10pm"', "recap.sleep_target_bed"),
    ('week_starts = "monday"', 'week_starts = "friday"', "week_starts"),
])
def test_each_bad_value_names_the_key_and_the_fix(tmp_path, find, replace, key):
    """A typo in profile.toml must say which key and how to fix it, not crash later."""
    path = write_profile(tmp_path / "bad.toml", **{find: replace})
    with pytest.raises(profile.ProfileError) as e:
        profile.load(path)
    lines = str(e.value).splitlines()
    assert len(lines) == 1 and key in lines[0] and "Fix:" in lines[0]


def test_valid_profile_loads():
    """The test profile is valid and gets defaults filled in."""
    p = profile.load()
    assert p["name"] == "Alex" and p["features"]["weather"] is True


def test_template_profile_only_needs_a_name():
    """The template (a copy, never the live profile.toml) fails on the empty name and nothing else."""
    with pytest.raises(profile.ProfileError) as e:
        profile.load(TEMPLATE)
    assert str(e.value).splitlines() == [l for l in str(e.value).splitlines() if "name is empty" in l]


def test_dry_run_falls_back_to_demo_profile(monkeypatch):
    """A dry run works before setup has filled in the profile."""
    monkeypatch.setenv("KIT_DRY_RUN", "1")
    assert profile.load(TEMPLATE)["name"] == profile.DEMO["name"]


def test_weather_skipped_at_zero_zero(tmp_path):
    """lat = lon = 0 means 'no location yet': weather is skipped, not an error."""
    p = profile.load(write_profile(tmp_path / "z.toml", **{"lat = 40.0": "lat = 0.0", "lon = -105.0": "lon = 0.0"}))
    assert weather.wanted(p) is False
    assert weather.forecast(p) is None


def test_setup_missing_is_empty_dict(monkeypatch, kit_env):
    """No setup.json yet is {} (runtime code then says which setup step is missing)."""
    monkeypatch.setattr(profile, "SETUP", kit_env / "nope.json")
    assert profile.setup() == {}


def test_unknown_metric_is_a_warning_not_an_error(tmp_path, capsys):
    """After an update renames a metric, the old name must warn (naming valid ones), never stop recaps."""
    path = write_profile(tmp_path / "m.toml")
    path.write_text(path.read_text() + "\n[metrics]\nhrv = false\nstres = false\n")
    p = profile.load(path)
    err = capsys.readouterr().err
    assert "stres" in err and "stress" in err and "body_battery" in err
    assert "stres" not in p["metrics"] and p["metrics"]["hrv"] is False and p["metrics"]["stress"] is True


def test_metric_must_be_true_or_false(tmp_path):
    """'hrv = "no"' is an error, not a truthy string that leaves HRV on."""
    path = write_profile(tmp_path / "m.toml")
    path.write_text(path.read_text() + '\n[metrics]\nhrv = "no"\n')
    with pytest.raises(profile.ProfileError, match="metrics.hrv must be true or false"):
        profile.load(path)


def test_missing_metrics_section_means_all_on(tmp_path):
    """Profiles from before v0.2 have no [metrics]: everything stays on."""
    p = profile.load(write_profile(tmp_path / "old.toml"))
    assert all(p["metrics"].values()) and len(p["metrics"]) == 14 and p["devices"]["wears"] == ""
