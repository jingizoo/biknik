// Setup v2 (`get_setup_overview_v2`) context-scope regression -- the #369
// review's Required Regression, at the browser layer.
//
// #369's review correction rearchitected get_setup_overview_v2
// (backend/hockey_scheduler/api/service.py) so that EVERY collection it
// returns ceilings on the persisted ACTIVE Program (ContextService.
// resolve_with_league), not on the caller's full authorized Program set:
//   * `programs` collapses to just `[active_program]`;
//   * `seasons`/`leagues` are all of the active Program's (Season is NOT a
//     further ceiling on this structural surface, unlike the Dashboard);
//   * `divisions`/`teams` narrow further when a League is selected;
//   * clubs / organizations / officials / venues / rinks / ice_slots have no
//     direct Program FK and are scoped by DERIVED JOINS into the active
//     Program (Club: >=1 Team there; Venue: an active SeasonVenueAccess grant
//     to one of the Program's Seasons; Rink/IceSlot: cascade from Venue;
//     Organization: owns >=1 in-scope Venue; Official: home_club in-scope OR
//     an assignment whose Game's Season is in-scope);
//   * the additive `unassigned_*` lists carry records linked to NO Program at
//     all, which app.js's `withUnassigned(sv, key)` unions back into the
//     Setup Records cards and create-drawer pickers so create-then-link flows
//     still work.
//
// This file is the reviewer's verbatim Required Regression: "seed two
// authorized Programs and two Leagues with distinguishable teams, players,
// venues/rinks/slots, clubs/orgs, and officials; prove Program A + No League
// excludes Program B everywhere, and Program A + League A excludes League B,
// at desktop and 390px." Every record carries an unmistakable PROGA-/PROGB-
// name, so a leak shows up as a SUBSTRING of the rendered card rather than
// only as an id mismatch, and every check runs through the REAL Setup Records
// UI (the SETUP_ENTITIES cards) with the context driven by the REAL context
// bar (#ctx-select / #ctx-league-select), never by poking the context API or
// localStorage -- the whole point is that the PERSISTED context drives the
// read.
//
// Explicitly NOT re-tested here (covered elsewhere, not duplicated):
//   * The scoping/derived-join logic's own correctness across roles, stores
//     and negative cases -- test_league_filtered_overview_v2.py's facade
//     matrix.
//   * The context bar itself (persistence, hash, keyboard reach, atomic
//     cross-Program rejection) -- league-context-bar.js's own scope.
//   * The Setup HUB's client-side League narrowing and the render()
//     generation guard -- league-filtered-data.js's own scope.
//   * The `Ice slots` card, which deliberately renders no list at all (ice
//     inventory is managed on the Arena Calendar), so `ice_slots` /
//     `unassigned_ice_slots` have no Records surface to assert against here;
//     slots are still seeded per Program so the fixture matches the
//     reviewer's wording, and their scoping is asserted at the facade level.
// The `Players` card USED to be excluded here, on the reasoning that it is
// sourced from its own /api/players call rather than this endpoint's payload
// and so sat outside get_setup_overview_v2's contract. That reasoning was the
// blind spot: the card is on this very screen, the requirement names players
// among the entities that must not cross Programs, and /api/players was in
// fact still answering installation-wide -- it later turned out to leak
// another Program's player names AND emails outright, including through an
// explicit ?team_id=. Players are now tagged with their Program like every
// other entity, so the grid-wide "no foreign PROG* token anywhere" net covers
// the Players card as well. "Fed by a different endpoint" is a reason to
// assert MORE here, not less.
//
// Fails on any unexpected browser console/page error, and on horizontal
// overflow. Runs the full matrix at desktop (1440x900) and canonical 390x844.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8381 },
  { label: "phone", width: 390, height: 844, port: 8382 },
];

// The two never-linked bootstrap records (#369's `unassigned_*` contract):
// a Club with no Team anywhere and a Venue with no SeasonVenueAccess grant
// and no owning Organization. Neither is any Program's data, so BOTH
// Programs' Setup Records must keep showing them -- the create-then-link
// flows depend on it.
const FREE_CLUB = "FREE-Club";
const FREE_VENUE = "FREE-Venue";

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

// The two Programs' expected record names, one entry per Setup Records card
// this journey asserts on. Both Programs are built by the SAME fixture
// builder, so "Program B excludes A" is exactly as strong a check as
// "Program A excludes B" -- neither direction can pass by accident because
// one side happens to have less data.
const PROGRAM_A = {
  tag: "PROGA", program: "PROGA-Program", season: "PROGA-Season",
  leagues: ["PROGA-League-A1", "PROGA-League-A2"],
  divisions: ["PROGA-Division-A1", "PROGA-Division-A2"],
  teams: ["PROGA-Team-A1", "PROGA-Team-A2"],
  players: ["PROGA-Player-A1", "PROGA-Player-A2"],
  club: "PROGA-Club", org: "PROGA-Org", official: "PROGA-Official",
  venue: "PROGA-Venue", rink: "PROGA-Rink",
};
const PROGRAM_B = {
  tag: "PROGB", program: "PROGB-Program", season: "PROGB-Season",
  leagues: ["PROGB-League-B1"],
  divisions: ["PROGB-Division-B1"],
  teams: ["PROGB-Team-B1"],
  players: ["PROGB-Player-B1"],
  club: "PROGB-Club", org: "PROGB-Org", official: "PROGB-Official",
  venue: "PROGB-Venue", rink: "PROGB-Rink",
};

// Two fully-populated, independently authorized Programs -- each with its own
// Organization, Season, Club, Venue (granted to its own Season), Rink, ice
// slot and Official, plus one League per competition with its own Division,
// Team, registration and Player. Program A gets TWO Leagues (A1/A2) so the
// League axis has something to narrow between; Program B gets one, which is
// all the cross-Program checks need.
//
// Built via raw fetch POSTs (the established convention -- see
// league-filtered-data.js and team-club-optional.js) so this journey's UI
// interactions stay entirely on the behavior under test. Every Season gets a
// League and every Program a granted Venue + a real registration, because
// get_onboarding_status_v2's readiness checks are INSTALLATION-WIDE: a Season
// without a League, or an install without schedulable ice, redirects a fresh
// League Admin session into the Initial Setup wizard instead of the normal
// shell, regardless of anything #369 changed.
async function buildFixture(page) {
  return page.evaluate(async () => {
    const post = async (p, b) => {
      const r = await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      });
      const j = await r.json().catch(() => ({}));
      if (j && j.error) throw new Error(`${p} -> ${JSON.stringify(j.error)}`);
      return j;
    };
    // A Program's own arena side: Organization -> Venue -> Rink -> ice slot,
    // with the Venue granted to THIS Program's Season. That grant is the only
    // thing that puts the Venue (and, by cascade, the Rink/slot, and by
    // ownership the Organization) inside this Program's derived-join scope.
    const buildProgram = async (tag) => {
      const org = await post("/api/v2/setup/organization", { name: `${tag}-Org` });
      const program = await post("/api/v2/setup/program",
        { name: `${tag}-Program`, operator_organization_id: org.id });
      const season = await post("/api/v2/setup/season",
        { program_id: program.id, name: `${tag}-Season` });
      const club = await post("/api/v2/setup/club", { name: `${tag}-Club` });
      const venue = await post("/api/v2/setup/venue",
        { name: `${tag}-Venue`, organization_id: org.id });
      await post(`/api/v2/setup/seasons/${season.id}/venue-access`, { venue_id: venue.id });
      const rink = await post("/api/v2/setup/rink", { venue_id: venue.id, name: `${tag}-Rink` });
      await post("/api/v2/setup/ice-slot", {
        rink_id: rink.id, start_time: "2026-09-05T18:00:00+00:00",
        end_time: "2026-09-05T19:30:00+00:00", slot_type: "game",
      });
      // home_club_id is what puts this Official in its Program's scope --
      // and, because that Club DOES have Teams, what keeps it out of
      // `unassigned_officials` and therefore out of the other Program's view.
      await post("/api/v2/setup/official",
        { name: `${tag}-Official`, home_club_id: club.id });
      return { org, program, season, club, venue, rink };
    };
    // One competition inside a Program: League -> Division -> Team (+ its
    // season registration and one Player). The Player DOES carry the Program
    // tag, so the grid-wide foreign-token net below covers the Players card
    // too -- see the header comment for why that changed.
    const buildLeague = async (base, tag, suffix) => {
      const league = await post("/api/v2/setup/league",
        { season_id: base.season.id, name: `${tag}-League-${suffix}` });
      const division = await post("/api/v2/setup/division",
        { league_id: league.id, name: `${tag}-Division-${suffix}` });
      const team = await post("/api/v2/setup/team",
        { club_id: base.club.id, league_id: league.id, name: `${tag}-Team-${suffix}` });
      await post(`/api/v2/setup/seasons/${base.season.id}/team-registrations`,
        { team_id: team.id, league_id: league.id, division_id: division.id });
      await post("/api/v2/setup/player",
        { team_id: team.id, name: `${tag}-Player-${suffix}`, position: "forward" });
      return league.id;
    };

    const a = await buildProgram("PROGA");
    const b = await buildProgram("PROGB");
    const leagueA1 = await buildLeague(a, "PROGA", "A1");
    const leagueA2 = await buildLeague(a, "PROGA", "A2");
    await buildLeague(b, "PROGB", "B1");

    // The never-linked bootstrap pair (#369's `unassigned_*` contract): a
    // Club with no Team and an owner-less Venue with no Season grant. Neither
    // has a chain into ANY Program, so both must stay visible from BOTH.
    await post("/api/v2/setup/club", { name: "FREE-Club" });
    await post("/api/v2/setup/venue", { name: "FREE-Venue" });

    return {
      programA: a.program.id, seasonA: a.season.id,
      programB: b.program.id, seasonB: b.season.id,
      leagueA1, leagueA2,
    };
  });
}

// Every Setup Records card, keyed by its visible `.sc-title`, with the row
// titles (`.li-title`, an EXACT match surface) and the full card-body text (a
// SUBSTRING surface, so a leak hiding in a row's subtitle -- a club name, an
// owning organization, a parent league -- is caught too). `gridText` is the
// whole `.setup-grid`, for the catch-all foreign-token net.
async function readRecords(page) {
  return page.evaluate(() => {
    const cards = {};
    document.querySelectorAll(".setup-grid .setup-card").forEach((card) => {
      const title = (card.querySelector(".sc-title")?.textContent || "").trim();
      const body = card.querySelector(".setup-card-body");
      cards[title] = {
        titles: Array.from(card.querySelectorAll(".setup-card-body .li-title"))
          .map((el) => el.textContent.trim()),
        text: (body?.textContent || "").trim(),
      };
    });
    return {
      cards,
      gridText: (document.querySelector(".setup-grid")?.textContent || ""),
    };
  });
}

// Wait for the Records grid to reflect a context switch. The Teams card is
// the settle signal because it is the ONE card that changes on BOTH axes
// (Program and League), so every switch this journey makes has a genuinely
// new expected value here -- there is no state where waiting on it could
// return immediately against the pre-switch paint. On timeout it reports what
// actually rendered, so a real narrowing regression reads as a precise diff
// rather than an opaque hang; the same expectation is then re-asserted
// explicitly below, so a late paint landing wrong content still fails.
async function settleRecords(page, expectedTeams, step) {
  await page.waitForSelector(".setup-grid", { timeout: 10000 }).catch(() => fail(
    `[${step}] the Setup Records grid never rendered`));
  const want = [...expectedTeams].sort();
  await page.waitForFunction((expected) => {
    const card = Array.from(document.querySelectorAll(".setup-grid .setup-card"))
      .find((c) => (c.querySelector(".sc-title")?.textContent || "").trim() === "Teams");
    if (!card) return false;
    const got = Array.from(card.querySelectorAll(".setup-card-body .li-title"))
      .map((el) => el.textContent.trim()).sort();
    return got.length === expected.length && got.every((v, i) => v === expected[i]);
  }, want, { timeout: 10000 }).catch(async () => {
    const { cards } = await readRecords(page);
    fail(`[${step}] the Records "Teams" card never settled on ${JSON.stringify(want)} `
      + `-- rendered ${JSON.stringify((cards["Teams"] || {}).titles)}`);
  });
}

async function selectProgram(page, programId, seasonId, expected, step) {
  await page.selectOption("#ctx-select", `${programId}|${seasonId}`);
  await settleRecords(page, expected.teams, step);
}

async function selectLeague(page, leagueId, expected, step) {
  await page.selectOption("#ctx-league-select", leagueId || "");
  // The switch is fire-and-forget from the <select>'s own onchange (the #345
  // contextRevision idiom), so poll the control's settled value rather than
  // sleeping a guessed interval -- same discipline league-context-bar.js and
  // league-filtered-data.js use.
  await page.waitForFunction((v) => {
    const el = document.getElementById("ctx-league-select");
    return el && el.value === (v || "");
  }, leagueId, { timeout: 10000 }).catch(() => fail(
    `[${step}] the League select never settled on "${leagueId || "(No League)"}"`));
  await settleRecords(page, expected.teams, step);
}

// The full Setup Records assertion for one persisted context.
//
//   `self`/`other`  -- the active Program's records and the OTHER Program's.
//   `expected`      -- the League-narrowed subsets (divisions/teams). Seasons
//                      and Leagues are deliberately NOT in here: they are all
//                      of the active Program's on this structural surface,
//                      whatever League is selected, and asserting that
//                      explicitly is what makes a future over-narrowing
//                      regression fail here.
async function assertRecordsScope(page, self, other, expected, step) {
  const { cards, gridText } = await readRecords(page);
  const card = (title) => {
    const c = cards[title];
    if (!c) {
      fail(`[${step}] no "${title}" Setup Records card rendered -- the card/`
        + `selector assumption is stale, update this check (saw `
        + `${JSON.stringify(Object.keys(cards))})`);
    }
    return c;
  };
  // Positive control: without this, every negative check below would pass on
  // a view that rendered nothing at all.
  const present = (title, name) => {
    const c = card(title);
    if (!c.titles.includes(name)) {
      fail(`[${step}] the "${title}" card is missing "${name}" -- the active `
        + `Program's own records must still be there (positive control); `
        + `rendered ${JSON.stringify(c.titles)}`);
    }
  };
  const absent = (title, name) => {
    const c = card(title);
    if (c.text.includes(name)) {
      fail(`[${step}] the "${title}" card LEAKS "${name}" -- out-of-scope for `
        + `the persisted context; rendered ${JSON.stringify(c.text)}`);
    }
  };

  // 1. The active Program itself, and only it (`programs` collapses to
  //    [active_program] -- the context BAR, not this surface, is the
  //    cross-Program picker).
  present("Programs", self.program);
  absent("Programs", other.program);

  // 2. Seasons and Leagues: ALL of the active Program's, never narrowed by
  //    the selected Season or League (the deliberate structural-surface
  //    contract), and never another Program's.
  present("Seasons", self.season);
  absent("Seasons", other.season);
  self.leagues.forEach((name) => present("Leagues", name));
  other.leagues.forEach((name) => absent("Leagues", name));

  // 3. Divisions and Teams: the active Program's, narrowed further by the
  //    selected League when there is one.
  self.divisions.forEach((name) => (expected.divisions.includes(name)
    ? present("Divisions", name) : absent("Divisions", name)));
  other.divisions.forEach((name) => absent("Divisions", name));
  self.teams.forEach((name) => (expected.teams.includes(name)
    ? present("Teams", name) : absent("Teams", name)));
  other.teams.forEach((name) => absent("Teams", name));
  // Players narrow on BOTH axes. Unlike Clubs/Officials/Venues/Organizations
  // -- which have no League axis in the domain model and so stay Program-wide
  // -- a Player reaches a League through its Team's real permanent
  // `Team.league_id` (#283), so the same-Program cross-League case is a
  // genuine requirement here, not an invented one.
  self.players.forEach((name) => (expected.players.includes(name)
    ? present("Players", name) : absent("Players", name)));
  other.players.forEach((name) => absent("Players", name));

  // 4. The derived-join entities -- no direct Program FK, scoped by a real
  //    validated chain into the active Program. These are Program-level and
  //    are NOT League-narrowed: every caller of this helper passes through
  //    here under "No League", League A1 and League A2 alike, so an
  //    over-narrowing regression on any of them fails.
  present("Clubs", self.club);
  absent("Clubs", other.club);
  present("Facility owners", self.org);
  absent("Facility owners", other.org);
  present("Officials", self.official);
  absent("Officials", other.official);
  present("Venues", self.venue);
  absent("Venues", other.venue);
  present("Rinks", self.rink);
  absent("Rinks", other.rink);

  // 5. The `unassigned_*` bootstrap contract: a record linked to NO Program
  //    at all is nobody's data yet, so it stays visible from EVERY Program's
  //    Records (unioned in by app.js's withUnassigned). It must regress
  //    neither into "invisible everywhere" nor back into "leaks like before".
  present("Clubs", FREE_CLUB);
  present("Venues", FREE_VENUE);

  // 6. Catch-all: no token belonging to the other Program may appear ANYWHERE
  //    in the Records grid -- including in a card this journey does not name
  //    individually, and including inside a row subtitle.
  if (gridText.includes(`${other.tag}-`)) {
    const offenders = Object.keys(cards)
      .filter((t) => cards[t].text.includes(`${other.tag}-`));
    fail(`[${step}] "${other.tag}-" appears somewhere in the Setup Records `
      + `grid while the OTHER Program is active -- leaking card(s): `
      + `${JSON.stringify(offenders)}`);
  }
}

async function checkViewport(browser, viewport) {
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
    if ((await loginAs(page, "admin")).status !== 200) {
      throw new Error(`[${L}] admin login failed`);
    }
    const f = await buildFixture(page);
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });

    await page.click('.tab[data-tab="setup"]:not([data-setup-workflow-nav])');
    await page.waitForFunction(() => document.body.dataset.view === "setup",
      null, { timeout: 10000 }).catch(async () => fail(
        `[${L}] clicking the Setup tab never reached the setup view (was `
        + `"${await page.evaluate(() => document.body.dataset.view)}")`));
    await page.click('[data-setup-view="records"]');
    await page.waitForSelector(".setup-grid", { timeout: 10000 }).catch(() => fail(
      `[${L}] the Setup Records grid never rendered`));

    const bothA = { divisions: PROGRAM_A.divisions, teams: PROGRAM_A.teams,
                    players: PROGRAM_A.players };
    const onlyA1 = { divisions: ["PROGA-Division-A1"], teams: ["PROGA-Team-A1"],
                     players: ["PROGA-Player-A1"] };
    const onlyA2 = { divisions: ["PROGA-Division-A2"], teams: ["PROGA-Team-A2"],
                     players: ["PROGA-Player-A2"] };
    const allB = { divisions: PROGRAM_B.divisions, teams: PROGRAM_B.teams,
                   players: PROGRAM_B.players };

    // (1) Program A + No League: every one of Program B's records is absent
    //     from every card, and A's own equivalents are present.
    await selectProgram(page, f.programA, f.seasonA, bothA, `${L}/A-select`);
    await page.waitForFunction(() => {
      const s = document.getElementById("ctx-league-select");
      return s && !s.hidden;
    }, null, { timeout: 10000 }).catch(() => fail(
      `[${L}] the League select never appeared for Program A`));
    await selectLeague(page, "", bothA, `${L}/A-none`);
    await assertRecordsScope(page, PROGRAM_A, PROGRAM_B, bothA, `${L}/A+none`);

    // (3) Program A + League A1: A2's Division/Team drop out; A1's stay. The
    //     Program-level records (Seasons, Leagues, Clubs, Facility owners,
    //     Officials, Venues, Rinks) are re-asserted PRESENT inside
    //     assertRecordsScope -- they are deliberately NOT League-narrowed.
    await selectLeague(page, f.leagueA1, onlyA1, `${L}/A-league-A1`);
    await assertRecordsScope(page, PROGRAM_A, PROGRAM_B, onlyA1, `${L}/A+A1`);

    //     ...and A2 flips it, rather than merely adding to A1's view.
    await selectLeague(page, f.leagueA2, onlyA2, `${L}/A-league-A2`);
    await assertRecordsScope(page, PROGRAM_A, PROGRAM_B, onlyA2, `${L}/A+A2`);

    //     Back to "No League": the union of A's Leagues returns, still with
    //     no trace of Program B.
    await selectLeague(page, "", bothA, `${L}/A-none-again`);
    await assertRecordsScope(page, PROGRAM_A, PROGRAM_B, bothA, `${L}/A+none-again`);

    // (2) Switching the context bar to Program B flips it ENTIRELY: B's
    //     records present, A's absent -- the mirror image, asserted with the
    //     exact same helper so neither direction can pass by asymmetry.
    await selectProgram(page, f.programB, f.seasonB, allB, `${L}/B-select`);
    await assertRecordsScope(page, PROGRAM_B, PROGRAM_A, allB, `${L}/B+none`);

    //     And back to A, so the switch is proven reversible rather than a
    //     one-way narrowing that happens to look right once.
    await selectProgram(page, f.programA, f.seasonA, bothA, `${L}/A-return`);
    await assertRecordsScope(page, PROGRAM_A, PROGRAM_B, bothA, `${L}/A+none-return`);

    if (errors.length) {
      fail(`[${L}] unexpected console/page errors: ${JSON.stringify(errors)}`);
    }
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth
      - document.documentElement.clientWidth);
    if (overflow > 1) {
      fail(`[${L}] horizontal overflow: scrollWidth exceeds clientWidth by ${overflow}px`);
    }
    console.log(`[${L}] OK — Setup Records scopes to the persisted active Program `
      + `(A excludes B and B excludes A across Programs/Seasons/Leagues/Divisions/`
      + `Teams/Clubs/Facility owners/Officials/Venues/Rinks), League A1/A2 narrow `
      + `Divisions+Teams only, and the never-linked FREE-Club/FREE-Venue stay `
      + `visible from both.`);
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
    for (const viewport of VIEWPORTS) await checkViewport(browser, viewport);
    console.log("Setup v2 context-scope browser journey passed.");
  } catch (error) {
    console.error("Setup v2 context-scope browser journey FAILED.");
    console.error(error && error.message ? error.message : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
