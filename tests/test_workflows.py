"""Workflow YAML checks, text-based (no YAML library): the reliability rules can't be edited away quietly."""
import re
from pathlib import Path

import pytest

WF = Path(__file__).resolve().parent.parent / ".github" / "workflows"
RUNTIME = {"recap.yml": 20, "collect.yml": 20, "command.yml": 20, "watchdog.yml": 10}


def text(name):
    return (WF / name).read_text(encoding="utf-8")


def steps(src):
    """The job's steps as text chunks, in order."""
    lines = src.splitlines()
    start = next(i for i, l in enumerate(lines) if l.strip() == "steps:")
    indent = len(lines[start]) - len(lines[start].lstrip()) + 2
    out, cur = [], None
    for l in lines[start + 1:]:
        if l.strip() and len(l) - len(l.lstrip()) < indent:
            break
        if l.startswith(" " * indent + "- "):
            cur = [l]
            out.append(cur)
        elif cur is not None:
            cur.append(l)
    return ["\n".join(c) for c in out]


def step(src, name):
    return next(s for s in steps(src) if re.search(rf"name: {re.escape(name)}\s*$", s, re.M))


def run_blocks(src):
    """Every run: script, single-line or block."""
    lines, out = src.splitlines(), []
    for i, l in enumerate(lines):
        m = re.match(r"^(\s*)(- )?run:\s*(.*)$", l)
        if not m:
            continue
        if m.group(3) not in ("|", ">", "|-", ">-"):
            out.append(m.group(3))
            continue
        base = len(m.group(1)) + (2 if m.group(2) else 0)
        body = []
        for nxt in lines[i + 1:]:
            if nxt.strip() and len(nxt) - len(nxt.lstrip()) <= base:
                break
            body.append(nxt)
        out.append("\n".join(body))
    return out


ALL = sorted(p.name for p in WF.glob("*.yml"))


def test_all_five_workflows_exist():
    """The contract's five workflows are all there."""
    assert ALL == ["collect.yml", "command.yml", "recap.yml", "test.yml", "watchdog.yml"]


@pytest.mark.parametrize("name", ALL)
def test_no_expressions_inside_run_scripts(name):
    """Inputs reach scripts only through env: a ${{ }} pasted into a script can run odd input as code."""
    for block in run_blocks(text(name)):
        assert "${{" not in block, f"{name}: {block}"


@pytest.mark.parametrize("name", ALL)
def test_every_job_has_a_timeout(name):
    """A hung step must not burn Actions minutes for 6 hours."""
    assert re.search(r"timeout-minutes: \d+", text(name))


@pytest.mark.parametrize("name,minutes", RUNTIME.items())
def test_runtime_timeouts_and_checkout_main(name, minutes):
    """Each job runs on the newest main and has the contract's timeout."""
    src = text(name)
    assert f"timeout-minutes: {minutes}" in src
    assert re.search(r"uses: actions/checkout@v4\s+with:\s+ref: main", src)


@pytest.mark.parametrize("name", ["recap.yml", "collect.yml"])
def test_notes_are_cleared_only_after_a_confirmed_push(name):
    """The Worker forgets notes only once the commit holding them is on main."""
    src = text(name)
    names = [re.search(r"name: (.+)", s).group(1).strip() for s in steps(src) if "name:" in s]
    assert names.index("Save") < names.index("Clear collected notes")
    assert "if: always()" in step(src, "Save") and "bash tools/push.sh" in step(src, "Save")
    clear = step(src, "Clear collected notes")
    assert "if: success()" in clear and "python -m kit.log ack" in clear


@pytest.mark.parametrize("name", ["recap.yml", "collect.yml", "watchdog.yml"])
def test_failure_alert_is_the_last_step(name):
    """The alert runs last, so a failed save or clear is reported too."""
    last = steps(text(name))[-1]
    assert "if: failure()" in last and "kit.telegram alerts --failed" in last


def test_command_says_it_failed_last_and_only_if_it_never_replied():
    """Commands reply with their own errors; the workflow only speaks up when setup broke first."""
    last = steps(text("command.yml"))[-1]
    assert "steps.cmd.outcome == 'skipped'" in last and "--failed" in last


def test_concurrency_groups():
    """Data-committing jobs queue in one group; commands have none so they never drop each other."""
    for name in ("recap.yml", "collect.yml"):
        assert re.search(r"concurrency:\s+group: kit-data\s+cancel-in-progress: false", text(name))
    assert not re.search(r"^concurrency:", text("command.yml"), re.M)


def test_least_permissions():
    """Write access only where a commit is pushed; read-only actions access only where runs are read."""
    for name in ALL:
        src = text(name)
        assert re.search(r"^permissions:", src, re.M), name
        writes = "contents: write" in src
        assert writes == (name in ("recap.yml", "collect.yml", "command.yml")), name
        assert ("actions: read" in src) == (name in ("command.yml", "watchdog.yml")), name


def test_watchdog_backstop_schedule():
    """GitHub's own cron is the backstop that notices the Worker's clock stopped."""
    src = text("watchdog.yml")
    assert 'cron: "0 20 * * *"' in src and "'backstop'" in src


def test_ci_runs_both_test_suites():
    """CI runs pytest and the Worker tests (Node 22 needs the file glob)."""
    src = text("test.yml")
    assert "pytest -q" in src and "node --test worker/test/*.test.js" in src


@pytest.mark.parametrize("name", ALL)
def test_claude_cli_install_is_pinned(name):
    """An unpinned `npm install -g @anthropic-ai/claude-code` lets a new release break the recap overnight."""
    for line in text(name).splitlines():
        if "@anthropic-ai/claude-code" in line and "npm install" in line:
            assert re.search(r"@anthropic-ai/claude-code@\d+\.\d+\.\d+", line), line
