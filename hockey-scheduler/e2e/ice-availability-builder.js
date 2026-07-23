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
//     the preview + Create, so an edited form can never be committed;
//   * a stale preview (its resolved snapshot moved) is refused by the server
//     and the UI re-previews the current proposal instead of writing it;
//   * an exact-tuple collision with existing incompatible ice (e.g. a
//     maintenance slot) is reported as a conflict, never hidden as capacity.
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
  page.on("console", (m) => {
    if (m.type() !== "error") return;
    const text = m.text();
    // Browser network-status noise for a 4xx the app handles gracefully (e.g. a
    // refused stale-preview commit in step H) is not a page bug — the functional
    // assertions below catch real breakage. Keep genuine JS console errors.
    if (/Failed to load resource/i.test(text)) return;
    errors.push(`[console] ${text}`);
  });
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
      return { league: league.id, season: season.id, rink: rink.id, rink2: rink2.id };
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

    // (H) A STALE preview is refused and refreshed. Simulate the resolved
    // snapshot moving under the operator (a concurrent Season/timezone edit
    // invalidates the stored fingerprint) by staling it; the server rejects the
    // commit and the UI re-previews the current proposal instead of writing the
    // stale set. (The service/HTTP suites drive the real Season/tz change.)
    const rink6 = await page.evaluate(async (season) => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      const venue = await post("/api/setup/venue", { name: "South", league_id: null });
      await post(`/api/v2/setup/seasons/${season}/venue-access`, { venue_id: venue.id });
      return (await post("/api/setup/rink", { venue_id: venue.id, name: "South Ice" })).id;
    }, ids.season);
    await page.click("[data-ib-cancel]");
    await page.waitForSelector("[data-ice-builder-open]", { timeout: 10000 });
    await page.click("[data-ice-builder-open]");
    await page.waitForSelector(".ib-form", { timeout: 10000 });
    await page.check(`.ib-rink[value="${rink6}"]`);
    await preview(page);
    s = await previewState(page);
    if (s.commitDisabled !== false) fail(`refresh test needs an enabled Create: ${JSON.stringify(s)}`);
    await page.evaluate(() => { iceBuilder.preview.template_fingerprint = "staledeadbeef00"; });
    await page.click("[data-ib-commit]");
    // The commit is refused and the UI re-previews the current proposal — its
    // toast announces the refresh (only the preview_mismatch branch sets it).
    await page.waitForFunction(
      () => /changed since preview/i.test((document.querySelector(".toast-msg") || {}).textContent || ""),
      null, { timeout: 10000 });
    // Crucially, the stale commit wrote nothing, and the builder stayed open
    // (a successful commit would have closed it back to the calendar).
    await page.waitForSelector(".ib-form", { timeout: 10000 });
    const staleCommitted = await page.evaluate(async (rink) => {
      const ov = await (await fetch("/api/demo/overview", { credentials: "same-origin" })).json();
      return (ov.ice_slots || []).filter((x) => x.rink_id === rink).length;
    }, rink6);
    if (staleCommitted !== 0) fail(`a refused stale commit must write nothing, got ${staleCommitted}`);

    // (H2) A same-slot-set template edit that slips past the frontend's
    // invalidation listener is still caught by the SERVER token (#158 review):
    // the token binds the whole reviewed payload, not just the generated tuples.
    // Preview, then extend the window END by 5 min (22:00 -> 22:05) by setting the
    // inputs' value WITHOUT firing `change` — so the stored preview/fingerprint
    // survive as if the edit slipped the suspenders. The same three slots/day
    // still fit, so a tuple-only token would have committed the unreviewed window;
    // the full-payload binding moves the fingerprint, the server refuses the
    // stale commit, and the UI re-previews the CURRENT (22:05) proposal — same
    // slot count, new token — which then commits.
    const rink6b = await page.evaluate(async (season) => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      const venue = await post("/api/setup/venue", { name: "West", league_id: null });
      await post(`/api/v2/setup/seasons/${season}/venue-access`, { venue_id: venue.id });
      return (await post("/api/setup/rink", { venue_id: venue.id, name: "West Ice" })).id;
    }, ids.season);
    await page.click("[data-ib-cancel]");
    await page.waitForSelector("[data-ice-builder-open]", { timeout: 10000 });
    await page.click("[data-ice-builder-open]");
    await page.waitForSelector(".ib-form", { timeout: 10000 });
    await page.check(`.ib-rink[value="${rink6b}"]`);
    await preview(page);
    const before = await previewState(page);
    if (!(before.new >= 1) || before.commitDisabled !== false) {
      fail(`same-slot edit test needs an enabled preview with slots: ${JSON.stringify(before)}`);
    }
    // Extend BOTH selected days' end time WITHOUT a change event (the listener
    // that would drop the preview never fires), so the real fingerprint is stale.
    await page.evaluate(() => {
      for (const el of document.querySelectorAll(".ib-wd-end")) el.value = "22:05";
    });
    await page.click("[data-ib-commit]");
    // Refused (same slots, but the reviewed window moved) -> refresh toast.
    await page.waitForFunction(
      () => /changed since preview/i.test((document.querySelector(".toast-msg") || {}).textContent || ""),
      null, { timeout: 10000 });
    const refreshed = await previewState(page);
    if (refreshed.new !== before.new) {
      fail(`the refreshed preview should show the SAME slot count (same tuples): ${before.new} -> ${refreshed.new}`);
    }
    if (refreshed.commitDisabled !== false) fail("Create should be enabled after the refresh");
    // The refreshed token (for the 22:05 window) now commits exactly those slots.
    await page.click("[data-ib-commit]");
    await page.waitForSelector("[data-ice-builder-open]", { timeout: 10000 });
    const editCommitted = await page.evaluate(async (rink) => {
      const ov = await (await fetch("/api/demo/overview", { credentials: "same-origin" })).json();
      return (ov.ice_slots || []).filter((x) => x.rink_id === rink).length;
    }, rink6b);
    if (editCommitted !== before.new) {
      fail(`the re-previewed edit should commit its ${before.new} slots, got ${editCommitted}`);
    }
    // The successful commit closed the builder; reopen it so the next step starts
    // from the shared "builder open" invariant (each step cancels the open builder
    // then reopens with a fresh rink).
    await page.click("[data-ice-builder-open]");
    await page.waitForSelector(".ib-form", { timeout: 10000 });

    // (I) An exact-tuple collision with EXISTING incompatible ice is REPORTED as
    // a conflict, never hidden as duplicate capacity (#158 review). A
    // maintenance slot at the builder's first window (Sep 1 18:00-19:00, program
    // tz UTC) collides exactly with a generated tuple; the display path is the
    // same the ALLOCATED-active-Game case (covered by the service + HTTP suites)
    // takes. The collided window must show as a conflict and drop out of the new
    // count, not be silently counted as idempotent capacity.
    const rink7 = await page.evaluate(async (season) => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      const venue = await post("/api/setup/venue", { name: "West End", league_id: null });
      await post(`/api/v2/setup/seasons/${season}/venue-access`, { venue_id: venue.id });
      const rink = (await post("/api/setup/rink", { venue_id: venue.id, name: "West End Ice" })).id;
      await post("/api/setup/ice-slot", {
        rink_id: rink, start_time: "2026-09-01T18:00:00+00:00",
        end_time: "2026-09-01T19:00:00+00:00", slot_type: "maintenance" });
      return rink;
    }, ids.season);
    await page.click("[data-ib-cancel]");
    await page.waitForSelector("[data-ice-builder-open]", { timeout: 10000 });
    await page.click("[data-ice-builder-open]");
    await page.waitForSelector(".ib-form", { timeout: 10000 });
    await page.check(`.ib-rink[value="${rink7}"]`);
    await preview(page);
    s = await previewState(page);
    if (s.conflict < 1) fail(`the exact maintenance collision must be a conflict: ${JSON.stringify(s)}`);
    if (s.new !== EXPECTED_NEW - 1) fail(`the collided window must NOT be counted as capacity (expected ${EXPECTED_NEW - 1} new): ${JSON.stringify(s)}`);
    if (!(await page.$(".ib-warn"))) fail("a conflict warning should be visible for the collision");
    // The maintenance collision's row is now listed with its exact target (not a
    // bare count): every conflict is individually reviewable before commit.
    const maintConflict = await page.$eval(
      ".ib-slot-conflict", (el) => el.textContent).catch(() => null);
    if (!maintConflict || !/maintenance/i.test(maintConflict)) {
      fail(`the maintenance conflict row must show its exact target, got ${JSON.stringify(maintConflict)}`);
    }

    // (J) A season-long template (>60 generated days) with an existing-Game
    // conflict LATE in the range must expose EVERY row — the final generated day
    // and the exact Game collision (its target Game id) — not just the first 60
    // days or a bare count (#158 review). The "AHL" program tz is UTC, so the
    // seeded Game's slot tuple and a generated window coincide exactly.
    const long = await page.evaluate(async (ctx) => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      const venue = await post("/api/setup/venue", { name: "Long Range", league_id: null });
      await post(`/api/v2/setup/seasons/${ctx.season}/venue-access`, { venue_id: venue.id });
      const rink = (await post("/api/setup/rink", { venue_id: venue.id, name: "Long Ice" })).id;
      // Seed a Game on a slot LATE in the range (Nov 1 = day 62 of a Sep 1
      // start), so its conflict row falls BEYOND the old 60-day cap. An
      // exhibition game needs only active season participation (no grouping
      // league), keeping the setup minimal.
      const club = await post("/api/setup/club", { name: "Range Club" });
      const division = await post("/api/setup/division", { season_id: ctx.season, name: "Range Div" });
      const mk = async (name) => (await post("/api/setup/team",
        { club_id: club.id, division_id: division.id, name, league_id: ctx.league })).id;
      const home = await mk("Range Home");
      const away = await mk("Range Away");
      await post(`/api/setup/seasons/${ctx.season}/team-registrations`, { team_id: home, division_id: division.id });
      await post(`/api/setup/seasons/${ctx.season}/team-registrations`, { team_id: away, division_id: division.id });
      const slot = await post("/api/setup/ice-slot", {
        rink_id: rink, start_time: "2026-11-01T18:00:00+00:00",
        end_time: "2026-11-01T19:00:00+00:00", slot_type: "game" });
      const g = await post("/api/setup/game", {
        season_id: ctx.season, division_id: division.id, home_team_id: home,
        away_team_id: away, ice_slot_id: slot.id, game_type: "exhibition" });
      return { rink, game: (g && (g.id || (g.game && g.game.id))) || null };
    }, { season: ids.season, league: ids.league });
    if (!long.game) fail("failed to seed the conflicting Game for the long-range preview");

    await page.click("[data-ib-cancel]");
    await page.waitForSelector("[data-ice-builder-open]", { timeout: 10000 });
    await page.click("[data-ice-builder-open]");
    await page.waitForSelector(".ib-form", { timeout: 10000 });
    // Select EVERY weekday. The weekday inputs are visually-hidden custom toggles
    // (their label is the click target), so set them in the DOM and fire ONE
    // change — the builder's listener reads all boxes, updates state and
    // re-renders with each day's window row. Do this BEFORE the rink so the rink
    // check survives that re-render.
    await page.evaluate(() => {
      const boxes = Array.from(document.querySelectorAll(".ib-weekday"));
      boxes.forEach((cb) => { cb.checked = true; });
      if (boxes[0]) boxes[0].dispatchEvent(new Event("change", { bubbles: true }));
    });
    await page.waitForSelector(".ib-form", { timeout: 10000 });
    await page.check(`.ib-rink[value="${long.rink}"]`);
    await page.fill("#ib-from", "2026-09-01");
    await page.fill("#ib-to", "2026-11-05");        // 66 distinct days, every weekday
    await page.evaluate(() => { const p = document.querySelector(".ib-preview"); if (p) p.remove(); });
    await page.click("[data-ib-preview]");
    await page.waitForSelector(".ib-preview", { timeout: 15000 });
    const lp = await page.evaluate(() => {
      const p = document.querySelector(".ib-preview");
      const last = p.querySelector("[data-ib-last-day]");
      const conflict = p.querySelector("[data-ib-conflict-game]");
      return {
        days: +p.getAttribute("data-ib-days"),
        newCount: +p.getAttribute("data-ib-new"),
        conflict: +p.getAttribute("data-ib-conflict"),
        lastDayDate: p.getAttribute("data-ib-last-day-date"),
        lastDayRowDate: last ? last.getAttribute("data-ib-day") : null,
        conflictGame: conflict ? conflict.getAttribute("data-ib-conflict-game") : null,
        conflictDay: conflict ? conflict.closest(".ib-day-row").getAttribute("data-ib-day") : null,
      };
    });
    if (lp.days <= 60) fail(`the long-range preview must generate >60 days, got ${JSON.stringify(lp)}`);
    if (lp.lastDayDate !== "2026-11-05" || lp.lastDayRowDate !== "2026-11-05") {
      fail(`the final generated day (2026-11-05) must be reviewable, got ${JSON.stringify(lp)}`);
    }
    if (lp.conflictGame !== long.game) {
      fail(`the exact Game conflict target must be visible, got ${JSON.stringify(lp)} (game ${long.game})`);
    }
    if (lp.conflictDay !== "2026-11-01") {
      fail(`the conflict on a day beyond the old 60-cap must be reviewable, got ${JSON.stringify(lp)}`);
    }
    // Commit stays bound to the COMPLETE preview: it creates exactly the new rows
    // across the WHOLE range (the late Game collision skipped), not a truncated
    // 60-day subset.
    await page.click("[data-ib-commit]");
    await page.waitForFunction(
      () => document.body.dataset.view === "calendar" && !document.querySelector(".ib-form"),
      null, { timeout: 15000 });
    const longCreated = await page.evaluate(async (rink) => {
      const ov = await (await fetch("/api/demo/overview", { credentials: "same-origin" })).json();
      return (ov.ice_slots || []).filter((x) => x.rink_id === rink && !x.game_id).length;
    }, long.rink);
    if (longCreated !== lp.newCount) {
      fail(`commit must create the FULL previewed set (${lp.newCount}), got ${longCreated}`);
    }

    if (errors.length) fail(`console/page errors:\n${errors.join("\n")}`);
    console.log(`[${viewport.label}] OK — month grid renders; builder previews ${EXPECTED_NEW} slots, reports un-granted venue, commits idempotently, honors exclusions, applies per-weekday windows (narrow Thursday => 19), binds commit to the preview (edit invalidates it), refuses+refreshes a stale preview (both a bogus token and a same-slot-set window edit that slips the suspenders), reports an exact-tuple collision as a conflict WITH its target, and exposes every row of a >60-day template — the final day and a late Game collision's exact target — while committing the full previewed set.`);
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
