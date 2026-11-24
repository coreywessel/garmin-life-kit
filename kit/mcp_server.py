"""
Garmin MCP server (stdio), so the owner can ask Claude about their own data directly.

    garmin-life-mcp                     (console script from pyproject.toml)
    uv run python -m kit.mcp_server

Read-only tools: get_sleep, get_hrv, get_daily_summary, list_activities, weekly_rollup.
Dates are YYYY-MM-DD and default to today in the profile's time zone. Answers are trimmed
JSON: Garmin's per-minute arrays are replaced with a count, never passed through.

The key comes from GARMIN_PERSONAL_API_KEY, else KIT_ENV_FILE, else the one
~/.config/garmin-life-kit/*.env file that has it (written by setup). With no profile.toml to
find (run with uvx), dates use the computer's own time zone and metric units. The 24h token
is cached in the user cache folder (plain JSON, mode 0600), so asking ten questions in a row
spends one key exchange, not ten. KIT_DRY_RUN=1 answers from the fixtures.

Built on the official `mcp` package. In mcp 2.x FastMCP is called MCPServer.
"""
import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from kit import fetch, garmin, metrics, numbers, profile

MAX_LIST = 12
NOISE = re.compile(r"(?i)(gmt|uuid|^userprofilepk$|^ownerid$|^ownerdisplayname$|^userid$|privacy|version)")
ENV_DIR = Path.home() / ".config" / "garmin-life-kit"

server = MCPServer("garmin-life-mcp", instructions=(
    "Read-only access to the owner's Garmin data. Report numbers; never coach, never give health advice."))
_client = None


def cache_dir():
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "garmin-life-kit"


def env_files():
    """KIT_ENV_FILE if set, else every *.env setup wrote in ~/.config/garmin-life-kit/."""
    if os.environ.get("KIT_ENV_FILE"):
        return [Path(os.environ["KIT_ENV_FILE"]).expanduser()]
    return sorted(ENV_DIR.glob("*.env"))


def key_in(f):
    try:
        for line in f.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            if name.strip() == garmin.KEY_ENV and value.strip():
                return value.strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def find_key():
    """The env var wins; else the one env file holding the key. Two copies of the kit set up on one
    computer means two files: refuse to guess which person's data to read."""
    key = os.environ.get(garmin.KEY_ENV, "").strip()
    if key:
        return key
    found = [(f, k) for f in env_files() if (k := key_in(f))]
    if len(found) > 1:
        names = ", ".join(f.name for f, _ in found)
        raise garmin.KeyRejected(f"more than one env file has {garmin.KEY_ENV} ({names})",
                                 f"set {garmin.KEY_ENV}, or KIT_ENV_FILE to the one file to use, in this "
                                 "MCP server's env settings")
    if not found:
        where = os.environ.get("KIT_ENV_FILE") or f"{ENV_DIR}/*.env"
        raise garmin.KeyRejected(f"no {garmin.KEY_ENV} in the environment or in {where}",
                                 "ask Claude to run `uv run setup/kit.py secret GARMIN_PERSONAL_API_KEY` in the kit repo")
    return found[0][1]


def owner_profile():
    """The profile, or None when there is none to find (e.g. run with uvx, outside the repo).
    KIT_PROFILE wins. A profile that exists but is wrong is a plain-words tool error."""
    if not os.environ.get("KIT_PROFILE") and not profile.PROFILE.exists():
        return None
    try:
        return profile.load()
    except profile.ProfileError as e:
        raise ToolError(str(e)) from None


def today():
    """Today in the profile's time zone, or the computer's own zone when there's no profile."""
    if owner_profile() is not None or os.environ.get("KIT_TODAY") or profile.dry():
        return profile.today()
    return datetime.now().astimezone().date()


def client():
    global _client
    if _client is None:
        _client = garmin.DryRunClient() if profile.dry() else \
            garmin.Client(find_key(), garmin.FileCache(cache_dir() / "garmin-token.json"))
    return _client


def trim(x):
    """Drop bookkeeping fields and replace long per-minute lists with a count."""
    if isinstance(x, dict):
        return {k: trim(v) for k, v in x.items() if v is not None and not NOISE.search(k)}
    if isinstance(x, list):
        return f"[{len(x)} items dropped]" if len(x) > MAX_LIST else [trim(v) for v in x]
    return x


def day(d):
    """'' -> today in the profile's zone. Anything else must be YYYY-MM-DD."""
    if not d:
        return today()
    try:
        return date.fromisoformat(d)
    except ValueError:
        raise ToolError(f"'{d}' isn't a date. Use YYYY-MM-DD.") from None


def units():
    return (owner_profile() or {}).get("units", "metric")


def on():
    """Metrics switched on in profile.toml [metrics]. No profile = all on."""
    return metrics.enabled(owner_profile())


def off_note(name):
    return {"note": f"{name} is switched off in profile.toml [metrics]"}


def answer(fn, *args):
    """Run a Garmin call and return JSON text. Garmin errors become a ToolError, whose plain-words
    message (with the fix) is what the client sees."""
    try:
        return json.dumps(fn(*args), indent=1, ensure_ascii=False)
    except garmin.GarminError as e:
        raise ToolError(str(e)) from None


@server.tool()
def get_sleep(date: str = "") -> str:
    """Sleep for the night ending on the morning of `date` (YYYY-MM-DD, default today): bed and
    wake time, hours, sleep score, stage seconds, breathing."""
    d = day(date)

    enabled = on()
    if not enabled & {"sleep", "sleep_score", "sleep_stages", "respiration", "spo2"}:
        return json.dumps({"date": d.isoformat(), **off_note("sleep")})

    def go():
        rec = client().sleep(d)
        n = numbers.night(rec) if "sleep" in enabled else None
        out = {"date": d.isoformat(), "record": metrics.strip_off(trim(rec), enabled)}
        if n:
            out["summary"] = {"bed": profile.time12(n["bed"]), "wake": profile.time12(n["wake"]), "hours": n["hours"]}
        return out
    return answer(go)


@server.tool()
def get_hrv(date: str = "") -> str:
    """Overnight HRV for `date` (YYYY-MM-DD, default today): last-night average, weekly average,
    Garmin's status and baseline."""
    d = day(date)
    if "hrv" not in on():
        return json.dumps({"date": d.isoformat(), **off_note("hrv")})
    return answer(lambda: {"date": d.isoformat(), **trim(client().hrv(d))})


@server.tool()
def get_daily_summary(date: str = "") -> str:
    """Garmin's daily summary for `date` (YYYY-MM-DD, default today): steps, resting HR, stress,
    Body Battery, calories, distance."""
    d = day(date)
    enabled = on()
    return answer(lambda: {"date": d.isoformat(), **metrics.strip_off(trim(client().daily_summary(d)), enabled)})


@server.tool()
def list_activities(start: str = "", end: str = "") -> str:
    """Activities from `start` to `end` (YYYY-MM-DD, both default today), each with the
    fields the kit uses plus formatted distance, time and pace in the owner's units."""
    s, e = day(start), day(end)
    if e < s:
        raise ToolError("end is before start")
    if (e - s).days > 366:
        raise ToolError("ask for a year or less at a time")

    enabled, u = on(), units()

    def go():
        acts = sorted((fetch.slim(a) for a in client().activities(s, e)), key=lambda a: a.get("startTimeLocal", ""))
        return [metrics.strip_off({**a, "formatted": numbers.activity(a, u, enabled, detail=True)}, enabled)
                for a in acts]
    return answer(go)


@server.tool()
def weekly_rollup(week_start: str = "") -> str:
    """Seven days from `week_start` (YYYY-MM-DD, default the start of this week): activity count,
    time and distance per sport, and average sleep hours, HRV and resting HR."""
    p = owner_profile() or {"units": "metric", "week_starts": "monday"}
    start = day(week_start) if week_start else numbers.week_start(today(), p)
    end = start + timedelta(days=6)
    enabled = metrics.enabled(p)

    def go():
        c = client()
        acts = [fetch.slim(a) for a in c.activities(start, end)]
        by_sport, secs = {}, 0
        for a in acts:
            s = by_sport.setdefault(numbers.sport(a), {"count": 0, "seconds": 0, "meters": 0})
            s["count"] += 1
            s["seconds"] += a.get("duration") or 0
            s["meters"] += a.get("distance") or 0
            secs += a.get("duration") or 0
        hours, hrv, rhr = [], [], []
        for i in range(7):
            d = start + timedelta(days=i)
            if d > today():          # no point asking Garmin about days that haven't happened
                break
            n = numbers.night(c.sleep(d)) if "sleep" in enabled else None
            if n:
                hours.append(n["hours"])
            if "hrv" in enabled:
                hrv.append(metrics.hrv({"hrv": c.hrv(d)}))
            if "resting_hr" in enabled:
                rhr.append(metrics.resting_hr({"summary": c.daily_summary(d)}))
        avg = lambda v: round(numbers._avg(v), 1) if numbers._avg(v) is not None else None
        return numbers.prune({
            "week_start": start.isoformat(), "week_end": end.isoformat(), "activities": len(acts),
            "time": profile.hours_minutes(secs) if acts else None,
            "by_sport": {k: {"count": v["count"], "time": profile.hours_minutes(v["seconds"]),
                             "distance": profile.dist(v["meters"], p["units"]) if v["meters"] else None}
                         for k, v in sorted(by_sport.items())},
            "avg_sleep_hours": avg(hours), "nights_with_sleep": len(hours) if "sleep" in enabled else None,
            "avg_hrv_ms": avg(hrv), "avg_resting_hr": avg(rhr)})
    return answer(go)


def main():
    server.run()


if __name__ == "__main__":
    main()
