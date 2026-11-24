"""
The one table of metrics the kit can show, and how each is read from Garmin's responses.

    METRICS = {name: (label, where it comes from, extractor)}

Day metrics come from one day's responses: the extractor gets {"sleep": ..., "hrv": ...,
"summary": ...} for a date. Activity metrics come from one activity (a raw or slim activity
dict). An extractor returns a usable value or None; discovery calls a metric available when
any sampled response gives a value.

profile.toml [metrics] switches these on and off by name. Weight is not in v1: there is no
verified Personal API endpoint for it.

Field names follow what kore-life's code reads and the fixtures use. Names marked UNVERIFIED
come from Garmin Connect's own responses and have not been seen from the Personal API yet;
if Garmin names them differently, discovery simply reports the metric as not found.
"""
import re

DAY, ACTIVITY = "day", "activity"
NOT_IN_KIT = {"weight": "not available through the kit yet"}


def num(v):
    """Garmin writes -1/-2 (or nothing) when it didn't have enough data. Those are missing, not numbers."""
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else None


def _first(*vals):
    return next((v for v in vals if v is not None), None)


def _sleep(day):
    return day.get("sleep") or {}


def _summary(day):
    return day.get("summary") or {}


def _hrv(day):
    return (day.get("hrv") or {}).get("hrvSummary") or {}


def sleep(day):
    s = _sleep(day)
    return (s["sleepStartTimestampLocal"], s["sleepEndTimestampLocal"]) \
        if s.get("sleepStartTimestampLocal") and s.get("sleepEndTimestampLocal") else None


def sleep_stages(day):
    # UNVERIFIED on the Personal API: deep/light/rem/awakeSleepSeconds (Garmin Connect's dailySleepDTO names).
    s = _sleep(day)
    stages = {k: num(s.get(f"{k}SleepSeconds")) for k in ("deep", "light", "rem", "awake")}
    return stages if any(stages[k] for k in ("deep", "light", "rem")) else None


def sleep_score(day):
    return num(((_sleep(day).get("sleepScores") or {}).get("overall") or {}).get("value"))


def respiration(day):
    return num(_sleep(day).get("averageRespirationValue"))


def spo2(day):
    # UNVERIFIED: averageSpO2Value (sleep record) and averageSpo2 (daily summary).
    return _first(num(_sleep(day).get("averageSpO2Value")), num(_summary(day).get("averageSpo2")))


def hrv(day):
    return num(_hrv(day).get("lastNightAvg"))


def resting_hr(day):
    return num(_summary(day).get("restingHeartRate"))


def body_battery(day):
    s = _summary(day)
    return _first(*(num(s.get(k)) for k in ("bodyBatteryAtWakeTime", "bodyBatteryHighestValue",
                                             "bodyBatteryLowestValue")))


def stress(day):
    return num(_summary(day).get("averageStressLevel"))


def steps(day):
    return num(_summary(day).get("totalSteps"))


def vo2max(a):
    # UNVERIFIED: vO2MaxValue on the activity (Garmin Connect's activity list name).
    return num(a.get("vO2MaxValue"))


def training_load(a):
    return num(a.get("activityTrainingLoad")) or None


def training_effect(a):
    return _first(num(a.get("aerobicTrainingEffect")) or None, num(a.get("anaerobicTrainingEffect")) or None)


def hr_zones(a):
    z = [num(a.get(f"hrTimeInZone_{i}")) or 0 for i in range(1, 6)]
    return z if sum(z) else None


METRICS = {
    "sleep": ("Sleep (bed, wake, hours)", "sleep", sleep),
    "sleep_stages": ("Sleep stages (deep, light, REM, awake)", "sleep", sleep_stages),
    "sleep_score": ("Sleep score", "sleep", sleep_score),
    "hrv": ("Overnight HRV", "hrv", hrv),
    "resting_hr": ("Resting heart rate", "daily summary", resting_hr),
    "body_battery": ("Body Battery", "daily summary", body_battery),
    "stress": ("Stress", "daily summary", stress),
    "steps": ("Steps", "daily summary", steps),
    "respiration": ("Breathing rate while asleep", "sleep", respiration),
    "spo2": ("Blood oxygen (SpO2)", "sleep / daily summary", spo2),
    "vo2max": ("VO2 max", "activities", vo2max),
    "training_load": ("Training load", "activities", training_load),
    "training_effect": ("Training effect", "activities", training_effect),
    "hr_zones": ("Heart rate zones", "activities", hr_zones),
}
NAMES = tuple(METRICS)

# Raw Garmin field names each metric owns, so the MCP tools can leave a switched-off metric out
# of a raw response. Matched case-insensitively against keys at any depth.
FIELDS = {
    "sleep": r"^sleep(Start|End)Timestamp|^sleepTimeSeconds$",
    "sleep_stages": r"^(deep|light|rem|awake)SleepSeconds$|^sleepLevels$",
    "sleep_score": r"^sleepScores$",
    "hrv": r"hrv",
    "resting_hr": r"restingHeartRate",
    "body_battery": r"^bodyBattery",
    "stress": r"stress",
    "steps": r"step",
    "respiration": r"respiration",
    "spo2": r"spo2",
    "vo2max": r"vo2",
    "training_load": r"trainingLoad",
    "training_effect": r"trainingEffect",
    "hr_zones": r"hrTimeInZone",
}


def kind(name):
    return ACTIVITY if METRICS[name][1] == "activities" else DAY


def enabled(p):
    """The set of metric names switched on. No profile or no [metrics] section = all on."""
    switches = (p or {}).get("metrics") or {}
    return {n for n in NAMES if switches.get(n, True)}


def strip_off(x, on):
    """Drop the raw fields of every switched-off metric, at any depth."""
    pats = [re.compile(FIELDS[n], re.I) for n in FIELDS if n not in on]
    if not pats:
        return x

    def walk(v):
        if isinstance(v, dict):
            return {k: walk(val) for k, val in v.items() if not any(p.search(k) for p in pats)}
        if isinstance(v, list):
            return [walk(i) for i in v]
        return v
    return walk(x)
