// v1-dependency proof (#233 B2c): every application UI workflow talks to the
// canonical /api/v2/setup/... surface. The ONLY frontend call still on
// /api/setup/... is the temporary Venue→Program compatibility bridge
// (POST /api/setup/venue/{id}/assign-league) — deliberately deferred to
// Slice E, when the Venue.league_id coupling itself is removed.
//
// This journey drives a broad sweep of Setup/Records/Calendar workflows —
// creating a Program/Season/League/Division/Club/Team/Venue/Rink/Ice-slot,
// creating an Organization, an Official and a Player through their drawers,
// reassigning a Player to a different Team, applying the one documented v1
// bridge, scheduling a game through the Calendar wizard (League required,
// Division optional), and deleting both a spare Organization and the draft
// game just created — while recording every request the page makes. It then
// asserts the ONLY /api/setup/... calls observed are that one documented
// bridge call — any other stray v1 call fails the journey outright.
//
// It also asserts the removed legacy Team→Division reassignment control
// (superseded by SeasonTeamRegistration) is never rendered anywhere.
//
// Fails on any browser console/page error.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
const CAL_DAY = "2026-09-05";
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8171 },
  { label: "phone", width: 390, height: 844, port: 8172 },
];

// The one documented, deliberately-deferred v1 call (removed in Slice E).
const ALLOWED_V1 = /^\/api\/setup\/venue\/[^/]+\/assign-league$/;

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

  // Record every /api/setup/... (v1) request the page itself makes, for the
  // life of this page — the actual subject of this journey.
  const v1Calls = [];
  page.on("request", (req) => {
    let p;
    try { p = new URL(req.url()).pathname; } catch { return; }
    if (p.startsWith("/api/setup/")) v1Calls.push(`${req.method()} ${p}`);
  });

  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });

    // Build the structural fixtures the drawer/wizard/reassign/delete steps
    // below need, entirely through the canonical v2 API (never through the
    // page's own click-driven creates — those are what this journey proves).
    const fx = await page.evaluate(async (day) => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      const org = await post("/api/v2/setup/organization", { name: "Fixture Facilities" });
      const program = await post("/api/v2/setup/program",
        { name: "V1 Proof Program", operator_organization_id: org.id });
      const season = await post("/api/v2/setup/season", { program_id: program.id, name: "2026-27" });
      const league = await post("/api/v2/setup/league", { season_id: season.id, name: "Adult League" });
      const division = await post("/api/v2/setup/division", { league_id: league.id, name: "Gold" });
      const club = await post("/api/v2/setup/club", { name: "Fixture Club" });
      const team1 = await post("/api/v2/setup/team",
        { program_id: program.id, club_id: club.id, name: "Team One" });
      const team2 = await post("/api/v2/setup/team",
        { program_id: program.id, club_id: club.id, name: "Team Two" });
      await post(`/api/v2/setup/seasons/${season.id}/team-registrations`,
        { team_id: team1.id, league_id: league.id, division_id: division.id });
      await post(`/api/v2/setup/seasons/${season.id}/team-registrations`,
        { team_id: team2.id, league_id: league.id, division_id: division.id });
      const venue = await post("/api/v2/setup/venue", { name: "Fixture Venue", organization_id: org.id });
      const rink = await post("/api/v2/setup/rink", { venue_id: venue.id, name: "Fixture Rink" });
      const slot = await post("/api/v2/setup/ice-slot", {
        rink_id: rink.id, start_time: `${day}T18:00:00+00:00`,
        end_time: `${day}T19:00:00+00:00`, slot_type: "game" });
      // A spare, dependency-free Organization for the delete step below.
      const spareOrg = await post("/api/v2/setup/organization", { name: "Spare Facilities" });
      // A second slot for the scheduler-committed draft built AFTER the venue
      // bridge is applied below (the round-robin generator requires the
      // slot's venue to already carry the legacy league_id).
      const draftSlot = await post("/api/v2/setup/ice-slot", {
        rink_id: rink.id, start_time: `${day}T20:00:00+00:00`,
        end_time: `${day}T21:00:00+00:00`, slot_type: "game" });
      return {
        org: org.id, program: program.id, season: season.id, league: league.id,
        division: division.id, club: club.id, team1: team1.id, team2: team2.id,
        venue: venue.id, rink: rink.id, slot: slot.id, spareOrg: spareOrg.id,
        draftSlot: draftSlot.id,
      };
    }, CAL_DAY);

    await page.click('.tab[data-tab="setup"]');
    await page.click('[data-setup-view="records"]');
    await page.waitForSelector(".setup-card", { timeout: 10000 });

    // (1) Official create — drawer submit must hit v2.
    await page.click('.setup-card .sc-new[data-drawer="official"]');
    await page.waitForSelector("#f-official", { timeout: 10000 });
    await page.fill("#f-official", "Proof Official");
    await page.click('[data-drawer-submit="official"]');
    await page.waitForFunction(
      () => !document.querySelector(".drawer[role=dialog]"), null, { timeout: 10000 });

    // (2) Player create — drawer submit must hit v2.
    await page.click('.setup-card .sc-new[data-drawer="player"]');
    await page.waitForSelector("#f-player-team", { timeout: 10000 });
    await page.selectOption("#f-player-team", fx.team1);
    await page.fill("#f-player-name", "Proof Player");
    await page.selectOption("#f-player-position", "forward");
    await page.click('[data-drawer-submit="player"]');
    await page.waitForFunction(
      () => !document.querySelector(".drawer[role=dialog]"), null, { timeout: 10000 });

    // (3) Organization create — drawer submit must hit v2.
    await page.click('.setup-card .sc-new[data-drawer="organization"]');
    await page.waitForSelector("#f-org", { timeout: 10000 });
    await page.fill("#f-org", "Proof Facilities");
    await page.click('[data-drawer-submit="organization"]');
    await page.waitForFunction(
      () => !document.querySelector(".drawer[role=dialog]"), null, { timeout: 10000 });

    // (4) Player→Team reassign — move the just-created player to Team Two.
    // Must hit v2 (assign_player_team), the successor to the removed
    // Team→Division control (seasonal placement is a SeasonTeamRegistration,
    // never a structural reassignment on the Team or the Player). The
    // reassign control lives on the Hierarchy tree view, not Records.
    await page.click('[data-setup-view="hierarchy"]');
    // The Rosters tree's team rows start collapsed — expand Team One's to
    // reveal the player row and its reassign control.
    await page.waitForSelector('[data-reassign="player:team"]', { state: "attached", timeout: 10000 });
    await page.click('[data-reassign="player:team"] >> xpath=ancestor::details/summary');
    await page.waitForSelector('[data-reassign="player:team"]', { timeout: 10000 });
    await page.click('[data-reassign="player:team"]');
    await page.waitForSelector(".rz-panel", { timeout: 10000 });
    await page.selectOption("#reassign-target", fx.team2);
    await page.click("[data-reassign-confirm]");
    await page.waitForFunction(
      () => !document.querySelector(".rz-panel"), null, { timeout: 10000 });

    // (5) The removed legacy Team→Division reassignment control must never
    // render anywhere on the page (#233 B2c) — seasonal placement is a
    // SeasonTeamRegistration, moved via Season participation's own
    // League→Division cascade, never a structural Team reassignment.
    if (await page.$('[data-reassign="team:division"]')) {
      throw new Error(`[${viewport.label}] a legacy team:division reassign control is still rendered`);
    }

    // (6) The one documented, deliberately-deferred v1 call: the temporary
    // Venue→Program compatibility bridge (Slice E removes it).
    await page.waitForSelector('[data-reassign="venue:league"]', { timeout: 10000 });
    await page.click('[data-reassign="venue:league"]');
    await page.waitForSelector(".rz-panel", { timeout: 10000 });
    await page.selectOption("#reassign-target", fx.program);
    await page.click("[data-reassign-confirm]");
    await page.waitForFunction(
      () => !document.querySelector(".rz-panel"), null, { timeout: 10000 });

    // A genuine scheduler draft (is_draft=True — a manually wizard-created
    // game defaults to committed, not draft, per (7) below), built now that
    // the venue carries its legacy league_id (the round-robin generator
    // requires it), purely to exercise the Scheduler view's draft-delete
    // control at (9). Neither /api/scheduler/... route is part of the
    // /api/setup/... v1 surface this journey is proving.
    const draftGame = await page.evaluate(async (i) => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      const commit = await post("/api/scheduler/commit",
        { division_id: i.division, slot_ids: [i.draftSlot] });
      if (commit.error) return { error: commit.error };
      const drafts = await (await fetch("/api/scheduler/drafts",
        { credentials: "same-origin" })).json();
      const draft = (drafts.draft_games || []).find((g) => g.division_id === i.division);
      return { id: draft && draft.game_id };
    }, fx);
    if (draftGame.error || !draftGame.id) {
      throw new Error(`[${viewport.label}] scheduler commit produced no draft game: ${JSON.stringify(draftGame)}`);
    }

    // (7) Schedule a game through the Calendar wizard — League required,
    // Division optional (#233 B2c) — must hit v2.
    await page.click('.tab[data-tab="calendar"]');
    await page.waitForSelector(`[data-slot="${fx.slot}"]`, { timeout: 15000 });
    await page.click(`[data-slot="${fx.slot}"]`);
    await page.waitForSelector("#w-league", { timeout: 10000 });
    await page.selectOption("#w-league", fx.league);
    await page.waitForFunction(
      (d) => !!Array.from(document.querySelectorAll("#w-div option")).find((o) => o.value === d),
      fx.division, { timeout: 10000 });
    await page.selectOption("#w-div", fx.division);
    await page.waitForFunction(
      (t) => !!Array.from(document.querySelectorAll("#w-home option")).find((o) => o.value === t),
      fx.team1, { timeout: 10000 });
    await page.selectOption("#w-home", fx.team1);
    await page.selectOption("#w-away", fx.team2);
    const createResp = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/game` && r.request().method() === "POST");
    await page.click("[data-wizcreate]");
    const created = await (await createResp).json();
    if (created.error) throw new Error(`[${viewport.label}] wizard game create failed: ${JSON.stringify(created.error)}`);

    // (8) Delete the spare Organization from the Facility tree (its only
    // delete control — Records has no delBtn for organizations) — must hit v2.
    await page.click('.tab[data-tab="setup"]');
    await page.click('[data-setup-view="hierarchy"]');
    await page.waitForSelector(`[data-del="organization"][data-del-id="${fx.spareOrg}"]`, { timeout: 15000 });
    await page.click(`[data-del="organization"][data-del-id="${fx.spareOrg}"]`);
    await page.waitForSelector(".modal.danger [data-del-confirm]", { timeout: 10000 });
    await page.click("[data-del-confirm]");
    await page.waitForSelector(".modal", { state: "detached", timeout: 10000 });

    // (9) Delete the scheduler-committed draft game (a manually wizard-created
    // game defaults to committed, not draft — its only actions are Publish/
    // Cancel, per (7) above). A draft's only delete control is on the
    // Scheduler view's list (the Games tab offers Cancel for a committed/
    // published game, never Delete) — must hit v2.
    await page.click('.tab[data-tab="scheduler"]');
    await page.waitForSelector(`[data-del="game"][data-del-id="${draftGame.id}"]`, { timeout: 15000 });
    await page.click(`[data-del="game"][data-del-id="${draftGame.id}"]`);
    await page.waitForSelector(".modal.danger [data-del-confirm]", { timeout: 10000 });
    await page.click("[data-del-confirm]");
    await page.waitForSelector(".modal", { state: "detached", timeout: 10000 });

    // The proof: every /api/setup/... call this page made is the one
    // documented bridge — anything else is a stray v1 dependency.
    const stray = v1Calls.filter((c) => !ALLOWED_V1.test(c.split(" ")[1]));
    if (stray.length) {
      throw new Error(`[${viewport.label}] stray /api/setup/... calls found: ${JSON.stringify(stray)}`);
    }
    const bridgeCalls = v1Calls.filter((c) => ALLOWED_V1.test(c.split(" ")[1]));
    if (!bridgeCalls.length) {
      throw new Error(`[${viewport.label}] the documented venue-bridge call never fired — the allowlist regex may be untested`);
    }

    if (errors.length) {
      throw new Error(`[${viewport.label}] console/page errors:\n${errors.join("\n")}`);
    }
    console.log(`[${viewport.label}] OK — every /api/setup/... call was the documented venue-bridge exception (${bridgeCalls.length}), zero stray v1 calls.`);
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
    console.log("v1-dependency proof browser journey passed.");
  } catch (error) {
    console.error("v1-dependency proof browser journey FAILED.");
    console.error(error && error.message ? error.message : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
