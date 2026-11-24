"""
What does this person's Garmin account actually return? Watches differ: some have no HRV, no
sleep stages, no Body Battery, no SpO2. Setup runs this once (and again after a new device)
and writes the answer into profile.toml [metrics].

    run(client, today) -> {
        "available": [names],            # some sampled response had a value: switch on
        "missing": [names],              # there were samples to look at, none had it: switch off
        "unknown": {name: reason},       # nothing to judge by (no activities / no sleep): leave as is
        "activity_types": {typeKey: n}, "calls": n, "sampled_dates": [...], "not_in_kit": {...}}

No samples is not the same as "this watch can't": with no activities in the last 14 days the
activity metrics are unknown, and with no sleep record on any day checked the sleep metrics are.

Cheap on purpose: one token exchange (through the client's cache), then about 10 data calls:
activities for the last 14 days, and sleep, HRV and daily summary for yesterday, 3 days ago and
7 days ago. A 429 is never retried here (RateLimited goes straight to the caller): retrying is
exactly what keeps Garmin rate-limiting the key.
"""
from collections import Counter
from datetime import timedelta

from kit import fetch
from kit.garmin import ExchangeFailed, GarminError, KeyRejected, RateLimited
from kit.metrics import ACTIVITY, METRICS, NAMES, NOT_IN_KIT, kind

SAMPLE_DAYS = (1, 3, 7)
FATAL = (KeyRejected, RateLimited, ExchangeFailed)
SLEEP_METRICS = ("sleep", "sleep_stages", "sleep_score", "respiration", "spo2")
NO_ACTIVITIES = "no activities in the last 14 days"
NO_SLEEP = "no sleep recorded on the days checked"


def run(client, today):
    calls = 0

    def call(fn, *args):
        nonlocal calls
        calls += 1
        try:
            return fn(*args)
        except FATAL:
            raise
        except GarminError as e:            # one odd endpoint = no data from it; code bugs still surface
            print(f"WARNING: Garmin call failed during discovery: {e}")
            return None

    acts = call(client.activities, (today - timedelta(days=13)).isoformat(), today.isoformat()) or []
    acts = [fetch.slim(a) for a in acts] if isinstance(acts, list) else []
    dates = [(today - timedelta(days=n)).isoformat() for n in SAMPLE_DAYS]
    days = [{"sleep": call(client.sleep, d) or {}, "hrv": call(client.hrv, d) or {},
             "summary": call(client.daily_summary, d) or {}} for d in dates]
    any_sleep = any(d["sleep"] for d in days)

    available, missing, unknown = [], [], {}
    for name in NAMES:
        extract = METRICS[name][2]
        samples = acts if kind(name) == ACTIVITY else days
        if any(extract(s) is not None for s in samples):
            available.append(name)
        elif kind(name) == ACTIVITY and not acts:
            unknown[name] = NO_ACTIVITIES
        elif name in SLEEP_METRICS and not any_sleep:
            unknown[name] = NO_SLEEP
        else:
            missing.append(name)
    return {"available": available, "missing": missing, "unknown": unknown,
            "activity_types": dict(Counter(a["typeKey"] for a in acts).most_common()),
            "calls": calls, "sampled_dates": dates, "not_in_kit": dict(NOT_IN_KIT)}
