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

// The demo app pins its calendar to September 2026 (demo seed date
// 2026-09-05) — like scheduler-empty-state's fixed ICE_DAY, anchor there, on
// the 7th so this spec's slots never collide with the demo's own 09-05 ice.
function anchor() {
  return new Date("2026-09-07T18:00:00Z");
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
    // An intentionally-refused move surfaces as a failed fetch in the console;
    // like coach-scope/factory-reset, only treat OTHER console errors as bugs.
    if (m.type() === "error" && !/Failed to load resource/.test(m.text()))
      errors.push(`[console] ${m.text()}`);
  });

  const fail = (msg) => { throw new Error(`[${viewport.label}] ${msg}`); };

  const t0 = anchor();
  const iso = (d) => d.toISOString().replace(/\.\d{3}Z$/, "+00:00");
  const plusMin = (d, m) => new Date(d.getTime() + m * 60 * 1000);
  const dayKey = t0.toISOString().slice(0, 10);

  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });

    // Seed: one Division with two registered teams, one rink with a hosted
    // game slot and a second free slot 10 minutes after it, and a rink-scope
    // policy of 5m warm-up + 10m resurfacing (required gap 15 > actual 10).
    const ids = await page.evaluate(async ({ s1s, s1e, s2s, s2e, s3s, s3e }) => {
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
      return { rink: rink.id, slot1: slot1.id, slot2: slot2.id,
               game: game.id, gameError: game.error || null,
               game2: game2.id, game2Error: game2.error || null,
               policyError: policy.error || null };
    }, { s1s: iso(t0), s1e: iso(plusMin(t0, 60)),
         s2s: iso(plusMin(t0, 70)), s2e: iso(plusMin(t0, 130)),
         s3s: iso(plusMin(t0, 300)), s3e: iso(plusMin(t0, 360)) });
    if (ids.gameError) fail(`seed game failed: ${JSON.stringify(ids.gameError)}`);
    if (ids.game2Error) fail(`seed game 2 failed: ${JSON.stringify(ids.game2Error)}`);
    if (ids.policyError) fail(`seed policy failed: ${JSON.stringify(ids.policyError)}`);

    // (A) Day board: hosted slot card shows the reserved facility span;
    // the free slot card shows none.
    await page.click('.tab[data-tab="calendar"]');
    // Calendar defaults to Day view (today); month cells carry data-cal-day.
    await page.waitForSelector('[data-mode="month"]', { state: "visible", timeout: 10000 });
    await page.click('[data-mode="month"]');
    await page.waitForSelector(".mo-grid .mo-cell", { timeout: 10000 });
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
    await page.fill("#ib-from", "2026-09-01");
    await page.fill("#ib-to", "2026-09-07");
    await page.evaluate(() => { const p = document.querySelector(".ib-preview"); if (p) p.remove(); });
    await page.click("[data-ib-preview]");
    await page.waitForSelector(".ib-preview, .banner.warn", { timeout: 15000 });
    const warnText = await page.$$eval(".ib-warn", (els) =>
      els.map((el) => el.textContent.replace(/\s+/g, " ").trim()).join(" | "));
    if (!warnText.includes("warm-up + resurfacing"))
      fail(`builder should warn about the sub-buffer turnover, got: ${warnText}`);
    if (!/reserves 15 min/.test(warnText))
      fail(`builder warning should name the policy buffer, got: ${warnText}`);

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
