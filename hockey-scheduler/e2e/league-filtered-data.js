// League-filtered Setup hub/landing summaries and the render() generation
// guard (#345/#367, browser layer).
//
// #367's backend work (this same PR) makes get_setup_progress,
// get_demo_overview, get_standings, and get_setup_overview_v2 respect the
// persisted Program/Season/League context instead of being Program/Season-
// only or fully global -- that scoping logic has its own thorough facade-
// level regression matrix (test_league_filtered_setup_progress.py,
// test_league_filtered_dashboard.py, test_league_filtered_standings.py,
// test_league_filtered_overview_v2.py). This file is the browser-layer
// complement: it proves the ACTUAL NEW FRONTEND CODE #367 added -- the six
// Setup workflow landings' client-side League filter (leagueScopedTeams(),
// used by the "teams"/"roster" workflows' summary() functions) and the
// contextRevision-based staleness guard newly added to render() -- behaves
// correctly end to end, not just at the unit level.
//
// Explicitly NOT re-tested here (already covered elsewhere, not duplicated):
//   * The League <select> itself -- persistence, hash, keyboard reach,
//     dual-Season carry-forward, atomic rejection -- all league-context-
//     bar.js's own scope.
//   * Setup hub grid/landing shape, primary-action audit, six-workflow
//     presence -- setup-workflow-hub.js's own scope.
//   * The backend scoping/authorization logic's correctness -- the four
//     test_league_filtered_*.py files above.
//
// Fails on any unexpected browser console/page error. Runs at desktop and
// canonical 390x844 for the core narrowing/invariance checks; the render()
// race-guard check is desktop-only (it exercises timing/JS logic, not
// layout).
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8371 },
  { label: "phone", width: 390, height: 844, port: 8372 },
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

// One Program/Season, two Leagues with DELIBERATELY different Team/Player
// counts -- X: 2 teams / 3 players (2 on X1, 1 on X2), Y: 1 team / 1 player
// -- the exact same numbers test_league_filtered_setup_progress.py's own
// fixture uses, so a reader cross-referencing the two sees the identical
// shape. An operator Organization is linked at Program creation so League
// Admin lands on the real Setup tab instead of the onboarding wizard
// (established convention, see league-context-bar.js's buildCoreFixture).
// A second, otherwise-unused "Distractor" Program is also created: with only
// ONE authorized Program/Season combination, renderContextSwitcher() shows a
// static read-only chip instead of a real <select> (entries.length <= 1) --
// the SAME reason league-context-bar.js's own fixture builds a second
// Program, so #ctx-select actually renders as a selectable dropdown here.
async function buildFixture(page) {
  return page.evaluate(async () => {
    const post = async (p, b) => (await fetch(p, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
    })).json();
    const org = await post("/api/v2/setup/organization", { name: "LF Org" });
    const program = await post("/api/v2/setup/program",
      { name: "LF Program", operator_organization_id: org.id });
    const season = await post("/api/v2/setup/season",
      { program_id: program.id, name: "LF Season" });
    const distractor = await post("/api/v2/setup/program", { name: "LF Distractor" });
    const distractorSeason = await post("/api/v2/setup/season",
      { program_id: distractor.id, name: "Distractor Season" });
    // get_onboarding_status_v2's "every Season needs a League" check is
    // INSTALLATION-WIDE, not Program-scoped -- the distractor Season above
    // (created only so #ctx-select has more than one entry and renders as a
    // real dropdown) would otherwise trip a global blocking onboarding
    // requirement and redirect a fresh League Admin session to the Initial
    // Setup wizard instead of the normal shell.
    await post("/api/v2/setup/league",
      { season_id: distractorSeason.id, name: "Distractor League" });
    const leagueX = await post("/api/v2/setup/league",
      { season_id: season.id, name: "League X" });
    const leagueY = await post("/api/v2/setup/league",
      { season_id: season.id, name: "League Y" });
    const club = await post("/api/v2/setup/club", { name: "LF Club" });
    const teamX1 = await post("/api/v2/setup/team",
      { club_id: club.id, name: "X1", league_id: leagueX.id });
    const teamX2 = await post("/api/v2/setup/team",
      { club_id: club.id, name: "X2", league_id: leagueX.id });
    const teamY1 = await post("/api/v2/setup/team",
      { club_id: club.id, name: "Y1", league_id: leagueY.id });
    // One Division per League -- get_demo_overview's `ov.divisions` is
    // server-side League-scoped (unlike get_setup_overview_v2's `sv`, which
    // is deliberately Program-wide always), so the Dashboard's standings-
    // snapshot card (keyed off `ov.divisions[0]`) genuinely differs by
    // League selection -- the race-guard check below needs this real
    // server-side variance, since a client-side-only filter (like the Setup
    // hub's Teams/Roster narrowing) always reads live, current state
    // regardless of which response happens to land last.
    const divisionX = await post("/api/v2/setup/division",
      { league_id: leagueX.id, name: "Division X" });
    const divisionY = await post("/api/v2/setup/division",
      { league_id: leagueY.id, name: "Division Y" });
    await post("/api/v2/setup/player",
      { team_id: teamX1.id, name: "X1 Player A", position: "forward" });
    await post("/api/v2/setup/player",
      { team_id: teamX1.id, name: "X1 Player B", position: "forward" });
    await post("/api/v2/setup/player",
      { team_id: teamX2.id, name: "X2 Player A", position: "forward" });
    await post("/api/v2/setup/player",
      { team_id: teamY1.id, name: "Y1 Player A", position: "forward" });
    // At least one real registration AND at least one granted venue are
    // required for the server's ready_to_schedule check
    // (get_onboarding_status_v2) to clear -- without them, a fresh League
    // Admin session's one-time route decision (onboarding.js) lands on the
    // Initial Setup wizard instead of the normal dashboard/Setup shell,
    // regardless of anything #367 changed.
    await post(`/api/v2/setup/seasons/${season.id}/team-registrations`,
      { team_id: teamX1.id, league_id: leagueX.id, division_id: divisionX.id });
    await post(`/api/v2/setup/seasons/${season.id}/team-registrations`,
      { team_id: teamY1.id, league_id: leagueY.id, division_id: divisionY.id });
    const venue = await post("/api/v2/setup/venue",
      { name: "LF Venue", organization_id: org.id });
    await post(`/api/v2/setup/seasons/${season.id}/venue-access`,
      { venue_id: venue.id });
    const rink = await post("/api/v2/setup/rink", { venue_id: venue.id, name: "LF Rink" });
    await post("/api/v2/setup/ice-slot", {
      rink_id: rink.id, start_time: "2026-09-01T18:30:00+00:00",
      end_time: "2026-09-01T20:00:00+00:00",
    });
    return {
      program: program.id, season: season.id,
      leagueX: leagueX.id, leagueY: leagueY.id,
      divisionXName: divisionX.name, divisionYName: divisionY.name,
    };
  });
}

async function openSetupHub(page, step) {
  await page.click('.tab[data-tab="setup"]:not([data-setup-workflow-nav])');
  await page.waitForFunction(() => document.body.dataset.view === "setup",
    null, { timeout: 10000 }).catch(async () => fail(
      `[${step}] clicking the Setup tab never reached the setup view (was `
      + `"${await page.evaluate(() => document.body.dataset.view)}")`));
  await page.click('[data-setup-view="hub"]');
  await page.waitForSelector(".swf-grid", { timeout: 10000 }).catch(() => fail(
    `[${step}] the workflow index never rendered after selecting Workflows`));
}

// Reads a hub card's (or a landing's) "N label" stat pairs into a plain
// object keyed by label, e.g. {"Teams": 2, "Clubs": 1}.
async function readStats(page, selector, step) {
  await page.waitForSelector(selector, { timeout: 10000 }).catch(() => fail(
    `[${step}] ${selector} never rendered`));
  return page.evaluate((sel) => {
    const root = document.querySelector(sel);
    if (!root) return null;
    const out = {};
    root.querySelectorAll(".swf-stat").forEach((el) => {
      const strong = el.querySelector("strong");
      const n = strong ? parseInt(strong.textContent.trim(), 10) : NaN;
      const label = (el.textContent || "").replace(strong ? strong.textContent : "", "").trim();
      out[label] = n;
    });
    return out;
  }, selector);
}

async function selectLeague(page, leagueId, step) {
  await page.selectOption("#ctx-league-select", leagueId || "");
  // A switch is fire-and-forget from the <select>'s own onchange (#345
  // contextRevision idiom) -- poll the real HTTP boundary rather than
  // sleeping a guessed interval, same discipline league-context-bar.js uses.
  await page.waitForFunction((v) => {
    const el = document.getElementById("ctx-league-select");
    return el && el.value === (v || "");
  }, leagueId, { timeout: 10000 }).catch(() => fail(
    `[${step}] League select never settled on "${leagueId || "(No League)"}"`));
  // Let the resulting render() (triggered by the switch) actually repaint.
  await page.waitForTimeout(300);
}

// ---------------------------------------------------------------------------
// Core: Setup hub Teams/Roster narrow to the selected League and show the
// union for "No League"; Setup Records stays Program-wide regardless.
// ---------------------------------------------------------------------------
async function checkCore(browser, viewport) {
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
    const f = await buildFixture(page);
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });

    await page.selectOption("#ctx-select", `${f.program}|${f.season}`);
    await page.waitForFunction(() => {
      const s = document.getElementById("ctx-league-select");
      return s && !s.hidden;
    }, null, { timeout: 10000 }).catch(() => fail(`[${L}] League select never appeared`));

    // League X selected: hub card shows X's own counts (2 teams, 3 players).
    await selectLeague(page, f.leagueX, `${L}/select-X`);
    await openSetupHub(page, `${L}/hub-X`);
    let teamsCard = await readStats(page,
      '[data-setup-workflow-card="teams"] .swf-stats', `${L}/hub-X-teams`);
    if (teamsCard["Teams"] !== 2) {
      fail(`[${L}] League X hub "Teams" card expected 2, got ${JSON.stringify(teamsCard)}`);
    }
    let rosterCard = await readStats(page,
      '[data-setup-workflow-card="roster"] .swf-stats', `${L}/hub-X-roster`);
    if (rosterCard["Players"] !== 3) {
      fail(`[${L}] League X hub "Players" card expected 3, got ${JSON.stringify(rosterCard)}`);
    }

    // Same numbers on the individual landing (not just the hub card) --
    // renderSetupWorkflowLanding shares setupSummaryHtml with the hub.
    await page.click('[data-setup-workflow="teams"]');
    await page.waitForSelector(".swf-landing", { timeout: 10000 });
    let teamsLanding = await readStats(page, ".swf-landing .swf-stats", `${L}/landing-X-teams`);
    if (teamsLanding["Teams"] !== 2) {
      fail(`[${L}] League X Teams LANDING expected 2, got ${JSON.stringify(teamsLanding)}`);
    }
    await openSetupHub(page, `${L}/back-to-hub`);

    // Switch to League Y: counts must FLIP (1 team, 1 player), never keep X's.
    await selectLeague(page, f.leagueY, `${L}/select-Y`);
    await openSetupHub(page, `${L}/hub-Y`);
    teamsCard = await readStats(page,
      '[data-setup-workflow-card="teams"] .swf-stats', `${L}/hub-Y-teams`);
    if (teamsCard["Teams"] !== 1) {
      fail(`[${L}] League Y hub "Teams" card expected 1, got ${JSON.stringify(teamsCard)} `
        + "-- switching League must flip the count, not keep League X's");
    }
    rosterCard = await readStats(page,
      '[data-setup-workflow-card="roster"] .swf-stats', `${L}/hub-Y-roster`);
    if (rosterCard["Players"] !== 1) {
      fail(`[${L}] League Y hub "Players" card expected 1, got ${JSON.stringify(rosterCard)}`);
    }

    // "No League": union of both (3 teams, 4 players) -- the broader view.
    await selectLeague(page, "", `${L}/select-none`);
    await openSetupHub(page, `${L}/hub-none`);
    teamsCard = await readStats(page,
      '[data-setup-workflow-card="teams"] .swf-stats', `${L}/hub-none-teams`);
    if (teamsCard["Teams"] !== 3) {
      fail(`[${L}] No-League hub "Teams" card expected the union (3), got `
        + `${JSON.stringify(teamsCard)}`);
    }
    rosterCard = await readStats(page,
      '[data-setup-workflow-card="roster"] .swf-stats', `${L}/hub-none-roster`);
    if (rosterCard["Players"] !== 4) {
      fail(`[${L}] No-League hub "Players" card expected the union (4), got `
        + `${JSON.stringify(rosterCard)}`);
    }

    // Setup Records is a structural/management surface -- deliberately
    // Program-wide, NEVER narrowed by League (see get_setup_overview_v2's own
    // docstring). All 3 teams must be listed regardless of which League (or
    // none) is currently active.
    await page.click('[data-setup-view="records"]');
    await page.waitForSelector(".setup-grid", { timeout: 10000 }).catch(() => fail(
      `[${L}] Records view never rendered`));
    const recordsTeamCountFor = async (leagueId, step) => {
      await selectLeague(page, leagueId, step);
      return page.evaluate(() => {
        const card = Array.from(document.querySelectorAll(".setup-card"))
          .find((c) => (c.querySelector(".sc-title")?.textContent || "").trim() === "Teams");
        if (!card) return null;
        const countEl = card.querySelector(".setup-count");
        return countEl ? parseInt(countEl.textContent.trim(), 10) : null;
      });
    };
    const recordsX = await recordsTeamCountFor(f.leagueX, `${L}/records-X`);
    const recordsY = await recordsTeamCountFor(f.leagueY, `${L}/records-Y`);
    const recordsNone = await recordsTeamCountFor("", `${L}/records-none`);
    if (recordsX === null) {
      fail(`[${L}] could not find a Teams record card to read a count from -- `
        + "selector/markup assumption is stale, update this check");
    }
    if (recordsX !== recordsY || recordsX !== recordsNone) {
      fail(`[${L}] Setup Records' Teams count must be IDENTICAL regardless of `
        + `League selection (Program-wide by design): X=${recordsX} `
        + `Y=${recordsY} none=${recordsNone}`);
    }
    if (recordsX !== 3) {
      fail(`[${L}] Setup Records' Teams count expected 3 (all Teams in the `
        + `Program), got ${recordsX}`);
    }

    if (errors.length) {
      fail(`[${L}] unexpected console/page errors: ${JSON.stringify(errors)}`);
    }
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth
      - document.documentElement.clientWidth);
    if (overflow > 1) {
      fail(`[${L}] horizontal overflow: scrollWidth exceeds clientWidth by ${overflow}px`);
    }
  } finally {
    await context.close();
    await stopServer(server);
    if (process.env.SMOKE_DEBUG) console.error(out);
  }
}

// ---------------------------------------------------------------------------
// Race guard: a DELAYED /api/demo/overview response for League X must never
// overwrite a NEWER League Y render() that already completed -- the
// render()-level contextRevision check #367 added. Deliberately built on the
// DASHBOARD (get_demo_overview), not the Setup hub: get_setup_overview_v2 is
// Program-wide by design (never narrowed by League server-side, see its own
// docstring), so the Setup hub's Teams/Roster counts are filtered entirely
// client-side against the ALWAYS-CURRENT contextOptions.selected.league_id
// -- a stale sv response can never show wrong League-scoped data there,
// because the filter re-reads live state at paint time regardless of which
// response happens to land last. get_demo_overview's `ov.divisions` (and the
// Dashboard's standings-snapshot card, keyed off `ov.divisions[0]`) IS
// genuinely League-scoped server-side, so a stale response really does
// carry a different Division's data -- this is where the guard's protection
// is actually observable. Desktop only (timing/JS logic, not layout).
// ---------------------------------------------------------------------------
async function checkRaceGuard(browser) {
  const viewport = { label: "desktop", width: 1440, height: 900, port: 8373 };
  const base = `http://${HOST}:${viewport.port}`;
  const server = startServer(viewport.port, {});
  let out = "";
  server.stdout.on("data", (d) => { out += d.toString(); });
  server.stderr.on("data", (d) => { out += d.toString(); });
  const { context, page, errors } = await newPage(browser, viewport);
  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    if ((await loginAs(page, "admin")).status !== 200) throw new Error("admin login failed");
    const f = await buildFixture(page);
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });
    await page.selectOption("#ctx-select", `${f.program}|${f.season}`);
    await page.waitForFunction(() => {
      const s = document.getElementById("ctx-league-select");
      return s && !s.hidden;
    }, null, { timeout: 10000 });
    // Let the Program/Season selection's OWN render (no League chosen yet)
    // fully settle before installing the route interception below -- else
    // ITS incidental /api/demo/overview request (not League X's) can race to
    // be "the first one" the interception delays.
    await page.waitForTimeout(500);

    // Delay exactly the FIRST upcoming /api/demo/overview response by
    // 1200ms; every subsequent one is untouched. Critically, this fetches
    // the REAL response FIRST (so it genuinely reflects League X's context
    // at the moment the request was made) and delays only its DELIVERY back
    // to the browser -- delaying the outgoing request instead (e.g. via
    // route.continue() after a timeout) would let it reach the server LATE,
    // long after the League Y switch already committed, and it would then
    // -- correctly, not as a race at all -- come back with Y's data,
    // proving nothing about stale-response handling.
    let delayedOnce = false;
    await page.route("**/api/demo/overview", async (route) => {
      if (!delayedOnce) {
        delayedOnce = true;
        const response = await route.fetch();
        await new Promise((r) => setTimeout(r, 1200));
        await route.fulfill({ response });
        return;
      }
      await route.continue();
    });

    // Select League X FIRST, fully settled (the switch itself, not the
    // resulting render(), which is fire-and-forget from setActiveContext's
    // perspective -- its /api/demo/overview fetch is the one now held by the
    // route interception above, genuinely in flight and not yet resolved).
    await selectLeague(page, f.leagueX, "race/select-X-delayed");
    // Then select League Y while X's fetch is still pending -- its OWN
    // (undelayed) fetch resolves first and repaints before X's does.
    await selectLeague(page, f.leagueY, "race/select-Y-while-X-in-flight");

    // Wait past the delayed response's own arrival time, then assert the
    // FINAL rendered Dashboard reflects League Y's Division (the newer
    // selection) -- never a late-arriving League X repaint.
    await page.waitForTimeout(1600);
    const finalHeading = await page.evaluate(() => {
      const h = Array.from(document.querySelectorAll(".dash-card-head h3"))
        .find((el) => /Standings$/.test(el.textContent || ""));
      return h ? h.textContent : null;
    });
    if (!finalHeading || !finalHeading.includes(f.divisionYName)) {
      fail(`race guard: the DELAYED League X response must never overwrite `
        + `the newer League Y render() -- expected the standings card to `
        + `show "${f.divisionYName}", got ${JSON.stringify(finalHeading)}`);
    }
    if (finalHeading.includes(f.divisionXName)) {
      fail(`race guard: standings card still shows League X's Division `
        + `("${finalHeading}") after switching to League Y -- the stale, `
        + `delayed response overwrote the newer render()`);
    }
    const finalLeagueSelect = await page.evaluate(
      () => document.getElementById("ctx-league-select").value);
    if (finalLeagueSelect !== f.leagueY) {
      fail(`race guard: League select itself drifted -- expected `
        + `"${f.leagueY}", got "${finalLeagueSelect}"`);
    }

    if (errors.length) {
      fail(`race guard: unexpected console/page errors: ${JSON.stringify(errors)}`);
    }
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
    for (const viewport of VIEWPORTS) await checkCore(browser, viewport);
    await checkRaceGuard(browser);
    console.log("League-filtered data browser journey passed.");
  } catch (error) {
    console.error("League-filtered data browser journey FAILED.");
    console.error(error && error.message ? error.message : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
