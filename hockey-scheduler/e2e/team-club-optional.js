// Club-optional Team lifecycle browser journey (#233 Slice D2).
//
// A Team's Club is an optional affiliation, never a structural requirement:
//   1. create a Team through the real Records drawer with NO Club selected
//      (the "Club (optional)" field defaults to "— none —");
//   2. assign a Club later through the Hierarchy tree's ⇄ Move control;
//   3. remove the Club again (unassign back to "— none —");
//   4. the Team's season registration, its game, and its player survive every
//      step untouched;
//   5. the now-unreferenced Club is then safely deleted from Records.
// Runs at desktop and 390px. Fails on any browser console/page error.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");
const { installContextFixture } = require("./context-fixture.js");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
const CAL_DAY = "2026-09-06";
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8181 },
  { label: "phone", width: 390, height: 844, port: 8182 },
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
  page.on("console", (m) => { if (m.type() === "error") errors.push(`[console] ${m.text()}`); });

  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });
    await installContextFixture(page);

    // Fixture prerequisites (org/program/season/league/venue/rink/slot/away
    // team/club) — proven elsewhere; built via raw fetch to keep this
    // journey's UI interactions focused on the Club-optional lifecycle.
    //
    // #409 EXPLICIT SELECTION. The Season create is PROGRAM-AXIS and the
    // League, venue-access grant and away-team registration are SEASON-OWNED,
    // so both selections are persisted here in the order the axis table
    // requires and each create is asserted at its own line
    // (./context-fixture.js). The Team and Player the journey then creates
    // through the real drawers are PROGRAM-AXIS and ride on the same saved
    // Program.
    const fx = await page.evaluate(async (d) => {
      const F = window.hsFixture;
      const org = await F.create("organization", "/api/v2/setup/organization",
        { name: "Club Optional Org" });
      const program = await F.create("program", "/api/v2/setup/program",
        { name: "Club Optional Program", operator_organization_id: org.id });
      await F.selectProgram("Program-only bootstrap", program.id);
      const season = await F.create("season", "/api/v2/setup/season",
        { program_id: program.id, name: "2026-27" });
      await F.selectProgramSeason("Program+Season", program.id, season.id);
      const league = await F.create("league", "/api/v2/setup/league",
        { season_id: season.id, name: "Adult League" });
      const venue = await F.create("venue", "/api/v2/setup/venue",
        { name: "V", organization_id: org.id });
      // Game ice eligibility (#233 Slice E) requires the ice slot's venue to
      // hold an active SeasonVenueAccess grant for this Season. Out of this
      // journey's scope (proven elsewhere).
      await F.call("season venue-access grant",
        `/api/v2/setup/seasons/${season.id}/venue-access`, { venue_id: venue.id });
      const rink = await F.create("rink", "/api/v2/setup/rink",
        { venue_id: venue.id, name: "R" });
      const slot = await F.create("ice slot", "/api/v2/setup/ice-slot", {
        rink_id: rink.id, start_time: `${d}T18:00:00+00:00`, end_time: `${d}T19:00:00+00:00`,
        slot_type: "game",
      });
      const club = await F.create("club", "/api/v2/setup/club", { name: "Lions Club" });
      const away = await F.create("team Away Team", "/api/v2/setup/team",
        { league_id: league.id, name: "Away Team" });
      await F.call("registration for Away Team",
        `/api/v2/setup/seasons/${season.id}/team-registrations`,
        { team_id: away.id, league_id: league.id });
      return { program: program.id, season: season.id, league: league.id,
               slot: slot.id, club: club.id, away: away.id };
    }, CAL_DAY);

    await page.click('.tab[data-tab="setup"]');
    await page.click('[data-setup-view="records"]');
    await page.waitForSelector(".setup-card", { timeout: 10000 });

    // (1) Create a Team through the real drawer with NO Club selected — the
    // "Club (optional)" select is left at its default "— none —" option.
    await page.click('.setup-card .sc-new[data-drawer="team"]');
    await page.waitForSelector(".drawer[role=dialog]", { timeout: 10000 });
    await page.selectOption("#f-team-perm-league", fx.league);
    await page.fill("#f-team", "Optional Club Team");
    const createResp = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/team` && r.request().method() === "POST");
    await page.click('[data-drawer-submit="team"]');
    const teamBody = await (await createResp).json();
    if (teamBody.error) {
      throw new Error(`[${viewport.label}] clubless team create failed: ${JSON.stringify(teamBody.error)}`);
    }
    if (teamBody.club_id !== null) {
      throw new Error(`[${viewport.label}] expected club_id null, got ${JSON.stringify(teamBody.club_id)}`);
    }
    const team = teamBody.id;
    await page.waitForFunction(
      () => !document.querySelector(".drawer[role=dialog]"), null, { timeout: 10000 });

    // The Records list row shows "No club" for the just-created clubless team.
    const rowTextNoClub = await page.locator(".li", {
      has: page.locator(".li-title", { hasText: "Optional Club Team" }),
    }).innerText();
    if (!rowTextNoClub.includes("No club")) {
      throw new Error(`[${viewport.label}] Records row did not show "No club": ${rowTextNoClub}`);
    }

    // Register the clubless team for the season/league, add a player to it,
    // and schedule a game — the fixtures this journey must prove survive
    // every Club assignment change untouched.
    // #409: the registration and the Game are both SEASON-OWNED, and real UI
    // work (a drawer Team create, a Records re-render) has happened since the
    // bootstrap. Re-assert the two-axis selection here rather than assume the
    // earlier one is still the saved row — an assumption is exactly the
    // inference this issue removes, and re-asserting costs one round trip.
    const fx2 = await page.evaluate(async (i) => {
      const F = window.hsFixture;
      await F.selectProgramSeason("Program+Season before the Game", i.program, i.season);
      await F.call("registration for the clubless team",
        `/api/v2/setup/seasons/${i.season}/team-registrations`,
        { team_id: i.team, league_id: i.league });
      const game = await F.create("game", "/api/v2/setup/game", {
        season_id: i.season, league_id: i.league,
        home_team_id: i.team, away_team_id: i.away, ice_slot_id: i.slot,
      });
      return { game: game.id };
    }, { program: fx.program, season: fx.season, league: fx.league, team,
         away: fx.away, slot: fx.slot });
    const game = fx2.game;

    await page.click('.setup-card .sc-new[data-drawer="player"]');
    await page.waitForSelector(".drawer[role=dialog]", { timeout: 10000 });
    await page.selectOption("#f-player-team", team);
    await page.fill("#f-player-name", "Proof Player");
    await page.selectOption("#f-player-position", "forward");
    const playerResp = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/player` && r.request().method() === "POST");
    await page.click('[data-drawer-submit="player"]');
    const player = (await (await playerResp).json()).id;
    await page.waitForFunction(
      () => !document.querySelector(".drawer[role=dialog]"), null, { timeout: 10000 });

    // (2) Assign a Club later — Hierarchy tree's "Permanent program teams"
    // panel renders the ⇄ Move control on every team row, open by default
    // (no expand needed, unlike the collapsed Rosters tree).
    await page.click('[data-setup-view="hierarchy"]');
    const reassignSel = `[data-reassign="team:club"][data-rz-id="${team}"]`;
    await page.waitForSelector(reassignSel, { timeout: 10000 });
    await page.click(reassignSel);
    await page.waitForSelector(".rz-panel", { timeout: 10000 });
    await page.selectOption("#reassign-target", fx.club);
    let reassignResp = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/team/${team}/assign-club` && r.request().method() === "POST");
    await page.click("[data-reassign-confirm]");
    if ((await reassignResp).status() !== 200) {
      throw new Error(`[${viewport.label}] assign-club non-200`);
    }
    await page.waitForFunction(
      () => !document.querySelector(".rz-panel"), null, { timeout: 10000 });

    // Registration, game, and player all survive the Club assignment.
    let state = await page.evaluate(async (i) => {
      const get = async (p) => (await fetch(p, { credentials: "same-origin" })).json();
      const sv = await get("/api/v2/setup/overview");
      const dov = await get("/api/demo/overview");
      const players = await get("/api/players");
      const t = (sv.teams || []).find((x) => x.id === i.team);
      const reg = (dov.registrations || []).find((r) => r.team_id === i.team);
      const gm = (dov.schedule || []).find((g) => g.game_id === i.game);
      const p = (Array.isArray(players) ? players : []).find((x) => x.id === i.player);
      return { clubId: t && t.club_id, hasReg: !!reg, hasGame: !!gm, playerTeam: p && p.team_id };
    }, { team, game, player });
    if (state.clubId !== fx.club) {
      throw new Error(`[${viewport.label}] club not assigned: ${JSON.stringify(state)}`);
    }
    if (!state.hasReg || !state.hasGame || state.playerTeam !== team) {
      throw new Error(`[${viewport.label}] fixtures lost after club assign: ${JSON.stringify(state)}`);
    }

    // Records row now shows the Club's name, not "No club".
    await page.click('[data-setup-view="records"]');
    await page.waitForSelector(".setup-card", { timeout: 10000 });
    const rowTextWithClub = await page.locator(".li", {
      has: page.locator(".li-title", { hasText: "Optional Club Team" }),
    }).innerText();
    if (!rowTextWithClub.includes("Lions Club")) {
      throw new Error(`[${viewport.label}] Records row did not show the assigned Club: ${rowTextWithClub}`);
    }

    // (3) Remove the Club again — reassign to "— none —".
    await page.click('[data-setup-view="hierarchy"]');
    await page.waitForSelector(reassignSel, { timeout: 10000 });
    await page.click(reassignSel);
    await page.waitForSelector(".rz-panel", { timeout: 10000 });
    await page.selectOption("#reassign-target", "");
    reassignResp = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/team/${team}/assign-club` && r.request().method() === "POST");
    await page.click("[data-reassign-confirm]");
    if ((await reassignResp).status() !== 200) {
      throw new Error(`[${viewport.label}] unassign-club non-200`);
    }
    await page.waitForFunction(
      () => !document.querySelector(".rz-panel"), null, { timeout: 10000 });

    // Registration, game, and player all survive the unassign too.
    state = await page.evaluate(async (i) => {
      const get = async (p) => (await fetch(p, { credentials: "same-origin" })).json();
      const sv = await get("/api/v2/setup/overview");
      const dov = await get("/api/demo/overview");
      const players = await get("/api/players");
      const t = (sv.teams || []).find((x) => x.id === i.team);
      const reg = (dov.registrations || []).find((r) => r.team_id === i.team);
      const gm = (dov.schedule || []).find((g) => g.game_id === i.game);
      const p = (Array.isArray(players) ? players : []).find((x) => x.id === i.player);
      return { clubId: t && t.club_id, hasReg: !!reg, hasGame: !!gm, playerTeam: p && p.team_id };
    }, { team, game, player });
    if (state.clubId !== null) {
      throw new Error(`[${viewport.label}] club not cleared: ${JSON.stringify(state)}`);
    }
    if (!state.hasReg || !state.hasGame || state.playerTeam !== team) {
      throw new Error(`[${viewport.label}] fixtures lost after club unassign: ${JSON.stringify(state)}`);
    }

    // Records row is back to "No club".
    await page.click('[data-setup-view="records"]');
    await page.waitForSelector(".setup-card", { timeout: 10000 });
    const rowTextAgain = await page.locator(".li", {
      has: page.locator(".li-title", { hasText: "Optional Club Team" }),
    }).innerText();
    if (!rowTextAgain.includes("No club")) {
      throw new Error(`[${viewport.label}] Records row did not revert to "No club": ${rowTextAgain}`);
    }

    // (4) The now-unreferenced Club deletes cleanly from Records.
    await page.waitForSelector(`[data-del="club"][data-del-id="${fx.club}"]`, { timeout: 10000 });
    await page.click(`[data-del="club"][data-del-id="${fx.club}"]`);
    await page.waitForSelector(".modal.danger [data-del-confirm]", { timeout: 10000 });
    const delResp = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/club/${fx.club}/delete` && r.request().method() === "POST");
    await page.click("[data-del-confirm]");
    if ((await delResp).status() !== 200) throw new Error(`[${viewport.label}] club delete non-200`);
    await page.waitForSelector(".modal", { state: "detached", timeout: 10000 });

    const clubGone = await page.evaluate(async (clubId) => {
      const clubs = (await (await fetch("/api/v2/setup/overview",
        { credentials: "same-origin" })).json()).clubs || [];
      return !clubs.some((c) => c.id === clubId);
    }, fx.club);
    if (!clubGone) throw new Error(`[${viewport.label}] club still present after delete`);

    if (errors.length) {
      throw new Error(`[${viewport.label}] console/page errors:\n${errors.join("\n")}`);
    }
    console.log(`[${viewport.label}] OK — Club-optional Team lifecycle verified.`);
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
    console.log("Club-optional Team lifecycle browser journey passed.");
  } catch (error) {
    console.error("Club-optional Team lifecycle browser journey FAILED.");
    console.error(error && error.message ? error.message : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
