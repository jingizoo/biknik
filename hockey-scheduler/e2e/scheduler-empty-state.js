// Scheduler empty-state diagnostics when seasonal registrations are missing (#311).
//
// At desktop and 390px, an operator generates a draft for Divisions with varying
// eligible-registration counts and ice availability. The journey verifies the
// four acceptance states from #311 on the real UI:
//   * 0 eligible registrations  -> explanatory empty-state ("no Teams registered"),
//     the eligible-Team count, the selected context, a Setup -> Season
//     participation link, and a DISABLED "Commit as draft" (no invalid commit).
//   * 1 eligible registration   -> "at least two required" empty-state, disabled commit.
//   * 4 eligible + 0 game ice   -> six UNSCHEDULED conflict rows (not a 0/0 empty
//     state), commit still disabled because there is nothing schedulable to commit.
//   * 4 eligible + 6 game slots -> six preview games, commit ENABLED.
// It also proves the corrective path: the empty-state link navigates to Setup.
//
// The draft scheduler resolves teams from active SeasonTeamRegistration rows and
// draws ice from the GLOBAL pool of available game slots (services/scheduler.py),
// so the 0-ice and 6-ice cases share one 4-team Division: generate it once before
// any game slot exists, then create six slots and generate again.
//
// Fails on any browser console/page error.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
// Game-slot times — any distinct available game slots schedule the six pairings
// (the draft runs with no constraints, so no rest/blackout/date filtering).
const ICE_DAY = "2026-09-05";
const ICE_HOURS = [8, 10, 12, 14, 16, 18];
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8281 },
  { label: "phone", width: 390, height: 844, port: 8282 },
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

// Select a Division and Generate, then wait for the preview to reflect THAT draft.
// Each Generate rebuilds #content, so #sched-preview is recreated; waiting on a
// state-specific attribute selector avoids reading a stale preview.
async function generateFor(page, divId, waitSelector) {
  await page.selectOption("#sched-div", divId);
  await page.click("[data-sched-generate]");
  await page.waitForSelector(waitSelector, { timeout: 15000 });
}

// Read the rendered preview into a plain object for assertions in Node.
function previewState(page) {
  return page.evaluate(() => {
    const pv = document.querySelector("#sched-preview");
    if (!pv) return null;
    const commit = document.querySelector("[data-sched-commit]");
    const link = pv.querySelector('[data-goto="setup"]');
    return {
      teamCount: pv.getAttribute("data-team-count"),
      games: pv.getAttribute("data-games"),
      conflicts: pv.getAttribute("data-conflicts"),
      notEnough: pv.getAttribute("data-not-enough-teams"),
      commitPresent: !!commit,
      commitDisabled: commit ? commit.disabled : null,
      hasSeasonLink: !!link,
      linkText: link ? link.textContent.replace(/\s+/g, " ").trim() : null,
      conflictRows: pv.querySelectorAll(".li-sub.conflict").length,
      liRows: pv.querySelectorAll(".card .li").length,
      text: pv.textContent.replace(/\s+/g, " ").trim(),
    };
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

  const fail = (msg) => { throw new Error(`[${viewport.label}] ${msg}`); };

  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });

    // Build one League with three Divisions: 0-team, 1-team, and 4-team. Create a
    // rink but NO game ice yet — the 4-team Division is first generated with an
    // empty ice pool (six unscheduled), then again after six slots are added.
    const ids = await page.evaluate(async () => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      const league = await post("/api/setup/league", { name: "AHL Program" });
      const season = await post("/api/setup/season", { league_id: league.id, name: "Fall 2026" });
      const level = await post("/api/setup/level", { season_id: season.id, name: "Silver League" });
      const dEmpty = await post("/api/setup/division", { season_id: season.id, level_id: level.id, name: "SilverEmpty" });
      const dSolo = await post("/api/setup/division", { season_id: season.id, level_id: level.id, name: "SilverSolo" });
      const dQuad = await post("/api/setup/division", { season_id: season.id, level_id: level.id, name: "SilverNorth" });
      const club = await post("/api/setup/club", { name: "Club" });
      // Permanent Teams belong to the competition League (level); season/division
      // placement is a separate SeasonTeamRegistration below.
      const team = async (n) =>
        (await post("/api/v2/setup/team", { club_id: club.id, league_id: level.id, name: n })).id;
      const solo = await team("Solo");
      const q1 = await team("Quad 1");
      const q2 = await team("Quad 2");
      const q3 = await team("Quad 3");
      const q4 = await team("Quad 4");
      const register = (teamId, divisionId) => post(
        `/api/setup/seasons/${season.id}/team-registrations`,
        { team_id: teamId, division_id: divisionId });
      await register(solo, dSolo.id);            // one eligible team
      await register(q1, dQuad.id);              // four eligible teams
      await register(q2, dQuad.id);
      await register(q3, dQuad.id);
      await register(q4, dQuad.id);
      // dEmpty gets no registrations at all.
      const venue = await post("/api/setup/venue", { name: "Arena", league_id: league.id });
      // The draft draws ice only from slots whose Venue holds active
      // SeasonVenueAccess for the Season (league_scoped_scheduler); without this
      // grant every slot is filtered out as "not assigned to this season", so
      // scenario (4) could never schedule. Granting it now is harmless to the
      // no-ice case (3), which still has zero slots when it generates.
      await post(`/api/v2/setup/seasons/${season.id}/venue-access`, { venue_id: venue.id });
      const rink = await post("/api/setup/rink", { venue_id: venue.id, name: "Rink 1" });
      return { dEmpty: dEmpty.id, dSolo: dSolo.id, dQuad: dQuad.id, rink: rink.id };
    });

    // Navigate to the Scheduler (operator-only; the tab unhides once the session
    // has manage_schedule) and wait for the freshly-loaded overview to list our
    // Divisions in the picker.
    await page.waitForSelector('.tab[data-tab="scheduler"]', { state: "visible", timeout: 10000 });
    await page.click('.tab[data-tab="scheduler"]');
    await page.waitForSelector("#sched-div", { timeout: 10000 });
    await page.waitForFunction(
      (id) => !!document.querySelector(`#sched-div option[value="${id}"]`),
      ids.dQuad, { timeout: 10000 });

    // (1) Zero eligible registrations -> explanatory empty-state, disabled commit.
    await generateFor(page, ids.dEmpty,
      '#sched-preview[data-not-enough-teams="1"][data-team-count="0"]');
    let s = await previewState(page);
    if (s.games !== "0" || s.commitDisabled !== true) {
      fail(`0-team: expected 0 games + disabled commit, got ${JSON.stringify(s)}`);
    }
    if (!/No Teams are registered/i.test(s.text)) {
      fail(`0-team: missing "no Teams registered" explanation: ${s.text}`);
    }
    if (!/eligible Team/i.test(s.text) || !/actively registered/i.test(s.text)) {
      fail(`0-team: missing eligible-count / active-registration wording: ${s.text}`);
    }
    if (!s.text.includes("SilverEmpty")) {
      fail(`0-team: selected context (division name) not surfaced: ${s.text}`);
    }
    if (!s.hasSeasonLink || !/Season participation/i.test(s.linkText || "")) {
      fail(`0-team: missing Setup -> Season participation link: ${JSON.stringify(s)}`);
    }

    // (2) One eligible registration -> "at least two required" empty-state.
    await generateFor(page, ids.dSolo,
      '#sched-preview[data-not-enough-teams="1"][data-team-count="1"]');
    s = await previewState(page);
    if (s.games !== "0" || s.commitDisabled !== true) {
      fail(`1-team: expected 0 games + disabled commit, got ${JSON.stringify(s)}`);
    }
    if (!/at least two/i.test(s.text) || !/Only one Team/i.test(s.text)) {
      fail(`1-team: missing "at least two required" explanation: ${s.text}`);
    }
    if (!s.text.includes("SilverSolo") || !s.hasSeasonLink) {
      fail(`1-team: missing context or Season participation link: ${JSON.stringify(s)}`);
    }

    // (3) Four eligible + no game ice -> six unscheduled conflict rows, NOT a 0/0
    // empty-state. Commit stays disabled because nothing is schedulable.
    await generateFor(page, ids.dQuad,
      '#sched-preview[data-not-enough-teams="0"][data-team-count="4"][data-games="0"][data-conflicts="6"]');
    s = await previewState(page);
    if (s.notEnough !== "0") {
      fail(`4-team/no-ice: must not render the empty-state (data-not-enough-teams): ${JSON.stringify(s)}`);
    }
    if (s.conflictRows !== 6 || s.liRows !== 6) {
      fail(`4-team/no-ice: expected six conflict rows, got ${JSON.stringify(s)}`);
    }
    if (s.commitDisabled !== true) {
      fail(`4-team/no-ice: commit must be disabled with zero schedulable games: ${JSON.stringify(s)}`);
    }
    if (!/0 game\(s\), 6 conflict\(s\)/.test(s.text)) {
      fail(`4-team/no-ice: header should read 0 games / 6 conflicts: ${s.text}`);
    }

    // Add six game ice slots, then regenerate the same Division.
    await page.evaluate(async (arg) => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      for (const h of arg.hours) {
        const hh = String(h).padStart(2, "0");
        const eh = String(h + 1).padStart(2, "0");
        await post("/api/setup/ice-slot", {
          rink_id: arg.rink,
          start_time: `${arg.day}T${hh}:00:00+00:00`,
          end_time: `${arg.day}T${eh}:00:00+00:00`,
          slot_type: "game",
        });
      }
    }, { rink: ids.rink, day: ICE_DAY, hours: ICE_HOURS });

    // (4) Four eligible + six game slots -> six preview games, commit ENABLED.
    await generateFor(page, ids.dQuad,
      '#sched-preview[data-not-enough-teams="0"][data-team-count="4"][data-games="6"][data-conflicts="0"]');
    s = await previewState(page);
    if (s.conflictRows !== 0 || s.liRows !== 6) {
      fail(`4-team/ice: expected six game rows and no conflicts, got ${JSON.stringify(s)}`);
    }
    if (s.commitPresent !== true || s.commitDisabled !== false) {
      fail(`4-team/ice: commit must be present and enabled with six games: ${JSON.stringify(s)}`);
    }
    if (!/6 game\(s\), 0 conflict\(s\)/.test(s.text)) {
      fail(`4-team/ice: header should read 6 games / 0 conflicts: ${s.text}`);
    }

    // (5) Corrective path works: the empty-state link navigates to Setup. The
    // zero-team Division still resolves to the empty-state even with ice present
    // (team_count < 2 short-circuits before ice assignment).
    await generateFor(page, ids.dEmpty,
      '#sched-preview[data-not-enough-teams="1"][data-team-count="0"]');
    await page.click('#sched-preview [data-goto="setup"]');
    await page.waitForFunction(() => document.body.dataset.view === "setup", null, { timeout: 10000 });

    if (errors.length) {
      fail(`console/page errors:\n${errors.join("\n")}`);
    }
    console.log(`[${viewport.label}] OK — empty-state surfaces eligible-Team count, context, and Season-participation link; commit gated on schedulable games; no-ice stays visible as conflicts.`);
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
    console.log("Scheduler empty-state browser journey passed.");
  } catch (error) {
    console.error("Scheduler empty-state browser journey FAILED.");
    console.error(error && error.message ? error.message : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
