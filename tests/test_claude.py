"""How the recap calls Claude: outside the repo, bounded time, two tries, then a clear failure."""
import subprocess
from pathlib import Path

import pytest

from kit import claude, profile


class Done:
    def __init__(self, code, out="", err=""):
        self.returncode, self.stdout, self.stderr = code, out, err


@pytest.fixture
def runs(monkeypatch):
    calls, answers = [], []

    def fake(cmd, **kw):
        calls.append((cmd, kw))
        return answers.pop(0)
    monkeypatch.setattr(claude.subprocess, "run", fake)
    monkeypatch.setattr(claude, "WAIT", 0)
    return calls, answers


def test_runs_outside_the_repo_with_no_tools(runs):
    """The repo's CLAUDE.md (setup runbook) must never be loaded into a recap, and Claude gets no tools."""
    calls, answers = runs
    answers.append(Done(0, "words\n"))
    assert claude.ask("system", "input") == "words"
    cmd, kw = calls[0]
    assert Path(kw["cwd"]).resolve() != profile.ROOT.resolve()
    assert profile.ROOT.resolve() not in Path(kw["cwd"]).resolve().parents
    assert cmd[:4] == ["claude", "-p", "--tools", ""] and kw["input"] == "input"


def test_two_tries_fit_inside_the_job_timeout():
    """2 tries plus the pause must leave room for installs in the 20-minute job."""
    assert claude.TRIES * claude.TIMEOUT + claude.WAIT <= 11 * 60


def test_retries_once_then_raises_with_the_reason(runs):
    """One bad answer is retried; two raise ClaudeFailed carrying Claude's own message."""
    calls, answers = runs
    answers += [Done(1, err="Invalid API key"), Done(1, err="Invalid API key")]
    with pytest.raises(claude.ClaudeFailed, match="Invalid API key"):
        claude.ask("s", "i")
    assert len(calls) == 2


def test_timeout_is_retried(runs, monkeypatch):
    """A hung Claude counts as a failed try, not a hung job."""
    calls, answers = runs
    seq = [subprocess.TimeoutExpired("claude", 300), Done(0, "ok")]

    def fake(cmd, **kw):
        calls.append(kw)
        x = seq.pop(0)
        if isinstance(x, Exception):
            raise x
        return x
    monkeypatch.setattr(claude.subprocess, "run", fake)
    assert claude.ask("s", "i") == "ok"
