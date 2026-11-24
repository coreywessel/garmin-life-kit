**Give this link to Claude Code and say: set this up for me.**

# garmin-life-kit

Your Garmin watch, recapped in a private Telegram group every morning and evening.

Claude reads your Garmin data and writes a short recap: how you slept, your HRV and resting
heart rate against your own averages, what you did yesterday, your week so far, and the
weather. Each new workout shows up as a card. You can ask for numbers any time with a command.

It **reports, it never coaches.** No "take a rest day", no training advice, no scores telling
you how to feel. Just your numbers next to your own normal.

## What you get

A Telegram group with four topics:

| Topic | What lands there |
|---|---|
| Today | Morning recap (sleep, recovery, yesterday, week, weather). Evening recap (today, week, tomorrow's weather). |
| Fitness | A card for each new activity: distance, time, pace, heart rate, zones. Snowboard days show vertical, top speed and runs. |
| Log | Type anything here (how you feel, a sore knee, a late night). It gets a 👍 and the recaps take it into account. |
| Alerts | Only when something breaks or a key is about to expire, with the fix. Quiet means it's working. |

Every watch records different things. During setup the kit checks what your Garmin data
actually includes (HRV, sleep stages, Body Battery, VO2 max and so on) and only shows that.
Want less? Say "drop stress" and it's gone.

Commands (type them in the group): `/today`, `/week`, `/last`, `/status`, `/help`.

Optional: connect Claude straight to your Garmin data, so you can ask it things like
"how did I sleep this week compared to last week?"

## What you need

- A Garmin watch that syncs to Garmin Connect
- An iPhone or Android phone with Telegram
- A Mac or Windows computer for the one-time setup (about 45 minutes)
- A Claude Pro plan ($20/month) with Claude Code. Everything else is free.

Claude walks you through making the free accounts it needs (GitHub and Cloudflare) and does
the technical parts itself. You only tap and paste where it asks.

## How to start

1. Get Claude Pro at [claude.ai](https://claude.ai).
2. Install the Claude desktop app from [claude.ai/download](https://claude.ai/download), open it,
   and click the **Code** tab. (Already use Claude Code in a terminal? That works too.)
3. Paste this page's link and say: **set this up for me.**

## Privacy

- Your copy of this kit lives in your own **private** GitHub account. Nobody else can see it.
- Your keys are stored as encrypted secrets in GitHub and Cloudflare, never in the code.
- Only you can use the bot. Messages from anyone else in the group are ignored.
- The recaps are written by Claude on your own Claude subscription.

## Where it runs

All of it runs in the cloud on free tiers, so your computer can be off:
GitHub Actions does the work, a small Cloudflare Worker keeps the clock and answers Telegram,
Claude writes the words. How it fits together: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Getting improvements later

This kit gets better over time. To pull the newest version into your copy, open Claude Code in
your `garmin-life-kit` folder and say: **pull the latest updates from coreywessel/garmin-life-kit**.
Claude merges the changes, keeps your settings and notes, checks everything still works, and
puts it live. Your keys are never touched.

## Changing things later

Open Claude Code in your copy of the kit and ask in plain words: "move the morning recap to
6:45", "switch to metric", "drop stress from my recaps", "I got a new watch". Everything about you lives in one file,
`profile.toml`.
