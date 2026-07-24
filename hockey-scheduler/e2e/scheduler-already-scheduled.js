// Scheduler preview surfaces already-scheduled pairings, not just games and
// conflicts (#206 slice 1 / #326, #328 review).
//
// renderScheduler() previously read only draft_games/created and unscheduled;
// a pairing the backend now reports in already_scheduled[] (#206 slice 1 —
// a real Game already exists for it, so it is neither proposed nor a
// conflict) was silently invisible, and an all-already-scheduled Division
// rendered the misleading generic "No games generated." At desktop and
// 390px, this journey proves two acceptance states on the real UI:
//   * MIXED   — a 4-team Division with 2 of its 6 round-robin pairings
//     already real Games: the preview shows 4 proposed games, 0 conflicts,
//     and the 2 already-scheduled pairings named with their existing Game
//     reference; commit stays ENABLED (there are 4 real games to commit).
//   * ALL-DONE — a 2-team Division whose one possible pairing already has a
//     real Game: the preview shows 0 games, 0 conflicts, 1 already
//     scheduled, an explanatory "already scheduled" message instead of the
//     generic empty state, and commit stays DISABLED.
//
// Fails on any browser console/page error.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
const ICE_DAY = "2026-09-12";
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8301 },
  { label: "phone", width: 390, height: 844, port: 8302 },
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
    return {
      games: pv.getAttribute("data-games"),
      conflicts: pv.getAttribute("data-conflicts"),
      alreadyScheduled: pv.getAttribute("data-already-scheduled"),
      commitPresent: !!commit,
      commitDisabled: commit ? commit.disabled : null,
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

    // Build one League with two Divisions: a 4-team "Mixed" (six round-robin
    // pairings) and a 2-team "AllDone" (exactly one pairing).
    const ids = await page.evaluate(async (day) => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      const league = await post("/api/setup/league", { name: "Already-Scheduled Program" });
      const season = await post("/api/setup/season", { league_id: league.id, name: "Fall 2026" });
      const level = await post("/api/setup/level", { season_id: season.id, name: "Silver League" });
      const dMixed = await post("/api/setup/division", { season_id: season.id, level_id: level.id, name: "SilverMixed" });
      const dAllDone = await post("/api/setup/division", { season_id: season.id, level_id: level.id, name: "SilverAllDone" });
      const club = await post("/api/setup/club", { name: "Club" });
      const team = async (n) =>
        (await post("/api/v2/setup/team", { club_id: club.id, league_id: level.id, name: n })).id;
      const m0 = await team("Mixed 0"), m1 = await team("Mixed 1");
      const m2 = await team("Mixed 2"), m3 = await team("Mixed 3");
      const a0 = await team("AllDone 0"), a1 = await team("AllDone 1");
      const register = (teamId, divisionId) => post(
        `/api/setup/seasons/${season.id}/team-registrations`,
        { team_id: teamId, division_id: divisionId });
      await register(m0, dMixed.id); await register(m1, dMixed.id);
      await register(m2, dMixed.id); await register(m3, dMixed.id);
      await register(a0, dAllDone.id); await register(a1, dAllDone.id);

      const venue = await post("/api/setup/venue", { name: "Arena", league_id: league.id });
      await post(`/api/v2/setup/seasons/${season.id}/venue-access`, { venue_id: venue.id });
      const rink = await post("/api/setup/rink", { venue_id: venue.id, name: "Rink 1" });
      const pad = (n) => String(n).padStart(2, "0");
      const slot = async (h) => (await post("/api/setup/ice-slot", {
        rink_id: rink.id, start_time: `${day}T${pad(h)}:00:00+00:00`,
        end_time: `${day}T${pad(h + 1)}:00:00+00:00`, slot_type: "game",
      })).id;

      // Ask the scheduler which pairings the Mixed round robin actually
      // produces (no ice yet, so all six land in unscheduled) instead of
      // re-implementing the circle method here; pre-seed real Games for
      // the first two so the NEXT preview is genuinely mixed.
      const bare = await post("/api/scheduler/draft", { division_id: dMixed.id });
      const toSeed = bare.unscheduled.slice(0, 2);
      for (const pairing of toSeed) {
        const seedSlot = await slot(6 + toSeed.indexOf(pairing));
        await post("/api/setup/game", {
          season_id: season.id, division_id: dMixed.id,
          home_team_id: pairing.home_team_id, away_team_id: pairing.away_team_id,
          ice_slot_id: seedSlot,
        });
      }
      // Ice for the four still-missing Mixed pairings.
      for (let h = 8; h < 8 + 4; h++) await slot(h);

      // AllDone's one pairing already has a real Game — no ice slot is
      // even needed for it to be picked up as already-scheduled.
      const allDoneSlot = await slot(20);
      await post("/api/setup/game", {
        season_id: season.id, division_id: dAllDone.id,
        home_team_id: a0, away_team_id: a1, ice_slot_id: allDoneSlot,
      });

      return { dMixed: dMixed.id, dAllDone: dAllDone.id };
    }, ICE_DAY);

    await page.waitForSelector('.tab[data-tab="scheduler"]', { state: "visible", timeout: 10000 });
    await page.click('.tab[data-tab="scheduler"]');
    await page.waitForSelector("#sched-div", { timeout: 10000 });
    await page.waitForFunction(
      (id) => !!document.querySelector(`#sched-div option[value="${id}"]`),
      ids.dMixed, { timeout: 10000 });

    // (1) Mixed: 4 missing pairings proposed, 2 already scheduled, 0 conflicts.
    await generateFor(page, ids.dMixed,
      '#sched-preview[data-games="4"][data-already-scheduled="2"]');
    let s = await previewState(page);
    if (s.conflicts !== "0") {
      fail(`mixed: expected 0 conflicts, got ${JSON.stringify(s)}`);
    }
    if (!/4 game\(s\), 0 conflict\(s\), 2 already scheduled/.test(s.text)) {
      fail(`mixed: header should name the already-scheduled count: ${s.text}`);
    }
    if (!/Already scheduled — Game/.test(s.text)) {
      fail(`mixed: already-scheduled pairings must reference their existing Game: ${s.text}`);
    }
    if (s.commitPresent !== true || s.commitDisabled !== false) {
      fail(`mixed: commit must stay enabled with 4 real missing games: ${JSON.stringify(s)}`);
    }

    // (2) All-done: nothing missing, nothing to commit, but NOT the
    // misleading generic "No games generated." — an explanatory message
    // naming that the round robin is already fully scheduled.
    await generateFor(page, ids.dAllDone,
      '#sched-preview[data-games="0"][data-already-scheduled="1"]');
    s = await previewState(page);
    if (s.conflicts !== "0") {
      fail(`all-done: expected 0 conflicts, got ${JSON.stringify(s)}`);
    }
    if (/No games generated\./.test(s.text)) {
      fail(`all-done: must not show the generic "No games generated." message: ${s.text}`);
    }
    if (!/already scheduled/i.test(s.text) || !/nothing missing/i.test(s.text)) {
      fail(`all-done: missing explanatory "already scheduled" message: ${s.text}`);
    }
    if (s.commitDisabled !== true) {
      fail(`all-done: commit must stay disabled with nothing missing: ${JSON.stringify(s)}`);
    }

    if (errors.length) {
      fail(`console/page errors:\n${errors.join("\n")}`);
    }
    console.log(`[${viewport.label}] OK — mixed and all-already-scheduled previews both name already-scheduled pairings distinctly from proposed games and conflicts.`);
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
    console.log("Scheduler already-scheduled browser journey passed.");
  } catch (error) {
    console.error("Scheduler already-scheduled browser journey FAILED.");
    console.error(error && error.message ? error.message : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
