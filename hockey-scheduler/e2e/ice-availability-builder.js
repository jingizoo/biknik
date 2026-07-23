// Ice Availability Builder + Arena Calendar month view (#158).
//
// At desktop and 390px, an arena operator builds a draft ice inventory from a
// recurring weekly template, previews it, and idempotently commits it. The
// journey verifies, on the real UI:
//   * the Arena Calendar has a Month view that renders a day grid;
//   * the builder previews the correct slot count for a Tue/Thu block in the
//     selected date range, and reports rinks whose Venue lacks SeasonVenueAccess
//     (never generating ice for them);
//   * committing creates exactly the previewed AVAILABLE Game ice;
//   * re-running the same template is idempotent — zero new, all duplicates,
//     and the create button is disabled;
//   * an exclusion date is honored (fewer slots, and it is reported);
//   * each selected weekday carries its OWN local start/end time (#158 flow):
//     a narrower Thursday window yields fewer Thursday games than Tuesday;
//   * commit is bound to the preview: editing the template after Preview drops
//     the preview + Create, so an edited form can never be committed.
//
// September 2026 has Tuesdays 1,8,15,22,29 and Thursdays 3,10,17,24 = 9 days;
// a 18:00-22:00 window with 60-minute games + 15-minute turnover yields 3 games
// per day => 27 slots on one accessible rink.
//
// Fails on any browser console/page error.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
const EXPECTED_NEW = 27;               // 9 Tue/Thu days * 3 games
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8283 },
  { label: "phone", width: 390, height: 844, port: 8284 },
];

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

// Fill the builder date range, then Preview and wait for the panel to reflect it.
async function preview(page) {
  await page.fill("#ib-from", "2026-09-01");
  await page.fill("#ib-to", "2026-09-30");
  // Drop any prior preview panel so the wait blocks for the FRESH render rather
  // than matching a stale one (each Preview fully re-renders #content).
  await page.evaluate(() => { const p = document.querySelector(".ib-preview"); if (p) p.remove(); });
  await page.click("[data-ib-preview]");
  await page.waitForSelector(".ib-preview, .banner.warn", { timeout: 15000 });
}

function previewState(page) {
  return page.evaluate(() => {
    const p = document.querySelector(".ib-preview");
    if (!p) return { error: !!document.querySelector(".banner.warn") };
    const commit = document.querySelector("[data-ib-commit]");
    return {
      new: +p.getAttribute("data-ib-new"),
      duplicate: +p.getAttribute("data-ib-duplicate"),
      conflict: +p.getAttribute("data-ib-conflict"),
      accessMissing: +p.getAttribute("data-ib-access-missing"),
      skipped: +p.getAttribute("data-ib-skipped"),
      commitDisabled: commit ? commit.disabled : null,
    };
  });
}

async function checkViewport(browser, viewport) {
  const base = `http://${HOST}:${viewport.port}`;
  const server = spawn(
    process.env.PYTHON || "python3",
    ["-u", "-m", "hockey_scheduler.web.server", "--host", HOST, "--port", String(viewport.port)],
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
  page.on("console", (m) => { if (m.type() === "error") errors.push(`[console] ${m.text()}`); });
  const fail = (msg) => { throw new Error(`[${viewport.label}] ${msg}`); };

  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });

    // One accessible rink (venue granted to the Season) and one whose Venue is
    // NOT granted, to exercise the venue-access report.
    const ids = await page.evaluate(async () => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      const league = await post("/api/setup/league", { name: "AHL" });
      const season = await post("/api/setup/season", { league_id: league.id, name: "Fall 2026" });
      const venue = await post("/api/setup/venue", { name: "Main Arena", league_id: league.id });
      await post(`/api/v2/setup/seasons/${season.id}/venue-access`, { venue_id: venue.id });
      const rink = await post("/api/setup/rink", { venue_id: venue.id, name: "Rink A" });
      // A second venue with NO season access.
      const venue2 = await post("/api/setup/venue", { name: "Annex", league_id: league.id });
      const rink2 = await post("/api/setup/rink", { venue_id: venue2.id, name: "Annex Ice" });
      return { season: season.id, rink: rink.id, rink2: rink2.id };
    });

    await page.click('.tab[data-tab="calendar"]');
    await page.waitForSelector('[data-mode="month"]', { state: "visible", timeout: 10000 });

    // (A) Month view renders a day grid.
    await page.click('[data-mode="month"]');
    await page.waitForSelector(".mo-grid .mo-cell", { timeout: 10000 });
    const cellCount = await page.$$eval(".mo-grid .mo-cell", (els) => els.length);
    if (cellCount !== 42) fail(`month grid should have 42 day cells, got ${cellCount}`);

    // (B) Open the builder and select BOTH rinks (one lacks venue access).
    await page.click("[data-ice-builder-open]");
    await page.waitForSelector(".ib-form", { timeout: 10000 });
    await page.check(`.ib-rink[value="${ids.rink}"]`);
    await page.check(`.ib-rink[value="${ids.rink2}"]`);
    await preview(page);
    let s = await previewState(page);
    if (s.new !== EXPECTED_NEW) fail(`expected ${EXPECTED_NEW} new slots, got ${JSON.stringify(s)}`);
    if (s.accessMissing !== 1) fail(`expected 1 access-missing rink, got ${JSON.stringify(s)}`);
    if (s.commitDisabled !== false) fail(`commit should be enabled with new slots: ${JSON.stringify(s)}`);
    const warn = await page.$(".ib-warn");
    if (!warn) fail("venue-access warning should be shown for the un-granted rink");

    // (C) Commit creates exactly the accessible rink's slots.
    await page.click("[data-ib-commit]");
    await page.waitForFunction(
      () => document.body.dataset.view === "calendar" && !document.querySelector(".ib-form"),
      null, { timeout: 10000 });
    const created = await page.evaluate(async (rink) => {
      const ov = await (await fetch("/api/demo/overview", { credentials: "same-origin" })).json();
      return (ov.ice_slots || []).filter((x) => x.rink_id === rink).length;
    }, ids.rink);
    if (created !== EXPECTED_NEW) fail(`expected ${EXPECTED_NEW} committed slots, got ${created}`);

    // (D) Idempotent rerun: zero new, all duplicates, commit disabled.
    await page.click("[data-ice-builder-open]");
    await page.waitForSelector(".ib-form", { timeout: 10000 });
    await page.check(`.ib-rink[value="${ids.rink}"]`);
    await preview(page);
    s = await previewState(page);
    if (s.new !== 0 || s.duplicate !== EXPECTED_NEW) {
      fail(`rerun should be idempotent (0 new / ${EXPECTED_NEW} dup), got ${JSON.stringify(s)}`);
    }
    if (s.commitDisabled !== true) fail(`commit should be disabled on an all-duplicate preview: ${JSON.stringify(s)}`);

    // (E) Exclusion date is honored and reported. Use a FRESH accessible rink so
    // the excluded run isn't all duplicates from (C), then exclude one Tuesday.
    const rink3 = await page.evaluate(async (season) => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      const venue = await post("/api/setup/venue", { name: "West", league_id: null });
      await post(`/api/v2/setup/seasons/${season}/venue-access`, { venue_id: venue.id });
      return (await post("/api/setup/rink", { venue_id: venue.id, name: "West Ice" })).id;
    }, ids.season);
    // Adding the exclusion re-renders the builder, which refetches the overview
    // and surfaces the new rink's checkbox.
    await page.fill("#ib-excl", "2026-09-08");
    await page.click("[data-ib-excl-add]");
    await page.waitForSelector(`.ib-rink[value="${rink3}"]`, { timeout: 10000 });
    await page.uncheck(`.ib-rink[value="${ids.rink}"]`);
    await page.check(`.ib-rink[value="${rink3}"]`);
    await preview(page);
    s = await previewState(page);
    if (s.new !== EXPECTED_NEW - 3) fail(`exclusion should drop 3 slots (=> ${EXPECTED_NEW - 3}), got ${JSON.stringify(s)}`);
    if (s.skipped < 1) fail(`the excluded date should be reported as skipped: ${JSON.stringify(s)}`);

    // (F) Per-weekday windows: each selected day carries its OWN local start/end
    // time. Open a fresh builder on a clean rink, keep Tuesday 18:00-22:00 (3
    // games) but narrow Thursday to 18:00-20:00 (1 game). September 2026 has 5
    // Tuesdays + 4 Thursdays, so per-weekday windows yield 5*3 + 4*1 = 19 —
    // where a single uniform block would be 27. Proves the per-day times reach
    // the planner and are not collapsed into one global window.
    const rink4 = await page.evaluate(async (season) => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      const venue = await post("/api/setup/venue", { name: "East", league_id: null });
      await post(`/api/v2/setup/seasons/${season}/venue-access`, { venue_id: venue.id });
      return (await post("/api/setup/rink", { venue_id: venue.id, name: "East Ice" })).id;
    }, ids.season);
    await page.click("[data-ib-cancel]");
    await page.waitForSelector("[data-ice-builder-open]", { timeout: 10000 });
    await page.click("[data-ice-builder-open]");
    await page.waitForSelector(".ib-form", { timeout: 10000 });
    await page.check(`.ib-rink[value="${rink4}"]`);
    // Tue (weekday 1) and Thu (weekday 3) are checked by default; each renders
    // its own start/end inputs. Narrow only Thursday's window.
    await page.fill("#ib-end-3", "20:00");
    await preview(page);
    s = await previewState(page);
    if (s.new !== 19) fail(`per-weekday windows should yield 19 new (5*3 Tue + 4*1 Thu), got ${JSON.stringify(s)}`);

    // (G) Commit is bound to the preview: editing the template AFTER Preview
    // drops the preview and its Create button, so a form edited post-preview can
    // never be committed (the server also rejects a mismatched fingerprint). All
    // template fields share one invalidation listener; editing a weekday time
    // exercises it. Use a fresh rink so "zero committed" is unambiguous.
    const rink5 = await page.evaluate(async (season) => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      const venue = await post("/api/setup/venue", { name: "North", league_id: null });
      await post(`/api/v2/setup/seasons/${season}/venue-access`, { venue_id: venue.id });
      return (await post("/api/setup/rink", { venue_id: venue.id, name: "North Ice" })).id;
    }, ids.season);
    await page.click("[data-ib-cancel]");
    await page.waitForSelector("[data-ice-builder-open]", { timeout: 10000 });
    await page.click("[data-ice-builder-open]");
    await page.waitForSelector(".ib-form", { timeout: 10000 });
    await page.check(`.ib-rink[value="${rink5}"]`);
    await preview(page);
    s = await previewState(page);
    if (s.new < 1) fail(`bind test needs a preview with slots, got ${JSON.stringify(s)}`);
    if (s.commitDisabled !== false) fail(`Create should be enabled on a fresh preview: ${JSON.stringify(s)}`);
    // Edit the Thursday end time AFTER previewing -> the preview (and Create) go.
    await page.fill("#ib-end-3", "20:30");
    await page.waitForSelector(".ib-preview", { state: "detached", timeout: 10000 });
    if (await page.$("[data-ib-commit]")) fail("Create must be gone after editing the template post-preview");
    const boundCommitted = await page.evaluate(async (rink) => {
      const ov = await (await fetch("/api/demo/overview", { credentials: "same-origin" })).json();
      return (ov.ice_slots || []).filter((x) => x.rink_id === rink).length;
    }, rink5);
    if (boundCommitted !== 0) fail(`an invalidated preview must commit nothing, got ${boundCommitted}`);

    if (errors.length) fail(`console/page errors:\n${errors.join("\n")}`);
    console.log(`[${viewport.label}] OK — month grid renders; builder previews ${EXPECTED_NEW} slots, reports un-granted venue, commits idempotently, honors exclusions, applies per-weekday windows (narrow Thursday => 19), and binds commit to the preview (edit invalidates it).`);
  } catch (error) {
    throw new Error(`${error.message}\n--- demo server output ---\n${serverOutput}`);
  } finally {
    await context.close();
    await stopServer(server);
  }
}

async function main() {
  let browser;
  try {
    browser = await chromium.launch(
      process.env.SMOKE_CHROMIUM_PATH ? { executablePath: process.env.SMOKE_CHROMIUM_PATH } : {});
    for (const viewport of VIEWPORTS) await checkViewport(browser, viewport);
    console.log("Ice Availability Builder browser journey passed.");
  } catch (error) {
    console.error("Ice Availability Builder browser journey FAILED.");
    console.error(error && error.message ? error.message : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
