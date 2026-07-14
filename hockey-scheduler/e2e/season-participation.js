// Season participation browser journey (#180 PR D, cut to v2 canonical #233
// Slice B2b).
//
// At desktop and phone widths, a League Admin builds a permanent program team,
// registers it for two different seasons — each under its own grouping League
// and division — through the Setup "Season participation" panel, confirms
// exactly one permanent Team backs two registrations, then removes it from one
// season and confirms the other season's registration is untouched. Fails on
// any browser console/page error.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");

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

    // Build a permanent program team + two seasons, each with its own grouping
    // League and division, through the canonical v2 API (demo default is
    // League Admin) — the same records an operator would type in. A v2
    // registration's League is REQUIRED, so each season needs one before the
    // panel can register anything into it (#233 Slice C2).
    const ids = await page.evaluate(async () => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      const program = await post("/api/v2/setup/program", { name: "Participation Program" });
      const s1 = await post("/api/v2/setup/season", { program_id: program.id, name: "2026-27" });
      const s2 = await post("/api/v2/setup/season", { program_id: program.id, name: "2027-28" });
      const lg1 = await post("/api/v2/setup/league", { season_id: s1.id, name: "Diamond" });
      const lg2 = await post("/api/v2/setup/league", { season_id: s2.id, name: "Diamond" });
      const dA = await post("/api/v2/setup/division", { league_id: lg1.id, name: "Division A" });
      const dB = await post("/api/v2/setup/division", { league_id: lg2.id, name: "Division B" });
      const club = await post("/api/v2/setup/club", { name: "Participation Club" });
      const team = await post("/api/v2/setup/team",
        { program_id: program.id, club_id: club.id, name: "Perma Lions" });
      return { program: program.id, s1: s1.id, s2: s2.id, lg1: lg1.id, lg2: lg2.id,
        dA: dA.id, dB: dB.id, team: team.id };
    }, );

    // Open Setup → Season participation reflects the fresh, empty seasons.
    // (Navigating to the tab re-renders and re-fetches the registration data;
    // no full reload, which would drop the signed-in session.) The register
    // control lives per League, keyed by the League's id.
    await page.click('.tab[data-tab="setup"]');
    await page.waitForFunction(
      (sel) => !!document.querySelector(sel), `#reg-team-${ids.lg1}`, { timeout: 15000 });

    // Register the permanent team for season 1 / League 1 / Division A. The
    // League select already defaults to lg1 (the section it's under).
    await page.selectOption(`#reg-team-${ids.lg1}`, ids.team);
    await page.selectOption(`#reg-div-add-${ids.lg1}`, ids.dA);
    let resp = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/seasons/${ids.s1}/team-registrations`
      && r.request().method() === "POST");
    await page.click(`[data-reg-add="${ids.lg1}"]`);
    if ((await resp).status() !== 200) throw new Error(`[${viewport.label}] register s1 failed`);

    // Register the SAME team for season 2 / League 2 / Division B. The team is
    // still available for s2 (registering it for s1 doesn't touch s2), so its
    // s2 register control is present.
    await page.waitForFunction(
      (sel) => !!document.querySelector(sel), `#reg-team-${ids.lg2}`, { timeout: 15000 });
    await page.selectOption(`#reg-team-${ids.lg2}`, ids.team);
    await page.selectOption(`#reg-div-add-${ids.lg2}`, ids.dB);
    resp = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/seasons/${ids.s2}/team-registrations`
      && r.request().method() === "POST");
    await page.click(`[data-reg-add="${ids.lg2}"]`);
    if ((await resp).status() !== 200) throw new Error(`[${viewport.label}] register s2 failed`);
    // Now the team is registered for BOTH seasons, so each league shows a
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
      (sel) => !!document.querySelector(sel), `#reg-team-${ids.lg2}`, { timeout: 15000 });

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

    if (errors.length) {
      throw new Error(`[${viewport.label}] console/page errors:\n${errors.join("\n")}`);
    }
    console.log(`[${viewport.label}] OK — one permanent team, two seasons, safe removal.`);
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
