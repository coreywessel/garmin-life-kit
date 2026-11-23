"""Log notes reach data/log.jsonl exactly once, and the Worker is only told to forget what was collected."""
import json

import pytest

from kit import log


@pytest.fixture
def worker(monkeypatch):
    monkeypatch.setenv("WORKER_URL", "https://w.example")
    monkeypatch.setenv("WORKER_API_SECRET", "s")
    calls = []
    items = [{"key": "log:100:1", "ts": 100, "thread": 4, "text": "slept badly", "message_id": 1},
             {"key": "log:200:2", "ts": 200, "thread": None, "text": "knee ok", "message_id": 2}]

    def fake(method, path, body=None):
        calls.append((method, path, body))
        if path == "/log":
            return {"ok": True, "items": items, "more": False}
        return {"ok": True, "deleted": len(body["keys"])}
    monkeypatch.setattr(log, "worker", fake)
    return calls


def filed():
    return [json.loads(l) for l in log.log_path().read_text(encoding="utf-8").splitlines()]


def test_collect_files_each_note_once(worker):
    """The Worker repeats a batch until it's cleared; repeats must not be filed twice."""
    log.collect()
    log.collect()
    rows = filed()
    assert [r["message_id"] for r in rows] == [1, 2]
    assert rows[0]["topic"] == "log" and rows[1]["topic"] == "general"


def test_ack_sends_exactly_the_collected_keys(worker):
    """ack clears only what this run collected (repeats included)."""
    log.collect()
    log.ack()
    assert worker[-1] == ("POST", "/log/ack", {"keys": ["log:100:1", "log:200:2"]})


def test_failed_collect_leaves_nothing_to_ack(worker, monkeypatch):
    """If collect fails, a leftover ack file must not clear notes that were never filed."""
    log.collect()
    def boom(*a, **k):
        raise log.LogError("couldn't reach the Worker")
    monkeypatch.setattr(log, "worker", boom)
    with pytest.raises(log.LogError):
        log.collect()
    assert json.loads(log.ACK.read_text(encoding="utf-8")) == []


def test_no_worker_is_a_no_op(monkeypatch):
    """Before the Worker is deployed, collect and ack do nothing and succeed."""
    assert log.collect() == 0 and log.ack() == 0
