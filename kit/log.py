"""
Log notes: what the owner types in the group. The Cloudflare Worker saves each one and gives the
👍 the moment it arrives; this files them into data/log.jsonl for the recaps to read.

    python -m kit.log collect   GET /log, append new notes, write out/log-ack.json
    python -m kit.log ack       POST /log/ack with those keys

The Worker keeps every note until ack clears it, and the workflows run ack only after the data
commit is pushed (`if: success()`). If the push fails the notes stay in the Worker and come back
next run; ones already in data/log.jsonl are skipped. So a note is never lost between the two.

No Worker configured (WORKER_URL / WORKER_API_SECRET unset) -> both are no-ops. A dry run skips
the Worker.
"""
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime

from kit import profile

ACK = profile.OUT / "log-ack.json"
SECRET_FIX = ("Fix: WORKER_API_SECRET in GitHub must match the Worker's. Ask Claude to run "
              "`uv run setup/kit.py secrets` and then `uv run setup/kit.py worker`")


class LogError(RuntimeError):
    pass


def log_path():
    return profile.data_dir() / "log.jsonl"


def configured():
    return bool(os.environ.get("WORKER_URL") and os.environ.get("WORKER_API_SECRET"))


def worker(method, path, body=None):
    url = os.environ["WORKER_URL"].rstrip("/") + path
    req = urllib.request.Request(url, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"X-Worker-Secret": os.environ["WORKER_API_SECRET"],
                                          "Content-Type": "application/json",
                                          # Cloudflare 403s Python's default user agent.
                                          "User-Agent": "garmin-life-kit/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            res = json.loads(r.read())
    except urllib.error.HTTPError as e:
        hint = SECRET_FIX if e.code in (401, 403) else "Fix: check the Worker in the Cloudflare dashboard, or ask Claude to run `uv run setup/kit.py worker`"
        raise LogError(f"Worker {path} answered HTTP {e.code}. {hint}") from None
    except (OSError, ValueError) as e:
        raise LogError(f"couldn't reach the Worker for {path} ({type(e).__name__}). "
                       "The notes are safe in the Worker; the next run tries again") from None
    if not res.get("ok"):
        raise LogError(f"Worker {path} said: {res.get('error', 'not ok')}")
    return res


def filed_ids():
    p = log_path()
    if not p.exists():
        return set()
    return {json.loads(line).get("message_id") for line in p.read_text(encoding="utf-8").splitlines() if line.strip()}


def collect():
    p = profile.load()
    ACK.parent.mkdir(exist_ok=True)
    ACK.write_text("[]\n", encoding="utf-8")                     # a stale ack file must never clear this run's notes
    if not p["features"]["log_notes"]:
        print("log notes are off in profile.toml - nothing to collect")
        return 0
    if profile.dry():
        print("dry run: Worker skipped - no notes collected")
        return 0
    if not configured():
        print("no Worker configured (WORKER_URL / WORKER_API_SECRET) - nothing to collect")
        return 0
    res = worker("GET", "/log")
    items = res.get("items") or []
    names = {v: k for k, v in ((profile.setup().get("telegram") or {}).get("topics") or {}).items()}
    seen = filed_ids()
    new = [{"ts": m["ts"], "topic": names.get(m.get("thread"), "general"), "text": m["text"],
            "message_id": m["message_id"]} for m in items if m.get("message_id") not in seen]
    if new:
        lp = log_path()
        lp.parent.mkdir(parents=True, exist_ok=True)
        with lp.open("a", encoding="utf-8") as f:
            for r in new:
                f.write(json.dumps(r) + "\n")
    # Every key, repeats included, so notes already filed get cleared too.
    ACK.write_text(json.dumps([m["key"] for m in items if str(m.get("key", "")).startswith("log:")]) + "\n", encoding="utf-8")
    print(f"filed {len(new)} new note(s), {len(items) - len(new)} already filed"
          + (" - more waiting, the next run collects them" if res.get("more") else ""))
    return 0


def ack():
    if profile.dry() or not configured():
        print("nothing to clear (no Worker configured or dry run)")
        return 0
    try:
        keys = json.loads(ACK.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        keys = []
    if not keys:
        print("nothing to clear in the Worker")
        return 0
    res = worker("POST", "/log/ack", {"keys": keys})
    print(f"cleared {res.get('deleted')} of {len(keys)} note(s) from the Worker")
    return 0


def recent(days=2, tz=None):
    """Notes from the last `days` days, for the recap: [{when, topic, text}]."""
    p = log_path()
    if not p.exists():
        return []
    tz = tz or profile.tz()
    cutoff = profile.now().timestamp() - days * 86400
    rows = [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [{"when": datetime.fromtimestamp(r["ts"], tz).strftime("%a %H:%M"), "topic": r.get("topic"),
             "text": r["text"]} for r in rows if r.get("ts", 0) >= cutoff]


def main(argv):
    cmd = argv[0] if argv else ""
    if cmd not in ("collect", "ack"):
        raise SystemExit(__doc__)
    try:
        return collect() if cmd == "collect" else ack()
    except LogError as e:
        profile.fail(f"Log notes: {e}")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
