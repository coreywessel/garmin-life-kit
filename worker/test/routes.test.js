import { test, afterEach, mock } from "node:test";
import assert from "node:assert/strict";
import worker, { timing } from "../src/index.js";
import { SECRETS, makeEnv, makeKV, makeCtx, mockFetch, telegramRequest, message } from "./helpers.js";

let net;
afterEach(() => {
  net?.restore();
  net = undefined;
  mock.timers.reset();
});

async function send(update, env = makeEnv(), secret) {
  const ctx = makeCtx();
  const res = await worker.fetch(telegramRequest(update, secret), env, ctx);
  await ctx.done();
  return { res, env, ctx };
}

function req(method, path, headers = {}, body) {
  return new Request(`https://w.example${path}`, { method, headers, body: body && JSON.stringify(body) });
}

// ---------- webhook auth and who may drive the bot ----------

// Why: without the webhook secret anyone who finds the URL could post fake messages.
test("webhook with a missing or wrong secret is 401 and does nothing", async () => {
  net = mockFetch();
  for (const secret of [null, "wrong", SECRETS.WORKER_API_SECRET]) {
    const { res, env } = await send(message({ text: "/today" }), makeEnv(), secret);
    assert.equal(res.status, 401);
    assert.equal(env.KIT.puts.length, 0);
  }
  assert.equal(net.calls.length, 0);
});

// Why: only the owner, in the owner's group, may start runs or write notes.
test("non-owner, bot, and other-chat messages are ignored", async () => {
  net = mockFetch();
  const cases = [
    message({ text: "/today", from: { id: 99, is_bot: false } }),
    message({ text: "a note", from: { id: 99, is_bot: false } }),
    message({ text: "/today", from: { id: 42, is_bot: true } }),
    message({ text: "a note", from: { id: 42, is_bot: true } }),
    message({ text: "/today", chat: -100999 }),
    message({ text: "a note", chat: -100999 }),
  ];
  for (const update of cases) {
    const { res, env } = await send(update);
    assert.equal(res.status, 200);
    assert.equal(env.KIT.puts.length, 0);
  }
  assert.equal(net.calls.length, 0);
});

// Why: OWNER_ID is required. Unset must mean nobody, never "anyone in the group".
test("OWNER_ID unset means nobody can drive it", async () => {
  net = mockFetch();
  for (const owner of [undefined, ""]) {
    for (const text of ["/today", "a note", "/help"]) {
      const { env } = await send(message({ text }), makeEnv({ OWNER_ID: owner }));
      assert.equal(env.KIT.puts.length, 0);
    }
  }
  assert.equal(net.calls.length, 0);
});

// ---------- commands ----------

// Why: /today must reach command.yml with the topic it was typed in, so the answer lands there.
test("/today dispatches command.yml with the thread id and stays quiet on success", async () => {
  net = mockFetch();
  await send(message({ text: "/today@my_bot", thread: 7 }));
  const [d] = net.dispatches();
  assert.match(d.url, /\/repos\/someone\/garmin-life-kit\/actions\/workflows\/command\.yml\/dispatches$/);
  assert.deepEqual(d.body, { ref: "main", inputs: { command: "today", thread_id: "7" } });
  assert.equal(d.init.headers.Authorization, "Bearer github_pat_test");
  assert.equal(net.telegram("sendMessage").length, 0);
});

// Why: fail loud. A command GitHub refused must say so in the same topic, not vanish.
test("dispatch failure replies in the same thread", async () => {
  net = mockFetch((url) => url.startsWith("https://api.github.com/") ? new Response("Bad credentials", { status: 401 }) : null);
  await send(message({ text: "/week", thread: 9 }));
  const [m] = net.telegram("sendMessage");
  assert.equal(m.body.message_thread_id, 9);
  assert.match(m.body.text, /^Couldn't start \/week: GitHub said 401 Bad credentials/);
});

// Why: /help is answered by the Worker itself (no GitHub run), and unknown commands get a pointer.
test("/help answers locally; unknown commands say so", async () => {
  net = mockFetch();
  await send(message({ text: "/help", thread: 3 }));
  await send(message({ text: "/pass", thread: 3 }));
  assert.equal(net.dispatches().length, 0);
  const [help, unknown] = net.telegram("sendMessage");
  assert.equal(help.body.message_thread_id, 3);
  for (const c of ["/today", "/week", "/last", "/status", "/help"]) assert.ok(help.body.text.includes(c));
  assert.equal(unknown.body.text, "Unknown command /pass. Send /help.");
});

// ---------- Log notes ----------

// Why: a note is only acknowledged once it is really stored, under its own key.
test("a note is written to KV then gets a thumbs-up", async () => {
  net = mockFetch();
  const { env } = await send(message({ text: "slept badly", id: 555, date: 1767225600, thread: 4 }));
  assert.deepEqual(env.KIT.puts, ["log:1767225600:555"]);
  assert.deepEqual(JSON.parse(env.KIT.store.get("log:1767225600:555")),
    { ts: 1767225600, thread: 4, text: "slept badly", message_id: 555 });
  const [r] = net.telegram("setMessageReaction");
  assert.deepEqual(r.body.reaction, [{ type: "emoji", emoji: "👍" }]);
  assert.equal(r.body.message_id, 555);
});

// Why: a thumbs-up on a note that was lost would be a silent failure.
test("KV failure replies Couldn't save and gives no thumbs-up", async () => {
  net = mockFetch();
  await send(message({ text: "a note", thread: 4 }), makeEnv({ KIT: makeKV({ failPut: true }) }));
  assert.equal(net.telegram("setMessageReaction").length, 0);
  const [m] = net.telegram("sendMessage");
  assert.match(m.body.text, /^Couldn't save that note/);
  assert.equal(m.body.message_thread_id, 4);
});

// Why: log_notes = false in profile.toml must stop notes being stored.
test("notes are not saved when log_notes is off", async () => {
  net = mockFetch();
  const { env } = await send(message({ text: "a note" }), makeEnv({ FEATURES: JSON.stringify({ log_notes: false }) }));
  assert.equal(env.KIT.puts.length, 0);
  assert.equal(net.calls.length, 0);
});

// ---------- /log, /log/ack, /health ----------

function seeded() {
  const env = makeEnv();
  env.KIT.store.set("log:200:2", JSON.stringify({ ts: 200, thread: 4, text: "b", message_id: 2 }));
  env.KIT.store.set("log:100:1", JSON.stringify({ ts: 100, thread: 4, text: "a", message_id: 1 }));
  env.KIT.store.set("wake:2026-01-15", "1");
  return env;
}

// Why: notes must survive until Actions has committed them; reading may never delete.
test("GET /log returns notes with keys, sorted, and deletes nothing", async () => {
  const env = seeded();
  const res = await worker.fetch(req("GET", "/log", { "X-Worker-Secret": SECRETS.WORKER_API_SECRET }), env, makeCtx());
  assert.equal(res.status, 200);
  const body = await res.json();
  assert.deepEqual(body.items.map((i) => i.key), ["log:100:1", "log:200:2"]);
  assert.equal(body.more, false);
  assert.equal(env.KIT.deletes.length, 0);
  assert.equal(env.KIT.store.size, 3);
});

// Why: ack is the only delete path and must never touch anything that isn't a log note.
test("POST /log/ack deletes only log: keys", async () => {
  const env = seeded();
  const res = await worker.fetch(req("POST", "/log/ack", { "X-Worker-Secret": SECRETS.WORKER_API_SECRET },
    { keys: ["log:100:1", "wake:2026-01-15", 7] }), env, makeCtx());
  assert.deepEqual(await res.json(), { ok: true, deleted: 1 });
  assert.deepEqual(env.KIT.deletes, ["log:100:1"]);
  assert.ok(env.KIT.store.has("wake:2026-01-15"));
});

// Why: the notes are private; /log and /log/ack need the API secret, and only that secret.
test("/log and /log/ack without the API secret are 401", async () => {
  const env = seeded();
  for (const headers of [{}, { "X-Worker-Secret": "nope" },
    { "X-Worker-Secret": SECRETS.TELEGRAM_WEBHOOK_SECRET }, { "X-Worker-Secret": SECRETS.SHORTCUT_SECRET },
    { "X-Telegram-Bot-Api-Secret-Token": SECRETS.WORKER_API_SECRET }]) {
    assert.equal((await worker.fetch(req("GET", "/log", headers), env, makeCtx())).status, 401);
    assert.equal((await worker.fetch(req("POST", "/log/ack", headers, { keys: ["log:100:1"] }), env, makeCtx())).status, 401);
  }
  assert.equal(env.KIT.deletes.length, 0);
});

// Why: the three secrets are separate; the API secret must not open the Telegram webhook.
test("the API secret does not open the webhook", async () => {
  net = mockFetch();
  const { res, env } = await send(message({ text: "a note" }), makeEnv(), SECRETS.WORKER_API_SECRET);
  assert.equal(res.status, 401);
  assert.equal(env.KIT.puts.length, 0);
});

// Why: the watchdog reads /health without a secret, so it may show counts only.
test("GET /health is public and counts only log notes", async () => {
  const res = await worker.fetch(req("GET", "/health"), seeded(), makeCtx());
  assert.deepEqual(await res.json(), { ok: true, log_pending: 2 });
});

// Why: unknown paths must not fall through to an authenticated handler.
test("anything else is 404", async () => {
  for (const [m, p] of [["GET", "/"], ["POST", "/health"], ["GET", "/telegram"], ["DELETE", "/log"]]) {
    assert.equal((await worker.fetch(req(m, p), makeEnv(), makeCtx())).status, 404);
  }
});

// ---------- /wake ----------

// Why: the Shortcut route is optional; without its secret it must not exist at all.
test("/wake is 404 when SHORTCUT_SECRET is unset", async () => {
  net = mockFetch();
  const res = await worker.fetch(req("POST", "/wake", { "X-Shortcut-Secret": "" }), makeEnv({ SHORTCUT_SECRET: undefined }), makeCtx());
  assert.equal(res.status, 404);
  assert.equal(net.calls.length, 0);
});

// Why: an alarm before 04:00 must not use up the day's recap, and a second alarm must not repeat it.
test("/wake starts the morning recap once per local day, only after 04:00", async () => {
  net = mockFetch();
  const env = makeEnv();
  const wake = async (secret = SECRETS.SHORTCUT_SECRET) => {
    const ctx = makeCtx();
    const res = await worker.fetch(req("POST", "/wake", { "X-Shortcut-Secret": secret }), env, ctx);
    await ctx.done();
    return res;
  };
  assert.equal((await wake(SECRETS.WORKER_API_SECRET)).status, 401);   // wrong secret

  mock.timers.enable({ apis: ["Date"], now: Date.parse("2026-01-15T10:30:00Z") });   // 03:30 Denver
  assert.equal((await (await wake()).json()).recap_started, false);
  assert.equal(net.dispatches().length, 0);

  mock.timers.setTime(Date.parse("2026-01-15T13:10:00Z"));   // 06:10 Denver
  assert.equal((await (await wake()).json()).recap_started, true);
  mock.timers.setTime(Date.parse("2026-01-15T14:00:00Z"));   // 07:00, same day
  assert.equal((await (await wake()).json()).recap_started, false);

  const d = net.dispatches();
  assert.equal(d.length, 1);
  assert.match(d[0].url, /recap\.yml\/dispatches$/);
  assert.deepEqual(d[0].body.inputs, { mode: "morning", source: "wake" });
});

// ---------- scheduled ----------

const MORNING_TICK = { scheduledTime: Date.parse("2026-01-15T14:30:00Z") };   // 07:30 Denver

// Why: a network blip must get one retry, and a final failure must reach Alerts, not vanish.
test("scheduled dispatch network error retries once then alerts in ALERTS_THREAD", async () => {
  timing.retryMs = 1;
  net = mockFetch((url) => { if (url.startsWith("https://api.github.com/")) throw new TypeError("fetch failed"); });
  await worker.scheduled(MORNING_TICK, makeEnv(), makeCtx());
  assert.equal(net.dispatches().length, 2);
  const [alert] = net.telegram("sendMessage");
  assert.equal(alert.body.message_thread_id, 5);
  assert.match(alert.body.text, /Couldn't start recap\.yml<\/b> on schedule\. GitHub said 0 /);
});

// Why: a 5xx followed by success is a hiccup, not an alert.
test("scheduled dispatch 5xx then success: no alert", async () => {
  timing.retryMs = 1;
  let n = 0;
  net = mockFetch((url) => url.startsWith("https://api.github.com/") && n++ === 0 ? new Response("oops", { status: 502 }) : null);
  await worker.scheduled(MORNING_TICK, makeEnv(), makeCtx());
  assert.equal(net.dispatches().length, 2);
  assert.equal(net.telegram("sendMessage").length, 0);
});

// Why: a 4xx (bad token, wrong repo) won't fix itself; alert at once without a wasted retry.
test("scheduled dispatch 4xx alerts without retrying", async () => {
  timing.retryMs = 1;
  net = mockFetch((url) => url.startsWith("https://api.github.com/") ? new Response("Not Found", { status: 404 }) : null);
  await worker.scheduled(MORNING_TICK, makeEnv(), makeCtx());
  assert.equal(net.dispatches().length, 1);
  assert.match(net.telegram("sendMessage")[0].body.text, /GitHub said 404 Not Found/);
});
