# garmin-life-kit — architecture and build contract

This is the contract every part of the kit is built against. If code and this file disagree,
fix one of them in the same change.

## What it is

A public GitHub **template**. A person gives the link to Claude Code, says "set this up for me",
and ends up with:

- a **private** copy of this repo on their own GitHub account (private because it stores their
  Log notes and receipts in `data/`),
- a Telegram group with four topics: **Today**, **Fitness**, **Log**, **Alerts**,
- a morning recap and an evening recap from their Garmin data, written by Claude through
  their own Claude Pro subscription, **report only, never coaching**,
- an activity card in Fitness each time a new Garmin activity appears,
- `/today`, `/week`, `/last`, `/status`, `/help` commands,
- a watchdog that posts in Alerts when anything stops working,
- optionally, a Garmin MCP server so they can ask Claude about their data directly.

All free except Claude Pro.

## The hard rule

Recaps and cards **report, never coach**. They state numbers against the person's own
averages and targets. They never tell the person to change, skip, add or cut training, never
suggest rest, never label risk, never give health advice. Prompts say this; the fallback text
built in code obeys it too.

## Moving parts

```
 Cloudflare Worker (free)                     GitHub Actions (free, private repo)
 ───────────────────────                      ────────────────────────────────────
 cron */5 ─ local-time schedule ─dispatch──►  recap.yml    morning / evening
                                              collect.yml  Log notes + new activity cards
 POST /telegram (webhook) ──────dispatch──►  command.yml  /today /week /last /status
   notes → KV + 👍                             watchdog.yml (also a daily GitHub-cron backstop)
 GET /log, POST /log/ack  ◄──── Actions ────  kit/log.py
 GET /health              ◄──── watchdog
 POST /wake (optional Shortcut) ─dispatch──►  recap.yml morning
```

Why a Worker keeps the clock: GitHub's own `schedule:` ran up to 8 hours late. The Worker ticks
every 5 minutes and dispatches each job at its **local** time, so times stay fixed through
daylight saving.

Claude runs as `claude -p` inside Actions, authenticated with `CLAUDE_CODE_OAUTH_TOKEN`
(made by `claude setup-token`, works on Pro, lasts 1 year).

Garmin data comes from the **Garmin Personal API** (`personal.connectapi.garmin.com`). The
person makes an API key at `connect.garmin.com/app/settings/personalApiToken`. The key is
swapped for a 24h JWT. The JWT is cached (encrypted) in `data/garmin-token.enc` and reused
while it has more than 1 hour left, because Garmin answers 429 after about a dozen exchanges in
a burst. The Personal API has **no** training readiness, training status, lap splits or PR list.
v1 does not use them. (The old `garminconnect` session is not part of v1; see "Later".)

## Files

```
README.md               for humans, non-technical. First line tells them what to say to Claude.
CLAUDE.md               for Claude: what this repo is, rules, where to start.
SETUP.md                for Claude: the step-by-step setup runbook.
profile.toml            THE one file about the person. Human-readable, comments.
setup.json              machine-written by setup (ids, repo, worker url, expiry dates). No secrets.
pyproject.toml          package `kit`, deps: cryptography, mcp. Script: garmin-life-mcp.
requirements.txt        pinned runtime deps for Actions (pip install -r).
kit/                    Python runtime (stdlib + cryptography; mcp only in mcp_server.py)
prompts/morning.md      Claude instructions, generic, report-only
prompts/evening.md
worker/src/index.js     Cloudflare Worker, no npm deps
worker/wrangler.toml.template   rendered to worker/wrangler.toml by setup
worker/test/*.test.js   node --test, no deps
setup/kit.py            cross-platform setup CLI (stdlib only; imports kit.garmin for the key check)
tools/push.sh           Actions: rebase + push data commit, .enc conflict rule
.github/workflows/      recap.yml collect.yml command.yml watchdog.yml test.yml
tests/                  pytest; tests/fixtures/ synthetic Garmin data (invented, never real)
data/.gitkeep           runtime state lives here (committed by Actions)
```

## profile.toml (exact keys)

```toml
# About you. The only file with personal settings in it.
# Ask Claude to change anything here. After changing [times], Claude redeploys the Worker.

name = ""                     # first name the recaps use. Required.
timezone = "America/Denver"   # IANA time zone name. Required.
units = "imperial"            # "imperial" (mi, ft, °F, mph) or "metric" (km, m, °C, km/h)
week_starts = "monday"        # "monday" or "sunday" (for /week and week totals)

[location]                    # weather line. City-level is plenty.
label = ""                    # e.g. "Denver, CO"
lat = 0.0
lon = 0.0

[times]                       # local, 24h "HH:MM", multiples of 5 minutes
morning = "07:30"
evening = "21:00"

[features]                    # true / false
morning_recap = true
evening_recap = true
activity_cards = true
weather = true
log_notes = true              # text typed in the group is saved and read by the recaps
watchdog = true

[recap]
sleep_target_bed = ""         # optional "22:30": morning says how far from it you went to bed
about = """
"""                           # optional plain sentences that help the recap read your data
                              # ("I snowboard most weekends", "night shifts on Tuesdays")
```

Validation (`kit.profile.load()`), each failure a one-line message saying the key and the fix:
name non-empty; timezone loads with `zoneinfo`; units in the two values; times match `HH:MM`
with minute % 5 == 0; lat in [-90, 90], lon in [-180, 180]; sleep_target_bed empty or `HH:MM`.
Weather is skipped (not an error) when lat == lon == 0.

## setup.json (machine-written; template ships without it)

```json
{
  "repo": "owner/name",
  "telegram": {"chat_id": -100..., "owner_id": 123, "bot_username": "x_bot",
               "topics": {"today": 2, "fitness": 3, "log": 4, "alerts": 5}},
  "worker": {"name": "garmin-life-kit", "url": "https://....workers.dev", "kv_id": "..."},
  "expiries": [
    {"name": "Claude token (CLAUDE_CODE_OAUTH_TOKEN)", "expires": "YYYY-MM-DD",
     "breaks": "...", "fix": "..."}
  ]
}
```
`kit.profile.setup()` returns {} when the file is missing. Runtime code that needs telegram ids
fails with "setup.json has no telegram ids yet - run setup step 6".

## Secrets (three separate shared secrets, never reused)

Local copy for setup only: `~/.config/garmin-life-kit/<owner>__<repo>.env` (so two people on one computer never share a file), mode 600, KEY=value
lines. Written only by `setup/kit.py secret` (hidden prompt) or generated by `setup/kit.py
secrets`. Never printed, never committed, never pasted into chat.

| Name | Where | Made by |
|---|---|---|
| TELEGRAM_BOT_TOKEN | Actions + Worker | BotFather, pasted by human |
| CLAUDE_CODE_OAUTH_TOKEN | Actions | `claude setup-token`, pasted by human |
| GARMIN_PERSONAL_API_KEY | Actions | Garmin Connect, pasted by human |
| WORKER_GITHUB_TOKEN | Worker (as GITHUB_TOKEN) | fine-grained PAT: this repo only, Actions RW + Contents R, pasted by human |
| GARMIN_CACHE_KEY | Actions | generated (Fernet key) |
| WORKER_API_SECRET | Actions + Worker | generated (hex 32): Actions ↔ Worker /log |
| TELEGRAM_WEBHOOK_SECRET | Worker | generated (hex 32): Telegram → Worker |
| SHORTCUT_SECRET | Worker | generated (hex 32): optional iPhone Shortcut → /wake |
| WORKER_URL | Actions | from deploy |

## Worker contract (`worker/src/index.js`)

`[vars]` in wrangler.toml (rendered from profile + setup.json): `REPO`, `TIMEZONE`,
`MORNING` ("HH:MM"), `EVENING`, `CHAT_ID`, `OWNER_ID`, `ALERTS_THREAD`, `LOG_THREAD`,
`FEATURES` (JSON object of the profile toggles). KV binding `KIT`. Secrets as above.

Schedule (pure function `dueJobs(date, env)`, exported for tests). Local HH:MM in TIMEZONE:
- `MORNING` → `recap.yml {mode: "morning", source: "schedule"}` (if morning_recap)
- `EVENING` → `recap.yml {mode: "evening", source: "schedule"}` (if evening_recap)
- `MORNING`+45 min → `watchdog.yml {slot: "morning"}`; `EVENING`+45 → `{slot: "evening"}` (if watchdog)
- every 2 hours at :20 from the morning hour through the evening hour → `collect.yml {}`
  (if log_notes or activity_cards). If the evening hour is earlier than the morning hour, wrap.

`scheduled()`: dispatch each due job; on status 0 or ≥500 wait 20s and retry once; on final
failure post to ALERTS_THREAD. `dispatch()` never throws (network error → status 0).

Routes:
- `POST /telegram` — header `X-Telegram-Bot-Api-Secret-Token` must equal
  TELEGRAM_WEBHOOK_SECRET (constant-time). Then require `chat.id == CHAT_ID`,
  `from.id == OWNER_ID` (**required**; unset OWNER_ID = nobody), not a bot, has text. Reply 200
  at once, work in `ctx.waitUntil`.
  - `/help` or `/start` → answered by the Worker (command list).
  - `/today /week /last /status` → dispatch `command.yml {command, thread_id}`; reply in the
    same thread only if dispatch failed ("Couldn't start /x: GitHub said ...").
  - unknown `/x` → "Unknown command /x. Send /help."
  - other text, if log_notes → KV `log:<date>:<message_id>` = {ts, thread, text, message_id},
    then 👍 reaction. If the KV put fails, reply "Couldn't save that note" (no 👍).
- `GET /log` (`X-Worker-Secret` == WORKER_API_SECRET) → up to 200 notes with their keys,
  sorted, `more` flag. **Deletes nothing.**
- `POST /log/ack` (same secret) → body `{keys: [...]}`, deletes only keys starting `log:`.
- `GET /health` → `{ok, log_pending}` (public, counts only).
- `POST /wake` (`X-Shortcut-Secret` == SHORTCUT_SECRET; 404 if the secret is unset) → once
  per local day, after 04:00 local, dispatch `recap.yml {mode: "morning", source: "wake"}`.
- anything else → 404.

## Actions contract

All workflows: `timeout-minutes` set (recap/collect/command 20, watchdog 10),
`actions/checkout@v4` with `ref: main`, Python 3.12 with pip cache, `pip install -r requirements.txt`.
Inputs reach scripts only through `env:`, never `${{ }}` pasted into `run:` text.
Least permissions per job (`contents: write` only where a commit is pushed, `actions: read` for
watchdog and /status).

**recap.yml** (`workflow_dispatch` inputs mode, source; `concurrency: {group: kit-data,
cancel-in-progress: false}`):
1. install deps + `npm install -g @anthropic-ai/claude-code`
2. `python -m kit.recap "$MODE" --source "$SOURCE"` — does guard → collect log (non-fatal) →
   fetch Garmin (non-fatal, alert with the specific reason) → expiry warnings (morning) →
   Claude → send → receipt. Exit 0 with "already sent" when guarded.
3. `Save` (`if: always()`): `git add data/`, commit if changed, `bash tools/push.sh`.
4. `Clear collected notes` (`if: success()` — i.e. recap AND save both succeeded):
   `python -m kit.log ack`.
5. `Alert on failure` (`if: failure()`, last): post to Alerts with the run URL.

**collect.yml** (no inputs; same concurrency group): `python -m kit.log collect` (if
log_notes), `python -m kit.cards` (if activity_cards), Save, Clear (same condition), Alert.

**command.yml** (inputs command, thread_id; **no** concurrency group, so commands never
drop each other): `python -m kit.commands "$COMMAND" "$THREAD_ID"`; Save (token cache may have
changed); "Say it failed" in the same thread if the run failed before replying.

**watchdog.yml** (inputs slot; plus `schedule: cron "0 20 * * *"` as backstop):
`python -m kit.watchdog "$SLOT"`.

**test.yml** (push/pull_request): `pytest -q` and `node --test worker/test/*.test.js`.

`tools/push.sh` — `git pull --rebase` + push, 3 tries with backoff. If the only conflicts
are `data/*.enc`, keep main's copy (during rebase that is `--ours`), drop the commit if
nothing remains, continue. Any other conflict: abort, `::error::`, exit 1.

## Python runtime (`kit/`)

Run as modules from the repo root (`python -m kit.recap`). Paths from
`kit.profile.ROOT` (repo root). All Telegram output is HTML (`<b>`, `<i>`) with text escaped.

- `profile.py` — `load()` → validated dict; `setup()` → setup.json dict; `tz()`; `today()`;
  `units` helpers: `dist(m)`, `elev(m)`, `speed(mps)`, `pace(mps)`, `temp(c)` returning strings
  in the profile's units.
- `garmin.py` — `Client(key, cache)`; `connect()` builds one from env
  `GARMIN_PERSONAL_API_KEY` + `RepoCache` (Fernet, `data/garmin-token.enc`, key
  `GARMIN_CACHE_KEY`; no key → no caching, still works). `FileCache(path)` (plain JSON, 0600)
  for the MCP server. Errors: `KeyRejected` (400/401/403 on exchange; 401 on data with a
  fresh token), `RateLimited` (429), `ExchangeFailed` (5xx/network/junk), `GarminError`.
  Cached token reused only if it came from the same key (sha256 prefix) and has > 3600 s left;
  a 401 on a cached token → one fresh exchange + retry. Write the cache only on a real change.
  Methods (Personal API paths):
  `activities(start_date, end_date)` (paged 100), `latest_activities(limit)`,
  `sleep(date)` (the record itself, not wrapped), `hrv(date)`, `daily_summary(date)`.
  `DryRunClient` reads `tests/fixtures/garmin/*.json` (used when `KIT_DRY_RUN=1`).
- `fetch.py` — `collect(client, today, days=7)` → `{today, days: {date: {summary, sleep, hrv}},
  activities: [slim...]}`; one failing endpoint records `{"error": "..."}` and moves on.
  Activities trimmed to the fields the code uses (name, typeKey, startTimeLocal, distance,
  duration, averageHR, maxHR, averageSpeed, calories, aerobic/anaerobicTrainingEffect,
  activityTrainingLoad, elevationGain, elevationLoss, maxSpeed, hrTimeInZone_1..5, lapCount,
  activityId).
- `numbers.py` — every number the recap shows, computed in code. Morning: last night's sleep
  (bed, wake, hours, score), 7-night average hours and bed time, distance from
  `sleep_target_bed` (correct across midnight), HRV last night vs 7-day avg + Garmin status and
  baseline, resting HR vs 7-day avg, Body Battery at wake / high / low yesterday, stress avg
  yesterday, steps yesterday, yesterday's activities, week-to-date totals (count, distance per
  sport, time), weather today, `plan: None` (hook). Evening: today's activities, steps so far,
  week-to-date, weather tomorrow, sleep target. Snowboarding (`resort_snowboarding`):
  vertical = elevationLoss, top speed = maxSpeed, runs only parsed from the auto name if present.
  Missing data → key absent, never a guessed number.
- `weather.py` — Open-Meteo (no key), daily high/low/precip/WMO code → words; units-aware;
  any failure → None.
- `claude.py` — `ask(system, input)` → text: `claude -p --tools "" --no-session-persistence
  --system-prompt <system>`, input on stdin, timeout 600, 2 attempts. Raises `ClaudeFailed`.
  In dry run returns a deterministic stub unless `KIT_DRY_CLAUDE=1`.
- `recap.py` — CLI `morning|evening [--source schedule|manual|wake]`. Guard: `data/receipts.json`
  has today's date for the mode and source != manual → print and exit 0. On Claude failure:
  send the code-built fallback (same numbers, plain labels) **and** alert "Claude didn't write
  the recap (most likely the Claude token) ...". Receipt written only after Telegram confirms.
- `cards.py` — new activities since last run → one card each in Fitness (silent). First run
  seeds `data/seen-activities.json` without posting. Seen list saved **after** a successful send.
- `commands.py` — `today` (activities + steps + last night), `week` (week-to-date per sport),
  `last` (card for the newest activity), `status` (last run per workflow via GitHub API,
  receipts, worker health, expiries). Replies in the given thread; errors replied there too.
- `telegram.py` — `send(topic_or_thread, html, silent=False)`: split at 4096 on blank lines,
  `parse_mode=HTML`, on "can't parse entities" resend as plain text, check `ok`, never print the
  URL. Dry run prints to stdout instead.
- `log.py` — `collect`: GET /log, append new ones to `data/log.jsonl` (skip message_ids already
  there), write `out/log-ack.json`. `ack`: POST the keys. No Worker configured → no-op.
- `watchdog.py` — slot check of today's receipt, every run in the last 13 h of each workflow
  (a failure followed by a green run still reports), Worker /health; Sunday morning all-clear
  summary; `backstop` slot checks a Worker-started watchdog ran in the last 26 h.
- `expiry.py` — warnings 30/14/7 days out and daily from 3 days, from setup.json expiries.
- `mcp_server.py` — FastMCP stdio server `garmin-life-mcp`. Read-only tools: `get_sleep(date)`,
  `get_hrv(date)`, `get_daily_summary(date)`, `list_activities(start, end)`,
  `weekly_rollup(week_start)`. Key from env `GARMIN_PERSONAL_API_KEY`, else from
  `~/.config/garmin-life-kit/*.env`. Token cache `FileCache` in the user cache dir.
  Dates default to today in the local zone. Returns trimmed JSON, never raw per-minute arrays.

## Dry run

`KIT_DRY_RUN=1`: Garmin from fixtures, Telegram to stdout, Claude stubbed (or real with
`KIT_DRY_CLAUDE=1`), Worker calls skipped, no git. `uv run setup/kit.py dry-run` runs morning,
evening, a card and every command against a temporary profile and prints each message. Works
with no accounts at all. Tests run the same path with sockets blocked.

## Later (hooks only, not built in v1)

- Training plan: `numbers.py` returns `plan: None`; prompts render a plan line only when
  `context.plan` exists. A plan module fills it.
- iPhone wake Shortcut: `/wake` route exists, gated by SHORTCUT_SECRET.
- Old garminconnect session (readiness, training status, laps, PR list).

## Setup CLI contract (`setup/kit.py`, run as `uv run setup/kit.py <cmd>` from the repo root)

Stdlib only (plus `kit.garmin` for the key check), works on macOS, Windows and Linux. Never
prints a secret value. Every command ends with one line starting `OK:` or `FAILED:` followed by
plain words, so Claude can tell what happened. Exit code 0 / 1 matches. `--dry-run` on any
command fakes every network call and external tool.

| Command | Does | Needs |
|---|---|---|
| `status` | What's done so far: profile filled, each local secret present (yes/no only), setup.json fields, worker deployed. Claude runs this first when resuming. | - |
| `check` | Prerequisites: python ≥3.11, git, gh (+ `gh auth status`), node ≥18 + npx, uv, claude. Prints the fix for each missing one. | - |
| `secret NAME` | Hidden prompt, light format check, writes to the local env file. For the human to run in their own terminal. Names: TELEGRAM_BOT_TOKEN, CLAUDE_CODE_OAUTH_TOKEN, GARMIN_PERSONAL_API_KEY, WORKER_GITHUB_TOKEN. | - |
| `profile --name N --timezone TZ --units U --city "Denver, CO" [--morning HH:MM --evening HH:MM]` | Writes profile.toml (keeps comments; geocodes the city with Open-Meteo's free geocoding API to lat/lon). | - |
| `telegram` | getMe; reads getUpdates to find the one forum supergroup the bot is in and the human who wrote there (owner_id); checks topics are on and the bot is admin with Manage Topics; creates Today/Fitness/Log/Alerts (skips ones already in setup.json); posts a hello in each; writes setup.json. Must run **before** the webhook is set. | TELEGRAM_BOT_TOKEN |
| `garmin --days N` | Exchanges the key, pulls the last 3 days, prints found/missing per data type, records the key's expiry (today + N) in setup.json. | GARMIN_PERSONAL_API_KEY |
| `secrets` | Generates GARMIN_CACHE_KEY, WORKER_API_SECRET, TELEGRAM_WEBHOOK_SECRET, SHORTCUT_SECRET if missing; records the Claude token expiry (today + 365) if not set, and the GitHub token's only with `--github-token-days N` (the PAT link makes a no-expiry token); `secret` re-stamps these when a new value is saved; pushes every Actions secret with `gh secret set --repo <repo>` over stdin. | repo in setup.json |
| `worker` | Renders worker/wrangler.toml; creates the KV namespace if setup.json has no kv_id; `wrangler secret put` each Worker secret over stdin; `wrangler deploy`; saves the URL; checks /health; sets the WORKER_URL Actions secret; `setWebhook` (secret_token, allowed_updates [message]); `setMyCommands` scoped to the chat. Wrangler is `npx --yes wrangler@4`. Re-run any time profile [times] or [features] change. | all above + `wrangler login` |
| `test` | Commits/pushes profile.toml + setup.json + worker/wrangler.toml if changed, then `gh workflow run recap.yml -f mode=morning -f source=manual`, waits for the run, prints its result and URL. | everything |
| `dry-run` | Full offline pipeline, prints every message. | nothing |
