// Destructive-action surfaces browser journey (#215 review 4).
//
// Proves the delete/cancel affordances live on their natural operator screens,
// at desktop and 390px:
//   * Club — deleted from the Setup Records view;
//   * an unused future Ice Slot — deleted from the Arena Calendar;
//   * a real scheduler-created draft game — deleted from the Scheduler view;
//   * a committed/published game — offers "Cancel game" and NEVER Delete, and
//     cancelling preserves the fixture.
// Fails on any browser console/page error.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");
const { installContextFixture } = require("./context-fixture.js");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8159 },
  { label: "phone", width: 390, height: 844, port: 8160 },
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

// The ice-slot half of this journey needs a day that satisfies TWO conditions
// at once, and it is the only spec here that does (#387/#389):
//
//   * it must be the day the Arena Calendar is showing, or `.slot-del` never
//     renders and the wait below times out; and
//   * it must be strictly in the future, because `delete_ice_slot` refuses
//     anything at or before its clock ("past slots are history",
//     setup_service.py) — the delete would 404.
//
// This used to be the literal 2026-09-05, which met both conditions only by
// coincidence: the app opened on that same literal, and the date had not yet
// arrived. Both halves of that were a countdown. The app now opens on the real
// current date, so "the day being shown" is TODAY — and a slot today is only
// future for part of the day, which would make the delete pass or fail
// depending on the hour the suite ran. That is exactly the failure class #384
// was about.
//
// So the calendar is stepped one day forward and the journey books there: the
// day is read back from the app's own `calendarDate` (never recomputed from a
// second clock), and tomorrow is strictly future at every hour.

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
    // Tomorrow, by the app's own date arithmetic on the app's own calendar
    // day — see the note above CAL_DAY's retirement.
    const day = await page.evaluate(() => addDays(calendarDate, 1));

    const ids = await page.evaluate(async (d) => {
      const F = window.hsFixture;
      const get = async (p) => (await fetch(p, { credentials: "same-origin" })).json();

      // #409 EXPLICIT SELECTION on the V1 SURFACE. `POST /api/setup/league`
      // mints the PROGRAM (v1 calls it "league"); `POST /api/setup/season` is
      // PROGRAM-AXIS on the body's `league_id` (server.py:3686), behind the
      // same preflight v2 uses (server.py:1160).
      const league = await F.create("v1 league (the Program)", "/api/setup/league", { name: "Surfaces League" });
      await F.selectProgram("Program-only bootstrap", league.id);
      const season = await F.create("season", "/api/setup/season", { league_id: league.id, name: "S1" });
      // The Divisions, the four registrations, the venue-access grant and the
      // Games are SEASON-OWNED and land in THIS Season.
      await F.selectProgramSeason("Program+Season", league.id, season.id);
      const dA = await F.create("division A", "/api/setup/division", { season_id: season.id, name: "Div A" });
      const dB = await F.create("division B", "/api/setup/division", { season_id: season.id, name: "Div B" });
      const venue = await F.create("venue", "/api/setup/venue", { name: "V", league_id: league.id });
      // Game ice eligibility (#233 Slice E) requires the venue to hold active
      // SeasonVenueAccess for the season the game is scheduled in.
      await F.call("season venue-access grant", `/api/v2/setup/seasons/${season.id}/venue-access`, { venue_id: venue.id });
      const rink = await F.create("rink", "/api/setup/rink", { venue_id: venue.id, name: "R" });
      const club = await F.create("club", "/api/setup/club", { name: "Roster Club" });
      // A bare club to delete from the Records view.
      const bareClub = await F.create("bareClub", "/api/setup/club", { name: "Empty Club" });

      const team = async (name) =>
        (await F.create("team", "/api/setup/team", { club_id: club.id, division_id: dA.id, name })).id;
      const teamA = await team("Aces");
      const teamB = await team("Bruins");
      const teamC = await team("Comets");
      const teamD = await team("Ducks");
      await F.call("team registration", `/api/setup/seasons/${season.id}/team-registrations`, { team_id: teamA, division_id: dA.id });
      await F.call("team registration", `/api/setup/seasons/${season.id}/team-registrations`, { team_id: teamB, division_id: dA.id });
      await F.call("team registration", `/api/setup/seasons/${season.id}/team-registrations`, { team_id: teamC, division_id: dB.id });
      await F.call("team registration", `/api/setup/seasons/${season.id}/team-registrations`, { team_id: teamD, division_id: dB.id });

      const pad = (n) => String(n).padStart(2, "0");
      const slot = async (h) => (await F.create("ice-slot", "/api/setup/ice-slot", {
        rink_id: rink.id,
        start_time: `${d}T${pad(h)}:00:00+00:00`,
        end_time: `${d}T${pad(h + 1)}:00:00+00:00`,
        slot_type: "game",
      })).id;
      const slotGame = await slot(18);
      const slotDraft = await slot(20);
      const slotFree = await slot(22);

      // A committed + published game (offers Cancel, never Delete).
      const game = await F.create("game", "/api/setup/game", {
        season_id: season.id, division_id: dA.id,
        home_team_id: teamA, away_team_id: teamB, ice_slot_id: slotGame,
      });
      await F.call("publish", `/api/games/${game.id}/publish`, {});

      // A real scheduler-created draft game on its own slot (offers Delete).
      // #328 review round 5: Commit is bound to the exact preview it
      // reviewed, so a direct API call must Generate first.
      const draftPreview = await post(
        "/api/scheduler/draft", { division_id: dB.id, slot_ids: [slotDraft] });
      await F.call("commit", "/api/scheduler/commit", {
        division_id: dB.id, slot_ids: [slotDraft],
        draft_fingerprint: draftPreview.draft_fingerprint,
      });
      const drafts = await get("/api/scheduler/drafts");
      const draft = (drafts.draft_games || drafts.drafts || []).find((g) => g.division_id === dB.id);

      return {
        league: league.id, bareClub: bareClub.id, slotFree,
        game: game.id, draft: draft && draft.game_id,
      };
    }, day);

    if (!ids.draft) {
      throw new Error(`[${viewport.label}] scheduler commit produced no draft game`);
    }

    // (1) Club delete — Setup Records view.
    await page.click('.tab[data-tab="setup"]');
    await page.click('[data-setup-view="records"]');
    await page.waitForSelector(`[data-del="club"][data-del-id="${ids.bareClub}"]`, { timeout: 15000 });
    await page.click(`[data-del="club"][data-del-id="${ids.bareClub}"]`);
    await page.waitForSelector(".modal.danger [data-del-confirm]", { timeout: 10000 });
    let resp = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/club/${ids.bareClub}/delete` && r.request().method() === "POST");
    await page.click("[data-del-confirm]");
    if ((await resp).status() !== 200) throw new Error(`[${viewport.label}] club delete non-200`);
    await page.waitForSelector(".modal", { state: "detached", timeout: 10000 });

    // (2) Future Ice Slot delete — Arena Calendar, stepped forward one day to
    // the day the fixture booked (the calendar opens on today).
    await page.click('.tab[data-tab="calendar"]');
    await page.waitForSelector('[data-cal="1"]', { timeout: 10000 });
    await page.click('[data-cal="1"]');
    // Assert rather than assume the step landed where the ice is: if the run
    // straddled UTC midnight between the two reads, the board and the fixture
    // would disagree, and this says so instead of timing out below.
    const shown = await page.evaluate(() => calendarDate);
    if (shown !== day) fail(`calendar shows ${shown}, ice was booked on ${day}`);
    const slotDel = `.slot-del[data-del="ice-slot"][data-del-id="${ids.slotFree}"]`;
    await page.waitForSelector(slotDel, { timeout: 15000 });
    await page.click(slotDel);
    await page.waitForSelector(".modal.danger [data-del-confirm]", { timeout: 10000 });
    resp = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/ice-slot/${ids.slotFree}/delete` && r.request().method() === "POST");
    await page.click("[data-del-confirm]");
    if ((await resp).status() !== 200) throw new Error(`[${viewport.label}] slot delete non-200`);
    await page.waitForSelector(".modal", { state: "detached", timeout: 10000 });

    // (3) Committed/published game — Games view offers Cancel, never Delete.
    await page.click('.tab[data-tab="games"]');
    await page.waitForSelector(`[data-games-toggle="${ids.game}"]`, { timeout: 15000 });
    await page.click(`[data-games-toggle="${ids.game}"]`);
    await page.waitForSelector(`[data-game-cancel="${ids.game}"]`, { timeout: 10000 });
    if (await page.$(`[data-del="game"][data-del-id="${ids.game}"]`)) {
      throw new Error(`[${viewport.label}] a committed game offered Delete instead of Cancel`);
    }
    await page.click(`[data-game-cancel="${ids.game}"]`);
    await page.waitForSelector(".modal.danger [data-cancel-game-confirm]", { timeout: 10000 });
    resp = page.waitForResponse((r) =>
      r.url() === `${base}/api/games/${ids.game}/cancel` && r.request().method() === "POST");
    await page.click("[data-cancel-game-confirm]");
    if ((await resp).status() !== 200) throw new Error(`[${viewport.label}] game cancel non-200`);
    await page.waitForSelector(".modal", { state: "detached", timeout: 10000 });

    // (4) Scheduler draft game — Scheduler view offers Delete draft.
    await page.click('.tab[data-tab="scheduler"]');
    const draftDel = `[data-del="game"][data-del-id="${ids.draft}"]`;
    await page.waitForSelector(draftDel, { timeout: 15000 });
    await page.click(draftDel);
    await page.waitForSelector(".modal.danger [data-del-confirm]", { timeout: 10000 });
    resp = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/game/${ids.draft}/delete` && r.request().method() === "POST");
    await page.click("[data-del-confirm]");
    if ((await resp).status() !== 200) throw new Error(`[${viewport.label}] draft delete non-200`);
    await page.waitForSelector(".modal", { state: "detached", timeout: 10000 });

    // Verify the resulting server state.
    const state = await page.evaluate(async (i) => {
      const get = async (p) => (await fetch(p, { credentials: "same-origin" })).json();
      const hierarchy = await get("/api/setup/hierarchy");
      const clubs = (await get("/api/demo/overview")).clubs || [];
      const drafts = await get("/api/scheduler/drafts");
      const draftList = drafts.draft_games || drafts.drafts || [];
      const board = await get(`/api/games/${i.game}/board`).catch(() => null);
      return {
        clubGone: !clubs.some((c) => c.id === i.bareClub),
        draftGone: !draftList.some((g) => g.game_id === i.draft),
        cancelled: board && board.game ? board.game.cancelled : null,
        hierarchyOk: (hierarchy.leagues || []).some((l) => l.id === i.league),
      };
    }, ids);
    if (!state.clubGone) throw new Error(`[${viewport.label}] bare club still present`);
    if (!state.draftGone) throw new Error(`[${viewport.label}] draft game still present`);
    if (state.cancelled !== true) throw new Error(`[${viewport.label}] game not cancelled: ${JSON.stringify(state)}`);

    if (errors.length) {
      throw new Error(`[${viewport.label}] console/page errors:\n${errors.join("\n")}`);
    }
    console.log(`[${viewport.label}] OK — club, slot, draft deleted; committed game cancelled.`);
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
    console.log("Destructive-action surfaces browser journey passed.");
  } catch (error) {
    console.error("Destructive-action surfaces browser journey FAILED.");
    console.error(error && error.message ? error.message : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
