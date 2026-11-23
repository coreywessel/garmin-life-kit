"""
Is everything actually running? Posts to Alerts only when something is wrong, plus an
all-clear summary every Sunday morning so a quiet Alerts topic can be trusted.

    python -m kit.watchdog morning    started by the Worker 45 min after the morning recap
    python -m kit.watchdog evening    45 min after the evening recap
    python -m kit.watchdog backstop   GitHub's own daily schedule: is the Worker's clock still ticking?

Slot checks: today's receipt for that recap, every run of each workflow since the previous
watchdog slot (so a failure followed by a green run still reports), the collect job ran in the
last 4.5 h, and the Worker's /health. Needs GITHUB_TOKEN + GITHUB_REPOSITORY (Actions sets
both), TELEGRAM_BOT_TOKEN, and WORKER_URL once the Worker is deployed.
"""
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from kit import profile, telegram
from kit.telegram import esc

WORKFLOWS = {"recap.yml": "Recap", "collect.yml": "Collect", "command.yml": "Commands"}
UA = "garmin-life-kit/1.0"
# "cancelled" is fine: GitHub replaces a waiting run in the kit-data group with a newer one.
OK = ("success", "skipped", "cancelled")
COLLECT_MAX_H = 4.5      # collect runs every 2 h between the recaps
BACKSTOP_H = 26


def gh(path):
    req = urllib.request.Request(f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}/{path}",
                                 headers={"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
                                          "Accept": "application/vnd.github+json", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def runs(workflow, since=None):
    """None if the workflow file isn't on GitHub yet (404). Any other error raises, so it gets
    reported instead of looking like 'nothing to check'."""
    q = f"actions/workflows/{workflow}/runs?per_page=50"
    if since:
        q += f"&created=>={since}"
    try:
        return gh(q).get("workflow_runs", [])
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def age_hours(iso):
    return (datetime.now(timezone.utc) - datetime.fromisoformat(iso.replace("Z", "+00:00"))).total_seconds() / 3600


def window_hours(p, slot):
    """Hours since the previous watchdog slot, plus a little, so no run falls between two checks."""
    hm = lambda t: int(t[:2]) * 60 + int(t[3:])
    m, e = hm(p["times"]["morning"]), hm(p["times"]["evening"])
    gap = (m - e) % 1440 if slot == "morning" else (e - m) % 1440
    return (gap or 1440) / 60 + 0.5


def receipt_problem(slot, now, p):
    """Today's receipt for the slot. A late evening (23:15+) puts its watchdog after midnight:
    before the slot's own time, yesterday's receipt is the one that counts."""
    try:
        sent = json.loads((profile.data_dir() / "receipts.json").read_text(encoding="utf-8")).get(slot)
    except (OSError, ValueError):
        sent = None
    h, m = map(int, p["times"][slot].split(":"))
    ok = {now.date().isoformat()}
    if (now.hour, now.minute) < (h, m):
        ok.add((now.date() - timedelta(days=1)).isoformat())
    if sent not in ok:
        return f"No {slot} recap sent today (last: {sent or 'never'}). Fix: open the Recap run in GitHub Actions and read the failed step."
    return None


def health():
    """(health dict or None, problem or None)."""
    url = os.environ.get("WORKER_URL", "").rstrip("/")
    if not url:
        return None, "WORKER_URL isn't set in GitHub, so the Worker can't be checked. Fix: ask Claude to run `uv run setup/kit.py worker`."
    try:
        req = urllib.request.Request(f"{url}/health", headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            h = json.loads(r.read())
    except Exception as e:
        return None, (f"Worker not answering ({type(e).__name__}): commands, the schedule and Log notes are down. "
                      "Fix: ask Claude to run `uv run setup/kit.py worker`.")
    return h, None if h.get("ok") else f"Worker /health says not ok: {h}"


def post(title, problems):
    telegram.send("alerts", f"<b>{title}</b>\n" + "\n".join(f"- {esc(x)}" for x in problems))


def backstop(p, now):
    """The Worker starts every job. If its clock stops, nothing runs and nothing complains, so
    GitHub's own (sometimes hours-late) schedule checks the Worker-started watchdog still runs."""
    problems = []
    if profile.dry():
        print("dry run: GitHub not checked")
        return problems
    try:
        rs = runs("watchdog.yml") or []
        started = [r for r in rs if r["event"] == "workflow_dispatch"]
        if not started or age_hours(started[0]["created_at"]) > BACKSTOP_H:
            problems.append(f"No Worker-started watchdog in {BACKSTOP_H}h+. The Worker's schedule may be down. "
                            "Fix: Cloudflare dashboard > Workers > garmin-life-kit > Settings > Triggers should show "
                            "*/5 * * * *; if not, ask Claude to run `uv run setup/kit.py worker`.")
    except Exception as e:
        problems.append(f"Couldn't read workflow runs from GitHub ({type(e).__name__}).")
    return problems


def check(p, slot, now):
    problems = []
    if p["features"][f"{slot}_recap"]:
        problems.append(receipt_problem(slot, now, p))
    if profile.dry():
        print("dry run: GitHub and the Worker not checked")
        return [x for x in problems if x], None
    hours = window_hours(p, slot)
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    collect_on = p["features"]["log_notes"] or p["features"]["activity_cards"]
    for wf, name in WORKFLOWS.items():
        try:
            rs = runs(wf, since)
        except Exception as e:
            problems.append(f"{name}: couldn't read runs from GitHub ({type(e).__name__}).")
            continue
        if rs is None:
            continue
        done = [r for r in rs if r["status"] == "completed"]
        bad = [r for r in done if r["conclusion"] not in OK]
        if bad:
            problems.append(f"{name}: {len(bad)} run(s) not green in the last {hours:.0f} h, newest "
                            f"{bad[0]['conclusion']} ({bad[0]['html_url']})")
        if wf == "collect.yml" and collect_on:
            if not done:
                problems.append(f"{name}: no run in {hours:.0f} h (should run every 2 h during the day).")
            elif age_hours(done[0]["updated_at"]) > COLLECT_MAX_H:
                problems.append(f"{name}: last run {age_hours(done[0]['updated_at']):.0f} h ago (should be every 2 h).")
    h, problem = health()
    problems.append(problem)
    return [x for x in problems if x], h


def weekly(p, problems, h, now):
    lines = ["<b>Weekly system check</b>"]
    if not profile.dry():
        since = (now - timedelta(days=7)).date().isoformat()
        for wf, name in WORKFLOWS.items():
            try:
                rs = runs(wf, since)
            except Exception as e:
                lines.append(f"{name}: couldn't read runs ({type(e).__name__})")
                continue
            if rs is None:
                continue
            ok = sum(1 for r in rs if r["conclusion"] in OK)
            bad = sum(1 for r in rs if r["conclusion"] not in OK + (None,))
            lines.append(f"{name}: {ok} ok, {bad} failed")
    if h:
        lines.append(f"Worker: up, {h.get('log_pending', 0)} note(s) waiting")
    lines.append("All clear." if not problems else f"{len(problems)} issue(s) posted above.")
    telegram.send("alerts", "\n".join(lines))


def run(slot):
    # The public template and a fresh copy have no setup.json yet. The daily GitHub cron must not
    # email a failure every day for a kit nobody has set up.
    if not profile.setup():
        print("setup.json not found - this copy isn't set up yet, nothing to check")
        return 0
    try:
        p = profile.load()
    except profile.ProfileError as e:
        profile.fail(str(e))
    if not p["features"]["watchdog"]:
        print("watchdog is off in profile.toml - nothing to do")
        return 0
    now = profile.now()
    try:
        if slot == "backstop":
            problems = backstop(p, now)
            if problems:
                post("Watchdog backstop", problems)
            print(f"backstop: {len(problems)} problem(s)")
            return 0
        problems, h = check(p, slot, now)
        if problems:
            post("Watchdog", problems)
        if slot == "morning" and now.weekday() == 6:
            weekly(p, problems, h, now)
    except telegram.TelegramError as e:
        profile.fail(f"The watchdog found problems but couldn't post them. {e}")
    print(f"{slot}: {len(problems)} problem(s)")
    return 0


def main(argv):
    slot = argv[0] if argv else ""
    if slot not in ("morning", "evening", "backstop"):
        raise SystemExit(__doc__)
    return run(slot)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
