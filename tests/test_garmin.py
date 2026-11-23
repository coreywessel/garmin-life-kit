"""The Personal API token is cached so each job doesn't spend a key exchange (Garmin 429s after ~12)."""
import base64
import io
import json
import os
import stat
import urllib.error

import pytest
from cryptography.fernet import Fernet

from kit import garmin

NOW = 1_800_000_000
KEY = "test-api-key"


def jwt(exp):
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{enc({'alg': 'none'})}.{enc({'exp': exp})}.sig"


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def net(tmp_path, monkeypatch):
    """A fake Garmin: counts exchanges, records which token each data call used."""
    monkeypatch.setattr(garmin.time, "time", lambda: NOW)
    state = {"exchange": 0, "data": [], "fresh": jwt(NOW + 24 * 3600), "exchange_error": None,
             "data_401": set(), "data_error": None, "fkey": Fernet.generate_key().decode()}
    state["cache"] = garmin.RepoCache(tmp_path / "garmin-token.enc", key=state["fkey"])

    def urlopen(req, timeout=None):
        if req.full_url.endswith(garmin.TOKEN_PATH):
            state["exchange"] += 1
            if state["exchange_error"]:
                raise urllib.error.HTTPError(req.full_url, state["exchange_error"], "x", {}, None)
            return Resp(state["fresh"].encode())
        token = req.get_header("Authorization").split()[1]
        state["data"].append(token)
        if token in state["data_401"]:
            raise urllib.error.HTTPError(req.full_url, 401, "x", {}, None)
        if state["data_error"]:
            raise urllib.error.HTTPError(req.full_url, state["data_error"], "x", {}, None)
        return Resp(b'{"totalSteps": 1}')
    monkeypatch.setattr(garmin.urllib.request, "urlopen", urlopen)
    return state


def seed(net, token, key=KEY):
    net["cache"].save({"key": garmin._key_id(key), "token": token})


def cached(net):
    return net["cache"].load().get("token")


def test_fresh_cached_token_is_reused(net):
    """A token with hours left must be reused, and the cache file not rewritten (no commit churn)."""
    tok = jwt(NOW + 3 * 3600)
    seed(net, tok)
    before = net["cache"].path.read_bytes()
    c = garmin.Client(KEY, net["cache"])
    c.daily_summary("2026-01-14")
    c.sleep("2026-01-14")
    assert net["exchange"] == 0
    assert net["data"] == [tok, tok]
    assert net["cache"].path.read_bytes() == before


def test_token_with_under_an_hour_left_is_replaced(net):
    """A token about to expire mid-run must be swapped first, and the new one cached."""
    seed(net, jwt(NOW + 30 * 60))
    garmin.Client(KEY, net["cache"]).daily_summary("2026-01-14")
    assert net["exchange"] == 1
    assert net["data"] == [net["fresh"]]
    assert cached(net) == net["fresh"]


def test_token_from_another_key_is_never_used(net):
    """After a new key is pasted, an old key's token must not 'prove' the new key works."""
    seed(net, jwt(NOW + 10 * 3600), key="old-key")
    garmin.Client(KEY, net["cache"]).daily_summary("2026-01-14")
    assert net["exchange"] == 1
    assert net["data"] == [net["fresh"]]


def test_one_exchange_per_client(net):
    """Many calls on one client spend exactly one exchange when there's no cache."""
    c = garmin.Client(KEY, None)
    for _ in range(3):
        c.hrv("2026-01-14")
    assert net["exchange"] == 1


def test_401_on_cached_token_exchanges_once_and_retries(net):
    """Garmin can revoke a cached token early: one fresh exchange and a retry, not a failed run."""
    old = jwt(NOW + 5 * 3600)
    seed(net, old)
    net["data_401"].add(old)
    assert garmin.Client(KEY, net["cache"]).daily_summary("2026-01-14") == {"totalSteps": 1}
    assert net["exchange"] == 1
    assert net["data"] == [old, net["fresh"]]
    assert cached(net) == net["fresh"]


def test_401_on_fresh_token_is_key_rejected(net):
    """A brand-new token refused means the key itself is bad: KeyRejected, no retry loop."""
    net["data_401"].add(net["fresh"])
    with pytest.raises(garmin.KeyRejected) as e:
        garmin.Client(KEY, net["cache"]).daily_summary("2026-01-14")
    assert net["exchange"] == 1
    assert garmin.KEY_PAGE in str(e.value)


@pytest.mark.parametrize("code", [400, 401, 403])
def test_rejected_exchange_is_key_rejected_with_fix(net, code):
    """An expired or deleted key must alert as 'key rejected' with where to make a new one."""
    net["exchange_error"] = code
    with pytest.raises(garmin.KeyRejected) as e:
        garmin.Client(KEY).sleep("2026-01-14")
    assert garmin.KEY_PAGE in e.value.fix


def test_429_on_exchange_is_rate_limited(net):
    """A 429 is not the key's fault: RateLimited, so the alert says 'wait', not 'make a new key'."""
    net["exchange_error"] = 429
    with pytest.raises(garmin.RateLimited) as e:
        garmin.Client(KEY).sleep("2026-01-14")
    assert not isinstance(e.value, garmin.KeyRejected)
    assert "on its own" in e.value.fix


def test_429_on_data_is_rate_limited(net):
    """Rate limiting on a data call must also say RateLimited, not a generic error."""
    net["data_error"] = 429
    with pytest.raises(garmin.RateLimited):
        garmin.Client(KEY).sleep("2026-01-14")


def test_5xx_exchange_is_exchange_failed(net):
    """Garmin being down is ExchangeFailed: different alert from a bad key."""
    net["exchange_error"] = 503
    with pytest.raises(garmin.ExchangeFailed):
        garmin.Client(KEY).sleep("2026-01-14")


def test_no_cache_key_still_works_and_writes_nothing(net, tmp_path):
    """No GARMIN_CACHE_KEY must not break Garmin, and must not write a plaintext token anywhere."""
    cache = garmin.RepoCache(tmp_path / "plain.enc", key="")
    garmin.Client(KEY, cache).sleep("2026-01-14")
    assert net["exchange"] == 1
    assert not (tmp_path / "plain.enc").exists()


def test_file_cache_is_private(tmp_path, net):
    """The MCP token cache on a personal computer must be readable by the owner only."""
    cache = garmin.FileCache(tmp_path / "c" / "token.json")
    garmin.Client(KEY, cache).sleep("2026-01-14")
    assert stat.S_IMODE(os.stat(cache.path).st_mode) == 0o600
    assert cache.load()["token"] == net["fresh"]


def test_sleep_unwraps_dailysleepdto(net, monkeypatch):
    """If Garmin ever wraps the record, callers still get the record itself."""
    monkeypatch.setattr(garmin.Client, "_get", lambda self, path, **p: {"dailySleepDTO": {"sleepTimeSeconds": 5}})
    assert garmin.Client(KEY).sleep("2026-01-14") == {"sleepTimeSeconds": 5}
