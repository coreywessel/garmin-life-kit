"""Discovery against a fake Garmin built from the fixtures: right answer, cheap, never retries a 429."""
import base64
import io
import json
import urllib.error
import urllib.parse
from datetime import date
from pathlib import Path

import pytest

from kit import discover, garmin
from kit.metrics import NAMES

FIX = Path(__file__).resolve().parent / "fixtures"
TODAY = date(2026, 1, 14)
NOW = 1_800_000_000


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def jwt(exp):
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{enc({'alg': 'none'})}.{enc({'exp': exp})}.sig"


@pytest.fixture
def fake_garmin(monkeypatch):
    """Serve a fixture folder through the real Client's HTTP layer, counting every request."""
    monkeypatch.setattr(garmin.time, "time", lambda: NOW)
    state = {"exchange": 0, "data": 0, "folder": "garmin", "error": None}

    def urlopen(req, timeout=None):
        url = urllib.parse.urlparse(req.full_url)
        q = dict(urllib.parse.parse_qsl(url.query))
        if url.path == garmin.TOKEN_PATH:
            state["exchange"] += 1
            return Resp(jwt(NOW + 86400).encode())
        state["data"] += 1
        if state["error"]:
            raise urllib.error.HTTPError(req.full_url, state["error"], "x", {}, None)
        f = lambda n: json.loads((FIX / state["folder"] / f"{n}.json").read_text(encoding="utf-8"))
        if "activities" in url.path:
            body = [a for a in f("activities") if q["startDate"] <= a["startTimeLocal"][:10] <= q["endDate"]]
        elif "dailySleep" in url.path:
            body = f("sleep").get(q["date"], {})
        elif url.path.startswith("/hrv-service/hrv/"):
            body = f("hrv").get(url.path.rsplit("/", 1)[1], {})
        else:
            body = f("summary").get(q["calendarDate"], {})
        return Resp(json.dumps(body).encode())
    monkeypatch.setattr(garmin.urllib.request, "urlopen", urlopen)
    return state


SPARSE = ["sleep", "sleep_score", "resting_hr", "steps", "respiration"]
ACT = {"vo2max", "training_load", "training_effect", "hr_zones"}
SLEEP = {"sleep", "sleep_stages", "sleep_score", "respiration", "spo2"}


@pytest.mark.parametrize("folder,available,unknown", [
    ("garmin", set(NAMES), set()),
    ("garmin-sparse", set(SPARSE), set()),
    ("garmin-empty", {"steps"}, ACT | SLEEP),
])
def test_discovery_finds_what_the_account_returns(fake_garmin, folder, available, unknown):
    """Each kind of account gets exactly the metrics it really has, in the table's order."""
    fake_garmin["folder"] = folder
    out = discover.run(garmin.Client("k"), TODAY)
    assert out["available"] == [n for n in NAMES if n in available]
    assert set(out["unknown"]) == unknown
    assert out["missing"] == [n for n in NAMES if n not in available and n not in unknown]
    assert out["sampled_dates"] == ["2026-01-13", "2026-01-11", "2026-01-07"]
    assert out["not_in_kit"] == {"weight": "not available through the kit yet"}


def test_no_samples_is_unknown_not_missing(fake_garmin):
    """No activities or no sleep means 'can't tell', so setup must not switch those metrics off."""
    fake_garmin["folder"] = "garmin-empty"
    out = discover.run(garmin.Client("k"), TODAY)
    assert out["unknown"]["hr_zones"] == discover.NO_ACTIVITIES
    assert out["unknown"]["sleep_stages"] == discover.NO_SLEEP
    assert not ACT & set(out["missing"]) and not SLEEP & set(out["missing"])
    # A basic watch WITH activities but no zones: that's a real "missing".
    fake_garmin["folder"] = "garmin-sparse"
    assert "hr_zones" in discover.run(garmin.Client("k"), TODAY)["missing"]


def test_discovery_is_cheap(fake_garmin):
    """One token exchange and at most 12 data calls, so a re-run can't trigger Garmin's 429."""
    out = discover.run(garmin.Client("k"), TODAY)
    assert fake_garmin["exchange"] == 1
    assert fake_garmin["data"] <= 12 and out["calls"] == fake_garmin["data"]


def test_discovery_counts_activity_types(fake_garmin):
    """Activity types from the last 14 days, so setup can say what it saw."""
    out = discover.run(garmin.Client("k"), TODAY)
    assert out["activity_types"] == {"running": 4, "cycling": 1, "strength_training": 1, "resort_snowboarding": 1}
    fake_garmin["folder"] = "garmin-empty"
    assert discover.run(garmin.Client("k"), TODAY)["activity_types"] == {}


def test_429_during_discovery_raises_without_retry(fake_garmin):
    """Retrying a 429 is what keeps a key rate-limited: stop at the first one."""
    fake_garmin["error"] = 429
    with pytest.raises(garmin.RateLimited):
        discover.run(garmin.Client("k"), TODAY)
    assert fake_garmin["exchange"] == 1 and fake_garmin["data"] == 1


def test_one_bad_endpoint_is_just_missing(fake_garmin, monkeypatch):
    """A 500 on one call means 'no data from it', not a failed discovery."""
    c = garmin.Client("k")
    real = c.hrv

    def broken(d):
        raise garmin.GarminError("Garmin /hrv answered HTTP 500")
    monkeypatch.setattr(c, "hrv", broken)
    out = discover.run(c, TODAY)
    assert "hrv" in out["missing"] and "sleep" in out["available"]
    assert real  # the real method existed; only this instance was broken


def test_code_bug_surfaces_instead_of_switching_a_metric_off(fake_garmin, monkeypatch):
    """A bug (not a Garmin error) must crash discovery, not quietly mark HRV as missing."""
    c = garmin.Client("k")

    def bug(d):
        raise KeyError("oops")
    monkeypatch.setattr(c, "hrv", bug)
    with pytest.raises(KeyError):
        discover.run(c, TODAY)
