// One permanent Team across two Seasons + registration-based scheduling (#180).
//
// At desktop and 390px, a League Admin has one permanent Team registered in two
// Seasons in DIFFERENT Divisions. The journey verifies:
//   * Season participation shows the single Team in each Season's own Division;
//   * the Arena Calendar scheduling wizard offers that Team only in a Division it
//     is REGISTERED in (via SeasonTeamRegistration), never via the legacy
//     Team.division_id — a division it isn't registered in doesn't list it;
//   * the SAME permanent Team is reachable in its OTHER Season's Division after
//     an explicit context switch — one Team, two Seasons, two Divisions, proven
//     through the real UI rather than through one read that spans both.
//
// #369: the Calendar/wizard read (`get_demo_overview`) ceilings HARD on the
// active Season — divisions, registrations, games and (via SeasonVenueAccess)
// venues/rinks/ice slots are the active Season's only. So this journey never
// leans on one context showing every Season's data: it drives the REAL header
// context switcher (`#ctx-select`, values "<programId>|<seasonId>") to the
// Season each step operates in, exactly as an operator must. The switcher's
// option list is seeded once per page load, so the fixture build is followed by
// a reload before the first switch.
// Fails on any browser console/page error.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
// The Arena Calendar is pinned to this demo date in the app.
const CAL_DAY = "2026-09-05";
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8161 },
  { label: "phone", width: 390, height: 844, port: 8162 },
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

// Move the REAL header context switcher to a (Program, Season) pair and wait
// until the app has actually repainted from it. `#ctx-select` option values are
// "<programId>|<seasonId>"; the list is seeded once per page load, so a Season
// created after that point is only selectable after a reload. The context is
// never poked into localStorage or set through a direct API call — the point of
// every assertion downstream is that the PERSISTED context drives the read.
async function switchSeason(page, label, programId, seasonId) {
  // render() rewrites #content wholesale, so a stamp on the painted view cannot
  // survive a repaint — a real happened-after signal, not a guessed sleep.
  await page.evaluate(() => {
    const el = document.querySelector("#content > *");
    if (el) el.dataset.tdpStamp = "1";
  });
  const value = `${programId}|${seasonId}`;
  await page.selectOption("#ctx-select", value);
  await page.waitForFunction((v) => {
    const el = document.getElementById("ctx-select");
    return el && el.value === v;
  }, value, { timeout: 10000 }).catch(() => {
    throw new Error(`[${label}] the context select never settled on "${value}"`);
  });
  // The switch is fire-and-forget from the <select>'s own onchange, so wait for
  // the PERSISTED context — what every scoped read resolves through — and then
  // for the repaint it triggers.
  await page.waitForFunction(async (s) => {
    const r = await (await fetch("/api/context", { credentials: "same-origin" })).json();
    return !!r && r.season_id === s;
  }, seasonId, { timeout: 10000, polling: 250 }).catch(() => {
    throw new Error(`[${label}] the active context never persisted Season ${seasonId}`);
  });
  await page.waitForFunction(() => {
    const el = document.querySelector("#content > *");
    return !!el && el.dataset.tdpStamp !== "1";
  }, null, { timeout: 15000 }).catch(() => {
    throw new Error(`[${label}] the view never repainted after the context switch`);
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

    const ids = await page.evaluate(async (day) => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      // An operator Organization is linked at Program creation so that the
      // reload below lands a League Admin on the normal shell (with the header
      // context switcher) instead of the blocking Initial Setup wizard — the
      // established fixture convention, see league-filtered-data.js.
      const org = await post("/api/v2/setup/organization", { name: "Perm Org" });
      const league = await post("/api/setup/league",
        { name: "Perm League", operator_organization_id: org.id });
      const s1 = await post("/api/setup/season", { league_id: league.id, name: "2026" });
      const s2 = await post("/api/setup/season", { league_id: league.id, name: "2027" });
      // #283 Slice E / rule 7: Perma's two registrations (d1 in s1, d2 in s2)
      // must both fall under Perma's SINGLE permanent League. So one permanent
      // League L_A (lv1) spans BOTH Seasons — it is created for s1 here, and
      // binds to s2 automatically when d2 is created under it below (a Division
      // create find-or-creates the LeagueSeason for its (league, season) pair).
      // A SECOND permanent League L_B (lv2) also exists in s1 so the scheduling
      // wizard's League picker still offers ≥2 Leagues (never implicitly
      // pre-selected) and has a foreign Division to prove league-scoping.
      const lv1 = await post("/api/setup/level", { season_id: s1.id, name: "Level One" });  // L_A
      const lv2 = await post("/api/setup/level", { season_id: s1.id, name: "Level Two" });  // L_B
      const d1 = await post("/api/setup/division", { season_id: s1.id, level_id: lv1.id, name: "S1 Div One" });
      const d1b = await post("/api/setup/division", { season_id: s1.id, level_id: lv1.id, name: "S1 Div Two" });
      // d2 is L_A's Division in s2 — creating it binds L_A to s2 (rule 7 keeps
      // Perma's s1 + s2 registrations in the same permanent League).
      const d2 = await post("/api/setup/division", { season_id: s2.id, level_id: lv1.id, name: "S2 Div One" });
      // dOther is L_B's own Division in s1 — a foreign-League division that must
      // never leak into L_A's picker (assertion B1).
      const dOther = await post("/api/setup/division", { season_id: s1.id, level_id: lv2.id, name: "S1 L2 Div" });
      const venue = await post("/api/setup/venue", { name: "V", league_id: league.id });
      // Game ice eligibility (#233 Slice E) requires the venue to hold active
      // SeasonVenueAccess for the season the game is scheduled in. Since #369
      // that grant is also what makes the venue — and its rinks and ice slots —
      // VISIBLE at all: the Calendar read is ceilinged on the active Season, so
      // ice granted only to s1 simply does not exist while s2 is active. This
      // journey works the wizard in BOTH Seasons (step (E) schedules in s2), so
      // the one arena is granted to both — the same physical rink used across
      // two seasons.
      //
      // Each grant is a WRITE gated on the ACTIVE Season (#369 prereq): the
      // no-context fallback would resolve to the latest-id Season (s2), so a
      // grant is only accepted while its own Season is selected. So the context
      // is switched explicitly before each one and BOTH responses are asserted
      // — this helper decodes the body without checking status, and a silent
      // 404 would otherwise surface much later as an unexplained wizard
      // timeout. The production guard is honoured, never bypassed. The context
      // is left on s1, the Season steps (B)–(D) work in (step (A) reloads and
      // calls switchSeason for s1 anyway, so this is belt-and-braces).
      const grantVenue = async (seasonId, label) => {
        const ctx = await post("/api/context", { program_id: league.id, season_id: seasonId });
        if (!ctx || (ctx.season || {}).id !== seasonId) {
          throw new Error(`could not select ${label} context: ${JSON.stringify(ctx)}`);
        }
        const grant = await post(`/api/v2/setup/seasons/${seasonId}/venue-access`, { venue_id: venue.id });
        if (!grant || !grant.id) {
          throw new Error(`${label} venue-access grant refused: ${JSON.stringify(grant)}`);
        }
      };
      await grantVenue(s2.id, "s2");
      await grantVenue(s1.id, "s1");
      const rink = await post("/api/setup/rink", { venue_id: venue.id, name: "R" });
      const club = await post("/api/setup/club", { name: "Club" });
      // #180/#283 Slice E: create every permanent Team under its permanent
      // League L_A (lv1) via the canonical v2 route (league_id REQUIRED; the
      // service derives Program from it). Its season/division placement is set
      // separately via registration below. The v1 route can't be used here: the
      // Program now holds two permanent Leagues, so a v1 create keyed only on
      // the Program (no division) is ambiguous (team_league_required).
      const team = async (n) =>
        (await post("/api/v2/setup/team", { club_id: club.id, league_id: lv1.id, name: n })).id;
      const perma = await team("Perma");   // the one permanent team we track
      const mateA = await team("Mate A");
      const mateB = await team("Mate B");
      const otherD = await team("Other D");
      // Perma plays d1 in season 1 and d2 in season 2 (different divisions);
      // it is NOT registered in d1b.
      await post(`/api/setup/seasons/${s1.id}/team-registrations`, { team_id: perma, division_id: d1.id });
      await post(`/api/setup/seasons/${s1.id}/team-registrations`, { team_id: mateA, division_id: d1.id });
      await post(`/api/setup/seasons/${s2.id}/team-registrations`, { team_id: perma, division_id: d2.id });
      await post(`/api/setup/seasons/${s2.id}/team-registrations`, { team_id: mateB, division_id: d2.id });
      await post(`/api/setup/seasons/${s1.id}/team-registrations`, { team_id: otherD, division_id: d1b.id });
      // A league-only registration (no Division) under lv1 — #233 B2c
      // review: proves the wizard's no-Division path still creates a valid
      // league-only game.
      const leagueOnly = await team("League Only");
      await post(`/api/setup/seasons/${s1.id}/team-registrations`,
        { team_id: leagueOnly, division_id: null });
      const slot = await post("/api/setup/ice-slot", {
        rink_id: rink.id, start_time: `${day}T18:00:00+00:00`,
        end_time: `${day}T19:00:00+00:00`, slot_type: "game" });
      // A second game slot for the #283 Slice D Exhibition step below, and a
      // third for the season-2 wizard step (E) — the ice is physical and shared,
      // but slots 1 and 2 are consumed by the games created in season 1.
      const slot2 = await post("/api/setup/ice-slot", {
        rink_id: rink.id, start_time: `${day}T20:00:00+00:00`,
        end_time: `${day}T21:00:00+00:00`, slot_type: "game" });
      const slot3 = await post("/api/setup/ice-slot", {
        rink_id: rink.id, start_time: `${day}T22:00:00+00:00`,
        end_time: `${day}T23:00:00+00:00`, slot_type: "game" });
      return { league: league.id, s1: s1.id, s2: s2.id, lv1: lv1.id,
        d1: d1.id, d1b: d1b.id, d2: d2.id, dOther: dOther.id, perma, mateA,
        leagueOnly, slot: slot.id, slot2: slot2.id, slot3: slot3.id };
    }, CAL_DAY);

    // (A) Season participation: one permanent Team, two seasons, two divisions.
    const part = await page.evaluate(async (i) => {
      const get = async (p) => (await fetch(p, { credentials: "same-origin" })).json();
      const teams = (await get(`/api/setup/leagues/${i.league}/teams`)).teams;
      const r1 = (await get(`/api/setup/seasons/${i.s1}/team-registrations`)).registrations;
      const r2 = (await get(`/api/setup/seasons/${i.s2}/team-registrations`)).registrations;
      const permaCount = teams.filter((t) => t.id === i.perma).length;
      const in1 = r1.find((r) => r.team_id === i.perma && r.active);
      const in2 = r2.find((r) => r.team_id === i.perma && r.active);
      return { permaCount, div1: in1 && in1.division_id, div2: in2 && in2.division_id };
    }, ids);
    if (part.permaCount !== 1) {
      throw new Error(`[${viewport.label}] expected exactly one permanent Team, got ${part.permaCount}`);
    }
    if (part.div1 !== ids.d1 || part.div2 !== ids.d2) {
      throw new Error(`[${viewport.label}] Perma resolved wrong per-season divisions: ${JSON.stringify(part)}`);
    }

    // Both Seasons were created after this page loaded, so the context
    // switcher — seeded once by loadContextOptions() — does not know about
    // them yet. Reload, then switch to the Season steps (B)–(D) operate in.
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });
    await page.waitForSelector("#ctx-select:not([hidden])", { timeout: 10000 });
    await switchSeason(page, viewport.label, ids.league, ids.s1);

    // (B) Scheduling wizard filters by registration, not legacy division.
    await page.click('.tab[data-tab="calendar"]');
    await page.waitForSelector(`[data-slot="${ids.slot}"]`, { timeout: 15000 });
    await page.click(`[data-slot="${ids.slot}"]`);
    await page.waitForSelector("#w-league", { timeout: 10000 });

    // (B0) #233 B2c review: with TWO Leagues in the store (lv1, lv2), the
    // League select must NEVER implicitly pre-select one — an operator must
    // choose explicitly, or a game could silently write to the wrong
    // competitive grouping. It starts on a disabled placeholder, and every
    // downstream control (Division/Home/Away/Create) stays disabled until a
    // League is chosen. League and Division also carry accessible names.
    const preSelect = await page.evaluate(() => {
      const lg = document.querySelector("#w-league");
      const div = document.querySelector("#w-div");
      return {
        leagueValue: lg.value, leagueLabel: lg.getAttribute("aria-label"),
        divLabel: div.getAttribute("aria-label"),
        divDisabled: div.disabled,
        homeDisabled: document.querySelector("#w-home").disabled,
        awayDisabled: document.querySelector("#w-away").disabled,
        createDisabled: document.querySelector("[data-wizcreate]").disabled,
      };
    });
    if (preSelect.leagueValue !== "") {
      throw new Error(`[${viewport.label}] League was implicitly pre-selected: ${JSON.stringify(preSelect)}`);
    }
    if (preSelect.leagueLabel !== "League" || preSelect.divLabel !== "Division") {
      throw new Error(`[${viewport.label}] League/Division controls lack accessible names: ${JSON.stringify(preSelect)}`);
    }
    if (!preSelect.divDisabled || !preSelect.homeDisabled || !preSelect.awayDisabled || !preSelect.createDisabled) {
      throw new Error(`[${viewport.label}] downstream controls were not disabled before a League was chosen: ${JSON.stringify(preSelect)}`);
    }

    // League is required (#233 B2c) — select the season's League so its
    // Division picker is scoped to d1/d1b.
    await page.selectOption("#w-league", ids.lv1);
    await page.waitForFunction(
      (d) => !!Array.from(document.querySelectorAll("#w-div option")).find((o) => o.value === d),
      ids.d1, { timeout: 10000 });

    // (B1) Choosing a League enables the downstream controls, and scopes the
    // Division picker to it — dOther (L_B's own division, in this same season)
    // must never leak into L_A's picker. d2 is L_A's own division in the OTHER
    // season and is absent too: since #369 the read is ceilinged on the active
    // Season, so the picker is League-scoped AND Season-scoped, and scheduling
    // into another Season's division is not offered from here at all. (Step (E)
    // below reaches d2 the supported way — by switching Season.)
    const postSelect = await page.evaluate(() => ({
      divDisabled: document.querySelector("#w-div").disabled,
      homeDisabled: document.querySelector("#w-home").disabled,
      awayDisabled: document.querySelector("#w-away").disabled,
      divOptions: Array.from(document.querySelectorAll("#w-div option")).map((o) => o.value),
    }));
    if (postSelect.divDisabled || postSelect.homeDisabled || postSelect.awayDisabled) {
      throw new Error(`[${viewport.label}] downstream controls stayed disabled after choosing a League: ${JSON.stringify(postSelect)}`);
    }
    if (postSelect.divOptions.includes(ids.dOther)) {
      throw new Error(`[${viewport.label}] L_A's Division picker leaked L_B's own division: ${JSON.stringify(postSelect.divOptions)}`);
    }
    if (postSelect.divOptions.includes(ids.d2)) {
      throw new Error(`[${viewport.label}] Division picker leaked the OTHER season's division while season 1 was active: ${JSON.stringify(postSelect.divOptions)}`);
    }

    const homeOptions = async () => page.$$eval(
      "#w-home option", (opts) => opts.map((o) => o.value));

    // In d1, Perma IS registered → offered.
    await page.selectOption("#w-div", ids.d1);
    await page.waitForFunction(
      (p) => Array.from(document.querySelectorAll("#w-home option"))
        .some((o) => o.value === p), ids.perma, { timeout: 10000 });

    // In d1b, Perma is NOT registered → must not be offered (legacy division_id
    // would have wrongly listed it; registrations do not).
    await page.selectOption("#w-div", ids.d1b);
    await page.waitForFunction(
      (p) => !Array.from(document.querySelectorAll("#w-home option"))
        .some((o) => o.value === p), ids.perma, { timeout: 10000 });
    const inD1b = await homeOptions();
    if (inD1b.includes(ids.perma)) {
      throw new Error(`[${viewport.label}] Perma was offered in a division it isn't registered in`);
    }

    // (C) #233 B2c review: the no-Division path still creates a valid
    // league-only game, and the v2 create payload carries exactly the
    // League chosen with a null division_id (never omitted, never a stray
    // legacy field). Reopen the wizard on the same (still-available) slot.
    await page.click("[data-wizcancel]");
    await page.waitForFunction(() => !document.querySelector(".wizard"), null, { timeout: 10000 });
    await page.click(`[data-slot="${ids.slot}"]`);
    await page.waitForSelector("#w-league", { timeout: 10000 });
    await page.selectOption("#w-league", ids.lv1);
    await page.waitForFunction(
      (t) => !!Array.from(document.querySelectorAll("#w-home option")).find((o) => o.value === t),
      ids.leagueOnly, { timeout: 10000 });
    // Division stays at its default "No division" — a league-only game.
    await page.selectOption("#w-home", ids.leagueOnly);
    await page.selectOption("#w-away", ids.perma);
    const createReq = page.waitForRequest((r) =>
      r.url() === `${base}/api/v2/setup/game` && r.method() === "POST");
    await page.click("[data-wizcreate]");
    const createBody = (await createReq).postDataJSON();
    if (createBody.league_id !== ids.lv1 || createBody.division_id !== null) {
      throw new Error(`[${viewport.label}] league-only wizard create sent an unexpected payload: ${JSON.stringify(createBody)}`);
    }
    await page.waitForFunction(() => !document.querySelector(".wizard"), null, { timeout: 10000 });

    // (D) #283 Slice D: an Exhibition (friendly) is Season-scoped — the toggle
    // swaps the League/Division cascade for a Season picker, offers every team
    // registered in that Season (regardless of League), and the create payload
    // carries game_type="exhibition" with NO league_id (a friendly has no
    // owning League and never counts toward standings). Creating the previous
    // game navigated to the games view, so return to the calendar first.
    await page.click('.tab[data-tab="calendar"]');
    await page.waitForSelector(`[data-slot="${ids.slot2}"]`, { timeout: 15000 });
    await page.click(`[data-slot="${ids.slot2}"]`);
    await page.waitForSelector("#w-exhibition", { timeout: 10000 });
    await page.check("#w-exhibition");
    await page.waitForSelector("#w-season", { timeout: 10000 });
    // The Season picker offers exactly the ACTIVE Season and nothing else:
    // since #369 the wizard's read is ceilinged on it, so the "exactly one
    // unambiguous choice" auto-select always fires here. What must hold is that
    // the friendly can only ever be created in the Season the operator actually
    // switched to — never blank, and never the other Season, whose teams are
    // out of scope entirely. (The "never implicitly pick one of several" rule
    // still has real force on the League axis, asserted in B0 above: `ov.levels`
    // is Program-wide, so both Leagues are offered there.)
    const exSeason = await page.$eval("#w-season", (s) => ({
      value: s.value,
      options: Array.from(s.options).map((o) => o.value),
    }));
    if (exSeason.value !== ids.s1 || exSeason.options.length !== 1
        || exSeason.options[0] !== ids.s1) {
      throw new Error(`[${viewport.label}] Exhibition Season picker was not scoped to the active Season: ${JSON.stringify(exSeason)}`);
    }
    await page.selectOption("#w-season", ids.s1);
    await page.waitForFunction(
      (t) => Array.from(document.querySelectorAll("#w-home option")).some((o) => o.value === t),
      ids.perma, { timeout: 10000 });
    await page.selectOption("#w-home", ids.perma);
    await page.selectOption("#w-away", ids.leagueOnly);
    const exReq = page.waitForRequest((r) =>
      r.url() === `${base}/api/v2/setup/game` && r.method() === "POST");
    await page.click("[data-wizcreate]");
    const exBody = (await exReq).postDataJSON();
    if (exBody.game_type !== "exhibition" || exBody.league_id !== undefined
        || exBody.season_id !== ids.s1) {
      throw new Error(`[${viewport.label}] exhibition create sent an unexpected payload: ${JSON.stringify(exBody)}`);
    }
    await page.waitForFunction(() => !document.querySelector(".wizard"), null, { timeout: 10000 });

    // (E) The cross-season half, reached the supported way (#369): switch the
    // REAL context bar to season 2 and work the SAME permanent Team there. Its
    // s2 registration puts it in d2 — a different Division than in s1 — so the
    // wizard must now offer d2 (and only L_A's s2 divisions), offer Perma in it,
    // and no longer offer mateA, whose registration is s1's. That is the same
    // one-Team/two-Seasons/two-Divisions fact assertion (A) reads from the
    // registration API, proven here through the operational UI.
    // No game is created in this step: `ov.levels` serializes ONE season_id per
    // League, so the wizard's regular-game payload derives its Season from the
    // League's first-bound Season — an s2 create through the wizard is a
    // separate concern this journey does not assert.
    await switchSeason(page, viewport.label, ids.league, ids.s2);
    await page.click('.tab[data-tab="calendar"]');
    await page.waitForSelector(`[data-slot="${ids.slot3}"]`, { timeout: 15000 });
    await page.click(`[data-slot="${ids.slot3}"]`);
    await page.waitForSelector("#w-league", { timeout: 10000 });
    await page.selectOption("#w-league", ids.lv1);
    await page.waitForFunction(
      (d) => !!Array.from(document.querySelectorAll("#w-div option")).find((o) => o.value === d),
      ids.d2, { timeout: 10000 });
    const s2Divs = await page.$$eval("#w-div option", (opts) => opts.map((o) => o.value));
    if (s2Divs.includes(ids.d1) || s2Divs.includes(ids.d1b)) {
      throw new Error(`[${viewport.label}] season 1's divisions were still offered after switching to season 2: ${JSON.stringify(s2Divs)}`);
    }
    await page.selectOption("#w-div", ids.d2);
    await page.waitForFunction(
      (p) => Array.from(document.querySelectorAll("#w-home option"))
        .some((o) => o.value === p), ids.perma, { timeout: 10000 });
    const inD2 = await homeOptions();
    if (!inD2.includes(ids.perma)) {
      throw new Error(`[${viewport.label}] the permanent Team was not offered in its season-2 division`);
    }
    if (inD2.includes(ids.mateA)) {
      throw new Error(`[${viewport.label}] a season-1-only team was offered in a season-2 division: ${JSON.stringify(inD2)}`);
    }
    await page.click("[data-wizcancel]");
    await page.waitForFunction(() => !document.querySelector(".wizard"), null, { timeout: 10000 });

    if (errors.length) {
      throw new Error(`[${viewport.label}] console/page errors:\n${errors.join("\n")}`);
    }
    console.log(`[${viewport.label}] OK — one team across two seasons (each reached by switching the real context bar); wizard filters by registration; no implicit League selection; Exhibition friendly is season-scoped.`);
  } catch (error) {
    // Keep the original error (and its stack) so a timeout names the failing
    // request/selector; just append the server tail to its message.
    error.message = `${error.message}\n--- demo server output ---\n${serverOutput}`;
    throw error;
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
    console.log("Team/division participation browser journey passed.");
  } catch (error) {
    console.error("Team/division participation browser journey FAILED.");
    console.error(error && error.message ? error.message : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
