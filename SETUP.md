# SETUP.md — the setup runbook (written for Claude)

You are Claude, helping one person set up garmin-life-kit for themselves. They gave you this
repo's link and said "set this up for me". Most people doing this are **not technical**. Follow
these steps in order. Each step says what to **tell the person**, what **you run**, how to
**verify**, and what to do **if it fails**.

Template repo: `coreywessel/garmin-life-kit` (called TEMPLATE below).

## How to talk to the person

- One action at a time. Wait for them to say it's done before the next one.
- Give the exact click path: "Tap **Settings** (bottom right) → **Privacy** → ...". Name buttons
  in bold, exactly as they appear.
- Plain words. No jargon unless they need it to find a button. Never say "just".
- Before you start, tell them the whole thing takes about 45 minutes, has 13 steps, and they
  can stop at any point and pick up later (you'll know where they left off).
- Tell them what you're doing while you run commands, in one short line.
- If something on their screen doesn't match what you described, ask them to tell you what
  they see (or send a screenshot) and adapt. App layouts change.

## Secrets: the one rule that matters most

The person will make four secret values: a Telegram bot token, a Claude token, a Garmin API
key and a GitHub token. **They must never paste a secret into this chat.** Chat text is stored
in transcripts. Each secret goes in through a hidden prompt that they run in their own terminal
window:

```
uv run setup/kit.py secret NAME
```

The first time, show them how to open a terminal **in the kit folder** (step 4 tells you the
folder):

- Mac: press **Cmd + Space**, type **Terminal**, press **Return**. Then paste
  `cd ~/garmin-life-kit` and press **Return**.
- Windows: press the **Windows key**, type **PowerShell**, press **Enter**. Then paste
  `cd $HOME\garmin-life-kit` and press **Enter**.

Tell them: "Paste this line and press Return. It asks for the value. Paste it and press Return.
Nothing shows while you paste. That's normal." Keep that window open for the later secrets.

If they paste a secret into the chat anyway: tell them it's OK, finish setting it up with the
hidden prompt, and at the end of setup help them replace that one with a fresh value (each
step's "If it fails" says how to make a new one).

## Resuming

If this is not the first session, or you are unsure where things stand: `cd` into the kit
folder (step 4) and run `uv run setup/kit.py status`. It lists what's done. Skip finished
steps and say so in one line.

## What every setup command prints

Every `uv run setup/kit.py ...` command ends with one line that starts with `OK:` or
`FAILED:`, written so you can pass it on in plain words. Exit code 0 = OK, 1 = FAILED. Never
show the person raw stack traces; read them yourself and explain.

---

## Step 1 — Claude Pro and Claude Code

**Tell the person:** "First, a quick check that your Claude plan works for this."
Ask: "Are you on Claude **Pro** (or Max)? You can check at claude.ai → your name (bottom left)
→ **Settings** → **Billing**." The free plan can't make the token the kit needs.

**You run:** `claude --version`

**Verify:** it prints a version. (They're talking to you, so Claude Code itself works. This
checks the `claude` command exists in the terminal, which step 9 needs.)

**If it fails** (command not found, common with the desktop app): install the command line
version.
- Mac / Linux: `curl -fsSL https://claude.ai/install.sh | bash`
- Windows (PowerShell): `irm https://claude.ai/install.ps1 | iex`

Then run `claude --version` again. If the shell still can't find it, open a new terminal (the
installer adds it to PATH for new windows) or use the full path the installer printed.

## Step 2 — Install the tools

The kit needs: **git**, **gh** (GitHub's command line tool), **Node.js** 18+ (for Cloudflare's
`wrangler`), and **uv** (runs Python, installs it if needed).

**You run** first: `git --version; gh --version; node --version; uv --version` and install only
what's missing.

**Mac:**
1. Check for Homebrew: `brew --version`.
2. If Homebrew is missing, **tell the person:** "I need to install Homebrew, a standard tool
   installer for Mac. In your Terminal window, paste this line, press Return, and type your Mac
   password when it asks (nothing shows while you type). It takes a few minutes. Tell me when it
   says Installation successful."
   ```
   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
   ```
   At the end Homebrew prints two or three "Next steps" lines starting with `echo` / `eval`.
   Ask them to paste those too (they add brew to PATH). It also installs git.
   If a window pops up asking to install "command line developer tools", tell them to click
   **Install** and wait for it to finish.
3. **You run:** `brew install gh node uv`

**Windows:**
- **You run:** `winget install --id Git.Git -e --source winget; winget install --id GitHub.cli -e --source winget; winget install --id OpenJS.NodeJS.LTS -e --source winget; winget install --id astral-sh.uv -e --source winget`
- After it finishes, the new commands are only on PATH in new windows. Ask the person to
  restart the Claude app (or open a new PowerShell for their own window).

**Verify:** `git --version; gh --version; node --version; uv --version` all print versions,
Node 18 or newer.

**If it fails:** read the error. Common fixes: Homebrew not on PATH (run the "Next steps" lines
from its install output); winget missing on older Windows (ask them to install **App
Installer** from the Microsoft Store); corporate laptops can block installs (ask whether this is
a work computer; if so, a personal computer is the easier path).

## Step 3 — GitHub account and login

GitHub is where the kit's code lives and where it runs on a schedule. It's free.

**Tell the person** (skip the account part if they already have one):
1. "Open **github.com/signup** in your browser."
2. "Enter your email, make a password, pick a username. Remember the username."
3. "GitHub sends a code to your email. Enter it."
4. "If it asks about plans, pick **Free**. Skip the questions about how you'll use it."

**You run:** `gh auth status`. If already logged in to the right account, skip to verify.
Otherwise run, in the background so you can read its output while it waits:

```
gh auth login --hostname github.com --git-protocol https --web
```

It prints a one-time code like `ABCD-1234` and waits. **Tell the person:** "Open
**github.com/login/device**, sign in, type this code: **ABCD-1234**, then click **Continue** and
**Authorize github**." If gh is waiting for you to press Enter, send a newline to it, or kill it
and use the fallback below.

**Verify:** `gh auth status` shows "Logged in to github.com account <username>". Then run
`gh auth setup-git`.

**If it fails:** fallback: ask the person to paste `gh auth login --hostname github.com
--git-protocol https --web` into their own terminal window and follow its prompts (press
**Return** to open the browser, then the same code steps). A code only works once; if it timed
out, start over for a fresh one.

## Step 4 — Make their own copy of the kit

**You run** (replace USERNAME with their GitHub username from `gh api user -q .login`):

```
cd ~ && gh repo create USERNAME/garmin-life-kit --template coreywessel/garmin-life-kit --private --clone
cd ~/garmin-life-kit
```

On Windows the folder is `$HOME\garmin-life-kit`. **Private matters:** their copy stores their
Log notes. Never make it public.

If `~/garmin-life-kit` already exists from an earlier try, `cd` into it and run
`git remote get-url origin` to check it is their copy, then carry on.

Then:

```
uv run setup/kit.py check
uv run setup/kit.py dry-run
```

**Tell the person:** "I made your own private copy of the kit on your GitHub account and
checked that it runs on your computer."

**Verify:** `check` ends with `OK:`. `dry-run` ends with `OK:` and printed a sample morning
recap, evening recap, activity card and command answers made from made-up data. You may show
them the sample morning recap so they see what's coming.

**If it fails:** `gh repo create` "Could not resolve to a Repository": TEMPLATE name wrong or
GitHub hiccup; retry once, then check `gh repo view coreywessel/garmin-life-kit`. "Name already
exists": they already made it; `gh repo clone USERNAME/garmin-life-kit ~/garmin-life-kit`.
`check` FAILED: install what it names (step 2). `dry-run` FAILED: a code problem, not theirs.
Read the error, fix it in their copy if it's clearly a bug, and tell them it's not something
they did.

## Step 5 — Their profile

Ask these one or two at a time, in plain words:
1. "What first name should the recaps use?"
2. "What city do you live in? It's for the weather line and your time zone."
3. "Miles or kilometers?"
4. "When do you want the morning recap? Most people pick a bit after they usually wake up,
   so the watch has synced last night's sleep. And the evening one?" (Defaults 7:30 and 9:00 pm.)
5. Optional: "Do you have a bedtime you aim for? If so the morning recap shows how far from it
   you went to bed. Totally fine to skip."
6. Optional: "Anything about your life that would help the recap make sense of your numbers?
   For example night shifts, a newborn, you snowboard on weekends." Keep their words.

Work out the IANA time zone from the city (e.g. Denver → `America/Denver`) and confirm it with
them in plain words ("Mountain Time, right?").

**You run:**

```
uv run setup/kit.py profile --name "NAME" --timezone "America/Denver" --units imperial --city "Denver, CO" --morning 07:30 --evening 21:00
```

`--units` is `imperial` or `metric`. Then edit `profile.toml` by hand for the optional
`sleep_target_bed` and `about` answers (keep the comments).

**Verify:** the command ends with `OK:` and shows the place it found. Read `profile.toml` and
check the lat/lon looks like the right city.

**If it fails:** the city lookup found nothing or the wrong place: try "City, State" or
"City, Country", or set `lat` and `lon` in `profile.toml` yourself.

## Step 6 — Telegram: the bot and the group

They already have Telegram on their phone. This step is all taps on the phone, then one
secret and one command. Do it in this order. The group must have Topics on and the bot must be
an admin **before** the setup command runs.

### 6a. Make the bot

**Tell the person,** one message at a time:
1. "In Telegram, tap the search bar and search for **BotFather** (it has a blue check mark).
   Open it and tap **Start**."
2. "Send it this message: `/newbot`"
3. "It asks for a name. Send something like **Sam's Garmin** (anything you like)."
4. "It asks for a username. It must end in **bot** and be unique, like **sam_garmin_life_bot**.
   If it's taken, try another."
5. "BotFather replies with a long token that looks like `123456789:AAH...`. **Don't send it to
   me.** You need it on your computer. Easiest way: open **web.telegram.org** on the computer,
   log in by scanning the QR code with your phone (Telegram → **Settings** → **Devices** →
   **Link Desktop Device**), open the BotFather chat there and copy the token. (iPhone + Mac
   with the same Apple ID: you can also copy on the phone and paste on the Mac.)"
6. "In your terminal window, paste this line, press Return, then paste the token and press
   Return:"
   ```
   uv run setup/kit.py secret TELEGRAM_BOT_TOKEN
   ```

**Verify:** they see a line starting `OK:`. You can confirm with `uv run setup/kit.py status`
(TELEGRAM_BOT_TOKEN: yes).

**If it fails:** `FAILED: that doesn't look like a bot token`: they copied part of it, or the
whole BotFather message. Ask them to copy just the token and run the line again.

### 6b. Make the group, turn on Topics, add the bot as admin

**Tell the person:**
1. "Back in Telegram, go to **Chats**, tap the **new message** icon (pencil, top right), then
   **New Group**."
2. "Search for your bot's username (the one ending in bot), tap it, then **Next**."
3. "Name the group anything you like, for example **Life**, and tap **Create**."
4. "Open the group, tap its name at the top, then **Edit**. Turn on **Topics**. Tap **Done**
   or **Save**."
5. "Still in the group info, tap **Edit** → **Administrators** → **Add Admin**, pick your bot.
   Make sure **Manage Topics** is switched on. Tap **Done** / **Save**."
6. "Now go back to the group and send any message, like **hi**."

On Android the words are close: the pencil icon is bottom right, and Edit is a pencil icon in
group info.

**You run:**

```
uv run setup/kit.py telegram
```

It finds the group and the person, creates the **Today**, **Fitness**, **Log** and **Alerts**
topics, posts a hello in each, and saves the ids in `setup.json`.

**Verify:** ends with `OK:`. **Ask the person:** "Do you see four new topics in the group, each
with a hello from the bot?"

**If it fails** (the FAILED line says which):
- "found no group": the hi message wasn't seen. Check the bot is in the group and an admin, ask
  them to send another message, run again.
- "found several groups": run again with the `--chat-id` it lists for the right title.
- "topics are off" / "bot is not an admin with Manage Topics": redo 6b step 4 or 5, send
  another message, run again.
- Changing a group to Topics can change its id. If the person turned Topics on after sending
  hi, ask them to send a new message and run again.

## Step 7 — Garmin API key

**Tell the person:**
1. "On your computer, open **connect.garmin.com/app/settings/personalApiToken** and sign in
   with your Garmin account."
2. "Click to create a new API key. If it asks how long it should last, pick the longest option.
   Tell me how many days that was."
3. "Garmin shows the key **once**. Copy it. Don't send it to me."
4. "In your terminal window, paste this line, press Return, then paste the key and press
   Return:"
   ```
   uv run setup/kit.py secret GARMIN_PERSONAL_API_KEY
   ```

**You run** (N = the number of days they told you):

```
uv run setup/kit.py garmin --days N
```

It swaps the key for a token, pulls the last 3 days and shows what came back: sleep, HRV,
resting heart rate, activities.

**Verify:** ends with `OK:`. Tell them in a line what it found ("I can see last night's sleep,
your HRV, and 4 activities from the last two weeks").

**If it fails:**
- "key rejected": wrong or partial copy, or deleted. Make a new key (same page) and redo 7.4.
- "rate limited": Garmin limits how often a key can be swapped. Wait 30-60 minutes, then run
  `garmin` again. Don't retry in a loop.
- Sleep or HRV "missing": the watch may not have synced, or that watch doesn't record it. Ask
  them to open the Garmin Connect app on their phone so it syncs, then run again. Missing data
  is fine; the recap says "no data" for it.
- The page doesn't exist or has no API key option: tell them, and stop here. The kit needs
  this key. (Garmin decides who gets it; this is the one thing you can't work around.)

## Step 8 — Cloudflare account and login

Cloudflare runs a tiny always-on program (a "Worker") that keeps the clock and answers
Telegram. Free.

**Tell the person** (skip if they have an account):
1. "Open **dash.cloudflare.com/sign-up**, enter your email and a password, and verify your
   email."
2. "If it asks what you want to do or offers plans, pick **Free** and skip anything else."
3. "Open **Workers & Pages** in the left menu once. If it asks you to pick a subdomain, choose
   any name and confirm."

**You run** (it opens their browser; run it in the background and wait):

```
npx --yes wrangler@4 login
```

**Tell the person:** "A Cloudflare page opened in your browser. Click **Allow**."

**Verify:** `npx --yes wrangler@4 whoami` shows their email / account.

**If it fails:** no browser opened: the command prints a URL; give it to them. "Allow" page
says error: run `login` again. Several accounts listed: ask which one, then add
`--account-id ID` (the id `whoami` shows) to the `worker` command in step 10.

## Step 9 — Secrets: the Claude token and the GitHub token

### 9a. Claude token

This lets the kit write recaps using their Claude subscription. It lasts a year; the kit warns
them in Alerts before it runs out.

**Tell the person:**
1. "In your terminal window, paste `claude setup-token` and press Return. A browser page opens.
   Click **Authorize**."
2. "The terminal then shows a long token starting with `sk-ant-oat`. Copy it. Don't send it to
   me." (Mac: select it and press **Cmd + C**. Windows: select it and right-click, or
   **Ctrl + Shift + C**.)
3. "Now paste this line, press Return, paste the token, press Return:"
   ```
   uv run setup/kit.py secret CLAUDE_CODE_OAUTH_TOKEN
   ```

**If it fails:** `setup-token` says the plan doesn't support it: they're not on Pro/Max (step
1). Format FAILED: copy the whole token (it can wrap onto two lines; copy all of it).

### 9b. GitHub token for the Worker

This lets the Cloudflare Worker start the kit's jobs in their GitHub copy, and nothing else.

**Tell the person:**
1. "Open this link. It fills in most of the form for you:"
   `https://github.com/settings/personal-access-tokens/new?name=garmin-life-kit-worker&description=Lets+the+Cloudflare+Worker+start+this+kit%27s+jobs&expires_in=none&actions=write&contents=read`
2. "Under **Repository access**, choose **Only select repositories**, then pick
   **garmin-life-kit**."
3. "Scroll down and check the permissions say **Actions: Read and write** and **Contents:
   Read-only**. (Metadata: Read-only is added by itself.)"
4. "Click **Generate token**, then **Generate token** again to confirm. Copy the token that
   starts with `github_pat_`. Don't send it to me."
5. "Paste this line, press Return, paste the token, press Return:"
   ```
   uv run setup/kit.py secret WORKER_GITHUB_TOKEN
   ```
   If they had to pick an expiry date instead of **No expiration**, add `--days N` (N = days
   until it expires) so Alerts warns them before it runs out.

If the link doesn't pre-fill, the click path is: **github.com** → profile picture (top right)
→ **Settings** → **Developer settings** (bottom of the left menu) → **Personal access tokens** →
**Fine-grained tokens** → **Generate new token**. Name it `garmin-life-kit-worker`, Expiration
**No expiration** (or the longest offered), then steps 2-5.

### 9c. Push the secrets

**You run:**

```
uv run setup/kit.py secrets
```

It makes the kit's own random secrets (three separate ones: Telegram → Worker, GitHub →
Worker, and a spare for an optional iPhone Shortcut), notes when each pasted key expires, and
copies everything the GitHub jobs need into their repo's encrypted secrets.

**Verify:** ends with `OK:`. `gh secret list` shows TELEGRAM_BOT_TOKEN,
CLAUDE_CODE_OAUTH_TOKEN, GARMIN_PERSONAL_API_KEY, GARMIN_CACHE_KEY, WORKER_API_SECRET
(WORKER_URL arrives in step 10).

**If it fails:** "missing: X": that secret wasn't entered; redo its step. `gh` permission
error: `gh auth status`, then `gh auth refresh -s repo`.

## Step 10 — Deploy

**Tell the person:** "Now I'm putting the always-on part live on Cloudflare and connecting it
to your bot. About a minute."

**You run:**

```
uv run setup/kit.py worker
```

It writes `worker/wrangler.toml` from their profile, pushes their profile and setup files to
their GitHub copy (so scheduled jobs never run on the blank template), makes the Worker's
storage, gives the Worker its secrets, deploys it, checks it answers, saves its address as a GitHub secret, points
Telegram at it, and adds the command menu to the group.

**Verify:** ends with `OK:` and the Worker's address. **Ask the person:** "In the group, type
`/help`. Does the bot answer with a list of commands?"

**If it fails:**
- "no workers.dev subdomain": step 8.3 wasn't done. Ask them to do it, then run `worker` again.
- "not logged in": step 8 login again.
- "more than one account": run `npx --yes wrangler@4 whoami`, ask which account is theirs,
  then `uv run setup/kit.py worker --account-id ID`.
- `/help` gets no answer: run `npx --yes wrangler@4 tail` in the background, ask them to send
  `/help` again, read the log. A 401 means the webhook secret didn't match: run `worker` again
  (it resets the webhook). Nothing logged at all: run `worker` again.
- Free plan cron limit reached ("too many cron triggers"): their account allows 5 scheduled
  Workers. Only possible if they already use Cloudflare. Ask before removing anything.

## Step 11 — First real recap

**You run:**

```
uv run setup/kit.py test
```

It commits their profile and setup files, starts a morning recap on GitHub, waits for it
(2-4 minutes) and prints the result.

**Tell the person:** "I've started your first real recap. It takes a few minutes. It will show
up in the **Today** topic."

**Verify:** ends with `OK:`, and **ask the person** whether the recap arrived in Today. Then
ask them to try two things:
1. Type `/today` in the group. An answer should arrive in a minute or two.
2. Type a note in **Log**, like "slept badly, neighbor's dog". It should get a 👍 within a
   few seconds.

**If it fails:** the FAILED line gives the run's URL. Read the log with
`gh run view --log-failed`. Common causes: a secret pasted with a missing piece (redo that
secret, then `uv run setup/kit.py secrets`), Claude token from a plan without access (step 1),
Garmin rate limit (the recap still goes out, without Garmin numbers; Alerts explains). A
`/today` that never answers: check `gh run list --workflow command.yml`; no run means the
Worker couldn't start it (GitHub token, step 9b: wrong repo or missing Actions write).

## Step 12 — Optional: ask Claude about your Garmin data

Ask: "Want me to connect Claude straight to your Garmin data, so you can ask questions like
'how did I sleep this week'? Takes one minute."

**You run** (if yes):

```
claude mcp add garmin --scope user -- uvx --from git+https://github.com/coreywessel/garmin-life-kit garmin-life-mcp
```

It reads the Garmin key from the local secrets file made in step 7, so no key goes in Claude's
settings. **Tell the person** to restart the Claude app (or start a new chat), then ask
something like "how did I sleep last night?"

**Verify:** `claude mcp list` shows `garmin` as connected.

**If it fails:** "no Garmin key": the local secrets file is missing on this computer; run
`uv run setup/kit.py secret GARMIN_PERSONAL_API_KEY` from the kit folder. Rate limited: wait
an hour.

## Step 13 — Wrap up

**Tell the person,** in a short message:
- When each recap arrives (their times), and that activity cards show up within about two hours
  of syncing a workout.
- The commands: `/today`, `/week`, `/last`, `/status`, `/help`.
- Alerts stays quiet unless something breaks, plus a short all-clear every Sunday morning.
- To change anything later, open Claude Code in the `garmin-life-kit` folder and ask in plain
  words.
- They can close this chat.

Then run `uv run setup/kit.py status` one last time and make sure nothing is left undone.

---

## Setting it up for someone else on your computer

Possible, but every account must be **theirs**: their GitHub (log `gh` in as them, or use
`gh auth switch`), their Cloudflare (`wrangler logout`, then `login` as them), their Telegram
bot and group, their Garmin key, and a Claude token from **their** Claude Pro plan. The local
secrets file is named after their GitHub account and repo, so it won't mix with your own. When done, delete their secrets
file from `~/.config/garmin-life-kit/` and log `gh` and `wrangler` back into your accounts.
