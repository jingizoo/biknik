// Scheduler preview never offers a team two overlapping games (#373).
//
// The reported defect, on the real UI: a Division whose round robin has more
// pairings than the ice can hold, with two rinks free at the SAME time. The
// generator walked slots earliest-first without any team-wide occupancy, so it
// handed one team both 17:00 sheets, rendered them as two committable rows,
// and headlined the preview "2 game(s), 0 conflict(s)" with "Commit as draft"
// enabled. Committing was refused server-side — the gate always held — but the
// operator was shown, and invited to commit, a physically impossible schedule.
//
// At desktop and 390px this journey proves, against the REAL backend (nothing
// stubbed):
//   (1) PREVIEW — the same three teams and two same-time rinks now yield ONE
//       proposed game and a NON-ZERO conflict count, each conflict row naming
//       the team that is already booked. No two proposed rows share a team at
//       overlapping times, so the two conflicting games are never
//       simultaneously committable.
//   (2) COMMIT  — committing that preview persists exactly the one possible
//       game, and the resulting schedule read back from the API contains no
//       team in two places at once.
//   (3) RE-PREVIEW — generating again now that a real Game exists proves the
//       occupancy is read from persisted fixtures too, not only from within
//       one batch: every remaining pairing is reported as a conflict, nothing
//       is proposed, and Commit is disabled.
//
// Fails on any browser console/page error.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");
const { installContextFixture } = require("./context-fixture.js");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
const ICE_DAY = "2026-11-03";
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

async function generateFor(page, divId, waitSelector) {
  await page.selectOption("#sched-div", divId);
  await page.click("[data-sched-generate]");
  await page.waitForSelector(waitSelector, { timeout: 15000 });
}

function previewState(page) {
  return page.evaluate(() => {
    const pv = document.querySelector("#sched-preview");
    if (!pv) return null;
    const commit = document.querySelector("[data-sched-commit]");
    const rows = Array.from(pv.querySelectorAll(".card .li")).map((li) => ({
      title: ((li.querySelector(".li-title") || {}).textContent || "").trim(),
      sub: ((li.querySelector(".li-sub") || {}).textContent || "").trim(),
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
      // The raw proposal behind the render — asserting the DOM alone could
      // pass on a UI that merely hides an impossible row the API still offers.
      preview: schedulerState.preview,
    };
  });
}

// Read the PERSISTED schedule back from the API and report any team booked
// into two overlapping games. The acceptance criterion is about what ends up
// in the database, so this is the assertion that matters most — a tidy preview
// over a broken schedule would be no fix at all.
//
// A committed batch is created as DRAFT games, which /api/overview's
// `schedule` deliberately omits, so this reads /api/scheduler/drafts. That
// endpoint's own `team_double_booked` issue flag is computed across ALL
// non-cancelled games (drafts and published alike), so it is asserted
// alongside an independent overlap computation over the returned rows.
function doubleBookings(page) {
  return page.evaluate(async () => {
    const body = await (await fetch("/api/scheduler/drafts", { credentials: "same-origin" })).json();
    const games = body.draft_games || [];
    const clashes = [];
    for (let i = 0; i < games.length; i++) {
      for (let j = i + 1; j < games.length; j++) {
        const si = new Date(games[i].start_time).getTime();
        const ei = new Date(games[i].end_time).getTime();
        const sj = new Date(games[j].start_time).getTime();
        const ej = new Date(games[j].end_time).getTime();
        if (!(si < ej && sj < ei)) continue;
        const shared = [games[i].home_team_id, games[i].away_team_id]
          .filter((t) => t && [games[j].home_team_id, games[j].away_team_id].includes(t));
        if (shared.length) clashes.push({ a: games[i].game_id, b: games[j].game_id, shared });
      }
    }
    const flagged = games.filter((g) => (g.issues || []).includes("team_double_booked"));
    return { clashes, flagged, gameCount: games.length };
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
  page.on("console", (m) => {
    if (m.type() === "error") errors.push(`[console] ${m.text()}`);
  });

  const fail = (msg) => { throw new Error(`[${viewport.label}] ${msg}`); };

  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });
    await installContextFixture(page);

    // Issue #373's own fixture: three teams (so consecutive round-robin
    // pairings necessarily share a team) and one 17:00 game slot on EACH of
    // two rinks.
    const ids = await page.evaluate(async (day) => {
      const F = window.hsFixture;
      // #409 EXPLICIT SELECTION on the V1 SURFACE. The route family differs;
      // the axis rule does not. `POST /api/setup/league` mints the PROGRAM
      // (v1 calls it "league") and `POST /api/setup/season` is a PROGRAM-AXIS
      // create comparing the body's `league_id` (server.py:3686), guarded by
      // the same `setup_create_context_error` preflight v2 uses
      // (server.py:1160). Minting the Program is not selecting it.
      const league = await F.create("v1 league (the Program)", "/api/setup/league", { name: "Overlap Program" });
      await F.selectProgram("Program-only bootstrap", league.id);
      const season = await F.create("season", "/api/setup/season", { league_id: league.id, name: "2026-27" });
      // The v1 "level" IS the v2 League; it, the Divisions, the registrations
      // and the venue-access grant are all SEASON-OWNED and land in THIS
      // Season, so both axes are persisted before them.
      await F.selectProgramSeason("Program+Season", league.id, season.id);
      const level = await F.create("level (the v2 League)", "/api/setup/level", { season_id: season.id, name: "Bronze League" });
      const division = await F.create("division", "/api/setup/division", {
        season_id: season.id, level_id: level.id, name: "Bronze" });
      const club = await F.create("club", "/api/setup/club", { name: "Club" });
      const team = async (n) =>
        (await F.create("team", "/api/v2/setup/team", { club_id: club.id, league_id: level.id, name: n })).id;
      const gold1 = await team("Gold team 1");
      const gold2 = await team("Gold team 2");
      const redwings = await team("Redwings");
      for (const t of [gold1, gold2, redwings]) {
        await F.call("team-registrations", `/api/setup/seasons/${season.id}/team-registrations`,
                   { team_id: t, division_id: division.id });
      }
      const venue = await F.create("venue", "/api/setup/venue", { name: "Arena", league_id: league.id });
      await F.call("season venue-access grant", `/api/v2/setup/seasons/${season.id}/venue-access`, { venue_id: venue.id });
      const red = await F.create("red", "/api/setup/rink", { venue_id: venue.id, name: "Red Rink" });
      const blue = await F.create("blue", "/api/setup/rink", { venue_id: venue.id, name: "Blue Rink" });
      for (const rink of [red, blue]) {
        await F.call("ice-slot", "/api/setup/ice-slot", {
          rink_id: rink.id, start_time: `${day}T17:00:00+00:00`,
          end_time: `${day}T18:00:00+00:00`, slot_type: "game" });
      }
      return { division: division.id, gold1, gold2, redwings };
    }, ICE_DAY);

    await page.waitForSelector('.tab[data-tab="scheduler"]', { state: "visible", timeout: 10000 });
    await page.click('.tab[data-tab="scheduler"]');
    await page.waitForSelector("#sched-div", { timeout: 10000 });
    await page.waitForFunction(
      (id) => !!document.querySelector(`#sched-div option[value="${id}"]`),
      ids.division, { timeout: 10000 });

    // (1) Preview: one possible game, and the rest REPORTED as conflicts.
    // Before #373 this rendered data-games="2" data-conflicts="1" with the
    // same team on both 17:00 rows.
    await generateFor(page, ids.division, '#sched-preview[data-games="1"]');
    let s = await previewState(page);
    if (s.conflicts === "0") {
      fail(`preview must not report 0 conflicts for an impossible batch: ${s.text}`);
    }
    if (!/1 game\(s\), 2 conflict\(s\)/.test(s.text)) {
      fail(`preview header should name one game and two conflicts: ${s.text}`);
    }
    // Every conflict row must explain itself as a team already being booked —
    // not the generic "no available ice" the pre-fix generator fell back to.
    const conflictRows = s.rows.filter((r) => r.conflict);
    if (conflictRows.length !== 2) {
      fail(`expected 2 rendered conflict rows, got ${JSON.stringify(s.rows)}`);
    }
    for (const row of conflictRows) {
      if (!/already (has|have) an overlapping game/i.test(row.sub)) {
        fail(`conflict row must name the overlapping booking: ${JSON.stringify(row)}`);
      }
    }
    // The machine-readable half the UI renders from, asserted on the wire.
    const unscheduled = (s.preview && s.preview.unscheduled) || [];
    if (!unscheduled.length
        || !unscheduled.every((u) => (u.reason_codes || []).includes("team_overlap"))) {
      fail(`every unscheduled row should carry the team_overlap code: ${JSON.stringify(unscheduled)}`);
    }
    for (const u of unscheduled) {
      const conflicts = u.team_conflicts || [];
      if (!conflicts.length) {
        fail(`unscheduled row missing structured team_conflicts: ${JSON.stringify(u)}`);
      }
      for (const c of conflicts) {
        if (!c.team_id || !c.team_name || !c.conflict_source) {
          fail(`team_conflicts entry must name the team and its source: ${JSON.stringify(c)}`);
        }
        if (![u.home_team_id, u.away_team_id].includes(c.team_id)) {
          fail(`team_conflicts names a team not in the pairing: ${JSON.stringify(u)}`);
        }
      }
    }
    // The two conflicting games are not simultaneously committable: only one
    // proposed row exists at all, and it is the only thing Commit can write.
    const proposed = (s.preview && s.preview.draft_games) || [];
    if (proposed.length !== 1) {
      fail(`exactly one game should be committable: ${JSON.stringify(proposed)}`);
    }
    if (s.commitPresent !== true || s.commitDisabled !== false) {
      fail(`commit should stay enabled for the one possible game: ${JSON.stringify(s)}`);
    }

    // (2) Commit: exactly the reviewed game lands, and nothing impossible is
    // persisted.
    await page.click("[data-sched-commit]");
    await page.waitForFunction(
      () => /Committed 1 draft game\(s\)/
        .test((document.querySelector(".toast-msg") || {}).textContent || ""),
      null, { timeout: 15000 });
    const persisted = await doubleBookings(page);
    if (persisted.gameCount !== 1) {
      fail(`exactly one game should have been persisted, got ${persisted.gameCount}`);
    }
    if (persisted.clashes.length || persisted.flagged.length) {
      fail(`impossible schedule persisted: ${JSON.stringify(persisted)}`);
    }

    // (3) Re-preview against the now-real Game: occupancy is read from
    // persisted fixtures too, so both remaining pairings are conflicts, there
    // is nothing to propose, and Commit is disabled.
    await page.waitForSelector("[data-sched-generate]", { timeout: 10000 });
    await generateFor(page, ids.division, '#sched-preview[data-games="0"]');
    s = await previewState(page);
    if (s.conflicts !== "2") {
      fail(`re-preview should report both remaining pairings as conflicts: ${s.text}`);
    }
    if (s.alreadyScheduled !== "1") {
      fail(`re-preview should name the committed pairing as already scheduled: ${s.text}`);
    }
    if (s.commitDisabled !== true) {
      fail(`commit must be disabled with nothing committable: ${JSON.stringify(s)}`);
    }
    const reUnscheduled = (s.preview && s.preview.unscheduled) || [];
    if (!reUnscheduled.every((u) => (u.reason_codes || []).includes("team_overlap"))) {
      fail(`re-preview conflicts should be team overlaps: ${JSON.stringify(reUnscheduled)}`);
    }
    // ...and each names the PERSISTED game it collides with, not a candidate.
    for (const u of reUnscheduled) {
      const sources = (u.team_conflicts || []).map((c) => c.conflict_source);
      if (!sources.includes("existing_game")) {
        fail(`re-preview conflict should point at the committed Game: ${JSON.stringify(u)}`);
      }
      for (const c of u.team_conflicts || []) {
        if (c.conflict_source === "existing_game" && !c.conflict_game_id) {
          fail(`existing_game conflict must name its Game id: ${JSON.stringify(c)}`);
        }
      }
    }
    const after = await doubleBookings(page);
    if (after.clashes.length || after.flagged.length || after.gameCount !== 1) {
      fail(`re-preview must not have changed the persisted schedule: ${JSON.stringify(after)}`);
    }

    if (errors.length) fail(`browser errors: ${errors.join(" | ")}`);
  } catch (err) {
    if (serverOutput.trim()) {
      console.error(`[${viewport.label}] server output:\n${serverOutput}`);
    }
    throw err;
  } finally {
    await context.close();
    await stopServer(server);
  }
}

(async () => {
  const browser = await chromium.launch();
  try {
    for (const viewport of VIEWPORTS) {
      await checkViewport(browser, viewport);
      console.log(`scheduler team-overlap journey OK (${viewport.label})`);
    }
  } finally {
    await browser.close();
  }
})().catch((err) => {
  console.error(err.message || err);
  process.exit(1);
});
