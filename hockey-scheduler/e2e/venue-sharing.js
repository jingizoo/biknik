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
//      an external, unrelated operator (Illinois High School Hockey) — and
//      re-reads the PERSISTED Venue-owner and Program-operator links (not
//      just the ids submitted on the forms) to confirm all three
//      organizations are actually distinct.
//   3. Grants Program A's Season access to BOTH venues (multi-venue Season).
//   4. Grants Program B's Season access to the SAME shared venue Program A
//      already uses (multi-program Venue) — confirming the shared venue is
//      still offered in Program B's Allow picker despite Program A's grant,
//      then reads EACH Season's own "Allowed venues" subsection (scoped to
//      that Season's own hierarchy node, not a page-wide count) and asserts
//      its exact venue-name set: Season A = {shared, second}, Season B =
//      {shared} — neither list leaking the other's rows.
//   5. Attempts, through the real Calendar wizard, to create a Game for
//      Program B on the second Venue — which Season B was never granted —
//      and asserts it is rejected (venue_access_missing), the wizard stays
//      open rather than silently closing, and afterward no Game exists on
//      that slot and the slot itself is still available.
//   6. Successfully creates a Game for each Program on the shared venue, and
//      a further Game for Program A on the second venue, through the
//      Calendar wizard — proving eligibility and isolation both hold for the
//      allowed combinations while the ungranted one stays blocked.
//
// #367 review: the Calendar/wizard read (get_demo_overview) now scopes ice
// slots/venues/leagues to whichever Program is the caller's ACTIVE context,
// not to every Program the League Admin administers (that broader list is
// Setup > Records' own get_setup_overview_v2 read, a different surface) — so
// steps 5-6 above each explicitly switch the active Program (through the
// real header context switcher, `#ctx-select`) to whichever Program the step
// is acting for before touching the wizard. Program B is also given a SECOND
// Season (never bound to League B or any Team) with its own grant to the
// second Venue: #367 scopes Calendar visibility to "any of the active
// Program's Seasons has an active grant", so without it the second Venue
// would never appear in Program B's own Calendar at all, making step 5's
// negative case unreachable through real UI navigation — Season B itself
// (the one League B's teams actually play under) still has no access,
// exactly as the original acceptance criteria require.
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
  const consoleErrorHandler = (m) => { if (m.type() === "error") errors.push(`[console] ${m.text()}`); };
  page.on("console", consoleErrorHandler);

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

  // Switches the signed-in operator's active Program/Season through the real
  // header context switcher (#159/#345) — required post-#367, since the
  // Calendar/wizard read (get_demo_overview) scopes ice slots/venues/leagues
  // to whichever Program is currently ACTIVE, not every Program the caller
  // administers (that broader, unscoped list is Setup > Records' own
  // get_setup_overview_v2 read, a different surface). `#ctx-select`'s option
  // list is seeded once at page load/reload (loadContextOptions(), never
  // re-polled on every render) so a Program created after that point is
  // simply absent from it until the caller reloads.
  const switchContext = async (programId, seasonId) => {
    const value = `${programId}|${seasonId}`;
    await page.waitForSelector("#ctx-select", { timeout: 10000 });
    await page.selectOption("#ctx-select", value);
    await page.waitForFunction(
      (v) => document.getElementById("ctx-select").value === v,
      value, { timeout: 10000 });
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

  // Attempts a Game create through the real Calendar wizard on a slot whose
  // Venue has NOT been granted to this League's Season, and asserts it is
  // rejected end to end: the create POST fails with venue_access_missing,
  // the wizard stays open (no silent success path), and — after closing it —
  // no Game exists on the slot and the slot itself is still available.
  const assertGameDenied = async (slotId, leagueId, homeId, awayId) => {
    await page.click('.tab[data-tab="calendar"]');
    await page.waitForSelector(`[data-slot="${slotId}"]`, { timeout: 15000 });
    await page.click(`[data-slot="${slotId}"]`);
    await page.waitForSelector("#w-league", { timeout: 10000 });
    await page.selectOption("#w-league", leagueId);
    await page.waitForFunction(
      (t) => !!Array.from(document.querySelectorAll("#w-home option")).find((o) => o.value === t),
      homeId, { timeout: 10000 });
    await page.selectOption("#w-home", homeId);
    await page.selectOption("#w-away", awayId);
    const createReq = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/game` && r.request().method() === "POST");
    // A deliberately-rejected create's 400 response logs a benign "Failed to
    // load resource" Chromium console entry, not a page bug (same pattern as
    // venue-access-cleanup.js's blocked-delete checks).
    page.off("console", consoleErrorHandler);
    await page.click("[data-wizcreate]");
    const body = await (await createReq).json();
    page.on("console", consoleErrorHandler);
    const reason = body.error && body.error.details && body.error.details.reason;
    if (reason !== "venue_access_missing") {
      throw new Error(`[${viewport.label}] expected the ungranted-Venue create to be rejected ` +
        `with venue_access_missing, got: ${JSON.stringify(body)}`);
    }
    // render() briefly shows a loading skeleton (re-fetching state) before
    // redrawing — the wizard element itself is absent during that window
    // even though `wizard` was never nulled — so wait for the full re-render
    // to settle rather than checking .wizard the instant the response lands.
    try {
      await page.waitForSelector(".wizard", { timeout: 5000 });
    } catch (_) {
      throw new Error(`[${viewport.label}] wizard closed despite a rejected, blocked game create`);
    }
    // The error toast doesn't auto-dismiss and, at 390px, overlaps the
    // wizard's Cancel button — dismiss it first via its own close control.
    const toastClose = await page.$("[data-toast-close]");
    if (toastClose) await toastClose.click();
    await page.click("[data-wizcancel]");
    await page.waitForFunction(() => !document.querySelector(".wizard"), null, { timeout: 10000 });
    const outcome = await page.evaluate(async (sid) => {
      const ov = await (await fetch("/api/demo/overview", { credentials: "same-origin" })).json();
      const slot = (ov.ice_slots || []).find((s) => s.id === sid);
      return {
        status: slot && slot.status,
        gameOnSlot: (ov.schedule || []).some((g) => g.ice_slot_id === sid),
      };
    }, slotId);
    if (outcome.gameOnSlot) {
      throw new Error(`[${viewport.label}] a Game exists on the denied slot despite the block`);
    }
    if (outcome.status !== "available") {
      throw new Error(`[${viewport.label}] the denied slot's status is "${outcome.status}", ` +
        `expected it to remain "available"`);
    }
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
      { "f-team-club": clubA.id, "f-team-perm-league": leagueA.id, "f-team": "Adult D1" },
      "/api/v2/setup/team");
    const teamA2 = await createViaDrawer("team",
      { "f-team-club": clubA.id, "f-team-perm-league": leagueA.id, "f-team": "Adult D2" },
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
      { "f-team-club": "", "f-team-perm-league": leagueB.id, "f-team": "Varsity Home" },
      "/api/v2/setup/team");
    const teamB2 = await createViaDrawer("team",
      { "f-team-club": "", "f-team-perm-league": leagueB.id, "f-team": "Varsity Away" },
      "/api/v2/setup/team");
    // A SECOND Season under Program B, deliberately unrelated to League B —
    // #367 scoped the Calendar's ice-slot/venue view to "any of the active
    // Program's Seasons has an active grant" (Program-level visibility), not
    // to whichever Season is currently selected. Without SOME Season under
    // Program B holding access to the second Venue, that Venue (and its ice
    // slots) would never appear in Program B's own Calendar at all, making
    // the ungranted-Venue negative case below unreachable through real UI
    // navigation. Granting THIS season (never bound to League B or used by
    // any Team) keeps the Venue discoverable while Season B itself — the one
    // League B's teams actually play under — still has no access, so the
    // negative case's exact original assumption ("Season B was never
    // granted") stays true to the letter.
    const seasonBAlt = await createViaDrawer("season",
      { "f-season-league": programB.id, "f-season": "2026-27 JV (unused)" },
      "/api/v2/setup/season");
    // A THIRD ice slot on the second Venue, deliberately never granted to
    // Season B — the ungranted-Venue negative case below (#258 review).
    const slotSecondDenied = await createViaDrawer("ice-slot", {
      "f-slot-rink": rinkSecond.id, "f-slot-date": CAL_DAY,
      "f-slot-start": "10:00", "f-slot-end": "11:00",
    }, "/api/v2/setup/ice-slot");

    // (#258 review) Confirm the persisted owner/operator links, not just the
    // ids submitted on the drawer forms: the Venue owner and both Program
    // operators must be the three DISTINCT organizations just created.
    if (venueShared.organization_id !== orgFacility.id
      || venueSecond.organization_id !== orgFacility.id) {
      throw new Error(`[${viewport.label}] a Venue's persisted organization_id does not match ` +
        `the facility owner it was created under`);
    }
    if (programA.operator_organization_id !== orgProgramA.id
      || programB.operator_organization_id !== orgProgramB.id) {
      throw new Error(`[${viewport.label}] a Program's persisted operator_organization_id does ` +
        `not match the organization it was created under`);
    }
    const distinctOrgIds = new Set([orgFacility.id, orgProgramA.id, orgProgramB.id]);
    if (distinctOrgIds.size !== 3) {
      throw new Error(`[${viewport.label}] the facility owner and the two Program operators are ` +
        `not three distinct organizations`);
    }

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
    // Program-level-only visibility grant (see seasonBAlt's own comment
    // above) — keeps the second Venue in Program B's Calendar scope without
    // giving Season B (or League B) itself any access to it.
    await grantViaUi(seasonBAlt.id, venueSecond.id);

    // Neither Season's Allowed-venues list leaks the other's rows: read each
    // Season's OWN "Allowed venues" subsection (scoped from its delete button
    // in the Season's own <details> node, not a page-wide count) and check
    // the exact venue-name set it lists (#258 review).
    const allowedVenueNamesFor = async (seasonId) => page.evaluate((sid) => {
      // Season nodes are duplicated across trees (Competition structure AND
      // Season participation); only the latter — scoped by its own
      // #season-participation container — carries the venueAccessSection
      // this journey needs (#258 review fix: an unscoped querySelector can
      // silently grab the wrong copy).
      const panel = document.getElementById("season-participation");
      const del = panel && panel.querySelector(`[data-del="season"][data-del-id="${sid}"]`);
      if (!del) return null;
      const seasonDetails = del.closest("details.tn");
      const child = Array.from(seasonDetails.querySelectorAll(
        ":scope > div.tn-children > details.tn"))
        .find((d) => (d.querySelector("summary")?.textContent || "").includes("Allowed venues"));
      if (!child) return null;
      return Array.from(child.querySelectorAll("[data-va-revoke]")).map((btn) =>
        btn.closest(".tn-leaf").querySelector(".tn-label").textContent.trim()
          .replace(/^\S+\s*/, ""));  // drop the leading emoji glyph
    }, seasonId);

    const namesA = await allowedVenueNamesFor(seasonA.id);
    const namesB = await allowedVenueNamesFor(seasonB.id);
    const sameSet = (actual, expected) =>
      actual && actual.length === expected.length
      && expected.every((n) => actual.includes(n));
    if (!sameSet(namesA, [venueShared.name, venueSecond.name])) {
      throw new Error(`[${viewport.label}] Season A's Allowed-venues list should be exactly ` +
        `{${venueShared.name}, ${venueSecond.name}}, found ${JSON.stringify(namesA)}`);
    }
    if (!sameSet(namesB, [venueShared.name])) {
      throw new Error(`[${viewport.label}] Season B's Allowed-venues list should be exactly ` +
        `{${venueShared.name}}, found ${JSON.stringify(namesB)}`);
    }

    // The header context switcher's option list is only seeded at page
    // load/reload (#367) — Program B (and its Seasons) were created well
    // after this page loaded, so a reload is required before `#ctx-select`
    // can offer them at all.
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });
    // A reload can land on the onboarding shell (progress < all stages) rather
    // than the normal tabbed interface -- that shell doesn't run the ordinary
    // render()/renderContextSwitcher() pipeline. Navigate to a real tab to
    // reach it, exactly as an operator would.
    await page.click('.tab[data-tab="calendar"]');
    await page.waitForFunction(() => {
      const wrap = document.getElementById("context-switcher");
      return wrap && !wrap.hidden;
    }, null, { timeout: 15000 });

    // (5) Program B's Season was never granted the second Venue — attempting
    // a Game there through the real Calendar UI must be rejected, with zero
    // mutation (no Game created, slot stays available), not merely "some
    // positive path happens to succeed elsewhere" (#258 review). Program B
    // must be the ACTIVE context (#367) for its own League to be selectable
    // in the wizard at all.
    await switchContext(programB.id, seasonB.id);
    await assertGameDenied(slotSecondDenied.id, leagueB.id, teamB1.id, teamB2.id);

    // (6) Program A schedules a Game on the shared venue AND on its second
    // venue; Program B independently schedules a Game on the same shared
    // venue — proving eligibility and isolation both hold end to end. Each
    // creation needs ITS OWN Program active (#367 scopes the wizard's League
    // choices to the active Program only).
    await switchContext(programA.id, seasonA.id);
    const gameA1 = await createGameViaWizard(
      slotSharedA.id, leagueA.id, divisionA.id, teamA1.id, teamA2.id);
    const gameA2 = await createGameViaWizard(
      slotSecond.id, leagueA.id, divisionA.id, teamA1.id, teamA2.id);
    await switchContext(programB.id, seasonB.id);
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
