// "Today" really means today — the calendar's anchor, and what hangs off it
// (#389 review, following #387).
//
// #387 made the demo's ice inventory RELATIVE to the instant it is seeded at.
// It deliberately left the other half alone: `app.js` still hard-coded
// `calendarDate = "2026-09-05"`, the calendar's "Today" button reset to that
// same literal, and the ice-slot drawer defaulted its Date field to it. Those
// two hard-coded constants used to agree with each other — the seed booked
// 2026-09-05..19 and the app called 2026-09-05 "today" — so the demo looked
// coherent while being wrong about both. Moving one of them exposed the other:
// the landing dashboard's "Games this week" tile read 0 and its "today" list
// silently fell back to showing every game in the league.
//
// So this gate holds the two halves together, at desktop and 390px:
//
//   1. The calendar opens on the current UTC date — not a literal, and not a
//      date derived some other way that happens to look right.
//   2. The "Today" button returns to that date after navigating away, instead
//      of jumping to a fixed day in 2026.
//   3. The ice-slot drawer's Date field defaults to the day being VIEWED, so
//      an operator on the 12th gets the 12th, not a literal and not today.
//   4. On a freshly-seeded demo the dashboard's "Games this week" tile is
//      non-zero, and reads the exact number the seed puts in that window
//      (`tests/test_demo_relative_ice.py` pins the same 10 from the data
//      side). This is the regression itself: it is what reads 0 if day zero
//      is pushed outside the dashboard's 7-day window again.
//   5. The games card relabels itself when it falls back from "today's games"
//      to the whole schedule.
//   6. The demo really is seeded past the retired window, and its ice is on
//      the calendar where the operator can reach it (pinned pass only).
//   7. Structurally, `app.js` names no calendar date at all — the guard that
//      keeps any of the three sites above from quietly re-acquiring one.
//
// AND IT RUNS TWICE — the part that is easy to get wrong (#389 review).
//
// A browser journey against the machine's real clock can only prove the app
// correct on TODAY'S date, and today is still before the retired September 2026
// window. That is not a small gap: it is the identical gap that let the
// original bug survive. Hardcoding `todayISO()` to return the real current date
// passes every behavioural assertion above, start to finish, on a real-clock
// run — measured, not assumed.
//
// So the first pass pins BOTH clocks to `PINNED_INSTANT`, five years past that
// window: the server's demo seed instant (via HOCKEY_DEMO_SEED_INSTANT) and the
// browser's `Date` (via an init script, before app code loads). Both, because
// either alone would leave the two disagreeing about what week it is and make
// the counts meaningless. The second pass then runs on the real clock, so the
// pinned pass cannot merely be agreeing with itself.
//
// Every expected day is derived from the SAME instant the app is using
// (`page.evaluate(() => calendarDate)` and the app's own `addDays`), except in
// (1), where comparing the app against an independent reading of the clock is
// the whole point of the assertion.
//
// Fails on any browser console/page error.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const fs = require("fs");
const http = require("http");
const path = require("path");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const APP_JS = path.resolve(
  BACKEND_DIR, "hockey_scheduler", "web", "static", "app.js");
const READY_TIMEOUT_MS = 15000;
// The number of demo games the dashboard's inclusive [today, today + 6] window
// must contain on a brand-new demo. Day zero is a 3-day lead, so the window
// holds day zero (1 game) plus the first three days of the pilot pack (3 each).
// Pinned as an exact number on both sides of the stack on purpose: an
// approximate ">= 1" here would keep passing while the demo drifted back out
// of the operator's week one day at a time.
const GAMES_THIS_WEEK = 10;
// The instant the second pass travels to: five years past the retired
// 2026-09-05..19 window, deliberately NOT midnight and NOT the old anchor
// weekday (2031-04-17 is a Thursday), so neither the time of day nor the day
// of the week can be inherited and mistaken for a correct derivation. Same
// instant as the backend's FAR_FUTURE, so the two halves of the proof are
// talking about the same moment.
const PINNED_INSTANT = "2031-04-17T13:05:41.123456+00:00";

function waitForServer(url, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const attempt = () => {
      const request = http.get(url, (response) => { response.resume(); resolve(); });
      request.setTimeout(2000, () => request.destroy(new Error("request timed out")));
      request.on("error", () => {
        if (Date.now() > deadline) reject(new Error(`Server never came up at ${url}`));
        else setTimeout(attempt, 250);
      });
    };
    attempt();
  });
}

function stopServer(server) {
  return new Promise((resolve) => {
    if (server.exitCode !== null || server.signalCode !== null) return resolve();
    const escalate = setTimeout(() => server.kill("SIGKILL"), 3000);
    server.once("exit", () => { clearTimeout(escalate); resolve(); });
    server.kill("SIGTERM");
  });
}

const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8231 },
  { label: "phone", width: 390, height: 844, port: 8232 },
];

// The current UTC calendar day, read independently of the app. Only (1) uses
// this — everything else derives from what the app itself is showing.
function utcToday() {
  return new Date().toISOString().slice(0, 10);
}

// Pins the page's clock BEFORE any app code runs. `app.js` is a classic script
// (index.html) and `calendarDate` is initialised at its top level, so this has
// to be an init script — anything evaluated after navigation is far too late.
//
// Only the zero-argument forms move: `new Date()` and `Date.now()`. Every
// parsing form is left alone, because the app leans on them constantly
// (`new Date(dateStr + "T00:00:00Z")` in addDays/addMonths/weekDays). Subclassing
// rather than wrapping keeps the prototype chain, `instanceof`, and the
// inherited statics (`parse`, `UTC`) exactly as they were.
function pinBrowserClock(context, iso) {
  return context.addInitScript((pinnedIso) => {
    const FIXED = new Date(pinnedIso).getTime();
    const RealDate = Date;
    class PinnedDate extends RealDate {
      constructor(...args) {
        if (args.length === 0) super(FIXED);
        else super(...args);
      }
      static now() { return FIXED; }
    }
    globalThis.Date = PinnedDate;
  }, iso);
}

// `pinned` is null for the real-clock pass, or an ISO-8601 instant to travel
// to. When set, the SERVER seeds its demo at that instant (via the env hook)
// and the BROWSER believes it is that instant — both halves, or the two would
// disagree about what week it is and the counts below would be meaningless.
async function checkViewport(browser, viewport, pinned) {
  const base = `http://${HOST}:${viewport.port}`;
  const server = spawn(
    process.env.PYTHON || "python3",
    ["-u", "-m", "hockey_scheduler.web.server", "--host", HOST,
     "--port", String(viewport.port)],
    { cwd: BACKEND_DIR, stdio: ["ignore", "pipe", "pipe"],
      env: pinned
        ? { ...process.env, HOCKEY_DEMO_SEED_INSTANT: pinned }
        : process.env });
  let serverOutput = "";
  server.stdout.on("data", (d) => { serverOutput += d.toString(); });
  server.stderr.on("data", (d) => { serverOutput += d.toString(); });

  const context = await browser.newContext({
    viewport: { width: viewport.width, height: viewport.height },
  });
  if (pinned) await pinBrowserClock(context, pinned);
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(`[pageerror] ${e.message}`));
  page.on("console", (m) => {
    if (m.type() === "error") errors.push(`[console] ${m.text()}`);
  });
  const label = `${viewport.label}${pinned ? " @" + pinned.slice(0, 10) : ""}`;
  const fail = (msg) => { throw new Error(`[${label}] ${msg}`); };
  const calDay = () => page.evaluate(() => calendarDate);

  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);

    // (1) The calendar opens on the current UTC date.
    //
    // On the real-clock pass this is bracketed by two independent clock
    // readings, so a run that straddles UTC midnight names both permitted days
    // rather than flaking — still an exact claim, a set of at most two dates.
    // On the pinned pass there is nothing to straddle: one exact date.
    const before = pinned ? pinned.slice(0, 10) : utcToday();
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });
    const after = pinned ? pinned.slice(0, 10) : utcToday();
    const opened = await calDay();
    if (![before, after].includes(opened)) {
      fail(`calendar opened on ${opened}, not the current UTC date `
        + `(${before === after ? before : `${before} or ${after}`})`);
    }
    const today = opened;

    // (2) "Today" returns to today after navigating away.
    await page.click('.tab[data-tab="calendar"]');
    await page.waitForSelector('[data-cal="0"]', { timeout: 10000 });
    await page.click('[data-cal="1"]');
    await page.click('[data-cal="1"]');
    await page.click('[data-cal="1"]');
    const wandered = await calDay();
    const expectedWander = await page.evaluate((d) => addDays(d, 3), today);
    // Premise: the journey really did leave today, so "Today" below has
    // somewhere to come back FROM. Without this, a "Today" button that did
    // nothing at all would pass step (2).
    if (wandered !== expectedWander) {
      fail(`three forward steps from ${today} landed on ${wandered}, expected ${expectedWander}`);
    }
    await page.click('[data-cal="0"]');
    const returned = await calDay();
    if (returned !== today) {
      fail(`"Today" navigated to ${returned}, not today (${today})`);
    }

    // (3) The ice-slot drawer's Date defaults to the day being VIEWED.
    //
    // Loaded first so the drawer has a Rink to offer; the demo's own ice is
    // also what step (4) counts.
    const loadStatus = await page.evaluate(() => fetch("/api/demo/load", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" }, body: "{}",
    }).then((r) => r.status));
    if (loadStatus !== 200) fail(`demo load failed (status ${loadStatus})`);
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });

    await page.click('.tab[data-tab="calendar"]');
    await page.waitForSelector('[data-cal="0"]', { timeout: 10000 });
    await page.click('[data-cal="1"]');
    await page.click('[data-cal="1"]');
    const viewed = await calDay();
    const expectedViewed = await page.evaluate((d) => addDays(d, 2), today);
    if (viewed !== expectedViewed) {
      fail(`two forward steps landed on ${viewed}, expected ${expectedViewed}`);
    }
    await page.click('.tab[data-tab="setup"]');
    await page.click('[data-setup-view="records"]');
    await page.waitForSelector('.setup-card .sc-new[data-drawer="ice-slot"]',
                              { timeout: 15000 });
    await page.click('.setup-card .sc-new[data-drawer="ice-slot"]');
    await page.waitForSelector(".drawer[role=dialog] #f-slot-date",
                               { timeout: 10000 });
    const slotDefault = await page.$eval("#f-slot-date", (el) => el.value);
    if (slotDefault !== viewed) {
      fail(`ice-slot drawer defaulted its Date to ${slotDefault}, `
        + `not the day being viewed (${viewed})`);
    }
    await page.click(".drawer[role=dialog] button.drawer-x[data-drawer-close]");
    await page.waitForFunction(
      () => !document.querySelector(".drawer[role=dialog]"),
      null, { timeout: 10000 });

    // (4) A freshly-seeded demo has games in the dashboard's own week.
    await page.click('.tab[data-tab="calendar"]');
    await page.waitForSelector('[data-cal="0"]', { timeout: 10000 });
    await page.click('[data-cal="0"]');   // back to today before reading it
    await page.click('.tab[data-tab="dashboard"]');
    await page.waitForSelector(".dash-stats .dash-stat", { timeout: 15000 });
    const tile = await page.$$eval(".dash-stats .dash-stat", (els) => {
      const hit = els.find((e) => {
        const label = e.querySelector(".ds-label");
        return label && label.textContent.trim() === "Games this week";
      });
      if (!hit) return null;
      return {
        n: hit.querySelector(".ds-n").textContent.trim(),
        sub: hit.querySelector(".ds-sub").textContent.trim(),
      };
    });
    if (!tile) fail(`the dashboard has no "Games this week" tile`);
    if (tile.n !== String(GAMES_THIS_WEEK)) {
      fail(`"Games this week" read ${tile.n} on a fresh demo, expected `
        + `${GAMES_THIS_WEEK} (sub: "${tile.sub}")`);
    }
    // The breadcrumb computes the same window separately (#118 Phase 7 /
    // #145 review) — both must agree, or one of them is counting a different
    // seven days from the other.
    const crumb = await page.$eval("#breadcrumb", (el) => el.textContent.trim());
    if (!crumb.includes(`${GAMES_THIS_WEEK} games this week`)) {
      fail(`breadcrumb disagrees with the tile: "${crumb}"`);
    }

    // (5) The games card SAYS which list it is showing.
    //
    // The demo's first game is a few days out, never today — day zero cannot
    // be today without being past-dated for part of the day, which is the
    // failure #387 removed. So the card falls back from "today's games" to the
    // whole schedule, and the point is that it RELABELS itself when it does:
    // the blocker for this change was a silent fallback, and this is what
    // keeps it from being silent. A card headed "Today's Games" while listing
    // games that are not today is the thing being ruled out.
    const card = await page.evaluate(() => {
      // The games card is the one whose header links onward to Games.
      const link = document.querySelector('.dash-card [data-goto="games"]');
      if (!link) return null;
      const box = link.closest(".dash-card");
      const head = box.querySelector(".dash-card-head");
      return {
        title: head.querySelector("h3").textContent.trim(),
        sub: head.querySelector(".dch-sub").textContent.trim(),
        rows: box.querySelectorAll(".tg-row").length,
      };
    });
    if (!card) fail("the dashboard has no games card");
    if (card.title !== "Scheduled Games") {
      fail(`games card is headed "${card.title}" with nothing scheduled today; `
        + `it must relabel itself rather than call the fallback "Today's Games"`);
    }
    if (card.sub !== `${card.rows} games`) {
      fail(`games card counts "${card.sub}" but lists ${card.rows} rows`);
    }

    // (6) PINNED PASS ONLY — the point of travelling at all.
    //
    // Everything above would also pass on 2026-08-03, which is the criticism
    // that produced this pass: a journey run against the machine's real clock
    // proves the app correct on today's date, and today's date is still BEFORE
    // the retired September 2026 window. So here the whole stack has been moved
    // five years past that window, and the demo must still be a live demo:
    // seeded on the far side of the expired bomb, with its ice on the calendar
    // where the operator can reach it, and nothing left behind in 2026-09.
    if (pinned) {
      const dayZero = await page.evaluate((d) => addDays(d, 3), today);
      const slotDays = await page.evaluate(async () => {
        const r = await fetch("/api/demo/overview", { credentials: "same-origin" });
        const ov = await r.json();
        return (ov.ice_slots || []).map((s) => String(s.start_time).slice(0, 10));
      });
      // Premise: a full inventory really came back, so the emptiness checks
      // below are about WHERE the ice is and not about there being none.
      if (slotDays.length !== 57) {
        fail(`expected the demo's 57 ice slots, got ${slotDays.length}`);
      }
      const retired = slotDays.filter((d) => d >= "2026-09-05" && d <= "2026-09-19");
      if (retired.length) {
        fail(`${retired.length} slot(s) still land in the retired 2026-09-05..19 `
          + `window: ${[...new Set(retired)].join(", ")}`);
      }
      const earliest = slotDays.slice().sort()[0];
      if (earliest !== dayZero) {
        fail(`earliest demo ice is ${earliest}, expected day zero ${dayZero} `
          + `(the pinned instant + a 3-day lead)`);
      }
      // And it is REACHABLE: three steps from the day the app opened on is
      // day zero, and the day board paints that ice rather than an empty day.
      await page.click('.tab[data-tab="calendar"]');
      await page.waitForSelector('[data-cal="0"]', { timeout: 10000 });
      await page.click('[data-cal="0"]');
      await page.click('[data-cal="1"]');
      await page.click('[data-cal="1"]');
      await page.click('[data-cal="1"]');
      const shown = await calDay();
      if (shown !== dayZero) fail(`three steps landed on ${shown}, not ${dayZero}`);
      await page.waitForSelector("[data-slot]", { timeout: 15000 });
      // Day zero's three Main Rink slots, by the state each is really in:
      // 16:00 and 20:30 free (so `data-slot`, clickable to schedule) and 18:30
      // holding the seeded game (so `.slot-card.allocated`). Split out rather
      // than totalled, because "3 cards" would also be satisfied by three
      // empty ones — the seeded game being visible is half the claim.
      const painted = await page.evaluate(() => ({
        available: document.querySelectorAll("[data-slot]").length,
        allocated: document.querySelectorAll(".slot-card.allocated").length,
      }));
      if (painted.available !== 2 || painted.allocated !== 1) {
        fail(`day zero painted ${painted.available} available + `
          + `${painted.allocated} allocated ice cards, expected 2 + 1`);
      }
    }

    if (errors.length) fail(`browser errors: ${errors.join(" | ")}`);
    console.log(`  ${label}: calendar opens on ${today}, `
      + `"Today" returns there, slot drawer follows the viewed day, `
      + `${GAMES_THIS_WEEK} games this week`);
  } catch (e) {
    if (serverOutput.trim()) console.error(serverOutput.trim());
    throw e;
  } finally {
    await context.close();
    await stopServer(server);
  }
}

// (7) Structural: no calendar date may be named in app.js at all.
//
// Runs LAST, deliberately. It used to run first and short-circuit the whole
// gate, which meant a re-introduced literal was only ever demonstrated to fail
// a source scan — and a source scan is not evidence about behaviour. With the
// browser passes ahead of it, restoring `calendarDate = "2026-09-05"` fails the
// real journey at the pinned instant first, and this is the backstop for the
// case behaviour cannot see: a literal that happens to equal the day the suite
// is run on.
function checkNoCalendarDateLiteral() {
  const src = fs.readFileSync(APP_JS, "utf8");
  const lines = src.split("\n");
  const hits = [];
  lines.forEach((line, i) => {
    // Only quoted literals — comments explaining the retired dates by name
    // are exempt, exactly as in the backend's own structural guard.
    const code = line.replace(/\/\/.*$/, "");
    const m = code.match(/["'`]\d{4}-\d{2}-\d{2}["'`]/);
    if (m) hits.push(`app.js:${i + 1}: ${m[0]}`);
  });
  if (hits.length) {
    throw new Error(
      `app.js names calendar dates; the calendar must derive them from the `
      + `clock:\n  ${hits.join("\n  ")}`);
  }
  // Premise: the file really was read, so an empty hit list is evidence about
  // app.js and not about an unreadable path.
  if (src.length < 100000) {
    throw new Error(`app.js read back only ${src.length} bytes — wrong path?`);
  }
  console.log(`  static: app.js names no calendar date `
    + `(${lines.length} lines scanned)`);
}

async function main() {
  const browser = await chromium.launch();
  try {
    // Two passes over both viewports, PINNED FIRST.
    //
    // The PINNED pass is the one that matters for #387: it moves the server's
    // seed instant AND the browser's Date five years past the retired window,
    // so the journey runs after the time bomb has expired instead of before
    // it. Without it every assertion here would be satisfied by the very bug
    // being fixed — a hardcoded date equal to the day the suite happens to run
    // on passes a real-clock journey completely, which is how that bug
    // survived for years. It runs first so that is the failure reported.
    //
    // The REAL-CLOCK pass then proves the app reads the ACTUAL clock, so the
    // pinned pass cannot be merely agreeing with itself.
    for (const pinned of [PINNED_INSTANT, null]) {
      for (const viewport of VIEWPORTS) {
        await checkViewport(browser, viewport, pinned);
      }
    }
  } finally {
    await browser.close();
  }
  checkNoCalendarDateLiteral();
  console.log("calendar-today: OK");
}

main().catch((e) => { console.error(e.message); process.exit(1); });
