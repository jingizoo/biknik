// Season rollover browser journey (#180, cut to v2 canonical #233 Slice B2b).
//
// At desktop and phone widths, a League Admin builds two permanent program
// teams registered in a source season, pre-registers one of them in the
// target season, then uses the Setup "Season rollover" panel to carry the
// source season's participation forward: the pre-registered team is shown as
// already registered (and never offered), the remaining team is selected, and
// — crucially — the commit stays disabled with an inline row error until that
// team is assigned a target-season League (Division stays optional in v2), so
// an unassigned selection can never be submitted and no write happens before a
// valid choice (review #216, re-scoped to League for v2). The journey then
// commits and verifies the carried team lands in the chosen target
// league/division, the already-registered team is untouched, and no permanent
// Team is duplicated.
//
// Section (5) (#331 review round 19 finding 4) then covers a Team with TWO
// simultaneously active SOURCE registrations (legacy data / a write path
// predating Rule 7 -- register_team_for_season/assign_season_team_league/
// transfer_team_to_league all now refuse to create this for a Team with a
// permanent League already set) — the rollover panel must render both as
// genuinely distinct, independently selectable/editable rows (never
// colliding on a team-id-only key) and must carry the SPECIFIC row's own
// Division into the committed request, keyboard-operated (real Enter
// activation, not just a click) to prove the fix isn't just mouse-path-only.
// Fails on any browser console/page error.
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
  { label: "desktop", width: 1440, height: 900, port: 8143 },
  { label: "phone", width: 390, height: 844, port: 8144 },
];

// Plants a second active SeasonTeamRegistration for an already-registered
// Team directly into the SAME durable SQLite file the running server uses,
// from a separate process (no write contention with the server -- the
// identical technique season-participation.js's own
// injectSecondActiveRegistration uses).
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

// Selects the Hierarchy sub-view via the user-visible toggle and confirms it
// took effect (#345 batch 2: Setup now lands on the six-workflow hub, so a
// journey asserting against Hierarchy controls has to navigate there). Never
// sets setupView directly, and asserts the segment is active BEFORE any
// domain control is awaited, so a future default change fails with "wrong
// sub-view" rather than an opaque selector timeout.
async function enterSetupHierarchy(page) {
  await page.click('[data-setup-view="hierarchy"]');
  await page.waitForFunction(() => {
    const seg = document.querySelector(".setup-viewtoggle .seg.active");
    return !!(seg && seg.dataset.setupView === "hierarchy");
  }, null, { timeout: 10000 });
}

async function checkViewport(browser, viewport) {
  const base = `http://${HOST}:${viewport.port}`;
  // A durable SQLite file (not the in-memory default) so section (5) below
  // can inject a second active registration directly into the same file
  // from a separate process (#331 review round 19, mirroring season-
  // participation.js). Demo mode still boots to a clean slate against it
  // (DemoState.__init__ calls reset(seed=False)), so every scenario above
  // behaves identically to the in-memory store it replaces.
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "hockey-rollover-"));
  const databasePath = path.join(tempDir, `rollover-${viewport.label}.sqlite`);
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
  page.on("console", (m) => { if (m.type() === "error") errors.push(`[console] ${m.text()}`); });

  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });
    await installContextFixture(page);

    // Build three permanent teams, each under its OWN permanent League (#283
    // Slice E rules 2, 3, 7): a Team is created with a REQUIRED league_id and
    // may only ever register into — and be rolled into — that same permanent
    // League. Lions→lgA, Bears→lgB, Wolves→lgC. All three play the source
    // season (registered into their own League). Lions is ALSO pre-registered
    // in the target (under its own permanent League) so the rollover has both
    // an eligible team (Bears) and an already-registered one (Lions, skipped).
    //
    // lgA/lgB are created against the TARGET season first (and given a target
    // Division while each is still bound to a single season — create_division
    // under a League that spans seasons is ambiguous), so Bears' permanent
    // League already participates in the target with a Division to carry into.
    // lgC is created against the SOURCE season ONLY, so Wolves' permanent
    // League does NOT yet participate in the target — its rollover row offers
    // just "No division", and the commit binds lgC's target LeagueSeason on
    // the fly (covers a carried selection whose stored division_id is null).
    // A fourth, league-less season lets the panel refuse a target with no
    // Leagues (review #216, point 3 — re-scoped to League).
    const ids = await page.evaluate(async () => {
      const F = window.hsFixture;
      const program = await F.create("program", "/api/v2/setup/program", { name: "Rollover Program" });
      // #409 EXPLICIT SELECTION, boundary 1: all three Season creates are
      // PROGRAM-AXIS, and minting the Program is not selecting it.
      await F.selectProgram("Program-only bootstrap", program.id);
      const src = await F.create("source season", "/api/v2/setup/season", { program_id: program.id, name: "2026-27" });
      const dst = await F.create("destination season", "/api/v2/setup/season", { program_id: program.id, name: "2027-28" });
      // A fourth season in the same program with NO leagues — the rollover must
      // refuse it as a target (review #216, point 3 — re-scoped to League).
      const empty = await F.create("empty season", "/api/v2/setup/season", { program_id: program.id, name: "2028-29" });
      // BOUNDARY 2. lgA/dstA/lgB/dstB are SEASON-OWNED and land in the
      // DESTINATION season, so the destination is what must be saved for
      // them. This fixture straddles two Seasons deliberately, so each
      // create's Season is stated rather than inherited.
      await F.selectProgramSeason("Program + destination season", program.id, dst.id);
      // Permanent Leagues. lgA/lgB bound to the target first so each can get a
      // target Division before it also spans the source season (below).
      const lgA = await F.create("league lgA", "/api/v2/setup/league", { season_id: dst.id, name: "Adult League" });
      const dstA = await F.create("destination division Gold", "/api/v2/setup/division", { league_id: lgA.id, name: "Gold" });
      const lgB = await F.create("league lgB", "/api/v2/setup/league", { season_id: dst.id, name: "Junior League" });
      const dstB = await F.create("destination division Silver", "/api/v2/setup/division", { league_id: lgB.id, name: "Silver" });
      // lgC lives in the SOURCE season only — no target participation yet.
      // lgC is SEASON-OWNED in the SOURCE season, so the saved Season moves.
      await F.selectProgramSeason("Program + source season", program.id, src.id);
      const lgC = await F.create("league lgC", "/api/v2/setup/league", { season_id: src.id, name: "Youth League" });
      const club = await F.create("club", "/api/v2/setup/club", { name: "Rollover Club" });
      // Each Team is created under its permanent League (league_id REQUIRED).
      const lions = await F.create("lions", "/api/v2/setup/team",
        { league_id: lgA.id, club_id: club.id, name: "Lions" });
      const bears = await F.create("bears", "/api/v2/setup/team",
        { league_id: lgB.id, club_id: club.id, name: "Bears" });
      // Carried in a SEPARATE second rollover round below with only its
      // (fixed, permanent) League and Division left at "No division" — covers
      // a rollover selection whose stored division_id is actually null.
      const wolves = await F.create("wolves", "/api/v2/setup/team",
        { league_id: lgC.id, club_id: club.id, name: "Wolves" });
      // All three teams play the source season, each into its OWN permanent
      // League (rule 7). Registering binds lgA/lgB to the source season too;
      // the source Division is left null (not asserted below).
      // #331 review round 19 finding 4: data-rollover-pick/-league/-div now
      // key off the SOURCE registration's own id (never team_id — two rows
      // for one team must never collide on a shared key), so every
      // selector below needs the created registration's id, not the team's.
      // The three SOURCE registrations are SEASON-OWNED in the source season.
      await F.selectProgramSeason("Program + source season", program.id, src.id);
      const lionsSrcReg = await F.create("Lions' source registration", `/api/v2/setup/seasons/${src.id}/team-registrations`,
        { team_id: lions.id, league_id: lgA.id, division_id: null });
      const bearsSrcReg = await F.create("Bears' source registration", `/api/v2/setup/seasons/${src.id}/team-registrations`,
        { team_id: bears.id, league_id: lgB.id, division_id: null });
      const wolvesSrcReg = await F.create("Wolves' source registration", `/api/v2/setup/seasons/${src.id}/team-registrations`,
        { team_id: wolves.id, league_id: lgC.id, division_id: null });
      // Lions is already in the target season under its OWN permanent League
      // (lgA + Gold) — the rollover must skip it, not duplicate it.
      // Lions' DESTINATION registration lands in the destination season, so
      // the saved Season moves back to it. It also leaves the fixture ON the
      // destination — which is what the roll-forward below requires: for that
      // one surface the production rule is SPLIT (server.py:3649 plus the
      // `"program"` narrowing at service.py:2213-2222), the DESTINATION
      // season must equal the saved Season while the SOURCE is bounded only
      // by the saved Program. Selecting the source here would be refused.
      await F.selectProgramSeason("Program + destination season", program.id, dst.id);
      await F.call("Lions' destination registration",
        `/api/v2/setup/seasons/${dst.id}/team-registrations`,
        { team_id: lions.id, league_id: lgA.id, division_id: dstA.id });
      return { program: program.id, src: src.id, dst: dst.id, empty: empty.id,
        lgA: lgA.id, lgB: lgB.id, lgC: lgC.id,
        dstA: dstA.id, dstB: dstB.id, lions: lions.id, bears: bears.id, wolves: wolves.id,
        lionsSrcReg: lionsSrcReg.id, bearsSrcReg: bearsSrcReg.id,
        wolvesSrcReg: wolvesSrcReg.id };
    });

    // Open Setup and reach the Season rollover panel.
    await page.click('.tab[data-tab="setup"]');
    await enterSetupHierarchy(page);
    await page.waitForFunction(
      () => !!document.querySelector("[data-rollover-from]"), null, { timeout: 15000 });

    // The demo seed may include other multi-season programs, so pick ours
    // explicitly when the program selector is present.
    if (await page.$("[data-rollover-program]")) {
      await page.selectOption("[data-rollover-program]", ids.program);
    }
    await page.selectOption("[data-rollover-from]", ids.src);

    // Point a rollover at the league-less season: the panel blocks it with a
    // clear message and offers no commit path (review #216, point 3).
    await page.selectOption("[data-rollover-to]", ids.empty);
    await page.waitForFunction(
      () => /no leagues yet/i.test(document.querySelector("#content").textContent),
      null, { timeout: 10000 });
    if (await page.$("[data-rollover-commit]")) {
      throw new Error(`[${viewport.label}] a league-less target season still offered a commit control`);
    }

    await page.selectOption("[data-rollover-to]", ids.dst);

    // Preview: Bears is eligible (a pick checkbox), Lions is not offered (it's
    // already registered in the target season). #331 review round 19 finding
    // 4: keyed by each team's SOURCE registration id, not team_id.
    await page.waitForFunction(
      (b) => !!document.querySelector(`[data-rollover-pick="${b}"]`),
      ids.bearsSrcReg, { timeout: 15000 });
    if (await page.$(`[data-rollover-pick="${ids.lionsSrcReg}"]`)) {
      throw new Error(`[${viewport.label}] Lions was offered for rollover despite already being registered`);
    }

    // #283 Slice E: a Team only ever rolls into its OWN permanent League, so
    // Bears' target-League control is FIXED to lgB (a single-option select
    // preselected to the team's permanent League — never a free picker). Assert
    // that fixed value rather than selecting a different League.
    const bearsLeagueSel = await page.$eval(`[data-rollover-league="${ids.bearsSrcReg}"]`,
      (sel) => ({ value: sel.value, optionValues: Array.from(sel.options).map((o) => o.value) }));
    if (bearsLeagueSel.value !== ids.lgB) {
      throw new Error(`[${viewport.label}] Bears' target League wasn't fixed to its permanent League `
        + `lgB: ${JSON.stringify(bearsLeagueSel)}`);
    }
    if (bearsLeagueSel.optionValues.filter(Boolean).some((v) => v !== ids.lgB)) {
      throw new Error(`[${viewport.label}] Bears' League select offered a League other than its `
        + `permanent one: ${JSON.stringify(bearsLeagueSel.optionValues)}`);
    }

    // Commit is disabled before any team is chosen.
    if (!(await page.$eval("[data-rollover-commit]", (b) => b.disabled))) {
      throw new Error(`[${viewport.label}] commit was enabled with nothing selected`);
    }

    // Zero writes so far: the target season still holds only the pre-registered
    // Lions — merely opening the preview writes nothing.
    const beforeCheck = await page.evaluate(async (i) => {
      const get = async (p) => (await fetch(p, { credentials: "same-origin" })).json();
      const regs = (await get(`/api/v2/setup/seasons/${i.dst}/team-registrations`)).registrations;
      return regs.filter((r) => r.active).length;
    }, ids);
    if (beforeCheck !== 1) {
      throw new Error(`[${viewport.label}] target season was written before any commit: ${beforeCheck} active regs`);
    }

    // Check Bears: its League is already fixed to lgB, so the commit gate is
    // satisfied immediately (no unassigned-League row error to clear) and the
    // button enables even with Division left at its optional "No division"
    // default.
    await page.check(`[data-rollover-pick="${ids.bearsSrcReg}"]`);
    await page.waitForFunction(() => {
      const commit = document.querySelector("[data-rollover-commit]");
      return commit && !commit.disabled;
    }, null, { timeout: 10000 });
    // The inline "no permanent league" row error stays hidden — Bears HAS one.
    const bearsRowErrHidden = await page.evaluate((b) => {
      const row = document.querySelector(`[data-rollover-pick="${b}"]`).closest(".reg-row");
      const err = row.querySelector(".ro-row-err");
      return !err || err.hidden;
    }, ids.bearsSrcReg);
    if (!bearsRowErrHidden) {
      throw new Error(`[${viewport.label}] Bears (with a permanent League) showed a row error`);
    }

    // Checking a box still writes nothing before the explicit commit.
    const before = await page.evaluate(async (i) => {
      const get = async (p) => (await fetch(p, { credentials: "same-origin" })).json();
      const regs = (await get(`/api/v2/setup/seasons/${i.dst}/team-registrations`)).registrations;
      return regs.filter((r) => r.active).length;
    }, ids);
    if (before !== 1) {
      throw new Error(`[${viewport.label}] target season was written before a valid commit: ${before} active regs`);
    }

    // Bears' permanent League (lgB) already participates in the target with a
    // Division (Silver), so pick it — commit stays enabled (Division optional).
    await page.selectOption(`[data-rollover-div="${ids.bearsSrcReg}"]`, ids.dstB);
    if (await page.$eval("[data-rollover-commit]", (b) => b.disabled)) {
      throw new Error(`[${viewport.label}] commit disabled after picking an optional division`);
    }

    const resp = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/seasons/${ids.dst}/roll-forward`
      && r.request().method() === "POST");
    await page.click("[data-rollover-commit]");
    if ((await resp).status() !== 200) {
      throw new Error(`[${viewport.label}] roll-forward returned non-200`);
    }
    await page.getByRole("heading", { name: "Rollover complete" }).waitFor();

    // Verify the resulting target-season registrations: Bears now active under
    // the target League in Target B, Lions still active in Target A
    // (untouched), and the permanent Team count in the program is unchanged
    // (no team was copied — Wolves is the 3rd permanent team, created above
    // for the second rollover round below, not carried yet).
    const state = await page.evaluate(async (i) => {
      const get = async (p) => (await fetch(p, { credentials: "same-origin" })).json();
      const regs = (await get(`/api/v2/setup/seasons/${i.dst}/team-registrations`)).registrations;
      const teams = (await get(`/api/v2/setup/programs/${i.program}/teams`)).teams;
      const active = regs.filter((r) => r.active);
      const bears = active.find((r) => r.team_id === i.bears);
      const lions = active.find((r) => r.team_id === i.lions);
      return {
        teamCount: teams.length,
        bearsLeague: bears && bears.league_id,
        bearsDiv: bears && bears.division_id,
        lionsDiv: lions && lions.division_id,
        activeCount: active.length,
      };
    }, ids);
    if (state.teamCount !== 3) {
      throw new Error(`[${viewport.label}] expected 3 permanent teams, got ${state.teamCount}`);
    }
    if (state.bearsLeague !== ids.lgB) {
      throw new Error(`[${viewport.label}] Bears not carried into its permanent League: ${JSON.stringify(state)}`);
    }
    if (state.bearsDiv !== ids.dstB) {
      throw new Error(`[${viewport.label}] Bears not carried into Silver: ${JSON.stringify(state)}`);
    }
    if (state.lionsDiv !== ids.dstA) {
      throw new Error(`[${viewport.label}] Lions' existing registration was altered: ${JSON.stringify(state)}`);
    }
    if (state.activeCount !== 2) {
      throw new Error(`[${viewport.label}] expected 2 active target registrations, got ${state.activeCount}`);
    }

    // (4) Rollover with division_id: null — a SEPARATE second round carries
    // Wolves (still eligible: registered in the source season, not yet in
    // the target) with only a target League chosen; its Division select is
    // left at the default "No division". The picker persists alongside the
    // "Rollover complete" banner from the first commit above, so no reset
    // step is needed before checking Wolves.
    await page.waitForFunction(
      (w) => !!document.querySelector(`[data-rollover-pick="${w}"]`),
      ids.wolvesSrcReg, { timeout: 15000 });
    await page.check(`[data-rollover-pick="${ids.wolvesSrcReg}"]`);
    // Wolves' target League is FIXED to its permanent League (lgC), which does
    // NOT yet participate in the target season — so the row offers only "No
    // division" and the commit binds lgC's target LeagueSeason on the fly.
    const wolvesLeagueSel = await page.$eval(`[data-rollover-league="${ids.wolvesSrcReg}"]`,
      (sel) => sel.value);
    if (wolvesLeagueSel !== ids.lgC) {
      throw new Error(`[${viewport.label}] Wolves' target League wasn't fixed to lgC: ${wolvesLeagueSel}`);
    }
    // Division select is untouched — stays at its "No division" default.
    await page.waitForFunction(() => {
      const commit = document.querySelector("[data-rollover-commit]");
      return commit && !commit.disabled;
    }, null, { timeout: 10000 });
    const resp2 = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/seasons/${ids.dst}/roll-forward`
      && r.request().method() === "POST");
    await page.click("[data-rollover-commit]");
    const resp2Req = (await resp2).request();
    if (resp2Req.postDataJSON) {
      const body2 = resp2Req.postDataJSON();
      const wolvesSel = (body2.selections || []).find((s) => s.team_id === ids.wolves);
      if (!wolvesSel || wolvesSel.division_id !== null) {
        throw new Error(`[${viewport.label}] wolves roll-forward selection division_id: ${JSON.stringify(wolvesSel)}`);
      }
    }
    const wolvesStored = await page.evaluate(async (i) => {
      const get = async (p) => (await fetch(p, { credentials: "same-origin" })).json();
      const regs = (await get(`/api/v2/setup/seasons/${i.dst}/team-registrations`)).registrations;
      return regs.find((r) => r.active && r.team_id === i.wolves);
    }, ids);
    if (!wolvesStored || wolvesStored.division_id !== null || !("division_id" in wolvesStored)) {
      throw new Error(`[${viewport.label}] wolves stored registration division_id wasn't null: ${
        JSON.stringify(wolvesStored)}`);
    }

    // (5) #331 review round 19 finding 4: a Team with TWO simultaneously
    // active SOURCE registrations must render as two genuinely distinct,
    // independently controllable rows -- never colliding on a shared
    // team-id-only key -- and the row actually CHECKED must carry ITS OWN
    // Division into the committed request, not whichever row a global
    // (non-scoped) attribute lookup happened to find first.
    const foxFixture = await page.evaluate(async (i) => {
      const F = window.hsFixture;
      // Foxes' permanent League (lgD) participates in BOTH seasons, with a
      // target Division ("Copper") both of its source rows' Division
      // selects will offer (Division options derive from the TEAM's
      // permanent League, never the individual source row).
      // #409: Copper League and its Division are SEASON-OWNED in the
      // DESTINATION season.
      await F.selectProgramSeason("Program + destination season", i.program, i.dst);
      const lgD = await F.create("Copper League", "/api/v2/setup/league", { season_id: i.dst, name: "Copper League" });
      const dstD = await F.create("destination division Copper", "/api/v2/setup/division", { league_id: lgD.id, name: "Copper" });
      // A second, SOURCE-only League purely to give the stray row a real
      // LeagueSeason to sit at -- never Foxes' own permanent League, so it
      // reads as the same "stray active row under a different League" shape
      // league_scope.py's own docstring names.
      // Stray League and Foxes' real registration are SEASON-OWNED in the
      // SOURCE season.
      await F.selectProgramSeason("Program + source season", i.program, i.src);
      const lgE = await F.create("Stray League", "/api/v2/setup/league", { season_id: i.src, name: "Stray League" });
      const club = await F.create("club", "/api/v2/setup/club", { name: "Fox Club" });
      const foxes = await F.create("foxes", "/api/v2/setup/team",
        { league_id: lgD.id, club_id: club.id, name: "Foxes" });
      const realReg = await F.create("Foxes' real source registration", `/api/v2/setup/seasons/${i.src}/team-registrations`,
        { team_id: foxes.id, league_id: lgD.id, division_id: null });
      return { lgD: lgD.id, dstD: dstD.id, lgE: lgE.id, foxes: foxes.id, realReg: realReg.id };
    }, { program: ids.program, src: ids.src, dst: ids.dst });
    // Planted directly into the SAME durable SQLite file the running server
    // uses -- no current write path can leave this behind for a Team with a
    // permanent League already set (register_team_for_season/
    // assign_season_team_league/transfer_team_to_league all refuse it).
    const strayRegId = injectSecondActiveRegistration(databasePath, {
      seasonId: ids.src, leagueId: foxFixture.lgE, teamId: foxFixture.foxes,
    });

    // The injection happened in a separate process -- re-click the Setup
    // tab to force render() to re-fetch seasonRegs from the server (its
    // onclick calls render() unconditionally, even when already on that
    // tab, exactly like season-participation.js's own refreshSetup).
    await page.click('.tab[data-tab="setup"]');
    await enterSetupHierarchy(page);
    await page.waitForFunction(
      (s) => !!document.querySelector(`[data-rollover-pick="${s}"]`),
      strayRegId, { timeout: 15000 });

    // Both rows exist, are DISTINCT elements, and each names Foxes — the
    // pre-fix team-id keying would collide both onto the SAME
    // data-rollover-pick value.
    const foxRows = await page.evaluate((i) => {
      const realCb = document.querySelector(`[data-rollover-pick="${i.realReg}"]`);
      const strayCb = document.querySelector(`[data-rollover-pick="${i.strayReg}"]`);
      return {
        realText: realCb && realCb.closest(".reg-row").textContent,
        strayText: strayCb && strayCb.closest(".reg-row").textContent,
        sameElement: realCb === strayCb,
      };
    }, { realReg: foxFixture.realReg, strayReg: strayRegId });
    if (!foxRows.realText || !/Foxes/.test(foxRows.realText)) {
      throw new Error(`[${viewport.label}] Foxes' real source-registration row missing/wrong: ${JSON.stringify(foxRows)}`);
    }
    if (!foxRows.strayText || !/Foxes/.test(foxRows.strayText)) {
      throw new Error(`[${viewport.label}] Foxes' stray source-registration row missing/wrong: ${JSON.stringify(foxRows)}`);
    }
    if (foxRows.sameElement) {
      throw new Error(`[${viewport.label}] both Foxes rows resolved to the SAME element/registration id`);
    }

    // #409 SPLIT RULE, second round. The roll-forward writes its
    // registrations into the DESTINATION season, so the destination is what
    // must be SAVED; the source is bounded only by the saved Program. The fox
    // fixture above finished on the SOURCE season (it created Stray League
    // and Foxes' source registration there), so state the destination again
    // before committing — a fixture that left the source selected would be
    // refused here, and that refusal would look like a rollover bug.
    await selectProgramSeason(page, `[${viewport.label}] destination season for the roll-forward`,
      ids.program, ids.dst);

    // Check ONLY the stray row (real interaction, not a JS-internal state
    // poke) and pick "Copper" for ITS OWN Division select — the real row
    // stays unchecked, at its untouched "No division" default.
    await page.check(`[data-rollover-pick="${strayRegId}"]`);
    await page.selectOption(`[data-rollover-div="${strayRegId}"]`, foxFixture.dstD);
    await page.waitForFunction(() => {
      const commit = document.querySelector("[data-rollover-commit]");
      return commit && !commit.disabled;
    }, null, { timeout: 10000 });

    // Keyboard-activate Commit (focus + a real Enter keypress through
    // Playwright's native input pipeline, not a click) — the fix must work
    // for a keyboard-only operator too, not only a mouse click path.
    const foxCommitResp = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/seasons/${ids.dst}/roll-forward`
      && r.request().method() === "POST");
    await page.evaluate(() => {
      document.querySelector("[data-rollover-commit]").focus();
    });
    await page.keyboard.press("Enter");
    const foxCommitResolved = await foxCommitResp;
    if (foxCommitResolved.status() !== 200) {
      throw new Error(`[${viewport.label}] Foxes roll-forward returned non-200`);
    }
    const foxCommitReq = foxCommitResolved.request();
    if (foxCommitReq.postDataJSON) {
      const foxBody = foxCommitReq.postDataJSON();
      if (foxBody.selections.length !== 1) {
        throw new Error(`[${viewport.label}] expected exactly one Foxes selection, got: ${
          JSON.stringify(foxBody.selections)}`);
      }
      const foxSel = foxBody.selections[0];
      if (foxSel.registration_id !== strayRegId || foxSel.division_id !== foxFixture.dstD) {
        throw new Error(`[${viewport.label}] Foxes roll-forward selection named the wrong row/division `
          + `(expected registration_id=${strayRegId}, division_id=${foxFixture.dstD}): ${JSON.stringify(foxSel)}`);
      }
    }

    // The committed target registration carries the CHECKED row's own
    // Division (Copper) — the pre-fix global querySelector would have read
    // the UNCHECKED real row's untouched "No division" select instead,
    // silently persisting division_id: null regardless of what the
    // operator picked.
    const foxTarget = await page.evaluate(async (i) => {
      const get = async (p) => (await fetch(p, { credentials: "same-origin" })).json();
      const regs = (await get(`/api/v2/setup/seasons/${i.dst}/team-registrations`)).registrations;
      return regs.find((r) => r.active && r.team_id === i.foxes);
    }, { dst: ids.dst, foxes: foxFixture.foxes });
    if (!foxTarget || foxTarget.division_id !== foxFixture.dstD) {
      throw new Error(`[${viewport.label}] Foxes' committed target registration division_id wasn't Copper: ${
        JSON.stringify(foxTarget)}`);
    }
    // Foxes' REAL source registration (never checked) is byte-for-byte
    // untouched — the fix must never let the checked stray row's commit
    // bleed into the unrelated real row.
    const foxRealUntouched = await page.evaluate(async (i) => {
      const get = async (p) => (await fetch(p, { credentials: "same-origin" })).json();
      const regs = (await get(`/api/v2/setup/seasons/${i.src}/team-registrations`)).registrations;
      return regs.find((r) => r.id === i.realReg);
    }, { src: ids.src, realReg: foxFixture.realReg });
    if (!foxRealUntouched || !foxRealUntouched.active
        || foxRealUntouched.division_id !== null) {
      throw new Error(`[${viewport.label}] Foxes' real source registration was altered: ${
        JSON.stringify(foxRealUntouched)}`);
    }

    if (errors.length) {
      throw new Error(`[${viewport.label}] console/page errors:\n${errors.join("\n")}`);
    }
    console.log(`[${viewport.label}] OK — eligible team carried forward, already-registered team skipped, `
      + `league-only rollover stored a null division_id, and two source registrations for one team `
      + `rolled forward independently by their own registration id.`);
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
    console.log("Season rollover browser journey passed.");
  } catch (error) {
    console.error("Season rollover browser journey FAILED.");
    console.error(error && error.message ? error.message : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
