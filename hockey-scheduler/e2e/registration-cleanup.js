// Inactive-registration cleanup browser journey (#251) — the frontend half of
// the hidden-inactive-registration bug fixed on the backend in PR #252.
// unregister_team_from_season() only deactivates a SeasonTeamRegistration
// (Season/Team/League/Division identity retained for history); those rows
// were previously invisible in the UI and silently blocked League/Season/
// Team deletes forever, with no way to resolve them.
//
// At desktop and 390px, through the real Setup UI (Season participation
// panel, id="season-participation"):
//   (1) register two teams into a League (one with a Division, one without),
//       remove both from the Season — the panel now shows an "Inactive
//       registrations" section listing both, each with Team/League/optional
//       Division/registration id and a permanent-cleanup trash action;
//   (2) Program/Season/League delete buttons exist in this panel (not just
//       the Competition structure tree above it);
//   (3) permanently clean both inactive rows via their trash action, then
//       delete the now-empty League from this same panel — success;
//   (4) a SEPARATE, still game-backed inactive registration stays blocked
//       with an actionable message (no "remove or reassign" dead end), zero
//       mutation.
// Fails on any browser console/page error.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
const CAL_DAY = "2026-10-05";
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8201 },
  { label: "phone", width: 390, height: 844, port: 8202 },
];

function waitForServer(url, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const attempt = () => {
      const request = http.get(url, (r) => { r.resume(); resolve(); });
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
  const consoleErrorHandler = (m) => { if (m.type() === "error") errors.push(`[console] ${m.text()}`); };
  page.on("console", consoleErrorHandler);

  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });

    // Fixture prerequisites — proven elsewhere, built via raw fetch so this
    // journey's UI interactions stay focused on the new inactive-registration
    // surface. Two Leagues under one Season/Program: `leagueClean` (the main
    // cleanup scenario) and `leagueGame` (a separate, game-backed scenario)
    // so deleting leagueClean is never blocked by leagueGame's registrations.
    const fx = await page.evaluate(async (d) => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      const org = await post("/api/v2/setup/organization", { name: "Reg Cleanup Org" });
      const program = await post("/api/v2/setup/program",
        { name: "Reg Cleanup Program", operator_organization_id: org.id });
      const season = await post("/api/v2/setup/season", { program_id: program.id, name: "2027-28" });
      const leagueClean = await post("/api/v2/setup/league", { season_id: season.id, name: "Clean League" });
      const leagueGame = await post("/api/v2/setup/league", { season_id: season.id, name: "Game League" });
      const division = await post("/api/v2/setup/division",
        { league_id: leagueClean.id, name: "Clean Division" });
      const club = await post("/api/v2/setup/club", { name: "Reg Cleanup Club" });
      const team = async (name) =>
        (await post("/api/v2/setup/team", { program_id: program.id, club_id: club.id, name })).id;
      const teamA = await team("Cleanup Team A");
      const teamB = await team("Cleanup Team B");
      const teamGameHome = await team("Game Home");
      const teamGameAway = await team("Game Away");

      // Fixture for scenario (4): a cancelled Game under leagueGame, both its
      // teams then removed from the season (a Game still blocks even with
      // zero active registrations left, and there is no available action).
      const venue = await post("/api/v2/setup/venue", { name: "V", organization_id: org.id });
      // The temporary Venue→Program compatibility bridge (#233, deferred to
      // Slice E) — create_game requires the ice slot's venue to already
      // carry a league_id. Out of this journey's scope (proven elsewhere).
      await post(`/api/setup/venue/${venue.id}/assign-league`, { league_id: program.id });
      const rink = await post("/api/v2/setup/rink", { venue_id: venue.id, name: "R" });
      const slot = await post("/api/v2/setup/ice-slot", {
        rink_id: rink.id, start_time: `${d}T18:00:00+00:00`, end_time: `${d}T19:00:00+00:00`,
        slot_type: "game",
      });
      const regGameHome = await post(`/api/v2/setup/seasons/${season.id}/team-registrations`,
        { team_id: teamGameHome, league_id: leagueGame.id });
      const regGameAway = await post(`/api/v2/setup/seasons/${season.id}/team-registrations`,
        { team_id: teamGameAway, league_id: leagueGame.id });
      const game = await post("/api/v2/setup/game", {
        season_id: season.id, league_id: leagueGame.id,
        home_team_id: teamGameHome, away_team_id: teamGameAway, ice_slot_id: slot.id,
      });
      await post(`/api/games/${game.id}/cancel`, {});
      await post(`/api/v2/setup/season-team-registration/${regGameHome.id}/remove`, {});

      return {
        program: program.id, season: season.id, leagueClean: leagueClean.id,
        leagueGame: leagueGame.id, division: division.id, teamA, teamB,
        regGameHome: regGameHome.id,
      };
    }, CAL_DAY);

    await page.click('.tab[data-tab="setup"]');
    await page.waitForFunction(
      (sel) => !!document.querySelector(sel), `#reg-team-${fx.leagueClean}`, { timeout: 15000 });

    // (1) Register both teams through the real Season participation panel —
    // teamA with a Division, teamB without (Division stays optional).
    const registerVia = async (teamId, divId) => {
      await page.selectOption(`#reg-team-${fx.leagueClean}`, teamId);
      await page.selectOption(`#reg-div-add-${fx.leagueClean}`, divId || "");
      const resp = page.waitForResponse((r) =>
        r.url() === `${base}/api/v2/setup/seasons/${fx.season}/team-registrations`
        && r.request().method() === "POST");
      await page.click(`[data-reg-add="${fx.leagueClean}"]`);
      const body = await (await resp).json();
      if (body.error) throw new Error(`[${viewport.label}] register failed: ${JSON.stringify(body.error)}`);
      return body.id;
    };
    const regA = await registerVia(fx.teamA, fx.division);
    await page.waitForFunction(
      (sel) => !!document.querySelector(sel), `#reg-team-${fx.leagueClean}`, { timeout: 15000 });
    const regB = await registerVia(fx.teamB, "");

    // Remove both from the season — they become inactive, not gone.
    for (const regId of [regA, regB]) {
      const resp = page.waitForResponse((r) =>
        r.url() === `${base}/api/v2/setup/season-team-registration/${regId}/remove`
        && r.request().method() === "POST");
      await page.click(`[data-reg-remove="${regId}"]`);
      if ((await resp).status() !== 200) throw new Error(`[${viewport.label}] remove ${regId} failed`);
    }

    // The Inactive registrations section under this Season now lists both
    // rows, each carrying Team/League/optional Division/registration id.
    await page.waitForSelector(
      `#season-participation [data-del="season-team-registration"][data-del-id="${regA}"]`,
      { timeout: 10000 });
    await page.waitForSelector(
      `#season-participation [data-del="season-team-registration"][data-del-id="${regB}"]`,
      { timeout: 10000 });
    const inactiveText = await page.textContent("#season-participation");
    if (!/Inactive registrations/i.test(inactiveText)
      || !/Cleanup Team A/.test(inactiveText) || !/Cleanup Team B/.test(inactiveText)
      || !/Clean League/.test(inactiveText) || !/Clean Division/.test(inactiveText)
      || !/inactive/i.test(inactiveText)) {
      throw new Error(`[${viewport.label}] inactive registrations section missing expected detail`);
    }

    // (2) Program/Season/League delete buttons exist in THIS panel, not just
    // the Competition structure tree above it.
    for (const [kind, id] of [
      ["league", fx.program], ["season", fx.season], ["level", fx.leagueClean]]) {
      await page.waitForSelector(
        `#season-participation [data-del="${kind}"][data-del-id="${id}"]`, { timeout: 10000 });
    }

    // (3) Permanently clean both inactive rows via the trash action.
    const cleanupOne = async (regId, teamName) => {
      await page.click(`#season-participation [data-del="season-team-registration"][data-del-id="${regId}"]`);
      await page.waitForSelector(".modal.danger [data-del-confirm]", { timeout: 10000 });
      const resp = page.waitForResponse((r) =>
        r.url() === `${base}/api/v2/setup/season-team-registration/${regId}/delete`
        && r.request().method() === "POST");
      await page.click("[data-del-confirm]");
      const body = await (await resp).json();
      if (body.error) throw new Error(`[${viewport.label}] cleanup ${regId} failed: ${JSON.stringify(body.error)}`);
      if (body.team_name !== teamName) {
        throw new Error(`[${viewport.label}] cleanup response missing team_name: ${JSON.stringify(body)}`);
      }
      await page.waitForSelector(".modal", { state: "detached", timeout: 10000 });
    };
    await cleanupOne(regA, "Cleanup Team A");
    await cleanupOne(regB, "Cleanup Team B");

    // Both rows are now fully gone, not just inactive.
    const afterCleanup = await page.evaluate(async (i) => {
      const get = async (p) => (await fetch(p, { credentials: "same-origin" })).json();
      const list = await get(`/api/v2/setup/seasons/${i.season}/team-registrations`);
      return (list.registrations || []).map((r) => r.id);
    }, { season: fx.season });
    if (afterCleanup.includes(regA) || afterCleanup.includes(regB)) {
      throw new Error(`[${viewport.label}] a cleaned registration still exists: ${JSON.stringify(afterCleanup)}`);
    }

    // The empty Division itself still blocks the League (proven separately
    // by division-delete-cleanup.js) — clear it via the API so this journey
    // stays focused on the registration-cleanup surface.
    const divDelResult = await page.evaluate(async (i) => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b || {}),
      })).json();
      return post(`/api/v2/setup/division/${i.division}/delete`, {});
    }, { division: fx.division });
    if (divDelResult.error) {
      throw new Error(`[${viewport.label}] Division cleanup failed: ${JSON.stringify(divDelResult.error)}`);
    }

    // Delete the now-empty League from the Season participation panel.
    await page.waitForSelector(
      `#season-participation [data-del="level"][data-del-id="${fx.leagueClean}"]`, { timeout: 10000 });
    await page.click(`#season-participation [data-del="level"][data-del-id="${fx.leagueClean}"]`);
    await page.waitForSelector(".modal.danger [data-del-confirm]", { timeout: 10000 });
    const leagueDelResp = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/league/${fx.leagueClean}/delete` && r.request().method() === "POST");
    await page.click("[data-del-confirm]");
    const leagueDelBody = await (await leagueDelResp).json();
    if (leagueDelBody.error) {
      throw new Error(`[${viewport.label}] League delete failed: ${JSON.stringify(leagueDelBody.error)}`);
    }
    await page.waitForSelector(".modal", { state: "detached", timeout: 10000 });
    const leagueGone = await page.evaluate(async (i) => {
      const get = async (p) => (await fetch(p, { credentials: "same-origin" })).json();
      const ov = await get("/api/v2/setup/overview");
      return !(ov.leagues || []).some((lv) => lv.id === i.leagueClean);
    }, { leagueClean: fx.leagueClean });
    if (!leagueGone) throw new Error(`[${viewport.label}] League still exists after delete`);

    // (4) A SEPARATE, game-backed inactive registration stays blocked with an
    // actionable message — never a dead-end "remove or reassign" that has no
    // corresponding action.
    await page.click('.tab[data-tab="setup"]');
    await page.waitForFunction(
      (sel) => !!document.querySelector(sel),
      `#season-participation [data-del="season-team-registration"][data-del-id="${fx.regGameHome}"]`,
      { timeout: 15000 });
    await page.click(
      `#season-participation [data-del="season-team-registration"][data-del-id="${fx.regGameHome}"]`);
    await page.waitForSelector(".modal.danger [data-del-confirm]", { timeout: 10000 });
    const blockedResp = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/season-team-registration/${fx.regGameHome}/delete`
      && r.request().method() === "POST");
    // Detach the console-error listener only for this deliberately-blocked
    // delete: the server's 409 response logs a benign "Failed to load
    // resource" Chromium console entry, not a real page bug (same pattern as
    // division-delete-cleanup.js / season-participation.js).
    page.off("console", consoleErrorHandler);
    await page.click("[data-del-confirm]");
    await blockedResp;
    await page.waitForSelector(".modal.blocked", { timeout: 10000 });
    page.on("console", consoleErrorHandler);
    const blockedText = await page.textContent(".modal.blocked");
    if (!/game/i.test(blockedText || "")) {
      throw new Error(`[${viewport.label}] blocked modal did not mention the Game reference: ${blockedText}`);
    }
    if (/remove or reassign/i.test(blockedText || "")) {
      throw new Error(`[${viewport.label}] blocked modal gave an unactionable "remove or reassign" `
        + `instruction for permanent Game history: ${blockedText}`);
    }
    await page.click(".modal.blocked [data-modal-close]");
    await page.waitForSelector(".modal", { state: "detached", timeout: 10000 });

    // Zero mutation on the blocked attempt.
    const stillThere = await page.evaluate(async (i) => {
      const get = async (p) => (await fetch(p, { credentials: "same-origin" })).json();
      const list = await get(`/api/v2/setup/seasons/${i.season}/team-registrations`);
      return (list.registrations || []).some((r) => r.id === i.reg);
    }, { season: fx.season, reg: fx.regGameHome });
    if (!stillThere) throw new Error(`[${viewport.label}] game-backed registration vanished despite the block`);

    if (errors.length) {
      throw new Error(`[${viewport.label}] console/page errors:\n${errors.join("\n")}`);
    }
    console.log(`[${viewport.label}] OK — inactive-registration cleanup UI verified.`);
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
    console.log("Inactive-registration cleanup browser journey passed.");
  } catch (error) {
    console.error("Inactive-registration cleanup browser journey FAILED.");
    console.error(error && error.message ? error.message : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
