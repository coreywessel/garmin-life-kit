"""
Ask Claude to write words. Runs `claude -p` on the owner's Claude subscription
(CLAUDE_CODE_OAUTH_TOKEN in Actions). No tools, no saved session: text in, text out.

    ask(system, input) -> text      raises ClaudeFailed after 2 tries

A dry run returns the given stub (or a fixed line) without calling Claude, unless
KIT_DRY_CLAUDE=1, which calls the real Claude so prompt changes can be tried locally.
"""
import os
import subprocess
import tempfile
import time

from kit import profile

TIMEOUT = 300           # 2 tries + the pause must fit well inside the 20-minute job
TRIES = 2
WAIT = 20


class ClaudeFailed(RuntimeError):
    """Claude didn't answer. Most often the token expired or was revoked."""


def ask(system, prompt_input, stub=None):
    if profile.dry() and os.environ.get("KIT_DRY_CLAUDE") != "1":
        return stub if stub is not None else "(dry run: Claude was not called)"
    last = "no attempt made"
    for attempt in range(1, TRIES + 1):
        try:
            r = subprocess.run(["claude", "-p", "--tools", "", "--no-session-persistence",
                                "--system-prompt", system],
                               input=prompt_input, capture_output=True, text=True, timeout=TIMEOUT,
                               # Outside the repo, so its CLAUDE.md (the setup runbook) never leaks into a recap.
                               cwd=tempfile.gettempdir())
        except FileNotFoundError:
            raise ClaudeFailed("the claude command isn't installed on this runner "
                               "(npm install -g @anthropic-ai/claude-code)") from None
        except subprocess.TimeoutExpired:
            last = f"no answer in {TIMEOUT // 60} minutes"
        else:
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
            detail = (r.stderr.strip() or r.stdout.strip())[:300] or "no output"
            last = f"exit {r.returncode}: {detail}"
        print(f"claude attempt {attempt} failed ({last})")
        if attempt < TRIES:
            time.sleep(WAIT)
    raise ClaudeFailed(last)
