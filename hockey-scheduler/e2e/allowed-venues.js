// Allowed Venues regression (#254 E2a review): proves the full production
// workflow end to end entirely through supported UI surfaces — the exact
// path the CI failure this PR's first push produced (a newly-created
// Venue/Season had no SeasonVenueAccess grant, so the Calendar wizard's game
// create 400'd) has a real, reachable fix, not just a raw-fetch test fixture.
//
// At desktop and 390px, a League Admin:
//   1. Creates Organization -> Venue -> Rink -> Ice slot and
//      Program -> Season -> League -> Division -> Club -> Teams entirely
//      through the real Setup/Records drawers.
//   2. Grants the Season access to the Venue through the "Allowed venues"
//      control in Season participation (Setup > Hierarchy) — the supported
//      UI path, never a raw fetch.
//   3. Confirms the grant is listed with a Revoke control.
//   4. Schedules a Game on the ice slot through the Calendar wizard and
//      confirms it succeeds (no venue_access_missing 400).
//   5. Drafts a second Game through the Scheduler's round-robin commit on a
//      second slot, confirming the draft path also sees the granted access.
//
// Fails on any browser console/page error.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");
const {
  installContextFixture, selectProgram, selectProgramSeason,
} = require("./context-fixture.js");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
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
  page.on("console", (m) => { if (m.type() === "error") errors.push(`[console] ${m.text()}`); });

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
    await installContextFixture(page);
    // This journey books ice and then reads it back off the Arena Calendar's
    // DEFAULT day, without navigating — so the day it books on must be the day
    // the calendar opens on. That used to be the literal "2026-09-05", which
    // worked only because app.js opened on the same literal: two constants
    // agreeing with each other about a date that real time would pass
    // (#387/#389). Read from the app's own `calendarDate` global instead, so
    // the booked day and the rendered day cannot drift apart.
    const CAL_DAY = await page.evaluate(() => calendarDate);
    await page.click('.tab[data-tab="setup"]');
    await page.click('[data-setup-view="records"]');
    await page.waitForSelector(".setup-card", { timeout: 10000 });

    // (1) Every structural record, driven through its real Records drawer.
    const org = await createViaDrawer("organization",
      { "f-org": "Allowed Venues Org" }, "/api/v2/setup/organization");
    const program = await createViaDrawer("league",
      { "f-league": "Allowed Venues Program", "f-league-org": org.id }, "/api/v2/setup/program");
    // THE EXPLICIT SELECTION (#409), boundary 1 — Program-only. Minting the
    // Program above does not select it, and the Season drawer below is a
    // PROGRAM-AXIS create, as are the Team, Venue, Rink and Ice-slot drawers
    // further down. ./context-fixture.js carries the axis table and explains
    // why the selection is proved by the write echo rather than by a GET the
    // fallback resolver can satisfy on its own.
    await selectProgram(page, `[${viewport.label}] Program-only bootstrap`, program.id);
    const season = await createViaDrawer("season",
      { "f-season-league": program.id, "f-season": "2026-27" }, "/api/v2/setup/season");
    // BOUNDARY 2 — Program+Season. The League ("level"), Division, the three
    // team registrations and the Season venue-access grant are all
    // SEASON-OWNED, and every one of them writes into THIS Season.
    await selectProgramSeason(page, `[${viewport.label}] Program+Season`,
      program.id, season.id);
    const league = await createViaDrawer("level",
      { "f-level-season": season.id, "f-level": "Adult League" }, "/api/v2/setup/league");
    const division = await createViaDrawer("division",
      { "f-div-league": league.id, "f-div": "Gold" }, "/api/v2/setup/division");
    const club = await createViaDrawer("club",
      { "f-club": "Allowed Venues Club" }, "/api/v2/setup/club");
    const team1 = await createViaDrawer("team",
      { "f-team-club": club.id, "f-team-perm-league": league.id, "f-team": "Team One" },
      "/api/v2/setup/team");
    const team2 = await createViaDrawer("team",
      { "f-team-club": club.id, "f-team-perm-league": league.id, "f-team": "Team Two" },
      "/api/v2/setup/team");
    // A third team (#206 slice 1): the manual wizard game below (step 4)
    // takes the division's only pairing if just two teams are registered,
    // so the scheduler draft in step 5 would then have nothing genuinely
    // missing left to place. A third team keeps a pairing open for it.
    const team3 = await createViaDrawer("team",
      { "f-team-club": club.id, "f-team-perm-league": league.id, "f-team": "Team Three" },
      "/api/v2/setup/team");
    const venue = await createViaDrawer("venue",
      { "f-venue": "Allowed Venue", "f-venue-org": org.id }, "/api/v2/setup/venue");
    const rink = await createViaDrawer("rink",
      { "f-rink-venue": venue.id, "f-rink": "Allowed Rink" }, "/api/v2/setup/rink");
    const slot = await createViaDrawer("ice-slot", {
      "f-slot-rink": rink.id, "f-slot-date": CAL_DAY,
      "f-slot-start": "18:00", "f-slot-end": "19:00",
    }, "/api/v2/setup/ice-slot");
    const draftSlot = await createViaDrawer("ice-slot", {
      "f-slot-rink": rink.id, "f-slot-date": CAL_DAY,
      "f-slot-start": "20:00", "f-slot-end": "21:00",
    }, "/api/v2/setup/ice-slot");

    // Team-registration creation is Season participation's own already-proven
    // UI (Slice B2b) — out of THIS regression's scope, so built via raw fetch
    // like every other journey's non-target prerequisites.
    // Each registration is ASSERTED (#409 review round). The bare `post()` that
    // stood here decoded the body and dropped the status, so all three could be
    // refused in silence and the failure would surface much later as a
    // scheduler draft that had no registered teams to place — blaming the
    // scheduler for a prerequisite that was never allowed to exist.
    await page.evaluate(async (i) => {
      await window.hsFixture.create("registration for Team One",
        `/api/v2/setup/seasons/${i.season}/team-registrations`,
        { team_id: i.team1, league_id: i.league, division_id: i.division });
      await window.hsFixture.create("registration for Team Two",
        `/api/v2/setup/seasons/${i.season}/team-registrations`,
        { team_id: i.team2, league_id: i.league, division_id: i.division });
      await window.hsFixture.create("registration for Team Three",
        `/api/v2/setup/seasons/${i.season}/team-registrations`,
        { team_id: i.team3, league_id: i.league, division_id: i.division });
    }, { season: season.id, league: league.id, division: division.id,
         team1: team1.id, team2: team2.id, team3: team3.id });

    // (2) Grant the Season access to the Venue — the actual workflow under
    // regression — through the real "Allowed venues" control in Season
    // participation (Setup > Hierarchy), never a raw fetch.
    await page.click('[data-setup-view="hierarchy"]');
    const vaSelId = `#va-add-${season.id}`;
    await page.waitForSelector(vaSelId, { timeout: 10000 });
    const beforeOptions = await page.$$eval(`${vaSelId} option`, (opts) => opts.map((o) => o.value));
    if (!beforeOptions.includes(venue.id)) {
      throw new Error(`[${viewport.label}] the newly-created venue is not offered in the Allow picker`);
    }
    await page.selectOption(vaSelId, venue.id);
    const vaReq = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/seasons/${season.id}/venue-access`
      && r.request().method() === "POST");
    await page.click(`[data-va-add="${season.id}"]`);
    const vaBody = await (await vaReq).json();
    if (vaBody.error) {
      throw new Error(`[${viewport.label}] season venue-access grant failed: ${JSON.stringify(vaBody.error)}`);
    }

    // (3) The grant is now listed with a Revoke control, and the Allow
    // picker no longer offers the now-granted venue.
    await page.waitForSelector(`[data-va-revoke="${vaBody.id}"]`, { timeout: 10000 });
    const afterOptions = await page.$$eval(`${vaSelId} option`, (opts) => opts.map((o) => o.value))
      .catch(() => []);
    if (afterOptions.includes(venue.id)) {
      throw new Error(`[${viewport.label}] the granted venue is still offered in the Allow picker`);
    }

    // (4) Schedule a Game on the granted venue's ice through the Calendar
    // wizard — this is the exact path that 400'd before the grant existed.
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
    const createReq = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/game` && r.request().method() === "POST");
    await page.click("[data-wizcreate]");
    const createBody = await (await createReq).json();
    if (createBody.error) {
      throw new Error(`[${viewport.label}] wizard game create failed: ${JSON.stringify(createBody.error)}`);
    }
    await page.waitForFunction(() => !document.querySelector(".wizard"), null, { timeout: 10000 });

    // (5) A scheduler-generated draft on the second slot sees the same
    // granted access — the round-robin path, not just the manual wizard.
    const draft = await page.evaluate(async (i) => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      // #328 review round 5: Commit is now bound to the exact preview it
      // reviewed, so a direct API call must Generate first, same as the
      // real Scheduler UI does.
      const preview = await post(
        "/api/scheduler/draft", { division_id: i.division, slot_ids: [i.draftSlot] });
      return post("/api/scheduler/commit", {
        division_id: i.division, slot_ids: [i.draftSlot],
        draft_fingerprint: preview.draft_fingerprint,
      });
    }, { division: division.id, draftSlot: draftSlot.id });
    if (draft.error || !draft.created || !draft.created.length) {
      throw new Error(`[${viewport.label}] scheduler commit produced no draft game: ${JSON.stringify(draft)}`);
    }

    if (errors.length) {
      throw new Error(`[${viewport.label}] console/page errors:\n${errors.join("\n")}`);
    }
    console.log(`[${viewport.label}] OK — venue access granted through the real UI, wizard game and scheduler draft both created.`);
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
    console.log("Allowed Venues browser journey passed.");
  } catch (error) {
    console.error("Allowed Venues browser journey FAILED.");
    console.error(error && error.message ? error.message : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
