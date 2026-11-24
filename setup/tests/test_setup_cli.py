"""Tests for setup/kit.py, the setup CLI. Every command runs with --dry-run against a temp copy of
the repo layout, so no accounts or network are needed. Run: uv run --with pytest pytest -q setup/"""
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REAL_ROOT = Path(__file__).resolve().parents[2]

# profile.toml exactly as the contract (docs/ARCHITECTURE.md) defines it.
PROFILE = '''# About you. The only file with personal settings in it.
# Ask Claude to change anything here. After changing [times], Claude redeploys the Worker.

name = ""                     # first name the recaps use. Required.
timezone = "America/Denver"   # IANA time zone name. Required.
units = "imperial"            # "imperial" (mi, ft, °F, mph) or "metric" (km, m, °C, km/h)
week_starts = "monday"        # "monday" or "sunday" (for /week and week totals)

[location]                    # weather line. City-level is plenty.
label = ""                    # e.g. "Denver, CO"
lat = 0.0
lon = 0.0

[times]                       # local, 24h "HH:MM", multiples of 5 minutes
morning = "07:30"
evening = "21:00"

[features]                    # true / false
morning_recap = true
evening_recap = true
activity_cards = true
weather = true
log_notes = true              # text typed in the group is saved and read by the recaps
watchdog = true

[recap]
sleep_target_bed = ""         # optional "22:30": morning says how far from it you went to bed
about = """
"""                           # optional plain sentences that help the recap read your data
                              # ("I snowboard most weekends", "night shifts on Tuesdays")
'''

GOOD = {
    "TELEGRAM_BOT_TOKEN": "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawZ",
    "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-" + "x" * 60,
    "GARMIN_PERSONAL_API_KEY": "gpk_" + "k" * 40,
    "WORKER_GITHUB_TOKEN": "github_pat_" + "g" * 70,
}


# A tiny stand-in for kit/ so discover's own logic is tested apart from builder-python's code.
STUB_KIT = {
    "__init__.py": "",
    "metrics.py": 'METRICS = {"sleep": ("Sleep", "sleep", None), "hrv": ("HRV", "hrv", None), '
                  '"stress": ("Stress", "summary", None), "spo2": ("SpO2", "summary", None)}\n',
    "garmin.py": """import os
class GarminError(RuntimeError):
    fix = ""
class KeyRejected(GarminError): pass
class RateLimited(GarminError): pass
class ExchangeFailed(GarminError): pass
class RepoCache:
    def __init__(self, path=None, key=None):
        self.key = key or os.environ.get("GARMIN_CACHE_KEY")
class Client:
    made = []
    def __init__(self, key, cache=None):
        Client.made.append((key, cache))
        self.fixtures = "garmin"
    def sleep(self, d): return {}
class DryRunClient:
    def __init__(self):
        assert os.environ.get("KIT_DRY_RUN") == "1"
        self.fixtures = os.environ.get("KIT_DRY_FIXTURES", "garmin")
    def sleep(self, d): return {}
""",
    "discover.py": """SETS = {"garmin": ["sleep", "hrv", "stress", "spo2"], "garmin-sparse": ["sleep"], "garmin-empty": []}
TYPES = {"garmin": {"running": 3, "cycling": 1}, "garmin-sparse": {"walking": 1}, "garmin-empty": {}}
UNKNOWN = {"garmin-empty": {"sleep": "no sleep record on the days checked", "hrv": "no sleep record on the days checked"}}
def run(client, today):
    client.sleep(today)
    f = getattr(client, "fixtures", "garmin")
    avail = SETS[f]
    unknown = UNKNOWN.get(f, {})
    return {"available": avail, "missing": [m for m in SETS["garmin"] if m not in avail and m not in unknown],
            "unknown": unknown, "not_in_kit": {"weight": "not available through the kit yet"},
            "activity_types": TYPES[f], "calls": 10, "sampled_dates": [str(today)]}
""",
}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "garmin-life-kit"
    (root / "setup").mkdir(parents=True)
    (root / "worker").mkdir()
    (root / "setup" / "kit.py").write_text((REAL_ROOT / "setup" / "kit.py").read_text())
    (root / "worker" / "wrangler.toml.template").write_text((REAL_ROOT / "worker" / "wrangler.toml.template").read_text())
    (root / "profile.toml").write_text(PROFILE, encoding="utf-8")
    (root / "kit").mkdir()
    for name, body in STUB_KIT.items():
        (root / "kit" / name).write_text(body)
    monkeypatch.delenv("KIT_DRY_FIXTURES", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return root


def env_file(root):
    # dry-run's fake `gh repo view` says someone/garmin-life-kit -> <owner>__<name>.env
    return Path(os.environ["HOME"]) / ".config" / "garmin-life-kit" / "someone__garmin-life-kit.env"


def read_env(root):
    p = env_file(root)
    return dict(l.split("=", 1) for l in p.read_text().splitlines() if "=" in l) if p.exists() else {}


def cli(root, *args, stdin=""):
    """Run the CLI as a subprocess, like Claude does. Returns (exit code, stdout)."""
    p = subprocess.run([sys.executable, str(root / "setup" / "kit.py"), *args, "--dry-run"],
                       input=stdin, capture_output=True, text=True, cwd=root, env=os.environ.copy(), timeout=60)
    return p.returncode, p.stdout + p.stderr


def last(out):
    return out.strip().splitlines()[-1]


def load(root):
    """Fresh in-process copy of the CLI (fresh fakes) for tests that tweak the fakes."""
    spec = importlib.util.spec_from_file_location(f"setup_kit_{id(root)}_{os.urandom(4).hex()}", root / "setup" / "kit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def go(mod, capsys, *args):
    rc = mod.main([*args, "--dry-run"])
    out = capsys.readouterr().out
    return rc, out


def prime(root, mod=None, upto="garmin"):
    """Run the setup steps before the one under test (in-process or subprocess)."""
    for name, value in GOOD.items():
        rc, out = cli(root, "secret", name, stdin=value + "\n")
        assert rc == 0, out
    steps = [("profile", "--name", "Sam", "--timezone", "America/Denver", "--units", "imperial", "--city", "Denver, CO"),
             ("telegram",), ("garmin", "--days", "365"), ("secrets",), ("worker",)]
    for step in steps:
        rc, out = cli(root, *step)
        assert rc == 0, out
        if step[0] == upto:
            break


# ---------- the whole setup ----------

# Why: proves the whole runbook (every command, in SETUP.md order) works end to end without accounts,
# leaves the right files behind, and never prints a secret.
def test_full_setup_in_dry_run(repo):
    outs = []
    for name, value in GOOD.items():
        rc, out = cli(repo, "secret", name, stdin=value + "\n")
        outs.append(out)
        assert rc == 0 and last(out).startswith(f"OK: Saved {name}"), out
    for step in [("check",),
                 ("profile", "--name", "Sam", "--timezone", "America/Denver", "--units", "metric",
                  "--city", "Denver, CO", "--morning", "06:45", "--evening", "21:30"),
                 ("telegram",), ("garmin", "--days", "180"), ("secrets",), ("worker",), ("test",), ("status",)]:
        rc, out = cli(repo, *step)
        outs.append(out)
        assert rc == 0, out
        assert last(out).startswith("OK: "), out
        assert sum(l.startswith(("OK:", "FAILED:")) for l in out.splitlines()) == 1, out
    assert "Setup is complete" in last(outs[-1])

    # setup.json: ids, Worker, expiries (Garmin + Claude; GitHub token has no expiry unless asked)
    s = json.loads((repo / "setup.json").read_text())
    assert s["repo"] == "someone/garmin-life-kit"
    assert s["telegram"]["chat_id"] < 0 and s["telegram"]["owner_id"] == 7001
    assert set(s["telegram"]["topics"]) == {"today", "fitness", "log", "alerts"}
    assert len(set(s["telegram"]["topics"].values())) == 4
    assert s["worker"]["url"].endswith(".workers.dev") and len(s["worker"]["kv_id"]) == 32
    assert sorted(e["name"] for e in s["expiries"]) == ["Claude token (CLAUDE_CODE_OAUTH_TOKEN)",
                                                        "Garmin API key (GARMIN_PERSONAL_API_KEY)"]

    # wrangler.toml: valid TOML with every [vars] key the Worker reads
    w = tomllib.loads((repo / "worker" / "wrangler.toml").read_text())
    v = w["vars"]
    assert (v["REPO"], v["TIMEZONE"], v["MORNING"], v["EVENING"]) == ("someone/garmin-life-kit", "America/Denver", "06:45", "21:30")
    assert v["CHAT_ID"] == str(s["telegram"]["chat_id"]) and v["OWNER_ID"] == "7001"
    assert v["ALERTS_THREAD"] == str(s["telegram"]["topics"]["alerts"]) and v["LOG_THREAD"] == str(s["telegram"]["topics"]["log"])
    assert json.loads(v["FEATURES"])["log_notes"] is True
    assert w["kv_namespaces"][0] == {"binding": "KIT", "id": s["worker"]["kv_id"]}
    assert w["triggers"]["crons"] == ["*/5 * * * *"]

    # profile.toml: values written, comments kept
    text = (repo / "profile.toml").read_text(encoding="utf-8")
    p = tomllib.loads(text)
    assert (p["name"], p["units"], p["location"]["lat"], p["location"]["lon"]) == ("Sam", "metric", 39.74, -104.98)
    assert 'name = "Sam"                  # first name the recaps use. Required.' in text
    assert '# ("I snowboard most weekends", "night shifts on Tuesdays")' in text

    # env file: all 8 secrets, private permissions
    env = read_env(repo)
    assert set(env) == set(GOOD) | {"GARMIN_CACHE_KEY", "WORKER_API_SECRET", "TELEGRAM_WEBHOOK_SECRET", "SHORTCUT_SECRET"}
    assert len({env["WORKER_API_SECRET"], env["TELEGRAM_WEBHOOK_SECRET"], env["SHORTCUT_SECRET"]}) == 3
    if os.name != "nt":
        assert stat.S_IMODE(env_file(repo).stat().st_mode) == 0o600
        assert stat.S_IMODE(env_file(repo).parent.stat().st_mode) == 0o700

    # no secret value ever printed
    everything = "\n".join(outs)
    for value in env.values():
        assert value not in everything


# ---------- secret ----------

# Why: a half-copied token must be caught at paste time, not hours later as a failed recap.
@pytest.mark.parametrize("name,bad,words", [
    ("TELEGRAM_BOT_TOKEN", "Use this token to access the HTTP API: 123:abc", "doesn't look like a bot token"),
    ("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-short", "doesn't look like a Claude token"),
    ("WORKER_GITHUB_TOKEN", "ghp_" + "a" * 36, "classic GitHub token"),
    ("GARMIN_PERSONAL_API_KEY", "abc", "too short"),
])
def test_secret_rejects_bad_format(repo, name, bad, words):
    rc, out = cli(repo, "secret", name, stdin=bad + "\n")
    assert rc == 1 and last(out).startswith("FAILED:") and words in last(out)
    assert not env_file(repo).exists()
    assert bad not in out


# Why: if Claude runs `secret` itself (no terminal), it must say the human has to run it, not save junk.
def test_secret_with_nothing_entered_fails(repo):
    rc, out = cli(repo, "secret", "TELEGRAM_BOT_TOKEN", stdin="")
    assert rc == 1 and "nothing was entered" in last(out) and "own terminal" in last(out)


# Why: only the four pasted secrets may be set by hand; generated ones come from `secrets`.
def test_secret_unknown_name_fails(repo):
    rc, out = cli(repo, "secret", "WORKER_API_SECRET", stdin="x" * 64 + "\n")
    assert rc == 1 and "isn't one you paste" in last(out)


# Why: re-entering a secret must replace the old value, not add a second line that shadows it.
def test_secret_replaces_existing_value(repo):
    cli(repo, "secret", "GARMIN_PERSONAL_API_KEY", stdin="a" * 20 + "\n")
    cli(repo, "secret", "GARMIN_PERSONAL_API_KEY", stdin="b" * 20 + "\n")
    lines = env_file(repo).read_text().splitlines()
    assert lines == ["GARMIN_PERSONAL_API_KEY=" + "b" * 20]


# ---------- profile ----------

# Why: profile.toml is the person's file; rewriting must keep every comment and only change values.
def test_profile_rewrites_in_place(repo):
    rc, out = cli(repo, "profile", "--name", 'Jo "JJ" #1', "--timezone", "Europe/London", "--units", "metric",
                  "--lat", "51.5072", "--lon", "-0.1276", "--city", "London")
    assert rc == 0, out
    text = (repo / "profile.toml").read_text(encoding="utf-8")
    p = tomllib.loads(text)
    assert p["name"] == 'Jo "JJ" #1' and p["timezone"] == "Europe/London"
    assert (p["location"]["lat"], p["location"]["lon"], p["location"]["label"]) == (51.51, -0.13, "London")
    assert [l for l in text.splitlines() if "#" in l and "=" not in l] == \
           [l for l in PROFILE.splitlines() if "#" in l and "=" not in l]
    assert text.count("\n") == PROFILE.count("\n")


# Why: a bad time would make the Worker never fire; it must be refused and the file left alone.
def test_profile_bad_time_fails_and_writes_nothing(repo):
    rc, out = cli(repo, "profile", "--name", "Sam", "--morning", "7:33")
    assert rc == 1 and "5-minute mark" in last(out)
    assert (repo / "profile.toml").read_text(encoding="utf-8") == PROFILE


# Why: the profile isn't usable without a name; the OK line must not claim it is.
def test_profile_without_name_fails_loud(repo):
    rc, out = cli(repo, "profile", "--city", "Denver, CO")
    assert rc == 1 and "name is empty" in last(out)


# Why: "Portland, OR" must land in Oregon, not the first Portland the lookup returns.
def test_geocode_uses_state_hint(repo, monkeypatch):
    mod = load(repo)
    results = [{"name": "Portland", "latitude": 43.66, "longitude": -70.26, "admin1": "Maine", "country": "United States", "country_code": "US"},
               {"name": "Portland", "latitude": 45.52, "longitude": -122.68, "admin1": "Oregon", "country": "United States", "country_code": "US"}]
    monkeypatch.setattr(mod, "http", lambda *a, **k: (200, {"results": results}))
    assert mod.geocode("Portland, OR")["admin1"] == "Oregon"
    assert mod.geocode("Portland")["admin1"] == "Maine"


# Why: a value containing '#' or quotes must not eat the comment or break the file.
def test_toml_set_keeps_comment_column(repo):
    mod = load(repo)
    text = 'name = ""                     # first name\n[times]\nmorning = "07:30"\n'
    out = mod.toml_set(text, "", "name", mod.toml_str('A#"b'))
    assert tomllib.loads(out)["name"] == 'A#"b'
    assert out.splitlines()[0].index("#", 14) == text.splitlines()[0].index("#")
    assert tomllib.loads(mod.toml_set(out, "times", "morning", '"06:00"'))["times"]["morning"] == "06:00"


# ---------- telegram ----------

def telegram_ready(repo):
    cli(repo, "secret", "TELEGRAM_BOT_TOKEN", stdin=GOOD["TELEGRAM_BOT_TOKEN"] + "\n")


# Why: re-running telegram after the webhook is on must still work, and say the webhook needs restoring.
def test_telegram_409_deletes_webhook_and_retries(repo, capsys):
    telegram_ready(repo)
    mod = load(repo)
    mod.FAKE.webhook_active = True
    rc, out = go(mod, capsys, "telegram")
    assert rc == 0, out
    methods = [c[2].rsplit("/", 1)[-1] for c in mod.FAKE.calls if c[0] == "http"]
    assert methods[:4] == ["getMe", "getUpdates", "deleteWebhook", "getUpdates"]
    assert "webhook" in last(out) and "setup/kit.py worker" in last(out)


# Why: with no group visible, the human needs the exact next action, not a stack trace.
def test_telegram_no_group(repo, capsys):
    telegram_ready(repo)
    mod = load(repo)
    mod.FAKE.updates = []
    rc, out = go(mod, capsys, "telegram")
    assert rc == 1 and "found no group" in last(out) and "Send any message in your new group, then run this again" in last(out)
    assert not (repo / "setup.json").exists()


# Why: the bot may be in several groups; guessing would post into the wrong one.
def test_telegram_several_groups_lists_them_then_accepts_chat_id(repo, capsys):
    telegram_ready(repo)
    mod = load(repo)
    mk = lambda uid, cid, title: {"update_id": uid, "message": {"message_id": uid, "text": "hi",  # noqa: E731
                                  "chat": {"id": cid, "type": "supergroup", "title": title, "is_forum": True},
                                  "from": {"id": 7001, "is_bot": False}}}
    mod.FAKE.updates = [mk(1, -1001, "Life"), mk(2, -1002, "Work")]
    rc, out = go(mod, capsys, "telegram")
    assert rc == 1 and "found several groups" in last(out)
    assert '"Life" --chat-id -1001' in last(out) and '"Work" --chat-id -1002' in last(out)
    rc, out = go(mod, capsys, "telegram", "--chat-id", "-1002")
    assert rc == 0, out
    assert json.loads((repo / "setup.json").read_text())["telegram"]["chat_id"] == -1002


# Why: topics must be on and the bot must manage topics, or creating Today/Fitness/Log/Alerts fails later.
@pytest.mark.parametrize("change,words", [
    ({"is_forum": False}, "topics are off"),
    ({"member": {"status": "member"}}, "not an admin with Manage Topics"),
    ({"member": {"status": "administrator", "can_manage_topics": False}}, "not an admin with Manage Topics"),
])
def test_telegram_checks_forum_and_admin(repo, capsys, change, words):
    telegram_ready(repo)
    mod = load(repo)
    for k, v in change.items():
        setattr(mod.FAKE, k, v)
    rc, out = go(mod, capsys, "telegram")
    assert rc == 1 and words in last(out)
    assert not any("createForumTopic" in c[2] for c in mod.FAKE.calls if c[0] == "http")


# Why: owner_id decides who can drive the bot; it must be the human, never a bot that spoke last.
def test_telegram_owner_is_latest_human_not_bot(repo, capsys):
    telegram_ready(repo)
    mod = load(repo)
    chat = {"id": -1005, "type": "supergroup", "title": "Life", "is_forum": True}
    mod.FAKE.updates = [
        {"update_id": 1, "message": {"message_id": 1, "chat": chat, "from": {"id": 111, "is_bot": False}, "text": "a"}},
        {"update_id": 2, "message": {"message_id": 2, "chat": chat, "from": {"id": 222, "is_bot": False}, "text": "b"}},
        {"update_id": 3, "message": {"message_id": 3, "chat": chat, "from": {"id": 999, "is_bot": True}, "text": "c"}},
    ]
    rc, out = go(mod, capsys, "telegram")
    assert rc == 0, out
    assert json.loads((repo / "setup.json").read_text())["telegram"]["owner_id"] == 222


# Why: turning Topics on moves a group to a new id; a "hi" sent before that must still identify the owner.
def test_telegram_follows_group_migration(repo, capsys):
    telegram_ready(repo)
    mod = load(repo)
    mod.FAKE.updates = [
        {"update_id": 1, "message": {"message_id": 1, "chat": {"id": -55, "type": "group", "title": "Life"},
                                     "from": {"id": 333, "is_bot": False}, "text": "hi"}},
        {"update_id": 2, "message": {"message_id": 2, "chat": {"id": -55, "type": "group", "title": "Life"},
                                     "from": {"id": 333, "is_bot": False}, "migrate_to_chat_id": -1009}},
        {"update_id": 3, "my_chat_member": {"chat": {"id": -1009, "type": "supergroup", "title": "Life", "is_forum": True},
                                            "from": {"id": 333, "is_bot": False}}},
    ]
    rc, out = go(mod, capsys, "telegram")
    assert rc == 0, out
    t = json.loads((repo / "setup.json").read_text())["telegram"]
    assert (t["chat_id"], t["owner_id"]) == (-1009, 333)


# Why: a wrong bot token must say how to replace it.
def test_telegram_bad_token(repo, capsys):
    telegram_ready(repo)
    mod = load(repo)
    mod.FAKE.bot_ok = False
    rc, out = go(mod, capsys, "telegram")
    assert rc == 1 and "didn't accept the bot token" in last(out) and "secret TELEGRAM_BOT_TOKEN" in last(out)


# Why: re-running must not create duplicate topics.
def test_telegram_rerun_keeps_topics(repo, capsys):
    telegram_ready(repo)
    mod = load(repo)
    go(mod, capsys, "telegram")
    first = json.loads((repo / "setup.json").read_text())["telegram"]["topics"]
    mod.FAKE.calls.clear()
    rc, out = go(mod, capsys, "telegram")
    assert rc == 0
    assert json.loads((repo / "setup.json").read_text())["telegram"]["topics"] == first
    assert not any("createForumTopic" in c[2] for c in mod.FAKE.calls if c[0] == "http")


# ---------- discover (was: garmin) ----------

def garmin_ready(repo):
    cli(repo, "secret", "GARMIN_PERSONAL_API_KEY", stdin=GOOD["GARMIN_PERSONAL_API_KEY"] + "\n")


def metrics(repo):
    return tomllib.loads((repo / "profile.toml").read_text(encoding="utf-8")).get("metrics")


# Why: Garmin rate-limits key exchanges; a real run must build ONE client with the repo token cache
# (keyed by GARMIN_CACHE_KEY, generated here) so the first Actions runs reuse the token.
def test_discover_one_client_with_repo_cache(repo, capsys):
    garmin_ready(repo)
    (repo / "setup.json").write_text(json.dumps({"repo": "someone/garmin-life-kit"}))
    mod = load(repo)
    rc = mod.main(["discover", "--days", "90"])   # not a dry run: the stub kit stands in for Garmin
    out = capsys.readouterr().out
    assert rc == 0, out
    made = sys.modules["kit.garmin"].Client.made
    assert len(made) == 1 and made[0][0] == GOOD["GARMIN_PERSONAL_API_KEY"] and made[0][1].key
    assert len(read_env(repo)["GARMIN_CACHE_KEY"]) == 44
    for k in [k for k in sys.modules if k == "kit" or k.startswith("kit.")]:
        del sys.modules[k]


# Why: discovery writes what this account really has, one plain line each, keeping profile.toml's comments.
def test_discover_writes_metrics_and_lines(repo):
    garmin_ready(repo)
    rc, out = cli(repo, "discover", "--days", "365")
    assert rc == 0, out
    assert metrics(repo) == {"sleep": True, "hrv": True, "stress": True, "spo2": True}
    for line in ("Sleep: found", "HRV: found", "Weight: not available through the kit yet",
                 "Activity types, last 14 days: running 3, cycling 1"):
        assert line in out
    assert last(out).startswith("OK: The Garmin key works. Found 4 of 4 kinds of data, and 4 activities")
    text = (repo / "profile.toml").read_text(encoding="utf-8")
    assert text.startswith(PROFILE) and text.count("[metrics]") == 1
    s = json.loads((repo / "setup.json").read_text())
    assert s["discovered"]["available"] == ["sleep", "hrv", "stress", "spo2"]
    assert s["discovered"]["activity_types"] == {"running": 3, "cycling": 1}
    assert [e["name"] for e in s["expiries"]] == ["Garmin API key (GARMIN_PERSONAL_API_KEY)"]


# Why: the re-run rule. Gone -> off; newly found -> on; found both times but trimmed -> stays off.
def test_discover_rerun_rule(repo, monkeypatch):
    garmin_ready(repo)
    cli(repo, "discover", "--days", "365")
    p = repo / "profile.toml"
    p.write_text(p.read_text(encoding="utf-8").replace("stress = true", "stress = false"), encoding="utf-8")
    rc, out = cli(repo, "discover", "--days", "365")   # same device: the trim is kept
    assert rc == 0 and metrics(repo)["stress"] is False and "Stress: found (kept off, as you set it)" in out
    monkeypatch.setenv("KIT_DRY_FIXTURES", "garmin-sparse")   # a simpler watch
    rc, out = cli(repo, "discover", "--days", "365")
    assert rc == 0 and metrics(repo) == {"sleep": True, "hrv": False, "stress": False, "spo2": False}
    assert "HRV: not found" in out and "(not found: HRV, Stress, SpO2)" in last(out)
    monkeypatch.setenv("KIT_DRY_FIXTURES", "garmin")     # back to the full watch: newly found turns on
    cli(repo, "discover", "--days", "365")
    assert metrics(repo) == {"sleep": True, "hrv": True, "stress": True, "spo2": True}
    assert (repo / "profile.toml").read_text(encoding="utf-8").count("[metrics]") == 1


# Why: an account with nothing at all is almost always an unsynced watch; don't switch everything off.
def test_discover_empty_account_fails_without_writing_metrics(repo, monkeypatch):
    garmin_ready(repo)
    monkeypatch.setenv("KIT_DRY_FIXTURES", "garmin-empty")
    rc, out = cli(repo, "discover", "--days", "365")
    assert rc == 1 and "Garmin sent no data" in last(out) and "syncs" in last(out)
    assert metrics(repo) is None


# Why: on a rate limit the only right move is to stop and wait; nothing recorded, no retry.
@pytest.mark.parametrize("err,words", [("RateLimited", "rate limited"), ("KeyRejected", "key rejected"),
                                       ("ExchangeFailed", "couldn't reach Garmin"),
                                       ("GarminError", "Garmin gave an error for HRV")])
def test_discover_errors_stop(repo, capsys, err, words):
    garmin_ready(repo)
    mod = load(repo)
    mod.FAKE.garmin_error = err
    rc, out = go(mod, capsys, "discover", "--days", "365")
    assert rc == 1 and last(out).startswith(f"FAILED: {words}")
    assert "wait 30-60 minutes" in last(out).lower() or err != "RateLimited"
    assert len([c for c in mod.FAKE.calls if c[:2] == ("garmin", "DryRunClient")]) == 1
    assert not (repo / "setup.json").exists() and metrics(repo) is None


# Why: the key's lifetime drives the expiry warning; it must be asked for, not guessed.
def test_discover_needs_days(repo):
    garmin_ready(repo)
    rc, out = cli(repo, "discover")
    assert rc == 1 and "--days 365" in last(out)


# Why: old runbooks and notes say `garmin`; it must keep working as the same command.
def test_garmin_is_an_alias(repo):
    garmin_ready(repo)
    rc, out = cli(repo, "garmin", "--days", "30")
    assert rc == 0 and "profile.toml [metrics] is updated" in last(out)


# Why: status is how Claude resumes; it must say whether discovery ran and when.
def test_status_shows_discovery(repo):
    garmin_ready(repo)
    rc, out = cli(repo, "status")
    assert "Garmin data check: not run yet" in out
    cli(repo, "discover", "--days", "365")
    rc, out = cli(repo, "status")
    assert "Garmin data check: ran " in out and "(4 kinds of data found)" in out


# Why: what they wear explains gaps; the setting must be added once and then rewritten in place.
def test_profile_wears(repo):
    rc, out = cli(repo, "profile", "--name", "Sam", "--wears", "Forerunner 265, HRM-Pro strap")
    assert rc == 0, out
    rc, out = cli(repo, "profile", "--wears", "Fenix 8")
    text = (repo / "profile.toml").read_text(encoding="utf-8")
    assert tomllib.loads(text)["devices"]["wears"] == "Fenix 8"
    assert text.count("[devices]") == 1 and text.count("wears =") == 1


@pytest.mark.skipif(not (REAL_ROOT / "kit" / "discover.py").exists(), reason="kit/discover.py not built yet")
@pytest.mark.parametrize("fixtures", ["garmin", "garmin-sparse", "garmin-empty"])
def test_discover_against_real_kit(repo, monkeypatch, fixtures):
    # Why: the real kit.discover + kit.metrics + fixtures must drive the same command end to end.
    import shutil
    shutil.rmtree(repo / "kit")
    shutil.copytree(REAL_ROOT / "kit", repo / "kit", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(REAL_ROOT / "tests" / "fixtures", repo / "tests" / "fixtures")
    garmin_ready(repo)
    monkeypatch.setenv("KIT_DRY_FIXTURES", fixtures)
    rc, out = cli(repo, "discover", "--days", "365")
    assert last(out).startswith(("OK:", "FAILED:")), out
    if fixtures == "garmin-empty":   # no sleep, no activities: those switches are left alone, not turned off
        assert rc == 0, out
        assert "Sleep (bed, wake, hours): not checked" in out and "not checked yet and left as they were" in last(out)
        assert "sleep" not in metrics(repo) and "hr_zones" not in metrics(repo)
        return
    if fixtures == "garmin-sparse":
        assert rc == 0 and "not found" in out, out
        return
    if fixtures == "garmin":
        assert rc == 0, out
        names = subprocess.run([sys.executable, "-c", "from kit.metrics import METRICS; print(' '.join(METRICS))"],
                               cwd=REAL_ROOT, capture_output=True, text=True, check=True).stdout.split()
        assert set(metrics(repo)) == set(names)
        return
        assert set(metrics(repo)) == set(METRICS)


# ---------- secrets ----------

# Why: every Actions secret must reach GitHub with its exact value, over stdin, and never on screen.
def test_secrets_pushes_over_stdin(repo, capsys):
    prime(repo, upto="garmin")
    mod = load(repo)
    rc, out = go(mod, capsys, "secrets")
    assert rc == 0, out
    env = read_env(repo)
    pushed = {c[1][3]: c[2] for c in mod.FAKE.calls if c[0] == "run" and c[1][:3] == ["gh", "secret", "set"]}
    assert set(pushed) == {"TELEGRAM_BOT_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "GARMIN_PERSONAL_API_KEY",
                           "GARMIN_CACHE_KEY", "WORKER_API_SECRET"}
    for name, stdin in pushed.items():
        assert stdin == env[name]
    for c in mod.FAKE.calls:
        if c[0] == "run":
            assert not any(v in " ".join(c[1]) for v in env.values())   # never on a command line
    for v in env.values():
        assert v not in out
    names = [e["name"] for e in json.loads((repo / "setup.json").read_text())["expiries"]]
    assert "GitHub token for the Worker (WORKER_GITHUB_TOKEN)" not in names


# Why: the GitHub token is made without expiry; only record one when the person says it has one.
def test_secrets_github_token_days_records_expiry(repo):
    prime(repo, upto="garmin")
    rc, out = cli(repo, "secrets", "--github-token-days", "90")
    assert rc == 0, out
    names = [e["name"] for e in json.loads((repo / "setup.json").read_text())["expiries"]]
    assert "GitHub token for the Worker (WORKER_GITHUB_TOKEN)" in names


# Why: the repo will hold Log notes; never push secrets into a public copy.
def test_secrets_refuses_public_repo(repo, capsys):
    prime(repo, upto="garmin")
    mod = load(repo)
    mod.FAKE.private = False
    rc, out = go(mod, capsys, "secrets")
    assert rc == 1 and "is public" in last(out) and "--visibility private" in last(out)
    assert not any(c[0] == "run" and c[1][:3] == ["gh", "secret", "set"] for c in mod.FAKE.calls)


# Why: a missing pasted secret must name itself and the exact command to fix it.
def test_secrets_missing_human_secret(repo):
    cli(repo, "secret", "TELEGRAM_BOT_TOKEN", stdin=GOOD["TELEGRAM_BOT_TOKEN"] + "\n")
    rc, out = cli(repo, "secrets")
    assert rc == 1
    assert "missing: CLAUDE_CODE_OAUTH_TOKEN, GARMIN_PERSONAL_API_KEY" in last(out)
    assert "secret CLAUDE_CODE_OAUTH_TOKEN" in last(out)


# ---------- worker ----------

# Why: the Worker must get its five secrets (GitHub token under its Worker name) over stdin, and
# Telegram must be pointed at it with the webhook secret and only message updates.
def test_worker_secrets_webhook_and_commands(repo, capsys):
    prime(repo, upto="secrets")
    mod = load(repo)
    rc, out = go(mod, capsys, "worker")
    assert rc == 0, out
    env = read_env(repo)
    puts = {c[1][5]: c[2] for c in mod.FAKE.calls if c[0] == "run" and c[1][3:5] == ["secret", "put"]}
    assert puts == {"TELEGRAM_BOT_TOKEN": env["TELEGRAM_BOT_TOKEN"], "GITHUB_TOKEN": env["WORKER_GITHUB_TOKEN"],
                    "WORKER_API_SECRET": env["WORKER_API_SECRET"], "TELEGRAM_WEBHOOK_SECRET": env["TELEGRAM_WEBHOOK_SECRET"],
                    "SHORTCUT_SECRET": env["SHORTCUT_SECRET"]}
    http = {c[2].rsplit("/", 1)[-1]: c[3] for c in mod.FAKE.calls if c[0] == "http" and "telegram" in c[2]}
    s = json.loads((repo / "setup.json").read_text())
    assert http["setMyCommands"]["scope"] == {"type": "chat", "chat_id": s["telegram"]["chat_id"]}
    assert [c["command"] for c in http["setMyCommands"]["commands"]] == ["today", "week", "last", "status", "help"]
    gh = [c[1][3] for c in mod.FAKE.calls if c[0] == "run" and c[1][:3] == ["gh", "secret", "set"]]
    assert gh == ["WORKER_URL", "WORKER_API_SECRET"]
    order = [" ".join(c[1][3:5]) for c in mod.FAKE.calls if c[0] == "run" and c[1][:3] == mod.WRANGLER]
    assert order.index("deploy") > order.index("secret put") and order[0] == "whoami"
    # the cron goes live at deploy, so profile/setup must already be pushed to GitHub
    seq = [" ".join(c[1][:2]) if c[1][0] == "git" else " ".join(c[1][3:4]) for c in mod.FAKE.calls if c[0] == "run"]
    assert seq.index("git push") < seq.index("deploy")
    assert http["setWebhook"] == {"url": "https://garmin-life-kit.example.workers.dev/telegram",
                                  "secret_token": env["TELEGRAM_WEBHOOK_SECRET"], "allowed_updates": ["message"],
                                  "drop_pending_updates": True}


# Why: a new Cloudflare account has no workers.dev subdomain; the fix is a one-time click, say exactly where.
def test_worker_no_subdomain(repo, capsys):
    prime(repo, upto="secrets")
    mod = load(repo)
    mod.FAKE.deploy_rc = 1
    mod.FAKE.deploy_out = ("✘ [ERROR] You can either deploy your worker to one or more routes by specifying them in your "
                           "wrangler.toml file, or register a workers.dev subdomain here:\n"
                           "https://dash.cloudflare.com/abc123/workers/onboarding\n")
    rc, out = go(mod, capsys, "worker")
    assert rc == 1
    assert last(out) == ("FAILED: no workers.dev subdomain on this Cloudflare account yet. Open dash.cloudflare.com > "
                         "Workers & Pages, pick a subdomain once, then run this again.")
    assert not any("setWebhook" in c[2] for c in mod.FAKE.calls if c[0] == "http")


# Why: wrangler login is a human browser step; the script must stop and say so, not hang.
def test_worker_not_logged_in(repo, capsys):
    prime(repo, upto="secrets")
    mod = load(repo)
    mod.FAKE.logged_in = False
    rc, out = go(mod, capsys, "worker")
    assert rc == 1 and "not logged in to Cloudflare" in last(out) and "wrangler@4 login" in last(out)


# Why: a Worker that deployed but doesn't answer must not get Telegram pointed at it.
def test_worker_health_failure_stops_before_webhook(repo, capsys):
    prime(repo, upto="secrets")
    mod = load(repo)
    mod.FAKE.health_ok = False
    rc, out = go(mod, capsys, "worker")
    assert rc == 1 and "isn't answering yet" in last(out)
    assert not any("setWebhook" in c[2] for c in mod.FAKE.calls if c[0] == "http")


# Why: the KV id and URL come from wrangler's text output; parsing must survive its real 4.x formats.
def test_wrangler_output_parsing(repo):
    mod = load(repo)
    json_snippet = ('\x1b[33m⛅️ wrangler 4.20.0\x1b[0m\n🌀 Creating namespace with title "kit-KIT"\n✨ Success!\n'
                    '{\n  "kv_namespaces": [\n    {\n      "binding": "kit_KIT",\n      "id": "e29b263ab50e42ce9b637fa8370175e8"\n    }\n  ]\n}\n')
    toml_snippet = '[[kv_namespaces]]\nbinding = "KIT"\nid = "0123456789abcdef0123456789abcdef"\n'
    assert mod.parse_kv_id(json_snippet) == "e29b263ab50e42ce9b637fa8370175e8"
    assert mod.parse_kv_id(toml_snippet) == "0123456789abcdef0123456789abcdef"
    assert mod.parse_kv_id("✘ [ERROR] something") == ""
    deploy = ("Your Worker has access to the following bindings:\nenv.REPO (\"x/y\")  Environment Variable\n"
              "Uploaded my-kit (1.23 sec)\nDeployed my-kit triggers (0.45 sec)\n  \x1b[36mhttps://my-kit.sam-1.workers.dev\x1b[0m\n"
              "  schedule: */5 * * * *\nCurrent Version ID: 1\n")
    assert mod.parse_worker_url(deploy, "my-kit") == "https://my-kit.sam-1.workers.dev"
    assert mod.parse_worker_url("Uploaded my-kit\nNo targets deployed", "my-kit") == ""
    assert mod.is_no_subdomain("You need to register a workers.dev subdomain before publishing to workers.dev")
    assert not mod.is_no_subdomain(deploy)
    listing = 'some banner\n[\n  {"id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "title": "other"},\n  {"id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "title": "kit-KIT"}\n]\n'
    assert mod.parse_kv_list(listing, "kit-KIT") == "b" * 32


# Why: --dry-run writes made-up ids; it must never overwrite a real setup.json.
def test_dry_run_refuses_real_setup(repo):
    real = {"repo": "someone/garmin-life-kit", "telegram": {"chat_id": -100777, "owner_id": 1,
            "topics": {"today": 2, "fitness": 3, "log": 4, "alerts": 5}}}
    (repo / "setup.json").write_text(json.dumps(real))
    cli(repo, "secret", "TELEGRAM_BOT_TOKEN", stdin=GOOD["TELEGRAM_BOT_TOKEN"] + "\n")
    rc, out = cli(repo, "telegram", "--chat-id", "-100777")
    assert rc == 1 and "already set up for real" in last(out)
    assert json.loads((repo / "setup.json").read_text()) == real


# ---------- test ----------

# Why: `test` must commit the Garmin token cache too, so the first Actions runs don't exchange the key again.
def test_test_commits_token_cache_and_reports_success(repo, capsys):
    prime(repo, upto="worker")
    (repo / "data").mkdir(exist_ok=True)
    (repo / "data" / "garmin-token.enc").write_text("x")
    mod = load(repo)
    rc, out = go(mod, capsys, "test")
    assert rc == 0, out
    add = next(c[1] for c in mod.FAKE.calls if c[0] == "run" and c[1][:2] == ["git", "add"])
    assert add[3:] == ["profile.toml", "setup.json", "worker/wrangler.toml", "data/garmin-token.enc"]
    assert "https://github.com/someone/garmin-life-kit/actions/runs/123" in last(out)
    assert any(c[0] == "run" and c[1][:3] == ["gh", "run", "watch"] for c in mod.FAKE.calls)


# Why: a failed first recap must say so with the run URL, not report OK.
def test_test_reports_failure_with_url(repo, capsys):
    prime(repo, upto="worker")
    mod = load(repo)
    mod.FAKE.conclusion = "failure"
    rc, out = go(mod, capsys, "test")
    assert rc == 1 and "ended as failure" in last(out) and "/actions/runs/123" in last(out)
    assert "something broke" in out


# ---------- status / check / misc ----------

# Why: Claude runs status first when resuming; on a fresh copy it must point at the first step.
def test_status_on_fresh_copy(repo):
    rc, out = cli(repo, "status")
    assert rc == 0 and "0 of 5" in last(out) and "Next: profile" in last(out)
    assert "TELEGRAM_BOT_TOKEN: no" in out


# Why: a mistyped command must still end in the one-line FAILED format Claude relays.
def test_unknown_command_fails_in_format(repo):
    rc, out = cli(repo, "deploy")
    assert rc == 1 and last(out).startswith("FAILED: that command wasn't understood")


# ---------- dry-run (the offline pipeline) ----------

FAKE_MODULE = '''import os, sys
assert os.environ["KIT_DRY_RUN"] == "1"
assert os.environ["KIT_PROFILE"].endswith("profile.toml") and os.path.exists(os.environ["KIT_PROFILE"])
assert not os.environ["KIT_DATA_DIR"].startswith(os.getcwd())
print("--- [today] ---\\n" + " ".join(sys.argv[1:]) + " from " + __name__)
sys.exit(int(os.environ.get("FAIL_" + __spec__.name.split(".")[-1].upper(), "0")))
'''


def fake_kit(repo):
    (repo / "kit").mkdir(exist_ok=True)
    for m in ("recap", "cards", "commands"):
        (repo / "kit" / f"{m}.py").write_text(FAKE_MODULE)


# Why: dry-run must run every message type with the dry-run env (temp profile, temp data dir).
def test_dry_run_command_runs_every_message(repo):
    fake_kit(repo)
    rc, out = cli(repo, "dry-run")
    assert rc == 0, out
    for want in ("morning --source manual", "evening --source manual", "today ", "week ", "last ", "status "):
        assert want in out
    assert out.count("--- [today] ---") == 7
    assert "morning recap, an evening recap, an activity card and 4 command answers" in last(out)


# Why: a broken module must turn into FAILED naming the step, not a quiet OK.
def test_dry_run_command_fails_loud(repo, monkeypatch):
    fake_kit(repo)
    monkeypatch.setenv("FAIL_CARDS", "1")
    rc, out = cli(repo, "dry-run")
    assert rc == 1 and "activity card" in last(out)


# Why: the real kit (when present) must survive the same offline run the runbook does in step 4.
@pytest.mark.skipif(not (REAL_ROOT / "kit" / "recap.py").exists(), reason="kit/ not built yet")
def test_dry_run_against_real_kit(tmp_path):
    p = subprocess.run([sys.executable, str(REAL_ROOT / "setup" / "kit.py"), "dry-run"], capture_output=True,
                       text=True, cwd=REAL_ROOT, timeout=600)
    assert p.returncode == 0, p.stdout[-3000:] + p.stderr[-3000:]
    assert last(p.stdout).startswith("OK: Dry run worked")


# ---------- reviewer round 2 ----------

# Why: a new Claude token lasts a year from today; the old expiry date must be replaced, not kept.
def test_secret_claude_restamps_expiry(repo):
    (repo / "setup.json").write_text(json.dumps({"dry_run": True, "expiries": [
        {"name": "Claude token (CLAUDE_CODE_OAUTH_TOKEN)", "expires": "2020-01-01", "breaks": "", "fix": ""}]}))
    rc, out = cli(repo, "secret", "CLAUDE_CODE_OAUTH_TOKEN", stdin=GOOD["CLAUDE_CODE_OAUTH_TOKEN"] + "\n")
    assert rc == 0, out
    exp = json.loads((repo / "setup.json").read_text())["expiries"]
    assert len(exp) == 1 and exp[0]["expires"] > "2026-01-01"


# Why: the GitHub token is made with no expiry; only a --days answer may create an expiry warning.
def test_secret_github_token_days(repo):
    rc, out = cli(repo, "secret", "WORKER_GITHUB_TOKEN", "--days", "90", stdin=GOOD["WORKER_GITHUB_TOKEN"] + "\n")
    assert rc == 0, out
    names = lambda: [e["name"] for e in json.loads((repo / "setup.json").read_text())["expiries"]]  # noqa: E731
    assert names() == ["GitHub token for the Worker (WORKER_GITHUB_TOKEN)"]
    rc, out = cli(repo, "secret", "WORKER_GITHUB_TOKEN", stdin=GOOD["WORKER_GITHUB_TOKEN"] + "\n")
    assert rc == 0 and "old expiry date was removed" in last(out)
    assert names() == []
    rc, out = cli(repo, "secret", "TELEGRAM_BOT_TOKEN", "--days", "30", stdin=GOOD["TELEGRAM_BOT_TOKEN"] + "\n")
    assert rc == 1 and "--days only goes with WORKER_GITHUB_TOKEN" in last(out)


# Why: two people set up on one computer must never share a secrets file.
def test_env_file_is_per_owner_and_repo(repo):
    mod = load(repo)
    (repo / "setup.json").write_text(json.dumps({"repo": "alice/garmin-life-kit"}))
    a = mod.env_path()
    (repo / "setup.json").write_text(json.dumps({"repo": "bob/garmin-life-kit"}))
    b = mod.env_path()
    assert (a.name, b.name) == ("alice__garmin-life-kit.env", "bob__garmin-life-kit.env")


# Why: a group with Topics off is a plain "group"; the person needs "turn on Topics", not "found no group".
def test_telegram_basic_group_says_topics_off(repo, capsys):
    telegram_ready(repo)
    mod = load(repo)
    mod.FAKE.updates = [{"update_id": 1, "message": {"message_id": 1, "text": "hi",
                         "chat": {"id": -77, "type": "group", "title": "Life"}, "from": {"id": 5, "is_bot": False}}}]
    rc, out = go(mod, capsys, "telegram")
    assert rc == 1 and last(out).startswith('FAILED: topics are off in "Life"')


# Why: a login with several Cloudflare accounts can't pick one by itself; say how, and let --account-id fix it.
def test_worker_multiple_accounts(repo, capsys):
    prime(repo, upto="secrets")
    mod = load(repo)
    mod.FAKE.deploy_rc = 1
    mod.FAKE.deploy_out = ("✘ [ERROR] More than one account available but unable to select one in non-interactive mode. "
                           "Please set the appropriate account ID ...")
    rc, out = go(mod, capsys, "worker")
    assert rc == 1 and "more than one account" in last(out) and "worker --account-id ID" in last(out)
    mod = load(repo)
    rc, out = go(mod, capsys, "worker", "--account-id", "abc123")
    assert rc == 0, out
    assert mod.ACCOUNT_ID == "abc123" and mod.child_env()["CLOUDFLARE_ACCOUNT_ID"] == "abc123"
    assert json.loads((repo / "setup.json").read_text())["worker"]["account_id"] == "abc123"


# Why: the run to watch is the NEW one, found without trusting this computer's clock.
def test_test_picks_new_run_not_older_one(repo, capsys):
    prime(repo, upto="worker")
    mod = load(repo)
    rc, out = go(mod, capsys, "test")
    assert rc == 0, out
    watched = next(c[1] for c in mod.FAKE.calls if c[0] == "run" and c[1][:3] == ["gh", "run", "watch"])
    assert watched[3] == "123"


# ---------- update (real git: a template repo and a copy with no shared history) ----------

def sh(cwd, *a):
    return subprocess.run(a, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def write(root, files):
    for name, body in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(body, encoding="utf-8")


PROFILE_STUB = """import pathlib, tomllib
def load():
    d = tomllib.loads(pathlib.Path("profile.toml").read_text(encoding="utf-8"))
    if d.get("units") not in ("imperial", "metric"):
        raise SystemExit("units must be imperial or metric")
    return d
"""


@pytest.fixture
def kit_repos(tmp_path, monkeypatch):
    """Template repo (2 commits) + a copy made like GitHub's "Use this template": one commit, same tree, own origin."""
    gitcfg = tmp_path / "gitconfig"
    gitcfg.write_text("[user]\n\tname = Test\n\temail = test@example.com\n[init]\n\tdefaultBranch = main\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitcfg))   # ignore the real user's signing/hooks settings
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    tpl, copy, origin = tmp_path / "template", tmp_path / "copy", tmp_path / "origin.git"
    tpl.mkdir()
    write(tpl, {"profile.toml": PROFILE, "setup/kit.py": (REAL_ROOT / "setup" / "kit.py").read_text(),
                "kit/__init__.py": "", "kit/a.py": "VALUE = 1\n", "worker/src/index.js": "// v1\n",
                "kit/profile.py": PROFILE_STUB,
                "tests/test_ok.py": "def test_ok():\n    assert True\n", "data/.gitkeep": ""})
    sh(tpl, "git", "init", "-q", "-b", "main")
    sh(tpl, "git", "add", "-A")
    sh(tpl, "git", "commit", "-q", "-m", "v1")
    write(tpl, {"kit/a.py": "VALUE = 2\n"})
    sh(tpl, "git", "commit", "-q", "-am", "v2")
    import shutil
    shutil.copytree(tpl, copy, ignore=shutil.ignore_patterns(".git"))
    sh(copy, "git", "init", "-q", "-b", "main")
    sh(copy, "git", "add", "-A")
    sh(copy, "git", "commit", "-q", "-m", "Initial commit")
    sh(tmp_path, "git", "init", "-q", "--bare", "-b", "main", str(origin))
    sh(copy, "git", "remote", "add", "origin", str(origin))
    sh(copy, "git", "push", "-q", "origin", "main")
    # the person's own changes in their copy
    prof = (copy / "profile.toml").read_text(encoding="utf-8").replace('name = ""                ', 'name = "Sam"             ')
    write(copy, {"profile.toml": prof, "setup.json": '{"repo": "sam/garmin-life-kit"}\n'})
    sh(copy, "git", "add", "-A")
    sh(copy, "git", "commit", "-q", "-m", "Setup")
    sh(copy, "git", "push", "-q", "origin", "main")
    # GitHub Actions commit data/ to main every day, so origin is ahead of this computer when update runs.
    actions = tmp_path / "actions"
    sh(tmp_path, "git", "clone", "-q", str(origin), str(actions))
    write(actions, {"data/log.jsonl": '{"text": "slept badly"}\n'})
    sh(actions, "git", "add", "-A")
    sh(actions, "git", "commit", "-q", "-m", "data")
    sh(actions, "git", "push", "-q", "origin", "main")
    return tpl, copy, origin


def upstream(tpl, files, msg="upstream"):
    write(tpl, files)
    sh(tpl, "git", "add", "-A")
    sh(tpl, "git", "commit", "-q", "-m", msg)


def update(copy, *extra):
    p = subprocess.run([sys.executable, str(copy / "setup" / "kit.py"), "update", *extra], cwd=copy,
                       capture_output=True, text=True, env=os.environ.copy(), timeout=600)
    return p.returncode, p.stdout + p.stderr


def drop_grafts(copy):
    for ref in sh(copy, "git", "for-each-ref", "--format=%(refname:short)", "refs/replace/").split():
        sh(copy, "git", "replace", "-d", ref.split("/")[-1])


# Why: the whole point: upstream code arrives, the person's profile/setup survive (even where the
# template changed a default), new settings appear with defaults, history gets linked, and it's pushed.
def test_update_merges_template_into_copy(kit_repos):
    tpl, copy, origin = kit_repos
    new_profile = PROFILE.replace('morning = "07:30"', 'morning = "07:00"').replace(
        "watchdog = true\n", "watchdog = true\nnew_toggle = true             # added upstream\n")
    new_profile += '\n[later]                       # a new section\nthing = "x"\n'
    upstream(tpl, {"kit/a.py": "VALUE = 3\n", "worker/src/index.js": "// v3\n", "profile.toml": new_profile})
    rc, out = update(copy, "--template", str(tpl))
    assert rc == 0, out
    assert "Linked this copy to the public kit's history" in out
    assert "The Worker changed: run uv run setup/kit.py worker next." in last(out)
    assert "features.new_toggle" in last(out) and "[later]" in last(out)
    assert (copy / "kit" / "a.py").read_text() == "VALUE = 3\n"
    text = (copy / "profile.toml").read_text(encoding="utf-8")
    p = tomllib.loads(text)
    assert p["name"] == "Sam" and p["times"]["morning"] == "07:30"          # theirs kept, not the new default
    assert p["features"]["new_toggle"] is True and p["later"]["thing"] == "x"
    assert "new_toggle = true             # added upstream" in text
    assert json.loads((copy / "setup.json").read_text()) == {"repo": "sam/garmin-life-kit"}
    assert (copy / "data" / "log.jsonl").exists()   # the Actions commit came in first
    assert sh(copy, "git", "status", "--porcelain") == ""
    assert sh(origin, "git", "rev-parse", "main") == sh(copy, "git", "rev-parse", "HEAD")
    drop_grafts(copy)   # the merge commit itself now links the histories
    assert sh(copy, "git", "merge-base", "HEAD", "template/main") == sh(tpl, "git", "rev-parse", "HEAD")


# Why: the second update must work from the merge commit alone (grafts are local-only and not pushed).
def test_second_update_without_graft(kit_repos):
    tpl, copy, origin = kit_repos
    upstream(tpl, {"kit/a.py": "VALUE = 3\n"})
    rc, out = update(copy, "--template", str(tpl))
    assert rc == 0, out
    drop_grafts(copy)
    upstream(tpl, {"kit/a.py": "VALUE = 4\n"})
    rc, out = update(copy)   # remote already there
    assert rc == 0, out
    assert "Linked" not in out and "No Worker changes, so nothing else to run." in last(out)
    assert (copy / "kit" / "a.py").read_text() == "VALUE = 4\n"
    rc, out = update(copy)
    assert rc == 0 and last(out) == "OK: Already up to date with the public kit. Nothing changed."
    write(copy, {"kit/c.py": "X = 1\n"})   # a local commit not pushed yet
    sh(copy, "git", "add", "-A")
    sh(copy, "git", "commit", "-q", "-m", "local")
    rc, out = update(copy)
    assert rc == 0 and "Pushed the commits that were waiting here" in last(out)
    assert sh(origin, "git", "rev-parse", "main") == sh(copy, "git", "rev-parse", "HEAD")


# Why: a code clash must never leave a half-merged repo; it aborts, lists the file, changes nothing.
def test_update_conflict_aborts_clean(kit_repos):
    tpl, copy, origin = kit_repos
    sh(copy, "git", "pull", "-q", "--ff-only", "origin", "main")
    write(copy, {"kit/a.py": "VALUE = 99\n"})
    sh(copy, "git", "commit", "-q", "-am", "my tweak")
    before = sh(copy, "git", "rev-parse", "HEAD")
    upstream(tpl, {"kit/a.py": "VALUE = 3\n"})
    rc, out = update(copy, "--template", str(tpl))
    assert rc == 1 and "clashes with changes made in this copy, in: kit/a.py" in last(out)
    assert sh(copy, "git", "rev-parse", "HEAD") == before
    assert sh(copy, "git", "status", "--porcelain") == ""
    assert not (copy / ".git" / "MERGE_HEAD").exists()


# Why: an update that breaks the tests must be undone, not pushed.
def test_update_red_tests_resets(kit_repos):
    tpl, copy, origin = kit_repos
    before = sh(origin, "git", "rev-parse", "main")
    upstream(tpl, {"tests/test_bad.py": "def test_bad():\n    assert False\n", "kit/a.py": "VALUE = 3\n"})
    rc, out = update(copy, "--template", str(tpl))
    assert rc == 1 and "made the kit's tests fail, so I undid it" in last(out)
    assert sh(copy, "git", "rev-parse", "HEAD") == before
    assert sh(origin, "git", "rev-parse", "main") == before
    assert (copy / "kit" / "a.py").read_text() == "VALUE = 2\n"


# Why: uncommitted work must never be mixed into (or lost by) an update.
def test_update_needs_clean_tree(kit_repos):
    tpl, copy, origin = kit_repos
    write(copy, {"kit/a.py": "VALUE = 7\n"})
    rc, out = update(copy, "--template", str(tpl))
    assert rc == 1 and "aren't committed" in last(out)
    assert (copy / "kit" / "a.py").read_text() == "VALUE = 7\n"


# Why: a copy that doesn't match any template commit can't be grafted safely; say so, change nothing.
def test_update_no_matching_template_commit(tmp_path, kit_repos):
    tpl, copy, origin = kit_repos
    other = tmp_path / "other"
    import shutil
    shutil.copytree(tpl, other, ignore=shutil.ignore_patterns(".git"))
    write(other, {"kit/a.py": "VALUE = 42\n"})
    sh(other, "git", "init", "-q", "-b", "main")
    sh(other, "git", "add", "-A")
    sh(other, "git", "commit", "-q", "-m", "Initial commit")
    sh(tmp_path, "git", "init", "-q", "--bare", "-b", "main", str(tmp_path / "other.git"))
    sh(other, "git", "remote", "add", "origin", str(tmp_path / "other.git"))
    sh(other, "git", "push", "-q", "origin", "main")
    rc, out = update(other, "--template", str(tpl))
    assert rc == 1 and "couldn't match this copy to the public kit's history" in last(out)
    assert "keeping profile.toml, setup.json, data/ and worker/wrangler.toml" in last(out)



# Why: grafts are local-only. A fresh clone of the pushed copy must be sound and still linked to the
# template's history with no replace ref at all, or the next update on another computer breaks.
def test_update_fresh_clone_is_linked_without_graft(tmp_path, kit_repos):
    tpl, copy, origin = kit_repos
    upstream(tpl, {"kit/a.py": "VALUE = 3\n"})
    rc, out = update(copy, "--template", str(tpl))
    assert rc == 0 and "Linked" in out, out
    fresh = tmp_path / "fresh"
    sh(tmp_path, "git", "clone", "-q", str(origin), str(fresh))
    assert subprocess.run(["git", "fsck", "--no-progress"], cwd=fresh, capture_output=True).returncode == 0
    sh(fresh, "git", "remote", "add", "template", str(tpl))
    sh(fresh, "git", "fetch", "-q", "template")
    assert sh(fresh, "git", "for-each-ref", "refs/replace/") == ""
    assert sh(fresh, "git", "merge-base", "HEAD", "template/main") == sh(tpl, "git", "rev-parse", "HEAD")


# Why: local commits AND new commits on GitHub can't be fast-forwarded; stop before touching anything.
def test_update_stops_when_diverged_from_origin(kit_repos):
    tpl, copy, origin = kit_repos
    write(copy, {"kit/b.py": "B = 1\n"})
    sh(copy, "git", "add", "-A")
    sh(copy, "git", "commit", "-q", "-m", "local only")
    before = sh(copy, "git", "rev-parse", "HEAD")
    rc, out = update(copy, "--template", str(tpl))
    assert rc == 1 and "both have changes" in last(out) and "git pull origin main" in last(out)
    assert sh(copy, "git", "rev-parse", "HEAD") == before


# Why: updating any branch but main would push the wrong thing or merge into a side branch.
def test_update_only_on_main(kit_repos):
    tpl, copy, origin = kit_repos
    sh(copy, "git", "checkout", "-q", "-b", "side")
    rc, out = update(copy, "--template", str(tpl))
    assert rc == 1 and "on side, not main" in last(out)


# Why: a merge left over from an earlier run must be cleaned up by a person, not built upon.
def test_update_refuses_stale_merge_head(kit_repos):
    tpl, copy, origin = kit_repos
    (copy / ".git" / "MERGE_HEAD").write_text(sh(copy, "git", "rev-parse", "HEAD") + "\n")
    rc, out = update(copy, "--template", str(tpl))
    assert rc == 1 and "Run: git merge --abort" in last(out)


# Why: an update whose kit rejects the person's profile would break every recap; undo it, push nothing.
def test_update_resets_when_profile_no_longer_loads(kit_repos):
    tpl, copy, origin = kit_repos
    before = sh(origin, "git", "rev-parse", "main")
    upstream(tpl, {"kit/profile.py": PROFILE_STUB.replace('("imperial", "metric")', '("metric",)')})
    rc, out = update(copy, "--template", str(tpl))
    assert rc == 1 and "no longer accepts profile.toml" in last(out) and "units must be" in last(out)
    assert sh(copy, "git", "rev-parse", "HEAD") == before == sh(origin, "git", "rev-parse", "main")
    assert sh(copy, "git", "status", "--porcelain") == ""


# Why: anything that blows up mid-merge (here a broken line in the template's profile) must abort the merge.
def test_update_aborts_merge_on_unexpected_error(kit_repos):
    tpl, copy, origin = kit_repos
    before = sh(origin, "git", "rev-parse", "main")
    upstream(tpl, {"profile.toml": PROFILE.replace("watchdog = true\n", "watchdog = true\nbroken =\n"),
                   "kit/a.py": "VALUE = 3\n"})
    rc, out = update(copy, "--template", str(tpl))
    assert rc == 1 and "\nFAILED: something unexpected went wrong" in out
    assert sh(copy, "git", "rev-parse", "HEAD") == before
    assert not (copy / ".git" / "MERGE_HEAD").exists() and sh(copy, "git", "status", "--porcelain") == ""


# Why: every v0.1 copy has no "discovered" record; a metric they already turned off must stay off.
def test_discover_keeps_explicit_false_without_history(repo):
    garmin_ready(repo)
    p = repo / "profile.toml"
    p.write_text(p.read_text(encoding="utf-8") + "\n[metrics]\nstress = false\n", encoding="utf-8")
    rc, out = cli(repo, "discover", "--days", "365")
    assert rc == 0, out
    assert metrics(repo) == {"stress": False, "sleep": True, "hrv": True, "spo2": True}
    assert "Stress: found (kept off, as you set it)" in out


# Why: with no sleep record there's nothing to judge by; "unknown" switches must stay exactly as set,
# say "not checked", and the run fails asking for a sync instead of switching things off.
def test_discover_unknown_left_alone(repo, monkeypatch):
    garmin_ready(repo)
    p = repo / "profile.toml"
    p.write_text(p.read_text(encoding="utf-8") + "\n[metrics]\nhrv = false\nsleep = true\n", encoding="utf-8")
    before = p.read_text(encoding="utf-8")
    monkeypatch.setenv("KIT_DRY_FIXTURES", "garmin-empty")
    rc, out = cli(repo, "discover", "--days", "365")
    assert rc == 1 and "Garmin sent no data" in last(out)
    assert p.read_text(encoding="utf-8") == before


def test_discover_unknown_not_counted(repo, capsys):
    # Why: an unknown metric is neither found nor missing; it is printed as not checked and keeps its value.
    garmin_ready(repo)
    p = repo / "profile.toml"
    p.write_text(p.read_text(encoding="utf-8") + "\n[metrics]\nspo2 = false\n", encoding="utf-8")
    mod = load(repo)
    import types as _t
    real = mod.import_kit
    def fake_import(*names):
        mods = real(*names)
        d = _t.SimpleNamespace(run=lambda c, t: {"available": ["sleep", "hrv"], "missing": ["stress"],
                                                 "unknown": {"spo2": "no activities to judge by"},
                                                 "activity_types": {}, "not_in_kit": {"weight": "not available through the kit yet"}})
        return [d if n == "discover" else m for n, m in zip(names, mods)]
    mod.import_kit = fake_import
    rc, out = go(mod, capsys, "discover", "--days", "365")
    assert rc == 0, out
    assert "SpO2: not checked (no activities to judge by)" in out and "Stress: not found" in out
    assert "Found 2 of 4 kinds of data (not found: Stress); 1 not checked yet" in last(out)
    assert metrics(repo) == {"spo2": False, "sleep": True, "hrv": True, "stress": False}
    for k in [k for k in sys.modules if k == "kit" or k.startswith("kit.")]:
        del sys.modules[k]
