// garmin-life-kit Worker (Cloudflare, free plan). No npm packages.
//
// Why it exists: GitHub's own `schedule:` can run hours late, and nothing in GitHub can
// answer a Telegram message quickly. This Worker is awake all the time. It keeps the clock
// (dispatching each job at its LOCAL time, so times hold through daylight saving) and answers
// Telegram at once, starting a GitHub run only when real work is needed.
//
//   POST /telegram   Telegram webhook. Commands start command.yml; other text is a Log note
//                    (saved to KV, then a thumbs-up).
//   GET  /log        kit/log.py reads saved notes (each with its KV key). Deletes nothing.
//   POST /log/ack    kit/log.py clears the keys it has committed.
//   GET  /health     watchdog: is the Worker up, how many notes are waiting.
//   POST /wake       optional iPhone Shortcut: starts the morning recap once a day.
//
// [vars] (wrangler.toml, rendered by setup/kit.py): REPO, TIMEZONE, MORNING, EVENING, CHAT_ID,
// OWNER_ID, ALERTS_THREAD, LOG_THREAD, FEATURES. KV binding: KIT.
// Secrets (wrangler secret put): TELEGRAM_BOT_TOKEN, GITHUB_TOKEN, WORKER_API_SECRET,
// TELEGRAM_WEBHOOK_SECRET, SHORTCUT_SECRET. Three separate shared secrets, never reused.

const COMMANDS = {
  today: "Today's activities, steps and last night's sleep",
  week: "This week so far, per sport",
  last: "Card for your most recent activity",
  status: "Last run of each job, pending notes, expiry dates",
  help: "This list",
};
const DISPATCHED = ["today", "week", "last", "status"];

const BATCH = 200;              // notes per /log call (KV allows ~1,000 operations per request)
const WAKE_EARLIEST_HOUR = 4;   // local: an alarm stopped before 4am must not use up the day's recap

// Tests shorten this so the retry path runs in milliseconds.
export const timing = { retryMs: 20000 };

// ---------- schedule ----------

function localParts(date, tz) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  }).formatToParts(date);
  const get = (t) => parts.find((p) => p.type === t).value;
  return { day: `${get("year")}-${get("month")}-${get("day")}`, hour: Number(get("hour")), minute: Number(get("minute")) };
}

function toMinutes(hhmm) {
  const m = /^(\d{1,2}):(\d{2})$/.exec(String(hhmm || "").trim());
  return m ? Number(m[1]) * 60 + Number(m[2]) : null;
}

function features(env) {
  try { return JSON.parse(env.FEATURES || "{}") || {}; } catch { return {}; }
}

// A feature is on unless the profile turned it off explicitly.
function on(f, name) {
  return f[name] !== false;
}

// Pure: which workflow dispatches are due at this instant. Runs every 5 minutes.
export function dueJobs(date, env) {
  const tz = env.TIMEZONE || "UTC";
  const { hour, minute } = localParts(date, tz);
  const now = hour * 60 + minute;
  const morning = toMinutes(env.MORNING);
  const evening = toMinutes(env.EVENING);
  const f = features(env);
  const jobs = [];

  if (morning !== null && now === morning && on(f, "morning_recap")) {
    jobs.push({ workflow: "recap.yml", inputs: { mode: "morning", source: "schedule" } });
  }
  if (evening !== null && now === evening && on(f, "evening_recap")) {
    jobs.push({ workflow: "recap.yml", inputs: { mode: "evening", source: "schedule" } });
  }
  if (on(f, "watchdog")) {
    if (morning !== null && now === (morning + 45) % 1440) jobs.push({ workflow: "watchdog.yml", inputs: { slot: "morning" } });
    if (evening !== null && now === (evening + 45) % 1440) jobs.push({ workflow: "watchdog.yml", inputs: { slot: "evening" } });
  }
  // Every 2 hours at :20, from the morning hour through the evening hour (wrapping past midnight).
  if (minute === 20 && morning !== null && evening !== null && (on(f, "log_notes") || on(f, "activity_cards"))) {
    const start = Math.floor(morning / 60);
    const span = (Math.floor(evening / 60) - start + 24) % 24;
    const since = (hour - start + 24) % 24;
    if (since <= span && since % 2 === 0) jobs.push({ workflow: "collect.yml", inputs: {} });
  }
  return jobs;
}

// ---------- entry points ----------

export default {
  async scheduled(controller, env, ctx) {
    for (const { workflow, inputs } of dueJobs(new Date(controller.scheduledTime), env)) {
      let res = await dispatch(env, workflow, inputs);
      if (!res.ok && (res.status === 0 || res.status >= 500)) {   // GitHub or network hiccup: one more try
        await new Promise((r) => setTimeout(r, timing.retryMs));
        res = await dispatch(env, workflow, inputs);
      }
      if (!res.ok) {
        await say(env, env.ALERTS_THREAD,
          `<b>Couldn't start ${esc(workflow)}</b> on schedule. GitHub said ${res.status} ${esc(res.body)}`, "HTML");
      }
    }
  },

  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const route = `${request.method} ${url.pathname}`;
    try {
      if (route === "POST /telegram") return await telegram(request, env, ctx);
      if (route === "GET /health") return json(await health(env));
      if (route === "GET /log" || route === "POST /log/ack") {
        if (!secretOk(request.headers.get("X-Worker-Secret"), env.WORKER_API_SECRET)) {
          return json({ ok: false, error: "unauthorized" }, 401);
        }
        return json(route === "GET /log" ? await readLog(env) : await ackLog(request, env));
      }
      if (route === "POST /wake") return await wake(request, env, ctx);
      return json({ ok: false, error: "not found" }, 404);
    } catch (e) {
      console.error(`${route} failed: ${e.stack || e}`);
      return json({ ok: false, error: "internal error" }, 500);
    }
  },
};

// ---------- Telegram webhook ----------

async function telegram(request, env, ctx) {
  // Telegram sends back the secret_token we gave it in setWebhook. Anyone else gets nothing.
  if (!secretOk(request.headers.get("X-Telegram-Bot-Api-Secret-Token"), env.TELEGRAM_WEBHOOK_SECRET)) {
    return new Response("unauthorized", { status: 401 });
  }
  const update = await request.json().catch(() => ({}));
  const m = update.message;
  // Only the owner's group, only the owner (OWNER_ID unset = nobody), never a bot, only text.
  if (m && env.CHAT_ID && String(m.chat?.id) === String(env.CHAT_ID) && isOwner(m.from, env)
      && !m.from?.is_bot && typeof m.text === "string" && m.text.trim()) {
    // Answer Telegram now and work after: a slow answer makes Telegram resend the update.
    ctx.waitUntil(handleMessage(m, env).catch((e) => console.error(`message ${m.message_id}: ${e.stack || e}`)));
  }
  return new Response("ok");
}

function isOwner(from, env) {
  return Boolean(env.OWNER_ID) && from?.id !== undefined && String(from.id) === String(env.OWNER_ID);
}

async function handleMessage(m, env) {
  // Messages in the General topic have no thread id.
  const thread = m.is_topic_message ? m.message_thread_id : undefined;
  const text = m.text.trim();

  if (text.startsWith("/")) {
    // "/week@my_bot extra words" -> "week"
    const name = text.slice(1).split(/\s+/)[0].split("@")[0].toLowerCase();
    if (name === "help" || name === "start") {
      return say(env, thread, Object.entries(COMMANDS).map(([c, d]) => `/${c} - ${d}`).join("\n"));
    }
    if (!DISPATCHED.includes(name)) return say(env, thread, `Unknown command /${name}. Send /help.`);
    const res = await dispatch(env, "command.yml", { command: name, thread_id: thread ? String(thread) : "" });
    // Fail loud: if GitHub said no, say so where it was asked.
    if (!res.ok) await say(env, thread, `Couldn't start /${name}: GitHub said ${res.status} ${res.body}`);
    return;
  }

  if (!on(features(env), "log_notes")) return;
  // One KV key per message, so two notes arriving at once never overwrite each other.
  const item = { ts: m.date, thread: thread ?? null, text: m.text, message_id: m.message_id };
  try {
    await env.KIT.put(`log:${m.date}:${m.message_id}`, JSON.stringify(item));
  } catch (e) {
    // No thumbs-up unless it was really saved.
    return say(env, thread, "Couldn't save that note. Send it again in a few minutes.");
  }
  const r = await tg(env, "setMessageReaction", {
    chat_id: env.CHAT_ID, message_id: m.message_id, reaction: [{ type: "emoji", emoji: "👍" }],
  });
  if (!r.ok) console.error(`reaction failed: ${r.description}`);
}

// ---------- endpoints for Actions, the watchdog and the Shortcut ----------

// Nothing is deleted here. A note stays in KV until kit/log.py has committed it and sends the
// key back to /log/ack, so a failed run just means the same notes come again next time.
async function readLog(env) {
  const all = (await keys(env, "log:")).sort();
  const items = [];
  for (const key of all.slice(0, BATCH)) {
    const v = await env.KIT.get(key);
    if (v) items.push({ ...JSON.parse(v), key });   // listing lags deletes, so a key can be gone
  }
  items.sort((a, b) => a.ts - b.ts || a.message_id - b.message_id);
  return { ok: true, items, more: all.length > BATCH };
}

async function ackLog(request, env) {
  const body = await request.json().catch(() => ({}));
  const done = [...new Set((Array.isArray(body.keys) ? body.keys : [])
    .filter((k) => typeof k === "string" && k.startsWith("log:")))].slice(0, BATCH);
  for (const key of done) await env.KIT.delete(key);
  return { ok: true, deleted: done.length };
}

async function health(env) {
  return { ok: true, log_pending: (await keys(env, "log:")).length };
}

async function wake(request, env, ctx) {
  if (!env.SHORTCUT_SECRET) return json({ ok: false, error: "not found" }, 404);
  if (!secretOk(request.headers.get("X-Shortcut-Secret"), env.SHORTCUT_SECRET)) {
    return json({ ok: false, error: "unauthorized" }, 401);
  }
  const { day, hour } = localParts(new Date(), env.TIMEZONE || "UTC");
  if (hour < WAKE_EARLIEST_HOUR) return json({ ok: true, recap_started: false, reason: "before 4am" });
  const key = `wake:${day}`;
  if (await env.KIT.get(key)) return json({ ok: true, recap_started: false, reason: "already started today" });
  await env.KIT.put(key, String(Date.now()), { expirationTtl: 36 * 3600 });
  ctx.waitUntil((async () => {
    const res = await dispatch(env, "recap.yml", { mode: "morning", source: "wake" });
    if (!res.ok) {
      await env.KIT.delete(key);   // let the next try start it
      await say(env, env.ALERTS_THREAD,
        `<b>Wake-up recap didn't start.</b> GitHub said ${res.status} ${esc(res.body)}`, "HTML");
    }
  })());
  return json({ ok: true, recap_started: true });
}

// ---------- helpers ----------

async function keys(env, prefix) {
  const out = [];
  let cursor;
  do {
    const page = await env.KIT.list({ prefix, cursor });
    out.push(...page.keys.map((k) => k.name));
    cursor = page.list_complete ? undefined : page.cursor;
  } while (cursor);
  return out;
}

// Never throws: a network failure comes back as status 0, so every caller's !res.ok path runs.
async function dispatch(env, workflow, inputs) {
  try {
    const r = await fetch(`https://api.github.com/repos/${env.REPO}/actions/workflows/${workflow}/dispatches`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "garmin-life-kit-worker",   // GitHub rejects requests without one
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ ref: "main", inputs }),
    });
    const body = r.ok ? "" : (await r.text()).slice(0, 200);
    if (!r.ok) console.error(`dispatch ${workflow} failed: ${r.status} ${body}`);
    return { ok: r.ok, status: r.status, body };
  } catch (e) {
    console.error(`dispatch ${workflow} failed: ${e.stack || e}`);
    return { ok: false, status: 0, body: String(e.message || e) };
  }
}

// Every Telegram call checks ok. Errors never include the URL (it holds the token).
async function tg(env, method, payload) {
  try {
    const r = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    return await r.json();
  } catch (e) {
    return { ok: false, description: `network failure: ${e.message || e}` };
  }
}

async function say(env, thread, text, parseMode) {
  const r = await tg(env, "sendMessage", {
    chat_id: env.CHAT_ID,
    message_thread_id: thread ? Number(thread) : undefined,
    text,
    parse_mode: parseMode,
    disable_web_page_preview: true,
  });
  if (!r.ok) console.error(`sendMessage failed: ${r.description}`);
  return r;
}

// Constant-time compare (after a length check) so a secret can't be guessed byte by byte.
// An unset secret never matches anything.
export function secretOk(given, expected) {
  if (!given || !expected) return false;
  const a = new TextEncoder().encode(given);
  const b = new TextEncoder().encode(expected);
  if (a.byteLength !== b.byteLength) return false;
  let diff = 0;
  for (let i = 0; i < a.byteLength; i++) diff |= a[i] ^ b[i];
  return diff === 0;
}

function esc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), { status, headers: { "Content-Type": "application/json" } });
}
