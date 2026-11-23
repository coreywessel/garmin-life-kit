"""
Today's and tomorrow's weather from Open-Meteo (free, no key), in the profile's units.

    forecast(profile) -> {"today": {...}, "tomorrow": {...}} or None

Any failure (network, odd reply, weather turned off, no location) -> None, and the recap
just leaves the weather line out. A dry run returns a fixed made-up forecast.
"""
import json
import sys
import urllib.parse
import urllib.request

from kit import profile

# WMO weather codes -> plain words (open-meteo.com/en/docs).
WMO = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "cloudy", 45: "fog", 48: "freezing fog",
       51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
       61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
       71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
       80: "rain showers", 81: "rain showers", 82: "heavy rain showers", 85: "snow showers", 86: "heavy snow showers",
       95: "thunderstorms", 96: "thunderstorms with hail", 99: "thunderstorms with hail"}
DRY = {"daily": {"time": ["2026-01-14", "2026-01-15"], "weather_code": [71, 2],
                 "temperature_2m_max": [1.5, 4.0], "temperature_2m_min": [-8.0, -6.5],
                 "precipitation_sum": [3.2, 0.0]}}


def wanted(p):
    loc = p["location"]
    return p["features"]["weather"] and not (float(loc["lat"]) == 0 and float(loc["lon"]) == 0)


def _fetch(p):
    if profile.dry():
        return DRY
    q = urllib.parse.urlencode({
        "latitude": f"{float(p['location']['lat']):.3f}", "longitude": f"{float(p['location']['lon']):.3f}",
        "timezone": p["timezone"], "forecast_days": 2,
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum"})
    req = urllib.request.Request(f"https://api.open-meteo.com/v1/forecast?{q}",
                                 headers={"User-Agent": "garmin-life-kit/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def forecast(p):
    if not wanted(p):
        return None
    try:
        d = _fetch(p)["daily"]
        out = {}
        for i, label in enumerate(("today", "tomorrow")):
            day = {"summary": WMO.get(d["weather_code"][i], "mixed"),
                   "high": profile.temp(d["temperature_2m_max"][i], p["units"]),
                   "low": profile.temp(d["temperature_2m_min"][i], p["units"])}
            if (d["precipitation_sum"][i] or 0) > 0:
                day["precip"] = profile.precip(d["precipitation_sum"][i], p["units"])
            out[label] = day
        if p["location"].get("label"):
            out["place"] = p["location"]["label"]
        return out
    except Exception as e:
        print(f"WARNING: weather unavailable ({type(e).__name__})", file=sys.stderr)
        return None
