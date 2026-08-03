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
//   1. The calendar opens on the REAL current UTC date — not a literal, and
//      not a date derived some other way that happens to look right.
//   2. The "Today" button returns to that date after navigating away, instead
//      of jumping to a fixed day in 2026.
//   3. The ice-slot drawer's Date field defaults to the day being VIEWED, so
//      an operator on the 12th gets the 12th, not a literal and not today.
//   4. On a freshly-seeded demo the dashboard's "Games this week" tile is
//      non-zero, and reads the exact number the seed puts in that window
//      (`tests/test_demo_relative_ice.py` pins the same 10 from the data
//      side). This is the regression itself: it is what reads 0 if day zero
//      is pushed outside the dashboard's 7-day window again.
//   5. Structurally, `app.js` names no calendar date at all — the guard that
//      keeps any of the three sites above from quietly re-acquiring one.
//
// Every expected day below is derived from the SAME instant the app is using
// (`page.evaluate(() => calendarDate)` and the app's own `addDays`), except in
// (1), where comparing the app against an independent clock reading is the
// whole point of the assertion.
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

async function checkViewport(browser, viewport) {
  const base = `http://${HOST}:${viewport.port}`;
  const server = spawn(
    process.env.PYTHON || "python3",
    ["-u", "-m", "hockey_scheduler.web.server", "--host", HOST,
     "--port", String(viewport.port)],
    { cwd: BACKEND_DIR, stdio: ["ignore", "pipe", "pipe"] });
  let serverOutput = "";
  server.stdout.on("data", (d) => { serverOutput += d.toString(); });
  server.stderr.on("data", (d) => { serverOutput += d.toString(); });

  const context = await browser.newContext({
    viewport: { width: viewport.width, height: viewport.height },
  });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(`[pageerror] ${e.message}`));
  page.on("console", (m) => {
    if (m.type() === "error") errors.push(`[console] ${m.text()}`);
  });
  const fail = (msg) => { throw new Error(`[${viewport.label}] ${msg}`); };
  const calDay = () => page.evaluate(() => calendarDate);

  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);

    // (1) The calendar opens on the real current UTC date.
    //
    // Bracketed by two independent clock readings so a run that straddles UTC
    // midnight names both permitted days rather than flaking. That is still an
    // exact claim — a set of at most two dates, never "some date".
    const before = utcToday();
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });
    const after = utcToday();
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

    if (errors.length) fail(`browser errors: ${errors.join(" | ")}`);
    console.log(`  ${viewport.label}: calendar opens on ${today}, `
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

// (5) Structural: no calendar date may be named in app.js at all.
//
// The three sites this gate exists for were all plain "2026-09-05" string
// literals, and a re-introduced one is invisible until real time walks past
// it. The behavioural checks above catch a literal only while it differs from
// today; this catches it on the day it is written, and on every other file in
// app.js that might grow one.
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
  checkNoCalendarDateLiteral();
  const browser = await chromium.launch();
  try {
    for (const viewport of VIEWPORTS) await checkViewport(browser, viewport);
  } finally {
    await browser.close();
  }
  console.log("calendar-today: OK");
}

main().catch((e) => { console.error(e.message); process.exit(1); });
