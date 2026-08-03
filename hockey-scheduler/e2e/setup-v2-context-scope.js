// Setup v2 (`get_setup_overview_v2`) context-scope regression -- the #369
// review's Required Regression, at the browser layer.
//
// #369's review correction rearchitected get_setup_overview_v2
// (backend/hockey_scheduler/api/service.py) so that EVERY collection it
// returns ceilings on the persisted ACTIVE tuple (ContextService.
// resolve_with_league), not on the caller's full authorized Program set.
// The #367 owner ruling then corrected two of those rules again, and this
// file carries both reversals:
//   * `programs` collapses to just `[active_program]`;
//   * `seasons` is the ACTIVE Season alone -- REVERSED from #369's "all of
//     the active Program's Seasons, Season is not a further ceiling here".
//     A sibling Season of the same Program is now as absent as another
//     Program's, on the hub, the six workflow landings and Records alike;
//   * `leagues` stays all of the active Program's: a League is PERMANENT
//     Program structure, narrowed by neither the Season nor the League
//     selection;
//   * `divisions`/`teams` narrow further when a League is selected, and so
//     do Clubs (through their Teams) and Officials (through an in-scope home
//     Club or assignment);
//   * venues / rinks / ice slots and their owning facility organization
//     follow the SEASON axis (SeasonVenueAccess) and have NO
//     competition-League axis at all, so they stay active-Season-wide under
//     ANY League selection -- deliberately, and asserted as such below;
//   * `pending_link_*` carries the unlinked records the CALLING account
//     created, which app.js's `withPendingLink(sv, key)` unions back into
//     the Setup Records cards and create-drawer pickers so create-then-link
//     flows still work -- REVERSING #369's `unassigned_*` lists, which
//     published every never-linked record in the installation to every
//     scoped operator.
//
// This file is the reviewer's verbatim Required Regression: "seed two
// authorized Programs and two Leagues with distinguishable teams, players,
// venues/rinks/slots, clubs/orgs, and officials; prove Program A + No League
// excludes Program B everywhere, and Program A + League A excludes League B,
// at desktop and 390px" -- plus #367 B1's "prove Setup hub, six landings and
// Records show only S1 under League and No League, then flip exactly when S2
// is selected". Every record carries an unmistakable PROGA-/PROGB-
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
//     `pending_link_ice_slots` have no Records surface to assert against
//     here; slots are still seeded per Program so the fixture matches the
//     reviewer's wording, and their scoping is asserted at the facade level.
//   * "and to nobody else" for the pending-link records -- a per-identity
//     claim needing two real sessions, proven over authenticated HTTP in
//     test_league_filtered_overview_v2.py.
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

// The two never-linked create-then-link records: a Club with no Team
// anywhere and a Venue with no SeasonVenueAccess grant and no owning
// Organization.
//
// #367 owner ruling: these are NOT "nobody's data" to be published to every
// scoped operator (the reversed `unassigned_*` mechanism). They reach the
// payload through `pending_link_*`, which carries only the unlinked records
// the CALLING account created -- and this journey's single admin session is
// exactly that account, which is why they must still be visible here, from
// either Program, right up until their first real link. The
// "and to nobody else" half is a per-identity claim with no pixel to check,
// so it lives in test_league_filtered_overview_v2.py (facade + authenticated
// HTTP, two different real sessions).
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

// ============ RENDER'S OWN FETCH WINDOW, UNDER TEST CONTROL ================
// /api/demo/overview is render()'s FIRST await. Everything between render()'s
// synchronous blank of #content and its final paint sits behind it, and on the
// Records surface that blank is a three-`<div class="skeleton">` rewrite with
// no `.setup-grid` in it at all -- the window in which this journey read
// `cards=[]`. On a loopback that window is single-digit milliseconds, which is
// why the shipped settlement lost the race only 1 time in 40 and why a
// falsifying mutation against it would otherwise be a coin flip rather than a
// proof.
//
// So it is intercepted once per page and made WIDE for the duration of a
// context switch. Not one response byte changes: the delay only guarantees
// that a settlement which returns before the scoped grid is repainted is
// caught inside the window EVERY time instead of occasionally. A build that
// waits correctly is unaffected -- it simply waits the extra beat. The knob
// deliberately lives in the switch helpers rather than inside the wait, so
// that reverting the WAIT to its shipped card-content-only form leaves the
// window exactly as wide and the resulting failure is deterministic.
const RENDER_OVERVIEW_RE = /\/api\/demo\/overview(\?|$)/;
const SWITCH_RENDER_DELAY_MS = 400;
let renderWindowDelayMs = 0;
async function installRenderWindowControl(page) {
  await page.route(RENDER_OVERVIEW_RE, async (route) => {
    const delay = renderWindowDelayMs;
    if (delay > 0) await new Promise((r) => setTimeout(r, delay));
    try { await route.continue(); } catch (e) { /* page closed mid-delay */ }
  });
}

async function newPage(browser, viewport) {
  const context = await browser.newContext({
    viewport: { width: viewport.width, height: viewport.height },
  });
  const page = await context.newPage();
  await installRenderWindowControl(page);
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
// Program A carries a SECOND Season (`season2`) with its own League,
// Division, Venue and Rink, so the #367 owner ruling's Season ceiling has
// something distinguishable to hide and to reveal on this surface. Every
// Season-bound name is listed here; which of them are EXPECTED at any moment
// is the `expected` argument, so anything not expected is asserted absent.
const PROGRAM_A = {
  tag: "PROGA", program: "PROGA-Program", season: "PROGA-Season",
  season2: "PROGA-Autumn",
  seasons: ["PROGA-Season", "PROGA-Autumn"],
  leagues: ["PROGA-League-A1", "PROGA-League-A2", "PROGA-League-A3"],
  divisions: ["PROGA-Division-A1", "PROGA-Division-A2", "PROGA-Division-A3"],
  teams: ["PROGA-Team-A1", "PROGA-Team-A2"],
  players: ["PROGA-Player-A1", "PROGA-Player-A2"],
  club: "PROGA-Club", org: "PROGA-Org", official: "PROGA-Official",
  venue: "PROGA-Venue", rink: "PROGA-Rink",
  venues: ["PROGA-Venue", "PROGA-Icehouse"],
  rinks: ["PROGA-Rink", "PROGA-Padtwo"],
};
const PROGRAM_B = {
  tag: "PROGB", program: "PROGB-Program", season: "PROGB-Season",
  seasons: ["PROGB-Season"],
  leagues: ["PROGB-League-B1"],
  divisions: ["PROGB-Division-B1"],
  teams: ["PROGB-Team-B1"],
  players: ["PROGB-Player-B1"],
  club: "PROGB-Club", org: "PROGB-Org", official: "PROGB-Official",
  venue: "PROGB-Venue", rink: "PROGB-Rink",
  venues: ["PROGB-Venue"],
  rinks: ["PROGB-Rink"],
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
      // The grant names an EXISTING Season, so its Season end is ceilinged on
      // the ACTIVE Season (#369 target authorization) -- with more than one
      // Season in the install, a grant made while another one is selected is
      // refused. Select this Program's own Season first, exactly as the
      // context bar does; the journey re-selects through the real UI later.
      await post("/api/context",
        { program_id: program.id, season_id: season.id });
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
    // The v2 Team create binds `league_id` to the ACTIVE Program (#367
    // prerequisite, PR #371), so building structure in a Program means
    // working IN it. The journey switches context per Program rather than
    // being exempted from the guard it is partly here to exercise.
    const useProgram = async (base) => {
      await post("/api/context",
        { program_id: base.program.id, season_id: base.season.id });
    };

    const buildLeague = async (base, tag, suffix) => {
      await useProgram(base);
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

    // Program A's SECOND Season, with its own League, Division and its own
    // granted Venue -> Rink (#367 owner ruling: the active Season is a hard
    // ceiling on this surface too, and the facility tree follows the SEASON
    // axis -- SeasonVenueAccess -- while ignoring the League axis entirely).
    // Every Season needs a League of its own or get_onboarding_status_v2's
    // INSTALLATION-WIDE readiness check redirects the session into the
    // Initial Setup wizard.
    await useProgram(a);
    const seasonA2 = await post("/api/v2/setup/season",
      { program_id: a.program.id, name: "PROGA-Autumn" });
    const leagueA3 = await post("/api/v2/setup/league",
      { season_id: seasonA2.id, name: "PROGA-League-A3" });
    await post("/api/v2/setup/division",
      { league_id: leagueA3.id, name: "PROGA-Division-A3" });
    const venueA2 = await post("/api/v2/setup/venue",
      { name: "PROGA-Icehouse", organization_id: a.org.id });
    // Same rule as in buildProgram: the grant's Season end is the ACTIVE
    // Season, so select PROGA-Autumn for it, then put the context back on
    // Program A's first Season where the rest of the fixture left it.
    await post("/api/context",
      { program_id: a.program.id, season_id: seasonA2.id });
    await post(`/api/v2/setup/seasons/${seasonA2.id}/venue-access`,
      { venue_id: venueA2.id });
    await post("/api/v2/setup/rink",
      { venue_id: venueA2.id, name: "PROGA-Padtwo" });
    await useProgram(a);

    // The never-linked bootstrap pair (#369's `unassigned_*` contract): a
    // Club with no Team and an owner-less Venue with no Season grant. Neither
    // has a chain into ANY Program, so both must stay visible from BOTH.
    await post("/api/v2/setup/club", { name: "FREE-Club" });
    await post("/api/v2/setup/venue", { name: "FREE-Venue" });

    return {
      programA: a.program.id, seasonA: a.season.id,
      seasonA2: seasonA2.id,
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

// ============ WHAT "THE SWITCH HAS SETTLED" MEANS ON THIS SURFACE =========
// This helper used to BE the settlement condition, and that was the defect
// behind the #385 shard-2 failure: `[phone/A+none] no "Programs" Setup
// Records card rendered ... (saw [])`. It is now only a CONTENT ASSERTION,
// run after the settlement below has already been reached.
//
// The failure reproduces on merged `main` exactly as often as on the #385
// head -- 1/40 serial idle runs of the failing step pair at 390px on each,
// 3/30 and 2/30 under full-CPU load -- so it was never the scheduler
// correction. An instrumented trace recording every write to #content's own
// children, stamped with the contextRevision that owned it, says which layer
// is wrong, and it is NOT the application:
//
//   render:ENTER             rev=49 intent=false grid=12  pass=24 revAtEnter=49
//   render:after-sync-blank  rev=49 grid=-1 kids=3   supersededAtBlank=false
//   #content childList       rev=49 added=3 removed=6      <- LOADING SKELETON
//   SETTLE-RETURNED (old)    rev=49 grid=-1                <- journey proceeds
//   TEST: readRecords        rev=49 grid=-1                <- cards=[]
//   #content childList       rev=49 added=6 removed=3 grid=12
//   render:RESOLVED          rev=49 supersededAtExit=false
//
// The empty grid is the CURRENT render's own skeleton blank (app.js's
// `c.innerHTML = <div class="skeleton">x3`), owned by the newest
// contextRevision, superseded neither on entry nor on exit, and repainted by
// that very same pass into the correct 12-card scoped grid. Across 140
// instrumented iterations the surface converged on the correct, correctly
// scoped, non-empty grid EVERY time, including every reproduction. No
// cancelled or superseded render ever cleared a newer scoped grid: the
// product invariant holds and app.js needs no change.
//
// What was wrong is that this journey settled on CARD CONTENT, and at the
// failing step that content DOES NOT CHANGE. Step (1) selects Program A (the
// Teams card becomes the two PROGA teams) and then selects "No League" on a
// bar that is ALREADY on "No League" (the Teams card stays the two PROGA
// teams). The comment this replaces named that exact trap -- the signal card
// "has to be one whose expected value genuinely CHANGES across the switch
// being made, or the wait returns immediately against the pre-switch paint
// and proves nothing" -- and the instrumentation caught it firing: the
// predicate was ALREADY TRUE before the change event was dispatched in 140 of
// 140 iterations. The journey returned on the pre-switch paint and then read
// the DOM inside the repaint window of the switch it had just asked for.
//
// A signal card cannot be repaired by picking a different card, because a
// League switch from "No League" to "No League" legitimately changes NO card
// at all. The settlement has to come from the switch LIFECYCLE instead -- see
// waitForScopedRecordsPaint() below -- and once it does, this check must stop
// being a WAIT.
//
// That is the second half of the same lesson, and it was worth a mutation to
// learn: with the lifecycle wait in place but this helper still polling until
// the content became right, deleting the settlement's own "and the paint left
// a .setup-grid standing" clause did NOT fail the journey (3 runs, 3 passes).
// The settlement was allowed to return on render()'s loading skeleton and this
// poll simply sat there until the real grid arrived and rescued it. A wait
// that recovers from an early settlement also HIDES one -- it made the
// settlement's correctness depend on its callers instead of on itself, which
// is the same coupling that produced the original defect.
//
// So the per-card expectations are now ASSERTED against the settled surface,
// in a single read, with no polling. render() writes the whole Records grid in
// one `c.innerHTML = renderSetup(...)`, so at the settled moment every card is
// already final and there is nothing legitimate left to wait for; anything
// still missing is a real narrowing regression and says so immediately.
async function assertCardTitles(page, cardTitle, expectedTitles, step) {
  const want = [...expectedTitles].sort();
  const got = await page.evaluate((title) => {
    const grid = document.querySelector(".setup-grid");
    if (!grid) return { grid: false };
    const card = Array.from(grid.querySelectorAll(".setup-card"))
      .find((c) => (c.querySelector(".sc-title")?.textContent || "").trim() === title);
    if (!card) {
      return { grid: true, card: false, cards: Array.from(grid.querySelectorAll(".sc-title"))
        .map((el) => el.textContent.trim()) };
    }
    return { grid: true, card: true,
      titles: Array.from(card.querySelectorAll(".setup-card-body .li-title"))
        .map((el) => el.textContent.trim()) };
  }, cardTitle);
  if (!got.grid) {
    fail(`[${step}] the switch settled on a surface with NO Setup Records grid `
      + `on it -- the settlement condition returned before the scoped grid was `
      + `painted`);
  }
  if (!got.card) {
    fail(`[${step}] no "${cardTitle}" card on the settled Setup Records grid -- `
      + `saw ${JSON.stringify(got.cards)}`);
  }
  const sorted = [...got.titles].sort();
  if (sorted.length !== want.length || !sorted.every((v, i) => v === want[i])) {
    fail(`[${step}] the Records "${cardTitle}" card settled on `
      + `${JSON.stringify(sorted)} -- expected ${JSON.stringify(want)}`);
  }
}

const assertTeamsCard = (page, expectedTeams, step) =>
  assertCardTitles(page, "Teams", expectedTeams, step);

// What the SERVER persisted, never what the <select> is displaying. The
// shipped League helper polled `#ctx-league-select.value`, which Playwright
// has already assigned before the change event it triggers even fires -- so
// that wait settled on the journey's own keystroke and observed nothing about
// the switch.
const confirmedTuple = (page) => page.evaluate(() => {
  const s = (contextOptions && contextOptions.selected) || {};
  return { programId: s.program_id || null, seasonId: s.season_id || null,
           leagueId: s.league_id || null };
});

// THE SETTLEMENT ITSELF -- the discipline setup-card-write-identity.js
// established for this family of bug, plus the one conjunct the Records
// surface needs and the Setup hub does not. Three things must ALL be true,
// and the paint must be OBSERVED rather than timed:
//
//   (1) THE CONFIRMED TUPLE -- program, season AND league, from
//       contextOptions.selected. All three: a League switch moves only the
//       third axis, so a wait that reads the first two is not waiting for it
//       at all.
//   (2) contextSwitchIntentPending === false -- the action-control withdrawal
//       released, i.e. a reconciliation genuinely happened rather than a POST
//       echo having landed.
//   (3) A #content DIRECT-CHILDREN paint (`subtree: false`) delivered while
//       (1) and (2) already hold, AND LEAVING A `.setup-grid` STANDING.
//
// The `.setup-grid` clause in (3) is what the hub journey does not need and
// this one cannot do without. render()'s retain-the-cards pass is conditioned
// on `setupView === "hub"`, so on Records render() takes the other branch and
// blanks #content to skeletons -- and it does that AFTER sendContextSwitch()
// has already confirmed the tuple and released the withdrawal. That blank is
// itself a direct-children rewrite satisfying (1), (2) and every observer
// shape the hub journey uses; it is precisely the paint this journey used to
// read `cards=[]` from. Only a paint that left a `.setup-grid` behind is the
// settled scoped surface.
//
// The observer is armed BEFORE the change event so a fast paint cannot land
// between two polls of the wait.
//
// FALSIFIABILITY -- every clause here was proven to fail on its own, with the
// window widener in place so each result is deterministic rather than a race
// re-run until it obliges:
//   * settlement replaced by the shipped card-content-only form (window
//     unchanged): 3/3 FAIL, `[desktop/A+none] no "Programs" Setup Records card
//     rendered ... (saw [])` -- the reported CI line, verbatim. With the
//     widener ALSO neutralized it reverts to the intermittent original: 5 runs,
//     2 passes and 3 failures, at `[phone/A+none]` -- the reported viewport and
//     step, which is what ties this journey's own defect to the shard failure.
//   * (3)'s `.setup-grid` clause deleted from observer and poll: 3/3 FAIL,
//     "the switch settled on a surface with NO Setup Records grid on it".
//   * (1)'s League axis inverted in the poll: 2/2 FAIL on the settle timeout
//     with its diagnostic.
//   * (2) inverted in the poll: 2/2 FAIL likewise.
// Note what the last two do and do not show. They prove those conjuncts are
// live and gating. They are inversions rather than deletions because DELETING
// (1) or (2) does not break this journey: clause (3) already forces the wait
// past the skeleton to the final paint, and nothing on the Records surface
// rewrites #content's own children between the POST echo and that paint. They
// are kept because they are what makes this wait fail LOUDLY, with the
// confirmed tuple in the message, when a switch is rejected or coalesced
// rather than merely slow -- and because a future step that switches twice in
// a row would need them.
async function armScopedPaintObserver(page, programId, seasonId, leagueId) {
  await page.evaluate(([p, s, lg]) => {
    if (window.__scopeObs) window.__scopeObs.disconnect();
    window.__scopePainted = false;
    const c = document.getElementById("content");
    window.__scopeObs = new MutationObserver(() => {
      if (contextSwitchIntentPending) return;
      const cur = (contextOptions && contextOptions.selected) || {};
      if (cur.program_id !== p) return;
      if ((cur.season_id || null) !== s) return;
      if ((cur.league_id || null) !== lg) return;
      if (!c.querySelector(".setup-grid")) return;
      window.__scopePainted = true;
    });
    if (c) window.__scopeObs.observe(c, { childList: true, subtree: false });
  }, [programId, seasonId, leagueId]);
}

async function waitForScopedRecordsPaint(page, programId, seasonId, leagueId, step) {
  await page.waitForFunction(([p, s, lg]) => {
    if (contextSwitchIntentPending) return false;
    const cur = (contextOptions && contextOptions.selected) || {};
    if (cur.program_id !== p) return false;
    if ((cur.season_id || null) !== s) return false;
    if ((cur.league_id || null) !== lg) return false;
    // Re-checked at poll time, not only at delivery: the grid this journey is
    // about to read has to be standing NOW, not merely to have existed once.
    if (!document.querySelector(".setup-grid")) return false;
    return window.__scopePainted === true;
  }, [programId, seasonId, leagueId], { timeout: 20000 }).catch(async () => {
    const why = await page.evaluate(() => ({
      intentPending: !!contextSwitchIntentPending,
      selected: (contextOptions && contextOptions.selected) || null,
      paintedAfterRelease: !!window.__scopePainted,
      gridCards: document.querySelectorAll(".setup-grid .setup-card").length,
      contentChildren: Array.from(document.getElementById("content").children)
        .map((el) => el.className || el.tagName),
    })).catch(() => null);
    fail(`[${step}] the context switch to ${programId}/${seasonId}/`
      + `${leagueId || "(No League)"} never SETTLED -- the confirmed tuple, the `
      + `release of the action-control withdrawal, and a #content repaint after `
      + `both that left a .setup-grid standing are all required before this `
      + `surface may be read: ${JSON.stringify(why)}`);
  });
  await page.evaluate(() => {
    if (window.__scopeObs) window.__scopeObs.disconnect();
    window.__scopeObs = null;
  });
}

async function selectProgram(page, programId, seasonId, seasonName, expected, step) {
  const cur = await confirmedTuple(page);
  // #ctx-select's own onchange CARRIES the active League forward across a
  // same-Program Season change and DROPS it on a Program change (app.js:
  // `const carryLeague = p === sel.program_id ? (sel.league_id || null) : null`).
  // Mirrored here so the tuple this waits for is the one the real control
  // actually asks the server for, rather than an assumption about which
  // Leagues this journey happens to have selected by now.
  const leagueId = programId === cur.programId ? cur.leagueId : null;
  renderWindowDelayMs = SWITCH_RENDER_DELAY_MS;
  try {
    await armScopedPaintObserver(page, programId, seasonId, leagueId);
    await page.selectOption("#ctx-select", `${programId}|${seasonId}`);
    await waitForScopedRecordsPaint(page, programId, seasonId, leagueId, step);
  } finally {
    renderWindowDelayMs = 0;
  }
  await assertCardTitles(page, "Seasons", [seasonName], step);
  await assertTeamsCard(page, expected.teams, step);
}

async function selectLeague(page, leagueId, expected, step) {
  // #ctx-league-select's own onchange CARRIES the active Program and Season
  // forward and moves only the League axis.
  const cur = await confirmedTuple(page);
  renderWindowDelayMs = SWITCH_RENDER_DELAY_MS;
  try {
    await armScopedPaintObserver(page, cur.programId, cur.seasonId, leagueId || null);
    await page.selectOption("#ctx-league-select", leagueId || "");
    await waitForScopedRecordsPaint(page, cur.programId, cur.seasonId,
                                    leagueId || null, step);
  } finally {
    renderWindowDelayMs = 0;
  }
  await assertTeamsCard(page, expected.teams, step);
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

  // 2. Seasons: exactly the ACTIVE one (#367 owner ruling -- REVERSED from
  //    "all of the active Program's Seasons"), so a sibling Season of the
  //    SAME Program is absent here just like another Program's is.
  //    Leagues: ALL of the active Program's, never narrowed by the Season or
  //    the League selection -- a League is permanent Program structure, and
  //    asserting that explicitly is what makes a future over-narrowing
  //    regression fail here.
  self.seasons.forEach((name) => (expected.seasons.includes(name)
    ? present("Seasons", name) : absent("Seasons", name)));
  other.seasons.forEach((name) => absent("Seasons", name));
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
  //    validated chain into the active tuple.
  //    Clubs and Officials ARE League-bound (a Club through its Teams, an
  //    Official through its home Club); this fixture gives each Program ONE
  //    Club shared by both of its Leagues' Teams, so both stay in scope
  //    under either League -- the same-Program cross-LEAGUE case for those
  //    two is proven at the facade level, where a per-League Club fixture
  //    can be built without duplicating this whole journey.
  //    Venues/Rinks/Facility owners follow the SEASON axis and have no
  //    competition-League axis at all: every caller of this helper passes
  //    through here under "No League", League A1 and League A2 alike, so an
  //    over-narrowing regression on them fails here.
  present("Clubs", self.club);
  absent("Clubs", other.club);
  present("Facility owners", self.org);
  absent("Facility owners", other.org);
  present("Officials", self.official);
  absent("Officials", other.official);
  self.venues.forEach((name) => (expected.venues.includes(name)
    ? present("Venues", name) : absent("Venues", name)));
  other.venues.forEach((name) => absent("Venues", name));
  self.rinks.forEach((name) => (expected.rinks.includes(name)
    ? present("Rinks", name) : absent("Rinks", name)));
  other.rinks.forEach((name) => absent("Rinks", name));

  // 5. The creator-owned `pending_link_*` contract (this comment used to
  //    describe the REVERSED `unassigned_*` one, down to a `withUnassigned`
  //    helper that no longer exists). A record linked to NO Program at all is
  //    not "nobody's data": it stays visible to the account that CREATED it,
  //    from every Program that account works in, and to nobody else. This
  //    journey drives a single admin session, which is that account -- so
  //    these two must be present here, and must regress neither into
  //    "invisible everywhere" nor back into "visible to everyone". The
  //    "and to nobody else" half needs two real sessions and lives in
  //    test_pending_link_ownership.py / test_zero_program_bootstrap_scoping.py.
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

// The Setup HUB and the workflow LANDINGS read the same payload as Records,
// so the #367 owner ruling's Season ceiling has to be visible there too --
// B1 names "the hub, all six landings, and Records" explicitly. The
// "participation" workflow's Divisions summary is the value that flips with
// the Season here (2 Divisions in Season A1, 1 in Season A2), and hub cards
// and landings share ONE renderer (setupSummaryHtml), so checking the hub
// card and then the landing covers both surfaces of the same number.
// Returns to the Records view afterwards, which is what the rest of this
// journey asserts against.
async function assertSetupSummaries(page, expectedDivisions, step) {
  const readDivisions = async (selector) => page.evaluate((sel) => {
    const root = document.querySelector(sel);
    if (!root) return null;
    const stat = Array.from(root.querySelectorAll(".swf-stat"))
      .find((el) => /Divisions/.test(el.textContent || ""));
    const strong = stat && stat.querySelector("strong");
    return strong ? parseInt(strong.textContent.trim(), 10) : null;
  }, selector);

  await page.click('[data-setup-view="hub"]');
  await page.waitForSelector(".swf-grid", { timeout: 10000 }).catch(() => fail(
    `[${step}] the Setup hub never rendered`));
  await page.waitForFunction((want) => {
    const root = document.querySelector(
      '[data-setup-workflow-card="participation"] .swf-stats');
    if (!root) return false;
    const stat = Array.from(root.querySelectorAll(".swf-stat"))
      .find((el) => /Divisions/.test(el.textContent || ""));
    const strong = stat && stat.querySelector("strong");
    return !!strong && parseInt(strong.textContent.trim(), 10) === want;
  }, expectedDivisions, { timeout: 10000 }).catch(async () => fail(
    `[${step}] the Setup HUB's "Season participation" card never settled on `
    + `${expectedDivisions} Divisions -- got `
    + `${await readDivisions('[data-setup-workflow-card="participation"] .swf-stats')}`));

  await page.click('[data-setup-workflow="participation"]');
  await page.waitForSelector(".swf-landing", { timeout: 10000 }).catch(() => fail(
    `[${step}] the Season participation LANDING never rendered`));
  const landing = await readDivisions(".swf-landing .swf-stats");
  if (landing !== expectedDivisions) {
    fail(`[${step}] the Season participation LANDING shows ${landing} Divisions, `
      + `expected ${expectedDivisions} -- the landings share the Records `
      + `surface's Season ceiling`);
  }
  // The scope note is the UI copy the ruling requires: it has to name the
  // active Season and state the deliberately Season-wide, League-blind
  // facility behavior, or an operator reads an empty card as data loss.
  const note = await page.evaluate(() => {
    const el = document.querySelector(".swf-landing [data-setup-scope-note]");
    return el ? el.textContent.replace(/\s+/g, " ").trim() : null;
  });
  if (!note || !/season/i.test(note) || !/Venues, rinks and ice/i.test(note)) {
    fail(`[${step}] the Setup scope note is missing or no longer explains the `
      + `Season ceiling and the season-wide facility rule -- got `
      + `${JSON.stringify(note)}`);
  }

  await page.click('[data-setup-view="records"]');
  await page.waitForSelector(".setup-grid", { timeout: 10000 }).catch(() => fail(
    `[${step}] the Records grid never came back`));
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

    // Season A1 is active in every `A*` expectation below: its two Divisions
    // are visible, Season A2's third one is not, and the facility tree shows
    // Season A1's granted Venue/Rink only.
    const A1_SEASON = { seasons: [PROGRAM_A.season],
                        venues: ["PROGA-Venue"], rinks: ["PROGA-Rink"] };
    const bothA = { ...A1_SEASON, teams: PROGRAM_A.teams,
                    players: PROGRAM_A.players,
                    divisions: ["PROGA-Division-A1", "PROGA-Division-A2"] };
    const onlyA1 = { ...A1_SEASON, divisions: ["PROGA-Division-A1"],
                     teams: ["PROGA-Team-A1"],
                     players: ["PROGA-Player-A1"] };
    const onlyA2 = { ...A1_SEASON, divisions: ["PROGA-Division-A2"],
                     teams: ["PROGA-Team-A2"],
                     players: ["PROGA-Player-A2"] };
    // Season A2 active: the Season-BOUND records flip wholesale, while the
    // permanent Program structure (Leagues) and the permanent Teams/Players
    // stay exactly as they were -- that contrast is what makes this a Season
    // ceiling rather than a blanket narrowing.
    const seasonTwoA = { seasons: [PROGRAM_A.season2],
                         venues: ["PROGA-Icehouse"], rinks: ["PROGA-Padtwo"],
                         divisions: ["PROGA-Division-A3"],
                         teams: PROGRAM_A.teams, players: PROGRAM_A.players };
    const allB = { divisions: PROGRAM_B.divisions, teams: PROGRAM_B.teams,
                   players: PROGRAM_B.players, seasons: PROGRAM_B.seasons,
                   venues: PROGRAM_B.venues, rinks: PROGRAM_B.rinks };

    // (1) Program A + No League: every one of Program B's records is absent
    //     from every card, and A's own equivalents are present.
    await selectProgram(page, f.programA, f.seasonA, PROGRAM_A.season, bothA,
                        `${L}/A-select`);
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

    // (4) The SEASON ceiling (#367 owner ruling), the reversal of this
    //     surface's previous "every Season of the active Program" contract.
    //     Switching Program A to its SECOND Season flips every Season-bound
    //     card -- Seasons, Divisions, Venues, Rinks -- while the permanent
    //     Program structure (Leagues, Teams, Clubs, Players) stays put.
    await selectProgram(page, f.programA, f.seasonA2, PROGRAM_A.season2,
                        seasonTwoA, `${L}/A-season2`);
    await assertRecordsScope(page, PROGRAM_A, PROGRAM_B, seasonTwoA,
                             `${L}/A+season2`);
    //     ...and the hub + workflow landings share the same ceiling, so the
    //     Divisions summary flips with it (2 in Season A1, 1 in Season A2).
    await assertSetupSummaries(page, 1, `${L}/season2`);

    //     Back to Season A1: it flips back, rather than accumulating.
    await selectProgram(page, f.programA, f.seasonA, PROGRAM_A.season, bothA,
                        `${L}/A-season1-again`);
    await assertRecordsScope(page, PROGRAM_A, PROGRAM_B, bothA,
                             `${L}/A+season1-again`);
    await assertSetupSummaries(page, 2, `${L}/season1`);
    //     ...and the Season ceiling holds under a selected League too, not
    //     only under "No League".
    await selectLeague(page, f.leagueA1, onlyA1, `${L}/A-season1-league-A1`);
    await assertRecordsScope(page, PROGRAM_A, PROGRAM_B, onlyA1,
                             `${L}/A+season1+A1`);
    await selectLeague(page, "", bothA, `${L}/A-season1-none`);

    // (2) Switching the context bar to Program B flips it ENTIRELY: B's
    //     records present, A's absent -- the mirror image, asserted with the
    //     exact same helper so neither direction can pass by asymmetry.
    await selectProgram(page, f.programB, f.seasonB, PROGRAM_B.season, allB,
                        `${L}/B-select`);
    await assertRecordsScope(page, PROGRAM_B, PROGRAM_A, allB, `${L}/B+none`);

    //     And back to A, so the switch is proven reversible rather than a
    //     one-way narrowing that happens to look right once.
    await selectProgram(page, f.programA, f.seasonA, PROGRAM_A.season, bothA,
                        `${L}/A-return`);
    await assertRecordsScope(page, PROGRAM_A, PROGRAM_B, bothA, `${L}/A+none-return`);

    if (errors.length) {
      fail(`[${L}] unexpected console/page errors: ${JSON.stringify(errors)}`);
    }
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth
      - document.documentElement.clientWidth);
    if (overflow > 1) {
      fail(`[${L}] horizontal overflow: scrollWidth exceeds clientWidth by ${overflow}px`);
    }
    console.log(`[${L}] OK — Setup Records scopes to the persisted active tuple `
      + `(A excludes B and B excludes A across Programs/Seasons/Leagues/Divisions/`
      + `Teams/Clubs/Facility owners/Officials/Venues/Rinks), the active SEASON `
      + `ceilings Seasons/Divisions/Venues/Rinks on Records AND on the hub + `
      + `landings (flipping both ways, under a League and under No League), `
      + `League A1/A2 narrow Divisions+Teams, and this session's own `
      + `never-linked FREE-Club/FREE-Venue stay visible from both Programs.`);
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
