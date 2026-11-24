"""
Answers to /today, /week, /last and /status, started by the Worker through command.yml.

    python -m kit.commands today|week|last|status THREAD_ID

The answer goes back to the thread the command came from (empty = General). Errors are
answered there too, with the fix, and the run exits 1 so the watchdog sees it. All numbers,
no Claude, no advice.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

from kit import cards, expiry, fetch, garmin, metrics, numbers, profile, telegram
from kit.telegram import esc

WORKFLOWS = {"recap.yml": "Recap", "collect.yml": "Collect", "command.yml": "Commands", "watchdog.yml": "Watchdog"}
UA = "garmin-life-kit/1.0"


class Fail(Exception):
    """A plain-words reason the command couldn't answer, with the fix."""


def garmin_call(fn, *args):
    try:
        return fn(*args)
    except garmin.GarminError as e:
        raise Fail(str(e)) from None


def client():
    return garmin_call(garmin.connect)


def day_label(d):
    return f"{d.strftime('%a')} {d.strftime('%b')} {d.day}"


def cmd_today(p, today):
    on = metrics.enabled(p)
    c = client()
    t = today.isoformat()
    rec = garmin_call(c.sleep, t) if on & {"sleep", "sleep_score"} else {}
    summary = garmin_call(c.daily_summary, t) if "steps" in on else {}
    acts = [fetch.slim(a) for a in garmin_call(c.activities, t, t)]
    lines = [f"<b>Today</b> · {day_label(today)}"]
    n = numbers.night(rec) if "sleep" in on else None
    score = metrics.sleep_score({"sleep": rec}) if "sleep_score" in on else None
    if n:
        lines.append(f"Last night: {profile.time12(n['bed'])} to {profile.time12(n['wake'])}, {n['hours']} h"
                     + (f", score {score}" if score is not None else ""))
    elif score is not None:
        lines.append(f"Sleep score: {score}")
    steps = metrics.steps({"summary": summary}) if "steps" in on else None
    if steps is not None:
        lines.append(f"Steps so far: {steps:,}")
    for a in sorted(acts, key=lambda a: a.get("startTimeLocal", "")):
        lines.append(esc(numbers.describe(numbers.activity(a, p["units"], on))))
    if not acts:
        lines.append("No activities on Garmin yet today.")
    return "\n".join(lines)


def cmd_week(p, today):
    start = numbers.week_start(today, p)
    acts = [fetch.slim(a) for a in garmin_call(client().activities, start.isoformat(), today.isoformat())]
    w = numbers.week_totals(acts, today, p)
    lines = [f"<b>Week so far</b> · since {day_label(start)}"]
    if not w["count"]:
        return "\n".join(lines + ["No activities yet this week."])
    lines.append(f"{w['count']} activit{'y' if w['count'] == 1 else 'ies'} · {w['time']}")
    per_sport = {}
    for a in acts:
        s = per_sport.setdefault(numbers.sport(a), {"n": 0, "secs": 0})
        s["n"] += 1
        s["secs"] += a.get("duration") or 0
    for name, s in sorted(per_sport.items()):
        d = w["distance_by_sport"].get(name)
        lines.append(f"{esc(name)}: {s['n']} · {profile.hours_minutes(s['secs'])}" + (f" · {d}" if d else ""))
    return "\n".join(lines)


def cmd_last(p, today):
    acts = garmin_call(client().latest_activities, 1)
    if not acts:
        return "No activities on Garmin yet."
    return cards.card(fetch.slim(acts[0]), p["units"], metrics.enabled(p))


# ---------- /status ----------

def github(path):
    repo = os.environ.get("GITHUB_REPOSITORY") or profile.setup().get("repo")
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/{path}", headers={
        "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def ago(ts):
    mins = int((time.time() - ts) / 60)
    return f"{mins} min ago" if mins < 90 else f"{mins // 60} h ago" if mins < 48 * 60 else f"{mins // 1440} days ago"


def workflow_lines():
    if profile.dry():
        return ["Workflows: not checked (dry run)"]
    if not os.environ.get("GITHUB_TOKEN"):
        return ["Workflows: not checked (no GITHUB_TOKEN here)"]
    out = []
    for wf, name in WORKFLOWS.items():
        try:
            runs = github(f"actions/workflows/{wf}/runs?status=completed&per_page=1")["workflow_runs"]
        except urllib.error.HTTPError as e:
            out.append(f"{name}: " + ("not on GitHub yet" if e.code == 404 else f"couldn't read (HTTP {e.code})"))
            continue
        except Exception as e:
            out.append(f"{name}: couldn't read ({type(e).__name__})")
            continue
        if not runs:
            out.append(f"{name}: no runs yet")
            continue
        r = runs[0]
        when = ago(datetime.fromisoformat(r["updated_at"].replace("Z", "+00:00")).timestamp())
        c = r["conclusion"]
        state = "ok" if c in ("success", "skipped") else "cancelled (a newer run replaced it)" if c == "cancelled" else str(c).upper()
        out.append(f"{name}: {state}, {when}")
    return out


def worker_line():
    url = os.environ.get("WORKER_URL", "").rstrip("/")
    if profile.dry():
        return "Worker: not checked (dry run)"
    if not url:
        return "Worker: WORKER_URL not set. Fix: ask Claude to run `uv run setup/kit.py worker`"
    try:
        req = urllib.request.Request(f"{url}/health", headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            h = json.loads(r.read())
        return f"Worker: up, {h.get('log_pending', '?')} note(s) waiting to be filed"
    except Exception as e:
        return f"Worker: NOT answering ({type(e).__name__}). Fix: ask Claude to run `uv run setup/kit.py worker`"


def cmd_status(p, today):
    lines = ["<b>Status</b>"] + workflow_lines()
    try:
        rec = json.loads((profile.data_dir() / "receipts.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        rec = {}
    for mode in ("morning", "evening"):
        if p["features"][f"{mode}_recap"]:
            sent = rec.get(mode)
            lines.append(f"{mode.title()} recap: " + ("sent today" if sent == today.isoformat()
                                                       else f"last sent {sent}" if sent else "never sent"))
    lines.append(esc(worker_line()))
    for e, d in expiry.rows():
        lines.append(f"{esc(e['name'])}: expires {d.isoformat()} ({(d - today).days} days)")
    return "\n".join(lines)


COMMANDS = {"today": cmd_today, "week": cmd_week, "last": cmd_last, "status": cmd_status}


def main(argv):
    cmd = (argv[0] if argv else "").strip().lower().lstrip("/")
    thread = (argv[1] if len(argv) > 1 else "").strip()
    target = int(thread) if thread.lstrip("-").isdigit() else ""
    if cmd not in COMMANDS:
        raise SystemExit(__doc__)
    code = 1
    try:
        p = profile.load()
        text, code = COMMANDS[cmd](p, profile.today()), 0
    except profile.ProfileError as e:
        text = f"<b>/{cmd} failed.</b>\n{esc(e)}"
    except Fail as e:
        text = f"<b>/{cmd} failed.</b> {esc(e)}"
    except Exception as e:
        run = os.environ.get("RUN_URL")
        text = f"<b>/{cmd} failed:</b> {type(e).__name__}: {esc(str(e)[:300])}" + (f"\nLog: {esc(run)}" if run else "")
    try:
        telegram.send(target, text)
    except telegram.TelegramError as e:
        profile.fail(f"/{cmd} couldn't reply. {e}")
    if code:
        profile.fail(f"/{cmd} failed - the reason was posted in the thread.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
