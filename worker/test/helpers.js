// Test doubles for the Worker: env, KV, fetch and ctx. Node 22 built-ins only.

export const SECRETS = {
  TELEGRAM_WEBHOOK_SECRET: "a".repeat(64),
  WORKER_API_SECRET: "b".repeat(64),
  SHORTCUT_SECRET: "c".repeat(64),
};

export function makeEnv(overrides = {}) {
  return {
    REPO: "someone/garmin-life-kit",
    TIMEZONE: "America/Denver",
    MORNING: "07:30",
    EVENING: "21:00",
    CHAT_ID: "-1001234567890",
    OWNER_ID: "42",
    ALERTS_THREAD: "5",
    LOG_THREAD: "4",
    FEATURES: JSON.stringify({ morning_recap: true, evening_recap: true, activity_cards: true,
      weather: true, log_notes: true, watchdog: true }),
    TELEGRAM_BOT_TOKEN: "123:test-token",
    GITHUB_TOKEN: "github_pat_test",
    ...SECRETS,
    KIT: makeKV(),
    ...overrides,
  };
}

// In-memory KV with the four methods the Worker uses. failPut makes put() throw.
export function makeKV({ failPut = false } = {}) {
  const store = new Map();
  return {
    store,
    puts: [],
    deletes: [],
    async get(k) { return store.has(k) ? store.get(k) : null; },
    async put(k, v) {
      if (failPut) throw new Error("KV unavailable");
      this.puts.push(k);
      store.set(k, v);
    },
    async delete(k) { this.deletes.push(k); store.delete(k); },
    async list({ prefix = "" } = {}) {
      return { keys: [...store.keys()].filter((k) => k.startsWith(prefix)).map((name) => ({ name })), list_complete: true };
    },
  };
}

export function makeCtx() {
  const tasks = [];
  return { tasks, waitUntil(p) { tasks.push(p); }, async done() { await Promise.all(tasks); } };
}

// Replaces global fetch. handler(url, init, call) returns a Response or throws.
// Default: GitHub dispatch 204, Telegram {ok: true}.
export function mockFetch(handler) {
  const calls = [];
  const real = globalThis.fetch;
  globalThis.fetch = async (url, init = {}) => {
    const call = { url: String(url), init, body: init.body ? JSON.parse(init.body) : undefined };
    calls.push(call);
    if (handler) {
      const r = await handler(call.url, init, call);
      if (r) return r;
    }
    if (call.url.startsWith("https://api.github.com/")) return new Response(null, { status: 204 });
    return Response.json({ ok: true, result: {} });
  };
  return {
    calls,
    restore() { globalThis.fetch = real; },
    dispatches() { return calls.filter((c) => c.url.startsWith("https://api.github.com/")); },
    telegram(method) { return calls.filter((c) => c.url.startsWith("https://api.telegram.org/") && c.url.endsWith(`/${method}`)); },
  };
}

export function telegramRequest(update, secret = SECRETS.TELEGRAM_WEBHOOK_SECRET) {
  const headers = { "Content-Type": "application/json" };
  if (secret !== null) headers["X-Telegram-Bot-Api-Secret-Token"] = secret;
  return new Request("https://w.example/telegram", { method: "POST", headers, body: JSON.stringify(update) });
}

export function message({ text = "hello", from = { id: 42, is_bot: false }, chat = -1001234567890,
  thread = 7, id = 100, date = 1767225600 } = {}) {
  return {
    update_id: 1,
    message: {
      message_id: id, date, text, from, chat: { id: chat, type: "supergroup" },
      ...(thread ? { is_topic_message: true, message_thread_id: thread } : {}),
    },
  };
}
