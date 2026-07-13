// Permanent-teams Setup structure journey (#180 UI correction).
//
// Proves the corrected Setup model at desktop and phone widths: a team is a
// first-class member of its PROGRAM (a "Permanent program teams" panel), created
// under the league — not a division — and the Competition tree is structure
// only (its subtitle no longer ends in "Team", and divisions carry no team
// children). Fails on any browser console/page error.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8143 },
  { label: "phone", width: 390, height: 844, port: 8144 },
];

function waitForServer(url, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const attempt = () => {
      const request = http.get(url, (r) => { r.resume(); resolve(); });
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

    // Create a permanent team under the LEAGUE (no division), the #180-correct
    // path — the route accepts league_id directly.
    const ids = await page.evaluate(async () => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      const league = await post("/api/setup/league", { name: "Permanent League" });
      await post("/api/setup/season", { league_id: league.id, name: "2026-27" });
      const club = await post("/api/setup/club", { name: "Perma Club" });
      const team = await post("/api/setup/team",
        { club_id: club.id, league_id: league.id, name: "Perma Bruins" });
      return { league: league.id, team: team.id,
               teamOk: !team.error && team.league_id === league.id && !team.division_id };
    });
    if (!ids.teamOk) throw new Error(`[${viewport.label}] team not created under league`);

    await page.click('.tab[data-tab="setup"]');
    await page.waitForFunction(
      () => [...document.querySelectorAll(".tree-title")]
        .some((x) => x.textContent.includes("Permanent program teams")),
      null, { timeout: 15000 });

    const checks = await page.evaluate(() => {
      const titles = [...document.querySelectorAll(".tree-title")].map((x) => x.textContent);
      const subs = [...document.querySelectorAll(".tree-sub")].map((x) => x.textContent);
      const compSub = subs.find((s) => s.includes("Program → Season"));
      return {
        hasPermanentPanel: titles.some((t) => t.includes("Permanent program teams")),
        hasCompetition: titles.some((t) => t.includes("Competition structure")),
        hasParticipation: titles.some((t) => t.includes("Season participation")),
        competitionSaysTeam: !!compSub && /Team\s*$/.test(compSub.trim()),
        // #233 Slice B1: competition subtitle uses the new hierarchy nouns.
        competitionUsesNewNouns: !!compSub && compSub.includes("Program → Season → League → Division"),
        bodyHasTeam: document.body.textContent.includes("Perma Bruins"),
      };
    });
    if (!checks.hasPermanentPanel) throw new Error(`[${viewport.label}] no "Permanent program teams" panel`);
    if (!checks.hasCompetition || !checks.hasParticipation)
      throw new Error(`[${viewport.label}] missing Competition/Participation sections`);
    if (checks.competitionSaysTeam)
      throw new Error(`[${viewport.label}] Competition subtitle still ends in "Team"`);
    if (!checks.competitionUsesNewNouns)
      throw new Error(`[${viewport.label}] Competition subtitle not "Program → Season → League → Division" (#233)`);
    if (!checks.bodyHasTeam)
      throw new Error(`[${viewport.label}] permanent team not shown on Setup`);

    if (errors.length) throw new Error(`[${viewport.label}] console/page errors:\n${errors.join("\n")}`);
    console.log(`[${viewport.label}] OK — permanent team under league; competition is structure-only.`);
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
    console.log("Permanent-teams structure browser journey passed.");
  } catch (error) {
    console.error("Permanent-teams structure browser journey FAILED.");
    console.error(error && error.message ? error.message : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
