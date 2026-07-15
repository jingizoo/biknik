// Multi-operator venue-sharing regression (#254 E2b, epic #233 Slice E
// acceptance criteria): proves, through real UI surfaces only, the three
// remaining unchecked Slice E acceptance boxes that allowed-venues.js's
// single-season/single-venue journey does not exercise:
//
//   - One Season can use multiple Venues.
//   - One Venue can host multiple independent Programs/Seasons
//     (the Twin Rinks-managed + external-IHSH-hosted scenario from #233).
//   - The facility owner and each Program's operator remain independent
//     organizations, end to end, after the legacy Venue->Program bridge's
//     removal in E2a.
//
// At desktop and 390px, a League Admin:
//   1. Creates a facility Organization owning two Venues (a shared arena and
//      a second, separate rink), entirely through Setup > Records drawers.
//   2. Creates two independent Programs, each operated by its OWN
//      organization (distinct from the facility owner and from each other) —
//      Program A modelling "Twin Rinks' own program", Program B modelling
//      an external, unrelated operator (Illinois High School Hockey).
//   3. Grants Program A's Season access to BOTH venues (multi-venue Season).
//   4. Grants Program B's Season access to the SAME shared venue Program A
//      already uses (multi-program Venue) — confirming the shared venue is
//      still offered in Program B's Allow picker despite Program A's grant,
//      and that neither Season's Allowed-venues list leaks the other's rows.
//   5. Successfully creates a Game for each Program on the shared venue, and
//      a further Game for Program A on the second venue, through the
//      Calendar wizard — proving eligibility and isolation both hold.
//
// Fails on any browser console/page error.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
const CAL_DAY = "2026-09-05";  // matches app.js's hardcoded default calendarDate
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8193 },
  { label: "phone", width: 390, height: 844, port: 8194 },
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

  // Grants seasonId access to venueId through the real "Allowed venues"
  // control (Setup > Hierarchy > Season participation), never a raw fetch.
  const grantViaUi = async (seasonId, venueId) => {
    const vaSelId = `#va-add-${seasonId}`;
    await page.waitForSelector(vaSelId, { timeout: 10000 });
    const options = await page.$$eval(`${vaSelId} option`, (opts) => opts.map((o) => o.value));
    if (!options.includes(venueId)) {
      throw new Error(`[${viewport.label}] venue ${venueId} is not offered in the ` +
        `Allow picker for season ${seasonId}`);
    }
    await page.selectOption(vaSelId, venueId);
    const req = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/seasons/${seasonId}/venue-access`
      && r.request().method() === "POST");
    await page.click(`[data-va-add="${seasonId}"]`);
    const body = await (await req).json();
    if (body.error) {
      throw new Error(`[${viewport.label}] venue-access grant failed: ${JSON.stringify(body.error)}`);
    }
    await page.waitForSelector(`[data-va-revoke="${body.id}"]`, { timeout: 10000 });
    return body;
  };

  // Creates a Game via the Calendar wizard on the given ice slot, for the
  // given league/division/home/away combination.
  const createGameViaWizard = async (slotId, leagueId, divisionId, homeId, awayId) => {
    await page.click('.tab[data-tab="calendar"]');
    await page.waitForSelector(`[data-slot="${slotId}"]`, { timeout: 15000 });
    await page.click(`[data-slot="${slotId}"]`);
    await page.waitForSelector("#w-league", { timeout: 10000 });
    await page.selectOption("#w-league", leagueId);
    if (divisionId) {
      await page.waitForFunction(
        (d) => !!Array.from(document.querySelectorAll("#w-div option")).find((o) => o.value === d),
        divisionId, { timeout: 10000 });
      await page.selectOption("#w-div", divisionId);
    }
    await page.waitForFunction(
      (t) => !!Array.from(document.querySelectorAll("#w-home option")).find((o) => o.value === t),
      homeId, { timeout: 10000 });
    await page.selectOption("#w-home", homeId);
    await page.selectOption("#w-away", awayId);
    const createReq = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/game` && r.request().method() === "POST");
    await page.click("[data-wizcreate]");
    const body = await (await createReq).json();
    if (body.error) {
      throw new Error(`[${viewport.label}] wizard game create failed: ${JSON.stringify(body.error)}`);
    }
    await page.waitForFunction(() => !document.querySelector(".wizard"), null, { timeout: 10000 });
    return body;
  };

  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });
    await page.click('.tab[data-tab="setup"]');
    await page.click('[data-setup-view="records"]');
    await page.waitForSelector(".setup-card", { timeout: 10000 });

    // (1) One facility Organization owns two Venues.
    const orgFacility = await createViaDrawer("organization",
      { "f-org": "Twin Rinks Facility" }, "/api/v2/setup/organization");
    const venueShared = await createViaDrawer("venue",
      { "f-venue": "Twin Rinks Arena", "f-venue-org": orgFacility.id }, "/api/v2/setup/venue");
    const rinkShared = await createViaDrawer("rink",
      { "f-rink-venue": venueShared.id, "f-rink": "Twin Rinks Main Sheet" }, "/api/v2/setup/rink");
    const slotSharedA = await createViaDrawer("ice-slot", {
      "f-slot-rink": rinkShared.id, "f-slot-date": CAL_DAY,
      "f-slot-start": "18:00", "f-slot-end": "19:00",
    }, "/api/v2/setup/ice-slot");
    const slotSharedB = await createViaDrawer("ice-slot", {
      "f-slot-rink": rinkShared.id, "f-slot-date": CAL_DAY,
      "f-slot-start": "20:00", "f-slot-end": "21:00",
    }, "/api/v2/setup/ice-slot");
    const venueSecond = await createViaDrawer("venue",
      { "f-venue": "North Annex Rink", "f-venue-org": orgFacility.id }, "/api/v2/setup/venue");
    const rinkSecond = await createViaDrawer("rink",
      { "f-rink-venue": venueSecond.id, "f-rink": "North Annex Sheet 1" }, "/api/v2/setup/rink");
    // Staggered so it doesn't overlap slotSharedA — Program A's two teams
    // play both of Program A's games, and a team can't be in two games at once.
    const slotSecond = await createViaDrawer("ice-slot", {
      "f-slot-rink": rinkSecond.id, "f-slot-date": CAL_DAY,
      "f-slot-start": "21:00", "f-slot-end": "22:00",
    }, "/api/v2/setup/ice-slot");

    // (2) Two independent Programs, each operated by its OWN organization —
    // distinct from the facility owner and from each other.
    const orgProgramA = await createViaDrawer("organization",
      { "f-org": "Twin Rinks Adult Hockey" }, "/api/v2/setup/organization");
    const programA = await createViaDrawer("league",
      { "f-league": "Adult Men", "f-league-org": orgProgramA.id }, "/api/v2/setup/program");
    const seasonA = await createViaDrawer("season",
      { "f-season-league": programA.id, "f-season": "2026-27 Adult" }, "/api/v2/setup/season");
    const leagueA = await createViaDrawer("level",
      { "f-level-season": seasonA.id, "f-level": "Adult League" }, "/api/v2/setup/league");
    const divisionA = await createViaDrawer("division",
      { "f-div-league": leagueA.id, "f-div": "Gold" }, "/api/v2/setup/division");
    const clubA = await createViaDrawer("club",
      { "f-club": "Adult Club" }, "/api/v2/setup/club");
    const teamA1 = await createViaDrawer("team",
      { "f-team-club": clubA.id, "f-team-league": programA.id, "f-team": "Adult D1" },
      "/api/v2/setup/team");
    const teamA2 = await createViaDrawer("team",
      { "f-team-club": clubA.id, "f-team-league": programA.id, "f-team": "Adult D2" },
      "/api/v2/setup/team");

    const orgProgramB = await createViaDrawer("organization",
      { "f-org": "Illinois High School Hockey" }, "/api/v2/setup/organization");
    const programB = await createViaDrawer("league",
      { "f-league": "High School", "f-league-org": orgProgramB.id }, "/api/v2/setup/program");
    const seasonB = await createViaDrawer("season",
      { "f-season-league": programB.id, "f-season": "2026-27 Varsity" }, "/api/v2/setup/season");
    const leagueB = await createViaDrawer("level",
      { "f-level-season": seasonB.id, "f-level": "Varsity League" }, "/api/v2/setup/league");
    const teamB1 = await createViaDrawer("team",
      { "f-team-club": "", "f-team-league": programB.id, "f-team": "Varsity Home" },
      "/api/v2/setup/team");
    const teamB2 = await createViaDrawer("team",
      { "f-team-club": "", "f-team-league": programB.id, "f-team": "Varsity Away" },
      "/api/v2/setup/team");

    // Team-registration creation is Season participation's own already-proven
    // UI (Slice B2b) — out of THIS regression's scope, so built via raw fetch
    // like every other journey's non-target prerequisites.
    await page.evaluate(async (i) => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      await post(`/api/v2/setup/seasons/${i.seasonA}/team-registrations`,
        { team_id: i.teamA1, league_id: i.leagueA, division_id: i.divisionA });
      await post(`/api/v2/setup/seasons/${i.seasonA}/team-registrations`,
        { team_id: i.teamA2, league_id: i.leagueA, division_id: i.divisionA });
      await post(`/api/v2/setup/seasons/${i.seasonB}/team-registrations`,
        { team_id: i.teamB1, league_id: i.leagueB });
      await post(`/api/v2/setup/seasons/${i.seasonB}/team-registrations`,
        { team_id: i.teamB2, league_id: i.leagueB });
    }, {
      seasonA: seasonA.id, leagueA: leagueA.id, divisionA: divisionA.id,
      teamA1: teamA1.id, teamA2: teamA2.id,
      seasonB: seasonB.id, leagueB: leagueB.id, teamB1: teamB1.id, teamB2: teamB2.id,
    });

    // (3) Season A uses BOTH venues — one Season, multiple Venues.
    await page.click('[data-setup-view="hierarchy"]');
    await grantViaUi(seasonA.id, venueShared.id);
    await grantViaUi(seasonA.id, venueSecond.id);

    // (4) Season B is granted the SAME shared venue Season A already uses —
    // one Venue, multiple independent Programs/Seasons — and the picker
    // still offers it despite Program A's grant.
    await grantViaUi(seasonB.id, venueShared.id);

    // Neither Season's Allowed-venues list leaks the other's rows: together
    // they add up to exactly 3 active grants (2 for Season A + 1 for B).
    const totalActiveGrants = await page.$$eval("[data-va-revoke]", (els) => els.length);
    if (totalActiveGrants !== 3) {
      throw new Error(`[${viewport.label}] expected 3 total active venue-access rows across ` +
        `both seasons (2 for A + 1 for B), found ${totalActiveGrants}`);
    }

    // (5) Program A schedules a Game on the shared venue AND on its second
    // venue; Program B independently schedules a Game on the same shared
    // venue — proving eligibility and isolation both hold end to end.
    const gameA1 = await createGameViaWizard(
      slotSharedA.id, leagueA.id, divisionA.id, teamA1.id, teamA2.id);
    const gameA2 = await createGameViaWizard(
      slotSecond.id, leagueA.id, divisionA.id, teamA1.id, teamA2.id);
    const gameB1 = await createGameViaWizard(
      slotSharedB.id, leagueB.id, null, teamB1.id, teamB2.id);
    if (!gameA1.id || !gameA2.id || !gameB1.id) {
      throw new Error(`[${viewport.label}] one or more wizard game creates did not return an id`);
    }

    if (errors.length) {
      throw new Error(`[${viewport.label}] console/page errors:\n${errors.join("\n")}`);
    }
    console.log(`[${viewport.label}] OK — multi-venue Season, multi-program Venue, and ` +
      `facility-owner/operator independence all confirmed through the real UI.`);
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
    console.log("Venue-sharing browser journey passed.");
  } catch (error) {
    console.error("Venue-sharing browser journey FAILED.");
    console.error(error && error.message ? error.message : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
