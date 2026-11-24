"""
Pull the last N days of Garmin data for the recap and commands.

    data = collect(client, today)   # {today, days: {date: {summary, sleep, hrv}}, activities: [...]}

One failing endpoint records {"error": "..."} and the rest carries on: a recap with most of
the numbers beats no recap. Key rejected, rate limited and a failed exchange are raised,
because every other call would fail the same way and the caller alerts with the fix.
"""
import sys
from datetime import timedelta

from kit.garmin import ExchangeFailed, KeyRejected, RateLimited

# The activity fields the code uses. Everything else (and Garmin's per-second arrays) is dropped.
ACTIVITY_KEYS = ("activityId", "activityName", "startTimeLocal", "distance", "duration", "averageHR",
                 "maxHR", "averageSpeed", "calories", "aerobicTrainingEffect", "anaerobicTrainingEffect",
                 "activityTrainingLoad", "elevationGain", "elevationLoss", "maxSpeed",
                 "hrTimeInZone_1", "hrTimeInZone_2", "hrTimeInZone_3", "hrTimeInZone_4", "hrTimeInZone_5",
                 "lapCount", "vO2MaxValue")
FATAL = (KeyRejected, RateLimited, ExchangeFailed)


def slim(a):
    """One activity cut to the fields the code uses, with typeKey pulled out of activityType."""
    out = {k: a[k] for k in ACTIVITY_KEYS if a.get(k) is not None}
    out["typeKey"] = (a.get("activityType") or {}).get("typeKey") or "other"
    return out


def safe(label, fn, *args):
    try:
        return fn(*args) or {}
    except FATAL:
        raise
    except Exception as e:
        print(f"WARNING: Garmin {label} failed: {e}", file=sys.stderr)
        return {"error": f"{label} unavailable"}


def collect(client, today, days=7):
    """The last `days` days (today included) of summary, sleep and HRV, plus every activity in them."""
    out = {"today": today.isoformat(), "days": {}}
    for i in range(days):
        d = (today - timedelta(days=i)).isoformat()
        out["days"][d] = {"summary": safe("daily summary", client.daily_summary, d),
                          "sleep": safe("sleep", client.sleep, d),
                          "hrv": safe("HRV", client.hrv, d)}
    start = (today - timedelta(days=days - 1)).isoformat()
    try:
        acts = client.activities(start, today.isoformat())
        out["activities"] = sorted((slim(a) for a in acts), key=lambda a: a.get("startTimeLocal", ""))
    except FATAL:
        raise
    except Exception as e:
        print(f"WARNING: Garmin activities failed: {e}", file=sys.stderr)
        out["activities"] = {"error": "activities unavailable"}
    return out
