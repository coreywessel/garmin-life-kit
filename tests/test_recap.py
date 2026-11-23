"""The recap guard, the receipt, and the Claude/Garmin failure paths."""
import json
import re

import pytest

from kit import claude, garmin, profile, recap, weather

COACHING = re.compile(r"(?i)\b(should|rest day|recover|try to|consider|advice|risk|take it easy|"
                      r"push harder|great|good job|well done|too much|too little)\b")


@pytest.fixture
def pipeline(monkeypatch):
    """Garmin from fixtures, no weather, Claude answers 'CLAUDE TEXT'."""
    monkeypatch.setattr(garmin, "connect", lambda: garmin.DryRunClient())
    monkeypatch.setattr(weather, "forecast", lambda p: None)
    monkeypatch.setattr(claude, "ask", lambda system, inp, stub=None: "CLAUDE TEXT")


def receipts():
    try:
        return json.loads((profile.data_dir() / "receipts.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def to(sent, topic):
    return [t for target, t in sent if target == topic]


def test_guard_stands_down_when_already_sent(pipeline, sent):
    """A late duplicate Worker tick must not send a second recap."""
    recap.save_receipt("morning", profile.today())
    assert recap.run("morning", "schedule") == 0
    assert sent == []


def test_guard_never_blocks_a_manual_run(pipeline, sent):
    """A person asking for a recap by hand always gets one."""
    recap.save_receipt("morning", profile.today())
    recap.run("morning", "manual")
    assert to(sent, "today") == ["CLAUDE TEXT"]


def test_guard_sends_when_receipt_is_from_yesterday(pipeline, sent):
    """Yesterday's receipt must not block today's recap."""
    (profile.data_dir()).mkdir(parents=True, exist_ok=True)
    (profile.data_dir() / "receipts.json").write_text('{"morning": "2026-01-13"}')
    recap.run("morning", "schedule")
    assert to(sent, "today") == ["CLAUDE TEXT"]
    assert receipts()["morning"] == "2026-01-14"


def test_receipt_only_after_telegram_confirms(pipeline, sent):
    """A failed send must leave no receipt, so the guard lets the next tick retry and the watchdog alerts."""
    sent.fail_on = "today"
    with pytest.raises(SystemExit) as e:
        recap.run("evening", "schedule")
    assert e.value.code == 1
    assert "evening" not in receipts()
    assert "not sent" in (profile.OUT / "error.txt").read_text(encoding="utf-8")


def test_receipt_is_written_after_the_send_not_before(pipeline, sent, monkeypatch):
    """The receipt must not exist yet at the moment of sending."""
    seen = {}
    real = recap.telegram.send

    def check(target, text, silent=False):
        if target == "today":
            seen["receipt_at_send"] = receipts().get("morning")
        return real(target, text, silent)
    monkeypatch.setattr(recap.telegram, "send", check)
    recap.run("morning", "schedule")
    assert seen["receipt_at_send"] is None
    assert receipts()["morning"] == "2026-01-14"


def test_claude_failure_sends_plain_numbers_and_alerts(pipeline, sent, monkeypatch):
    """No Claude must still mean a recap, plus an alert that names the likely cause."""
    def boom(*a, **k):
        raise claude.ClaudeFailed("exit 1: Invalid API key")
    monkeypatch.setattr(claude, "ask", boom)
    assert recap.run("morning", "schedule") == 0
    today = to(sent, "today")[0]
    assert "Plain numbers" in today and "Sleep: bed 11:05 pm" in today
    alerts = " ".join(to(sent, "alerts"))
    assert "Claude didn't write the morning recap" in alerts and "claude setup-token" in alerts
    assert receipts()["morning"] == "2026-01-14"


@pytest.mark.parametrize("error,head", [
    (garmin.KeyRejected("Garmin rejected the API key (HTTP 401)"), "API key rejected"),
    (garmin.RateLimited("Garmin rate-limited the API key (HTTP 429)"), "rate-limited"),
    (garmin.ExchangeFailed("couldn't reach Garmin (URLError)"), "Garmin fetch failed"),
])
def test_garmin_failure_alerts_the_specific_reason_and_still_sends(pipeline, sent, monkeypatch, error, head):
    """Each Garmin failure gets its own alert with its own fix, and the recap still goes out."""
    def boom():
        raise error
    monkeypatch.setattr(garmin, "connect", boom)
    recap.run("morning", "schedule")
    alerts = " ".join(to(sent, "alerts"))
    assert head in alerts and error.fix.split()[0] in alerts
    assert to(sent, "today") == ["CLAUDE TEXT"]


@pytest.mark.parametrize("mode", ["morning", "evening"])
def test_fallback_reports_and_never_coaches(mode):
    """The code-built text obeys the same hard rule as the prompts."""
    from kit import fetch, numbers
    p = profile.load()
    data = fetch.collect(garmin.DryRunClient(), profile.today())
    ctx = (numbers.morning if mode == "morning" else numbers.evening)(data, p, profile.today(),
                                                                     {"today": {"summary": "clear", "high": "40°F", "low": "20°F"},
                                                                      "tomorrow": {"summary": "snow", "high": "30°F", "low": "10°F"}})
    text = recap.fallback(mode, ctx)
    assert not COACHING.search(text), COACHING.search(text)
    assert re.search(r"\d", text)


@pytest.mark.parametrize("name", ["morning", "evening"])
def test_prompts_forbid_coaching(name):
    """Editing a prompt must not quietly drop the report-only rules."""
    text = (recap.PROMPTS / f"{name}.md").read_text(encoding="utf-8")
    for rule in ("report, never coach", "Never suggest rest", "Never make a health claim",
                 "Never label risk", "Use each value exactly as given"):
        assert rule in text, rule


def test_turned_off_recap_does_nothing_on_schedule(pipeline, sent, monkeypatch, kit_env):
    """A recap switched off in profile.toml must not send from the schedule."""
    from conftest import write_profile
    write_profile(kit_env / "profile2.toml", **{"evening_recap = true": "evening_recap = false"})
    monkeypatch.setenv("KIT_PROFILE", str(kit_env / "profile2.toml"))
    assert recap.run("evening", "schedule") == 0
    assert sent == []
