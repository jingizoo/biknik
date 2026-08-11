// The Dashboard's Season CEILING (#369 review's Required Regression, browser
// layer).
//
// #369 review rejected an earlier revision of get_demo_overview that treated
// the active Season as a soft hint (every Season in the resolved Program was
// in scope). The corrected contract is a HARD ceiling -- with a real role
// supplied, `in_scope_season_ids = {season.id} if season is not None else
// set()` -- which narrows divisions, registrations, venues/rinks/ice_slots
// (via SeasonVenueAccess), games/schedule/public_fixtures, and the standings
// snapshot the Dashboard keys off `ov.divisions[0]`. A Program-only active
// context (no Season resolved) narrows to ZERO Seasons, never "all of them".
//
// The review's Required Regression, verbatim:
//
//   "create two Seasons in the same Program with the same League and
//    distinguishable divisions, registrations, games, results, and venue
//    grants; with S1 active, every affected Dashboard/standings/facility
//    value must exclude S2, while switching to S2 flips the data. Cover
//    active League and No League, archived S2, Memory/SQLite/PostgreSQL,
//    authenticated HTTP, desktop, and 390px."
//
// That regression is deliberately SPLIT across two layers, and this file is
// only one half of it:
//   * the Memory/SQLite/PostgreSQL store matrix (plus the facade-level
//     role/negative matrix) is backend/tests/test_league_filtered_dashboard.py
//     -- specifically test_s1_active_excludes_s2_even_when_s2_is_archived,
//     test_s2_active_excludes_s1, test_program_only_context_narrows_season_
//     scoped_collections_to_empty, and test_facility_inventory_now_excludes_a_
//     different_seasons_venue_grant, each looping every configured store. A
//     store matrix is a persistence-layer question, and re-running the same
//     fixture through three stores from a browser would mean three server
//     processes proving something no pixel can observe;
//   * THIS file is the authenticated-HTTP + desktop + 390px half: one real
//     signed-in session, the REAL Dashboard view, and the REAL context bar
//     (#ctx-select / #ctx-league-select) driving every switch -- the point of
//     the assertions is that the PERSISTED context drives the read, so the
//     context is never poked into localStorage or set through a direct API
//     call here.
// Neither half is redundant and neither is a gap: read them together.
//
// Explicitly NOT re-tested here (covered elsewhere, not duplicated):
//   * The context bar's own behavior -- persistence, hash sync, keyboard
//     reach, archived read-only badge, atomic rejection -- context-switcher.js
//     and league-context-bar.js own that.
//   * The League axis's own narrowing within ONE Season, and the render()
//     staleness guard -- league-filtered-data.js owns those.
//   * get_standings' active-tuple ownership check -- test_league_filtered_
//     standings.py owns that; here the standings snapshot is only read as one
//     more Dashboard value that must respect the ceiling.
//
// Fails on any unexpected browser console/page error. Runs the whole matrix
// at desktop (1440x900) and canonical 390x844.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");
const { installContextFixture } = require("./context-fixture.js");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8391 },
  { label: "phone", width: 390, height: 844, port: 8392 },
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
function startServer(port, env) {
  return spawn(
    process.env.PYTHON || "python3",
    ["-u", "-m", "hockey_scheduler.web.server", "--host", HOST, "--port", String(port)],
    { cwd: BACKEND_DIR, stdio: ["ignore", "pipe", "pipe"], env: { ...process.env, ...env } });
}

function apiPost(page, url, body) {
  return page.evaluate(async ([u, b]) => {
    const r = await fetch(u, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(b || {}),
    });
    return { status: r.status, json: await r.json().catch(() => ({})) };
  }, [url, body]);
}
const loginAs = (page, username, password) =>
  apiPost(page, "/api/auth/login", { username, password: password || "demo" });

async function newPage(browser, viewport) {
  const context = await browser.newContext({
    viewport: { width: viewport.width, height: viewport.height },
  });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(`[pageerror] ${e.message}`));
  page.on("console", (m) => {
    if (m.type() === "error" && !/Failed to load resource/.test(m.text())) {
      errors.push(`[console] ${m.text()}`);
    }
  });
  return { context, page, errors };
}

function fail(msg) { throw new Error(msg); }

// ---------------------------------------------------------------------------
// Fixture: ONE Program, ONE operator Organization, TWO Seasons, and the SAME
// two Leagues bound to BOTH Seasons -- the review's "same League" requirement,
// so nothing below can pass merely because S1 and S2 happen to live under
// different Leagues (the League axis league-filtered-data.js already covers).
//
// Every Season-scoped record is named with its own "S1-"/"S2-" token so both
// directions of every assertion (the active Season's value PRESENT, the other
// Season's value ABSENT) can be checked by name rather than by count alone:
//   S1: divisions S1-DivA / S1-DivB, teams S1-Team*, venue S1-Venue,
//       rink S1-Rink, 3 ice slots, 2 published games with FINAL results
//   S2: divisions S2-DivA / S2-DivB, teams S2-Team*, venue S2-Venue,
//       rink S2-Rink, 4 ice slots, 2 published games with FINAL results
// The ice-slot TOTALS differ deliberately (3 vs 4, union 7): the Dashboard's
// "Ice slots booked" tile is the one place a SeasonVenueAccess grant is
// observable as a number, so a ceiling failure there reads as 7, not 3.
// The A/B suffix on division names exists so no expected name is a prefix of
// another ("S1-Div" would substring-match "S1-DivB" and make the standings
// card assertion meaningless).
//
// Two Leagues (not one) are what make the "No League" half of the requirement
// a real test rather than a vacuous one: under No League the Dashboard must
// broaden to BOTH Leagues' games WITHIN the active Season (positive control)
// while still excluding the other Season entirely (negative control). With a
// single League, a No-League implementation that returned nothing at all
// would pass.
//
// Teams are deliberately NOT part of any "absent" assertion: ov.teams is
// Program+League scoped and NOT Season scoped by design, so all eight teams
// are in scope in both Seasons. They are only ever read INSIDE a game row or
// a standings row, both of which are genuinely Season-scoped.
async function buildFixture(page) {
  const built = await page.evaluate(async () => {
    const F = window.hsFixture;
    // Kept as a thin alias so the non-create calls below (publish, result,
    // approve, assign-division) stay one-liners; it is `F.call`, so a refusal
    // still fails at its own line instead of decoding into a usable-looking
    // body (#409).
    const post = (p, b) => F.call(p, p, b);
    const org = await F.create("org", "/api/v2/setup/organization", { name: "Ceiling Org" });
    const program = await F.create("program", "/api/v2/setup/program",
      { name: "Ceiling Program", operator_organization_id: org.id });
    // #409 EXPLICIT SELECTION, boundary 1. Both Season creates are
    // PROGRAM-AXIS, and minting the Program above is not a selection — so the
    // Program-only choice is PERSISTED here and proved before either Season
    // exists. ./context-fixture.js carries the axis table.
    await F.selectProgram("Program-only bootstrap", program.id);
    const s1 = await F.create("Season One", "/api/v2/setup/season",
      { program_id: program.id, name: "Season One" });
    const s2 = await F.create("Season Two", "/api/v2/setup/season",
      { program_id: program.id, name: "Season Two" });
    // BOUNDARY 2. Everything from here to the S2 block is SEASON-OWNED and
    // writes into Season One, so Season One must be the SAVED Season — not
    // merely "a" Season, and not whichever one a create happens to name.
    await F.selectProgramSeason("Program + Season One", program.id, s1.id);
    // Creating a League against a Season binds it to THAT Season (the only
    // HTTP-reachable League create); the S2 binding for the SAME two Leagues
    // is created further down, by the first S2 registration.
    const leagueA = await F.create("leagueA", "/api/v2/setup/league",
      { season_id: s1.id, name: "League Alpha" });
    const leagueB = await F.create("leagueB", "/api/v2/setup/league",
      { season_id: s1.id, name: "League Bravo" });
    const club = await F.create("club", "/api/v2/setup/club", { name: "Ceiling Club" });
    const team = (name, leagueId) => F.create("team", "/api/v2/setup/team",
      { club_id: club.id, name, league_id: leagueId });
    // A Team is a PERMANENT member of one League (#180/#283 rule 7), so each
    // Season's teams are separate Team records under the same two Leagues.
    const s1a1 = await team("S1-TeamA1", leagueA.id);
    const s1a2 = await team("S1-TeamA2", leagueA.id);
    const s1b1 = await team("S1-TeamB1", leagueB.id);
    const s1b2 = await team("S1-TeamB2", leagueB.id);
    const s2a1 = await team("S2-TeamA1", leagueA.id);
    const s2a2 = await team("S2-TeamA2", leagueA.id);
    const s2b1 = await team("S2-TeamB1", leagueB.id);
    const s2b2 = await team("S2-TeamB2", leagueB.id);

    const division = (leagueId, name, seasonId) => F.create("division", "/api/v2/setup/division",
      { league_id: leagueId, name, season_id: seasonId });
    // A registration's id is used further down (assign-division), so this is
    // an ASSERTED create, not a bare call — a refusal must not reach the next
    // line as an id-less body (#409).
    const register = (seasonId, teamId, leagueId, divisionId) =>
      F.create("team registration",
        `/api/v2/setup/seasons/${seasonId}/team-registrations`,
        { team_id: teamId, league_id: leagueId, division_id: divisionId || null });

    const s1DivA = await division(leagueA.id, "S1-DivA", s1.id);
    const s1DivB = await division(leagueB.id, "S1-DivB", s1.id);
    await register(s1.id, s1a1.id, leagueA.id, s1DivA.id);
    await register(s1.id, s1a2.id, leagueA.id, s1DivA.id);
    await register(s1.id, s1b1.id, leagueB.id, s1DivB.id);
    await register(s1.id, s1b2.id, leagueB.id, s1DivB.id);

    // A Division can only be created against an EXISTING LeagueSeason binding
    // (create_division_under_league resolves it exactly and never creates
    // one), so S2's two bindings have to exist first. Registering a Team with
    // an explicit league_id is the HTTP path that creates them:
    // _resolve_registration_league_season find-or-creates LeagueSeason(League,
    // Season). Hence: register one team per League into S2 with no division,
    // create S2's divisions, then assign those two registrations into them.
    // #409: the S2 block writes into Season Two, so the saved Season moves
    // with it. The registrations, the Divisions and the division assignments
    // below are all SEASON-OWNED and all land in Season Two.
    await F.selectProgramSeason("Program + Season Two", program.id, s2.id);
    const s2RegA1 = await register(s2.id, s2a1.id, leagueA.id, null);
    const s2RegB1 = await register(s2.id, s2b1.id, leagueB.id, null);
    const s2DivA = await division(leagueA.id, "S2-DivA", s2.id);
    const s2DivB = await division(leagueB.id, "S2-DivB", s2.id);
    await post(`/api/v2/setup/season-team-registration/${s2RegA1.id}/assign-division`,
      { division_id: s2DivA.id });
    await post(`/api/v2/setup/season-team-registration/${s2RegB1.id}/assign-division`,
      { division_id: s2DivB.id });
    await register(s2.id, s2a2.id, leagueA.id, s2DivA.id);
    await register(s2.id, s2b2.id, leagueB.id, s2DivB.id);

    // One Venue per Season, each granted to ITS OWN Season only -- the
    // SeasonVenueAccess axis get_demo_overview scopes venues/rinks/ice slots
    // through. Different slot COUNTS (3 vs 4) so the Dashboard's ice tile
    // reports a number that is unique to the active Season.
    const venue = (name) => F.create("venue", "/api/v2/setup/venue",
      { name, organization_id: org.id });
    const s1Venue = await venue("S1-Venue");
    const s2Venue = await venue("S2-Venue");
    // A venue-access grant is Season-bound (#369 prereq, merged from #372):
    // it binds to the ACTIVE Season, so this two-Season fixture selects each
    // destination before granting into it, and asserts the grant landed rather
    // than parsing a refusal as ordinary JSON. Leaves the context on s1, which
    // is what the ceiling assertions below expect.
    const grantVenue = async (seasonId, venueId, label) => {
      // The selection is the app's own switch pipeline and is READ BACK
      // (#409), replacing a raw `POST /api/context` whose status was dropped
      // — a refused switch used to be indistinguishable from a successful one
      // until the grant that depended on it failed.
      await F.selectProgramSeason(`${label} destination Season`,
        program.id, seasonId);
      await F.create(`${label} venue-access grant`,
        `/api/v2/setup/seasons/${seasonId}/venue-access`, { venue_id: venueId });
    };
    await grantVenue(s2.id, s2Venue.id, "S2");
    await grantVenue(s1.id, s1Venue.id, "S1");
    const s1Rink = await F.create("s1Rink", "/api/v2/setup/rink",
      { venue_id: s1Venue.id, name: "S1-Rink" });
    const s2Rink = await F.create("s2Rink", "/api/v2/setup/rink",
      { venue_id: s2Venue.id, name: "S2-Rink" });
    // Far-future, one game per day: the Dashboard's game list shows TODAY's
    // games and falls back to every scheduled game when none is today, so
    // dates that can never be "today" keep the list stable whenever this runs.
    const slot = (rinkId, date) => F.create("slot", "/api/v2/setup/ice-slot", {
      rink_id: rinkId,
      start_time: `${date}T18:00:00+00:00`, end_time: `${date}T20:00:00+00:00`,
    });
    const s1Slot1 = await slot(s1Rink.id, "2030-01-05");
    const s1Slot2 = await slot(s1Rink.id, "2030-01-06");
    await slot(s1Rink.id, "2030-01-07");
    const s2Slot1 = await slot(s2Rink.id, "2030-02-05");
    const s2Slot2 = await slot(s2Rink.id, "2030-02-06");
    await slot(s2Rink.id, "2030-02-07");
    await slot(s2Rink.id, "2030-02-08");

    // Scheduled + PUBLISHED + a recorded, APPROVED (FINAL) result: only a
    // FINAL result reaches the standings snapshot, which is one of the
    // Dashboard values the ceiling has to narrow.
    // Each Game is SEASON-OWNED and lands in the Season it names, so the
    // saved Season is moved to that one first (#409).
    const game = async (seasonId, leagueId, divisionId, home, away, slotId,
                        homeScore, awayScore) => {
      await F.selectProgramSeason("Season the Game lands in", program.id, seasonId);
      const g = await F.create("game", "/api/v2/setup/game", {
        season_id: seasonId, league_id: leagueId, division_id: divisionId,
        home_team_id: home, away_team_id: away, ice_slot_id: slotId,
      });
      await post(`/api/games/${g.id}/publish`, {});
      await post(`/api/games/${g.id}/result`,
        { home_score: homeScore, away_score: awayScore });
      await post(`/api/games/${g.id}/result/approve`, {});
      return g;
    };
    await game(s1.id, leagueA.id, s1DivA.id, s1a1.id, s1a2.id, s1Slot1.id, 3, 1);
    await game(s1.id, leagueB.id, s1DivB.id, s1b1.id, s1b2.id, s1Slot2.id, 2, 2);
    await game(s2.id, leagueA.id, s2DivA.id, s2a1.id, s2a2.id, s2Slot1.id, 5, 0);
    await game(s2.id, leagueB.id, s2DivB.id, s2b1.id, s2b2.id, s2Slot2.id, 4, 3);

    return {
      program: program.id, s1: s1.id, s2: s2.id,
      leagueA: leagueA.id, leagueB: leagueB.id,
    };
  }).catch((e) => fail(`fixture build failed: ${e.message || e}`));
  return built;
}

// What the Dashboard must look like for each active Season. `token` is the
// prefix every one of that Season's own record names carries; `absent` is the
// other Season's -- asserted in BOTH directions everywhere below.
const SEASON_EXPECT = {
  s1: {
    token: "S1-", absent: "S2-", rink: "S1-Rink", iceSlots: 3,
    divisionA: "S1-DivA", divisionB: "S1-DivB",
    leagueAGame: ["S1-TeamA1", "S1-TeamA2"],
    leagueBGame: ["S1-TeamB1", "S1-TeamB2"],
  },
  s2: {
    token: "S2-", absent: "S1-", rink: "S2-Rink", iceSlots: 4,
    divisionA: "S2-DivA", divisionB: "S2-DivB",
    leagueAGame: ["S2-TeamA1", "S2-TeamA2"],
    leagueBGame: ["S2-TeamB1", "S2-TeamB2"],
  },
};

// Stamp the currently-painted Dashboard so the next read can prove it is
// looking at a FRESH render() rather than the pre-switch paint. render()
// replaces #content wholesale, so the stamp cannot survive a repaint -- this
// is a real happened-after signal, not a guessed sleep interval.
function stampDashboard(page) {
  return page.evaluate(() => {
    const el = document.querySelector(".dash-stats");
    if (el) el.dataset.ceilingStamp = "1";
  });
}
async function waitForFreshDashboard(page, step) {
  await page.waitForFunction(() => {
    const el = document.querySelector(".dash-stats");
    return !!el && el.dataset.ceilingStamp !== "1"
      && !document.querySelector("#content .skeleton");
  }, null, { timeout: 15000 }).catch(() => fail(
    `[${step}] the Dashboard never repainted after the context switch`));
}

async function selectSeason(page, value, step) {
  await stampDashboard(page);
  await page.selectOption("#ctx-select", value);
  await page.waitForFunction((v) => {
    const el = document.getElementById("ctx-select");
    return el && el.value === v;
  }, value, { timeout: 10000 }).catch(() => fail(
    `[${step}] the context select never settled on "${value}"`));
  await waitForFreshDashboard(page, step);
}

async function selectLeague(page, leagueId, step) {
  await stampDashboard(page);
  await page.selectOption("#ctx-league-select", leagueId || "");
  // A switch is fire-and-forget from the <select>'s own onchange (#345
  // contextRevision idiom) -- poll the real control, then the repaint.
  await page.waitForFunction((v) => {
    const el = document.getElementById("ctx-league-select");
    return el && el.value === (v || "");
  }, leagueId, { timeout: 10000 }).catch(() => fail(
    `[${step}] League select never settled on "${leagueId || "(No League)"}"`));
  await waitForFreshDashboard(page, step);
}

// Every Dashboard value the Season ceiling narrows, read off the REAL painted
// view: the scheduled-games list (rows carry division + rink + both team
// names), the standings snapshot card (keyed off ov.divisions[0]), and the
// ice tile (the only place SeasonVenueAccess surfaces as a number). `text` is
// renderDashboard's OWN output only -- the stats row plus the card grid --
// deliberately excluding the Home/Tasks setup-progress card, which is a
// different endpoint with its own coverage.
async function readDashboard(page, step) {
  await page.waitForSelector(".dash-stats", { timeout: 15000 }).catch(() => fail(
    `[${step}] the Dashboard stats never rendered`));
  const data = await page.evaluate(() => {
    const txt = (el) => (el ? (el.textContent || "").trim() : "");
    const rows = Array.from(document.querySelectorAll(".dash-grid .tg-row")).map((r) => ({
      teams: txt(r.querySelector(".tg-teams")),
      division: txt(r.querySelector(".tg-div")),
      rink: txt(r.querySelector(".tgt-r")),
    }));
    const heads = Array.from(document.querySelectorAll(".dash-card-head h3"));
    const standingsHead = heads.find((h) => / · Standings$/.test(h.textContent || ""));
    const standingsCard = standingsHead ? standingsHead.closest(".dash-card") : null;
    const iceStat = Array.from(document.querySelectorAll(".dash-stat")).find(
      (s) => /Ice slots booked/.test(txt(s.querySelector(".ds-label"))));
    const iceSub = iceStat ? txt(iceStat.querySelector(".ds-sub")) : "";
    const iceMatch = /of (\d+) slots/.exec(iceSub);
    const stats = document.querySelector(".dash-stats");
    const grid = document.querySelector(".dash-grid");
    return {
      rows,
      standingsHead: standingsHead ? txt(standingsHead) : null,
      standingsTeams: standingsCard
        ? Array.from(standingsCard.querySelectorAll(".ss-team")).map((e) => txt(e)) : [],
      standingsEmpty: !!(standingsCard && standingsCard.querySelector(".na-empty")),
      iceSlots: iceMatch ? parseInt(iceMatch[1], 10) : null,
      text: `${txt(stats)} ${txt(grid)}`,
    };
  });
  return data;
}

// `expectDivisions` is the exact set of division names whose games must be
// listed -- one entry when a League is selected, both when "No League" is.
function assertDashboard(step, d, season, expectDivisions) {
  const e = SEASON_EXPECT[season];
  const shown = d.rows.map((r) => r.division).sort();
  const want = expectDivisions.slice().sort();
  if (shown.length !== want.length || shown.some((v, i) => v !== want[i])) {
    fail(`[${step}] scheduled-games list must show exactly ${JSON.stringify(want)}, `
      + `got ${JSON.stringify(shown)} (rows: ${JSON.stringify(d.rows)})`);
  }
  for (const row of d.rows) {
    if (row.rink !== e.rink) {
      fail(`[${step}] every game row must sit on ${e.rink} (the active Season's `
        + `granted venue's rink), got "${row.rink}" for ${JSON.stringify(row)}`);
    }
    if (!row.teams.includes(e.token)) {
      fail(`[${step}] game row teams "${row.teams}" carry no "${e.token}" name -- `
        + "the other Season's game leaked into the list");
    }
  }
  if (d.iceSlots !== e.iceSlots) {
    fail(`[${step}] the ice tile must report ${e.iceSlots} slots (only `
      + `${e.rink}'s, via this Season's SeasonVenueAccess grant), got ${d.iceSlots}`
      + " -- 7 would mean both Seasons' venues were unioned");
  }
  if (!d.standingsHead) {
    fail(`[${step}] the standings snapshot card is missing entirely (ov.divisions `
      + "narrowed to nothing for the active Season)");
  }
  if (!d.standingsHead.includes(e.token) || d.standingsHead.includes(e.absent)) {
    fail(`[${step}] the standings snapshot must name one of the ACTIVE Season's `
      + `divisions, got "${d.standingsHead}"`);
  }
  if (!expectDivisions.some((name) => d.standingsHead.startsWith(`${name} `))) {
    fail(`[${step}] the standings snapshot names "${d.standingsHead}", which is `
      + `not one of the in-scope divisions ${JSON.stringify(expectDivisions)}`);
  }
  if (d.standingsEmpty || !d.standingsTeams.length) {
    fail(`[${step}] the standings snapshot has no rows -- this Season's own `
      + "FINAL result never reached it");
  }
  const strayStanding = d.standingsTeams.find((n) => !n.startsWith(e.token));
  if (strayStanding) {
    fail(`[${step}] standings row "${strayStanding}" belongs to the other Season `
      + `(rows: ${JSON.stringify(d.standingsTeams)})`);
  }
  // Both directions, over renderDashboard's whole painted output: the active
  // Season's token present, the other Season's nowhere at all.
  if (!d.text.includes(e.token)) {
    fail(`[${step}] no "${e.token}" value appears on the Dashboard at all`);
  }
  if (d.text.includes(e.absent)) {
    const leak = (d.text.match(new RegExp(`${e.absent}[A-Za-z0-9]*`, "g")) || []);
    fail(`[${step}] the other Season's data leaked onto the Dashboard: `
      + `${JSON.stringify(Array.from(new Set(leak)))}`);
  }
}

// ---------------------------------------------------------------------------
// The full matrix, at one viewport: League-selected and No-League, S1 and S2,
// before and after S2 is archived.
// ---------------------------------------------------------------------------
async function checkCeiling(browser, viewport) {
  const base = `http://${HOST}:${viewport.port}`;
  const server = startServer(viewport.port, {});
  let out = "";
  server.stdout.on("data", (d) => { out += d.toString(); });
  server.stderr.on("data", (d) => { out += d.toString(); });
  const { context, page, errors } = await newPage(browser, viewport);
  const L = viewport.label;
  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    if ((await loginAs(page, "admin")).status !== 200) throw new Error(`[${L}] admin login failed`);
    await page.waitForSelector("#content > *", { timeout: 10000 });
    await installContextFixture(page);
    const f = await buildFixture(page);
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });
    const view = await page.evaluate(() => document.body.dataset.view);
    if (view !== "dashboard") {
      await page.click('.tab[data-tab="dashboard"]');
      await page.waitForFunction(() => document.body.dataset.view === "dashboard",
        null, { timeout: 10000 }).catch(() => fail(
          `[${L}] never reached the Dashboard view (started on "${view}")`));
    }

    // (A) S1 active, League Alpha selected: S1's own division/game/venue/ice
    // values, and NONE of S2's -- the requirement's core case.
    await selectSeason(page, `${f.program}|${f.s1}`, `${L}/A-select-S1`);
    await page.waitForFunction(() => {
      const s = document.getElementById("ctx-league-select");
      return s && !s.hidden;
    }, null, { timeout: 10000 }).catch(() => fail(`[${L}] League select never appeared`));
    await selectLeague(page, f.leagueA, `${L}/A-league-alpha`);
    assertDashboard(`${L}/A S1+Alpha`, await readDashboard(page, `${L}/A`),
      "s1", [SEASON_EXPECT.s1.divisionA]);

    // (B) Switch Season to S2 through the REAL context bar (League Alpha is
    // bound to both Seasons, so it carries forward): everything must FLIP,
    // never accumulate.
    await selectSeason(page, `${f.program}|${f.s2}`, `${L}/B-select-S2`);
    const leagueAfterSwitch = await page.evaluate(
      () => document.getElementById("ctx-league-select").value);
    if (leagueAfterSwitch !== f.leagueA) {
      fail(`[${L}/B] a same-Program Season switch must carry the active League `
        + `forward (League Alpha is bound to both Seasons); select is now `
        + `"${leagueAfterSwitch}"`);
    }
    assertDashboard(`${L}/B S2+Alpha`, await readDashboard(page, `${L}/B`),
      "s2", [SEASON_EXPECT.s2.divisionA]);

    // (C) "No League" with S2 active: broadens across BOTH Leagues WITHIN S2
    // (both S2 divisions' games listed) and still excludes S1 entirely.
    await selectLeague(page, "", `${L}/C-no-league`);
    assertDashboard(`${L}/C S2+NoLeague`, await readDashboard(page, `${L}/C`),
      "s2", [SEASON_EXPECT.s2.divisionA, SEASON_EXPECT.s2.divisionB]);

    // (D) Still "No League", Season switched back to S1: the Season ceiling
    // applies to the broadened view too -- both of S1's Leagues, neither of
    // S2's. "No League" broadens across Leagues, NEVER across Seasons.
    await selectSeason(page, `${f.program}|${f.s1}`, `${L}/D-select-S1`);
    if ((await page.evaluate(
      () => document.getElementById("ctx-league-select").value)) !== "") {
      fail(`[${L}/D] the explicit "No League" selection must survive a Season switch`);
    }
    assertDashboard(`${L}/D S1+NoLeague`, await readDashboard(page, `${L}/D`),
      "s1", [SEASON_EXPECT.s1.divisionA, SEASON_EXPECT.s1.divisionB]);

    // (E) Archive S2. There is no archive control anywhere in the shipped UI
    // (the Season lifecycle is API-only today -- app.js only ever RENDERS the
    // resulting "archived (read-only)" option label), so this is a raw
    // authenticated POST to the same route context-switcher.js uses.
    // Season lifecycle is Season-BOUND (#367 ruling, enforced since #372):
    // archive acts on S2, so select S2 first. The context is restored to S1
    // immediately after, because every assertion below reads the Dashboard
    // under S1 and would otherwise be measuring the wrong Season.
    await apiPost(page, "/api/context",
      { program_id: f.program, season_id: f.s2 });
    const archived = await apiPost(page,
      `/api/v2/setup/seasons/${f.s2}/archive`, { reason: "season complete" });
    await apiPost(page, "/api/context",
      { program_id: f.program, season_id: f.s1 });
    if (archived.status !== 200) {
      fail(`[${L}/E] archiving S2 failed: ${archived.status} `
        + `${JSON.stringify(archived.json)}`);
    }

    // (F) Positive control FIRST: archiving must not have deleted or hidden
    // S2's data outright -- selecting the archived Season still reads its own
    // history. Without this, (G) below would pass vacuously if archiving
    // simply made S2 invisible everywhere.
    await selectSeason(page, `${f.program}|${f.s2}`, `${L}/F-select-archived-S2`);
    assertDashboard(`${L}/F archived S2 selected`, await readDashboard(page, `${L}/F`),
      "s2", [SEASON_EXPECT.s2.divisionA, SEASON_EXPECT.s2.divisionB]);

    // (G) Back to S1: S2's ARCHIVED history must stay excluded. Archived rows
    // reach the Dashboard through the same joins live rows do, so an
    // archived-history leak would be a second code path around the ceiling.
    await selectSeason(page, `${f.program}|${f.s1}`, `${L}/G-select-S1`);
    assertDashboard(`${L}/G S1+NoLeague, S2 archived`,
      await readDashboard(page, `${L}/G`), "s1",
      [SEASON_EXPECT.s1.divisionA, SEASON_EXPECT.s1.divisionB]);

    // (H) Same, with a real League selected rather than "No League" -- the
    // requirement names both context shapes for the archived case too.
    await selectLeague(page, f.leagueA, `${L}/H-league-alpha`);
    assertDashboard(`${L}/H S1+Alpha, S2 archived`,
      await readDashboard(page, `${L}/H`), "s1", [SEASON_EXPECT.s1.divisionA]);

    if (errors.length) {
      fail(`[${L}] unexpected console/page errors: ${JSON.stringify(errors)}`);
    }
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth
      - document.documentElement.clientWidth);
    if (overflow > 1) {
      fail(`[${L}] horizontal overflow: scrollWidth exceeds clientWidth by ${overflow}px`);
    }
    console.log(`[${L}] OK — S1 excludes S2 and S2 flips the data across `
      + "League-selected and No-League, before and after S2 is archived.");
  } finally {
    await context.close();
    await stopServer(server);
    if (process.env.SMOKE_DEBUG) console.error(out);
  }
}

async function main() {
  let browser;
  try {
    browser = await chromium.launch(
      process.env.SMOKE_CHROMIUM_PATH ? { executablePath: process.env.SMOKE_CHROMIUM_PATH } : {});
    for (const viewport of VIEWPORTS) await checkCeiling(browser, viewport);
    console.log("Dashboard Season ceiling browser journey passed.");
  } catch (error) {
    console.error("Dashboard Season ceiling browser journey FAILED.");
    console.error(error && error.message ? error.message : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
