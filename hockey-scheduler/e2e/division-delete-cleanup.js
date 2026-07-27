// Division-deletion cleanup browser journey (#233 Slice D1/D2 bundled fix,
// #248) — the reported bug: a Division can show 0 ACTIVE teams in the Setup
// UI while a hidden INACTIVE SeasonTeamRegistration still points at it, and
// delete_division() used to treat that hidden row as a live blocker forever.
//
// At desktop and 390px, through the real Setup UI:
//   (1) register two teams into a Division, remove both from the Season (the
//       UI now shows 0 active teams under it), delete the Division, confirm
//       the success toast reports the cleaned-registration count, and verify
//       via the API that both registrations survive — inactive, division_id
//       cleared, Season/Team/League identity untouched;
//   (2) a SEPARATE Division with an active registration still shows the
//       blocked-dependency modal with zero mutation;
//   (3) a THIRD Division referenced only by a cancelled Game (zero active
//       registrations) also still blocks, zero mutation.
// Fails on any browser console/page error.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
const CAL_DAY = "2026-09-07";
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8191 },
  { label: "phone", width: 390, height: 844, port: 8192 },
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
    // journey's UI interactions stay focused on register/remove/delete.
    const fx = await page.evaluate(async (d) => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      const org = await post("/api/v2/setup/organization", { name: "Div Cleanup Org" });
      const program = await post("/api/v2/setup/program",
        { name: "Div Cleanup Program", operator_organization_id: org.id });
      const season = await post("/api/v2/setup/season", { program_id: program.id, name: "2026-27" });
      const league = await post("/api/v2/setup/league", { season_id: season.id, name: "Adult League" });
      const divClean = await post("/api/v2/setup/division", { league_id: league.id, name: "Cleanup Gold" });
      const divActive = await post("/api/v2/setup/division", { league_id: league.id, name: "Active Gold" });
      const divGame = await post("/api/v2/setup/division", { league_id: league.id, name: "Game Gold" });
      const club = await post("/api/v2/setup/club", { name: "Div Cleanup Club" });
      const team = async (name) =>
        (await post("/api/v2/setup/team", { league_id: league.id, club_id: club.id, name })).id;
      const team1 = await team("Cleanup Team A");
      const team2 = await team("Cleanup Team B");
      const teamActive = await team("Active Blocker Team");
      const teamGameHome = await team("Game Home");
      const teamGameAway = await team("Game Away");
      // Fixtures for scenario (3): a cancelled Game referencing divGame, both
      // its teams then removed from the season (a Game still blocks even
      // with zero active registrations left).
      const venue = await post("/api/v2/setup/venue", { name: "V", organization_id: org.id });
      // Game ice eligibility (#233 Slice E) requires the ice slot's venue to
      // hold an active SeasonVenueAccess grant for this Season. Out of this
      // journey's scope (proven elsewhere).
      await post(`/api/v2/setup/seasons/${season.id}/venue-access`, { venue_id: venue.id });
      const rink = await post("/api/v2/setup/rink", { venue_id: venue.id, name: "R" });
      const slot = await post("/api/v2/setup/ice-slot", {
        rink_id: rink.id, start_time: `${d}T18:00:00+00:00`, end_time: `${d}T19:00:00+00:00`,
        slot_type: "game",
      });
      const regHome = await post(`/api/v2/setup/seasons/${season.id}/team-registrations`,
        { team_id: teamGameHome, league_id: league.id, division_id: divGame.id });
      const regAway = await post(`/api/v2/setup/seasons/${season.id}/team-registrations`,
        { team_id: teamGameAway, league_id: league.id, division_id: divGame.id });
      const game = await post("/api/v2/setup/game", {
        season_id: season.id, league_id: league.id, division_id: divGame.id,
        home_team_id: teamGameHome, away_team_id: teamGameAway, ice_slot_id: slot.id,
      });
      await post(`/api/games/${game.id}/cancel`, {});
      await post(`/api/v2/setup/season-team-registration/${regHome.id}/remove`, {});
      await post(`/api/v2/setup/season-team-registration/${regAway.id}/remove`, {});

      return {
        program: program.id, season: season.id, league: league.id,
        divClean: divClean.id, divActive: divActive.id, divGame: divGame.id,
        team1, team2, teamActive,
      };
    }, CAL_DAY);

    await page.click('.tab[data-tab="setup"]');
    await page.waitForFunction(
      (sel) => !!document.querySelector(sel), `#reg-team-${fx.season}-${fx.league}`, { timeout: 15000 });

    // (1) Register two teams into divClean through the real Season
    // participation panel.
    const registerVia = async (teamId) => {
      await page.selectOption(`#reg-team-${fx.season}-${fx.league}`, teamId);
      await page.selectOption(`#reg-div-add-${fx.season}-${fx.league}`, fx.divClean);
      const resp = page.waitForResponse((r) =>
        r.url() === `${base}/api/v2/setup/seasons/${fx.season}/team-registrations`
        && r.request().method() === "POST");
      await page.click(`[data-reg-add="${fx.league}"]`);
      const body = await (await resp).json();
      if (body.error) throw new Error(`[${viewport.label}] register failed: ${JSON.stringify(body.error)}`);
      return body.id;
    };
    const reg1 = await registerVia(fx.team1);
    await page.waitForFunction(
      (sel) => !!document.querySelector(sel), `#reg-team-${fx.season}-${fx.league}`, { timeout: 15000 });
    const reg2 = await registerVia(fx.team2);

    // Remove BOTH from the season — the UI now shows 0 active teams under
    // divClean, but the registrations still exist (inactive, division_id
    // retained) until the Division delete below clears them.
    for (const regId of [reg1, reg2]) {
      const resp = page.waitForResponse((r) =>
        r.url() === `${base}/api/v2/setup/season-team-registration/${regId}/remove`
        && r.request().method() === "POST");
      await page.click(`[data-reg-remove="${regId}"]`);
      if ((await resp).status() !== 200) throw new Error(`[${viewport.label}] remove ${regId} failed`);
    }

    const beforeDelete = await page.evaluate(async (i) => {
      const get = async (p) => (await fetch(p, { credentials: "same-origin" })).json();
      const dov = await get("/api/demo/overview");
      const activeInDiv = (dov.registrations || [])
        .filter((r) => r.division_id === i.divClean).length;
      return { activeInDiv };
    }, { divClean: fx.divClean });
    if (beforeDelete.activeInDiv !== 0) {
      throw new Error(`[${viewport.label}] expected 0 active regs before delete, got ${JSON.stringify(beforeDelete)}`);
    }

    // Delete the now-apparently-empty Division from Records.
    await page.click('[data-setup-view="hierarchy"]');
    await page.waitForSelector(`[data-del="division"][data-del-id="${fx.divClean}"]`, { timeout: 10000 });
    await page.click(`[data-del="division"][data-del-id="${fx.divClean}"]`);
    await page.waitForSelector(".modal.danger [data-del-confirm]", { timeout: 10000 });
    const delResp = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/division/${fx.divClean}/delete` && r.request().method() === "POST");
    await page.click("[data-del-confirm]");
    const delBody = await (await delResp).json();
    if (delBody.error) throw new Error(`[${viewport.label}] division delete failed: ${JSON.stringify(delBody.error)}`);
    if (delBody.inactive_registrations_cleaned !== 2) {
      throw new Error(`[${viewport.label}] expected inactive_registrations_cleaned=2, got ${JSON.stringify(delBody)}`);
    }
    await page.waitForSelector(".modal", { state: "detached", timeout: 10000 });

    // The success toast must surface the cleanup count, not just "Deleted".
    const toastText = await page.textContent("#toast-root .toast-msg");
    if (!/Cleared 2 inactive registration/.test(toastText || "")) {
      throw new Error(`[${viewport.label}] toast did not report the cleanup count: ${JSON.stringify(toastText)}`);
    }

    // Both registrations survive: inactive, division_id cleared, Season/Team/
    // League identity untouched.
    const after = await page.evaluate(async (i) => {
      const get = async (p) => (await fetch(p, { credentials: "same-origin" })).json();
      const list = await get(`/api/v2/setup/seasons/${i.season}/team-registrations`);
      const regs = list.registrations || [];
      return {
        r1: regs.find((r) => r.id === i.reg1) || null,
        r2: regs.find((r) => r.id === i.reg2) || null,
      };
    }, { season: fx.season, reg1, reg2 });
    for (const [label, r, teamId] of [["r1", after.r1, fx.team1], ["r2", after.r2, fx.team2]]) {
      if (!r) throw new Error(`[${viewport.label}] ${label} vanished: ${JSON.stringify(after)}`);
      if (r.active !== false) throw new Error(`[${viewport.label}] ${label} still active: ${JSON.stringify(r)}`);
      if (r.division_id !== null) throw new Error(`[${viewport.label}] ${label} division_id not cleared: ${JSON.stringify(r)}`);
      if (r.season_id !== fx.season || r.team_id !== teamId || r.league_id !== fx.league) {
        throw new Error(`[${viewport.label}] ${label} lost its Season/Team/League identity: ${JSON.stringify(r)}`);
      }
    }

    // (2) A SEPARATE Division with an ACTIVE registration still blocks, zero
    // mutation.
    await page.click('.tab[data-tab="setup"]');
    await page.waitForFunction(
      (sel) => !!document.querySelector(sel), `#reg-team-${fx.season}-${fx.league}`, { timeout: 15000 });
    await page.selectOption(`#reg-team-${fx.season}-${fx.league}`, fx.teamActive);
    await page.selectOption(`#reg-div-add-${fx.season}-${fx.league}`, fx.divActive);
    const activeRegResp = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/seasons/${fx.season}/team-registrations`
      && r.request().method() === "POST");
    await page.click(`[data-reg-add="${fx.league}"]`);
    if ((await activeRegResp).status() !== 200) throw new Error(`[${viewport.label}] active reg failed`);

    await page.click('[data-setup-view="hierarchy"]');
    await page.waitForSelector(`[data-del="division"][data-del-id="${fx.divActive}"]`, { timeout: 10000 });
    await page.click(`[data-del="division"][data-del-id="${fx.divActive}"]`);
    await page.waitForSelector(".modal.danger [data-del-confirm]", { timeout: 10000 });
    const blockedResp1 = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/division/${fx.divActive}/delete` && r.request().method() === "POST");
    // Detach the console-error listener only for this deliberately-blocked
    // delete: the server's 409 response logs a benign "Failed to load
    // resource" Chromium console entry, not a real page bug (same pattern
    // as season-participation.js's blocked-league-delete step).
    page.off("console", consoleErrorHandler);
    await page.click("[data-del-confirm]");
    await blockedResp1;
    await page.waitForSelector(".modal.blocked", { timeout: 10000 });
    page.on("console", consoleErrorHandler);
    const blockedText1 = await page.textContent(".modal.blocked");
    if (!/team registration/i.test(blockedText1 || "")) {
      throw new Error(`[${viewport.label}] blocked modal did not mention the active registration: ${blockedText1}`);
    }
    await page.click(".modal.blocked [data-modal-close]");
    await page.waitForSelector(".modal", { state: "detached", timeout: 10000 });

    // (3) A Division referenced only by a cancelled Game (zero active
    // registrations left — both teams were already removed in the fixture)
    // still blocks, zero mutation.
    await page.waitForSelector(`[data-del="division"][data-del-id="${fx.divGame}"]`, { timeout: 10000 });
    await page.click(`[data-del="division"][data-del-id="${fx.divGame}"]`);
    await page.waitForSelector(".modal.danger [data-del-confirm]", { timeout: 10000 });
    const blockedResp2 = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/division/${fx.divGame}/delete` && r.request().method() === "POST");
    page.off("console", consoleErrorHandler);
    await page.click("[data-del-confirm]");
    await blockedResp2;
    await page.waitForSelector(".modal.blocked", { timeout: 10000 });
    page.on("console", consoleErrorHandler);
    const blockedText2 = await page.textContent(".modal.blocked");
    if (!/game/i.test(blockedText2 || "")) {
      throw new Error(`[${viewport.label}] blocked modal did not mention the Game reference: ${blockedText2}`);
    }
    await page.click(".modal.blocked [data-modal-close]");
    await page.waitForSelector(".modal", { state: "detached", timeout: 10000 });

    // Zero mutation on both blocked attempts — both Divisions still exist.
    const finalState = await page.evaluate(async (i) => {
      const get = async (p) => (await fetch(p, { credentials: "same-origin" })).json();
      const sv = await get("/api/v2/setup/overview");
      const divs = new Set((sv.divisions || []).map((d) => d.id));
      return { activeStillExists: divs.has(i.divActive), gameStillExists: divs.has(i.divGame) };
    }, { divActive: fx.divActive, divGame: fx.divGame });
    if (!finalState.activeStillExists) throw new Error(`[${viewport.label}] divActive was deleted despite the block`);
    if (!finalState.gameStillExists) throw new Error(`[${viewport.label}] divGame was deleted despite the block`);

    if (errors.length) {
      throw new Error(`[${viewport.label}] console/page errors:\n${errors.join("\n")}`);
    }
    console.log(`[${viewport.label}] OK — Division-deletion cleanup + blocking verified.`);
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
    console.log("Division-deletion cleanup browser journey passed.");
  } catch (error) {
    console.error("Division-deletion cleanup browser journey FAILED.");
    console.error(error && error.message ? error.message : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
