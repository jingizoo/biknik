// Configurable turnaround from the previous game's END, on the real
// Scheduler UI (#390).
//
// The owner's worked example: *a 2:00-3:30 game followed by a 3:30 game is
// currently allowed*. One of the three compounding defects lives entirely in
// this file's territory and nowhere a unit or HTTP test can reach it — the
// Generate screen never sent a rest/turnaround value at all, so the backend's
// own field defaulted to zero and the other two defects stayed invisible
// behind it. A control that is not rendered, not read, and not transmitted
// enforces nothing, however correct the engine is.
//
// At desktop and 390px, against a two-Team Division whose pair already has
// its first of two meetings on the ice at 14:00-15:30:
//   * (1) DEFAULT (no turnaround) — the outstanding meeting is proposed onto
//     the 15:30 slot, back to back with the game that just ended. That is the
//     defect, still reachable by choice, and it is the anti-vacuity floor for
//     everything below: this fixture CAN place a game.
//   * (2) SIXTY MINUTES — Generate refuses both candidate slots and the row
//     reads "minimum turnaround not met". The request body is captured and
//     asserted to actually carry constraints.min_turnaround_minutes: 60 — a
//     preview that merely LOOKED refused would otherwise pass.
//   * (3) THIRTY MINUTES — the same fixture, a smaller turnaround, and the
//     16:00 slot (30 minutes clear) is proposed while the 15:30 one still is
//     not. Refusal and acceptance are therefore distinguishable, and the
//     boundary is the turnaround rather than the fixture running out of ice.
//   * (4) COMMIT SENDS THE REVIEWED TURNAROUND — committing that preview
//     creates the game, and the captured commit body carries the same 30. If
//     app.js dropped the field here, the backend's own regeneration would run
//     with no turnaround, the fingerprint could not match, and the commit
//     would be refused as preview_stale instead of creating anything.
//   * (5) A VISIBLE DATE — every proposed-preview row and every draft-review
//     row shows the calendar day, not only a clock time. Two proposals on
//     different days at the same time were previously indistinguishable.
//     The matchup title is separately asserted to be UNCHANGED, because the
//     other scheduler journeys compare it with ===.
//
// Fails on any browser console/page error.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
// A Saturday. The rendered date is asserted verbatim, so the weekday has to
// be a fact about this constant rather than about when the suite runs.
const ICE_DAY = "2026-09-05";
// Chromium renders en-GB September as "Sept"; the abbreviation is the
// platform's, not ours, so match both rather than pinning one runtime.
const EXPECTED_DATE = /Sat 5 Sept? 2026/;
const VIEWPORTS = [
  // 8401/8402 are unique across the whole e2e suite — no other journey binds
  // them, so this one can never race a shard-mate for a port.
  { label: "desktop", width: 1440, height: 900, port: 8401 },
  { label: "phone", width: 390, height: 844, port: 8402 },
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

// Read the rendered preview into a plain object for assertions in Node.
function previewState(page) {
  return page.evaluate(() => {
    const pv = document.querySelector("#sched-preview");
    if (!pv) return null;
    const commit = document.querySelector("[data-sched-commit]");
    const rows = Array.from(pv.querySelectorAll(".card .li"))
      .filter((li) => !(li.textContent || "").includes("Already scheduled"))
      .map((li) => ({
        title: ((li.querySelector(".li-title") || {}).textContent || "").trim(),
        sub: ((li.querySelector(".li-sub") || {}).textContent || "").trim(),
        // The date and the clock are read SEPARATELY, so "the row shows a
        // date" cannot be satisfied by a row that merely mentions a number.
        date: ((li.querySelector(".li-date") || {}).textContent || "").trim(),
        time: ((li.querySelector(".li-time") || {}).textContent || "").trim(),
        conflict: !!li.querySelector(".li-sub.conflict"),
      }));
    return {
      games: pv.getAttribute("data-games"),
      conflicts: pv.getAttribute("data-conflicts"),
      alreadyScheduled: pv.getAttribute("data-already-scheduled"),
      commitPresent: !!commit,
      commitDisabled: commit ? commit.disabled : null,
      rows,
      text: pv.textContent.replace(/\s+/g, " ").trim(),
    };
  });
}

// The draft-review card carries no id; its rows are the only ones with a
// per-row publish/discard checkbox, which is a structural fact rather than a
// class name that could drift.
function draftRows(page) {
  return page.evaluate(() => Array.from(document.querySelectorAll(".sched-pick"))
    .map((input) => input.closest(".li"))
    .filter(Boolean)
    .map((li) => ({
      title: ((li.querySelector(".li-title") || {}).textContent || "").trim(),
      date: ((li.querySelector(".li-date") || {}).textContent || "").trim(),
      time: ((li.querySelector(".li-time") || {}).textContent || "").trim(),
    })));
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

  // Capture the REAL request bodies app.js sends, so "the preview refused"
  // can be distinguished from "the turnaround was actually transmitted".
  const draftBodies = [];
  const commitBodies = [];
  await page.route("**/api/scheduler/draft", async (route) => {
    try { draftBodies.push(JSON.parse(route.request().postData() || "{}")); } catch (_) { /* ignore */ }
    await route.continue();
  });
  await page.route("**/api/scheduler/commit", async (route) => {
    try { commitBodies.push(JSON.parse(route.request().postData() || "{}")); } catch (_) { /* ignore */ }
    await route.continue();
  });

  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });

    const ids = await page.evaluate(async (day) => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      const league = await post("/api/setup/league", { name: "Turnaround Program" });
      const season = await post("/api/setup/season",
                                { league_id: league.id, name: "Fall 2026" });
      const level = await post("/api/setup/level",
                               { season_id: season.id, name: "Turnaround League" });
      const div = await post("/api/setup/division", {
        season_id: season.id, level_id: level.id, name: "TurnNorth" });
      const club = await post("/api/setup/club", { name: "Club" });
      const teams = [];
      for (const name of ["Turn Home", "Turn Away"]) {
        const t = await post("/api/v2/setup/team",
                             { club_id: club.id, league_id: level.id, name });
        await post(`/api/setup/seasons/${season.id}/team-registrations`,
                   { team_id: t.id, division_id: div.id });
        teams.push(t.id);
      }
      const venue = await post("/api/setup/venue",
                               { name: "Arena", league_id: league.id });
      // Without an active SeasonVenueAccess grant the league-scoped scheduler
      // filters out every slot as "not assigned to this season".
      await post(`/api/v2/setup/seasons/${season.id}/venue-access`,
                 { venue_id: venue.id });
      const rink = await post("/api/setup/rink",
                              { venue_id: venue.id, name: "Rink 1" });
      const slot = async (from, to) => (await post("/api/setup/ice-slot", {
        rink_id: rink.id, start_time: `${day}T${from}:00+00:00`,
        end_time: `${day}T${to}:00+00:00`, slot_type: "game",
      })).id;
      // The issue's own example: a 2:00-3:30 pm game, already on the ice.
      const priorSlot = await slot("14:00", "15:30");
      const prior = await post("/api/v2/setup/game", {
        season_id: season.id, division_id: div.id, league_id: level.id,
        home_team_id: teams[0], away_team_id: teams[1],
        ice_slot_id: priorSlot, game_type: "regular",
      });
      // Two candidates behind it: back to back, and thirty minutes clear.
      await slot("15:30", "16:00");
      await slot("16:00", "17:00");
      return { div: div.id, prior: prior.game_id || prior.id };
    }, ICE_DAY);

    await page.waitForSelector('.tab[data-tab="scheduler"]', { state: "visible", timeout: 10000 });
    await page.click('.tab[data-tab="scheduler"]');
    await page.waitForSelector("#sched-div", { timeout: 10000 });
    await page.waitForFunction(
      (id) => !!document.querySelector(`#sched-div option[value="${id}"]`),
      ids.div, { timeout: 10000 });

    // The control must actually exist before anything below means anything.
    if (!(await page.$("#sched-turnaround"))) {
      fail("the Scheduler panel has no minimum-turnaround control");
    }

    await page.selectOption("#sched-div", ids.div);
    // Two guaranteed games per team: this Division has exactly two teams, so
    // that is two meetings for the one pair. The REGULAR game above satisfies
    // the first, which is what leaves exactly ONE obligation outstanding for
    // the candidate slots to compete over. (#375 inverted this control from
    // "meetings per opponent" to guaranteed games per team; with T=2 the two
    // spellings coincide, so the fixture's arithmetic is unchanged.)
    // The format is now TWO controls: `#sched-format` chooses between the
    // legacy single round-robin and the guaranteed-games format, and
    // `#sched-games` is a bounded numeric input carrying the number (a list of
    // options could never represent the whole 1..MAX_GAMES_PER_TEAM range the
    // backend accepts). The number input stays disabled until the
    // guaranteed-games format is chosen, so the order below matters.
    await page.selectOption("#sched-format", "games");
    await page.fill("#sched-games", "2");
    await page.dispatchEvent("#sched-games", "change");

    // (1) DEFAULT: no turnaround configured -> the 3:30 slot is proposed
    // behind the 2:00-3:30 game. The defect, reachable by choice, and the
    // proof this fixture can place a game at all.
    await page.click("[data-sched-generate]");
    await page.waitForSelector('#sched-preview[data-games="1"]', { timeout: 15000 });
    let s = await previewState(page);
    if (draftBodies.length !== 1
        || !draftBodies[0].constraints
        || draftBodies[0].constraints.min_turnaround_minutes !== 0) {
      fail(`default: Generate must send constraints.min_turnaround_minutes 0, sent ${JSON.stringify(draftBodies)}`);
    }
    if (s.rows.length !== 1 || s.rows[0].time !== "15:30") {
      fail(`default: expected one 15:30 proposal, got ${JSON.stringify(s.rows)}`);
    }
    // (5a) The DATE, on the proposed row.
    if (!EXPECTED_DATE.test(s.rows[0].date)) {
      fail(`default: proposed row must show its calendar date, got ${JSON.stringify(s.rows[0])}`);
    }
    // The matchup title is what scheduler-already-scheduled.js and
    // scheduler-games-per-team.js compare with ===; the date must not have
    // leaked into it.
    if (s.rows[0].title !== "Turn Home vs Turn Away"
        && s.rows[0].title !== "Turn Away vs Turn Home") {
      fail(`default: matchup title must stay "Home vs Away", got ${JSON.stringify(s.rows[0])}`);
    }

    // (2) SIXTY MINUTES: both candidates refused, with the turnaround named.
    await page.selectOption("#sched-turnaround", "60");
    await page.click("[data-sched-generate]");
    await page.waitForSelector(
      '#sched-preview[data-games="0"][data-conflicts="1"]', { timeout: 15000 });
    s = await previewState(page);
    if (draftBodies.length !== 2
        || !draftBodies[1].constraints
        || draftBodies[1].constraints.min_turnaround_minutes !== 60) {
      fail(`60 minutes: Generate must send constraints.min_turnaround_minutes 60, sent ${JSON.stringify(draftBodies)}`);
    }
    const conflicts = s.rows.filter((r) => r.conflict);
    if (conflicts.length !== 1) {
      fail(`60 minutes: expected exactly one conflict row, got ${JSON.stringify(s.rows)}`);
    }
    if (!/minimum turnaround not met/i.test(conflicts[0].sub)) {
      fail(`60 minutes: the conflict must name the turnaround, got ${JSON.stringify(conflicts[0])}`);
    }

    // (3) THIRTY MINUTES: the 16:00 slot is 30 minutes clear of the 15:30
    // end, so it is proposed -- while the 15:30 slot still is not. Refusal
    // and acceptance are distinguishable on ONE fixture.
    await page.selectOption("#sched-turnaround", "30");
    await page.click("[data-sched-generate]");
    await page.waitForSelector(
      '#sched-preview[data-games="1"][data-conflicts="0"]', { timeout: 15000 });
    s = await previewState(page);
    if (draftBodies.length !== 3
        || draftBodies[2].constraints.min_turnaround_minutes !== 30) {
      fail(`30 minutes: Generate must send constraints.min_turnaround_minutes 30, sent ${JSON.stringify(draftBodies)}`);
    }
    if (s.rows.length !== 1 || s.rows[0].time !== "16:00") {
      fail(`30 minutes: expected the 16:00 slot to be proposed, got ${JSON.stringify(s.rows)}`);
    }
    if (!EXPECTED_DATE.test(s.rows[0].date)) {
      fail(`30 minutes: proposed row must show its calendar date, got ${JSON.stringify(s.rows[0])}`);
    }
    if (s.commitPresent !== true || s.commitDisabled !== false) {
      fail(`30 minutes: commit must be enabled with 1 game: ${JSON.stringify(s)}`);
    }

    // (4) COMMIT sends the reviewed turnaround, and really creates the game.
    await page.click("[data-sched-commit]");
    try {
      await page.waitForFunction(
        () => /Committed 1 draft game\(s\)/.test(document.body.textContent || ""),
        null, { timeout: 15000 });
    } catch (_) {
      // A Commit that dropped the reviewed turnaround regenerates with none,
      // produces a different fingerprint, and is refused as preview_stale.
      // Report what the operator would actually see rather than a bare
      // timeout, since that is the whole diagnosis.
      const toastText = await page.evaluate(() => {
        const root = document.querySelector("#toast-root");
        return (root ? root.textContent : "").replace(/\s+/g, " ").trim();
      });
      fail(`commit never reported success; the live region read: "${toastText}"`);
    }
    if (commitBodies.length !== 1
        || !commitBodies[0].constraints
        || commitBodies[0].constraints.min_turnaround_minutes !== 30) {
      fail(`commit must send constraints.min_turnaround_minutes 30, sent ${JSON.stringify(commitBodies)}`);
    }

    // (5b) The DATE, on the draft-review row the commit just produced.
    await page.waitForFunction(
      () => document.querySelectorAll(".sched-pick").length > 0,
      null, { timeout: 15000 });
    const drafts = await draftRows(page);
    const committed = drafts.filter((r) => /Turn (Home|Away) vs Turn (Home|Away)/.test(r.title));
    if (committed.length !== 1) {
      fail(`draft review: expected one committed draft row, got ${JSON.stringify(drafts)}`);
    }
    if (!EXPECTED_DATE.test(committed[0].date)) {
      fail(`draft review: the row must show its calendar date, got ${JSON.stringify(committed[0])}`);
    }
    if (committed[0].time !== "16:00") {
      fail(`draft review: the row must still show its clock time, got ${JSON.stringify(committed[0])}`);
    }

    if (errors.length) {
      fail(`console/page errors:\n${errors.join("\n")}`);
    }
    console.log(`[${viewport.label}] OK — turnaround control sends min_turnaround_minutes on Generate AND Commit; 60m refuses what 30m accepts; preview and draft-review rows show their date.`);
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
    console.log("Scheduler turnaround browser journey passed.");
  } catch (error) {
    console.error("Scheduler turnaround browser journey FAILED.");
    console.error(error && error.message ? error.message : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
