"""
Garmin data through the official Garmin Personal API (personal.connectapi.garmin.com).

The person makes an API key at connect.garmin.com/app/settings/personalApiToken. The key is
swapped for a 24-hour token (a JWT). The token is cached and reused while it has more than an
hour left, because Garmin answers 429 after about a dozen exchanges in a burst.

    client = connect()                       # key from env + encrypted cache in data/
    client.sleep("2026-01-14")               # the sleep record itself (night ending that morning)

The Personal API has no training readiness, training status, lap splits or PR list. v1
doesn't use them.

Errors say what broke and the fix in plain words (str(e) is the whole line, e.fix the fix):
KeyRejected (key expired, deleted or wrong), RateLimited (429), ExchangeFailed (Garmin down,
network, junk reply). All are GarminError.
"""
import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

from kit import profile

KEY_ENV = "GARMIN_PERSONAL_API_KEY"
CACHE_KEY_ENV = "GARMIN_CACHE_KEY"
BASE = os.environ.get("GARMIN_PERSONAL_API_BASE_URL", "https://personal.connectapi.garmin.com").rstrip("/")
TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"
KEY_PAGE = "connect.garmin.com/app/settings/personalApiToken"
MIN_LEFT = 3600          # reuse a cached token only with more than this many seconds left
PAGE = 100
TIMEOUT = 30
UA = "garmin-life-kit/1.0"
FIXTURES = profile.ROOT / "tests" / "fixtures" / "garmin"
NEW_KEY_FIX = (f"make a new key at {KEY_PAGE}, then ask Claude to run "
               "`uv run setup/kit.py secret GARMIN_PERSONAL_API_KEY` and `uv run setup/kit.py secrets`")


class GarminError(RuntimeError):
    fix = "check the Actions log for the Garmin step"

    def __init__(self, what, fix=None):
        self.what = what
        if fix:
            self.fix = fix
        super().__init__(f"{what}. Fix: {self.fix}")


class KeyRejected(GarminError):
    fix = NEW_KEY_FIX


class RateLimited(GarminError):
    fix = "nothing to do, it clears on its own within the hour. The next run tries again"


class ExchangeFailed(GarminError):
    fix = "usually Garmin or the network having a bad moment. The next run tries again; if it keeps happening, check status.garmin.com"


# ---------- token cache ----------

def _key_id(key):
    """Which key a cached token came from, so a new key is never 'checked' with an old token."""
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _seconds_left(token):
    """Seconds until the JWT's exp claim. 0 when it can't be read, so it's never reused."""
    try:
        part = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))["exp"] - time.time()
    except Exception:
        return 0


class RepoCache:
    """Encrypted token cache committed to the private repo (data/garmin-token.enc).
    No GARMIN_CACHE_KEY (or a bad one) = no caching; everything still works, it just spends
    one key exchange per run."""

    def __init__(self, path=None, key=None):
        self.path = Path(path or profile.data_dir() / "garmin-token.enc")
        key = (key if key is not None else os.environ.get(CACHE_KEY_ENV, "")).strip()
        self._f = None
        if key:
            try:
                from cryptography.fernet import Fernet
                self._f = Fernet(key.encode())
            except ValueError:
                print(f"WARNING: {CACHE_KEY_ENV} isn't a valid key - not caching the Garmin token")

    def load(self):
        if not self._f:
            return {}
        from cryptography.fernet import InvalidToken
        try:
            return json.loads(self._f.decrypt(self.path.read_bytes()))
        except (OSError, InvalidToken, ValueError):
            return {}

    def save(self, entry):
        if not self._f:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(self._f.encrypt(json.dumps(entry).encode()))


class FileCache:
    """Plain JSON token cache for the MCP server on the person's own computer (mode 0600)."""

    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save(self, entry):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(entry, f)
        os.chmod(self.path, 0o600)


def _iso(d):
    return d.isoformat() if isinstance(d, date) else str(d)


# ---------- client ----------

class Client:
    """One Garmin connection. The token is fetched on the first data call: a fresh cached token
    means no exchange at all, otherwise exactly one per Client."""

    def __init__(self, key, cache=None):
        self._key = key.strip()
        self._cache = cache
        self._token = None
        self._from_cache = False

    def _ensure_token(self):
        if self._token:
            return
        saved = self._cache.load() if self._cache else {}
        token = saved.get("token") if saved.get("key") == _key_id(self._key) else None
        if token and _seconds_left(token) > MIN_LEFT:
            self._token, self._from_cache = token, True
        else:
            self._new_token()

    def _new_token(self):
        self._token = self._exchange()
        self._from_cache = False
        if self._cache:
            entry = {"key": _key_id(self._key), "token": self._token}
            if self._cache.load() != entry:      # Fernet output differs every time: write only a real change
                self._cache.save(entry)

    def _exchange(self):
        req = urllib.request.Request(BASE + TOKEN_PATH, data=b"", method="POST",
                                     headers={"Accept": "*/*", "Authorization": f"ApiKey {self._key}",
                                              "User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                token = r.read().decode().strip()
        except urllib.error.HTTPError as e:
            if e.code in (400, 401, 403):
                raise KeyRejected(f"Garmin rejected the API key (HTTP {e.code}) - it has expired, "
                                  "been deleted, or was pasted wrong") from None
            if e.code == 429:
                raise RateLimited("Garmin rate-limited the API key (HTTP 429)") from None
            raise ExchangeFailed(f"Garmin's key exchange failed (HTTP {e.code})") from None
        except OSError as e:                     # URLError, timeouts, DNS
            raise ExchangeFailed(f"couldn't reach Garmin ({type(e).__name__})") from None
        if token.count(".") != 2:
            raise ExchangeFailed("Garmin's key exchange returned something that isn't a token")
        return token

    def _get(self, path, **params):
        self._ensure_token()
        params = {k: v for k, v in params.items() if v is not None}
        url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": UA,
                                                   "Authorization": f"Bearer {self._token}"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                body = r.read()
        except urllib.error.HTTPError as e:
            if e.code == 401 and self._from_cache:
                self._new_token()                # cached token no longer accepted: one fresh exchange, then retry
                return self._get(path, **params)
            if e.code == 401:
                raise KeyRejected(f"Garmin refused a brand-new token (HTTP 401 on {path})") from None
            if e.code == 429:
                raise RateLimited(f"Garmin rate-limited the API key (HTTP 429 on {path})") from None
            raise GarminError(f"Garmin {path} answered HTTP {e.code}") from None
        except OSError as e:
            raise GarminError(f"couldn't reach Garmin for {path} ({type(e).__name__})",
                              ExchangeFailed.fix) from None
        try:
            return json.loads(body) if body.strip() else None
        except ValueError:
            raise GarminError(f"Garmin {path} returned something that isn't JSON") from None

    # ---------- data ----------

    def activities(self, start_date, end_date):
        """Every activity from start_date through end_date (local dates), paged 100 at a time."""
        out, start = [], 0
        while True:
            page = self._get("/activitylist-service/activities/search/activities",
                             startDate=_iso(start_date), endDate=_iso(end_date),
                             start=start, limit=PAGE) or []
            out += page
            if len(page) < PAGE:
                return out
            start += PAGE

    def latest_activities(self, limit=20):
        return self._get("/activitylist-service/activities/search/activities", start=0, limit=limit) or []

    def sleep(self, d):
        """The sleep record for the night ending on the morning of d. Not wrapped."""
        rec = self._get("/sleep-service/sleep/dailySleep", date=_iso(d)) or {}
        return rec.get("dailySleepDTO", rec) if isinstance(rec, dict) else {}

    def hrv(self, d):
        return self._get(f"/hrv-service/hrv/{_iso(d)}") or {}

    def daily_summary(self, d):
        return self._get("/usersummary-service/usersummary/daily", calendarDate=_iso(d)) or {}


class DryRunClient:
    """Same methods, answered from tests/fixtures/garmin (invented data, never real).
    Used when KIT_DRY_RUN=1. The fixtures' today is profile.FIXTURE_TODAY."""

    def __init__(self, folder=FIXTURES):
        self._f = {n: json.loads((Path(folder) / f"{n}.json").read_text(encoding="utf-8"))
                   for n in ("activities", "sleep", "hrv", "summary")}

    def activities(self, start_date, end_date):
        s, e = _iso(start_date), _iso(end_date)
        return [a for a in self._f["activities"] if s <= a["startTimeLocal"][:10] <= e]

    def latest_activities(self, limit=20):
        return sorted(self._f["activities"], key=lambda a: a["startTimeLocal"], reverse=True)[:limit]

    def sleep(self, d):
        return self._f["sleep"].get(_iso(d)) or {}

    def hrv(self, d):
        return self._f["hrv"].get(_iso(d)) or {}

    def daily_summary(self, d):
        return self._f["summary"].get(_iso(d)) or {}


def connect():
    """The client the runtime uses: fixtures in a dry run, else the env key + repo cache."""
    if profile.dry():
        return DryRunClient()
    key = os.environ.get(KEY_ENV, "").strip()
    if not key:
        raise KeyRejected(f"{KEY_ENV} is not set", "ask Claude to run `uv run setup/kit.py secret "
                          "GARMIN_PERSONAL_API_KEY`, then `uv run setup/kit.py secrets`")
    return Client(key, RepoCache())
