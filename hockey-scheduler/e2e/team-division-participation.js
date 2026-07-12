// One permanent Team across two Seasons + registration-based scheduling (#180).
//
// At desktop and 390px, a League Admin has one permanent Team registered in two
// Seasons in DIFFERENT Divisions. The journey verifies:
//   * Season participation shows the single Team in each Season's own Division;
//   * the Arena Calendar scheduling wizard offers that Team only in a Division it
//     is REGISTERED in (via SeasonTeamRegistration), never via the legacy
//     Team.division_id — a division it isn't registered in doesn't list it.
// Fails on any browser console/page error.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
// The Arena Calendar is pinned to this demo date in the app.
const CAL_DAY = "2026-09-05";
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8161 },
  { label: "phone", width: 390, height: 844, port: 8162 },
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

  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });

    const ids = await page.evaluate(async (day) => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      const league = await post("/api/setup/league", { name: "Perm League" });
      const s1 = await post("/api/setup/season", { league_id: league.id, name: "2026" });
      const s2 = await post("/api/setup/season", { league_id: league.id, name: "2027" });
      const d1 = await post("/api/setup/division", { season_id: s1.id, name: "S1 Div One" });
      const d1b = await post("/api/setup/division", { season_id: s1.id, name: "S1 Div Two" });
      const d2 = await post("/api/setup/division", { season_id: s2.id, name: "S2 Div One" });
      const venue = await post("/api/setup/venue", { name: "V", league_id: league.id });
      const rink = await post("/api/setup/rink", { venue_id: venue.id, name: "R" });
      const club = await post("/api/setup/club", { name: "Club" });
      // #180: create the permanent Team under its LEAGUE (no division); its
      // season/division placement is set separately via registration below.
      const team = async (n) =>
        (await post("/api/setup/team", { club_id: club.id, league_id: league.id, name: n })).id;
      const perma = await team("Perma");   // the one permanent team we track
      const mateA = await team("Mate A");
      const mateB = await team("Mate B");
      const otherD = await team("Other D");
      // Perma plays d1 in season 1 and d2 in season 2 (different divisions);
      // it is NOT registered in d1b.
      await post(`/api/setup/seasons/${s1.id}/team-registrations`, { team_id: perma, division_id: d1.id });
      await post(`/api/setup/seasons/${s1.id}/team-registrations`, { team_id: mateA, division_id: d1.id });
      await post(`/api/setup/seasons/${s2.id}/team-registrations`, { team_id: perma, division_id: d2.id });
      await post(`/api/setup/seasons/${s2.id}/team-registrations`, { team_id: mateB, division_id: d2.id });
      await post(`/api/setup/seasons/${s1.id}/team-registrations`, { team_id: otherD, division_id: d1b.id });
      const slot = await post("/api/setup/ice-slot", {
        rink_id: rink.id, start_time: `${day}T18:00:00+00:00`,
        end_time: `${day}T19:00:00+00:00`, slot_type: "game" });
      return { league: league.id, s1: s1.id, s2: s2.id,
        d1: d1.id, d1b: d1b.id, d2: d2.id, perma, slot: slot.id };
    }, CAL_DAY);

    // (A) Season participation: one permanent Team, two seasons, two divisions.
    const part = await page.evaluate(async (i) => {
      const get = async (p) => (await fetch(p, { credentials: "same-origin" })).json();
      const teams = (await get(`/api/setup/leagues/${i.league}/teams`)).teams;
      const r1 = (await get(`/api/setup/seasons/${i.s1}/team-registrations`)).registrations;
      const r2 = (await get(`/api/setup/seasons/${i.s2}/team-registrations`)).registrations;
      const permaCount = teams.filter((t) => t.id === i.perma).length;
      const in1 = r1.find((r) => r.team_id === i.perma && r.active);
      const in2 = r2.find((r) => r.team_id === i.perma && r.active);
      return { permaCount, div1: in1 && in1.division_id, div2: in2 && in2.division_id };
    }, ids);
    if (part.permaCount !== 1) {
      throw new Error(`[${viewport.label}] expected exactly one permanent Team, got ${part.permaCount}`);
    }
    if (part.div1 !== ids.d1 || part.div2 !== ids.d2) {
      throw new Error(`[${viewport.label}] Perma resolved wrong per-season divisions: ${JSON.stringify(part)}`);
    }

    // (B) Scheduling wizard filters by registration, not legacy division.
    await page.click('.tab[data-tab="calendar"]');
    await page.waitForSelector(`[data-slot="${ids.slot}"]`, { timeout: 15000 });
    await page.click(`[data-slot="${ids.slot}"]`);
    await page.waitForSelector("#w-div", { timeout: 10000 });

    const homeOptions = async () => page.$$eval(
      "#w-home option", (opts) => opts.map((o) => o.value));

    // In d1, Perma IS registered → offered.
    await page.selectOption("#w-div", ids.d1);
    await page.waitForFunction(
      (p) => Array.from(document.querySelectorAll("#w-home option"))
        .some((o) => o.value === p), ids.perma, { timeout: 10000 });

    // In d1b, Perma is NOT registered → must not be offered (legacy division_id
    // would have wrongly listed it; registrations do not).
    await page.selectOption("#w-div", ids.d1b);
    await page.waitForFunction(
      (p) => !Array.from(document.querySelectorAll("#w-home option"))
        .some((o) => o.value === p), ids.perma, { timeout: 10000 });
    const inD1b = await homeOptions();
    if (inD1b.includes(ids.perma)) {
      throw new Error(`[${viewport.label}] Perma was offered in a division it isn't registered in`);
    }

    if (errors.length) {
      throw new Error(`[${viewport.label}] console/page errors:\n${errors.join("\n")}`);
    }
    console.log(`[${viewport.label}] OK — one team across two seasons; wizard filters by registration.`);
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
    console.log("Team/division participation browser journey passed.");
  } catch (error) {
    console.error("Team/division participation browser journey FAILED.");
    console.error(error && error.message ? error.message : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
