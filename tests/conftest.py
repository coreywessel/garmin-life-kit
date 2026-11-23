"""Every test runs against a temporary profile, setup.json and data folder, never the repo's."""
import json
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
# A copy of the shipped profile.toml template. Tests never read the live profile.toml: after setup
# it holds the owner's settings, and CI must stay green in every copy.
TEMPLATE = Path(__file__).resolve().parent / "fixtures" / "profile-template.toml"
sys.path.insert(0, str(ROOT))

from kit import log, profile, telegram  # noqa: E402

PROFILE = """
name = "Alex"
timezone = "America/Denver"
units = "imperial"
week_starts = "monday"

[location]
label = "Testville"
lat = 40.0
lon = -105.0

[times]
morning = "07:30"
evening = "21:00"

[features]
morning_recap = true
evening_recap = true
activity_cards = true
weather = true
log_notes = true
watchdog = true

[recap]
sleep_target_bed = "22:30"
about = "Test person."
"""
SETUP = {"repo": "someone/kit", "telegram": {"chat_id": -100123, "owner_id": 7, "bot_username": "t_bot",
                                             "topics": {"today": 2, "fitness": 3, "log": 4, "alerts": 5}},
         "worker": {"url": "https://w.example"}, "expiries": []}
CLEAR = ("KIT_DRY_RUN", "KIT_DRY_CLAUDE", "TELEGRAM_BOT_TOKEN", "GARMIN_PERSONAL_API_KEY", "GARMIN_CACHE_KEY",
         "WORKER_URL", "WORKER_API_SECRET", "GITHUB_TOKEN", "RUN_URL", "GARMIN_PERSONAL_API_BASE_URL")


def write_profile(path, text=PROFILE, **replace):
    for k, v in replace.items():
        text = text.replace(k, v)
    path.write_text(text)
    return path


@pytest.fixture(autouse=True)
def kit_env(tmp_path, monkeypatch):
    for k in CLEAR:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("KIT_PROFILE", str(write_profile(tmp_path / "profile.toml")))
    monkeypatch.setenv("KIT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KIT_TODAY", profile.FIXTURE_TODAY)
    (tmp_path / "setup.json").write_text(json.dumps(SETUP))
    monkeypatch.setattr(profile, "SETUP", tmp_path / "setup.json")
    monkeypatch.setattr(profile, "OUT", tmp_path / "out")
    monkeypatch.setattr(log, "ACK", tmp_path / "out" / "log-ack.json")
    return tmp_path


@pytest.fixture
def sent(monkeypatch):
    """Record every Telegram send instead of making it. Set sent.fail_on = "today" to make sends there fail."""
    class Box(list):
        fail_on = None

    box = Box()

    def fake_send(target, text, silent=False):
        if box.fail_on is not None and target == box.fail_on:
            raise telegram.TelegramError("Telegram didn't take the message: Bad Request: chat not found")
        box.append((target, text))
        return [len(box)]
    monkeypatch.setattr(telegram, "send", fake_send)
    return box


@pytest.fixture
def no_sockets(monkeypatch):
    """Any network attempt fails the test."""
    def blocked(*a, **k):
        raise AssertionError("network used in a test that must be offline")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
