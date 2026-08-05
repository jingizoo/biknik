// GUARANTEED GAMES PER TEAM on the real Scheduler UI (#375).
//
// The operator control is inverted: instead of asking how many times a team
// plays each opponent and letting games-per-team fall out, the operator asks
// for the number of games THEIR team is guaranteed and the backend derives the
// per-opponent count (`base = G // (T-1)`, with `rem = G % (T-1)` opponents
// played once more). The backend takes `games_per_team` on BOTH
// /api/scheduler/draft and /api/scheduler/commit, and binds it into
// draft_fingerprint so a Commit that disagrees with the reviewed preview is
// refused. None of that is reachable by an operator unless the select is
// rendered, read, and sent on both requests — which is exactly what unit and
// HTTP tests cannot prove and this journey can.
//
// At desktop and 390px, against a 4-team Division with ice to spare:
//   * (1) DEFAULT — an operator who never touches the control still gets the
//     historical single round-robin: 6 games (C(4,2)), one per pair, sent as
//     the legacy meetings_per_opponent: 1. "Play everyone once" is (T-1)
//     games, which differs per Division, so no fixed games-per-team value
//     expresses it and the option is kept rather than dropped.
//   * (2) EIGHT GUARANTEED GAMES — the case a meetings picker CANNOT express,
//     and therefore the one worth driving through the real UI. 8 games with 3
//     opponents is base 2 remainder 2: every team plays two opponents three
//     times and one opponent twice, for 16 rows (4 x 8 / 2). Asserted as the
//     operator experiences it — every TEAM appears exactly 8 times — plus the
//     per-pair 3/3/2 shape the remainder produces. The request body is
//     captured and asserted to carry games_per_team: 8 and NOT
//     meetings_per_opponent, since sending both is refused.
//   * (3) COMMIT SENDS THE REVIEWED FORMAT — committing that preview creates
//     16 real games, and the captured commit body carries the same 8. If
//     app.js dropped the field here, the backend's own regeneration would be a
//     single round-robin, the fingerprint could not match, and the commit
//     would be refused as preview_stale instead of creating anything.
//   * (4) IDEMPOTENT REGENERATION — Generate again, unchanged, at the same
//     format: every obligation is now satisfied, so the preview proposes 0
//     games and reports all 16 as already scheduled, and Commit is DISABLED.
//     This is the operator-visible face of "running it twice produces no
//     second set".
//
// Fails on any browser console/page error.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
const ICE_DAY = "2026-09-05";
// 24 game slots for the 16 rows 8 guaranteed games needs: deliberately MORE
// ice than the format requires, so scenario (4)'s "0 proposed" is proof that
// regeneration found nothing missing, never that it ran out of ice to propose
// onto.
const ICE_HOURS = 24;
const VIEWPORTS = [
  // 8311/8312 are unique across the whole e2e suite — no other journey binds
  // them, so this one can never race a shard-mate for a port.
  { label: "desktop", width: 1440, height: 900, port: 8311 },
  { label: "phone", width: 390, height: 844, port: 8312 },
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
    return {
      games: pv.getAttribute("data-games"),
      conflicts: pv.getAttribute("data-conflicts"),
      alreadyScheduled: pv.getAttribute("data-already-scheduled"),
      commitPresent: !!commit,
      commitDisabled: commit ? commit.disabled : null,
      // Each proposed row's matchup title, so the per-pair meeting count can
      // be asserted from what the operator actually sees rendered.
      titles: Array.from(pv.querySelectorAll(".card .li"))
        .filter((li) => !(li.textContent || "").includes("Already scheduled"))
        .map((li) => {
          const t = li.querySelector(".li-title");
          return (t ? t.textContent : "").replace(/\s+/g, " ").trim();
        })
        .filter(Boolean),
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

  // Capture the REAL request bodies app.js sends, so "the preview got bigger"
  // can be distinguished from "the format was actually transmitted".
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

    const ids = await page.evaluate(async ([day, hours]) => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      const league = await post("/api/setup/league", { name: "Format Program" });
      const season = await post("/api/setup/season", { league_id: league.id, name: "Fall 2026" });
      const level = await post("/api/setup/level", { season_id: season.id, name: "Format League" });
      const div = await post("/api/setup/division", {
        season_id: season.id, level_id: level.id, name: "FormatNorth" });
      const club = await post("/api/setup/club", { name: "Club" });
      const team = async (n) =>
        (await post("/api/v2/setup/team", { club_id: club.id, league_id: level.id, name: n })).id;
      for (const name of ["Format 1", "Format 2", "Format 3", "Format 4"]) {
        const id = await team(name);
        await post(`/api/setup/seasons/${season.id}/team-registrations`,
                   { team_id: id, division_id: div.id });
      }
      const venue = await post("/api/setup/venue", { name: "Arena", league_id: league.id });
      // Without an active SeasonVenueAccess grant the league-scoped scheduler
      // filters out every slot as "not assigned to this season".
      await post(`/api/v2/setup/seasons/${season.id}/venue-access`, { venue_id: venue.id });
      const rink = await post("/api/setup/rink", { venue_id: venue.id, name: "Rink 1" });
      // One slot per DAY, never per hour: two meetings of the same pair must
      // not be refused as a team double-booking (#373).
      const pad = (n) => String(n).padStart(2, "0");
      for (let i = 0; i < hours; i++) {
        const d = new Date(`${day}T08:00:00Z`);
        d.setUTCDate(d.getUTCDate() + i);
        const iso = `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`;
        await post("/api/setup/ice-slot", {
          rink_id: rink.id, start_time: `${iso}T08:00:00+00:00`,
          end_time: `${iso}T09:00:00+00:00`, slot_type: "game",
        });
      }
      return { div: div.id };
    }, [ICE_DAY, ICE_HOURS]);

    await page.waitForSelector('.tab[data-tab="scheduler"]', { state: "visible", timeout: 10000 });
    await page.click('.tab[data-tab="scheduler"]');
    await page.waitForSelector("#sched-div", { timeout: 10000 });
    await page.waitForFunction(
      (id) => !!document.querySelector(`#sched-div option[value="${id}"]`),
      ids.div, { timeout: 10000 });

    // The control must actually exist before anything below means anything.
    if (!(await page.$("#sched-games"))) {
      fail("the Scheduler panel has no guaranteed-games-per-team control");
    }
    // The accessible name must describe what the control now MEANS. A picker
    // that still says "games against each opponent" while sending
    // games_per_team is the doc-contradicts-code defect, in the one place a
    // screen-reader user cannot work around.
    const gamesLabel = await page.$eval(
      'label[for="sched-games"]', (el) => (el.textContent || "").trim());
    if (gamesLabel !== "Guaranteed games per team") {
      fail(`the format control's label must say what it sets, got "${gamesLabel}"`);
    }
    // No option may be one the backend would refuse. `teams x games` must be
    // even or no construction can honour the guarantee, and the screen cannot
    // know a Division's team count before Generate runs — so every numeric
    // option is even, which is feasible for EVERY team count.
    const offered = await page.$$eval(
      "#sched-games option", (els) => els.map((el) => el.value));
    for (const value of offered) {
      if (value === "rr") continue;
      const n = Number(value);
      if (!Number.isInteger(n) || n < 1 || n % 2 !== 0) {
        fail(`#sched-games offers "${value}", which the backend can refuse`);
      }
    }

    // (1) DEFAULT: untouched control -> the historical single round-robin.
    await page.selectOption("#sched-div", ids.div);
    await page.click("[data-sched-generate]");
    await page.waitForSelector('#sched-preview[data-games="6"]', { timeout: 15000 });
    let s = await previewState(page);
    if (s.titles.length !== 6) {
      fail(`default: expected 6 proposed rows, got ${JSON.stringify(s.titles)}`);
    }
    if (new Set(s.titles).size !== 6) {
      fail(`default: every pair should appear once, got ${JSON.stringify(s.titles)}`);
    }
    if (draftBodies.length !== 1 || draftBodies[0].meetings_per_opponent !== 1) {
      fail(`default: Generate must send meetings_per_opponent 1, sent ${JSON.stringify(draftBodies)}`);
    }
    if (draftBodies[0].games_per_team !== undefined) {
      fail(`default: Generate must not send both format fields, sent ${JSON.stringify(draftBodies)}`);
    }

    // (2) EIGHT GUARANTEED GAMES: 4 x 8 / 2 = 16 rows, and -- the part a
    // meetings picker cannot express -- 8 with 3 opponents is base 2
    // remainder 2, so each team plays two opponents THREE times and one
    // opponent twice.
    await page.selectOption("#sched-games", "8");
    await page.click("[data-sched-generate]");
    await page.waitForSelector('#sched-preview[data-games="16"]', { timeout: 15000 });
    s = await previewState(page);
    if (draftBodies.length !== 2 || draftBodies[1].games_per_team !== 8) {
      fail(`8 games: Generate must send games_per_team 8, sent ${JSON.stringify(draftBodies)}`);
    }
    if (draftBodies[1].meetings_per_opponent !== undefined) {
      fail(`8 games: Generate must not send both format fields, sent ${JSON.stringify(draftBodies)}`);
    }
    if (s.titles.length !== 16) {
      fail(`8 games: expected 16 proposed rows, got ${s.titles.length}`);
    }
    // THE GUARANTEE, as the operator reads it: every TEAM appears 8 times.
    // Counted per team rather than per pair, because "16 rows exist" is
    // satisfied by a schedule that gives one team 9 games and another 7.
    const perTeam = {};
    const perPair = {};
    for (const title of s.titles) {
      const names = title.split(" vs ").map((n) => n.trim());
      for (const name of names) perTeam[name] = (perTeam[name] || 0) + 1;
      // "A vs B" and "B vs A" are the same pair meeting twice, so normalise
      // the orientation before counting -- otherwise the home/away split
      // would make every meeting look like a different matchup.
      const key = names.slice().sort().join(" | ");
      perPair[key] = (perPair[key] || 0) + 1;
    }
    const teamNames = Object.keys(perTeam);
    if (teamNames.length !== 4) {
      fail(`8 games: expected 4 teams to appear, got ${JSON.stringify(perTeam)}`);
    }
    for (const name of teamNames) {
      if (perTeam[name] !== 8) {
        fail(`8 games: "${name}" plays ${perTeam[name]} games, not the guaranteed 8: ${JSON.stringify(perTeam)}`);
      }
    }
    // The remainder's shape: 6 distinct pairs, four met 3x and two met 2x
    // (each team: two opponents 3x, one 2x -> 3+3+2 = 8).
    const pairKeys = Object.keys(perPair);
    if (pairKeys.length !== 6) {
      fail(`8 games: expected 6 distinct pairs, got ${JSON.stringify(perPair)}`);
    }
    const pairCounts = pairKeys.map((k) => perPair[k]).sort();
    if (JSON.stringify(pairCounts) !== JSON.stringify([2, 2, 3, 3, 3, 3])) {
      fail(`8 games: expected a base-2 remainder-2 split (2,2,3,3,3,3), got ${JSON.stringify(perPair)}`);
    }
    if (s.commitPresent !== true || s.commitDisabled !== false) {
      fail(`8 games: commit must be enabled with 16 games: ${JSON.stringify(s)}`);
    }

    // (3) COMMIT sends the reviewed format, and really creates 16 games.
    await page.click("[data-sched-commit]");
    await page.waitForFunction(
      () => /Committed 16 draft game\(s\)/.test(document.body.textContent || ""),
      null, { timeout: 15000 });
    if (commitBodies.length !== 1 || commitBodies[0].games_per_team !== 8) {
      fail(`commit must send games_per_team 8, sent ${JSON.stringify(commitBodies)}`);
    }
    if (commitBodies[0].meetings_per_opponent !== undefined) {
      fail(`commit must not send both format fields, sent ${JSON.stringify(commitBodies)}`);
    }

    // (4) IDEMPOTENT: regenerate unchanged at the same format -> nothing
    // missing, everything already scheduled, commit disabled.
    await page.selectOption("#sched-games", "8");
    await page.click("[data-sched-generate]");
    await page.waitForSelector(
      '#sched-preview[data-games="0"][data-already-scheduled="16"]', { timeout: 15000 });
    s = await previewState(page);
    if (s.commitDisabled !== true) {
      fail(`idempotent regenerate: commit must be disabled with nothing missing: ${JSON.stringify(s)}`);
    }
    if (s.titles.length !== 0) {
      fail(`idempotent regenerate: expected no proposed rows, got ${JSON.stringify(s.titles)}`);
    }

    if (errors.length) {
      fail(`console/page errors:\n${errors.join("\n")}`);
    }
    console.log(`[${viewport.label}] OK — the picker sends games_per_team on Generate AND Commit; 8 guaranteed games per team (base 2, remainder 2) previewed and committed; regeneration is a visible no-op.`);
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
    console.log("Scheduler games-per-team browser journey passed.");
  } catch (error) {
    console.error("Scheduler meetings-format browser journey FAILED.");
    console.error(error && error.message ? error.message : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
