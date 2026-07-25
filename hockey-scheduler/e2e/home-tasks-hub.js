// Home/Tasks hub setup-progress card (#204/#330).
//
// At desktop and 390px: a League Admin whose Program has nothing configured
// yet lands on the existing Initial Setup wizard (#174) first — dismissing it
// reaches Dashboard, which now leads with a "Continue setup" primary card
// naming the actual next incomplete Setup workflow (league profile/seasons ->
// permanent teams -> season participation/divisions -> clubs/players/staff ->
// venues/rinks/ice -> imports/onboarding), with the other five listed below
// as a non-competing secondary list. The card's primary action deep-links to
// Setup; the card disappears entirely once every workflow reads done.
//
// Fails on any browser console/page error.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8295 },
  { label: "phone", width: 390, height: 844, port: 8296 },
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

// Read the rendered Home/Tasks card into a plain object for assertions.
function cardState(page) {
  return page.evaluate(() => {
    const heading = Array.from(document.querySelectorAll(".dash-card h3"))
      .find((h) => h.textContent === "Continue setup");
    if (!heading) return null;
    const card = heading.closest(".dash-card");
    const primary = card.querySelector(".act.primary");
    const rows = Array.from(card.querySelectorAll(".li")).map((li) => ({
      title: (li.querySelector(".li-title") || {}).textContent || "",
      done: !!li.querySelector(".badge.green"),
    }));
    return {
      nextTitle: (card.querySelector(".na-title") || {}).textContent || "",
      nextDetail: (card.querySelector(".na-sub") || {}).textContent || "",
      primaryLabel: primary ? primary.textContent.trim() : null,
      rows,
    };
  });
}

// Reach Dashboard, dismissing the #174 Initial Setup wizard if it intercepts
// first — it re-evaluates on every fresh page load (including a reload) for
// as long as /api/v2/onboarding/status reports incomplete, so every landing
// point in this journey (not just the first) needs this, not only the start.
async function reachDashboard(page) {
  await page.waitForFunction(
    () => document.body.dataset.view === "onboarding"
      || document.body.dataset.view === "dashboard", null, { timeout: 10000 });
  if (await page.evaluate(() => document.body.dataset.view) === "onboarding") {
    await page.click('[data-onboarding-goto="dashboard"]');
  }
  await page.waitForFunction(
    () => document.body.dataset.view === "dashboard", null, { timeout: 10000 });
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

  const fail = (msg) => { throw new Error(`[${viewport.label}] ${msg}`); };

  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });

    // A clean-slate boot auto-signs in the League Admin persona (#215) and,
    // since nothing is configured, the #174 Initial Setup wizard intercepts
    // Dashboard first — dismiss it once to reach the real Dashboard/hub. The
    // wizard's own async status check races app.js's bootstrap (documented in
    // onboarding-bootstrap.js), so either view can be the first one observed.
    await reachDashboard(page);

    // (1) No Program exists at all yet (clean slate: even the League Admin
    // account has none) — bootstrapping the very first Program is the
    // existing onboarding wizard's job (folds into the "Imports and
    // onboarding" workflow per #330's IA crosswalk), not this card's, so it
    // correctly renders nothing rather than claiming a workflow state that
    // has no Program to be scoped to.
    if (await cardState(page) !== null) {
      fail("expected no setup-progress card before any Program exists");
    }

    // Create the Program (what the onboarding wizard's own "Add program"
    // step does) and reload to observe the hub with a real Program active.
    await page.evaluate(async () => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      await post("/api/v2/setup/program", { name: "Riverside Hockey", country: "US" });
    });
    await page.reload();
    await reachDashboard(page);

    // (2) A Program with nothing else configured -> all six workflows todo,
    // primary action names the first one (league profile and seasons).
    await page.waitForFunction(() => {
      const h = Array.from(document.querySelectorAll(".dash-card h3"))
        .find((x) => x.textContent === "Continue setup");
      return !!h;
    }, null, { timeout: 10000 });
    let s = await cardState(page);
    if (!s) fail("setup-progress card did not render for a fresh, unconfigured Program");
    if (s.nextTitle !== "League profile and seasons") {
      fail(`expected next = "League profile and seasons", got ${JSON.stringify(s)}`);
    }
    if (s.primaryLabel !== "Add Season") {
      fail(`expected primary action "Add Season", got ${JSON.stringify(s)}`);
    }
    if (s.rows.length !== 6 || s.rows.some((r) => r.done)) {
      fail(`expected six todo rows, got ${JSON.stringify(s.rows)}`);
    }

    // (3) The primary action deep-links to Setup.
    await page.click(".dash-card .act.primary");
    await page.waitForFunction(
      () => document.body.dataset.view === "setup", null, { timeout: 10000 });
    await page.click('.tab[data-tab="dashboard"]');
    await page.waitForFunction(
      () => document.body.dataset.view === "dashboard", null, { timeout: 10000 });

    // (4) Build out league/season/team/registration/player/facility data via the
    // documented API — same shape as the backend's own progress computation
    // test — and confirm the card advances then disappears once complete.
    const ids = await page.evaluate(async () => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      const overview = await (await fetch("/api/v2/setup/overview",
        { credentials: "same-origin" })).json();
      const program = overview.programs[0];
      const season = await post("/api/v2/setup/season",
        { program_id: program.id, name: "Fall 2026" });
      const league = await post("/api/v2/setup/league",
        { season_id: season.id, name: "Adult League" });
      const club = await post("/api/v2/setup/club", { name: "Club" });
      const team = await post("/api/v2/setup/team",
        { club_id: club.id, league_id: league.id, name: "Team A" });
      return { programId: program.id, seasonId: season.id, leagueId: league.id,
        teamId: team.id };
    });

    await page.reload();
    await reachDashboard(page);
    // Dashboard shows a loading skeleton before its async data fetch (setup-
    // progress included) resolves — wait for the real card, not that skeleton.
    await page.waitForFunction(() => {
      const h = Array.from(document.querySelectorAll(".dash-card h3"))
        .find((x) => x.textContent === "Continue setup");
      return !!h;
    }, null, { timeout: 10000 });
    s = await cardState(page);
    if (!s || s.nextTitle !== "Season participation and divisions") {
      fail(`after season/league/team: expected next = "Season participation and `
        + `divisions", got ${JSON.stringify(s)}`);
    }
    if (!s.rows.find((r) => r.title === "League profile and seasons" && r.done)
        || !s.rows.find((r) => r.title === "Permanent teams" && r.done)) {
      fail(`after season/league/team: expected first two rows done, got ${JSON.stringify(s.rows)}`);
    }

    await page.evaluate(async (arg) => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      await post(`/api/v2/setup/seasons/${arg.seasonId}/team-registrations`,
        { team_id: arg.teamId, league_id: arg.leagueId });
      await post("/api/v2/setup/player",
        { team_id: arg.teamId, name: "Vince Skater", position: "forward" });
      const venue = await post("/api/v2/setup/venue",
        { name: "Arena", organization_id: null });
      const rink = await post("/api/v2/setup/rink",
        { venue_id: venue.id, name: "Rink 1" });
      await post("/api/v2/setup/ice-slot", {
        rink_id: rink.id, start_time: "2026-09-01T18:30:00+00:00",
        end_time: "2026-09-01T20:00:00+00:00", slot_type: "game" });
      await post(`/api/v2/setup/seasons/${arg.seasonId}/venue-access`,
        { venue_id: venue.id });
    }, ids);

    await page.reload();
    await reachDashboard(page);
    // (5) Every workflow done -> the card renders nothing at all.
    await page.waitForFunction(() => {
      const dashStats = document.querySelector(".dash-stats");
      return !!dashStats;  // Dashboard's own content is present...
    }, null, { timeout: 10000 });
    s = await cardState(page);
    if (s !== null) {
      fail(`expected the card to disappear once all workflows are done, got ${JSON.stringify(s)}`);
    }

    if (errors.length) {
      fail(`console/page errors:\n${errors.join("\n")}`);
    }
    console.log(`[${viewport.label}] OK — Home/Tasks hub card names the next `
      + `incomplete Setup workflow, deep-links to Setup, advances as data is `
      + `added, and disappears once complete.`);
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
    console.log("Home/Tasks hub browser journey passed.");
  } catch (error) {
    console.error("Home/Tasks hub browser journey FAILED.");
    console.error(error && error.message ? error.message : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
