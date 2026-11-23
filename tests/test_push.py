"""tools/push.sh against real git: two clones racing to push to one bare origin."""
import os
import subprocess
from pathlib import Path

import pytest

PUSH = Path(__file__).resolve().parent.parent / "tools" / "push.sh"
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def git(cwd, *args, check=True):
    return subprocess.run(["git", *args], cwd=cwd, env={**os.environ, **GIT_ENV}, check=check,
                          capture_output=True, text=True)


def write(repo, rel, text):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def commit(repo, msg):
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)


@pytest.fixture
def repos(tmp_path):
    """origin (bare) with one commit; clones a and b both at that commit."""
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    seed = tmp_path / "seed"
    git(tmp_path, "clone", "-q", str(origin), str(seed))
    git(seed, "checkout", "-q", "-b", "main")
    write(seed, "data/garmin-token.enc", "token-0\n")
    write(seed, "data/log.jsonl", "note-0\n")
    commit(seed, "seed")
    git(seed, "push", "-q", "-u", "origin", "main")
    a, b = tmp_path / "a", tmp_path / "b"
    git(tmp_path, "clone", "-q", str(origin), str(a))
    git(tmp_path, "clone", "-q", str(origin), str(b))
    return origin, a, b


def push(repo):
    return subprocess.run(["bash", str(PUSH)], cwd=repo, env={**os.environ, **GIT_ENV},
                          capture_output=True, text=True, timeout=120)


def on_origin(origin, rel):
    return git(origin, "show", f"main:{rel}").stdout


def test_enc_only_conflict_keeps_mains_copy(repos):
    """Two jobs each saving a Garmin token must not fail a run: main's token wins, push succeeds."""
    origin, a, b = repos
    write(a, "data/garmin-token.enc", "token-a\n"); commit(a, "a")
    assert push(a).returncode == 0
    write(b, "data/garmin-token.enc", "token-b\n"); commit(b, "b")
    r = push(b)
    assert r.returncode == 0, r.stdout + r.stderr
    assert on_origin(origin, "data/garmin-token.enc") == "token-a\n"
    assert not (b / ".git" / "rebase-merge").exists()


def test_enc_conflict_keeps_the_rest_of_the_commit(repos):
    """Keeping main's token must not drop the receipt that was in the same commit."""
    origin, a, b = repos
    write(a, "data/garmin-token.enc", "token-a\n"); commit(a, "a")
    assert push(a).returncode == 0
    write(b, "data/garmin-token.enc", "token-b\n")
    write(b, "data/receipts.json", '{"morning": "2026-01-14"}\n'); commit(b, "b")
    r = push(b)
    assert r.returncode == 0, r.stdout + r.stderr
    assert on_origin(origin, "data/garmin-token.enc") == "token-a\n"
    assert on_origin(origin, "data/receipts.json") == '{"morning": "2026-01-14"}\n'


def test_other_conflict_fails_loudly_and_aborts(repos):
    """A real conflict (Log notes) must fail the run with ::error:: and leave no half-done rebase."""
    origin, a, b = repos
    write(a, "data/log.jsonl", "note-0\nnote-a\n"); commit(a, "a")
    assert push(a).returncode == 0
    write(b, "data/log.jsonl", "note-0\nnote-b\n"); commit(b, "b")
    r = push(b)
    assert r.returncode == 1
    assert "::error::" in r.stdout + r.stderr
    assert on_origin(origin, "data/log.jsonl") == "note-0\nnote-a\n"
    assert not (b / ".git" / "rebase-merge").exists() and not (b / ".git" / "rebase-apply").exists()


def test_no_race_pushes_normally(repos):
    """The plain case: nothing new on main, the commit lands."""
    origin, a, _ = repos
    write(a, "data/receipts.json", "{}\n"); commit(a, "a")
    assert push(a).returncode == 0
    assert on_origin(origin, "data/receipts.json") == "{}\n"
