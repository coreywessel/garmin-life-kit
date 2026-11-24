"""
profile.toml (about the person) and setup.json (ids written by setup), plus unit helpers.

Every "today" in the kit comes from today() here, in the profile's time zone, so a run
just after midnight UTC never files the evening under the wrong day.

Env overrides (for dry runs and tests, never needed in normal use):
    KIT_PROFILE   path to a profile.toml to use instead of the repo's
    KIT_DATA_DIR  where runtime state goes (default data/, or out/dry-run in a dry run)
    KIT_TODAY     YYYY-MM-DD to use as today (a dry run defaults to the fixture day)
"""
import json
import os
import re
import sys
import tomllib
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from kit.metrics import NAMES as METRIC_NAMES

ROOT = Path(__file__).resolve().parent.parent
PROFILE = ROOT / "profile.toml"
SETUP = ROOT / "setup.json"
OUT = ROOT / "out"
FIXTURE_TODAY = "2026-01-14"          # the "today" of tests/fixtures/garmin (a Wednesday)
HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
FIX = "Fix: ask Claude to correct it in profile.toml."

# Used only in a dry run when profile.toml isn't filled in yet, so a dry run works before setup.
DEMO = {
    "name": "Sam", "timezone": "America/Denver", "units": "imperial", "week_starts": "monday",
    "location": {"label": "Denver, CO", "lat": 39.74, "lon": -104.99},
    "times": {"morning": "07:30", "evening": "21:00"},
    "features": {"morning_recap": True, "evening_recap": True, "activity_cards": True,
                 "weather": True, "log_notes": True, "watchdog": True},
    "recap": {"sleep_target_bed": "22:30", "about": "I snowboard most weekends in winter."},
    "devices": {"wears": "a Garmin watch"},
}
# Fake ids so a dry run can go through every send path with no setup.json.
FAKE_SETUP = {
    "repo": "you/garmin-life-kit",
    "telegram": {"chat_id": -1000000000001, "owner_id": 1, "bot_username": "dry_run_bot",
                 "topics": {"today": 2, "fitness": 3, "log": 4, "alerts": 5}},
    "worker": {"name": "garmin-life-kit", "url": "https://garmin-life-kit.example.workers.dev", "kv_id": ""},
    "expiries": [{"name": "Claude token (CLAUDE_CODE_OAUTH_TOKEN)", "expires": "2026-02-13",
                  "breaks": "the recaps (Claude can't write them)",
                  "fix": "run `claude setup-token`, then ask Claude to update the secret"}],
}
FEATURES = ("morning_recap", "evening_recap", "activity_cards", "weather", "log_notes", "watchdog")


class ProfileError(ValueError):
    """profile.toml is missing or has a bad value. The message has one line per problem, with the fix."""


def dry():
    return os.environ.get("KIT_DRY_RUN") == "1"


def data_dir():
    """Runtime state. A dry run never writes into the real data/ folder."""
    if os.environ.get("KIT_DATA_DIR"):
        return Path(os.environ["KIT_DATA_DIR"])
    return OUT / "dry-run" if dry() else ROOT / "data"


def validate(p):
    """One plain line per problem: the key, what's wrong, the fix."""
    errs = []
    if not str(p.get("name", "")).strip():
        errs.append(f'name is empty. Put your first name in name = "". {FIX}')
    tzname = p.get("timezone", "")
    try:
        ZoneInfo(str(tzname))
    except (ZoneInfoNotFoundError, ValueError):
        errs.append(f'timezone "{tzname}" is not a time zone name. Use one like "America/Denver" or "Europe/London". {FIX}')
    if p.get("units") not in ("imperial", "metric"):
        errs.append(f'units must be "imperial" or "metric", not "{p.get("units")}". {FIX}')
    if p.get("week_starts", "monday") not in ("monday", "sunday"):
        errs.append(f'week_starts must be "monday" or "sunday", not "{p.get("week_starts")}". {FIX}')
    for k in ("morning", "evening"):
        t = str((p.get("times") or {}).get(k, ""))
        m = HHMM.match(t)
        if not m or int(m.group(2)) % 5:
            errs.append(f'times.{k} "{t}" must be 24h "HH:MM" on a 5-minute mark, like "07:30". {FIX}')
    loc = p.get("location") or {}
    try:
        lat, lon = float(loc.get("lat", 0)), float(loc.get("lon", 0))
        if not -90 <= lat <= 90:
            errs.append(f"location.lat {lat} must be between -90 and 90. {FIX}")
        if not -180 <= lon <= 180:
            errs.append(f"location.lon {lon} must be between -180 and 180. {FIX}")
    except (TypeError, ValueError):
        errs.append(f"location.lat and location.lon must be numbers. {FIX}")
    metrics = p.get("metrics") or {}
    if not isinstance(metrics, dict):
        errs.append(f"[metrics] must be a section of name = true / false lines. {FIX}")
        metrics = {}
    for k, v in metrics.items():
        if k in METRIC_NAMES and not isinstance(v, bool):
            errs.append(f"metrics.{k} must be true or false, not {v!r}. {FIX}")
    bed = str((p.get("recap") or {}).get("sleep_target_bed", "") or "")
    if bed and not HHMM.match(bed):
        errs.append(f'recap.sleep_target_bed "{bed}" must be empty or 24h "HH:MM", like "22:30". {FIX}')
    return errs


def unknown_metrics(p):
    """Names under [metrics] the kit doesn't know. Only a warning: after an update renames or drops
    a metric, an old name must never stop the recaps."""
    m = p.get("metrics")
    return sorted(set(m) - set(METRIC_NAMES)) if isinstance(m, dict) else []


def _defaults(p):
    p = dict(p)
    p.setdefault("week_starts", "monday")
    p["location"] = {"label": "", "lat": 0.0, "lon": 0.0, **(p.get("location") or {})}
    p["features"] = {**{f: True for f in FEATURES}, **(p.get("features") or {})}
    p["recap"] = {"sleep_target_bed": "", "about": "", **(p.get("recap") or {})}
    p["recap"]["about"] = (p["recap"]["about"] or "").strip()
    p["devices"] = {"wears": "", **(p.get("devices") or {})}
    # A missing [metrics] section or key means on, so profiles from before v0.2 keep working.
    p["metrics"] = {n: (p.get("metrics") or {}).get(n, True) for n in METRIC_NAMES}
    return p


_cache = {}


def load(path=None):
    """The validated profile as a dict. Raises ProfileError listing every problem.
    In a dry run an unfilled profile falls back to the demo one, so a dry run needs no setup."""
    path = Path(path or os.environ.get("KIT_PROFILE") or PROFILE)
    try:
        stamp = (str(path), path.stat().st_mtime_ns, dry())
    except OSError:
        stamp = None
    if stamp and stamp in _cache:
        return _cache[stamp]
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        errs = validate(raw)
    except OSError:
        raw, errs = {}, [f"{path.name} is missing. Run setup, or copy it back from the template repo."]
    except tomllib.TOMLDecodeError as e:
        raw, errs = {}, [f"{path.name} isn't valid TOML ({e}). {FIX}"]
    if errs:
        if not dry():
            raise ProfileError("\n".join(f"profile.toml: {e}" for e in errs))
        print("dry run: profile.toml isn't filled in yet, using the demo profile", file=sys.stderr)
        raw = DEMO
    unknown = unknown_metrics(raw)
    if unknown:
        print(f"WARNING: profile.toml [metrics] has unknown name(s) {', '.join(unknown)} - ignored. "
              f"Valid names: {', '.join(METRIC_NAMES)}.", file=sys.stderr)
    p = _defaults(raw)
    if stamp:
        _cache[stamp] = p
    return p


def setup():
    """setup.json as a dict, {} when setup hasn't written it yet. A dry run uses fake ids."""
    if dry():
        return FAKE_SETUP
    try:
        return json.loads(SETUP.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def tz():
    return ZoneInfo(load()["timezone"])


def now():
    """Local now. With KIT_TODAY (or in a dry run) the date is pinned and the clock reads noon."""
    pinned = os.environ.get("KIT_TODAY") or (FIXTURE_TODAY if dry() else None)
    if pinned:
        return datetime.combine(date.fromisoformat(pinned), datetime.min.time().replace(hour=12), tz())
    return datetime.now(tz())


def today():
    return now().date()


def fail(msg):
    """Stop the run with a plain-words reason. out/error.txt lets the workflow's failure alert
    say what broke instead of only linking the log."""
    print(f"FAILED: {msg}", file=sys.stderr)
    try:
        OUT.mkdir(exist_ok=True)
        (OUT / "error.txt").write_text(msg.strip() + "\n", encoding="utf-8")
    except OSError:
        pass
    raise SystemExit(1)


# ---------- units: every number shown to the person goes through these ----------

M_PER_MI = 1609.344
FT_PER_M = 3.28084


def _imperial(units):
    return (units or load()["units"]) == "imperial"


def dist(m, units=None):
    return f"{m / M_PER_MI:.2f} mi" if _imperial(units) else f"{m / 1000:.2f} km"


def elev(m, units=None):
    return f"{m * FT_PER_M:,.0f} ft" if _imperial(units) else f"{m:,.0f} m"


def speed(mps, units=None):
    return f"{mps * 2.23694:.0f} mph" if _imperial(units) else f"{mps * 3.6:.0f} km/h"


def pace(mps, units=None):
    """Minutes per mile or km. None for a standstill (no pace to show)."""
    if not mps or mps <= 0:
        return None
    unit_m, label = (M_PER_MI, "mi") if _imperial(units) else (1000, "km")
    return f"{clock(unit_m / mps)}/{label}"


def temp(c, units=None):
    return f"{c * 9 / 5 + 32:.0f}°F" if _imperial(units) else f"{c:.0f}°C"


def precip(mm, units=None):
    return f"{mm / 25.4:.2f} in" if _imperial(units) else f"{mm:.0f} mm"


def clock(seconds):
    """42:10 or 1:05:30."""
    s = round(seconds)
    h, m, s = s // 3600, s % 3600 // 60, s % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def hours_minutes(seconds):
    """3h 05m, for totals."""
    m = round(seconds / 60)
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"


def time12(minutes):
    """Minutes after midnight -> '10:42 pm'."""
    minutes %= 1440
    h, m = divmod(minutes, 60)
    return f"{h % 12 or 12}:{m:02d} {'am' if h < 12 else 'pm'}"
