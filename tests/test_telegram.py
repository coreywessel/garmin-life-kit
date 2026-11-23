"""Telegram sends: long messages split cleanly, broken HTML still arrives, failures are loud."""
import pytest

from kit import profile, telegram


def test_split_keeps_sections_whole_and_under_the_limit():
    """A long message is cut only between sections, each part within Telegram's cap."""
    blocks = [f"<b>Section {i}</b>\n" + ("word " * 150).strip() for i in range(12)]
    text = "\n\n".join(blocks)
    parts = telegram.split(text)
    assert len(parts) > 1
    assert all(telegram.tg_len(p) <= telegram.LIMIT for p in parts)
    assert "\n\n".join(parts) == text


def test_split_cuts_one_enormous_line():
    """Even a single line longer than the cap is sent in pieces, not refused."""
    parts = telegram.split("x" * 9000)
    assert all(telegram.tg_len(p) <= telegram.LIMIT for p in parts)
    assert "".join(parts) == "x" * 9000


@pytest.fixture
def calls(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:SECRET")
    log = []
    replies = []

    def fake(method, **fields):
        log.append(fields)
        return replies.pop(0) if replies else {"ok": True, "result": {"message_id": len(log)}}
    monkeypatch.setattr(telegram, "call", fake)
    return log, replies


def test_broken_html_is_resent_as_plain_text(calls):
    """Claude's broken HTML must still reach the phone, as plain text."""
    log, replies = calls
    replies.append({"ok": False, "description": "Bad Request: can't parse entities: unclosed tag"})
    ids = telegram.send("today", "<b>Sleep: 7 h &amp; more")
    assert ids == [2]
    assert log[0]["parse_mode"] == "HTML" and log[0]["message_thread_id"] == 2
    assert "parse_mode" not in log[1] and log[1]["text"] == "Sleep: 7 h & more"


def test_refused_send_raises_without_leaking_the_token(calls):
    """A failed send must fail the run, and the error must not contain the bot token."""
    _, replies = calls
    replies.append({"ok": False, "description": "Forbidden: bot was kicked from the supergroup chat"})
    with pytest.raises(telegram.TelegramError) as e:
        telegram.send("alerts", "hello")
    assert "bot was kicked" in str(e.value)
    assert "SECRET" not in str(e.value) and "api.telegram.org" not in str(e.value)


def test_no_setup_ids_says_which_step(monkeypatch, kit_env):
    """Before setup step 6, sending says exactly that."""
    monkeypatch.setattr(profile, "SETUP", kit_env / "missing.json")
    with pytest.raises(telegram.TelegramError, match="run setup step 6"):
        telegram.send("today", "hi")


def test_thread_id_and_general_targets(calls):
    """Commands reply in the thread they came from, or General when there's none."""
    log, _ = calls
    telegram.send(77, "a")
    telegram.send("", "b")
    assert log[0]["message_thread_id"] == 77
    assert "message_thread_id" not in log[1] or log[1]["message_thread_id"] is None


def test_dry_run_prints_instead_of_sending(monkeypatch, capsys, no_sockets):
    """A dry run never touches the network."""
    monkeypatch.setenv("KIT_DRY_RUN", "1")
    telegram.send("fitness", "<b>card</b>", silent=True)
    assert "--- [fitness] (silent) ---" in capsys.readouterr().out


def test_failure_alert_includes_reason_and_log(monkeypatch, kit_env):
    """The workflow failure alert says what broke (out/error.txt) and links the run."""
    (kit_env / "out").mkdir()
    (kit_env / "out" / "error.txt").write_text("The morning recap was not sent. chat not found\n")
    monkeypatch.setenv("RUN_URL", "https://github.com/x/y/actions/runs/1")
    text = telegram.failed_text("The morning recap")
    assert "<b>The morning recap failed.</b>" in text and "chat not found" in text and "runs/1" in text
