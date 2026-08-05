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
//   * (5) AN ODD FORMAT ON AN EVEN DIVISION — 5 guaranteed games on a 4-team
//     Division (10 games). This used to be UNREACHABLE: every numeric option
//     was even, on the reasoning that `teams x games` must be even and the
//     screen cannot know a Division's team count. But odd G is feasible for
//     EVERY even team count, so the even-only list did not prevent a
//     refusal — it deleted a supported, common format from the product. The
//     option is selected with the REAL KEYBOARD, the request body is
//     asserted to carry 5, and the guarantee is checked per TEAM.
//   * (6) THE SAME ODD FORMAT ON AN ODD DIVISION — 5 guaranteed games on a
//     5-team Division genuinely is impossible (5 x 5 = 25 team appearances).
//     The screen must SURFACE the backend's structured refusal, naming the
//     nearest achievable counts 4 and 6, keep Commit unavailable, and do it
//     without a console error or horizontal overflow. That surface is what
//     replaces the even-only list: the operator is told what to ask for
//     instead, rather than never being allowed to ask.
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
// Deliberately MORE ice than every scenario below needs put together (16 rows
// committed by (3), then 10 more for (5)), so scenario (4)'s "0 proposed" is
// proof that regeneration found nothing missing — never that it ran out of ice
// to propose onto — and (5)'s 10 rows are all genuinely placed.
const ICE_HOURS = 44;
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

// Chromium's <select> typeahead buffer, in ms. Typing restarts the search
// only after it expires; without the wait, selecting 5 and then 4 searches
// for "54" and silently leaves the previous format selected.
const SELECT_TYPEAHEAD_RESET_MS = 1100;

// Change a <select> with the REAL KEYBOARD, never page.selectOption(): that
// sets the value straight through the DOM and passes even on a control a
// keyboard user cannot reach at all. Focus it, then TYPE the number — the
// option labels start with it ("5 guaranteed games per team"), so Chromium's
// own typeahead lands on it and fires a real `change`.
//
// Typed rather than arrowed on purpose. On macOS, ArrowUp/ArrowDown on a
// CLOSED select opens the native popup instead of moving the selection, and
// headless Chromium never renders that popup — so an arrow-driven helper
// reports "landed on the previous value" here while working on Linux CI,
// which is worse than not testing the keyboard at all. Typeahead is handled
// inside Blink and behaves identically on both.
async function keyboardSelect(page, selector, value, fail) {
  await page.focus(selector);
  const focused = await page.$eval(selector, (el) => el === document.activeElement);
  if (!focused) fail(`${selector} did not take keyboard focus`);
  const options = await page.$$eval(`${selector} option`,
    (els) => els.map((el) => ({ value: el.value, label: el.textContent })));
  const wanted = options.find((o) => o.value === value);
  if (!wanted) fail(`${selector} offers no option "${value}"`);
  // The TRAILING SPACE is load-bearing. Typeahead searches forward from the
  // current selection, so typing "5" from "8" finds "52 guaranteed games per
  // team" before wrapping round to "5 …" — a silent mis-selection that still
  // produces a valid-looking preview. Searching for "5 " cannot match "52…",
  // so the landing option is the one asked for from ANY starting value.
  if (!wanted.label.startsWith(`${value} `)) {
    fail(`${selector} option "${value}" is labelled "${wanted.label}", which `
      + `keyboard typeahead cannot address unambiguously`);
  }
  await page.waitForTimeout(SELECT_TYPEAHEAD_RESET_MS);
  await page.keyboard.type(`${value} `);
  await page.waitForTimeout(150);
  const landed = await page.$eval(selector, (el) => el.value);
  if (landed !== value) {
    fail(`keyboard selection of "${value}" on ${selector} landed on "${landed}"`);
  }
}

// The page must never scroll sideways — asserted at BOTH viewports, since a
// refusal panel wide enough to fit 1440px can still push a 390px phone.
async function assertNoHorizontalOverflow(page, where, fail) {
  const overflow = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  }));
  if (overflow.scrollWidth > overflow.clientWidth + 1) {
    fail(`${where}: horizontal overflow — the page scrolls sideways `
      + `(${overflow.scrollWidth} > ${overflow.clientWidth})`);
  }
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
  // Scenario (6) provokes a deliberate 400 (the parity refusal), and Chromium
  // logs a console error for any non-2xx fetch on its own account. Allowed as
  // narrowly as the fact permits: ONLY a resource-load failure naming 400, and
  // ONLY while the journey is standing in that one step. Every other console
  // error — including a 400 from anywhere else or at any other moment — still
  // fails the run, so scenarios (1)-(5) keep the zero-errors bar they had. A
  // blanket ignore would hide exactly the request defects this journey exists
  // to catch.
  let expectingFormatRefusal = false;
  page.on("pageerror", (e) => errors.push(`[pageerror] ${e.message}`));
  page.on("console", (m) => {
    if (m.type() !== "error") return;
    const text = m.text();
    if (expectingFormatRefusal && /Failed to load resource/.test(text)
        && /\b400\b/.test(text)) return;
    errors.push(`[console] ${text}`);
  });

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
      // A SECOND even Division, kept untouched by (1)-(4), so scenario (5)
      // asks for its odd format against an empty calendar rather than against
      // the 16 games (3) commits into FormatNorth.
      const south = await post("/api/setup/division", {
        season_id: season.id, level_id: level.id, name: "FormatSouth" });
      // And an ODD one, for the refusal in (6). 5 teams x 5 games = 25 team
      // appearances, which no schedule can split into whole games.
      const odd = await post("/api/setup/division", {
        season_id: season.id, level_id: level.id, name: "FormatOdd" });
      const club = await post("/api/setup/club", { name: "Club" });
      const team = async (n) =>
        (await post("/api/v2/setup/team", { club_id: club.id, league_id: level.id, name: n })).id;
      const register = async (names, divisionId) => {
        for (const name of names) {
          const id = await team(name);
          await post(`/api/setup/seasons/${season.id}/team-registrations`,
                     { team_id: id, division_id: divisionId });
        }
      };
      await register(["Format 1", "Format 2", "Format 3", "Format 4"], div.id);
      await register(["South 1", "South 2", "South 3", "South 4"], south.id);
      await register(["Odd 1", "Odd 2", "Odd 3", "Odd 4", "Odd 5"], odd.id);
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
      return { div: div.id, south: south.id, odd: odd.id };
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
    // Every numeric option must be one the backend ACCEPTS — an integer in
    // 1..MAX_GAMES_PER_TEAM (120), the range `_normalize_games_per_team`
    // validates. That is a different and much weaker bar than the one this
    // replaces ("every option is EVEN, so no Division can ever refuse it"),
    // and deliberately so: an even-only list is unconditionally safe only
    // because it deletes every odd format, including the ones an even-team
    // Division can play perfectly well. Feasibility is a fact about the
    // SELECTED Division, so the backend decides it and the screen surfaces
    // the answer — scenarios (5) and (6) below drive both halves for real.
    const offered = await page.$$eval(
      "#sched-games option", (els) => els.map((el) => el.value));
    const numeric = offered.filter((v) => v !== "rr").map(Number);
    for (const n of numeric) {
      if (!Number.isInteger(n) || n < 1 || n > 120) {
        fail(`#sched-games offers "${n}", outside the range the backend accepts`);
      }
    }
    // ANTI-VACUITY. Without this, the range check above is satisfied by the
    // very even-only list it replaces, and the whole blocker would look
    // fixed while the capability was still missing. Odd values must actually
    // be on offer, and 5 specifically, since (5) and (6) select it.
    if (!numeric.some((n) => n % 2 === 1)) {
      fail(`#sched-games offers no odd value: ${JSON.stringify(offered)}`);
    }
    if (!numeric.includes(5)) {
      fail(`#sched-games does not offer 5: ${JSON.stringify(offered)}`);
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

    // (5) AN ODD FORMAT ON AN EVEN DIVISION — the capability the even-only
    // option list removed. 4 teams x 5 games = 20 team appearances = 10
    // games, entirely feasible; the option is reached with the REAL KEYBOARD.
    await page.selectOption("#sched-div", ids.south);
    await keyboardSelect(page, "#sched-games", "5", fail);
    await page.click("[data-sched-generate]");
    await page.waitForSelector('#sched-preview[data-games="10"]', { timeout: 15000 });
    s = await previewState(page);
    // The REAL request, not just the rendered result: a control that looked
    // right but sent the previous format would still produce a preview.
    const oddBody = draftBodies[draftBodies.length - 1];
    if (!oddBody || oddBody.games_per_team !== 5) {
      fail(`5 games: Generate must send games_per_team 5, sent ${JSON.stringify(oddBody)}`);
    }
    if (oddBody.meetings_per_opponent !== undefined) {
      fail(`5 games: Generate must not send both format fields, sent ${JSON.stringify(oddBody)}`);
    }
    if (oddBody.division_id !== ids.south) {
      fail(`5 games: Generate must target FormatSouth, sent ${JSON.stringify(oddBody)}`);
    }
    if (s.titles.length !== 10) {
      fail(`5 games: expected 10 proposed rows, got ${s.titles.length}`);
    }
    // THE GUARANTEE, per TEAM: 10 rows is also what a schedule giving one
    // team 6 and another 4 would produce, so the row count proves nothing.
    const oddPerTeam = {};
    for (const title of s.titles) {
      for (const name of title.split(" vs ").map((n) => n.trim())) {
        oddPerTeam[name] = (oddPerTeam[name] || 0) + 1;
      }
    }
    if (Object.keys(oddPerTeam).length !== 4) {
      fail(`5 games: expected 4 teams to appear, got ${JSON.stringify(oddPerTeam)}`);
    }
    for (const name of Object.keys(oddPerTeam)) {
      if (oddPerTeam[name] !== 5) {
        fail(`5 games: "${name}" plays ${oddPerTeam[name]}, not the guaranteed 5: ${JSON.stringify(oddPerTeam)}`);
      }
    }
    if (s.commitPresent !== true || s.commitDisabled !== false) {
      fail(`5 games: commit must be enabled with 10 games: ${JSON.stringify(s)}`);
    }
    await assertNoHorizontalOverflow(page, "5 games on 4 teams", fail);

    // (6) THE SAME FORMAT ON AN ODD DIVISION — genuinely impossible, and the
    // screen must say so ACTIONABLY rather than the option never existing.
    await page.selectOption("#sched-div", ids.odd);
    await keyboardSelect(page, "#sched-games", "5", fail);
    expectingFormatRefusal = true;
    await page.click("[data-sched-generate]");
    await page.waitForSelector("#sched-format-refusal", { timeout: 15000 });
    const refusal = await page.evaluate(() => {
      const el = document.querySelector("#sched-format-refusal");
      return {
        reason: el.getAttribute("data-reason"),
        nearest: el.getAttribute("data-nearest"),
        text: el.textContent.replace(/\s+/g, " ").trim(),
        preview: !!document.querySelector("#sched-preview"),
        commit: !!document.querySelector("[data-sched-commit]"),
        // render() replaces #content wholesale, so the Generate button the
        // operator just activated is destroyed. Where focus lands decides
        // whether a keyboard user can act on the guidance at all.
        focused: document.activeElement
          ? document.activeElement.getAttribute("data-sched-generate") !== null
          : false,
      };
    });
    if (refusal.reason !== "games_per_team_infeasible") {
      fail(`5 games on 5 teams: expected the structured parity refusal, got ${JSON.stringify(refusal)}`);
    }
    if (refusal.nearest !== "4,6") {
      fail(`5 games on 5 teams: expected the nearest achievable counts 4 and 6, got ${JSON.stringify(refusal)}`);
    }
    // The guidance must be READABLE, not merely present in an attribute.
    if (!/\b4 or 6\b/.test(refusal.text)) {
      fail(`5 games on 5 teams: the visible text must name 4 or 6, got "${refusal.text}"`);
    }
    // Commit must be unavailable: there is no reviewed proposal to commit.
    if (refusal.preview || refusal.commit) {
      fail(`5 games on 5 teams: a refusal must leave no preview and no Commit: ${JSON.stringify(refusal)}`);
    }
    // The guidance says "pick a different number and Generate again", so the
    // operator has to be able to. Focus must be back on Generate, not on the
    // document body the wholesale re-render would otherwise leave it in.
    if (!refusal.focused) {
      fail("5 games on 5 teams: a refusal must return focus to Generate, "
        + "not drop a keyboard user to the document body");
    }
    await assertNoHorizontalOverflow(page, "5 games on 5 teams", fail);
    if (errors.length) {
      fail(`console/page errors after the refusal:\n${errors.join("\n")}`);
    }

    // And the guidance can be TAKEN: 4 on the same odd Division works, so
    // the refusal named a number that is really achievable rather than a
    // plausible-looking one. The 400 allowance closes here — this Generate
    // must succeed, so any console error from it is a real one again.
    expectingFormatRefusal = false;
    await keyboardSelect(page, "#sched-games", "4", fail);
    await page.click("[data-sched-generate]");
    await page.waitForSelector('#sched-preview[data-games="10"]', { timeout: 15000 });
    if (await page.$("#sched-format-refusal")) {
      fail("a successful Generate must clear the previous format refusal");
    }

    if (errors.length) {
      fail(`console/page errors:\n${errors.join("\n")}`);
    }
    console.log(`[${viewport.label}] OK — the picker sends games_per_team on Generate AND Commit; 8 guaranteed games per team (base 2, remainder 2) previewed and committed; regeneration is a visible no-op; 5 guaranteed games is keyboard-reachable and honoured on a 4-team Division, and refused with actionable 4-or-6 guidance on a 5-team one.`);
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
    console.error("Scheduler games-per-team browser journey FAILED.");
    console.error(error && error.message ? error.message : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
