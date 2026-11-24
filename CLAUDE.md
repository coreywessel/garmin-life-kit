# CLAUDE.md — garmin-life-kit

You are working in garmin-life-kit: Garmin data → Claude-written recaps → a private Telegram
group. GitHub Actions does the work, a Cloudflare Worker keeps the clock and answers Telegram,
`claude -p` writes the words on the owner's Claude subscription.

**Setting it up for someone? Read [SETUP.md](SETUP.md) and follow it step by step.** Start with
`uv run setup/kit.py status` if anything may already be done.

How it fits together, file by file: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Rules

1. **Report, never coach.** Recaps, cards and command answers state numbers against the
   person's own averages and targets. Never tell them to change, skip, add or cut training,
   never suggest rest, never label risk, never give health advice. This holds for prompts,
   code-built text and anything you write for them.
2. **Code computes, Claude writes.** Every number is worked out in `kit/numbers.py`. Claude only
   turns numbers into short sentences. Never let a prompt ask Claude to calculate.
3. **One profile file.** Everything about the person lives in `profile.toml`. Machine-found ids
   live in `setup.json`. Nothing personal anywhere else, ever.
4. **Secrets never in chat, never in the repo.** They go in through
   `uv run setup/kit.py secret NAME` in the person's own terminal, live locally in
   `~/.config/garmin-life-kit/<owner>__<repo>.env`, and are copied to GitHub / Cloudflare by setup.
   Never print one, never `cat` the env file, never put one in a commit or command line.
5. **Fail loud.** Anything broken says so in Alerts with the fix in plain words. Silence must
   mean it works.
6. **Ask before** anything that costs money, makes something public, or deletes data.
7. The person's repo is **private** (it holds their Log notes). Never make it public.

## Talking to the person

Most owners are not technical. Short sentences, one action at a time, exact click paths,
buttons in bold. Say what you're doing in one line. No jargon.

## Common requests

| They ask | You do |
|---|---|
| Change recap times | Edit `[times]` in `profile.toml` (HH:MM, multiples of 5), then `uv run setup/kit.py worker` (redeploys the schedule), commit and push. |
| Turn a feature on/off | Edit `[features]`, then `uv run setup/kit.py worker`, commit and push. |
| Metric / imperial, name, city | Edit `profile.toml` (for a new city: `uv run setup/kit.py profile --city "..."` re-geocodes). Commit and push. No redeploy needed unless the time zone changed (then run `worker`). |
| Change how recaps read | Edit `prompts/morning.md` / `prompts/evening.md`. Keep rule 1. Test with `KIT_DRY_RUN=1 KIT_DRY_CLAUDE=1 uv run python -m kit.recap morning`. Edited prompts can clash with a later `update`; then merge by hand, keeping their wording and adding any new lines from the template. |
| Send a recap now | `gh workflow run recap.yml -f mode=morning -f source=manual` |
| "Is it working?" | `uv run setup/kit.py status`, `gh run list --limit 10`, or tell them to send `/status` in the group. |
| A key expired / Alerts says so | The Alerts message has the fix. New value → `uv run setup/kit.py secret NAME` → `uv run setup/kit.py secrets` (and `worker` for WORKER_GITHUB_TOKEN or TELEGRAM_BOT_TOKEN). |
| "Pull the latest updates" / "update the kit" | `uv run setup/kit.py update`. It merges improvements from coreywessel/garmin-life-kit, keeps their `profile.toml`, `setup.json` and `data/`, runs the tests, pushes, and says if `worker` needs re-running. Secrets are never touched. If it FAILS on a conflict, merge by hand, keeping their files. |
| "Drop stress" / hide a number | Set that metric to `false` under `[metrics]` in `profile.toml`, commit and push. |
| "I got a new watch" / something's missing | Update `[devices] wears` (`uv run setup/kit.py profile --wears "..."`), then `uv run setup/kit.py discover --days N` (one key swap; never in a loop). It turns on new data and keeps what they trimmed. Commit and push. |
| Ask about their Garmin data | If the `garmin` MCP server is connected, use it. If not, offer SETUP.md step 12. |
| Add a training plan | Not in v1. The hook: `kit/numbers.py` returns `plan: None`; the prompts show a plan line only when `context.plan` exists. Write a module that fills it, keep rule 1. |

## Alerts → cause → fix

| Alert says | Cause | Fix |
|---|---|---|
| Garmin API key rejected | Key expired or deleted | New key at connect.garmin.com/app/settings/personalApiToken → `secret GARMIN_PERSONAL_API_KEY` → `discover --days N` → `secrets` |
| Garmin rate-limited | Too many key swaps in a short time | Clears within the hour. Recap went out without Garmin numbers. |
| Claude didn't write the recap | Claude token expired/revoked, or plan changed | `claude setup-token` → `secret CLAUDE_CODE_OAUTH_TOKEN` → `secrets`. A numbers-only recap still went out. |
| Couldn't start X on schedule | Worker can't reach GitHub: GitHub token revoked/wrong | New token (SETUP.md 9b) → `secret WORKER_GITHUB_TOKEN` → `worker` |
| No morning recap sent today | A run failed or never started | `gh run list --workflow recap.yml`, read `gh run view <id> --log-failed` |
| Worker not answering | Worker deleted / Cloudflare issue | `uv run setup/kit.py worker` |

## Development

- Tests: `uv run --with pytest --with cryptography --with mcp pytest -q` and
  `node --test worker/test/*.test.js`. CI runs both on every push.
- Dry run with no accounts: `uv run setup/kit.py dry-run`, or any module with `KIT_DRY_RUN=1`.
- Python 3.11+, stdlib + `cryptography`; `mcp` only in `kit/mcp_server.py`. The Worker has no
  npm dependencies. Wrangler is always `npx --yes wrangler@4`.
- Getting updates from the public kit: `uv run setup/kit.py update` (see the table above).
