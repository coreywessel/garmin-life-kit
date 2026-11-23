import { test } from "node:test";
import assert from "node:assert/strict";
import { dueJobs } from "../src/index.js";
import { makeEnv } from "./helpers.js";

const TICK = 5 * 60 * 1000;

// Every 5-minute tick between two UTC instants, with the jobs due at each.
function ticks(fromIso, toIso, env) {
  const out = [];
  for (let t = Date.parse(fromIso); t < Date.parse(toIso); t += TICK) {
    const d = new Date(t);
    for (const job of dueJobs(d, env)) out.push({ at: d.toISOString(), ...job });
  }
  return out;
}

function local(iso, tz = "America/Denver") {
  return new Intl.DateTimeFormat("en-GB", { timeZone: tz, hour: "2-digit", minute: "2-digit", hourCycle: "h23" })
    .format(new Date(iso));
}

const recapAt = (jobs, mode) => jobs.filter((j) => j.workflow === "recap.yml" && j.inputs.mode === mode).map((j) => j.at);

// Each case: a whole local day in Denver, scanned tick by tick. A recap must fire exactly once,
// at the UTC instant that is 07:30 / 21:00 local that day, including both DST switch days.
const DAYS = [
  // [label, scan from (UTC), scan to (UTC), expected morning UTC, expected evening UTC]
  ["winter (MST, UTC-7)", "2026-01-15T07:00:00Z", "2026-01-16T07:00:00Z", "2026-01-15T14:30:00.000Z", "2026-01-16T04:00:00.000Z"],
  ["summer (MDT, UTC-6)", "2026-07-15T06:00:00Z", "2026-07-16T06:00:00Z", "2026-07-15T13:30:00.000Z", "2026-07-16T03:00:00.000Z"],
  ["spring-forward day 2026-03-08", "2026-03-08T07:00:00Z", "2026-03-09T06:00:00Z", "2026-03-08T13:30:00.000Z", "2026-03-09T03:00:00.000Z"],
  ["fall-back day 2026-11-01", "2026-11-01T06:00:00Z", "2026-11-02T07:00:00Z", "2026-11-01T14:30:00.000Z", "2026-11-02T04:00:00.000Z"],
];

for (const [label, from, to, morning, evening] of DAYS) {
  // Why: the whole point of the Worker clock is that recap times stay at the same local time all year.
  test(`recaps fire once at 07:30 and 21:00 local, ${label}`, () => {
    const jobs = ticks(from, to, makeEnv());
    assert.deepEqual(recapAt(jobs, "morning"), [morning]);
    assert.deepEqual(recapAt(jobs, "evening"), [evening]);
    assert.deepEqual(jobs.find((j) => j.inputs.mode === "morning").inputs, { mode: "morning", source: "schedule" });
  });
}

// Why: the watchdog checks 45 minutes after each recap; any other time it would report a recap
// as missing before it had a chance to run.
test("watchdog fires at morning+45 and evening+45 local", () => {
  const jobs = ticks("2026-01-15T07:00:00Z", "2026-01-16T07:00:00Z", makeEnv())
    .filter((j) => j.workflow === "watchdog.yml");
  assert.deepEqual(jobs.map((j) => [local(j.at), j.inputs.slot]), [["08:15", "morning"], ["21:45", "evening"]]);
});

// Why: watchdog for a late evening recap lands after midnight; the +45 must wrap the clock, not vanish.
test("evening+45 wraps past midnight", () => {
  const jobs = ticks("2026-01-15T07:00:00Z", "2026-01-16T07:00:00Z", makeEnv({ EVENING: "23:30" }))
    .filter((j) => j.workflow === "watchdog.yml" && j.inputs.slot === "evening");
  assert.deepEqual(jobs.map((j) => local(j.at)), ["00:15"]);
});

// Why: Log notes and activity cards are collected every 2 hours across the waking day only.
test("collect runs every 2h at :20 from the morning hour through the evening hour", () => {
  const jobs = ticks("2026-01-15T07:00:00Z", "2026-01-16T07:00:00Z", makeEnv())
    .filter((j) => j.workflow === "collect.yml");
  assert.deepEqual(jobs.map((j) => local(j.at)),
    ["07:20", "09:20", "11:20", "13:20", "15:20", "17:20", "19:20", "21:20"]);
  assert.deepEqual(jobs[0].inputs, {});
});

// Why: a night-shift profile (evening before morning on the clock) must still collect across midnight.
test("evening earlier than morning wraps the collect window past midnight", () => {
  const env = makeEnv({ MORNING: "18:00", EVENING: "03:00" });
  const jobs = ticks("2026-01-15T07:00:00Z", "2026-01-16T07:00:00Z", env).filter((j) => j.workflow === "collect.yml");
  assert.deepEqual(jobs.map((j) => local(j.at)).sort(), ["00:20", "02:20", "18:20", "20:20", "22:20"]);
});

// Why: turning a feature off in profile.toml must stop its job, not just hide its output.
test("a disabled feature never schedules", () => {
  const all = (features) => ticks("2026-07-15T06:00:00Z", "2026-07-16T06:00:00Z", makeEnv({ FEATURES: JSON.stringify(features) }));

  const noMorning = all({ morning_recap: false });
  assert.equal(recapAt(noMorning, "morning").length, 0);
  assert.equal(recapAt(noMorning, "evening").length, 1);

  const noEvening = all({ evening_recap: false });
  assert.equal(recapAt(noEvening, "evening").length, 0);
  assert.equal(recapAt(noEvening, "morning").length, 1);

  assert.equal(all({ watchdog: false }).filter((j) => j.workflow === "watchdog.yml").length, 0);
  assert.equal(all({ log_notes: false, activity_cards: false }).filter((j) => j.workflow === "collect.yml").length, 0);
  // Either collector alone keeps collect running.
  assert.equal(all({ log_notes: false, activity_cards: true }).filter((j) => j.workflow === "collect.yml").length, 8);
});
