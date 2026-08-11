// Season participation browser journey (#180 PR D, cut to v2 canonical #233
// Slice B2b; re-modelled for #283 Slice E permanent Leagues).
//
// At desktop and phone widths, a League Admin builds a permanent program team
// under its PERMANENT League (#283 rule 2: a Team is created with a required
// league_id), registers it for two different seasons — a permanent League
// spans Seasons, so BOTH registrations are under that same League (#283 rule
// 7: a Team may only register into its own permanent League) — through the
// Setup "Season participation" panel, confirms exactly one permanent Team
// backs two registrations, then removes it from one season and confirms the
// other season's registration is untouched. It then drives the Save controls'
// Division cascade within the team's permanent League, proves the backend
// refuses to move a registration OUT of that League (rule 7), and exercises
// the Needs-assignment repair surface. Fails on any browser console/page
// error.
const { chromium } = require("playwright");
const { spawn, spawnSync } = require("child_process");
const fs = require("fs");
const http = require("http");
const os = require("os");
const path = require("path");
const {
  installContextFixture, selectProgramSeason,
} = require("./context-fixture.js");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8141 },
  { label: "phone", width: 390, height: 844, port: 8142 },
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

// #233 B2b review r3: registration_league_not_in_season has no remaining
// documented v2 mutation path (delete_league now blocks on a live
// registration referencing it), so the repair UI's browser coverage below
// manufactures it the same way the backend's direct-injection test does —
// via the store, bypassing the service layer entirely — but from a SEPARATE
// process against the SAME durable SQLite file the running server was
// started against (see the DATABASE_URL passed into spawn() below). The
// server's own SQLite connection is autocommit/single-statement, and this
// script only runs between browser actions (never concurrently with a live
// request), so there is no write contention with the running server.
function injectCorruptRegistration(databasePath, { seasonId, teamId }) {
  const script = `
import os
from hockey_scheduler.store import create_store
from hockey_scheduler.domain import SeasonTeamRegistration, LeagueSeason

store = create_store(os.environ["FIXTURE_DB_PATH"])
try:
    # #283: registration_league_not_in_season is only reachable when a
    # registration's LeagueSeason binds its Season to a League that is NOT a
    # real League of that Season. Binding the Season to any REAL League makes
    # that League a legitimate member of the Season (a League may span
    # Seasons), so it produces NO defect — the only faithful reproduction,
    # matching the backend's test_repair_via_v2_after_direct_injection, is a
    # ghost LeagueSeason binding this Season to a non-existent League id.
    sid = os.environ["FIXTURE_SEASON_ID"]
    ghost_ls = store.add_league_season(LeagueSeason(
        id=store.next_id("leagueseason"),
        league_id="league_ghost", season_id=sid))
    reg_id = store.next_id("streg")
    store.add_season_team_registration(SeasonTeamRegistration(
        id=reg_id, league_season_id=ghost_ls.id,
        team_id=os.environ["FIXTURE_TEAM_ID"], division_id=None, active=True))
    print(reg_id)
finally:
    store.close()
`;
  const result = spawnSync(process.env.PYTHON || "python3", ["-c", script], {
    cwd: BACKEND_DIR,
    env: {
      ...process.env,
      FIXTURE_DB_PATH: databasePath,
      FIXTURE_SEASON_ID: seasonId,
      FIXTURE_TEAM_ID: teamId,
    },
    encoding: "utf8",
  });
  if (result.status !== 0) {
    throw new Error(`Fixture injection failed (exit ${result.status}): ${result.stderr}`);
  }
  return result.stdout.trim();
}

// #331 review round 18: renderSeasonParticipation's Save/Remove controls must
// render unique, distinct rows when a Team holds TWO simultaneously active
// registrations in the SAME Season across DIFFERENT Leagues -- a Rule 7
// violation (register_team_for_season/assign_season_team_league both refuse
// to create this) that only legacy data or a write path predating Rule 7 can
// leave behind, exactly like injectCorruptRegistration above reproduces
// registration_league_not_in_season. Plants a second SeasonTeamRegistration
// for an EXISTING team directly into the SAME durable SQLite file, under a
// DIFFERENT League's LeagueSeason, from a separate process (no write
// contention with the running server -- see injectCorruptRegistration).
function injectSecondActiveRegistration(databasePath, { seasonId, leagueId, teamId }) {
  const script = `
import os
from hockey_scheduler.store import create_store
from hockey_scheduler.domain import SeasonTeamRegistration

store = create_store(os.environ["FIXTURE_DB_PATH"])
try:
    ls = store.league_season_for(os.environ["FIXTURE_LEAGUE_ID"], os.environ["FIXTURE_SEASON_ID"])
    # SAY WHAT WENT WRONG (#409). None here means no LeagueSeason links this
    # League to this Season -- i.e. the create that should have made it was
    # REFUSED, for want of an explicit selection, and this injector is the
    # first thing downstream to touch the missing row. Falling through raised
    # "AttributeError: 'NoneType' object has no attribute 'id'" from the line
    # below, inside a subprocess, so the journey reported "launcher process
    # exited unexpectedly" and never mentioned context at all -- the single
    # most expensive misdiagnosis in this sweep. Name the real cause here.
    if ls is None:
        raise SystemExit(
            "no LeagueSeason links league {} to season {}, so there is nothing "
            "to register against. The create that should have made it was "
            "REFUSED -- check that the journey explicitly SELECTED the Program "
            "(and, for a Season-owned create, the Season) beforehand.".format(
                os.environ["FIXTURE_LEAGUE_ID"], os.environ["FIXTURE_SEASON_ID"]))
    reg_id = store.next_id("streg")
    store.add_season_team_registration(SeasonTeamRegistration(
        id=reg_id, league_season_id=ls.id,
        team_id=os.environ["FIXTURE_TEAM_ID"], division_id=None, active=True))
    print(reg_id)
finally:
    store.close()
`;
  const result = spawnSync(process.env.PYTHON || "python3", ["-c", script], {
    cwd: BACKEND_DIR,
    env: {
      ...process.env,
      FIXTURE_DB_PATH: databasePath,
      FIXTURE_SEASON_ID: seasonId,
      FIXTURE_LEAGUE_ID: leagueId,
      FIXTURE_TEAM_ID: teamId,
    },
    encoding: "utf8",
  });
  if (result.status !== 0) {
    throw new Error(`Second-registration fixture injection failed (exit ${result.status}): ${result.stderr}`);
  }
  return result.stdout.trim();
}

// Selects the Hierarchy sub-view via the user-visible toggle and confirms it
// took effect. Centralized so every Setup entry uses the same path and a
// later mutation cannot silently leave us on the workflow hub.
async function enterSetupHierarchy(page) {
  await page.click('[data-setup-view="hierarchy"]');
  await page.waitForFunction(() => {
    const seg = document.querySelector(".setup-viewtoggle .seg.active");
    return !!(seg && seg.dataset.setupView === "hierarchy");
  }, null, { timeout: 10000 });
}

async function checkViewport(browser, viewport) {
  const base = `http://${HOST}:${viewport.port}`;
  // A durable SQLite file (not the in-memory demo default) so the repair-UI
  // coverage below can inject a corrupt row directly into the same file from
  // a separate process (#233 B2b review r3). Demo mode still boots to a
  // clean slate against it (DemoState.__init__ calls reset(seed=False)), so
  // every other scenario in this file behaves identically to the in-memory
  // store it replaces.
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "hockey-participation-"));
  const databasePath = path.join(tempDir, `participation-${viewport.label}.sqlite`);
  const server = spawn(
    process.env.PYTHON || "python3",
    ["-u", "-m", "hockey_scheduler.web.server", "--host", HOST, "--port", String(viewport.port)],
    { cwd: BACKEND_DIR, env: { ...process.env, DATABASE_URL: databasePath },
      stdio: ["ignore", "pipe", "pipe"] });
  let serverOutput = "";
  server.stdout.on("data", (d) => { serverOutput += d.toString(); });
  server.stderr.on("data", (d) => { serverOutput += d.toString(); });

  const context = await browser.newContext({
    viewport: { width: viewport.width, height: viewport.height },
  });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(`[pageerror] ${e.message}`));
  // Named (not anonymous) so the partial-failure scenario below can detach it
  // for the one deliberately-failing request — Chromium logs a benign
  // "Failed to load resource: 500" console entry for any non-2xx response,
  // which isn't a real page bug and shouldn't fail the journey.
  const consoleErrorHandler = (m) => { if (m.type() === "error") errors.push(`[console] ${m.text()}`); };
  page.on("console", consoleErrorHandler);

  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });
    await installContextFixture(page);

    // Shared helpers (defined up front so both the main flow and the
    // edit/repair sections below can use them). refreshSetup re-clicks the
    // Setup tab (its onclick calls render() unconditionally, even when already
    // on that tab) so a raw fetch that mutated server state is reflected in
    // the page's own in-memory `hv`/`seasonRegs`/`leagueDivisions` state.
    const refreshSetup = async (marker) => {
      await page.click('.tab[data-tab="setup"]');
      // Setup now LANDS on the six-workflow hub (#345 batch 2). Select the
      // Hierarchy sub-view through the real toggle -- never by setting
      // setupView directly -- and assert the segment is actually active
      // BEFORE waiting on any domain control, so a future default change
      // fails here with "wrong sub-view" instead of an opaque selector
      // timeout on a control that was simply never rendered.
      await enterSetupHierarchy(page);
      await page.waitForFunction((m) => {
        const sel = document.querySelector(m.selector);
        return !!sel && (!m.optionValue || Array.from(sel.options).some((o) => o.value === m.optionValue));
      }, marker, { timeout: 15000 });
    };
    const toastText = () => page.$eval(
      "#toast-root .toast-msg", (el) => el.textContent).catch(() => "");
    const waitForToast = (expected) => page.waitForFunction(
      (t) => (document.querySelector("#toast-root .toast-msg") || {}).textContent === t,
      expected, { timeout: 15000 });
    // Every setup mutation on an EXISTING record binds to the ACTIVE Program +
    // Season (#369 prerequisite: the target guard refuses a record whose Season
    // isn't the selected one, with the same wording as a nonexistent id). With
    // no saved context the fallback picks the latest-id active Season, so a
    // fixture that builds two Seasons and then edits the FIRST one's rows would
    // be silently refused. This journey therefore selects its target Season
    // explicitly before each block of guarded edits rather than leaning on that
    // fallback — the guard is never weakened for the fixture's convenience.
    // #409: this is now ./context-fixture.js's asserted selection rather than
    // a local raw `POST /api/context`. Two things change. It goes through
    // `setActiveContext`, the app's own switch pipeline -- this journey drives
    // the Setup UI on the same page between blocks, so the client has to move
    // with the server. And the selection is proved by the server's own WRITE
    // ECHO as well as a read-back; the local version checked only the POST's
    // body, which cannot distinguish a persisted choice from the fallback
    // resolver's guess.
    const selectContext = (programId, seasonId) => selectProgramSeason(
      page, `[${viewport.label}] select ${programId}/${seasonId}`,
      programId, seasonId);

    // Build a permanent program team under its PERMANENT League, plus two
    // seasons. #283 Slice E: a Team is created with a REQUIRED league_id (its
    // permanent League) and may only ever register into THAT League (rule 7).
    // A permanent League spans Seasons, so the single League `lg1` is what the
    // team plays in BOTH seasons — created against season 1 here and bound to
    // season 2 later (there is no v2 "bind league to season" call; a League
    // joins a Season only via a registration, so the season-2 binding is
    // bootstrapped in the middle of the flow below). A second Division `dA2`
    // under lg1 (created now, while lg1 spans a single Season — a Division
    // create is ambiguous once its League spans several) gives the edit-path
    // Save a real Division to move the registration to WITHIN its own League.
    const ids = await page.evaluate(async () => {
      const F = window.hsFixture;
      const program = await F.create("program", "/api/v2/setup/program", { name: "Participation Program" });
      // #409 EXPLICIT SELECTION, boundary 1: both Season creates are
      // PROGRAM-AXIS, and minting the Program is not selecting it.
      await F.selectProgram("Program-only bootstrap", program.id);
      const s1 = await F.create("season 2026-27", "/api/v2/setup/season", { program_id: program.id, name: "2026-27" });
      const s2 = await F.create("season 2027-28", "/api/v2/setup/season", { program_id: program.id, name: "2027-28" });
      // BOUNDARY 2: the League and both Divisions are SEASON-OWNED and land
      // in SEASON ONE — not merely "a" Season, and not the s2 that was
      // created last. With two Seasons in the install the fallback would pick
      // the latest-id one, which is exactly the wrong answer here.
      await F.selectProgramSeason("Program + Season One", program.id, s1.id);
      const lg1 = await F.create("Adult League", "/api/v2/setup/league", { season_id: s1.id, name: "Adult League" });
      const dA = await F.create("division Gold", "/api/v2/setup/division", { league_id: lg1.id, name: "Gold" });
      const dA2 = await F.create("division Platinum", "/api/v2/setup/division", { league_id: lg1.id, name: "Platinum" });
      const club = await F.create("club", "/api/v2/setup/club", { name: "Participation Club" });
      const team = await F.create("team", "/api/v2/setup/team",
        { league_id: lg1.id, club_id: club.id, name: "Perma Lions" });
      // lg1 is the team's ONE permanent League; the team plays it in BOTH
      // seasons. lg2 is kept as an alias so the season-2 selectors below read
      // naturally — it is the SAME permanent League id.
      return { program: program.id, s1: s1.id, s2: s2.id, lg1: lg1.id, lg2: lg1.id,
        dA: dA.id, dA2: dA2.id, team: team.id };
    });

    // Open Setup → Season participation reflects the fresh, empty seasons.
    // (Navigating to the tab re-renders and re-fetches the registration data;
    // no full reload, which would drop the signed-in session.) The register
    // control lives per Season+League (#331 review round 3 finding 2 — a
    // League-only id collided across Seasons sharing that same permanent
    // League, which this file's own lg1/lg2 fixture below exercises).
    await page.click('.tab[data-tab="setup"]');
    // Setup now LANDS on the six-workflow hub (#345 batch 2). Select the
    // Hierarchy sub-view through the real toggle -- never by setting
    // setupView directly -- and assert the segment is actually active
    // BEFORE waiting on any domain control, so a future default change
    // fails here with "wrong sub-view" instead of an opaque selector
    // timeout on a control that was simply never rendered.
    await enterSetupHierarchy(page);
    await page.waitForFunction(
      (sel) => !!document.querySelector(sel), `#reg-team-${ids.s1}-${ids.lg1}`, { timeout: 15000 });

    // #409 ORDERING FIX. This used to select SEASON TWO here — but the very
    // next thing the journey does is register the team into SEASON ONE
    // through the real UI, and a registration is SEASON-OWNED: it is judged
    // against the SAVED Season, which would have been the wrong one. The
    // Season-2 selection now sits where the Season-2 work actually starts
    // (the bootstrap block below states it for itself), and Season One is
    // stated here, for the Season-One registration that immediately follows.
    // The old order only survived because the create inferred its Season from
    // the parent it named; that inference is what #409 removes.
    await selectContext(ids.program, ids.s1);

    // #233 B2b review r3: the fixture Program above was created with no
    // operator_organization_id (optional on the canonical Program, B2a/ADR
    // 0001) — it must never be listed under Needs assignment as a
    // scheduling blocker (that panel's own copy says "can't be scheduled
    // until assigned", which no longer applies to a missing operator).
    const naText = await page.evaluate(() => {
      const el = document.querySelector(".tree-panel.na");
      return el ? el.textContent : "";
    });
    if (/operating organization/i.test(naText)) {
      throw new Error(`[${viewport.label}] orgless Program listed under Needs assignment: ${naText}`);
    }

    // Register the permanent team for season 1 under its permanent League /
    // Division Gold. The League select already defaults to lg1 (its section).
    await page.selectOption(`#reg-team-${ids.s1}-${ids.lg1}`, ids.team);
    await page.selectOption(`#reg-div-add-${ids.s1}-${ids.lg1}`, ids.dA);
    let resp = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/seasons/${ids.s1}/team-registrations`
      && r.request().method() === "POST");
    await page.click(`[data-reg-add="${ids.lg1}"]`);
    if ((await resp).status() !== 200) throw new Error(`[${viewport.label}] register s1 failed`);

    // Bootstrap the permanent League into season 2: a League participates in a
    // Season only via a LeagueSeason, which a registration creates (#283) —
    // there is no standalone v2 bind call. Register the team into season 2 and
    // immediately remove it, so the LeagueSeason(lg1, s2) persists but the team
    // has NO active season-2 registration. The team is registered in season 1
    // now, so its season-1 register control is gone — after this bootstrap the
    // team's season-2 register control (same League id) is the ONLY one on the
    // page, keeping the selector unambiguous, and the real UI registration
    // below reactivates the bootstrapped row in place.
    await page.evaluate(async (i) => {
      const F = window.hsFixture;
      // #409: this bootstrap registration is SEASON-OWNED and lands in
      // SEASON TWO, so Season Two is what has to be saved for it.
      await F.selectProgramSeason("Program + Season Two", i.program, i.s2);
      const boot = await F.create("season-2 bootstrap registration",
        `/api/v2/setup/seasons/${i.s2}/team-registrations`,
        { team_id: i.team, league_id: i.lg1, division_id: null });
      await F.call("remove the bootstrap registration",
        `/api/v2/setup/season-team-registration/${boot.id}/remove`, {});
    }, ids);
    await refreshSetup({ selector: `#reg-team-${ids.s2}-${ids.lg2}` });

    // Register the SAME permanent team for season 2 under the SAME permanent
    // League (rule 7 — it can register nowhere else). Division is left at "No
    // division": a Division under lg1 for season 2 can't be created via the v2
    // API once lg1 spans both seasons (ambiguous), so this registration is
    // league-only. The team is still available for s2 (its bootstrapped row is
    // inactive), so its s2 register control is present.
    await page.selectOption(`#reg-team-${ids.s2}-${ids.lg2}`, ids.team);
    resp = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/seasons/${ids.s2}/team-registrations`
      && r.request().method() === "POST");
    await page.click(`[data-reg-add="${ids.lg2}"]`);
    if ((await resp).status() !== 200) throw new Error(`[${viewport.label}] register s2 failed`);
    // Now the team is registered for BOTH seasons, so each season shows a
    // Remove control (and its register select is gone — nothing left to add).
    await page.waitForFunction(
      () => document.querySelectorAll("[data-reg-remove]").length >= 2, null, { timeout: 15000 });

    // Exactly one permanent Team backs two season registrations.
    const state = await page.evaluate(async (i) => {
      const get = async (p) => (await fetch(p, { credentials: "same-origin" })).json();
      const teams = await get(`/api/v2/setup/programs/${i.program}/teams`);
      const r1 = await get(`/api/v2/setup/seasons/${i.s1}/team-registrations`);
      const r2 = await get(`/api/v2/setup/seasons/${i.s2}/team-registrations`);
      return { teamCount: teams.teams.length,
               r1: r1.registrations.filter((r) => r.active).length,
               r2: r2.registrations.filter((r) => r.active).length };
    }, ids);
    if (state.teamCount !== 1 || state.r1 !== 1 || state.r2 !== 1) {
      throw new Error(`[${viewport.label}] expected 1 team + 2 registrations, got ${JSON.stringify(state)}`);
    }

    // Both registrations are under the team's ONE permanent League (rule 7).
    const bothLeagues = await page.evaluate(async (i) => {
      const get = async (p) => (await fetch(p, { credentials: "same-origin" })).json();
      const r1 = (await get(`/api/v2/setup/seasons/${i.s1}/team-registrations`)).registrations
        .find((r) => r.active && r.team_id === i.team);
      const r2 = (await get(`/api/v2/setup/seasons/${i.s2}/team-registrations`)).registrations
        .find((r) => r.active && r.team_id === i.team);
      return { l1: r1 && r1.league_id, l2: r2 && r2.league_id };
    }, ids);
    if (bothLeagues.l1 !== ids.lg1 || bothLeagues.l2 !== ids.lg1) {
      throw new Error(`[${viewport.label}] registrations weren't both under the permanent League: ${
        JSON.stringify(bothLeagues)}`);
    }

    // Remove the team from season 2; season 1 must be untouched. Season/league
    // blocks render in season order (s1 then s2), so the last Remove button is
    // s2's.
    const removeBtns = await page.$$("[data-reg-remove]");
    resp = page.waitForResponse((r) => r.url().includes("/remove") && r.request().method() === "POST");
    await removeBtns[removeBtns.length - 1].click();
    await resp;
    // Removed from s2 → the team is available for s2 again, so its register
    // control reappears; wait for the panel to settle on that.
    await page.waitForFunction(
      (sel) => !!document.querySelector(sel), `#reg-team-${ids.s2}-${ids.lg2}`, { timeout: 15000 });

    const after = await page.evaluate(async (i) => {
      const get = async (p) => (await fetch(p, { credentials: "same-origin" })).json();
      const r1 = await get(`/api/v2/setup/seasons/${i.s1}/team-registrations`);
      const r2 = await get(`/api/v2/setup/seasons/${i.s2}/team-registrations`);
      const teams = await get(`/api/v2/setup/programs/${i.program}/teams`);
      return { r1: r1.registrations.filter((r) => r.active).length,
               r2: r2.registrations.filter((r) => r.active).length,
               teamCount: teams.teams.length };
    }, ids);
    if (after.teamCount !== 1) throw new Error(`[${viewport.label}] team was deleted on removal`);
    if (after.r1 !== 1) throw new Error(`[${viewport.label}] season 1 registration was lost`);
    if (after.r2 !== 0) throw new Error(`[${viewport.label}] season 2 removal didn't take effect`);

    // --- Edit-path + repair-surface coverage (#233 B2b review, #283 rule 7) --
    // The journey above only exercised Register/Remove. These steps drive the
    // Save controls' Division cascade (shared by both Season participation and
    // the Needs-assignment repair row via saveRegistrationPlacement() in
    // app.js) WITHIN the team's permanent League, prove the backend refuses to
    // move a registration OUT of that League (rule 7), and drive the repair
    // row itself. New fixtures are created via raw v2 fetches (like the setup
    // above); refreshSetup() forces the page to refetch its in-memory state.

    // Lions (still active in season 1 under lg1 / Division Gold) is reused for
    // the edit-path steps. A SECOND permanent League ("Sapphire") with its own
    // Division ("Division C") in the SAME season is where the rule-7 rejection
    // below tries (and fails) to move Lions, and is the permanent League of a
    // NEW team (Perma Bears) used for the league-only register step.
    const edit = await page.evaluate(async (i) => {
      const F = window.hsFixture;
      const get = async (p) => (await fetch(p, { credentials: "same-origin" })).json();
      // #409: Sapphire and Division C are SEASON-OWNED and land in Season
      // One, which the block above left the saved Season off.
      await F.selectProgramSeason("Program + Season One", i.program, i.s1);
      const lg1b = await F.create("league Sapphire", "/api/v2/setup/league", { season_id: i.s1, name: "Sapphire" });
      const divC = await F.create("divC", "/api/v2/setup/division", { league_id: lg1b.id, name: "Division C" });
      const club = await F.create("club", "/api/v2/setup/club", { name: "Edit Coverage Club" });
      // Perma Bears' permanent League is Sapphire (rule 2/7).
      const bears = await F.create("bears", "/api/v2/setup/team",
        { league_id: lg1b.id, club_id: club.id, name: "Perma Bears" });
      const r1 = (await get(`/api/v2/setup/seasons/${i.s1}/team-registrations`)).registrations;
      const lionsReg = r1.find((r) => r.active && r.team_id === i.team);
      return { lg1b: lg1b.id, divC: divC.id, club: club.id, bears: bears.id, lionsReg: lionsReg.id };
    }, ids);
    await refreshSetup({ selector: `#reg-league-${edit.lionsReg}`, optionValue: edit.lg1b });

    // Every edit below acts on Lions' SEASON-1 registration (and the s1-scoped
    // blocked-league-delete), so switch the active context to s1 before the
    // first guarded edit — otherwise the guard refuses the assign-division with
    // the nonexistent-id wording and the success toast never appears.
    await selectContext(ids.program, ids.s1);

    // (1) Division change WITHIN the permanent League: move Lions from Gold to
    // Platinum (both Divisions of lg1). League is unchanged, so Save fires a
    // single assign-division to the new Division, and the stored registration
    // lands on lg1 / Platinum.
    let seq = [];
    const track = (req) => {
      const url = req.url();
      if (req.method() === "POST" && url.includes(`/season-team-registration/${edit.lionsReg}/`)) {
        seq.push({ url, body: req.postDataJSON() });
      }
    };
    page.on("request", track);
    await page.selectOption(`#reg-div-${edit.lionsReg}`, ids.dA2);
    await page.click(`[data-reg-save="${edit.lionsReg}"]`);
    await waitForToast("Season registration updated.");
    page.off("request", track);
    if (seq.length !== 1
        || !seq[0].url.endsWith("/assign-division") || seq[0].body.division_id !== ids.dA2) {
      throw new Error(`[${viewport.label}] unexpected division-change request sequence: ${JSON.stringify(seq)}`);
    }
    let stored = await page.evaluate(async (i) => {
      const get = async (p) => (await fetch(p, { credentials: "same-origin" })).json();
      const regs = (await get(`/api/v2/setup/seasons/${i.s1}/team-registrations`)).registrations;
      return regs.find((r) => r.id === i.reg);
    }, { s1: ids.s1, reg: edit.lionsReg });
    if (!stored || stored.league_id !== ids.lg1 || stored.division_id !== ids.dA2) {
      throw new Error(`[${viewport.label}] division change didn't land on lg1 / Platinum: ${JSON.stringify(stored)}`);
    }

    // (2) Clear Division, keep League: Save with the League unchanged and
    // Division reset to "No division" fires exactly ONE request.
    seq = [];
    page.on("request", track);
    await page.selectOption(`#reg-div-${edit.lionsReg}`, "");
    await page.click(`[data-reg-save="${edit.lionsReg}"]`);
    await waitForToast("Season registration updated.");
    page.off("request", track);
    if (seq.length !== 1 || !seq[0].url.endsWith("/assign-division") || seq[0].body.division_id !== null) {
      throw new Error(`[${viewport.label}] clear-division Save fired unexpected requests: ${JSON.stringify(seq)}`);
    }
    stored = await page.evaluate(async (i) => {
      const get = async (p) => (await fetch(p, { credentials: "same-origin" })).json();
      const regs = (await get(`/api/v2/setup/seasons/${i.s1}/team-registrations`)).registrations;
      return regs.find((r) => r.id === i.reg);
    }, { s1: ids.s1, reg: edit.lionsReg });
    if (!stored || stored.league_id !== ids.lg1 || stored.division_id) {
      throw new Error(`[${viewport.label}] clear-division didn't keep lg1 with a null Division: ${JSON.stringify(stored)}`);
    }

    // (3) League-only registration: register a NEW team (Perma Bears) under
    // its permanent League (Sapphire) but leaving Division at "No division" —
    // the create POST body must carry division_id: null (not omitted, not "").
    await page.waitForFunction(
      (sel) => !!document.querySelector(sel), `#reg-team-${ids.s1}-${edit.lg1b}`, { timeout: 15000 });
    await page.selectOption(`#reg-team-${ids.s1}-${edit.lg1b}`, edit.bears);
    const addResp = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/seasons/${ids.s1}/team-registrations`
      && r.request().method() === "POST");
    await page.click(`[data-reg-add="${edit.lg1b}"]`);
    const addBody = (await addResp).request().postDataJSON();
    if (addBody.division_id !== null) {
      throw new Error(`[${viewport.label}] league-only register body had division_id ${
        JSON.stringify(addBody.division_id)}, expected null`);
    }
    stored = await page.evaluate(async (i) => {
      const get = async (p) => (await fetch(p, { credentials: "same-origin" })).json();
      const regs = (await get(`/api/v2/setup/seasons/${i.s1}/team-registrations`)).registrations;
      return regs.find((r) => r.active && r.team_id === i.bears);
    }, { s1: ids.s1, bears: edit.bears });
    if (!stored || stored.league_id !== edit.lg1b || stored.division_id !== null) {
      throw new Error(`[${viewport.label}] league-only registration didn't store Sapphire + null division: ${
        JSON.stringify(stored)}`);
    }

    // (5) Rule-7 rejection (#283 Slice E): a Team may only ever be in its OWN
    // permanent League, so trying to move Lions' registration out of lg1 into
    // Sapphire (Perma Bears' League, NOT Lions') is refused by the backend and
    // the registration is left exactly as it was. This replaces the pre-#283
    // cross-league edit path, which is no longer a valid operation.
    await page.waitForFunction(
      (sel) => !!document.querySelector(sel), `#reg-league-${edit.lionsReg}`, { timeout: 15000 });
    await page.selectOption(`#reg-league-${edit.lionsReg}`, edit.lg1b);
    // Detach the console-error listener only for this deliberately-failing
    // request: the server's 400 response logs a benign "Failed to load
    // resource: 400" Chromium console entry, not a real page bug.
    page.off("console", consoleErrorHandler);
    await page.click(`[data-reg-save="${edit.lionsReg}"]`);
    const expectedRejectToast = "A team may only register in its own League.";
    await waitForToast(expectedRejectToast);
    page.on("console", consoleErrorHandler);
    const actualToast = await toastText();
    if (actualToast !== expectedRejectToast) {
      throw new Error(`[${viewport.label}] rule-7 rejection toast mismatch: ${actualToast}`);
    }
    stored = await page.evaluate(async (i) => {
      const get = async (p) => (await fetch(p, { credentials: "same-origin" })).json();
      const regs = (await get(`/api/v2/setup/seasons/${i.s1}/team-registrations`)).registrations;
      return regs.find((r) => r.id === i.reg);
    }, { s1: ids.s1, reg: edit.lionsReg });
    if (!stored || stored.league_id !== ids.lg1 || stored.division_id) {
      throw new Error(`[${viewport.label}] rejected league move altered the registration: ${JSON.stringify(stored)}`);
    }

    // (6) Blocked delete: a League still holding a live, division-less
    // registration cannot be deleted (#233 B2b review r2 — delete_league now
    // checks registrations, not just Divisions, as a dependent; see
    // setup_service.py). This used to be reachable and was exactly how an
    // earlier version of this journey manufactured an orphaned registration
    // for the repair-surface UI below — that path is now correctly blocked,
    // which also means registration_league_not_in_season (like
    // registration_league_division_mismatch, already established as
    // unreachable) has no remaining documented-v2-mutation path to produce
    // it. test_v2_onboarding_status.py's test_invalid_registrations_are_reported
    // and test_repair_via_v2_after_direct_injection cover detection and
    // repair at the backend level via direct store injection; section (7)
    // below drives the same defect through the actual browser repair UI
    // (data-repair-* controls), using the same direct-injection technique
    // against this journey's own durable SQLite-backed server (#233 B2b
    // review r3).
    const lgGuarded = await page.evaluate(async (i) => {
      const F = window.hsFixture;
      // #409: Bronze and Perma Foxes' registration are SEASON-OWNED in
      // Season One.
      await F.selectProgramSeason("Program + Season One", i.program, i.s1);
      const lg = await F.create("league Bronze", "/api/v2/setup/league", { season_id: i.s1, name: "Bronze" });
      // Perma Foxes' permanent League is Bronze; it registers there (rule 7).
      const foxes = await F.create("foxes", "/api/v2/setup/team",
        { league_id: lg.id, club_id: i.club, name: "Perma Foxes" });
      const reg = await F.create("reg", `/api/v2/setup/seasons/${i.s1}/team-registrations`,
        { team_id: foxes.id, league_id: lg.id, division_id: null });
      return { lg: lg.id, reg: reg.id };
    }, { s1: ids.s1, program: ids.program, club: edit.club });
    // Scoped to the Competition structure tree (#251): the Season
    // participation panel below it now renders its own delBtn for the same
    // League id, so an unscoped selector would match twice.
    await refreshSetup({
      selector: `#competition-structure [data-del="level"][data-del-id="${lgGuarded.lg}"]` });
    await page.click(
      `#competition-structure [data-del="level"][data-del-id="${lgGuarded.lg}"]`);
    // Level (grouping League) isn't a high-risk kind, so its confirm button
    // is enabled immediately — no typed DELETE needed (unlike the umbrella
    // Program/"league" kind exercised in safe-destructive.js).
    await page.waitForSelector(".modal.danger [data-del-confirm]", { timeout: 10000 });
    // Detach the console-error listener only for this deliberately-blocked
    // delete: the server's 409 response logs a benign "Failed to load
    // resource" Chromium console entry, not a real page bug (same pattern
    // as the simulated partial-failure request above).
    page.off("console", consoleErrorHandler);
    await page.click("[data-del-confirm]");
    await page.waitForSelector(".modal.blocked", { timeout: 10000 });
    page.on("console", consoleErrorHandler);
    const blockedText = await page.textContent(".modal.blocked .modal-body");
    if (!/team registration/i.test(blockedText)) {
      throw new Error(`[${viewport.label}] blocked-league-delete modal missing the registration dependency: ${blockedText}`);
    }
    await page.click(".modal.blocked [data-modal-close]");
    await page.waitForSelector(".modal", { state: "detached", timeout: 10000 });
    const stillThere = await page.evaluate(async (i) => {
      const get = async (p) => (await fetch(p, { credentials: "same-origin" })).json();
      return !!(await get("/api/v2/setup/hierarchy")).programs
        .flatMap((p) => p.seasons).flatMap((s) => s.leagues || [])
        .some((lv) => lv.id === i.lg);
    }, { lg: lgGuarded.lg });
    if (!stillThere) {
      throw new Error(`[${viewport.label}] a blocked league delete removed the league anyway`);
    }

    // (7) Repair surface, driven through the actual browser controls (#233
    // B2b review r3): the prior rounds proved the repair MECHANICS
    // (saveRegistrationPlacement()) via the edit-path steps above and proved
    // SERVICE-level detection/repair via a direct-injection backend test —
    // neither exercises the dedicated data-repair-* rendering/wiring. This
    // manufactures the same registration_league_not_in_season defect via a
    // direct write to this journey's own durable SQLite file (see
    // injectCorruptRegistration above), never through a documented v2
    // mutation, then drives the row through Needs assignment end to end.
    const repairFixture = await page.evaluate(async (i) => {
      const F = window.hsFixture;
      // The CORRECT League (repair target) lives in seasonR and is the
      // permanent League of the corrupt row's team (rule 7 — the repair's
      // assign-league only succeeds into the team's own League). The corrupt
      // row itself references a GHOST (non-existent) League — the only shape
      // that reproduces registration_league_not_in_season under #283, matching
      // test_repair_via_v2_after_direct_injection in test_v2_onboarding_status.py
      // (see injectCorruptRegistration). A second real League (otherLeagueR) in
      // a DIFFERENT season under the same program proves the repair row's
      // League select is scoped to seasonR — it must offer leagueR but never
      // otherLeagueR.
      // #409: the two Season creates are PROGRAM-AXIS; each League is
      // SEASON-OWNED and lands in a DIFFERENT Season, so the saved Season
      // moves to the one being written into before each. That distinction is
      // the point of this fixture — the repair row's League select must be
      // scoped to seasonR and must never offer otherLeagueR.
      await F.selectProgram("Program-only for the repair Seasons", i.program);
      const seasonR = await F.create("Repair Season", "/api/v2/setup/season", { program_id: i.program, name: "Repair Season" });
      await F.selectProgramSeason("Program + Repair Season", i.program, seasonR.id);
      const leagueR = await F.create("Repair League", "/api/v2/setup/league", { season_id: seasonR.id, name: "Repair League" });
      const otherSeasonR = await F.create("Repair Season B", "/api/v2/setup/season", { program_id: i.program, name: "Repair Season B" });
      await F.selectProgramSeason("Program + Repair Season B", i.program, otherSeasonR.id);
      const otherLeagueR = await F.create("Repair League B", "/api/v2/setup/league", { season_id: otherSeasonR.id, name: "Repair League B" });
      // Back to seasonR: Repair Foxes is PROGRAM-AXIS, but leaving the saved
      // Season on Repair Season B would misstate which Season this fixture
      // block is building into.
      await F.selectProgramSeason("Program + Repair Season", i.program, seasonR.id);
      const clubR = await F.create("clubR", "/api/v2/setup/club", { name: "Repair Club" });
      const teamR = await F.create("teamR", "/api/v2/setup/team",
        { league_id: leagueR.id, club_id: clubR.id, name: "Repair Foxes" });
      return { seasonR: seasonR.id, leagueR: leagueR.id, otherLeagueR: otherLeagueR.id, teamR: teamR.id };
    }, { program: ids.program });
    const repairRegId = injectCorruptRegistration(databasePath, {
      seasonId: repairFixture.seasonR, teamId: repairFixture.teamR,
    });

    // The repair Save acts on a registration in seasonR (its ghost LeagueSeason
    // binds seasonR), so select seasonR before driving the repair. The corrupt
    // row's League is a GHOST — deliberately left judged by its own Season by
    // the target guard so a broken-League registration stays repairable — and
    // this journey is what proves that repair path end to end.
    await selectContext(ids.program, repairFixture.seasonR);

    await refreshSetup({ selector: `[data-repair-save="${repairRegId}"]` });

    // The invalid row appears with its diagnostic and the offending team's
    // name; the League select offers only Leagues from its OWN season
    // (leagueR, never otherLeagueR — a different season's League).
    const repairRow = await page.evaluate((regId) => {
      const btn = document.querySelector(`[data-repair-save="${regId}"]`);
      const row = btn.closest(".repair-row");
      const leagueSel = document.querySelector(`[data-repair-league-for="${regId}"]`);
      return {
        text: row.textContent,
        leagueOptions: Array.from(leagueSel.options).map((o) => o.value).filter(Boolean),
      };
    }, repairRegId);
    if (!/Repair Foxes/.test(repairRow.text) || !/isn't in this season/.test(repairRow.text)) {
      throw new Error(`[${viewport.label}] repair row missing team name/diagnostic: ${repairRow.text}`);
    }
    if (!repairRow.leagueOptions.includes(repairFixture.leagueR)
        || repairRow.leagueOptions.includes(repairFixture.otherLeagueR)) {
      throw new Error(`[${viewport.label}] repair row League options wrong: ${JSON.stringify(repairRow.leagueOptions)}`);
    }

    // Readiness blocks on the invalid registration before repair.
    let onboarding = await page.evaluate(async () =>
      (await fetch("/api/v2/onboarding/status", { credentials: "same-origin" })).json());
    if (!onboarding.blocking.some((b) => b.code === "invalid_registrations")) {
      throw new Error(`[${viewport.label}] readiness didn't block on the injected invalid registration`);
    }

    // Repair through the cascade: choose the correct League (the team's own),
    // leave Division at "No division", Save.
    await page.selectOption(`[data-repair-league-for="${repairRegId}"]`, repairFixture.leagueR);
    await page.click(`[data-repair-save="${repairRegId}"]`);
    await waitForToast("Registration repaired — moved into the selected league/division.");

    // The row disappears from Needs assignment...
    await page.waitForSelector(`[data-repair-save="${repairRegId}"]`, { state: "detached", timeout: 15000 });
    // ...and the team reappears in the valid League branch (league-only —
    // no division was chosen).
    const afterRepair = await page.evaluate(async (i) => {
      const hv = await (await fetch("/api/v2/setup/hierarchy", { credentials: "same-origin" })).json();
      const seasonNode = hv.programs.flatMap((p) => p.seasons).find((s) => s.id === i.seasonR);
      const leagueNode = (seasonNode.leagues || []).find((lv) => lv.id === i.leagueR);
      return {
        issuesLeft: (seasonNode.needs_assignment && seasonNode.needs_assignment.registrations) || [],
        inLeague: !!(leagueNode && (leagueNode.teams_without_division || []).some((t) => t.id === i.teamR)),
      };
    }, repairFixture);
    if (afterRepair.issuesLeft.length) {
      throw new Error(`[${viewport.label}] needs_assignment still reports the repaired registration: ${
        JSON.stringify(afterRepair.issuesLeft)}`);
    }
    if (!afterRepair.inLeague) {
      throw new Error(`[${viewport.label}] repaired team didn't land under the chosen League`);
    }

    // Readiness no longer blocks on it.
    onboarding = await page.evaluate(async () =>
      (await fetch("/api/v2/onboarding/status", { credentials: "same-origin" })).json());
    if (onboarding.blocking.some((b) => b.code === "invalid_registrations")) {
      throw new Error(`[${viewport.label}] readiness still blocks on invalid_registrations after repair`);
    }

    // (8) #331 review round 18: a Team with TWO simultaneously active
    // registrations in the SAME Season, across DIFFERENT Leagues (a Rule 7
    // violation only legacy data/a stale write path can leave behind — the
    // exact shape commit_teams_players_import's team_registration_conflict
    // rejection exists to catch before an import can create it) must render
    // as two DISTINCT, independently addressable rows — never one row's
    // Save/Remove silently acting on the OTHER row's registration.
    const dupFixture = await page.evaluate(async (i) => {
      const F = window.hsFixture;
      // #409: both Leagues and Perma Wolves' registration are SEASON-OWNED
      // in Season One, and the repair block above left the saved Season on
      // Repair Season.
      await F.selectProgramSeason("Program + Season One", i.program, i.s1);
      const home = await F.create("league Timber", "/api/v2/setup/league", { season_id: i.s1, name: "Timber League" });
      const stray = await F.create("league Ridge", "/api/v2/setup/league", { season_id: i.s1, name: "Ridge League" });
      const club = await F.create("club", "/api/v2/setup/club", { name: "Duplicate Coverage Club" });
      const team = await F.create("team", "/api/v2/setup/team",
        { league_id: home.id, club_id: club.id, name: "Perma Wolves" });
      const homeReg = await F.create("homeReg", `/api/v2/setup/seasons/${i.s1}/team-registrations`,
        { team_id: team.id, league_id: home.id, division_id: null });
      return { home: home.id, stray: stray.id, team: team.id, homeReg: homeReg.id };
    }, { s1: ids.s1, program: ids.program });
    const strayRegId = injectSecondActiveRegistration(databasePath, {
      seasonId: ids.s1, leagueId: dupFixture.stray, teamId: dupFixture.team,
    });
    // The duplicate-registration coverage operates on s1 rows (Perma Wolves'
    // two s1 registrations); the repair block above left the context on
    // seasonR, so switch back to s1 before the keyboard-driven Remove.
    await selectContext(ids.program, ids.s1);
    await refreshSetup({ selector: `[data-reg-remove="${strayRegId}"]` });

    // Both rows exist, are DISTINCT elements, and each names Perma Wolves —
    // the pre-fix `regByTeam` collapse would leave one of these selectors
    // missing (the last-write-wins map only ever exposed ONE reg id to
    // BOTH League sections).
    const dupRows = await page.evaluate((i) => {
      const homeBtn = document.querySelector(`[data-reg-remove="${i.homeReg}"]`);
      const strayBtn = document.querySelector(`[data-reg-remove="${i.strayReg}"]`);
      return {
        homeText: homeBtn && homeBtn.closest(".reg-row").textContent,
        strayText: strayBtn && strayBtn.closest(".reg-row").textContent,
        sameElement: homeBtn === strayBtn,
      };
    }, { homeReg: dupFixture.homeReg, strayReg: strayRegId });
    if (!dupRows.homeText || !/Perma Wolves/.test(dupRows.homeText)) {
      throw new Error(`[${viewport.label}] Timber League row for Perma Wolves missing/wrong: ${JSON.stringify(dupRows)}`);
    }
    if (!dupRows.strayText || !/Perma Wolves/.test(dupRows.strayText)) {
      throw new Error(`[${viewport.label}] Ridge League row for Perma Wolves missing/wrong: ${JSON.stringify(dupRows)}`);
    }
    if (dupRows.sameElement) {
      throw new Error(`[${viewport.label}] both League rows resolved to the SAME element/registration id`);
    }

    // Keyboard-operate the Ridge League row's Remove control (Tab-reachable
    // focus + Enter, not just a click) — only ITS registration deactivates.
    await page.evaluate((regId) => {
      document.querySelector(`[data-reg-remove="${regId}"]`).focus();
    }, strayRegId);
    const removeResp = page.waitForResponse((r) =>
      r.url().includes(`/season-team-registration/${strayRegId}/remove`) && r.request().method() === "POST");
    await page.keyboard.press("Enter");
    if ((await removeResp).status() !== 200) {
      throw new Error(`[${viewport.label}] keyboard-activated Remove on the Ridge League row failed`);
    }
    // Wait for the SETTLED post-render state in one shot (both the stray
    // row's removal AND the home row's survival) rather than checking them
    // as two separate steps -- render() tears down and rebuilds the whole
    // tree on completion, so polling right after the stray node detaches
    // could race a rebuild that hasn't reached the home row yet.
    await page.waitForFunction((i) =>
      !document.querySelector(`[data-reg-remove="${i.strayReg}"]`)
      && !!document.querySelector(`[data-reg-remove="${i.homeReg}"]`),
      { strayReg: strayRegId, homeReg: dupFixture.homeReg }, { timeout: 15000 });
    const finalRegs = await page.evaluate(async (i) => {
      const get = async (p) => (await fetch(p, { credentials: "same-origin" })).json();
      const regs = (await get(`/api/v2/setup/seasons/${i.s1}/team-registrations`)).registrations;
      return {
        home: regs.find((r) => r.id === i.homeReg),
        stray: regs.find((r) => r.id === i.strayReg),
      };
    }, { s1: ids.s1, homeReg: dupFixture.homeReg, strayReg: strayRegId });
    if (!finalRegs.home || !finalRegs.home.active) {
      throw new Error(`[${viewport.label}] Timber League's registration was deactivated by mistake: ${JSON.stringify(finalRegs.home)}`);
    }
    if (!finalRegs.stray || finalRegs.stray.active) {
      throw new Error(`[${viewport.label}] Ridge League's registration wasn't deactivated: ${JSON.stringify(finalRegs.stray)}`);
    }

    if (errors.length) {
      throw new Error(`[${viewport.label}] console/page errors:\n${errors.join("\n")}`);
    }
    console.log(`[${viewport.label}] OK — one permanent team, two seasons under its permanent league, safe removal.`);
  } catch (error) {
    // Append the server tail WITHOUT discarding the original error: rewrapping
    // in a new Error dropped the Playwright stack, so a timeout surfaced only as
    // its message and never named the failing selector/assertion.
    error.message = `${error.message}\n--- demo server output ---\n${serverOutput}`;
    throw error;
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
    console.log("Season participation browser journey passed.");
  } catch (error) {
    console.error("Season participation browser journey FAILED.");
    console.error(error && error.message ? error.message : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
