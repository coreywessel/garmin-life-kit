"""
Every number the recaps show, worked out in code. Claude gets these and only writes the words.

    morning(data, p, today, weather) -> dict      data = kit.fetch.collect(...) or {} when Garmin failed
    evening(data, p, today, weather) -> dict

Values are already formatted in the person's units ("5.02 mi", "10:42 pm"), so Claude never
converts or rounds. Missing data means the key is absent, never a guessed number.
`plan` is always None in v1: the hook a training-plan module would fill.

Only metrics switched on in profile.toml [metrics] are built (kit/metrics.py names them). A
metric that's on but has no data is simply left out: a sparse account still gets a short recap.
"""
import re
from datetime import date, datetime, timedelta, timezone

from kit import metrics
from kit.metrics import num as _num
from kit.profile import clock, dist, elev, hours_minutes, pace, speed, time12

SNOW = ("resort_snowboarding", "resort_skiing_snowboarding_ws", "resort_skiing", "backcountry_snowboarding",
        "backcountry_skiing")
PACE_SPORTS = ("running", "walking", "hiking")       # typeKey contains one of these -> show pace
SPEED_SPORTS = ("cycling", "biking", "ride")
SPORT_NAMES = {"resort_snowboarding": "snowboarding", "resort_skiing_snowboarding_ws": "skiing/snowboarding",
               "resort_skiing": "skiing", "backcountry_snowboarding": "backcountry snowboarding",
               "backcountry_skiing": "backcountry skiing", "lap_swimming": "pool swimming",
               "open_water_swimming": "open water swimming", "strength_training": "strength",
               "indoor_cycling": "indoor cycling", "treadmill_running": "treadmill running"}
NOON = 12 * 60


# ---------- reading Garmin records ----------

def _rec(data, d, kind):
    r = ((data.get("days") or {}).get(d) or {}).get(kind) or {}
    return {} if "error" in r else r


def _avg(vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def _iso(d):
    return d.isoformat() if isinstance(d, date) else str(d)


def activities(data):
    acts = data.get("activities")
    return acts if isinstance(acts, list) else []


def day_of(a):
    return (a.get("startTimeLocal") or "")[:10]


def prune(d):
    """Drop None and empty values, so missing data is simply absent. `plan` stays (it's the hook)."""
    if isinstance(d, dict):
        out = {}
        for k, v in d.items():
            v = prune(v)
            if k == "plan" or v not in (None, {}, [], ""):
                out[k] = v
        return out
    if isinstance(d, list):
        return [prune(v) for v in d]
    return d


# ---------- sleep ----------

def night(rec):
    """Bed and wake as minutes after midnight, and hours asleep. Garmin stores local wall time
    as if it were UTC milliseconds, so read it back as UTC."""
    start, end = rec.get("sleepStartTimestampLocal"), rec.get("sleepEndTimestampLocal")
    if not start or not end:
        return None
    wall = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc)
    b, w = wall(start), wall(end)
    secs = _num(rec.get("sleepTimeSeconds")) or (end - start) / 1000
    return {"bed": b.hour * 60 + b.minute, "wake": w.hour * 60 + w.minute, "hours": round(secs / 3600, 1)}


def night_mins(m):
    """A bed time on a night scale: before noon counts as after midnight, so 00:15 is later than 22:30."""
    return m + 1440 if m < NOON else m


def minutes_vs_target(bed, target):
    """Minutes after (+) or before (-) the target bed time, correct across midnight."""
    return night_mins(bed) - night_mins(target)


def span(mins):
    h, m = divmod(abs(mins), 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def vs_target_text(diff):
    if diff == 0:
        return "at target"
    return f"{span(diff)} {'after' if diff > 0 else 'before'} target"


def hhmm(t):
    h, m = map(int, t.split(":"))
    return h * 60 + m


# ---------- activities ----------

def sport(a):
    key = a.get("typeKey") or "other"
    return SPORT_NAMES.get(key, key.replace("_", " "))


def is_snow(a):
    key = a.get("typeKey") or ""
    return key in SNOW or "snowboard" in key or "skiing" in key


def ride_runs(a):
    """Runs on a snow day. The Personal API has no run count, but Garmin's auto-name often has
    one ("... Snowboard (12 Runs)"). No number in the name -> no runs figure."""
    m = re.search(r"(\d+)\s*runs?\b", a.get("activityName") or "", re.I)
    return int(m.group(1)) if m else None


def start_text(a):
    try:
        t = datetime.fromisoformat(a["startTimeLocal"])
    except (KeyError, ValueError):
        return None
    return f"{t.strftime('%a')} {time12(t.hour * 60 + t.minute)}"


def zones_text(z):
    total = sum(z)
    pct = lambda x: round(100 * x / total)
    return f"Z1-2 {pct(z[0] + z[1])}% · Z3 {pct(z[2])}% · Z4-5 {pct(z[3] + z[4])}%"


def activity(a, units, on=None, detail=False):
    """One activity's numbers, formatted. Snow days: vertical is the descent (the climb is the
    lift), top speed is maxSpeed. detail=True (cards) adds zones, training effect and load, each
    only when its metric is on and the activity has it."""
    on = set(metrics.NAMES) if on is None else on
    key = a.get("typeKey") or ""
    out = {"name": a.get("activityName") or sport(a), "sport": sport(a), "start": start_text(a)}
    if (a.get("distance") or 0) > 0:
        out["distance"] = dist(a["distance"], units)
    if a.get("duration"):
        out["time"] = clock(a["duration"])
    if any(s in key for s in PACE_SPORTS) and a.get("averageSpeed"):
        out["pace"] = pace(a["averageSpeed"], units)
    elif any(s in key for s in SPEED_SPORTS) and a.get("averageSpeed"):
        out["avg_speed"] = speed(a["averageSpeed"], units)
    if a.get("averageHR"):
        out["avg_hr"] = round(a["averageHR"])
    if a.get("maxHR"):
        out["max_hr"] = round(a["maxHR"])
    if is_snow(a):
        if a.get("elevationLoss"):
            out["vertical"] = elev(a["elevationLoss"], units)
        if a.get("maxSpeed"):
            out["top_speed"] = speed(a["maxSpeed"], units)
        if ride_runs(a):
            out["runs"] = ride_runs(a)
    elif a.get("elevationGain"):
        out["climb"] = elev(a["elevationGain"], units)
    if detail:
        z = metrics.hr_zones(a) if "hr_zones" in on else None
        if z:
            out["zones"] = zones_text(z)
        if "training_effect" in on and metrics.training_effect(a) is not None:
            out["effect"] = f"{a.get('aerobicTrainingEffect') or 0:.1f} aerobic / {a.get('anaerobicTrainingEffect') or 0:.1f} anaerobic"
        if "training_load" in on and metrics.training_load(a) is not None:
            out["load"] = round(a["activityTrainingLoad"])
        if "vo2max" in on and metrics.vo2max(a) is not None:
            out["vo2max"] = round(a["vO2MaxValue"])
    return out


def describe(line):
    """'Morning Run: 3.20 mi · 28:00 · 8:47/mi' - the plain one-liner used by fallbacks and commands."""
    bits = [line.get(k) for k in ("distance", "time", "pace", "avg_speed")]
    if line.get("vertical"):
        bits.append(f"{line['vertical']} vertical")
    if line.get("runs"):
        bits.append(f"{line['runs']} runs")
    bits = [b for b in bits if b]
    return f"{line['name']}: {' · '.join(bits)}" if bits else line["name"]


def week_start(today, p):
    first = 0 if p.get("week_starts", "monday") == "monday" else 6
    return today - timedelta(days=(today.weekday() - first) % 7)


def week_totals(acts, today, p):
    """Week to date: how many activities, total time, distance per sport (sports with distance only)."""
    start = week_start(today, p)
    wk = [a for a in acts if start.isoformat() <= day_of(a) <= today.isoformat()]
    by_sport = {}
    for a in wk:
        if (a.get("distance") or 0) > 0:
            by_sport[sport(a)] = by_sport.get(sport(a), 0) + a["distance"]
    return {"starts": start.isoformat(), "count": len(wk),
            "time": hours_minutes(sum(a.get("duration") or 0 for a in wk)) if wk else None,
            "distance_by_sport": {k: dist(v, p["units"]) for k, v in sorted(by_sport.items())}}


# ---------- the two recaps ----------

def _header(p, today):
    return {"name": p["name"], "date": today.isoformat(), "weekday": today.strftime("%A"),
            "date_label": f"{today.strftime('%A')}, {today.strftime('%b')} {today.day}",
            "units": p["units"], "plan": None}


def sleep_block(data, p, today, days=7):
    """Last night plus the 7-night averages and the distance from the target bed time, and the
    sleep extras (score, stages, breathing, SpO2) that are switched on and present."""
    on = metrics.enabled(p)
    t = today.isoformat()
    rec = _rec(data, t, "sleep")
    day = {"sleep": rec, "summary": _rec(data, t, "summary")}
    out = {}
    if "sleep" in on:
        last = night(rec)
        nights = [night(_rec(data, (today - timedelta(days=i)).isoformat(), "sleep")) for i in range(days)]
        nights = [n for n in nights if n]
        if last:
            out.update(bed=time12(last["bed"]), wake=time12(last["wake"]), hours=last["hours"])
        if nights:
            out["avg_hours_7"] = round(_avg([n["hours"] for n in nights]), 1)
            out["avg_bed_7"] = time12(round(_avg([night_mins(n["bed"]) for n in nights])))
            out["nights_in_avg"] = len(nights)
        target = p["recap"].get("sleep_target_bed")
        if target:
            out["target_bed"] = time12(hhmm(target))
            if last:
                diff = minutes_vs_target(last["bed"], hhmm(target))
                out["vs_target"] = vs_target_text(diff)
                out["minutes_vs_target"] = diff
    if "sleep_score" in on:
        out["score"] = metrics.sleep_score(day)
    if "sleep_stages" in on and metrics.sleep_stages(day):
        out["stages"] = {k: hours_minutes(v) for k, v in metrics.sleep_stages(day).items() if v}
    if "respiration" in on:
        r = metrics.respiration(day)
        out["breathing_per_min"] = round(r, 1) if r is not None else None
    if "spo2" in on:
        o = metrics.spo2(day)
        out["spo2_pct"] = round(o) if o is not None else None
    return out


def morning(data, p, today, weather=None):
    data = data or {}
    on = metrics.enabled(p)
    t, y = today.isoformat(), (today - timedelta(days=1)).isoformat()
    week_days = [(today - timedelta(days=i)).isoformat() for i in range(7)]
    ctx = _header(p, today)
    if not data:
        ctx["garmin"] = "unavailable today"
    ctx["sleep"] = sleep_block(data, p, today)

    if "hrv" in on:
        hrv = _rec(data, t, "hrv").get("hrvSummary") or {}
        base = hrv.get("baseline") or {}
        hrv_7 = _avg([_num((_rec(data, d, "hrv").get("hrvSummary") or {}).get("lastNightAvg")) for d in week_days])
        ctx["hrv"] = {"last_night_ms": _num(hrv.get("lastNightAvg")),
                      "avg_7_ms": round(hrv_7) if hrv_7 is not None else None,
                      "garmin_status": (hrv.get("status") or "").replace("_", " ").lower() or None,
                      "baseline_ms": f"{base['balancedLow']}-{base['balancedUpper']}"
                      if base.get("balancedLow") and base.get("balancedUpper") else None}

    today_sum, y_sum = _rec(data, t, "summary"), _rec(data, y, "summary")
    if "resting_hr" in on:
        rhr_7 = _avg([_num(_rec(data, d, "summary").get("restingHeartRate")) for d in week_days])
        ctx["resting_hr"] = {"today": _num(today_sum.get("restingHeartRate")),
                             "avg_7": round(rhr_7) if rhr_7 is not None else None}
    if "body_battery" in on:
        ctx["body_battery"] = {"at_wake": _num(today_sum.get("bodyBatteryAtWakeTime")),
                               "high_yesterday": _num(y_sum.get("bodyBatteryHighestValue")),
                               "low_yesterday": _num(y_sum.get("bodyBatteryLowestValue"))}
    if "stress" in on:
        ctx["stress_avg_yesterday"] = _num(y_sum.get("averageStressLevel"))
    if "steps" in on:
        steps = _num(y_sum.get("totalSteps"))
        ctx["steps_yesterday"] = f"{steps:,}" if steps is not None else None

    acts = activities(data)
    ctx["yesterday_activities"] = [activity(a, p["units"], on) for a in acts if day_of(a) == y]
    if "vo2max" in on:
        latest = [a for a in acts if metrics.vo2max(a) is not None]
        if latest:
            ctx["vo2max"] = round(latest[-1]["vO2MaxValue"])      # acts are oldest first: the newest reading
    if isinstance(data.get("activities"), list):
        ctx["week"] = week_totals(acts, today, p)
    if weather and weather.get("today"):
        ctx["weather_today"] = {**weather["today"], "place": weather.get("place")}
    return prune(ctx)


def evening(data, p, today, weather=None):
    data = data or {}
    on = metrics.enabled(p)
    t = today.isoformat()
    ctx = _header(p, today)
    if not data:
        ctx["garmin"] = "unavailable today"
    acts = activities(data)
    ctx["today_activities"] = [activity(a, p["units"], on) for a in acts if day_of(a) == t]
    if "steps" in on:
        steps = _num(_rec(data, t, "summary").get("totalSteps"))
        ctx["steps_today"] = f"{steps:,}" if steps is not None else None
    if isinstance(data.get("activities"), list):
        ctx["week"] = week_totals(acts, today, p)
    if weather and weather.get("tomorrow"):
        ctx["weather_tomorrow"] = {**weather["tomorrow"], "place": weather.get("place")}
    if "sleep" in on and p["recap"].get("sleep_target_bed"):
        ctx["sleep_target_bed"] = time12(hhmm(p["recap"]["sleep_target_bed"]))
    return prune(ctx)
