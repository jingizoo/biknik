// v1-dependency proof (#233 B2c, bridge removed in Slice E): every
// application UI workflow talks to the canonical /api/v2/setup/... surface.
// The temporary Venue→Program compatibility bridge
// (POST /api/setup/venue/{id}/assign-league) has been removed entirely
// (#233 Slice E) — there is no longer any documented v1 exception.
//
// This journey drives EVERY structural create through its real Records-view
// drawer — Organization (x2), Program, Season, League, Division, Club, Team
// (x2), Venue, Rink, Ice-slot (x2), Official, Player — plus a Player→Team
// reassign, scheduling a game through the Calendar wizard (League required,
// Division optional), and deleting both a spare Organization and a draft
// game, while recording every request the page makes. Driving creates
// through the real drawers (not a raw-fetch fixture) means reverting any
// SETUP_POST entry back to v1 would be caught here, not just by the separate
// check-v1-route-contract.js static check. It then asserts ZERO
// /api/setup/... calls were observed — any v1 call fails the journey.
//
// It also asserts the removed legacy Team→Division reassignment control
// (superseded by SeasonTeamRegistration) and the removed Venue→Program
// bridge control are never rendered anywhere.
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

  // Open a create drawer, fill its fields (select vs. fill by tag), submit,
  // and return the parsed response body — asserting it POSTed to exactly the
  // expected canonical v2 URL. Filling every entity through its real drawer
  // (rather than a raw fetch) means a SETUP_POST entry reverted to v1 fails
  // this journey directly.
  const createViaDrawer = async (key, fields, expectedUrl) => {
    await page.click(`.setup-card .sc-new[data-drawer="${key}"]`);
    await page.waitForSelector(".drawer[role=dialog]", { timeout: 10000 });
    for (const [id, value] of Object.entries(fields)) {
      const tag = await page.$eval(`#${id}`, (el) => el.tagName);
      if (tag === "SELECT") await page.selectOption(`#${id}`, value);
      else await page.fill(`#${id}`, value);
    }
    const resp = page.waitForResponse((r) =>
      r.url() === `${base}${expectedUrl}` && r.request().method() === "POST");
    await page.click(`[data-drawer-submit="${key}"]`);
    const body = await (await resp).json();
    if (body.error) {
      throw new Error(`[${viewport.label}] ${key} create failed: ${JSON.stringify(body.error)}`);
    }
    await page.waitForFunction(
      () => !document.querySelector(".drawer[role=dialog]"), null, { timeout: 10000 });
    return body;
  };

  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });
    await page.click('.tab[data-tab="setup"]');
    await page.click('[data-setup-view="records"]');
    await page.waitForSelector(".setup-card", { timeout: 10000 });

    // Every structural create driven through its real drawer, in dependency
    // order — the actual subject of this journey's SETUP_POST proof.
    const org = await createViaDrawer("organization",
      { "f-org": "Fixture Facilities" }, "/api/v2/setup/organization");
    const spareOrg = await createViaDrawer("organization",
      { "f-org": "Spare Facilities" }, "/api/v2/setup/organization");
    const program = await createViaDrawer("league",
      { "f-league": "V1 Proof Program", "f-league-org": org.id }, "/api/v2/setup/program");
    const season = await createViaDrawer("season",
      { "f-season-league": program.id, "f-season": "2026-27" }, "/api/v2/setup/season");
    const league = await createViaDrawer("level",
      { "f-level-season": season.id, "f-level": "Adult League" }, "/api/v2/setup/league");
    const division = await createViaDrawer("division",
      { "f-div-league": league.id, "f-div": "Gold" }, "/api/v2/setup/division");
    const club = await createViaDrawer("club",
      { "f-club": "Fixture Club" }, "/api/v2/setup/club");
    const team1 = await createViaDrawer("team",
      { "f-team-club": club.id, "f-team-league": program.id, "f-team": "Team One" },
      "/api/v2/setup/team");
    const team2 = await createViaDrawer("team",
      { "f-team-club": club.id, "f-team-league": program.id, "f-team": "Team Two" },
      "/api/v2/setup/team");
    const venue = await createViaDrawer("venue",
      { "f-venue": "Fixture Venue", "f-venue-org": org.id }, "/api/v2/setup/venue");
    const rink = await createViaDrawer("rink",
      { "f-rink-venue": venue.id, "f-rink": "Fixture Rink" }, "/api/v2/setup/rink");
    const slot = await createViaDrawer("ice-slot", {
      "f-slot-rink": rink.id, "f-slot-date": CAL_DAY,
      "f-slot-start": "18:00", "f-slot-end": "19:00",
    }, "/api/v2/setup/ice-slot");
    // A second slot for the scheduler-committed draft built AFTER the venue
    // is granted season access below (the round-robin generator requires the
    // slot's venue to hold active SeasonVenueAccess for the season, #233
    // Slice E).
    const draftSlot = await createViaDrawer("ice-slot", {
      "f-slot-rink": rink.id, "f-slot-date": CAL_DAY,
      "f-slot-start": "20:00", "f-slot-end": "21:00",
    }, "/api/v2/setup/ice-slot");

    // Team-registration creation is Season participation's own UI (already
    // proven v2 in Slice B2b) — out of this journey's explicit scope, so
    // built via raw fetch, matching the established fixture-building pattern
    // used elsewhere for prerequisites this journey isn't itself proving.
    await page.evaluate(async (i) => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      await post(`/api/v2/setup/seasons/${i.season}/team-registrations`,
        { team_id: i.team1, league_id: i.league, division_id: i.division });
      await post(`/api/v2/setup/seasons/${i.season}/team-registrations`,
        { team_id: i.team2, league_id: i.league, division_id: i.division });
    }, {
      season: season.id, league: league.id, division: division.id,
      team1: team1.id, team2: team2.id,
    });

    // (1) Official create — drawer submit must hit v2.
    await createViaDrawer("official", { "f-official": "Proof Official" }, "/api/v2/setup/official");

    // (2) Player create — drawer submit must hit v2.
    await createViaDrawer("player", {
      "f-player-team": team1.id, "f-player-name": "Proof Player",
      "f-player-position": "forward",
    }, "/api/v2/setup/player");

    // (3) Player→Team reassign — move the just-created player to Team Two.
    // Must hit v2 (assign_player_team), the successor to the removed
    // Team→Division control (seasonal placement is a SeasonTeamRegistration,
    // never a structural reassignment on the Team or the Player). The
    // reassign control lives on the Hierarchy tree view, not Records.
    await page.click('[data-setup-view="hierarchy"]');

    // (2b) Season venue access (#233 Slice E) — grant the venue to the season
    // through the real "Allowed venues" control in Season participation, the
    // supported UI path a League Admin uses (no raw-fetch fixture).
    const vaSelId = `#va-add-${season.id}`;
    await page.waitForSelector(vaSelId, { timeout: 10000 });
    await page.selectOption(vaSelId, venue.id);
    const vaReq = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/seasons/${season.id}/venue-access`
      && r.request().method() === "POST");
    await page.click(`[data-va-add="${season.id}"]`);
    const vaBody = await (await vaReq).json();
    if (vaBody.error) {
      throw new Error(`[${viewport.label}] season venue-access grant failed: ${JSON.stringify(vaBody.error)}`);
    }
    await page.waitForSelector(`[data-va-revoke="${vaBody.id}"]`, { timeout: 10000 });

    // The Rosters tree's team rows start collapsed — expand Team One's to
    // reveal the player row and its reassign control.
    await page.waitForSelector('[data-reassign="player:team"]', { state: "attached", timeout: 10000 });
    await page.click('[data-reassign="player:team"] >> xpath=ancestor::details/summary');
    await page.waitForSelector('[data-reassign="player:team"]', { timeout: 10000 });
    await page.click('[data-reassign="player:team"]');
    await page.waitForSelector(".rz-panel", { timeout: 10000 });
    await page.selectOption("#reassign-target", team2.id);
    await page.click("[data-reassign-confirm]");
    await page.waitForFunction(
      () => !document.querySelector(".rz-panel"), null, { timeout: 10000 });

    // (4) The removed legacy Team→Division reassignment control must never
    // render anywhere on the page (#233 B2c) — seasonal placement is a
    // SeasonTeamRegistration, moved via Season participation's own
    // League→Division cascade, never a structural Team reassignment.
    if (await page.$('[data-reassign="team:division"]')) {
      throw new Error(`[${viewport.label}] a legacy team:division reassign control is still rendered`);
    }
    // The removed Venue→Program bridge control (#233 Slice E) must never
    // render anywhere either — the Facility tree's venue rows no longer
    // offer a "⇄ Move" to a program.
    await page.click('.tab[data-tab="setup"]');
    await page.click('[data-setup-view="hierarchy"]');
    if (await page.$('[data-reassign="venue:league"]')) {
      throw new Error(`[${viewport.label}] a removed venue:league bridge reassign control is still rendered`);
    }

    // A genuine scheduler draft (is_draft=True — a manually wizard-created
    // game defaults to committed, not draft, per (6) below), purely to
    // exercise the Scheduler view's draft-delete control at (8). Neither
    // /api/scheduler/... route is part of the /api/setup/... v1 surface
    // this journey is proving.
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
    }, { division: division.id, draftSlot: draftSlot.id });
    if (draftGame.error || !draftGame.id) {
      throw new Error(`[${viewport.label}] scheduler commit produced no draft game: ${JSON.stringify(draftGame)}`);
    }

    // (6) Schedule a game through the Calendar wizard — League required,
    // Division optional (#233 B2c) — must hit v2.
    await page.click('.tab[data-tab="calendar"]');
    await page.waitForSelector(`[data-slot="${slot.id}"]`, { timeout: 15000 });
    await page.click(`[data-slot="${slot.id}"]`);
    await page.waitForSelector("#w-league", { timeout: 10000 });
    await page.selectOption("#w-league", league.id);
    await page.waitForFunction(
      (d) => !!Array.from(document.querySelectorAll("#w-div option")).find((o) => o.value === d),
      division.id, { timeout: 10000 });
    await page.selectOption("#w-div", division.id);
    await page.waitForFunction(
      (t) => !!Array.from(document.querySelectorAll("#w-home option")).find((o) => o.value === t),
      team1.id, { timeout: 10000 });
    await page.selectOption("#w-home", team1.id);
    await page.selectOption("#w-away", team2.id);
    const createResp = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/game` && r.request().method() === "POST");
    await page.click("[data-wizcreate]");
    const created = await (await createResp).json();
    if (created.error) throw new Error(`[${viewport.label}] wizard game create failed: ${JSON.stringify(created.error)}`);

    // (7) Delete the spare Organization from the Facility tree (its only
    // delete control — Records has no delBtn for organizations) — must hit v2.
    await page.click('.tab[data-tab="setup"]');
    await page.click('[data-setup-view="hierarchy"]');
    await page.waitForSelector(`[data-del="organization"][data-del-id="${spareOrg.id}"]`, { timeout: 15000 });
    await page.click(`[data-del="organization"][data-del-id="${spareOrg.id}"]`);
    await page.waitForSelector(".modal.danger [data-del-confirm]", { timeout: 10000 });
    await page.click("[data-del-confirm]");
    await page.waitForSelector(".modal", { state: "detached", timeout: 10000 });

    // (8) Delete the scheduler-committed draft game (a manually wizard-created
    // game defaults to committed, not draft — its only actions are Publish/
    // Cancel, per (6) above). A draft's only delete control is on the
    // Scheduler view's list (the Games tab offers Cancel for a committed/
    // published game, never Delete) — must hit v2.
    await page.click('.tab[data-tab="scheduler"]');
    await page.waitForSelector(`[data-del="game"][data-del-id="${draftGame.id}"]`, { timeout: 15000 });
    await page.click(`[data-del="game"][data-del-id="${draftGame.id}"]`);
    await page.waitForSelector(".modal.danger [data-del-confirm]", { timeout: 10000 });
    await page.click("[data-del-confirm]");
    await page.waitForSelector(".modal", { state: "detached", timeout: 10000 });

    // The proof: NO /api/setup/... call was made anywhere in this journey —
    // the Venue→Program bridge (the last documented v1 exception) is gone.
    if (v1Calls.length) {
      throw new Error(`[${viewport.label}] stray /api/setup/... calls found: ${JSON.stringify(v1Calls)}`);
    }

    if (errors.length) {
      throw new Error(`[${viewport.label}] console/page errors:\n${errors.join("\n")}`);
    }
    console.log(`[${viewport.label}] OK — zero /api/setup/... calls, the app is fully on the v2 surface.`);
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
