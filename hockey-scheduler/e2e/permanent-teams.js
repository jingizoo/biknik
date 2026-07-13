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
//    must show the display noun, never the internal league/level key), the
//    Season-participation add control, the move/reassign dialog nouns + legacy
//    warnings, and the delete-modal nouns. The Venue→Program link and the
//    Program→org coupling are labelled as temporary legacy v1 relationships, and
//    the Program operator is an "operating organization" (never "facility
//    owner"). The visible trees never expose the internal "level" grouping word,
//    and the unrelated "League Admin" policy role is left untouched. The internal
//    entity keys and the v1 API (POST /api/setup/{league,level}) are unchanged.
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
  // Open a delete-confirm modal for an entity kind from the Setup trees and read
  // its heading back, then dismiss it.
  const delModalTitle = async (kind) => {
    await page.click(`.setup-trees [data-del="${kind}"]`);
    await page.waitForSelector(".modal.danger[role=dialog]", { timeout: 5000 });
    const title = await page.$eval(".modal.danger h2", (h) => h.textContent.trim());
    await page.click(".modal.danger .modal-x");
    await page.waitForFunction(() => !document.querySelector(".modal[role=dialog]"), null, { timeout: 5000 });
    return title;
  };
  // Open a reassignment panel by "kind:parent" from the Setup trees, read its
  // title + any warning, then cancel out.
  const reassignPanel = async (key) => {
    await page.click(`.setup-trees [data-reassign="${key}"]`);
    await page.waitForSelector(".rz-panel", { timeout: 5000 });
    const info = await page.evaluate(() => {
      const p = document.querySelector(".rz-panel");
      return {
        title: (p.querySelector(".ca-title") || {}).textContent || "",
        warn: (p.querySelector(".ca-warn") || {}).textContent || "",
      };
    });
    await page.click(".rz-panel [data-reassign-cancel]");
    await page.waitForFunction(() => !document.querySelector(".rz-panel"), null, { timeout: 5000 });
    return info;
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
    // The empty-operator note must name the parent by the Program-operator role
    // ("operating organization"), never "facility owner" — that role word is
    // for Venue ownership, not Program operation (ADR 0001).
    const programNote = programDrawer.notes.join(" | ");
    if (!/Create an operating organization first/i.test(programNote))
      fail(`Program drawer empty operator note is not "Create an operating organization first" (got ${JSON.stringify(programDrawer.notes)})`);
    if (/facility owner/i.test(programNote))
      fail(`Program drawer operator note still says "facility owner" (got ${JSON.stringify(programDrawer.notes)})`);
    await closeDrawer();

    // 3) Now build a program → season → league(grouping) → division, plus a
    //    club → permanent team, through the v1 API (still POST
    //    /api/setup/{league,season,level,division,club,team}). The team is
    //    created under the umbrella LEAGUE (= Program), not a division (#180).
    //    The grouping + division give the hierarchy real League and Division
    //    nodes so their move/delete dialog nouns can be asserted below.
    const ids = await page.evaluate(async () => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      const league = await post("/api/setup/league", { name: "Permanent League" });
      const season = await post("/api/setup/season", { league_id: league.id, name: "2026-27" });
      const level = await post("/api/setup/level", { season_id: season.id, name: "Diamond" });
      const division = await post("/api/setup/division",
        { season_id: season.id, level_id: level.id, name: "U14" });
      const club = await post("/api/setup/club", { name: "Perma Club" });
      const team = await post("/api/setup/team",
        { club_id: club.id, league_id: league.id, name: "Perma Bruins" });
      return { league: league.id, team: team.id,
               teamOk: !team.error && team.league_id === league.id && !team.division_id,
               structureOk: !level.error && !division.error };
    });
    if (!ids.teamOk) fail(`team not created under league`);
    if (!ids.structureOk) fail(`league grouping / division not created`);

    // 4) With a program present, the Venue drawer's program field is labelled as
    //    an explicitly legacy/transitional link (removed in Slice E) — never the
    //    old "sets the owner" nor a target-model "operating program" phrasing.
    await page.click('[data-setup-view="records"]');
    await page.waitForSelector(".setup-card", { timeout: 10000 });
    const venueDrawer = await openDrawer("venue");
    const venueLabels = venueDrawer.labels.join(" | ");
    if (!/Legacy program grouping/i.test(venueLabels) || !/removed in Slice E/i.test(venueLabels))
      fail(`Venue drawer program field not marked legacy/transitional (got ${JSON.stringify(venueDrawer.labels)})`);
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

    // 6) The visible Setup trees must not expose the internal "level" grouping
    //    noun anywhere — it renders as "League" now (the old "Level"/"Add
    //    level"/"No level" strings are gone). data-* attributes still carry the
    //    frozen key; only user-visible text is checked.
    const treeText = await page.$eval(".setup-trees", (el) => el.textContent);
    if (/\blevel\b/i.test(treeText)) fail(`Setup trees still show the internal "level" grouping noun`);

    // 7) Move/reassign dialog nouns + legacy warnings. The Program's operator
    //    move says "operating organization" (not "facility owner") and warns
    //    that the venue coupling is a temporary legacy v1 rule; moving a Division
    //    targets a "league" (the grouping), never a "level".
    const progMove = await reassignPanel("league:organization");
    if (!/operating organization/i.test(progMove.title))
      fail(`Program move dialog title is not "…operating organization" (got "${progMove.title}")`);
    if (/facility owner/i.test(progMove.title))
      fail(`Program move dialog still says "facility owner"`);
    if (!/legacy v1/i.test(progMove.warn) || !/Slice E/i.test(progMove.warn))
      fail(`Program move warning does not flag the legacy v1 coupling (got "${progMove.warn}")`);
    const divMove = await reassignPanel("division:level");
    if (!/\bleague\b/i.test(divMove.title) || /\blevel\b/i.test(divMove.title))
      fail(`Division move dialog does not target a "league" (got "${divMove.title}")`);

    // 8) Delete-modal nouns: the umbrella deletes a "program"; the grouping
    //    deletes a "league" — never "level".
    const progDel = await delModalTitle("league");
    if (!/Delete this program\?/i.test(progDel)) fail(`umbrella delete modal is not "Delete this program?" (got "${progDel}")`);
    const grpDel = await delModalTitle("level");
    if (!/Delete this league\?/i.test(grpDel) || /Delete this level\?/i.test(grpDel))
      fail(`grouping delete modal is not "Delete this league?" (got "${grpDel}")`);

    // 9) The League Admin *role* is unrelated to the competition-model rename and
    //    must be untouched — its policy label stays exactly "League Admin".
    const roles = await page.evaluate(() =>
      fetch("/api/auth/roles", { credentials: "same-origin" }).then((r) => r.json()));
    const laRole = (roles.roles || []).find((r) => r.id === "league_admin");
    if (!laRole || laRole.label !== "League Admin")
      fail(`League Admin role label changed (got ${JSON.stringify(laRole && laRole.label)})`);

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
