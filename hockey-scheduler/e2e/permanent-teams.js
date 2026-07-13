// Permanent-teams + competition-terminology Setup journey (#180 structure, #233
// Slice B1 labels).
//
// Proves, at desktop and phone widths:
//  - #180: a team is a first-class member of its PROGRAM (a "Permanent program
//    teams" panel), created under the league — not a division — and the
//    Competition tree is structure only (no team children).
//  - #233 B1: the Setup surface shows the canonical hierarchy nouns
//    (Program / League) everywhere the operator reads them — Records-view card
//    titles, create-drawer field labels and titles, empty-select notes (which
//    must show the display noun, never the internal league/level key), and the
//    Season-participation add control. The internal entity keys and the v1 API
//    (POST /api/setup/{league,level}) are unchanged.
// Fails on any browser console/page error.
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
  const tag = viewport.label;
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

  const fail = (msg) => { throw new Error(`[${tag}] ${msg}`); };
  // Open a create drawer by entity key from the Records grid and read it back.
  const openDrawer = async (key) => {
    await page.click(`.setup-card .sc-new[data-drawer="${key}"]`);
    await page.waitForSelector(".drawer[role=dialog]", { timeout: 5000 });
    return page.evaluate(() => {
      const d = document.querySelector(".drawer[role=dialog]");
      return {
        title: (d.querySelector(".drawer-title") || {}).textContent || "",
        labels: [...d.querySelectorAll("label")].map((l) => l.textContent.trim()),
        notes: [...d.querySelectorAll(".drawer-note")].map((n) => n.textContent.trim()),
      };
    });
  };
  const closeDrawer = async () => {
    await page.click(".drawer-foot [data-drawer-close]");
    await page.waitForFunction(() => !document.querySelector(".drawer[role=dialog]"), null, { timeout: 5000 });
  };

  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });
    await page.click('.tab[data-tab="setup"]');
    await page.click('[data-setup-view="records"]');
    await page.waitForSelector(".setup-card", { timeout: 10000 });

    // 1) Records-view card titles use the canonical nouns: the umbrella entity
    //    reads "Programs" and the tier entity reads "Leagues"; the old "Levels"
    //    label is gone.
    const cardTitles = await page.$$eval(".setup-card .sc-title", (els) => els.map((e) => e.textContent.trim()));
    if (!cardTitles.includes("Programs")) fail(`Records grid missing a "Programs" card (got ${JSON.stringify(cardTitles)})`);
    if (!cardTitles.includes("Leagues")) fail(`Records grid missing a "Leagues" card (got ${JSON.stringify(cardTitles)})`);
    if (cardTitles.includes("Levels")) fail(`Records grid still shows the old "Levels" card`);

    // 2) Empty-state drawers: with nothing created yet, a required parent select
    //    is empty and its note must name the parent by its DISPLAY noun, never
    //    the internal key. The Season drawer's Program parent → "program"
    //    (internal key "league"); the Program drawer's operator parent →
    //    "facility owner" (internal key "organization").
    const seasonDrawer = await openDrawer("season");
    const seasonNote = seasonDrawer.notes.join(" | ");
    if (!/Create a program first/i.test(seasonNote))
      fail(`Season drawer empty-select note is not "Create a program first" (got ${JSON.stringify(seasonDrawer.notes)})`);
    if (/\bleague\b/i.test(seasonNote))
      fail(`Season drawer empty-select note leaks the internal "league" key (got ${JSON.stringify(seasonDrawer.notes)})`);
    await closeDrawer();

    const programDrawer = await openDrawer("league");
    if (programDrawer.title.trim() !== "New program")
      fail(`Program drawer title is not "New program" (got "${programDrawer.title}")`);
    const programLabels = programDrawer.labels.join(" | ");
    if (!/Program name/.test(programLabels)) fail(`Program drawer missing "Program name" field`);
    if (!/Operating organization/.test(programLabels)) fail(`Program drawer missing "Operating organization" field`);
    if (!programDrawer.notes.some((n) => /Create a facility owner first/i.test(n)))
      fail(`Program drawer empty operator select note wrong (got ${JSON.stringify(programDrawer.notes)})`);
    await closeDrawer();

    // 3) Now build a program → season → club → permanent team through the v1
    //    API (still POST /api/setup/{league,season,club,team}); the team is
    //    created under the LEAGUE, not a division (#180).
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
    if (!ids.teamOk) fail(`team not created under league`);

    // 4) With a program present, the Venue drawer's operating-program field is
    //    labelled "Operating program (optional)" — not the old "sets the owner".
    await page.click('[data-setup-view="records"]');
    await page.waitForSelector(".setup-card", { timeout: 10000 });
    const venueDrawer = await openDrawer("venue");
    const venueLabels = venueDrawer.labels.join(" | ");
    if (!/Operating program \(optional\)/.test(venueLabels))
      fail(`Venue drawer missing "Operating program (optional)" (got ${JSON.stringify(venueDrawer.labels)})`);
    if (/sets the owner/i.test(venueLabels))
      fail(`Venue drawer still says "sets the owner"`);
    await closeDrawer();

    // 5) Hierarchy view: the #180 permanent-team model and the #233 competition
    //    nouns, including the Season-participation add control.
    await page.click('[data-setup-view="hierarchy"]');
    await page.waitForFunction(
      () => [...document.querySelectorAll(".tree-title")]
        .some((x) => x.textContent.includes("Permanent program teams")),
      null, { timeout: 15000 });

    const checks = await page.evaluate(() => {
      const titles = [...document.querySelectorAll(".tree-title")].map((x) => x.textContent);
      const subs = [...document.querySelectorAll(".tree-sub")].map((x) => x.textContent);
      const compSub = subs.find((s) => s.includes("Program → Season"));
      const body = document.body.textContent;
      return {
        hasPermanentPanel: titles.some((t) => t.includes("Permanent program teams")),
        hasCompetition: titles.some((t) => t.includes("Competition structure")),
        hasParticipation: titles.some((t) => t.includes("Season participation")),
        competitionSaysTeam: !!compSub && /Team\s*$/.test(compSub.trim()),
        competitionUsesNewNouns: !!compSub && compSub.includes("Program → Season → League → Division"),
        bodyHasTeam: body.includes("Perma Bruins"),
        addProgramTeam: body.includes("Add a program team"),
        leaksLeagueTeam: /league team/i.test(body),
        leaksNoLeagues: /No leagues yet/i.test(body),
      };
    });
    if (!checks.hasPermanentPanel) fail(`no "Permanent program teams" panel`);
    if (!checks.hasCompetition || !checks.hasParticipation) fail(`missing Competition/Participation sections`);
    if (checks.competitionSaysTeam) fail(`Competition subtitle still ends in "Team"`);
    if (!checks.competitionUsesNewNouns) fail(`Competition subtitle not "Program → Season → League → Division" (#233)`);
    if (!checks.bodyHasTeam) fail(`permanent team not shown on Setup`);
    if (!checks.addProgramTeam) fail(`Season participation add control is not "Add a program team…"`);
    if (checks.leaksLeagueTeam) fail(`Setup still says "league team" somewhere`);
    if (checks.leaksNoLeagues) fail(`Setup still says "No leagues yet"`);

    if (errors.length) fail(`console/page errors:\n${errors.join("\n")}`);
    console.log(`[${tag}] OK — permanent team under league; Setup uses Program/League nouns end to end.`);
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
    console.log("Permanent-teams + competition-terminology browser journey passed.");
  } catch (error) {
    console.error("Permanent-teams + competition-terminology browser journey FAILED.");
    console.error(error && error.message ? error.message : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
