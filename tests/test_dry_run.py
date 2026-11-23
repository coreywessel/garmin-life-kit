"""A full dry run with no network, no secrets and no setup.json: what `setup/kit.py dry-run` relies on."""
import pytest

from conftest import TEMPLATE
from kit import cards, commands, profile, recap, watchdog


@pytest.fixture
def dry(monkeypatch, kit_env, no_sockets):
    monkeypatch.setenv("KIT_DRY_RUN", "1")
    monkeypatch.delenv("KIT_DATA_DIR")
    monkeypatch.delenv("KIT_TODAY")
    monkeypatch.setenv("KIT_PROFILE", str(TEMPLATE))   # an unfilled template copy, never the live profile
    monkeypatch.setattr(profile, "SETUP", kit_env / "no-setup.json")


def test_full_dry_run_offline(dry, capsys, kit_env):
    """Morning, evening, a card, every command and the watchdog all print, with sockets blocked."""
    before = sorted(p.name for p in (profile.ROOT / "data").iterdir())
    assert recap.main(["morning"]) == 0
    assert recap.main(["evening"]) == 0
    assert cards.run() == 0
    for c in ("today", "week", "last", "status"):
        assert commands.main([c, ""]) == 0
    assert watchdog.run("evening") == 0
    out = capsys.readouterr().out
    assert out.count("--- [today] ---") == 1             # morning, with a sound
    assert out.count("--- [today] (silent) ---") == 1    # evening closes the day quietly
    assert "<b>Morning, Sam.</b>" in out and "<b>Evening, Sam.</b>" in out
    assert "Sleep: bed 11:05 pm, up 6:40 am" in out
    assert "--- [fitness] (silent) ---" in out and "<b>Morning Run</b>" in out
    assert "<b>Week so far</b>" in out and "<b>Status</b>" in out
    assert "--- [alerts] ---" in out                 # the fake Claude-token expiry warning
    # Dry-run state went to out/dry-run (here a temp dir), never the real data/ folder.
    assert sorted(p.name for p in (profile.ROOT / "data").iterdir()) == before
    assert (kit_env / "out" / "dry-run" / "receipts.json").exists()


def test_dry_run_cards_repeat_the_same_card(dry, capsys):
    """Every dry run shows a card (the seen list is never saved in a dry run)."""
    cards.run()
    cards.run()
    assert capsys.readouterr().out.count("<b>Morning Run</b>") == 2
