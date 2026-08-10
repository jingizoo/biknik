// Player Home, Guardian Home, and Official Inbox journeys (#204/#330).
//
// These three role-specific landing screens already existed with real
// backing endpoints (/api/me/player-home, /api/me/guardian/home,
// /api/me/assignments) but had ZERO e2e coverage before this — the
// operator-UX requirements package (#324) named that gap explicitly and
// required "a real e2e journey (even a thin one)" for each role as part of
// this slice's own test plan. At desktop and 390px: a scoped Player account
// signs in and lands on Player Home (not the operator Dashboard); a verified
// Guardian link surfaces the linked junior on Guardian Home; a scoped
// Official account with no assignments yet lands on Inbox and sees its named
// empty state. Fails on any browser console/page error.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");
const { installContextFixture } = require("./context-fixture.js");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8297 },
  { label: "phone", width: 390, height: 844, port: 8298 },
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

async function apiPost(page, p, body) {
  return page.evaluate(async (arg) => {
    const r = await fetch(arg.p, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(arg.body),
    });
    return { status: r.status, body: await r.json() };
  }, { p, body });
}

async function loginAs(page, username, password) {
  const res = await apiPost(page, "/api/auth/login", { username, password });
  if (res.status !== 200 || res.body.error) {
    throw new Error(`login as ${username} failed: ${JSON.stringify(res)}`);
  }
}

async function logout(page) {
  await apiPost(page, "/api/auth/logout", {});
}

// Dashboard/Home screens show a loading skeleton before their async data
// fetch resolves — a bare `view === X` wait can observe that skeleton, not
// the real content, so every content assertion below waits for this first.
async function waitForRealContent(page) {
  await page.waitForFunction(
    () => !document.querySelector("#content .skeleton")
      && document.getElementById("content").children.length > 0,
    null, { timeout: 10000 });
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
    await installContextFixture(page);
    await reachDashboard(page);  // signed in as the auto-provisioned League Admin

    // Build one Program/Team/Player and the three scoped accounts as the
    // League Admin, all through the documented v2 setup + accounts API.
    //
    // #409 EXPLICIT SELECTION. Minting the Program does not select it, and
    // the Season create below is PROGRAM-AXIS while the League is
    // SEASON-OWNED; both selections are therefore PERSISTED here in axis
    // order, and every create is asserted at its own line
    // (./context-fixture.js). The Program id is read back off the overview
    // rather than the create response, so the id fed to the selection is the
    // one the server actually holds.
    const ids = await page.evaluate(async () => {
      const F = window.hsFixture;
      await F.create("program", "/api/v2/setup/program",
        { name: "Riverside Hockey", country: "US" });
      const overview = await (await fetch("/api/v2/setup/overview",
        { credentials: "same-origin" })).json();
      const program = overview.programs[0];
      if (!program || !program.id) {
        throw new Error("fixture: the setup overview listed no Program to select");
      }
      await F.selectProgram("Program-only bootstrap", program.id);
      const season = await F.create("season", "/api/v2/setup/season",
        { program_id: program.id, name: "Fall 2026" });
      await F.selectProgramSeason("Program+Season", program.id, season.id);
      const league = await F.create("league", "/api/v2/setup/league",
        { season_id: season.id, name: "Adult League" });
      const club = await F.create("club", "/api/v2/setup/club", { name: "Club" });
      const team = await F.create("team", "/api/v2/setup/team",
        { club_id: club.id, league_id: league.id, name: "Riverside A" });
      const player = await F.create("player Priya", "/api/v2/setup/player",
        { team_id: team.id, name: "Priya Player", position: "forward" });
      const junior = await F.create("player Jamie", "/api/v2/setup/player",
        { team_id: team.id, name: "Jamie Junior", position: "defense" });
      const official = await F.create("official", "/api/v2/setup/official",
        { name: "Ozzy Official" });
      return { teamId: team.id, playerId: player.id, juniorId: junior.id,
        officialId: official.id };
    });

    const suffix = viewport.port;  // keep usernames unique per viewport/port
    const playerAcct = await apiPost(page, "/api/accounts", {
      username: `hub_player_${suffix}`, password: "scoped-account-pw",
      role: "player", scope: { player_id: ids.playerId, team_id: ids.teamId },
    });
    if (playerAcct.status !== 200 || playerAcct.body.error) {
      fail(`player account create failed: ${JSON.stringify(playerAcct)}`);
    }
    const guardianAcct = await apiPost(page, "/api/accounts", {
      username: `hub_guardian_${suffix}`, password: "scoped-account-pw",
      role: "guardian",
    });
    if (guardianAcct.status !== 200 || guardianAcct.body.error) {
      fail(`guardian account create failed: ${JSON.stringify(guardianAcct)}`);
    }
    const link = await apiPost(page, "/api/guardians/links", {
      guardian_user_id: guardianAcct.body.id, player_id: ids.juniorId,
    });
    if (link.status !== 200 || link.body.error) {
      fail(`guardian link create failed: ${JSON.stringify(link)}`);
    }
    const verify = await apiPost(page, `/api/guardians/links/${link.body.id}/verify`,
      { consent_method: "verbal_confirmed" });
    if (verify.status !== 200 || verify.body.error) {
      fail(`guardian link verify failed: ${JSON.stringify(verify)}`);
    }
    const officialAcct = await apiPost(page, "/api/accounts", {
      username: `hub_official_${suffix}`, password: "scoped-account-pw",
      role: "official", scope: { official_id: ids.officialId },
    });
    if (officialAcct.status !== 200 || officialAcct.body.error) {
      fail(`official account create failed: ${JSON.stringify(officialAcct)}`);
    }

    // (1) Player: signs in and lands on Player Home, not Dashboard — real
    // welcome text and the "no upcoming game" empty state confirm the
    // /api/me/player-home payload is actually reaching the page, not just a
    // route change.
    await logout(page);
    await loginAs(page, `hub_player_${suffix}`, "scoped-account-pw");
    // A fresh navigation, not reload(): reload() would carry forward any
    // #ctx= deep-link hash left in the address bar from the prior identity's
    // session (the League Admin's Program context, or another role's), which
    // is a foreign/unauthorized context for this role — restoreContextDeepLink
    // handles that gracefully at the app level (falls back with a toast), but
    // it is not what this journey is testing, so start clean instead.
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForFunction(
      () => document.body.dataset.view === "player_home", null, { timeout: 10000 });
    await waitForRealContent(page);
    let text = await page.evaluate(() =>
      (document.getElementById("content").textContent || "").replace(/\s+/g, " "));
    if (!/Hi, Priya Player/i.test(text)) {
      fail(`player_home: missing welcome text for Priya Player: ${text.slice(0, 200)}`);
    }
    if (!/No upcoming game/i.test(text)) {
      fail(`player_home: missing "no upcoming game" empty state: ${text.slice(0, 200)}`);
    }
    if (!(await page.$('[data-goto="notifications"]'))) {
      fail("player_home: missing Notifications row");
    }

    // (2) Guardian: signs in and lands on Guardian Home, listing the
    // verified junior by name (an unverified link would surface nothing —
    // proving verification, not just the link row, actually gates this).
    await logout(page);
    await loginAs(page, `hub_guardian_${suffix}`, "scoped-account-pw");
    // A fresh navigation, not reload(): reload() would carry forward any
    // #ctx= deep-link hash left in the address bar from the prior identity's
    // session (the League Admin's Program context, or another role's), which
    // is a foreign/unauthorized context for this role — restoreContextDeepLink
    // handles that gracefully at the app level (falls back with a toast), but
    // it is not what this journey is testing, so start clean instead.
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForFunction(
      () => document.body.dataset.view === "guardian_home", null, { timeout: 10000 });
    await waitForRealContent(page);
    text = await page.evaluate(() =>
      (document.getElementById("content").textContent || "").replace(/\s+/g, " "));
    if (!/My Players/i.test(text)) {
      fail(`guardian_home: missing "My Players" header: ${text.slice(0, 200)}`);
    }
    if (!/Jamie Junior/i.test(text)) {
      fail(`guardian_home: missing linked junior Jamie Junior: ${text.slice(0, 200)}`);
    }

    // (3) Official: signs in and lands on Inbox with its own named empty
    // state (no assignment machinery needed for a thin journey — the point
    // is proving the role lands here at all, which had zero prior coverage).
    await logout(page);
    await loginAs(page, `hub_official_${suffix}`, "scoped-account-pw");
    // A fresh navigation, not reload(): reload() would carry forward any
    // #ctx= deep-link hash left in the address bar from the prior identity's
    // session (the League Admin's Program context, or another role's), which
    // is a foreign/unauthorized context for this role — restoreContextDeepLink
    // handles that gracefully at the app level (falls back with a toast), but
    // it is not what this journey is testing, so start clean instead.
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForFunction(
      () => document.body.dataset.view === "inbox", null, { timeout: 10000 });
    await waitForRealContent(page);
    text = await page.evaluate(() =>
      (document.getElementById("content").textContent || "").replace(/\s+/g, " "));
    if (!/No assignments yet/i.test(text)) {
      fail(`inbox: missing "no assignments yet" empty state: ${text.slice(0, 200)}`);
    }

    if (errors.length) {
      fail(`console/page errors:\n${errors.join("\n")}`);
    }
    console.log(`[${viewport.label}] OK — Player, Guardian, and Official each `
      + `sign in and land on their own home screen with real, scoped data.`);
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
    console.log("Role home journeys browser journey passed.");
  } catch (error) {
    console.error("Role home journeys browser journey FAILED.");
    console.error(error && error.message ? error.message : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
