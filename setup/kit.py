"""
garmin-life-kit setup CLI. Run from the repo root:

    uv run setup/kit.py <command> [--dry-run]

Commands (SETUP.md says when to run each):
    status      what's done so far, and the next step
    check       prerequisites on this computer (python, git, gh, node, npx, uv, claude)
    secret NAME hidden prompt for one pasted secret, saved on this computer only
    profile     write profile.toml (--name --timezone --units --city --morning --evening)
    telegram    find the group and owner, create the four topics, write setup.json
    discover    check the Garmin key, learn which data it returns, write [metrics] (--days N = key lifetime)
                (`garmin` is the old name and still works)
    secrets     generate the kit's own secrets, push the GitHub Actions secrets
    worker      render wrangler.toml, deploy the Cloudflare Worker, point Telegram at it
    test        commit the setup files, run a real morning recap on GitHub, wait for it
    dry-run     the whole recap pipeline offline with sample data
    update      pull improvements from the public kit, keeping profile.toml, setup.json and data/

Every command ends with one line starting "OK:" or "FAILED:" in plain words; exit code 0 / 1.
Secret values are never printed. They live in ~/.config/garmin-life-kit/<repo>.env (mode 600)
and are piped to `gh secret set` / `wrangler secret put` over stdin.

--dry-run fakes every network call and external tool, so the whole setup can be exercised with
no accounts. It still writes profile.toml, setup.json, worker/wrangler.toml and the local env
file, so use it in a scratch copy; it refuses to touch a folder that is already set up for real.

Stdlib only. kit.garmin is imported only inside the `garmin` command.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import re
import secrets as pysecrets
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PROFILE = ROOT / "profile.toml"
SETUP = ROOT / "setup.json"
WORKER_DIR = ROOT / "worker"
TEMPLATE = WORKER_DIR / "wrangler.toml.template"
WRANGLER_TOML = WORKER_DIR / "wrangler.toml"
TOKEN_CACHE = ROOT / "data" / "garmin-token.enc"
WRANGLER = ["npx", "--yes", "wrangler@4"]

HUMAN_SECRETS = ["TELEGRAM_BOT_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "GARMIN_PERSONAL_API_KEY", "WORKER_GITHUB_TOKEN"]
GENERATED_SECRETS = ["GARMIN_CACHE_KEY", "WORKER_API_SECRET", "TELEGRAM_WEBHOOK_SECRET", "SHORTCUT_SECRET"]
ACTIONS_SECRETS = ["TELEGRAM_BOT_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "GARMIN_PERSONAL_API_KEY",
                   "GARMIN_CACHE_KEY", "WORKER_API_SECRET"]
# Worker secret name -> local env name
WORKER_SECRETS = {"TELEGRAM_BOT_TOKEN": "TELEGRAM_BOT_TOKEN", "GITHUB_TOKEN": "WORKER_GITHUB_TOKEN",
                  "WORKER_API_SECRET": "WORKER_API_SECRET", "TELEGRAM_WEBHOOK_SECRET": "TELEGRAM_WEBHOOK_SECRET",
                  "SHORTCUT_SECRET": "SHORTCUT_SECRET"}
TOPICS = {"today": "Today", "fitness": "Fitness", "log": "Log", "alerts": "Alerts"}
HELLO = {
    "today": "Hello. Your morning and evening recaps land here.",
    "fitness": "Hello. A card for each new Garmin activity lands here.",
    "log": "Hello. Type anything here (how you slept, how you feel). It's saved for your recaps, and you get a 👍 when it's saved.",
    "alerts": "Hello. If anything stops working, it shows up here with the fix. Quiet means it works.",
}
BOT_COMMANDS = [
    {"command": "today", "description": "Today: activities, steps, last night"},
    {"command": "week", "description": "This week so far"},
    {"command": "last", "description": "Most recent activity"},
    {"command": "status", "description": "Is everything running"},
    {"command": "help", "description": "Command list"},
]
EXPIRY_CLAUDE = "Claude token (CLAUDE_CODE_OAUTH_TOKEN)"
EXPIRY_GITHUB = "GitHub token for the Worker (WORKER_GITHUB_TOKEN)"
EXPIRY_GARMIN = "Garmin API key (GARMIN_PERSONAL_API_KEY)"

DRY = False
ACCOUNT_ID = ""   # Cloudflare account, when the login has more than one
_SECRET_VALUES: set[str] = set()


class Failed(Exception):
    """One plain-words line for the FAILED: output."""


# ---------- output (always scrubbed) ----------

def scrub(text: str) -> str:
    text = str(text)
    for v in sorted(_SECRET_VALUES, key=len, reverse=True):
        if v:
            text = text.replace(v, "[hidden]")
    return re.sub(r"bot\d+:[A-Za-z0-9_-]{20,}", "bot[hidden]", text)


def say(text: str = "") -> None:
    print(scrub(text), flush=True)


# ---------- local secrets file ----------

def config_dir() -> Path:
    return Path.home() / ".config" / "garmin-life-kit"


def origin_repo() -> str:
    """owner/name from .git/config's origin URL, read as a file (no subprocess)."""
    cfg = ROOT / ".git" / "config"
    if not cfg.is_file():
        return ""
    m = re.search(r'\[remote "origin"\][^\[]*?url\s*=\s*(\S+)', cfg.read_text(errors="replace"))
    if not m:
        return ""
    m2 = re.search(r"github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", m.group(1))
    return f"{m2.group(1)}/{m2.group(2)}" if m2 else ""


_GH_REPO: list = []   # memo of `gh repo view` (one call per run)


def full_repo() -> str:
    """owner/name: setup.json, else the git remote (read as a file), else `gh repo view`. "" if none."""
    repo = load_setup().get("repo") or origin_repo()
    if repo:
        return repo
    if not _GH_REPO:
        try:
            p = run(["gh", "repo", "view", "--json", "nameWithOwner"])
            _GH_REPO.append(json.loads(p.stdout).get("nameWithOwner", "") if p.returncode == 0 else "")
        except (Failed, ValueError, AttributeError):
            _GH_REPO.append("")
    return _GH_REPO[0]


def env_path() -> Path:
    """~/.config/garmin-life-kit/<owner>__<name>.env, so two people on one computer never share secrets."""
    repo = full_repo()
    name = repo.replace("/", "__") if repo else ROOT.name
    return config_dir() / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', name)}.env"


def read_env() -> dict[str, str]:
    path = env_path()
    out: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    _SECRET_VALUES.update(v for v in out.values() if len(v) >= 8)
    return out


def _chmod(path: Path, mode: int) -> None:
    if os.name != "nt":   # Windows has no POSIX modes; the file sits in the user's own profile
        os.chmod(path, mode)


def write_env(updates: dict[str, str]) -> None:
    path = env_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _chmod(path.parent, 0o700)
    lines = path.read_text().splitlines() if path.is_file() else []
    seen = set()
    for i, line in enumerate(lines):
        k = line.split("=", 1)[0].strip()
        if "=" in line and k in updates:
            lines[i] = f"{k}={updates[k]}"
            seen.add(k)
    lines += [f"{k}={v}" for k, v in updates.items() if k not in seen]
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")
    _chmod(path, 0o600)
    _SECRET_VALUES.update(v for v in updates.values() if len(v) >= 8)


def require_env(names, env=None) -> dict[str, str]:
    env = read_env() if env is None else env
    missing = [n for n in names if not env.get(n)]
    if missing:
        human = [n for n in missing if n in HUMAN_SECRETS]
        how = (f" Ask the person to run this in their own terminal: uv run setup/kit.py secret {human[0]}"
               if human else " Run: uv run setup/kit.py secrets")
        raise Failed(f"missing: {', '.join(missing)}.{how}")
    return env


# ---------- setup.json / profile.toml ----------

FAKE_CHAT_ID = -1000000000001   # --dry-run only
FAKE_WORKER_URL = "https://garmin-life-kit.example.workers.dev"


def load_setup() -> dict:
    if not SETUP.is_file():
        return {}
    try:
        return json.loads(SETUP.read_text())
    except json.JSONDecodeError as e:
        raise Failed(f"setup.json is not valid JSON ({e}). Fix or delete it, then run this again.")


def save_setup(data: dict) -> None:
    if DRY:
        guard_dry_run()
        data["dry_run"] = True   # marks made-up values; a real run removes the mark
    else:
        data.pop("dry_run", None)
    SETUP.write_text(json.dumps(data, indent=2) + "\n")


def guard_dry_run() -> None:
    """--dry-run writes made-up ids; never over a real setup."""
    s = load_setup()
    if s and not s.get("dry_run") and (s.get("repo") or s.get("telegram") or s.get("worker")):
        raise Failed("this folder is already set up for real, and --dry-run would overwrite setup.json "
                     "with made-up values. Run --dry-run in a scratch copy of the kit instead.")


def upsert_expiry(data: dict, name: str, expires: date, breaks: str, fix: str) -> None:
    data["expiries"] = [e for e in data.get("expiries", []) if e.get("name") != name] + [
        {"name": name, "expires": expires.isoformat(), "breaks": breaks, "fix": fix}]


def stamp_claude_expiry(data: dict) -> None:
    upsert_expiry(data, EXPIRY_CLAUDE, date.today() + timedelta(days=365),
                  "Recaps go out as numbers only, without Claude's words, and Alerts says Claude didn't write the recap",
                  "In a terminal: claude setup-token. Then in the kit folder: uv run setup/kit.py secret "
                  "CLAUDE_CODE_OAUTH_TOKEN, then uv run setup/kit.py secrets")


def stamp_github_expiry(data: dict, days: int) -> None:
    upsert_expiry(data, EXPIRY_GITHUB, date.today() + timedelta(days=days),
                  "The Worker can't start jobs: no scheduled recaps, commands don't answer, Alerts says Couldn't start",
                  "Make a new token (SETUP.md step 9b), then in the kit folder: uv run setup/kit.py secret "
                  "WORKER_GITHUB_TOKEN --days N, then uv run setup/kit.py worker")


def has_expiry(data: dict, name: str) -> bool:
    return any(e.get("name") == name for e in data.get("expiries", []))


def load_profile() -> dict:
    if not PROFILE.is_file():
        raise Failed("profile.toml is missing. Run this from the kit folder (the one with profile.toml in it).")
    try:
        return tomllib.loads(PROFILE.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise Failed(f"profile.toml has a typo ({e}). Fix that line, then run this again.")


HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def profile_problems(p: dict) -> list[str]:
    """Same checks as kit.profile.load(), each a key and the fix."""
    out = []
    if not str(p.get("name", "")).strip():
        out.append('name is empty: run profile again with --name "First name"')
    tz = p.get("timezone", "")
    if not tz_ok(tz):
        out.append(f'timezone "{tz}" is not a time zone name: use one like America/Denver or Europe/London')
    if p.get("units") not in ("imperial", "metric"):
        out.append('units must be "imperial" or "metric"')
    for k in ("morning", "evening"):
        v = str((p.get("times") or {}).get(k, ""))
        m = HHMM.match(v)
        if not m or int(m.group(2)) % 5:
            out.append(f'times.{k} "{v}" must be 24h HH:MM on a 5-minute mark, like 07:30')
    loc = p.get("location") or {}
    if not -90 <= float(loc.get("lat", 0) or 0) <= 90:
        out.append("location.lat must be between -90 and 90")
    if not -180 <= float(loc.get("lon", 0) or 0) <= 180:
        out.append("location.lon must be between -180 and 180")
    bed = str((p.get("recap") or {}).get("sleep_target_bed", "") or "")
    if bed and not HHMM.match(bed):
        out.append(f'recap.sleep_target_bed "{bed}" must be empty or HH:MM')
    return out


def tz_ok(name: str) -> bool:
    try:
        import zoneinfo
        zoneinfo.ZoneInfo(str(name))
        return True
    except Exception:
        try:
            import zoneinfo
            # No tz database at all (Windows without the tzdata package): can't check here.
            # The Actions runner (Linux) checks it again in kit.profile.load().
            return not zoneinfo.available_timezones() and bool(re.match(r"^[A-Za-z_]+(/[A-Za-z0-9_+-]+)+$", str(name)))
        except Exception:
            return False


def toml_str(s: str) -> str:
    return json.dumps(str(s), ensure_ascii=False)   # JSON string escapes are valid TOML basic-string escapes


def _value_len(rest: str) -> int:
    if rest.startswith('"""') or rest.startswith("'''"):
        raise Failed("can't rewrite a multi-line value in profile.toml. Edit that line by hand.")
    if rest.startswith('"'):
        i = 1
        while i < len(rest):
            if rest[i] == "\\":
                i += 2
                continue
            if rest[i] == '"':
                return i + 1
            i += 1
        raise Failed("profile.toml has an unclosed quote. Fix that line by hand.")
    if rest.startswith("'"):
        j = rest.find("'", 1)
        return j + 1 if j > 0 else len(rest)
    m = re.match(r"[^\s#]*", rest)
    return m.end()


def toml_set(text: str, section: str, key: str, literal: str) -> str:
    """Rewrite one value in place, keeping the comment (and its column, when it fits)."""
    lines = text.splitlines(keepends=True)
    current, in_multi = "", False
    for i, line in enumerate(lines):
        if in_multi:
            in_multi = line.count('"""') % 2 == 0
            continue
        stripped = line.strip()
        header = re.match(r"^\[([A-Za-z0-9_.-]+)\]", stripped)
        if header and not stripped.startswith("[["):
            current = header.group(1)
            continue
        if line.count('"""') % 2 == 1:
            in_multi = True
        if current != section:
            continue
        m = re.match(r"^(\s*" + re.escape(key) + r"\s*=\s*)", line)
        if not m:
            continue
        prefix = m.group(1)
        rest = line[len(prefix):]
        n = _value_len(rest)
        old, tail = rest[:n], rest[n:]
        newline = tail[len(tail.rstrip("\r\n")):]
        body = tail.rstrip("\r\n")
        if body.strip().startswith("#"):
            pad = len(body) - len(body.lstrip(" "))
            column = len(prefix) + len(old) + pad
            body = " " * max(1, column - len(prefix) - len(literal)) + body.lstrip(" ")
        lines[i] = prefix + literal + body + newline
        return "".join(lines)
    where = f"[{section}]" if section else "the top"
    raise Failed(f"profile.toml has no {key} line under {where}. Copy profile.toml from the template, then run this again.")


# ---------- network and subprocess (faked in --dry-run) ----------

def http(method: str, url: str, payload=None, headers=None, timeout: int = 30):
    """(status, parsed JSON or text). Never raises; a network failure is status 0."""
    if DRY:
        return FAKE.http(method, url, payload)
    data = json.dumps(payload).encode() if payload is not None else None
    h = {"User-Agent": "garmin-life-kit-setup", "Accept": "application/json", **(headers or {})}
    if data is not None:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    except (urllib.error.URLError, OSError, ValueError) as e:
        return 0, {"description": f"network error: {getattr(e, 'reason', e)}"}
    text = raw.decode("utf-8", errors="replace")
    try:
        return status, json.loads(text)
    except json.JSONDecodeError:
        return status, text


def tg(token: str, method: str, **params) -> dict:
    """Telegram Bot API call. Returns the JSON with "_status" added. Never prints the URL."""
    status, body = http("POST", f"https://api.telegram.org/bot{token}/{method}", params)
    if not isinstance(body, dict):
        body = {"ok": False, "description": str(body)[:200]}
    body["_status"] = status
    return body


def child_env() -> dict:
    env = {**os.environ, "PYTHONUTF8": "1"}   # Windows consoles default to cp1252
    if ACCOUNT_ID:
        env["CLOUDFLARE_ACCOUNT_ID"] = ACCOUNT_ID
    return env


def run(cmd: list[str], input: str | None = None, cwd: Path = ROOT, timeout: int = 900,
        capture: bool = True) -> subprocess.CompletedProcess:
    if DRY:
        return FAKE.run(cmd, input)
    exe = shutil.which(cmd[0])
    if not exe:
        raise Failed(f"{cmd[0]} is not installed on this computer. Run: uv run setup/kit.py check")
    try:
        return subprocess.run([exe, *cmd[1:]], input=input, cwd=cwd, timeout=timeout, text=True,
                              capture_output=capture, encoding="utf-8", errors="replace", env=child_env(),
                              stdin=None if input is not None else subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise Failed(f"{' '.join(cmd[:3])} took too long and was stopped. Run this again.")


ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def output(p: subprocess.CompletedProcess) -> str:
    return ANSI.sub("", (p.stdout or "") + "\n" + (p.stderr or ""))


def last_line(text: str) -> str:
    lines = [l.strip(" ▲✘") for l in text.splitlines() if l.strip() and not l.strip().startswith(("⛅", "───"))]
    errs = [l for l in lines if "ERROR" in l or "rror" in l]
    return scrub((errs or lines or ["no output"])[-1])[:300]


# ---------- wrangler output parsing (4.x) ----------

def parse_kv_id(text: str) -> str:
    text = ANSI.sub("", text)
    for pat in (r'"id"\s*:\s*"([0-9a-f]{32})"', r'\bid\s*=\s*"([0-9a-f]{32})"', r"\b([0-9a-f]{32})\b"):
        m = re.search(pat, text)
        if m:
            return m.group(1)
    return ""


def parse_worker_url(text: str, name: str = "") -> str:
    urls = re.findall(r"https://[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.workers\.dev\b", ANSI.sub("", text))
    for u in urls:
        if name and u.lower().startswith(f"https://{name.lower()}."):
            return u
    return urls[0] if urls else ""


def is_multiple_accounts(text: str) -> bool:
    return "more than one account" in text.lower()


MULTIPLE_ACCOUNTS = ("this Cloudflare login has more than one account. Run: npx --yes wrangler@4 whoami, pick the "
                     "account id to use, then run: uv run setup/kit.py worker --account-id ID")


def is_no_subdomain(text: str) -> bool:
    t = text.lower()
    return "workers.dev subdomain" in t or "/workers/onboarding" in t


def parse_kv_list(text: str, title: str) -> str:
    text = ANSI.sub("", text)
    start, end = text.find("["), text.rfind("]")
    try:
        items = json.loads(text[start:end + 1]) if start >= 0 else []
    except json.JSONDecodeError:
        return ""
    for ns in items:
        if ns.get("title") == title:
            return ns.get("id", "")
    for ns in items:
        if str(ns.get("title", "")).endswith(title):
            return ns.get("id", "")
    return ""


# ---------- repo ----------

def ensure_repo(data: dict) -> str:
    """owner/name of the person's private copy. Saved in setup.json."""
    p = run(["gh", "repo", "view", "--json", "nameWithOwner,isPrivate"])
    if p.returncode != 0:
        raise Failed("GitHub couldn't find this folder's repo (" + last_line(output(p)) + "). Check `gh auth status` "
                     "and that this folder is the person's own copy (`git remote get-url origin`).")
    info = json.loads(p.stdout)
    repo = info["nameWithOwner"]
    if data.get("repo") and data["repo"] != repo:
        raise Failed(f"setup.json says the repo is {data['repo']} but this folder is {repo}. Fix \"repo\" in setup.json.")
    if not info.get("isPrivate"):
        raise Failed(f"{repo} is public, and it will hold the person's Log notes. Make it private first: "
                     f"gh repo edit {repo} --visibility private --accept-visibility-change-consequences")
    if data.get("repo") != repo:
        data["repo"] = repo
        save_setup(data)
    return repo


def gh_secret(repo: str, name: str, value: str) -> None:
    p = run(["gh", "secret", "set", name, "--repo", repo], input=value)
    if p.returncode != 0:
        raise Failed(f"GitHub didn't accept the secret {name} ({last_line(output(p))}). Check `gh auth status`; "
                     "if it mentions scopes, run: gh auth refresh -s repo")


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    return (s or "garmin-life-kit")[:54]


# ========== commands ==========

def cmd_status(args) -> str:
    done, todo = [], []
    p = load_profile()
    probs = profile_problems(p)
    loc = p.get("location") or {}
    if not probs and (loc.get("lat") or loc.get("lon")):
        say(f"Profile: filled in ({p['name']}, {p['timezone']}, {loc.get('label') or 'location set'}, "
            f"morning {p['times']['morning']}, evening {p['times']['evening']})")
        done.append("profile")
    else:
        say("Profile: not finished (" + ("; ".join(probs) or "no city yet") + ")")
        todo.append(("profile", "SETUP.md step 5"))

    env = read_env()
    say(f"Secrets on this computer ({env_path()}):")
    for n in HUMAN_SECRETS + GENERATED_SECRETS:
        say(f"  {n}: {'yes' if env.get(n) else 'no'}")

    s = load_setup()
    t = s.get("telegram") or {}
    topics = t.get("topics") or {}
    w = s.get("worker") or {}
    names = [e.get("name") for e in s.get("expiries", [])]
    say("setup.json:")
    say(f"  repo: {s.get('repo') or 'not yet'}")
    say(f"  Telegram group: {'yes' if t.get('chat_id') else 'not yet'}"
        + (f" (owner id {'set' if t.get('owner_id') else 'MISSING'}, {sum(1 for k in TOPICS if topics.get(k))} of 4 topics)" if t.get("chat_id") else ""))
    for e in s.get("expiries", []):
        say(f"  expires {e.get('expires')}: {e.get('name')}")
    say(f"  Worker: {w.get('url') or 'not deployed yet'}")

    if not env.get("TELEGRAM_BOT_TOKEN"):
        todo.append(("Telegram bot token", "SETUP.md step 6a"))
    if t.get("chat_id") and t.get("owner_id") and all(topics.get(k) for k in TOPICS):
        done.append("telegram")
    else:
        todo.append(("Telegram group", "SETUP.md step 6b"))
    disc = s.get("discovered") or {}
    if disc.get("date"):
        say(f"  Garmin data check: ran {disc['date']} ({len(disc.get('available') or [])} kinds of data found)")
    else:
        say("  Garmin data check: not run yet")
    if env.get("GARMIN_PERSONAL_API_KEY") and EXPIRY_GARMIN in names and disc.get("date"):
        done.append("garmin")
    else:
        todo.append(("Garmin key and data check", "SETUP.md step 7"))
    for n, step in (("CLAUDE_CODE_OAUTH_TOKEN", "9a"), ("WORKER_GITHUB_TOKEN", "9b")):
        if not env.get(n):
            todo.append((n, f"SETUP.md step {step}"))
    if s.get("repo") and all(env.get(n) for n in GENERATED_SECRETS) and EXPIRY_CLAUDE in names:
        done.append("secrets")
    else:
        todo.append(("push the secrets", "SETUP.md step 9c"))
    if w.get("url"):
        st, body = http("GET", w["url"].rstrip("/") + "/health", timeout=10)
        up = st == 200 and isinstance(body, dict) and body.get("ok")
        say(f"  Worker answering: {'yes' if up else 'NO'}")
        if up:
            done.append("worker")
        else:
            todo.append(("Worker (not answering)", "SETUP.md step 10"))
    else:
        todo.append(("deploy the Worker", "SETUP.md step 10"))
    if not todo:
        return "Setup is complete. If the first recap hasn't arrived yet, run: uv run setup/kit.py test"
    first, step = todo[0]
    return f"{len(done)} of 5 setup parts are done (profile, Telegram, Garmin, secrets, Worker). Next: {first} ({step})."


FIXES = {
    "git": {"darwin": "brew install git", "win32": "winget install --id Git.Git -e --source winget",
            "other": "install git from https://git-scm.com/downloads"},
    "gh": {"darwin": "brew install gh", "win32": "winget install --id GitHub.cli -e --source winget",
           "other": "install GitHub CLI from https://cli.github.com"},
    "node": {"darwin": "brew install node", "win32": "winget install --id OpenJS.NodeJS.LTS -e --source winget",
             "other": "install Node.js 18 or newer from https://nodejs.org"},
    "uv": {"darwin": "brew install uv", "win32": "winget install --id astral-sh.uv -e --source winget",
           "other": "curl -LsSf https://astral.sh/uv/install.sh | sh"},
    "claude": {"darwin": "curl -fsSL https://claude.ai/install.sh | bash",
               "win32": "irm https://claude.ai/install.ps1 | iex",
               "other": "curl -fsSL https://claude.ai/install.sh | bash"},
}


def fix_for(tool: str) -> str:
    plat = sys.platform if sys.platform in ("darwin", "win32") else "other"
    return FIXES[tool][plat]


def cmd_check(args) -> str:
    missing = []

    def version(cmd):
        p = run(cmd)
        return ((p.stdout or p.stderr or "").strip().splitlines() or ["installed"])[0] if p.returncode == 0 else ""

    py = sys.version_info
    say(f"python: {py.major}.{py.minor}.{py.micro}" + ("" if py >= (3, 11) else "  (need 3.11+)"))
    if py < (3, 11):
        missing.append("python 3.11+ (uv installs it: uv python install 3.12)")
    for tool, cmd in (("git", ["git", "--version"]), ("gh", ["gh", "--version"]), ("uv", ["uv", "--version"]),
                      ("claude", ["claude", "--version"])):
        v = version(cmd) if (DRY or shutil.which(tool)) else ""
        say(f"{tool}: {v or 'MISSING  -> ' + fix_for(tool)}")
        if not v:
            missing.append(tool)
        elif tool == "gh":
            ok = run(["gh", "auth", "status"]).returncode == 0
            say(f"gh login: {'yes' if ok else 'NO  -> gh auth login --hostname github.com --git-protocol https --web'}")
            if not ok:
                missing.append("gh login")
    v = version(["node", "--version"]) if (DRY or shutil.which("node")) else ""
    m = re.match(r"v?(\d+)", v)
    node_ok = bool(m and int(m.group(1)) >= 18)
    say(f"node: {v or 'MISSING'}" + ("" if node_ok else f"  -> {fix_for('node')}"))
    if not node_ok:
        missing.append("node 18+")
    npx = DRY or bool(shutil.which("npx"))
    say(f"npx: {'yes' if npx else 'MISSING (comes with Node.js)'}")
    if not npx:
        missing.append("npx")
    if missing:
        raise Failed(f"missing: {', '.join(missing)}. Install them (the fix is printed next to each), then run this again.")
    return "Everything the kit needs is installed."


SECRET_HELP = {
    "TELEGRAM_BOT_TOKEN": "the bot token from BotFather",
    "CLAUDE_CODE_OAUTH_TOKEN": "the token from `claude setup-token`",
    "GARMIN_PERSONAL_API_KEY": "the Garmin API key",
    "WORKER_GITHUB_TOKEN": "the GitHub token (starts with github_pat_)",
}


def check_secret_format(name: str, v: str) -> None:
    if name == "TELEGRAM_BOT_TOKEN" and not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{30,}", v):
        raise Failed("that doesn't look like a bot token. Copy just the token from BotFather "
                     "(it looks like 123456789:AAH...), then run this again.")
    if name == "CLAUDE_CODE_OAUTH_TOKEN" and not (v.startswith("sk-ant-") and len(v) >= 40):
        raise Failed("that doesn't look like a Claude token. It starts with sk-ant-oat and is long; copy all of it "
                     "from the `claude setup-token` output, then run this again.")
    if name == "GARMIN_PERSONAL_API_KEY" and (len(v) < 16 or not re.fullmatch(r"[\x21-\x7e]+", v)):
        raise Failed("that looks too short to be a Garmin API key. Copy the whole key Garmin showed, then run this again.")
    if name == "WORKER_GITHUB_TOKEN":
        if v.startswith(("ghp_", "gho_")):
            raise Failed("that's a classic GitHub token. Make a fine-grained one instead (SETUP.md step 9b, "
                         "it starts with github_pat_), then run this again.")
        if not (v.startswith("github_pat_") and len(v) >= 40):
            raise Failed("that doesn't look like a GitHub token. It starts with github_pat_; copy all of it, then run this again.")


def cmd_secret(args) -> str:
    name = args.name
    if name not in HUMAN_SECRETS:
        raise Failed(f"{name} isn't one you paste. Use one of: {', '.join(HUMAN_SECRETS)}.")
    prompt = f"Paste {SECRET_HELP[name]} and press Return (nothing shows while you paste): "
    if sys.stdin.isatty():
        value = getpass.getpass(prompt)
    else:
        value = sys.stdin.readline()
    value = "".join(value.split()).strip("'\"")
    if not value:
        raise Failed(f"nothing was entered. The person runs this in their own terminal window: uv run setup/kit.py secret {name}")
    check_secret_format(name, value)
    if args.days is not None and (name != "WORKER_GITHUB_TOKEN" or args.days < 1):
        raise Failed("--days only goes with WORKER_GITHUB_TOKEN, and must be 1 or more (leave it out for no expiry).")
    write_env({name: value})
    note = ""
    if name == "CLAUDE_CODE_OAUTH_TOKEN":   # a new token means a new year
        data = load_setup()
        stamp_claude_expiry(data)
        save_setup(data)
        note = f" Its expiry is noted as {date.today() + timedelta(days=365)}."
    elif name == "WORKER_GITHUB_TOKEN":
        data = load_setup()
        if args.days:
            stamp_github_expiry(data, args.days)
            note = f" Its expiry is noted as {date.today() + timedelta(days=args.days)}."
        elif has_expiry(data, EXPIRY_GITHUB):
            data["expiries"] = [e for e in data["expiries"] if e.get("name") != EXPIRY_GITHUB]
            note = " No expiry for it (the old expiry date was removed)."
        if note:
            save_setup(data)
    return f"Saved {name} on this computer only ({env_path()}). The value is hidden and was not shown anywhere.{note}"


US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
    "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}


def geocode(city: str) -> dict:
    parts = [x.strip() for x in city.split(",") if x.strip()]
    if not parts:
        raise Failed('--city is empty. Use a city name like "Denver, CO" or "Lyon, France".')
    q = urllib.parse.urlencode({"name": parts[0], "count": 10, "language": "en", "format": "json"})
    status, body = http("GET", f"https://geocoding-api.open-meteo.com/v1/search?{q}")
    if status != 200 or not isinstance(body, dict):
        raise Failed("couldn't reach the free city lookup (Open-Meteo). Check the internet connection and run this "
                     "again, or pass --lat and --lon yourself.")
    results = body.get("results") or []
    if not results:
        raise Failed(f'the city lookup found nothing for "{parts[0]}". Check the spelling, or pass --lat and --lon yourself.')
    hints = [US_STATES.get(h.upper(), h).lower() for h in parts[1:]]

    def matches(r, field):
        return any(h and str(r.get(field, "")).lower().startswith(h) for h in hints)

    for field in ("admin1", "country", "country_code"):
        for r in results:
            if hints and matches(r, field):
                return r
    return results[0]


def cmd_profile(args) -> str:
    text = PROFILE.read_text(encoding="utf-8") if PROFILE.is_file() else ""
    load_profile()
    changes: list[tuple[str, str, str]] = []
    if args.name is not None:
        changes.append(("", "name", toml_str(args.name.strip())))
    if args.timezone is not None:
        if not tz_ok(args.timezone):
            raise Failed(f'"{args.timezone}" is not a time zone name. Use one like America/Denver or Europe/London.')
        changes.append(("", "timezone", toml_str(args.timezone)))
    if args.units is not None:
        if args.units not in ("imperial", "metric"):
            raise Failed('--units must be "imperial" or "metric".')
        changes.append(("", "units", toml_str(args.units)))
    for k in ("morning", "evening"):
        v = getattr(args, k)
        if v is not None:
            m = HHMM.match(v)
            if not m or int(m.group(2)) % 5:
                raise Failed(f"--{k} {v} must be 24h HH:MM on a 5-minute mark, like 07:30 or 21:00.")
            changes.append(("times", k, toml_str(v)))
    if args.wears is not None:
        changes.append(("devices", "wears", toml_str(args.wears.strip())))
    place = None
    if args.lat is not None or args.lon is not None:
        if args.lat is None or args.lon is None or not (-90 <= args.lat <= 90 and -180 <= args.lon <= 180):
            raise Failed("--lat and --lon go together: lat between -90 and 90, lon between -180 and 180.")
        lat, lon = args.lat, args.lon
    elif args.city:
        place = geocode(args.city)
        lat, lon = place["latitude"], place["longitude"]
    if args.city or args.lat is not None:
        changes += [("location", "label", toml_str((args.city or "").strip())),
                    ("location", "lat", repr(round(float(lat), 2))),
                    ("location", "lon", repr(round(float(lon), 2)))]
    if not changes:
        raise Failed("nothing to change. Pass at least one of --name --timezone --units --city --morning --evening --wears.")
    for section, key, literal in changes:
        if (section, key) == ("devices", "wears"):
            text = toml_put(text, section, key, literal, DEVICES_HEADER)
        else:
            text = toml_set(text, section, key, literal)
    tomllib.loads(text)   # never write a file that doesn't parse
    PROFILE.write_text(text, encoding="utf-8")
    p = load_profile()
    if place:
        found = ", ".join(str(x) for x in (place.get("name"), place.get("admin1"), place.get("country")) if x)
        say(f"Found {found} ({round(lat, 2)}, {round(lon, 2)}).")
        if place.get("timezone") and place["timezone"] != p.get("timezone"):
            say(f"Note: that place is in {place['timezone']}, but the profile's timezone is {p.get('timezone')}.")
    probs = profile_problems(p)
    if probs:
        raise Failed("profile.toml saved, but " + "; ".join(probs) + ".")
    loc = p["location"]
    return (f"profile.toml saved: {p['name']}, {p['timezone']}, {p['units']}, "
            f"{loc.get('label') or 'location'} ({loc['lat']}, {loc['lon']}), "
            f"morning {p['times']['morning']}, evening {p['times']['evening']}.")


# ---------- telegram ----------

def find_groups(updates: list) -> dict:
    """chat id -> {title, is_forum, owner (latest human sender id), name, basic}. Follows group->supergroup
    moves. A plain "group" (basic=True) is one with Topics off: Telegram makes it a supergroup when Topics go on."""
    groups: dict = {}
    moved: dict = {}
    basic: dict = {}
    for u in sorted(updates, key=lambda u: u.get("update_id", 0)):
        for key in ("message", "edited_message", "my_chat_member", "chat_member"):
            obj = u.get(key) or {}
            chat = obj.get("chat") or {}
            if key == "message" and obj.get("migrate_to_chat_id"):
                moved[chat.get("id")] = obj["migrate_to_chat_id"]
            if chat.get("type") == "group":
                basic[chat["id"]] = chat.get("title") or "untitled"
            if chat.get("type") != "supergroup":
                continue
            g = groups.setdefault(chat["id"], {"title": chat.get("title") or "untitled", "is_forum": False,
                                               "owner": None, "name": ""})
            g["title"] = chat.get("title") or g["title"]
            g["is_forum"] = g["is_forum"] or bool(chat.get("is_forum"))
            frm = obj.get("from") or {}
            if key in ("message", "edited_message") and frm.get("id") and not frm.get("is_bot"):
                g["owner"], g["name"] = frm["id"], frm.get("first_name", "")
    for u in updates:   # a human message sent before Topics turned the group into a supergroup
        m = u.get("message") or {}
        new = moved.get((m.get("chat") or {}).get("id"))
        frm = m.get("from") or {}
        if new in groups and not groups[new]["owner"] and frm.get("id") and not frm.get("is_bot"):
            groups[new]["owner"], groups[new]["name"] = frm["id"], frm.get("first_name", "")
    for cid, title in basic.items():
        if cid not in moved:
            groups.setdefault(cid, {"title": title, "is_forum": False, "owner": None, "name": "", "basic": True})
    return groups


def cmd_telegram(args) -> str:
    token = require_env(["TELEGRAM_BOT_TOKEN"])["TELEGRAM_BOT_TOKEN"]
    me = tg(token, "getMe")
    if not me.get("ok"):
        if me["_status"] in (401, 404):
            raise Failed("Telegram didn't accept the bot token. In BotFather send /token, pick the bot, then "
                         "the person runs: uv run setup/kit.py secret TELEGRAM_BOT_TOKEN")
        raise Failed(f"couldn't reach Telegram ({me.get('description')}). Check the internet connection and run this again.")
    bot = me["result"]
    data = load_setup()
    t = data.setdefault("telegram", {})
    notes = []

    chat_id = args.chat_id or t.get("chat_id")
    owner = t.get("owner_id") if chat_id and chat_id == t.get("chat_id") else None
    title = ""
    if not chat_id or not owner:
        up = tg(token, "getUpdates", timeout=0)
        if up["_status"] == 409:
            d = tg(token, "deleteWebhook")
            if not d.get("ok"):
                raise Failed(f"a webhook is set on this bot and Telegram wouldn't remove it ({d.get('description')}). Run this again.")
            notes.append("The bot's webhook was on, so I turned it off to read messages. "
                         "Run `uv run setup/kit.py worker` again afterwards to turn it back on.")
            say(notes[-1])
            up = tg(token, "getUpdates", timeout=0)
        if not up.get("ok"):
            raise Failed(f"Telegram wouldn't give the bot's recent messages ({up.get('description')}). Run this again.")
        groups = find_groups(up.get("result") or [])
        if chat_id:
            g = groups.get(int(chat_id)) or {}
            owner, title = g.get("owner"), g.get("title", "")
        else:
            forums = {k: v for k, v in groups.items() if v["is_forum"]}
            supers = {k: v for k, v in groups.items() if not v.get("basic")}
            pool = forums or supers
            if not pool and groups:
                titles = ", ".join(f'"{v["title"]}"' for v in groups.values())
                raise Failed(f"topics are off in {titles}. In Telegram: open the group, tap its name, Edit, turn on "
                             "Topics, save. Then send a new message in the group and run this again.")
            if not pool:
                raise Failed("found no group. Send any message in your new group, then run this again. "
                             "(The bot must be in the group and an admin, or it can't see messages.)")
            if len(pool) > 1:
                listing = "; ".join(f'"{v["title"]}" --chat-id {k}' for k, v in pool.items())
                raise Failed(f"found several groups: {listing}. Run again with the right one, e.g. "
                             f"uv run setup/kit.py telegram --chat-id {next(iter(pool))}")
            chat_id, g = next(iter(pool.items()))
            owner, title = g["owner"], g["title"]
        if not owner:
            raise Failed("found the group, but no message from you in it yet. Send any message in your new group "
                         "(as yourself, not anonymously), then run this again.")
    chat_id = int(chat_id)

    chat = tg(token, "getChat", chat_id=chat_id)
    if not chat.get("ok"):
        raise Failed(f"Telegram can't find that group for this bot ({chat.get('description')}). Add the bot to the group, "
                     "send a message there, and run this again.")
    title = chat["result"].get("title") or title
    if not chat["result"].get("is_forum"):
        raise Failed(f'topics are off in "{title}". In Telegram: open the group, tap its name, Edit, turn on Topics, '
                     "save. Then send a new message in the group and run this again.")
    mem = tg(token, "getChatMember", chat_id=chat_id, user_id=bot["id"])
    r = mem.get("result") or {}
    if not (mem.get("ok") and r.get("status") == "administrator" and r.get("can_manage_topics")):
        raise Failed(f'the bot is not an admin with Manage Topics in "{title}". In Telegram: group name, Edit, '
                     f"Administrators, Add Admin, pick @{bot.get('username')}, turn on Manage Topics, save. Then run this again.")

    if t.get("chat_id") != chat_id:
        t["topics"] = {}
    t.update({"chat_id": chat_id, "owner_id": int(owner), "bot_username": bot.get("username", "")})
    topics = t.setdefault("topics", {})
    save_setup(data)
    for key, name in TOPICS.items():
        for attempt in (1, 2):
            if not topics.get(key):
                made = tg(token, "createForumTopic", chat_id=chat_id, name=name)
                if not made.get("ok"):
                    raise Failed(f'couldn\'t create the {name} topic ({made.get("description")}). Check the bot is an admin '
                                 "with Manage Topics, then run this again.")
                topics[key] = made["result"]["message_thread_id"]
                save_setup(data)
                say(f"Created topic {name}.")
            sent = tg(token, "sendMessage", chat_id=chat_id, message_thread_id=topics[key], text=HELLO[key],
                      disable_notification=True)
            if sent.get("ok"):
                break
            if attempt == 1 and "thread not found" in str(sent.get("description", "")).lower():
                say(f"The {name} topic was deleted. Making it again.")
                topics[key] = None
                continue
            raise Failed(f"couldn't post in the {name} topic ({sent.get('description')}). Run this again.")
    save_setup(data)
    return (f'Telegram is ready: group "{title}" has Today, Fitness, Log and Alerts topics with a hello in each, '
            f"and the owner is set. {' '.join(notes)}").strip()


# ---------- discover (garmin) ----------

METRICS_HEADER = ("[metrics]           # written by `uv run setup/kit.py discover`; true = shown in recaps, cards and the MCP tools.\n"
                  "                    # Trim in plain words (\"drop stress\") by setting false. Re-run discover after a new device.\n")
DEVICES_HEADER = "[devices]\n"
DEVICES_WEARS = 'wears = ""          # what they told Claude they wear, e.g. "Forerunner 265, HRM-Pro strap". Explains gaps.\n'


def fernet_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


def local_today() -> date:
    try:
        import zoneinfo
        return datetime.now(zoneinfo.ZoneInfo(load_profile().get("timezone", "UTC"))).date()
    except Exception:
        return date.today()


def import_kit(*names):
    """Import kit.<name> from this repo (lazily). This script's folder must not shadow the package."""
    import importlib
    sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != HERE]
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    loaded = sys.modules.get("kit")
    if loaded is not None and not str(getattr(loaded, "__file__", "") or "").startswith(str(ROOT)):
        for n in [n for n in sys.modules if n == "kit" or n.startswith("kit.")]:
            del sys.modules[n]
    try:
        return [importlib.import_module(f"kit.{n}") for n in names]
    except Exception as e:
        raise Failed(f"couldn't load the kit's own code ({type(e).__name__}: {e}). Run it from the kit folder "
                     "with: uv run setup/kit.py discover --days N")


def toml_put(text: str, section: str, key: str, literal: str, header: str = "") -> str:
    """Like toml_set, but adds the key (or the whole section) when it isn't there yet."""
    try:
        return toml_set(text, section, key, literal)
    except Failed:
        return toml_insert(text, section, f"{key} = {literal}\n", header)


def toml_insert(text: str, section: str, new: str, header: str = "") -> str:
    """Insert raw key line(s) after the last key of a section; append the section if it's missing."""
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    current, in_multi = "", False
    start = last_key = (-1 if section == "" else None)
    for i, line in enumerate(lines):
        if in_multi:
            in_multi = line.count('"""') % 2 == 0
            if not in_multi and current == section:
                last_key = i
            continue
        stripped = line.strip()
        h = re.match(r"^\[([A-Za-z0-9_.-]+)\]", stripped)
        if h and not stripped.startswith("[["):
            current = h.group(1)
            if current == section:
                start = last_key = i
            continue
        if line.count('"""') % 2 == 1:
            in_multi = True
        if current == section and re.match(r"^\s*[A-Za-z0-9_-]+\s*=", line):
            last_key = i
    if start is None:   # no such section: append it
        lines += ["\n", header or f"[{section}]\n", new]
    else:
        lines.insert(last_key + 1, new)
    return "".join(lines)


def garmin_data_type(message: str) -> str:
    """Which data an error was about, from the Personal API path in kit.garmin's message."""
    m = message.lower()
    for needle, label in (("hrv", "HRV"), ("sleep", "sleep"), ("usersummary", "the daily summary"),
                          ("summary", "the daily summary"), ("activit", "activities")):
        if needle in m:
            return label
    return "the data check"


def discover_today() -> date:
    """Local today; in a dry run the fixture day (KIT_TODAY or kit.profile.FIXTURE_TODAY), so fixtures match."""
    if DRY:
        pinned = os.environ.get("KIT_TODAY")
        if not pinned:
            try:
                (kp,) = import_kit("profile")
                pinned = getattr(kp, "FIXTURE_TODAY", None)
            except Failed:
                pinned = None
        if pinned:
            return date.fromisoformat(str(pinned))
    return local_today()


def cmd_discover(args) -> str:
    if not args.days or args.days < 1:
        raise Failed("say how many days the Garmin key lasts: uv run setup/kit.py discover --days 365")
    env = require_env(["GARMIN_PERSONAL_API_KEY"])
    if not env.get("GARMIN_CACHE_KEY"):
        write_env({"GARMIN_CACHE_KEY": fernet_key()})
        env = read_env()
    os.environ["GARMIN_CACHE_KEY"] = env["GARMIN_CACHE_KEY"]   # RepoCache reads it from the environment
    os.environ.pop("KIT_DATA_DIR", None)   # the token cache must land in data/, where `test` commits it
    if DRY:
        os.environ["KIT_DRY_RUN"] = "1"    # kit.garmin's DryRunClient reads fixtures (KIT_DRY_FIXTURES picks the set)
    else:
        os.environ.pop("KIT_DRY_RUN", None)
    g, metrics, discover = import_kit("garmin", "metrics", "discover")
    METRICS = metrics.METRICS

    # Exactly one key exchange at most: one Client, token cached in data/garmin-token.enc so `test`
    # commits it and the first Actions runs reuse it. Never retried here: Garmin rate-limits exchanges.
    try:
        client = FAKE.garmin_client(g) if DRY else g.Client(env["GARMIN_PERSONAL_API_KEY"], g.RepoCache())
        res = discover.run(client, discover_today())
    except g.KeyRejected:
        raise Failed("key rejected: Garmin didn't accept the API key. Make a new one at "
                     "connect.garmin.com/app/settings/personalApiToken, then the person runs: "
                     "uv run setup/kit.py secret GARMIN_PERSONAL_API_KEY")
    except g.RateLimited:
        raise Failed("rate limited: Garmin is limiting key swaps right now. Wait 30-60 minutes, then run this "
                     "again once. Don't retry in a loop.")
    except g.ExchangeFailed as e:
        raise Failed(f"couldn't reach Garmin ({e}). Check the internet connection and run this again in a few minutes.")
    except g.GarminError as e:
        raise Failed(f"Garmin gave an error for {garmin_data_type(str(e))} ({e}). That's not something the person did. "
                     "Run this again in a few minutes; if it says the same, the kit needs a fix.")

    # "unknown" = nothing to judge by (no sleep record, no activities): those switches stay exactly as they are.
    unknown = {n: r for n, r in (res.get("unknown") or {}).items() if n in METRICS}
    now = [n for n in METRICS if n in set(res.get("available") or []) and n not in unknown]
    types = res.get("activity_types") or {}
    data = load_setup()
    expires = date.today() + timedelta(days=args.days)
    upsert_expiry(data, EXPIRY_GARMIN, expires,
                  "Garmin data stops: recaps go out without Garmin numbers, no activity cards, and Alerts says the key was rejected",
                  "Make a new key at connect.garmin.com/app/settings/personalApiToken, then in the kit folder: "
                  "uv run setup/kit.py secret GARMIN_PERSONAL_API_KEY, uv run setup/kit.py discover --days N, "
                  "uv run setup/kit.py secrets")
    if not now and "sleep" in unknown:
        save_setup(data)
        raise Failed("the key works, but Garmin sent no data for the days checked. Open the Garmin Connect app on the "
                     "phone so the watch syncs, then run this again.")

    prev = (data.get("discovered") or {}).get("available")
    prev = set(prev) if isinstance(prev, list) else None
    current = (load_profile().get("metrics") or {})
    text = PROFILE.read_text(encoding="utf-8")
    for name, spec in METRICS.items():
        label = spec[0] if isinstance(spec, (tuple, list)) else name
        if name in unknown:
            say(f"{label}: not checked ({unknown[name]})")
            continue
        if name not in now:
            value, line = False, f"{label}: not found"
        elif prev is not None and name not in prev:
            value, line = True, f"{label}: found"
        else:   # found before and now (or no earlier run on record): keep a choice to turn it off
            value = current.get(name, True) is not False
            line = f"{label}: found" + ("" if value else " (kept off, as you set it)")
        say(line)
        text = toml_put(text, "metrics", name, "true" if value else "false", METRICS_HEADER)
    for name, reason in (res.get("not_in_kit") or {"weight": "not available through the kit yet"}).items():
        say(f"{name[:1].upper() + name[1:]}: {reason}")
    tomllib.loads(text)
    PROFILE.write_text(text, encoding="utf-8")
    if types:
        say("Activity types, last 14 days: " + ", ".join(f"{k} {v}" for k, v in
                                                        sorted(types.items(), key=lambda kv: (-kv[1], kv[0]))))
    else:
        say("Activity types, last 14 days: none")

    data["discovered"] = {"date": date.today().isoformat(), "available": now, "activity_types": types}
    save_setup(data)
    missing = [(METRICS[n][0] if isinstance(METRICS[n], (tuple, list)) else n).split(" (")[0]
               for n in METRICS if n not in now and n not in unknown]
    gist = f"Found {len(now)} of {len(METRICS)} kinds of data" + (f" (not found: {', '.join(missing)})" if missing else "")
    if unknown:
        gist += f"; {len(unknown)} not checked yet and left as they were"
    acts = sum(types.values())
    return (f"The Garmin key works. {gist}, and {acts} activities in the last 14 days. "
            f"profile.toml [metrics] is updated. Key expires {expires}.")


# ---------- secrets ----------

def cmd_secrets(args) -> str:
    data = load_setup()
    repo = ensure_repo(data)
    env = read_env()
    new = {}
    for n in GENERATED_SECRETS:
        if not env.get(n):
            new[n] = fernet_key() if n == "GARMIN_CACHE_KEY" else pysecrets.token_hex(32)
    if new:
        write_env(new)
        say(f"Made new: {', '.join(new)}.")
        env = read_env()
    require_env(["TELEGRAM_BOT_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "GARMIN_PERSONAL_API_KEY"], env)

    noted = []
    if not has_expiry(data, EXPIRY_CLAUDE):
        stamp_claude_expiry(data)
        noted.append(f"Claude token expiry noted as {date.today() + timedelta(days=365)}.")
    if args.github_token_days:
        stamp_github_expiry(data, args.github_token_days)
        noted.append(f"GitHub token expiry noted as {date.today() + timedelta(days=args.github_token_days)}.")
    save_setup(data)

    names = list(ACTIONS_SECRETS)
    url = (data.get("worker") or {}).get("url")
    for n in names:
        gh_secret(repo, n, env[n])
        say(f"GitHub secret {n}: set")
    if url:
        gh_secret(repo, "WORKER_URL", url)
        say("GitHub secret WORKER_URL: set")
    count = len(names) + (1 if url else 0)
    return f"Pushed {count} secrets to {repo} on GitHub (values hidden). {' '.join(noted)}".strip()


# ---------- worker ----------

def render_wrangler(profile: dict, data: dict, name: str, kv_id: str) -> str:
    t = data["telegram"]
    topics = t.get("topics") or {}
    values = {
        "WORKER_NAME": name, "KV_ID": kv_id, "REPO": data["repo"], "TIMEZONE": profile["timezone"],
        "MORNING": profile["times"]["morning"], "EVENING": profile["times"]["evening"],
        "CHAT_ID": str(t["chat_id"]), "OWNER_ID": str(t["owner_id"]),
        "ALERTS_THREAD": str(topics.get("alerts", "")), "LOG_THREAD": str(topics.get("log", "")),
        "FEATURES": json.dumps({k: bool(v) for k, v in (profile.get("features") or {}).items()}, separators=(",", ":")),
    }
    text = TEMPLATE.read_text(encoding="utf-8")
    for k, v in values.items():
        text = text.replace("{{" + k + "}}", toml_str(v))
    left = re.findall(r"\{\{\w+\}\}", text)
    if left:
        raise Failed(f"worker/wrangler.toml.template has placeholders setup doesn't know: {', '.join(left)}.")
    tomllib.loads(text)
    return text


def cmd_worker(args) -> str:
    profile = load_profile()
    probs = profile_problems(profile)
    if probs:
        raise Failed("profile.toml isn't finished: " + "; ".join(probs) + ".")
    data = load_setup()
    if not data.get("repo"):
        raise Failed("setup.json has no repo yet. Run first: uv run setup/kit.py secrets")
    t = data.get("telegram") or {}
    topics = t.get("topics") or {}
    if not (t.get("chat_id") and t.get("owner_id") and topics.get("alerts") and topics.get("log")):
        raise Failed("setup.json has no Telegram group yet. Run first: uv run setup/kit.py telegram")
    env = require_env(list(WORKER_SECRETS.values()))
    if not TEMPLATE.is_file():
        raise Failed("worker/wrangler.toml.template is missing. Copy it from the template repo.")

    global ACCOUNT_ID
    w = data.setdefault("worker", {})
    ACCOUNT_ID = args.account_id or os.environ.get("CLOUDFLARE_ACCOUNT_ID") or w.get("account_id") or ""
    if args.account_id:
        w["account_id"] = args.account_id
    p = run([*WRANGLER, "whoami"], cwd=ROOT)
    if p.returncode != 0 or "not authenticated" in output(p).lower():
        raise Failed("not logged in to Cloudflare. Run: npx --yes wrangler@4 login (it opens the browser; click "
                     "Allow), then run this again.")

    name = w.get("name") or slug(data["repo"].split("/")[-1])
    w["name"] = name
    if not w.get("kv_id"):
        title = f"{name}-KIT"
        p = run([*WRANGLER, "kv", "namespace", "create", title], cwd=ROOT)
        kv = parse_kv_id(output(p)) if p.returncode == 0 else ""
        if not kv and "already exists" in output(p).lower():
            kv = parse_kv_list(output(run([*WRANGLER, "kv", "namespace", "list"], cwd=ROOT)), title)
        if not kv:
            if is_multiple_accounts(output(p)):
                raise Failed(MULTIPLE_ACCOUNTS)
            raise Failed(f"couldn't make the Worker's storage on Cloudflare ({last_line(output(p))}). Run this again.")
        w["kv_id"] = kv
        save_setup(data)
        say("Made the Worker's storage.")
    WRANGLER_TOML.write_text(render_wrangler(profile, data, name, w["kv_id"]), encoding="utf-8")
    say("Wrote worker/wrangler.toml.")

    for wname, local in WORKER_SECRETS.items():
        p = run([*WRANGLER, "secret", "put", wname], input=env[local], cwd=WORKER_DIR)
        if p.returncode != 0:
            out = output(p)
            if is_multiple_accounts(out):
                raise Failed(MULTIPLE_ACCOUNTS)
            if is_no_subdomain(out):
                raise Failed(NO_SUBDOMAIN)
            raise Failed(f"Cloudflare didn't take the Worker secret {wname} ({last_line(out)}). Run this again.")
    say(f"Gave the Worker its {len(WORKER_SECRETS)} secrets.")

    # The cron goes live with the deploy, so the person's profile and ids must be on GitHub first.
    push_setup_files()

    p = run([*WRANGLER, "deploy"], cwd=WORKER_DIR)
    out = output(p)
    url = parse_worker_url(out, name)
    if p.returncode != 0 or not url:
        if is_multiple_accounts(out):
            raise Failed(MULTIPLE_ACCOUNTS)
        if is_no_subdomain(out):
            raise Failed(NO_SUBDOMAIN)
        if "cron" in out.lower() and ("limit" in out.lower() or "too many" in out.lower()):
            raise Failed("too many cron triggers: this Cloudflare account already uses its 5 free scheduled Workers. "
                         "Ask the person before removing one.")
        raise Failed(f"the Cloudflare deploy failed ({last_line(out)}). Run this again.")
    w["url"] = url
    save_setup(data)
    say(f"Deployed: {url}")

    up = False
    for i in range(12):
        st, body = http("GET", url + "/health", timeout=10)
        if st == 200 and isinstance(body, dict) and body.get("ok"):
            up = True
            break
        if not DRY:
            time.sleep(5)
    if not up:
        raise Failed(f"the Worker deployed to {url} but isn't answering yet. New Cloudflare addresses can take a few "
                     "minutes. Wait 2 minutes, then run this again.")

    gh_secret(data["repo"], "WORKER_URL", url)
    gh_secret(data["repo"], "WORKER_API_SECRET", env["WORKER_API_SECRET"])
    say("GitHub secrets WORKER_URL and WORKER_API_SECRET: set")

    token = env["TELEGRAM_BOT_TOKEN"]
    hook = tg(token, "setWebhook", url=url + "/telegram", secret_token=env["TELEGRAM_WEBHOOK_SECRET"],
              allowed_updates=["message"], drop_pending_updates=True)   # the setup "hi" never becomes a note
    if not hook.get("ok"):
        raise Failed(f"Telegram didn't accept the Worker address ({hook.get('description')}). Run this again.")
    cmds = tg(token, "setMyCommands", commands=BOT_COMMANDS, scope={"type": "chat", "chat_id": t["chat_id"]})
    if not cmds.get("ok"):
        raise Failed(f"Telegram didn't take the command menu ({cmds.get('description')}). Run this again.")
    times = profile["times"]
    return (f"The Worker is live at {url}. Telegram now goes to it, and it starts the recaps at "
            f"{times['morning']} and {times['evening']} ({profile['timezone']}).")


NO_SUBDOMAIN = ("no workers.dev subdomain on this Cloudflare account yet. Open dash.cloudflare.com > Workers & Pages, "
                "pick a subdomain once, then run this again.")


# ---------- test ----------

def push_setup_files() -> None:
    """Commit + push profile.toml, setup.json, worker/wrangler.toml and the Garmin token cache if changed."""
    files = [f for f in ("profile.toml", "setup.json", "worker/wrangler.toml", "data/garmin-token.enc")
             if (ROOT / f).exists()]
    p = run(["git", "add", "--", *files])
    if p.returncode != 0:
        raise Failed(f"git couldn't stage the setup files ({last_line(output(p))}).")
    if run(["git", "diff", "--cached", "--quiet"]).returncode != 0:
        p = run(["git", "commit", "-q", "-m", "Setup: profile, ids and Worker config"])
        if p.returncode != 0:
            raise Failed(f"git couldn't commit the setup files ({last_line(output(p))}). If it asks who you are, run: "
                         'git config --global user.name "Your Name" and git config --global user.email you@example.com')
        say("Committed the setup files.")
    # --rebase=merges keeps an `update` merge commit (and so the link to the public kit's history).
    p = run(["git", "pull", "--rebase=merges", "-q", "origin", "main"])
    if p.returncode != 0:
        run(["git", "rebase", "--abort"])
        raise Failed(f"git couldn't bring in the latest changes from GitHub ({last_line(output(p))}).")
    p = run(["git", "push", "-q", "origin", "HEAD:main"])
    if p.returncode != 0:
        raise Failed(f"git couldn't push to GitHub ({last_line(output(p))}). Check `gh auth status`, then run: gh auth setup-git")
    say("Pushed the setup files to GitHub.")


def recap_runs(repo: str) -> list:
    p = run(["gh", "run", "list", "--workflow", "recap.yml", "--repo", repo, "--limit", "10",
             "--json", "databaseId,url,createdAt,event,headSha"])
    try:
        return json.loads(p.stdout or "[]") if p.returncode == 0 else []
    except json.JSONDecodeError:
        return []


def cmd_test(args) -> str:
    data = load_setup()
    repo = ensure_repo(data)
    push_setup_files()

    # The new run is the workflow_dispatch run that wasn't there before (no clock comparison, so a
    # computer with a wrong clock still finds it), on the commit just pushed when gh reports it.
    before = {r.get("databaseId") for r in recap_runs(repo)}
    head = (run(["git", "rev-parse", "HEAD"]).stdout or "").strip()
    p = run(["gh", "workflow", "run", "recap.yml", "--repo", repo, "--ref", "main", "-f", "mode=morning", "-f", "source=manual"])
    if p.returncode != 0:
        raise Failed(f"GitHub didn't start the recap ({last_line(output(p))}). Check the Actions tab is enabled: "
                     f"https://github.com/{repo}/actions")
    run_id, url = None, ""
    for _ in range(30):
        fresh = [r for r in recap_runs(repo) if r.get("event") == "workflow_dispatch"
                 and r.get("databaseId") not in before
                 and (not head or not r.get("headSha") or r["headSha"] == head)]
        if fresh:
            run_id, url = fresh[0]["databaseId"], fresh[0]["url"]
            break
        if not DRY:
            time.sleep(3)
    if not run_id:
        raise Failed(f"started the recap but couldn't find its run. Look at https://github.com/{repo}/actions")
    say(f"Recap running: {url} (takes 2-4 minutes)")
    run(["gh", "run", "watch", str(run_id), "--repo", repo, "--exit-status", "--interval", "10"], timeout=1800)
    p = run(["gh", "run", "view", str(run_id), "--repo", repo, "--json", "conclusion,url"])
    try:
        conclusion = json.loads(p.stdout).get("conclusion")
    except (json.JSONDecodeError, AttributeError):
        conclusion = None
    if conclusion == "success":
        return f"The first real recap ran on GitHub and finished. It should be in the Today topic now. Run: {url}"
    log = run(["gh", "run", "view", str(run_id), "--repo", repo, "--log-failed"])
    tail = (log.stdout or "").strip().splitlines()[-40:]
    if tail:
        say("Last lines of the failed run:")
        for line in tail:
            say("  " + line)
    raise Failed(f"the test recap ended as {conclusion or 'unknown'} on GitHub. Details: {url}")


# ---------- update (from the public kit) ----------

TEMPLATE_URL = "https://github.com/coreywessel/garmin-life-kit"
KEEP = ("profile.toml", "setup.json", "worker/wrangler.toml")   # plus everything under data/


def kept(path: str) -> bool:
    return path in KEEP or path.startswith("data/")


def git(*a, check: str = "") -> subprocess.CompletedProcess:
    """Run git; with check= set, a non-zero exit becomes FAILED: <check> (<git's last line>)."""
    p = run(["git", *a])
    if check and p.returncode != 0:
        raise Failed(f"{check} ({last_line(output(p))}).")
    return p


def toml_blocks(text: str) -> dict:
    """section -> {"header": [lines], "keys": {key: [lines]}} ("" = top level). Multi-line values stay whole."""
    blocks = {"": {"header": [], "keys": {}}}
    current, open_key = "", None
    for line in text.splitlines(keepends=True):
        if open_key:
            blocks[current]["keys"][open_key].append(line)
            if line.count('"""') % 2 == 1:
                open_key = None
            continue
        stripped = line.strip()
        h = re.match(r"^\[([A-Za-z0-9_.-]+)\]", stripped)
        if h and not stripped.startswith("[["):
            current = h.group(1)
            blocks[current] = {"header": [line], "keys": {}}
            continue
        k = re.match(r"^\s*([A-Za-z0-9_-]+)\s*=", line)
        if k:
            blocks[current]["keys"][k.group(1)] = [line]
            if line.count('"""') % 2 == 1:
                open_key = k.group(1)
        elif stripped.startswith("#"):   # comment continuing the key (or header) above
            if blocks[current]["keys"]:
                blocks[current]["keys"][list(blocks[current]["keys"])[-1]].append(line)
            else:
                blocks[current]["header"].append(line)
    return blocks


def merge_new_profile_keys(mine: str, theirs: str) -> tuple[str, list[str]]:
    """Append sections/keys the template's profile.toml has and the person's doesn't, with template defaults."""
    have = toml_blocks(mine)
    added = []
    text = mine
    for section, block in toml_blocks(theirs).items():
        if section not in have:
            if not block["keys"]:
                continue
            body = "".join(block["header"]) + "".join("".join(v) for v in block["keys"].values())
            text = text + ("" if text.endswith("\n") else "\n") + "\n" + body
            added.append(f"[{section}]")
            continue
        for key, lines in block["keys"].items():
            if key in have[section]["keys"]:
                continue
            raw = "".join(lines)
            text = toml_insert(text, section, raw if raw.endswith("\n") else raw + "\n")
            added.append(f"{section + '.' if section else ''}{key}")
    tomllib.loads(text)
    return text, added


def run_tests() -> subprocess.CompletedProcess:
    if shutil.which("uv"):
        return run(["uv", "run", "--with", "pytest", "pytest", "-q"], timeout=1800)
    return subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=ROOT, text=True, capture_output=True,
                          encoding="utf-8", errors="replace", env=child_env(), timeout=1800)


def merge_in_progress() -> bool:
    return git("rev-parse", "-q", "--verify", "MERGE_HEAD").returncode == 0


def push_main(what: str) -> None:
    """Plain push (a merge commit is never rebased). If GitHub moved meanwhile, merge that in and push once more."""
    if git("push", "-q", "origin", "HEAD:main").returncode == 0:
        return
    p = git("pull", "--no-rebase", "--no-edit", "-q", "origin", "main")
    if p.returncode != 0:
        if merge_in_progress():
            git("merge", "--abort")
        raise Failed(f"{what} on this computer, but GitHub changed at the same time and the two couldn't be combined "
                     f"({last_line(output(p))}). Run: git pull origin main, then git push origin HEAD:main")
    p = git("push", "-q", "origin", "HEAD:main")
    if p.returncode != 0:
        raise Failed(f"{what} on this computer but couldn't be pushed ({last_line(output(p))}). Run: git push origin HEAD:main")


def check_profile_loads() -> None:
    """The merged kit must still accept the person's profile.toml (kit.profile.load, in a fresh process)."""
    env = {k: v for k, v in child_env().items() if k not in ("KIT_DRY_RUN", "KIT_PROFILE", "KIT_DATA_DIR")}
    p = subprocess.run([sys.executable, "-c", "import kit.profile as p; p.load()"], cwd=ROOT, env=env, text=True,
                       capture_output=True, encoding="utf-8", errors="replace", timeout=120)
    if p.returncode != 0:
        raise Failed(f"after the update, the kit no longer accepts profile.toml ({last_line(output(p))}), so I undid it. "
                     "Nothing changed. Claude can fix profile.toml, then run this again.")


def cmd_update(args) -> str:
    if DRY:
        return "dry run: would fetch the public kit, merge it in (keeping your profile, setup and data), run the tests and push. Nothing changed."
    branch = git("symbolic-ref", "--short", "-q", "HEAD").stdout.strip()
    if branch != "main":
        raise Failed(f"this copy is on {branch or 'no branch'}, not main. Run: git checkout main, then run this again.")
    if merge_in_progress():
        raise Failed("an earlier merge was left unfinished. Run: git merge --abort, then run this again.")
    if git("status", "--porcelain", "--untracked-files=no").stdout.strip():
        raise Failed("there are changes here that aren't committed. Commit them (or run: git stash), then run this again.")

    # GitHub Actions commit data/ to main every day, so first catch up with the copy on GitHub.
    git("fetch", "-q", "origin", check="couldn't download your copy from GitHub. Check the internet connection and `gh auth status`")
    p = git("pull", "--ff-only", "-q", "origin", "main")
    if p.returncode != 0:
        raise Failed("this computer and GitHub both have changes the other doesn't, so I stopped before updating "
                     f"({last_line(output(p))}). Run: git pull origin main, then run this again.")

    url = args.template or TEMPLATE_URL
    if git("remote", "get-url", "template").returncode != 0:
        git("remote", "add", "template", url, check="couldn't add the public kit as a remote")
    elif args.template:
        git("remote", "set-url", "template", url, check="couldn't point the template remote at that address")
    git("fetch", "-q", "template", check="couldn't download the public kit. Check the internet connection, then run this again")
    if git("rev-parse", "-q", "--verify", "template/main").returncode != 0:
        raise Failed("the public kit has no main branch to update from.")

    linked = ""
    if git("merge-base", "HEAD", "template/main").returncode != 0:
        # A copy made from a GitHub template starts with a fresh history. Find the template commit it
        # started from (same files exactly) and tell git that's its parent.
        roots = git("rev-list", "--max-parents=0", "HEAD").stdout.split()
        trees = dict(line.split() for line in git("log", "--format=%T %H", "template/main").stdout.splitlines() if line)
        match = next(((r, trees[t]) for r in roots
                      if (t := git("rev-parse", f"{r}^{{tree}}").stdout.strip()) in trees), None)
        if not match:
            raise Failed("couldn't match this copy to the public kit's history, so it can't update by itself. Claude can "
                         "merge it by hand, keeping profile.toml, setup.json, data/ and worker/wrangler.toml.")
        git("replace", "--graft", match[0], match[1], check="couldn't link this copy to the public kit's history")
        linked = "Linked this copy to the public kit's history (first update). "
        say(linked.strip())

    orig = git("rev-parse", "HEAD").stdout.strip()
    their_profile = git("show", "template/main:profile.toml").stdout
    p = git("merge", "--no-ff", "--no-commit", "template/main")
    if not merge_in_progress():
        if p.returncode != 0:
            raise Failed(f"git couldn't start the merge ({last_line(output(p))}). Nothing changed.")
        if git("rev-list", "--count", "origin/main..HEAD").stdout.strip() not in ("", "0"):
            push_main("Your copy was already up to date")
            return "Already up to date with the public kit. Pushed the commits that were waiting here."
        return "Already up to date with the public kit. Nothing changed."

    added = []
    try:
        conflicts = git("diff", "--name-only", "--diff-filter=U").stdout.split()
        other = [f for f in conflicts if not kept(f)]
        if other:
            raise Failed(f"the update clashes with changes made in this copy, in: {', '.join(other)}. Nothing changed. "
                         "Claude can merge by hand: git merge template/main, fix those files, keep profile.toml, "
                         "setup.json, data/ and worker/wrangler.toml as they are here.")
        # The person's own files always win, conflict or not (a changed default must never overwrite their setting).
        mine = [f for f in git("ls-tree", "-r", "--name-only", orig).stdout.splitlines() if kept(f)]
        if mine:
            git("checkout", orig, "--", *mine, check="couldn't keep your own files during the update")
        if PROFILE.is_file() and their_profile:
            text, added = merge_new_profile_keys(PROFILE.read_text(encoding="utf-8"), their_profile)
            if added:
                PROFILE.write_text(text, encoding="utf-8")
                git("add", "profile.toml")
        for f in conflicts:   # anything still marked (e.g. a kept file deleted on one side)
            git("add", "-A", "--", f)
        p = git("commit", "-q", "--no-edit")
        if p.returncode != 0:
            raise Failed(f"git couldn't finish the merge ({last_line(output(p))}). Nothing changed.")
    except BaseException:   # Ctrl-C, a bad profile line, anything: never leave a half-done merge behind
        if merge_in_progress():
            git("merge", "--abort")
        raise

    keep = False
    try:   # only a green, still-valid update survives; anything else (red, timeout, Ctrl-C) goes back
        say("Running the kit's tests...")
        t = run_tests()
        if t.returncode not in (0, 5):   # 5 = no tests collected
            for line in ((t.stdout or "") + (t.stderr or "")).strip().splitlines()[-25:]:
                say("  " + line)
            raise Failed("the update made the kit's tests fail, so I undid it. Nothing changed. The test output is above.")
        check_profile_loads()
        keep = True
    finally:
        if not keep:
            git("reset", "-q", "--hard", orig)

    stat = git("diff", "--stat", orig, "HEAD").stdout.rstrip()
    if stat:
        say(stat)
    push_main("The update is merged")
    changed = [f for f in git("diff", "--name-only", orig, "HEAD").stdout.splitlines() if f]
    worker = any(f.startswith("worker/") and f != "worker/wrangler.toml" for f in changed)
    news = f" New profile settings added with their defaults: {', '.join(added)}." if added else ""
    nxt = (" The Worker changed: run uv run setup/kit.py worker next." if worker
           else " No Worker changes, so nothing else to run.")
    return (f"{linked}Updated from the public kit: {len(changed)} files changed, tests pass, pushed. "
            f"Your profile, setup.json and data were kept.{news}{nxt}")


# ---------- dry-run ----------

def cmd_dry_run(args) -> str:
    if not (ROOT / "kit").is_dir():
        raise Failed("the kit/ folder is missing, so there's nothing to run. Copy the full kit from the template.")
    steps = [("morning recap", ["kit.recap", "morning", "--source", "manual"]),
             ("evening recap", ["kit.recap", "evening", "--source", "manual"]),
             ("activity card", ["kit.cards"])]
    steps += [(f"/{c}", ["kit.commands", c, ""]) for c in ("today", "week", "last", "status")]
    failed = []
    with tempfile.TemporaryDirectory(prefix="garmin-life-kit-dry-") as tmp:
        prof = Path(tmp) / "profile.toml"
        if PROFILE.is_file():
            shutil.copy(PROFILE, prof)
        env = {**child_env(), "KIT_DRY_RUN": "1", "KIT_DATA_DIR": str(Path(tmp) / "data"),
               "KIT_PROFILE": str(prof), "PYTHONPATH": str(ROOT) + os.pathsep + os.environ.get("PYTHONPATH", "")}
        env.pop("KIT_DRY_CLAUDE", None)
        for label, mod in steps:
            say(f"===== {label} =====")
            try:
                p = subprocess.run([sys.executable, "-m", *mod], cwd=ROOT, env=env, text=True, capture_output=True,
                                   timeout=300, encoding="utf-8", errors="replace")
            except subprocess.TimeoutExpired:
                failed.append(label)
                say("(took too long)")
                continue
            say((p.stdout or "").rstrip())
            if p.returncode != 0:
                failed.append(label)
                say((p.stderr or "").rstrip()[-2000:])
    if failed:
        raise Failed(f"the dry run broke on: {', '.join(failed)}. That's a bug in the kit, not something the person did "
                     "(the error is printed above).")
    return "Dry run worked: a morning recap, an evening recap, an activity card and 4 command answers were made from sample data. Nothing was sent."


# ========== --dry-run fakes ==========

class Fake:
    """Stands in for Telegram, Open-Meteo, GitHub, gh, git, wrangler and Garmin. Tests tweak the attributes."""

    def __init__(self):
        self.calls: list = []            # ("http", method, url-without-token, payload) / ("run", cmd, stdin)
        self.webhook_active = False      # getUpdates answers 409 until deleteWebhook
        self.bot_ok = True
        self.updates = [
            {"update_id": 10, "my_chat_member": {"chat": {"id": FAKE_CHAT_ID, "type": "supergroup", "title": "Life",
                                                          "is_forum": True}, "from": {"id": 7001, "is_bot": False}}},
            {"update_id": 11, "message": {"message_id": 1, "chat": {"id": FAKE_CHAT_ID, "type": "supergroup",
                                                                     "title": "Life", "is_forum": True},
                                          "from": {"id": 7001, "is_bot": False, "first_name": "Sam"}, "text": "hi"}},
        ]
        self.is_forum = True
        self.member = {"status": "administrator", "can_manage_topics": True}
        self.next_topic = 2
        self.private = True
        self.logged_in = True
        self.deploy_out = ("Total Upload: 9.80 KiB / gzip: 3.20 KiB\nUploaded garmin-life-kit (1.2 sec)\n"
                           f"Deployed garmin-life-kit triggers (0.5 sec)\n  {FAKE_WORKER_URL}\n"
                           "  schedule: */5 * * * *\nCurrent Version ID: 00000000-0000-0000-0000-000000000000\n")
        self.deploy_rc = 0
        self.health_ok = True
        self.conclusion = "success"
        self.garmin_error = None         # e.g. "RateLimited", or "GarminError" for a plain HTTP 4xx
        self.dispatched = False

    def http(self, method, url, payload):
        clean = re.sub(r"/bot[^/]+/", "/bot<token>/", url)
        self.calls.append(("http", method, clean, payload))
        if "api.telegram.org" in url:
            return self.telegram(url.rsplit("/", 1)[-1], payload or {})
        if "geocoding-api.open-meteo.com" in url:
            return 200, {"results": [
                {"name": "Denver", "latitude": 39.73915, "longitude": -104.9847, "country": "United States",
                 "country_code": "US", "admin1": "Colorado", "timezone": "America/Denver"}]}
        if url.endswith("/health"):
            return (200, {"ok": True, "log_pending": 0}) if self.health_ok else (0, {"description": "network error"})
        return 404, {"description": "not faked"}

    def telegram(self, method, p):
        ok = lambda result=True: (200, {"ok": True, "result": result})  # noqa: E731
        if method == "getMe":
            return ok({"id": 9001, "is_bot": True, "username": "sams_garmin_bot"}) if self.bot_ok else \
                (401, {"ok": False, "error_code": 401, "description": "Unauthorized"})
        if method == "getUpdates":
            if self.webhook_active:
                return 409, {"ok": False, "error_code": 409,
                             "description": "Conflict: can't use getUpdates method while webhook is active"}
            return ok(self.updates)
        if method == "deleteWebhook":
            self.webhook_active = False
            return ok()
        if method == "getChat":
            return ok({"id": p.get("chat_id"), "type": "supergroup", "title": "Life", "is_forum": self.is_forum})
        if method == "getChatMember":
            return ok({"user": {"id": p.get("user_id")}, **self.member})
        if method == "createForumTopic":
            self.next_topic += 1
            return ok({"message_thread_id": self.next_topic, "name": p.get("name")})
        return ok()

    def run(self, cmd, stdin):
        self.calls.append(("run", list(cmd), stdin))
        done = lambda out="", rc=0, err="": subprocess.CompletedProcess(cmd, rc, out, err)  # noqa: E731
        c = " ".join(cmd)
        if cmd[0] == "gh":
            if c.startswith("gh repo view"):
                return done(json.dumps({"nameWithOwner": "someone/garmin-life-kit", "isPrivate": self.private}))
            if c.startswith("gh run list"):
                now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                runs = [{"databaseId": 100, "url": "https://github.com/someone/garmin-life-kit/actions/runs/100",
                         "createdAt": now, "event": "workflow_dispatch", "headSha": "abc123"}]
                if self.dispatched:
                    runs.insert(0, {"databaseId": 123, "url": "https://github.com/someone/garmin-life-kit/actions/runs/123",
                                    "createdAt": "2001-01-01T00:00:00Z", "event": "workflow_dispatch", "headSha": "abc123"})
                return done(json.dumps(runs))
            if c.startswith("gh workflow run"):
                self.dispatched = True
                return done()
            if c.startswith("gh run watch"):
                return done(rc=0 if self.conclusion == "success" else 1)
            if c.startswith("gh run view") and "--json" in cmd:
                return done(json.dumps({"conclusion": self.conclusion,
                                        "url": "https://github.com/someone/garmin-life-kit/actions/runs/123"}))
            if c.startswith("gh run view"):
                return done("recap\tRun\tError: something broke\n")
            return done("gh version 2.60.0\n")
        if cmd[:3] == WRANGLER:
            sub = " ".join(cmd[3:])
            if sub == "whoami":
                return done("You are logged in with an OAuth Token, associated with the email sam@example.com.\n"
                            if self.logged_in else "You are not authenticated. Please run `wrangler login`.\n")
            if sub.startswith("kv namespace create"):
                return done('🌀 Creating namespace with title "garmin-life-kit-KIT"\n✨ Success!\nTo access your new KV '
                            'Namespace in your Worker, add the following snippet to your configuration file:\n'
                            '{\n  "kv_namespaces": [\n    {\n      "binding": "garmin_life_kit_KIT",\n'
                            '      "id": "0f2ac74b498b48028cb68387c421e279"\n    }\n  ]\n}\n')
            if sub.startswith("secret put"):
                return done("✨ Success! Uploaded secret\n")
            if sub == "deploy":
                return done(self.deploy_out, self.deploy_rc)
        if cmd[0] == "git":
            if c == "git diff --cached --quiet":
                return done(rc=1)
            if c == "git rev-parse HEAD":
                return done("abc123\n")
            return done("git version 2.47.0\n" if c == "git --version" else "")
        if cmd[0] == "node":
            return done("v22.16.0\n")
        return done(f"{cmd[0]} 1.0.0\n")

    def garmin_client(self, g):
        """kit.garmin's DryRunClient (fixtures), or one that raises the error a test asked for."""
        self.calls.append(("garmin", "DryRunClient"))
        if not self.garmin_error:
            return g.DryRunClient()
        err = getattr(g, self.garmin_error)

        class Failing:
            def __getattr__(self, name):
                def boom(*a, **k):
                    raise err("Garmin /hrv-service/hrv/x answered HTTP 404")
                return boom
        return Failing()


FAKE = Fake()


# ========== main ==========

class Parser(argparse.ArgumentParser):
    def error(self, message):   # keep the one-line FAILED format even for a mistyped command
        raise Failed(f"that command wasn't understood ({message}). Run: uv run setup/kit.py --help")


def parser() -> argparse.ArgumentParser:
    common = Parser(add_help=False)
    common.add_argument("--dry-run", action="store_true", help="fake every network call and external tool")
    ap = Parser(prog="uv run setup/kit.py", description="garmin-life-kit setup", parents=[common])
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("status", parents=[common], help="what's done so far")
    sub.add_parser("check", parents=[common], help="prerequisites on this computer")
    s = sub.add_parser("secret", parents=[common], help="hidden prompt for one pasted secret")
    s.add_argument("name")
    s.add_argument("--days", type=int, help="WORKER_GITHUB_TOKEN only: days until it expires (none = no expiry)")
    s = sub.add_parser("profile", parents=[common], help="write profile.toml")
    s.add_argument("--name")
    s.add_argument("--timezone")
    s.add_argument("--units")
    s.add_argument("--city")
    s.add_argument("--morning")
    s.add_argument("--evening")
    s.add_argument("--wears", help='what they wear, e.g. "Forerunner 265, HRM-Pro strap"')
    s.add_argument("--lat", type=float)
    s.add_argument("--lon", type=float)
    s = sub.add_parser("telegram", parents=[common], help="find the group, create topics")
    s.add_argument("--chat-id", type=int)
    for alias in ("discover", "garmin"):
        s = sub.add_parser(alias, parents=[common], help="check the Garmin key and learn which data it has")
        s.add_argument("--days", type=int, help="how many days the Garmin key lasts")
    s = sub.add_parser("secrets", parents=[common], help="generate + push secrets")
    s.add_argument("--github-token-days", type=int, help="only if the GitHub token has an expiry")
    s = sub.add_parser("worker", parents=[common], help="deploy the Cloudflare Worker")
    s.add_argument("--account-id", help="Cloudflare account id, when the login has more than one")
    sub.add_parser("test", parents=[common], help="run a real morning recap on GitHub")
    sub.add_parser("dry-run", parents=[common], help="offline pipeline with sample data")
    s = sub.add_parser("update", parents=[common], help="pull improvements from the public kit")
    s.add_argument("--template", help="address of the kit to update from (default: the public kit)")
    return ap


COMMANDS = {"status": cmd_status, "check": cmd_check, "secret": cmd_secret, "profile": cmd_profile,
            "telegram": cmd_telegram, "discover": cmd_discover, "garmin": cmd_discover, "secrets": cmd_secrets, "worker": cmd_worker,
            "test": cmd_test, "dry-run": cmd_dry_run, "update": cmd_update}


def main(argv=None) -> int:
    global DRY
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")   # never crash printing a ✨ on a cp1252 console
        except (AttributeError, ValueError):
            pass
    ap = parser()
    try:
        args = ap.parse_args(argv)
    except Failed as e:
        say(f"FAILED: {e}")
        return 1
    except SystemExit as e:   # --help
        return int(e.code or 0)
    DRY = bool(args.dry_run)
    if DRY:
        say("(dry run: no network calls, no external tools)")
    try:
        result = COMMANDS[args.command](args)
    except Failed as e:
        say(f"FAILED: {e}")
        return 1
    except KeyboardInterrupt:
        say("FAILED: stopped before it finished. Run it again when ready.")
        return 1
    except Exception:
        print(scrub(traceback.format_exc()), file=sys.stderr)
        say("FAILED: something unexpected went wrong in the setup script (details above). That's a bug in the kit, "
            "not something the person did.")
        return 1
    say(f"OK: {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
