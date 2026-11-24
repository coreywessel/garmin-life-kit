"""
The morning or evening recap, start to finish.

    python -m kit.recap morning|evening [--source schedule|manual|wake]

guard -> collect Log notes (non-fatal) -> fetch Garmin (non-fatal, alert with the reason)
-> expiry warnings (morning) -> Claude writes it -> send to Today -> receipt.

Guard: one recap per slot per day. A schedule or wake run that finds today's receipt stands
down; a manual run always sends. The receipt is written only after Telegram confirmed the
send, because the guard and the watchdog both trust it.

If Claude fails, the same numbers go out as plain lines built in code, and Alerts says why.
"""
import argparse
import json
import sys

from kit import claude, expiry, fetch, garmin, log, numbers, profile, telegram, weather
from kit.telegram import esc

PROMPTS = profile.ROOT / "prompts"


def receipts_path():
    return profile.data_dir() / "receipts.json"


def load_receipts():
    try:
        return json.loads(receipts_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_receipt(mode, day):
    r = load_receipts()
    r[mode] = day.isoformat()
    path = receipts_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(r, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def alert(html):
    """Post to Alerts. A failed alert is printed, not raised: it must never stop the recap itself."""
    try:
        telegram.send("alerts", html)
    except telegram.TelegramError as e:
        print(f"WARNING: couldn't post to Alerts: {e}", file=sys.stderr)


def garmin_alert(e):
    head = {garmin.KeyRejected: "Garmin API key rejected.",
            garmin.RateLimited: "Garmin rate-limited the API key."}.get(type(e), "Garmin fetch failed.")
    return (f"<b>{head}</b> {esc(e.what)}. This recap goes out without Garmin numbers.\n"
            f"Fix: {esc(e.fix)}")


# ---------- the code-built version (Claude failed, or a dry run) ----------

def _week_line(ctx):
    w = ctx.get("week")
    if not w:
        return None
    if not w.get("count"):
        return "Week so far: no activities yet"
    bits = [f"{w['count']} activit{'y' if w['count'] == 1 else 'ies'}", w.get("time")]
    bits += [f"{k} {v}" for k, v in (w.get("distance_by_sport") or {}).items()]
    return "Week so far: " + " · ".join(b for b in bits if b)


def _weather_line(label, w):
    if not w:
        return None
    s = f"{label}: {w['summary']}, high {w['high']} / low {w['low']}"
    return s + (f", {w['precip']} precip" if w.get("precip") else "")


def fallback(mode, ctx, note="Plain numbers today: Claude didn't write this one."):
    """Same numbers, plain labels, no judgment. Report only, like the prompts."""
    hello = "Morning" if mode == "morning" else "Evening"
    lines = [f"<b>{hello}, {esc(ctx['name'])}.</b> {ctx['date_label']}"]
    if note:
        lines.append(f"<i>{esc(note)}</i>")
    if ctx.get("garmin"):
        lines.append("Garmin: no data today (details in Alerts)")
    if mode == "morning":
        s = ctx.get("sleep") or {}
        bits = []
        if s.get("bed"):
            bits.append(f"bed {s['bed']}, up {s['wake']} ({s['hours']} h)")
        if "score" in s:
            bits.append(f"score {s['score']}")
        if s.get("avg_hours_7"):
            bits.append(f"7-night avg {s['avg_hours_7']} h, bed {s['avg_bed_7']}")
        if s.get("vs_target"):
            bits.append(s["vs_target"])
        if bits:
            lines.append("Sleep: " + " · ".join(bits))
        st = s.get("stages") or {}
        bits = [" / ".join(f"{k} {v}" for k, v in st.items()) if st else None,
                f"breathing {s['breathing_per_min']}/min" if "breathing_per_min" in s else None,
                f"SpO2 {s['spo2_pct']}%" if "spo2_pct" in s else None]
        if any(bits):
            lines.append("Sleep detail: " + " · ".join(x for x in bits if x))
        h = ctx.get("hrv") or {}
        bits = [f"{h['last_night_ms']} ms" + (f" (7-day avg {h['avg_7_ms']})" if "avg_7_ms" in h else "")
                if "last_night_ms" in h else None,
                f"Garmin says {h['garmin_status']}" if h.get("garmin_status") else None,
                f"baseline {h['baseline_ms']}" if h.get("baseline_ms") else None]
        if any(bits):
            lines.append("HRV: " + " · ".join(b for b in bits if b))
        r = ctx.get("resting_hr") or {}
        if "today" in r:
            lines.append(f"Resting HR: {r['today']}" + (f" (7-day avg {r['avg_7']})" if "avg_7" in r else ""))
        b = ctx.get("body_battery") or {}
        bits = [f"{b['at_wake']} at wake" if "at_wake" in b else None,
                f"{b['high_yesterday']} high / {b.get('low_yesterday', '-')} low yesterday"
                if "high_yesterday" in b else None]
        if any(bits):
            lines.append("Body Battery: " + " · ".join(x for x in bits if x))
        bits = [f"stress avg {ctx['stress_avg_yesterday']}" if "stress_avg_yesterday" in ctx else None,
                f"{ctx['steps_yesterday']} steps" if "steps_yesterday" in ctx else None]
        if any(bits):
            lines.append("Yesterday: " + " · ".join(x for x in bits if x))
        acts = ctx.get("yesterday_activities") or []
        if acts:
            lines.append("Yesterday's activities: " + "; ".join(esc(numbers.describe(a)) for a in acts))
        if "vo2max" in ctx:
            lines.append(f"VO2 max: {ctx['vo2max']}")
        lines += [x for x in (_week_line(ctx), _weather_line("Weather", ctx.get("weather_today"))) if x]
    else:
        acts = ctx.get("today_activities") or []
        lines.append("Today: " + ("; ".join(esc(numbers.describe(a)) for a in acts) if acts
                                  else "nothing on Garmin today"))
        if "steps_today" in ctx:
            lines.append(f"Steps: {ctx['steps_today']}")
        lines += [x for x in (_week_line(ctx), _weather_line("Tomorrow", ctx.get("weather_tomorrow"))) if x]
        if ctx.get("sleep_target_bed"):
            lines.append(f"Bed target: {ctx['sleep_target_bed']}")
    return "\n".join(lines)


def build_input(p, ctx, notes):
    return "\n\n".join([
        "# About\n" + (p["recap"]["about"] or "(empty)"),
        "# Context (computed in code - use exactly as given)\n" + json.dumps(ctx, indent=1, ensure_ascii=False),
        "# Notes from the Log, last 2 days\n" + json.dumps(notes, indent=1, ensure_ascii=False),
    ])


def run(mode, source):
    try:
        p = profile.load()
    except profile.ProfileError as e:
        profile.fail(str(e))
    today = profile.today()
    if not p["features"][f"{mode}_recap"] and source != "manual":
        print(f"{mode} recap is turned off in profile.toml - nothing to do")
        return 0
    if source != "manual" and load_receipts().get(mode) == today.isoformat():
        print(f"{mode} recap already sent today ({today}) - standing down")
        return 0

    if p["features"]["log_notes"]:
        try:
            log.collect()
        except Exception as e:                   # a recap without the newest notes beats no recap
            print(f"WARNING: log collection failed: {e}", file=sys.stderr)

    data = {}
    try:
        data = fetch.collect(garmin.connect(), today)
    except garmin.GarminError as e:
        print(f"WARNING: {e}", file=sys.stderr)
        alert(garmin_alert(e))

    if mode == "morning":
        for w in expiry.warnings(today):
            alert(w)

    build = numbers.morning if mode == "morning" else numbers.evening
    ctx = build(data, p, today, weather.forecast(p))
    try:
        notes = log.recent(2)
    except Exception as e:
        print(f"WARNING: couldn't read Log notes: {e}", file=sys.stderr)
        notes = []
    system = (PROMPTS / f"{mode}.md").read_text(encoding="utf-8")
    stub = fallback(mode, ctx, note="Dry run: Claude was not called. This is the plain-numbers version.")
    claude_problem = None
    try:
        text = claude.ask(system, build_input(p, ctx, notes), stub=stub)
    except claude.ClaudeFailed as e:
        claude_problem = e
        text = fallback(mode, ctx)

    try:
        telegram.send("today", text, silent=(mode == "evening"))   # evening closes the day quietly
    except telegram.TelegramError as e:
        profile.fail(f"The {mode} recap was not sent. {e}")
    save_receipt(mode, today)                    # only now: Telegram confirmed
    print(f"{mode} recap sent ({len(text)} chars)")

    if claude_problem:
        alert(f"<b>Claude didn't write the {mode} recap</b> (most likely the Claude token). "
              f"The plain numbers went out instead.\nClaude said: {esc(str(claude_problem)[:300])}\n"
              "Fix: run <code>claude setup-token</code> on your computer, then ask Claude to run "
              "<code>uv run setup/kit.py secret CLAUDE_CODE_OAUTH_TOKEN</code> and "
              "<code>uv run setup/kit.py secrets</code>.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m kit.recap", description="Send the morning or evening recap.")
    ap.add_argument("mode", choices=["morning", "evening"])
    ap.add_argument("--source", choices=["schedule", "manual", "wake"], default="manual")
    a = ap.parse_args(argv)
    return run(a.mode, a.source)


if __name__ == "__main__":
    sys.exit(main())
