// Scheduling policy — reserved-vs-playable display + turnover conflict copy +
// builder advisory (#277 Slice B follow-on).
//
// At desktop and 390px, an operator with a rink policy (5m warm-up + 10m
// resurfacing) sees, on the real UI:
//   * the day board's hosted slot card shows the RESERVED facility span
//     ("reserved … (+5m warm-up, +10m resurfacing)") around the playable time,
//     while a free slot shows no reserved line;
//   * moving the game onto an adjacent slot 10 minutes away is refused by the
//     placement gate and the conflict panel renders the operator copy
//     ("Turnover buffer conflict", required vs actual gap) — not raw backend
//     text;
//   * the Ice Availability Builder preview warns when the template's
//     between-slot turnover (0m) is smaller than the policy buffer (15m), via
//     the fingerprint-bound policy_notes row.
//
// Slots are seeded in the demo's pinned September 2026 month so the default
// month grid contains them. Fails on any browser console/page error.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8291 },
  { label: "phone", width: 390, height: 844, port: 8292 },
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

// This is the only spec here that genuinely NAVIGATES the calendar: it switches
// to month view and clicks a `[data-cal-day=…]` cell, so its anchor only has to
// share a MONTH with the grid on screen — not a day. It used to anchor on
// 2026-09-07 because the app pinned its calendar to September 2026; the app now
// opens on the real current date (#387/#389), so that cell is simply not in the
// grid. See anchorFor() below for what replaced it.
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
    // An intentionally-refused move surfaces as a failed fetch in the console;
    // like coach-scope/factory-reset, only treat OTHER console errors as bugs.
    if (m.type() === "error" && !/Failed to load resource/.test(m.text()))
      errors.push(`[console] ${m.text()}`);
  });

  const fail = (msg) => { throw new Error(`[${viewport.label}] ${msg}`); };

  const iso = (d) => d.toISOString().replace(/\.\d{3}Z$/, "+00:00");
  const plusMin = (d, m) => new Date(d.getTime() + m * 60 * 1000);
  // Month view, one step forward. Filled in once the page is up, from the
  // app's own calendarDate and its own addMonths, so the spec and the grid
  // can never disagree about which month "next" is.
  let dayKey, monthKey, t0;
  const anchorFor = async () => {
    monthKey = (await page.evaluate(() => addMonths(calendarDate, 1))).slice(0, 7);
    dayKey = `${monthKey}-07`;
    t0 = new Date(`${dayKey}T18:00:00Z`);
  };
  // Every calendar visit below goes through here: month view, then one "›"
  // step, so the grid on screen is the month the fixture was built in. A whole
  // month ahead also keeps the fixture unambiguously in the future at any hour
  // of any day, which a same-month anchor could not promise near month end.
  const openMonthGrid = async () => {
    await page.click('.tab[data-tab="calendar"]');
    await page.waitForSelector('[data-mode="month"]', { state: "visible", timeout: 10000 });
    await page.click('[data-mode="month"]');
    await page.click('[data-cal="1"]');
    const shown = await page.evaluate(() => calendarDate);
    if (shown.slice(0, 7) !== monthKey) {
      fail(`month grid shows ${shown.slice(0, 7)}, fixture is in ${monthKey}`);
    }
    await page.waitForSelector(".mo-grid .mo-cell", { timeout: 10000 });
  };

  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });
    await anchorFor();

    // Seed: one Division with two registered teams, one rink with a hosted
    // game slot and a second free slot 10 minutes after it, and a rink-scope
    // policy of 5m warm-up + 10m resurfacing (required gap 15 > actual 10).
    const ids = await page.evaluate(async ({ s1s, s1e, s2s, s2e, s3s, s3e,
                                             s4s, s4e }) => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      const league = await post("/api/setup/league", { name: "Policy League" });
      const season = await post("/api/setup/season", { league_id: league.id, name: "Policy Season" });
      const level = await post("/api/setup/level", { season_id: season.id, name: "Level" });
      const division = await post("/api/setup/division", { season_id: season.id, level_id: level.id, name: "Div P" });
      const club = await post("/api/setup/club", { name: "Club P" });
      const teamA = await post("/api/v2/setup/team", { club_id: club.id, league_id: level.id, name: "Aurora" });
      const teamB = await post("/api/v2/setup/team", { club_id: club.id, league_id: level.id, name: "Borealis" });
      const teamC = await post("/api/v2/setup/team", { club_id: club.id, league_id: level.id, name: "Cyclone" });
      const teamD = await post("/api/v2/setup/team", { club_id: club.id, league_id: level.id, name: "Drift" });
      const register = (teamId) => post(
        `/api/setup/seasons/${season.id}/team-registrations`,
        { team_id: teamId, division_id: division.id });
      await register(teamA.id);
      await register(teamB.id);
      await register(teamC.id);
      await register(teamD.id);
      const venue = await post("/api/setup/venue", { name: "Policy Arena", league_id: league.id });
      await post(`/api/v2/setup/seasons/${season.id}/venue-access`, { venue_id: venue.id });
      const rink = await post("/api/setup/rink", { venue_id: venue.id, name: "Policy Rink" });
      const slot1 = await post("/api/setup/ice-slot", { rink_id: rink.id, start_time: s1s, end_time: s1e });
      const slot2 = await post("/api/setup/ice-slot", { rink_id: rink.id, start_time: s2s, end_time: s2e });
      const slot3 = await post("/api/setup/ice-slot", { rink_id: rink.id, start_time: s3s, end_time: s3e });
      // Far from every other slot (>= 50 min), so a committed draft here
      // passes the turnover gate cleanly.
      const slot4 = await post("/api/setup/ice-slot", { rink_id: rink.id, start_time: s4s, end_time: s4e });
      const game = await post("/api/setup/game", {
        season_id: season.id, division_id: division.id,
        home_team_id: teamA.id, away_team_id: teamB.id, ice_slot_id: slot1.id });
      // The MOVABLE game, parked far away: moving it next to game 1 must hit
      // the turnover buffer (its own old slot frees, so a lone game never
      // conflicts with itself — the backend pins that; the conflict needs a
      // SECOND game to be adjacent to).
      const game2 = await post("/api/setup/game", {
        season_id: season.id, division_id: division.id,
        home_team_id: teamC.id, away_team_id: teamD.id, ice_slot_id: slot3.id });
      const policy = await post("/api/setup/scheduling-policy", {
        scope_type: "rink", scope_id: rink.id,
        warmup_minutes: 5, resurfacing_minutes: 10 });
      // The legacy v1 "league" IS a v2 Program under the shim (server.py's
      // POST /api/setup/league routes straight to api.create_program(), and
      // /api/setup/season passes its own league_id through as
      // create_season()'s program_id) -- select it as the active #159
      // context so defaultIceForm() resolves this Season without needing
      // its own now-removed global-first fallback (#331 review round 8:
      // defaultIceForm() fails CLOSED, the same way Import's own Season
      // select already does, when no Season is actively selected -- this
      // fixture must actively select one, not rely on a silent global
      // default that no longer exists).
      await post("/api/context", { program_id: league.id, season_id: season.id });
      return { rink: rink.id, slot1: slot1.id, slot2: slot2.id,
               slot4: slot4.id, division: division.id,
               game: game.id, gameError: game.error || null,
               game2: game2.id, game2Error: game2.error || null,
               policyError: policy.error || null };
    }, { s1s: iso(t0), s1e: iso(plusMin(t0, 60)),
         s2s: iso(plusMin(t0, 70)), s2e: iso(plusMin(t0, 130)),
         s3s: iso(plusMin(t0, 300)), s3e: iso(plusMin(t0, 360)),
         s4s: iso(plusMin(t0, 180)), s4e: iso(plusMin(t0, 240)) });
    if (ids.gameError) fail(`seed game failed: ${JSON.stringify(ids.gameError)}`);
    if (ids.game2Error) fail(`seed game 2 failed: ${JSON.stringify(ids.game2Error)}`);
    if (ids.policyError) fail(`seed policy failed: ${JSON.stringify(ids.policyError)}`);
    // The /api/context call above is a bare fetch, bypassing
    // setActiveContext() (the real switcher's own handler) entirely -- it
    // moves the SERVER's active context but leaves the already-loaded
    // page's own client-side contextOptions (fetched once, before this
    // fixture's Program even existed) none the wiser. A reload re-runs the
    // boot sequence's own loadContextOptions() so defaultIceForm() sees the
    // real selection below.
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });

    // (A) Day board: hosted slot card shows the reserved facility span;
    // the free slot card shows none.
    // Calendar defaults to Day view (today); month cells carry data-cal-day.
    await openMonthGrid();
    await page.waitForSelector(`[data-cal-day="${dayKey}"]`, { timeout: 10000 });
    await page.click(`[data-cal-day="${dayKey}"]`);
    await page.waitForSelector(".slot-card", { timeout: 10000 });
    const cards = await page.$$eval(".slot-card", (els) =>
      els.map((el) => el.textContent.replace(/\s+/g, " ").trim()));
    const reservedCards = cards.filter((t) => t.includes("reserved"));
    if (reservedCards.length !== 2)
      fail(`exactly the two hosted cards should show a reserved span, got ${JSON.stringify(cards)}`);
    if (!/\+5m warm-up, \+10m resurfacing/.test(reservedCards[0]))
      fail(`reserved line should name both buffers: ${reservedCards[0]}`);

    // (B) Moving the game to the adjacent slot (10 < 15 min away) is refused
    // with the operator copy, not raw backend text.
    await page.click(`[data-move-game="${ids.game2}"]`);
    await page.click(`[data-drop="${ids.slot2}"]`);
    await page.waitForSelector(".cal-aside.bad", { timeout: 10000 });
    const panelText = await page.evaluate(() =>
      document.body.textContent.replace(/\s+/g, " "));
    if (!panelText.includes("Turnover buffer conflict"))
      fail("conflict panel should use the turnover copy title");
    if (!panelText.includes("warm-up and resurfacing"))
      fail("conflict panel should explain the buffer in operator terms");
    if (!/needs 15 minutes between games/.test(panelText))
      fail("conflict panel should show the required gap");

    // (C) Builder preview warns when template turnover < policy buffer,
    // via the fingerprint-bound policy_notes.
    await page.click("[data-ice-builder-open]");
    await page.waitForSelector(".ib-form", { timeout: 10000 });
    await page.check(`.ib-rink[value="${ids.rink}"]`);
    await page.fill("#ib-turnover", "0");
    // The same seven days the fixture lives in, in the fixture's own month.
    await page.fill("#ib-from", `${monthKey}-01`);
    await page.fill("#ib-to", `${monthKey}-07`);
    await page.evaluate(() => { const p = document.querySelector(".ib-preview"); if (p) p.remove(); });
    await page.click("[data-ib-preview]");
    await page.waitForSelector(".ib-preview, .banner.warn", { timeout: 15000 });
    const warnText = await page.$$eval(".ib-warn", (els) =>
      els.map((el) => el.textContent.replace(/\s+/g, " ").trim()).join(" | "));
    if (!warnText.includes("resurfacing + warm-up"))
      fail(`builder should warn about the sub-requirement pair, got: ${warnText}`);
    if (!/needs 15 min/.test(warnText))
      fail(`builder warning should name the requirement, got: ${warnText}`);
    if (!/only 0 min apart/.test(warnText))
      fail(`builder warning should name the offending pair's real gap, got: ${warnText}`);

    // (D) A committed DRAFT physically reserves ice: the calendar card and
    // the scheduler review row show the same derived span, and discarding
    // the draft frees it everywhere (#319 review).
    const commit = await page.evaluate(async ({ division, slot4 }) => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      // #328 review round 5: Commit is bound to the exact preview it
      // reviewed, so a direct API call must Generate first.
      const preview = await post(
        "/api/scheduler/draft", { division_id: division, slot_ids: [slot4] });
      return post("/api/scheduler/commit", {
        division_id: division, slot_ids: [slot4],
        draft_fingerprint: preview.draft_fingerprint,
      });
    }, { division: ids.division, slot4: ids.slot4 });
    if (commit.error || (commit.created || []).length !== 1)
      fail(`draft commit should create exactly one game: ${JSON.stringify(commit)}`);
    const openDay = async () => {
      await openMonthGrid();
      await page.waitForSelector(`[data-cal-day="${dayKey}"]`, { timeout: 10000 });
      await page.click(`[data-cal-day="${dayKey}"]`);
      await page.waitForSelector(".slot-card", { timeout: 10000 });
      return page.$$eval(".slot-card", (els) =>
        els.map((el) => el.textContent.replace(/\s+/g, " ").trim()));
    };
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });
    const cardsAfterDraft = await openDay();
    const reservedAfterDraft = cardsAfterDraft.filter((t) => t.includes("reserved"));
    if (reservedAfterDraft.length !== 3)
      fail(`the committed draft's slot should add a third reserved card, got ${JSON.stringify(cardsAfterDraft)}`);
    await page.click('.tab[data-tab="scheduler"]');
    await page.waitForSelector(".li .slot-reserved", { timeout: 10000 });
    const reviewReserved = await page.$$eval(".li .slot-reserved", (els) =>
      els.map((el) => el.textContent.replace(/\s+/g, " ").trim()));
    if (!reviewReserved.some((t) => /\+5m warm-up, \+10m resurfacing/.test(t)))
      fail(`the review row should show the same reserved span: ${JSON.stringify(reviewReserved)}`);
    const discard = await page.evaluate(async () => {
      const r = await fetch("/api/scheduler/drafts/discard", {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ all: true }) });
      return r.json();
    });
    if (discard.error) fail(`discard failed: ${JSON.stringify(discard)}`);
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });
    const cardsAfterDiscard = await openDay();
    const reservedAfterDiscard = cardsAfterDiscard.filter((t) => t.includes("reserved"));
    if (reservedAfterDiscard.length !== 2)
      fail(`discard should free the draft's reserved span, got ${JSON.stringify(cardsAfterDiscard)}`);

    if (errors.length) fail(`browser errors: ${errors.join(" ;; ")}`);
    console.log(`[${viewport.label}] scheduling-policy journey OK`);
  } catch (err) {
    const output = serverOutput ? `\n--- server output ---\n${serverOutput}` : "";
    throw new Error(`${err.message}${output}`);
  } finally {
    await context.close();
    await stopServer(server);
  }
}

(async () => {
  const browser = await chromium.launch(
    process.env.SMOKE_CHROMIUM_PATH ? { executablePath: process.env.SMOKE_CHROMIUM_PATH } : {});
  try {
    for (const viewport of VIEWPORTS) {
      await checkViewport(browser, viewport);
    }
  } finally {
    await browser.close();
  }
  console.log("scheduling-policy e2e passed");
})().catch((err) => {
  console.error(err.message || err);
  process.exit(1);
});
