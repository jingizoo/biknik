// The context bar governs Games, Roster AND Standings -- the cross-view
// property the topbar scope note is about (#345, browser layer).
//
// WHY THIS FILE EXISTS. The persistent note beside the context selects has
// now been wrong twice. It shipped as "display only · screens not filtered",
// was narrowed to "most existing screens (Games, Roster, Standings, etc.) are
// not filtered by this selection", and BOTH sentences were false statements
// about a head where those exact screens were scoped. The e2e assertion that
// was supposed to protect this copy matched its wording, which is precisely
// why it never caught either lie: a text match cannot tell "the copy changed"
// from "the copy became false". So the wording is deliberately NOT pinned
// anywhere -- context-switcher.js asserts only that a non-empty scope note is
// visible, and this file pins the PROPERTY the note is about, by behaviour:
//
//   switching the active context through the REAL controls changes what
//   Games, Roster and Standings show, in both directions.
//
// A property no rewording can quietly invalidate. If someone reverts the
// scoping, this fails; if someone rewrites the caption, this is untouched --
// which is the correct split, because the caption is not the contract.
//
// The axis here is TWO PROGRAMS, each with its own Season and League. That is
// deliberately the coarsest possible switch: nothing below can pass because
// two Seasons happen to share a League, or because a client-side League
// filter happened to run. Every record in each Program carries its own
// "Alpha"/"Bravo" token so every assertion runs in BOTH directions -- the
// active Program's records PRESENT (the positive control) and the other
// Program's records ABSENT (the negative one). A negative assertion alone
// proves nothing: "Games shows only Bravo's games" is satisfied by a Games
// screen that is simply broken and empty, so it is only ever asserted
// alongside the paired check that Alpha's games ARE there before the switch
// and ARE there again after switching back.
//
// Explicitly NOT re-tested here (covered elsewhere, not duplicated):
//   * The context bar's own mechanics -- persistence, #ctx= hash, deep-link
//     adopt/normalize, archived read-only, out-of-order response
//     reconciliation, the boot window -- context-switcher.js owns all of it.
//   * The League axis WITHIN one Season, and the Setup hub's client-side
//     League narrowing -- league-filtered-data.js owns those.
//   * The Season CEILING within one Program, and the Dashboard's own tiles --
//     dashboard-season-ceiling.js owns those.
//   * The backend scoping/authorization rules themselves -- the four
//     backend/tests/test_league_filtered_*.py matrices own those, across
//     every configured store.
// What is left, and is covered nowhere else, is the cross-VIEW half: that
// Games, Roster and Standings each respect a Program switch, that Roster
// drops a game selected under the old context rather than carrying it, and
// that a response generated under the OLD context cannot repaint the new one.
//
// Fails on any unexpected browser console/page error, and on horizontal page
// overflow. Runs the whole scenario at desktop (1440x900) and canonical
// 390x844.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8451 },
  { label: "phone", width: 390, height: 844, port: 8452 },
];

// How long a captured old-context response is held before delivery. Long
// enough that the switch below completes and the new context finishes
// painting first, so the stale body is genuinely delivered LATE.
const HOLD_MS = 3500;

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
function startServer(port, env) {
  return spawn(
    process.env.PYTHON || "python3",
    ["-u", "-m", "hockey_scheduler.web.server", "--host", HOST, "--port", String(port)],
    { cwd: BACKEND_DIR, stdio: ["ignore", "pipe", "pipe"], env: { ...process.env, ...env } });
}

function apiPost(page, url, body) {
  return page.evaluate(async ([u, b]) => {
    const r = await fetch(u, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(b || {}),
    });
    return { status: r.status, json: await r.json().catch(() => ({})) };
  }, [url, body]);
}
const loginAs = (page, username) =>
  apiPost(page, "/api/auth/login", { username, password: "demo" });

async function newPage(browser, viewport) {
  const context = await browser.newContext({
    viewport: { width: viewport.width, height: viewport.height },
  });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(`[pageerror] ${e.message}`));
  page.on("console", (m) => {
    if (m.type() === "error" && !/Failed to load resource/.test(m.text())) {
      errors.push(`[console] ${m.text()}`);
    }
  });
  return { context, page, errors };
}

function fail(msg) { throw new Error(msg); }

// ---------------------------------------------------------------------------
// Fixture: TWO complete, independent Programs -- the coarsest context switch
// there is. Each owns its own operator Organization, Season, League,
// Division, two Teams, Venue (granted to its OWN Season), Rink, ice slots and
// TWO published games with FINAL results.
//
// Every record in a Program is named with that Program's token ("Alpha"/
// "Bravo"), so every assertion below can be made by NAME in both directions
// rather than by count -- a count-only check passes just as happily on an
// empty screen as on a correctly-scoped one.
//
// TWO games per Program, not one, is load-bearing twice over: rosterGamePicker()
// only renders its <select> when more than one game is accessible (so a
// one-game fixture would silently skip the picker assertions entirely), and a
// second game is what makes "the previously selected game is dropped"
// distinguishable from "there was only ever one game to show".
//
// Built as the League Admin (the only role with manage_setup), then handed to
// a global ARENA MANAGER for the scenario itself. That role is what makes this
// journey possible at all: it is authorized for every Program, it can reach
// Games, Roster (canReadAnyPrivateGame) and Standings, and -- unlike League
// Admin -- it never triggers the client-onboarding wizard, which otherwise
// takes over #content and hides the context bar outright. A two-Program
// installation cannot satisfy that wizard by construction (its checks are
// installation-wide and it lands on whichever Program is short), so building
// around it rather than fighting it is the established convention here; see
// league-context-bar.js's own note on Arena Manager for the same reasoning.
//
// Venue-access grants are Season-BOUND and bind to the ACTIVE Season (#367
// ruling, enforced since #372), so each grant selects its own destination
// first. The context is left on Alpha, which is where the scenario starts.
async function buildFixture(page) {
  return page.evaluate(async () => {
    const post = async (p, b) => {
      const r = await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      });
      const json = await r.json().catch(() => ({}));
      if (!r.ok || (json && json.error)) {
        throw new Error(`${p} -> ${r.status} ${JSON.stringify(json)}`);
      }
      return json;
    };

    // One Program's whole world, built under its own token.
    const buildProgram = async (token, slotDates) => {
      const org = await post("/api/v2/setup/organization", { name: `${token} Org` });
      const program = await post("/api/v2/setup/program",
        { name: `${token} Program`, operator_organization_id: org.id });
      const season = await post("/api/v2/setup/season",
        { program_id: program.id, name: `${token} Season` });
      // Enter this Program/Season BEFORE building into it. Setup writes are
      // gated on the active context (#369's parent-id write gate): creating a
      // Team under a League the active tuple does not cover is refused with
      // "League not found or not accessible for this context". With two
      // Programs in play there is no ambient default that could be right for
      // both, so each one is entered explicitly -- which is also what an
      // operator actually does.
      await post("/api/context", { program_id: program.id, season_id: season.id });
      const league = await post("/api/v2/setup/league",
        { season_id: season.id, name: `${token} League` });
      const club = await post("/api/v2/setup/club", { name: `${token} Club` });
      const home = await post("/api/v2/setup/team",
        { club_id: club.id, name: `${token} Home`, league_id: league.id });
      const away = await post("/api/v2/setup/team",
        { club_id: club.id, name: `${token} Away`, league_id: league.id });
      const division = await post("/api/v2/setup/division",
        { league_id: league.id, name: `${token} Division`, season_id: season.id });
      await post(`/api/v2/setup/seasons/${season.id}/team-registrations`,
        { team_id: home.id, league_id: league.id, division_id: division.id });
      await post(`/api/v2/setup/seasons/${season.id}/team-registrations`,
        { team_id: away.id, league_id: league.id, division_id: division.id });

      const venue = await post("/api/v2/setup/venue",
        { name: `${token} Venue`, organization_id: org.id });
      // The grant binds to the ACTIVE Season, which the context POST above
      // already put on this Program's own Season.
      const granted = await post(`/api/v2/setup/seasons/${season.id}/venue-access`,
        { venue_id: venue.id });
      if (!granted || !granted.id) {
        throw new Error(`${token} venue-access grant did not succeed: `
          + JSON.stringify(granted));
      }
      const rink = await post("/api/v2/setup/rink",
        { venue_id: venue.id, name: `${token} Rink` });
      // Far-future dates, one game per day: the Games screen groups by day and
      // the Dashboard falls back to every scheduled game when none is "today",
      // so dates that can never be today keep this stable whenever it runs.
      const games = [];
      for (const date of slotDates) {
        const slot = await post("/api/v2/setup/ice-slot", {
          rink_id: rink.id,
          start_time: `${date}T18:00:00+00:00`, end_time: `${date}T20:00:00+00:00`,
        });
        const g = await post("/api/v2/setup/game", {
          season_id: season.id, league_id: league.id, division_id: division.id,
          home_team_id: home.id, away_team_id: away.id, ice_slot_id: slot.id,
        });
        // Published + a recorded, APPROVED (FINAL) result: only a FINAL result
        // reaches standings, which is one of the three screens under test.
        await post(`/api/games/${g.id}/publish`, {});
        await post(`/api/games/${g.id}/result`, { home_score: 3, away_score: 1 });
        await post(`/api/games/${g.id}/result/approve`, {});
        games.push(g.id);
      }
      return {
        program: program.id, season: season.id, league: league.id,
        division: division.id, games,
      };
    };

    const bravo = await buildProgram("Bravo", ["2030-03-05", "2030-03-06"]);
    const alpha = await buildProgram("Alpha", ["2030-01-05", "2030-01-06"]);
    // The account the scenario itself runs as. Global scope: authorized for
    // both Programs, so the switch below is a real operator choice rather
    // than an authorization side effect.
    const operator = await post("/api/accounts", {
      username: `scope_ops_${Date.now()}`, password: "demo",
      role: "arena_manager", scope: {},
    });
    return { alpha, bravo, operator: operator.username };
  });
}

// The two tuples, and the token each one's records carry. `absent` is the
// OTHER tuple's token -- every assertion checks both.
const EXPECT = {
  Alpha: { token: "Alpha", absent: "Bravo" },
  Bravo: { token: "Bravo", absent: "Alpha" },
};

// ---------------------------------------------------------------------------
// Navigation + repaint helpers.
// ---------------------------------------------------------------------------

// render() replaces #content wholesale, so a stamp on the current view cannot
// survive a repaint -- a real happened-after signal rather than a guessed
// sleep. Every read below waits for a FRESH paint before asserting.
function stampContent(page) {
  return page.evaluate(() => {
    const el = document.querySelector("#content > *");
    if (el) el.dataset.scopeStamp = "1";
  });
}
async function waitForFreshContent(page, step) {
  await page.waitForFunction(() => {
    const el = document.querySelector("#content > *");
    return !!el && el.dataset.scopeStamp !== "1"
      && !document.querySelector("#content .skeleton");
  }, null, { timeout: 20000 }).catch(() => fail(
    `[${step}] #content never repainted`));
}

async function gotoView(page, view, step) {
  await page.click(`.tab[data-tab="${view}"]`);
  await page.waitForFunction((v) => document.body.dataset.view === v, view,
    { timeout: 10000 }).catch(async () => fail(
      `[${step}] never reached the "${view}" view (was `
      + `"${await page.evaluate(() => document.body.dataset.view)}")`));
  await page.waitForFunction(
    () => !document.querySelector("#content .skeleton"),
    null, { timeout: 20000 }).catch(() => fail(
      `[${step}] the "${view}" view never finished loading`));
}

// The page must never scroll sideways at either viewport (#345 breakpoint
// rule). Checked after every repaint this file drives, since a leaked
// wide row from the other context is exactly the kind of thing that would
// blow the layout out at 390px.
async function assertNoHorizontalOverflow(page, step) {
  const l = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  }));
  if (l.scrollWidth > l.clientWidth) {
    fail(`[${step}] horizontal page overflow -- scrollWidth ${l.scrollWidth} `
      + `> clientWidth ${l.clientWidth}`);
  }
}

// ---------------------------------------------------------------------------
// The REAL keyboard switch, through the REAL control.
//
// The whole point of this journey is that the operator's own context bar --
// not a POST, not localStorage -- is what drives the change, so the switch is
// made by focusing #ctx-select and pressing arrow keys, and focus is asserted
// to survive it (the K5 contract). The macOS fallback below is copied
// deliberately from context-switcher.js and carries the same guarantee: it
// only ever engages after PROVING the keystroke reached the select, so a real
// focus/wiring regression still fails here rather than being papered over.
// ---------------------------------------------------------------------------
async function keyboardSwitchTo(page, targetValue, step) {
  await page.focus("#ctx-select");
  const focused = await page.evaluate(
    () => document.activeElement && document.activeElement.id);
  if (focused !== "ctx-select") {
    fail(`[${step}] #ctx-select is not keyboard-focusable (active="${focused}")`);
  }
  const plan = await page.evaluate((target) => {
    const s = document.getElementById("ctx-select");
    const values = Array.from(s.options).map((o) => o.value);
    return { from: values.indexOf(s.value), to: values.indexOf(target), values };
  }, targetValue);
  if (plan.to < 0) {
    fail(`[${step}] "${targetValue}" is not an option in #ctx-select `
      + `(options: ${JSON.stringify(plan.values)})`);
  }
  if (plan.from === plan.to) {
    fail(`[${step}] #ctx-select is already on "${targetValue}" -- this switch `
      + "would prove nothing");
  }
  // Record keydowns ON the select so the fallback can distinguish "the app
  // never received the keystroke" (a REAL regression -- must still fail) from
  // "the browser's own <select> widget ignored it" (a macOS harness quirk).
  await page.evaluate(() => {
    window.__scopeKeys = [];
    document.getElementById("ctx-select")
      .addEventListener("keydown", (e) => window.__scopeKeys.push(e.key));
  });
  const key = plan.to > plan.from ? "ArrowDown" : "ArrowUp";
  const steps = Math.abs(plan.to - plan.from);
  for (let i = 0; i < steps; i += 1) await page.keyboard.press(key);

  // LOCAL-ONLY HARNESS WORKAROUND -- READ BEFORE "SIMPLIFYING" THIS.
  // On macOS, Chromium renders <select> with the native Cocoa popup and a
  // CLOSED select does not respond to Arrow keys via CDP-injected key events:
  // the keydown IS delivered to the element (the listener above records it)
  // but the widget never moves the selection. On Linux -- CI, where this gate
  // actually runs -- the same press DOES move it, so the keyboard path itself
  // is exercised there. Decided in ~1.5s rather than by a full timeout.
  let movedByKeyboard = true;
  try {
    await page.waitForFunction((v) =>
      document.getElementById("ctx-select").value === v,
    targetValue, { timeout: 1500 });
  } catch (_) {
    movedByKeyboard = false;
  }
  if (!movedByKeyboard) {
    const delivered = await page.evaluate(() => (window.__scopeKeys || []).slice());
    if (!delivered.includes(key)) {
      fail(`[${step}] ${key} never reached #ctx-select (keydowns seen: `
        + `${JSON.stringify(delivered)}) -- the select is focused but not `
        + "receiving keyboard input, which is a real regression, not the "
        + "macOS widget quirk");
    }
    console.warn(`[${step}] NOTE: this browser's <select> widget ignored ${key} `
      + `on a closed select (known macOS-Chromium behaviour; the keydown WAS `
      + `delivered to #ctx-select). Driving the same change via selectOption so `
      + `the rest of this scenario still runs. On Linux/CI the keyboard path `
      + `itself is exercised.`);
    await page.selectOption("#ctx-select", targetValue);
    await page.waitForFunction((v) =>
      document.getElementById("ctx-select").value === v,
    targetValue, { timeout: 10000 }).catch(() => fail(
      `[${step}] the context select never settled on "${targetValue}"`));
  }
  // K5's own contract: operating the select must not throw focus elsewhere.
  const stillFocused = await page.evaluate(
    () => document.activeElement && document.activeElement.id);
  if (stillFocused !== "ctx-select") {
    fail(`[${step}] focus left #ctx-select during the switch `
      + `(now on "${stillFocused}")`);
  }
}

// ---------------------------------------------------------------------------
// The three screens, read off the REAL painted views.
// ---------------------------------------------------------------------------

// Games: every row's matchup + division, the "Showing N of M" count, and the
// division/team filter option lists (which are built from the payload, so a
// leak shows up there even when no row happens to render).
async function readGames(page, step) {
  await page.waitForSelector(".games-count", { timeout: 20000 }).catch(() => fail(
    `[${step}] the Games screen never rendered its count`));
  return page.evaluate(() => {
    const txt = (el) => (el ? (el.textContent || "").trim() : "");
    const optionTexts = (sel) => Array.from(
      document.querySelectorAll(`${sel} option`)).map((o) => txt(o));
    return {
      rows: Array.from(document.querySelectorAll(".games-row")).map((r) => ({
        title: txt(r.querySelector(".li-title")),
        sub: txt(r.querySelector(".li-sub")),
      })),
      count: txt(document.querySelector(".games-count")),
      divisionOptions: optionTexts("#games-f-div"),
      teamOptions: optionTexts("#games-f-team"),
      text: txt(document.getElementById("content")),
    };
  });
}

// Roster: the selected game's own header (matchup + division), the game
// picker's option list, and the two lineup tabs' team names -- i.e. both what
// the screen OFFERS and what it actually LOADED.
async function readRoster(page, step) {
  // A bare selector timeout here says nothing about WHY -- the roster renders
  // several different shapes (empty state, restricted guard, a picker-less
  // head when the selected game is not in the payload), and telling them
  // apart is the whole point when this fails.
  await page.waitForSelector(".roster-game-head", { timeout: 20000 })
    .catch(async () => fail(
      `[${step}] the Roster screen never rendered its game header. `
      + `#content was: ${JSON.stringify(await page.evaluate(() => {
        const c = document.getElementById("content");
        return c ? (c.textContent || "").trim().slice(0, 400) : null;
      }))}`));
  return page.evaluate(() => {
    const txt = (el) => (el ? (el.textContent || "").trim() : "");
    const picker = document.getElementById("roster-game");
    return {
      title: txt(document.querySelector(".rg-title")),
      sub: txt(document.querySelector(".rg-sub")),
      pickerOptions: picker
        ? Array.from(picker.options).map((o) => ({ value: o.value, label: txt(o) }))
        : null,
      pickerValue: picker ? picker.value : null,
      lineupTeams: Array.from(document.querySelectorAll(".ls .ls-team")).map((e) => txt(e)),
      text: txt(document.getElementById("content")),
    };
  });
}

// Standings: the division picker's option list (what is OFFERED) and the
// table's team rows (what is RENDERED) -- the brief's two halves.
async function readStandings(page, step) {
  await page.waitForSelector("#standings-div", { timeout: 20000 }).catch(() => fail(
    `[${step}] the Standings screen never rendered its division picker`));
  return page.evaluate(() => {
    const txt = (el) => (el ? (el.textContent || "").trim() : "");
    const sel = document.getElementById("standings-div");
    return {
      divisionOptions: Array.from(sel.options).map((o) => txt(o)),
      rows: Array.from(document.querySelectorAll(".st-table .st-team"))
        .map((e) => txt(e))
        .filter((t) => t && t !== "Team"),
      text: txt(document.getElementById("content")),
    };
  });
}

// Both directions, over a whole painted view: the active tuple's token must
// appear, and the other tuple's must appear NOWHERE. Pairing these is what
// keeps the negative half from passing vacuously on a blank screen.
function assertTokens(step, what, text, tuple) {
  const e = EXPECT[tuple];
  if (!text.includes(e.token)) {
    fail(`[${step}] ${what}: nothing named "${e.token}" appears at all -- the `
      + "screen is empty or broken, so the 'other Program is absent' half "
      + "below would pass vacuously");
  }
  if (text.includes(e.absent)) {
    const leak = text.match(new RegExp(`${e.absent}[A-Za-z0-9 ]*`, "g")) || [];
    fail(`[${step}] ${what}: the other Program's data leaked in: `
      + JSON.stringify(Array.from(new Set(leak)).slice(0, 8)));
  }
}

function assertGames(step, g, tuple) {
  const e = EXPECT[tuple];
  if (!g.rows.length) {
    fail(`[${step}] Games rendered no game rows at all (count line: `
      + `"${g.count}") -- with two published games in the active Program this `
      + "is a broken screen, not a correctly narrowed one");
  }
  for (const row of g.rows) {
    if (!row.title.includes(e.token) || !row.sub.includes(e.token)) {
      fail(`[${step}] Games row ${JSON.stringify(row)} is not one of `
        + `${e.token}'s games`);
    }
  }
  // "Showing N of M": M is the payload's own total, so a leak inflates it even
  // when a client-side filter would have hidden the extra rows.
  const m = /Showing (\d+) of (\d+) game/.exec(g.count);
  if (!m) fail(`[${step}] Games count line unreadable: "${g.count}"`);
  if (m[1] !== "2" || m[2] !== "2") {
    fail(`[${step}] Games must show exactly this Program's 2 games, got `
      + `"${g.count}" -- 4 would mean both Programs' schedules were unioned`);
  }
  assertTokens(step, "Games division filter",
    g.divisionOptions.join(" | "), tuple);
  assertTokens(step, "Games team filter", g.teamOptions.join(" | "), tuple);
  assertTokens(step, "the Games screen", g.text, tuple);
}

function assertRoster(step, r, tuple) {
  const e = EXPECT[tuple];
  if (!r.title.includes(e.token)) {
    fail(`[${step}] the Roster header names "${r.title}", which is not one of `
      + `${e.token}'s games`);
  }
  if (!r.lineupTeams.length) {
    fail(`[${step}] the Roster loaded no lineups at all -- an empty screen `
      + "cannot demonstrate that it loaded the RIGHT game");
  }
  for (const team of r.lineupTeams) {
    if (!team.includes(e.token)) {
      fail(`[${step}] a loaded lineup is for "${team}", which belongs to the `
        + "other Program -- the per-game lineup read used a stale game id");
    }
  }
  if (!r.pickerOptions || r.pickerOptions.length !== 2) {
    fail(`[${step}] the Roster game picker must offer exactly this Program's 2 `
      + `games, got ${JSON.stringify(r.pickerOptions)}`);
  }
  for (const o of r.pickerOptions) {
    if (!o.label.includes(e.token)) {
      fail(`[${step}] the Roster picker offers "${o.label}", a game from the `
        + "other Program");
    }
  }
  assertTokens(step, "the Roster screen", r.text, tuple);
}

function assertStandings(step, s, tuple) {
  const e = EXPECT[tuple];
  if (s.divisionOptions.length !== 1 || !s.divisionOptions[0].includes(e.token)) {
    fail(`[${step}] Standings must offer only ${e.token}'s own division, got `
      + JSON.stringify(s.divisionOptions));
  }
  if (!s.rows.length) {
    fail(`[${step}] Standings rendered no team rows -- this Program's own FINAL `
      + "results never reached the table, so 'no other Program's rows' would "
      + "pass vacuously");
  }
  for (const row of s.rows) {
    if (!row.includes(e.token)) {
      fail(`[${step}] Standings row "${row}" belongs to the other Program `
        + `(rows: ${JSON.stringify(s.rows)})`);
    }
  }
  assertTokens(step, "the Standings screen", s.text, tuple);
}

// Read and assert all three screens for one tuple, leaving the page on
// Standings (where the held-response scenario picks up).
async function assertAllThree(page, tuple, step) {
  await gotoView(page, "games", `${step}/games`);
  assertGames(`${step}/games`, await readGames(page, `${step}/games`), tuple);
  await assertNoHorizontalOverflow(page, `${step}/games`);

  await gotoView(page, "roster", `${step}/roster`);
  assertRoster(`${step}/roster`, await readRoster(page, `${step}/roster`), tuple);
  await assertNoHorizontalOverflow(page, `${step}/roster`);

  await gotoView(page, "standings", `${step}/standings`);
  assertStandings(`${step}/standings`,
    await readStandings(page, `${step}/standings`), tuple);
  await assertNoHorizontalOverflow(page, `${step}/standings`);
}

// ---------------------------------------------------------------------------
// The scenario, at one viewport.
// ---------------------------------------------------------------------------
async function checkScopeTruth(browser, viewport) {
  const base = `http://${HOST}:${viewport.port}`;
  const server = startServer(viewport.port, {});
  let out = "";
  server.stdout.on("data", (d) => { out += d.toString(); });
  server.stderr.on("data", (d) => { out += d.toString(); });
  const { context, page, errors } = await newPage(browser, viewport);
  const L = viewport.label;
  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    if ((await loginAs(page, "admin")).status !== 200) {
      throw new Error(`[${L}] admin login failed`);
    }
    const f = await buildFixture(page).catch((e) => fail(
      `[${L}] fixture build failed: ${e.message || e}`));
    const alphaValue = `${f.alpha.program}|${f.alpha.season}`;
    const bravoValue = `${f.bravo.program}|${f.bravo.season}`;

    // Hand the session to the Arena Manager and start it on Alpha explicitly,
    // so every "before" assertion below rests on a stated context rather than
    // on whatever a fresh account's fallback happens to resolve to.
    if ((await loginAs(page, f.operator)).status !== 200) {
      throw new Error(`[${L}] arena manager login failed`);
    }
    if ((await apiPost(page, "/api/context",
      { program_id: f.alpha.program, season_id: f.alpha.season })).status !== 200) {
      throw new Error(`[${L}] could not start the operator on Alpha`);
    }
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 15000 });
    await page.waitForFunction(() => {
      const s = document.getElementById("ctx-select");
      return s && !s.hidden && s.options.length >= 2;
    }, null, { timeout: 15000 }).catch(() => fail(
      `[${L}] the context select never offered both Programs`));

    // The scope note itself: NON-LITERAL on purpose. Presence and visibility
    // only -- never wording. Freezing another sentence here is exactly the
    // mistake this whole journey exists to stop repeating; the cross-view
    // assertions below are what actually pin the behaviour it describes.
    const note = page.locator("#ctx-scope-note");
    if (!(await note.isVisible()) || !((await note.textContent()) || "").trim()) {
      fail(`[${L}] no visible, non-empty context scope note beside the selects `
        + `(text: ${JSON.stringify(await note.textContent())})`);
    }

    // The fixture left the context on Alpha; make that explicit rather than
    // assumed, since every "before" assertion depends on it.
    if ((await page.evaluate(() => document.getElementById("ctx-select").value))
        !== alphaValue) {
      await page.selectOption("#ctx-select", alphaValue);
      await page.waitForFunction((v) =>
        document.getElementById("ctx-select").value === v,
      alphaValue, { timeout: 10000 });
    }

    // -----------------------------------------------------------------------
    // (A) POSITIVE CONTROL, before anything is switched: with Alpha active,
    //     all three screens genuinely show Alpha's games/lineups/standings.
    //     Every "only Bravo's data" assertion later in this file is meaningless
    //     without this -- it is what proves the screens were capable of showing
    //     Alpha in the first place.
    // -----------------------------------------------------------------------
    await assertAllThree(page, "Alpha", `${L}/A alpha-before`);

    // -----------------------------------------------------------------------
    // (B) Pick Alpha's SECOND game on the Roster, deliberately, through the
    //     real picker. This is the "previously selected A game" the switch
    //     below must drop: without an explicit non-default pick, "the Roster
    //     shows a Bravo game afterwards" could just be app.js defaulting to
    //     schedule[0] and never proves the stale selection was discarded.
    // -----------------------------------------------------------------------
    await gotoView(page, "roster", `${L}/B roster`);
    const before = await readRoster(page, `${L}/B roster`);
    const second = before.pickerOptions.find((o) => o.value !== before.pickerValue);
    if (!second) {
      fail(`[${L}/B] the Roster picker offered no second Alpha game to select `
        + `(${JSON.stringify(before.pickerOptions)})`);
    }
    await stampContent(page);
    await page.selectOption("#roster-game", second.value);
    await waitForFreshContent(page, `${L}/B roster-repick`);
    const repicked = await readRoster(page, `${L}/B roster-repick`);
    if (repicked.pickerValue !== second.value) {
      fail(`[${L}/B] selecting Alpha's second game did not take (picker is on `
        + `"${repicked.pickerValue}", expected "${second.value}")`);
    }
    assertRoster(`${L}/B roster-repick`, repicked, "Alpha");
    const staleGameId = second.value;   // an Alpha game id, now the live selection

    // -----------------------------------------------------------------------
    // HOLDING AN OLD-CONTEXT RESPONSE -- and why it takes two passes.
    //
    // render() awaits GET /api/demo/overview and only THEN issues GET
    // /api/standings/<id>, so the two reads are never in flight together: hold
    // the overview and the standings request is simply never made (the
    // contextRevision guard returns first). One pass can therefore only ever
    // hold ONE of them. So each switch direction holds a different endpoint --
    // the overview on the way to Bravo, the standings on the way back to Alpha
    // -- and between them both reads are proven unable to repaint a context
    // the operator has already left.
    //
    // Delivery is delayed, never the request: route.fetch() runs the read for
    // real against the server while the OLD tuple is still active, so the body
    // genuinely belongs to that tuple -- asserted below, not assumed. Delaying
    // the request instead would resolve it against the NEW tuple and prove
    // nothing at all, since the response would never disagree with the screen.
    // -----------------------------------------------------------------------
    const holdState = { endpoint: null, taken: false, held: [] };
    await page.route(/\/api\/(demo\/overview|standings\/)/, async (route) => {
      const isOverview = /demo\/overview/.test(route.request().url());
      const want = isOverview ? "overview" : "standings";
      const response = await route.fetch();
      const body = await response.text().catch(() => "");
      if (holdState.endpoint === want && !holdState.taken) {
        holdState.taken = true;
        holdState.held.push({ what: want, body });
        await new Promise((r) => setTimeout(r, HOLD_MS));
      }
      await route.fulfill({ response, body });
    });
    // The held body has to actually carry the OLD tuple's data, or delivering
    // it late could not have repainted anything and the whole scenario is
    // vacuous. This is the positive control for the hold itself.
    const assertHeld = (step, what, token) => {
      const held = holdState.held[holdState.held.length - 1];
      if (!held || held.what !== what) {
        fail(`[${step}] expected to hold a ${what} response generated under `
          + `${token}, held ${JSON.stringify(holdState.held.map((h) => h.what))}`);
      }
      if (!held.body.includes(token)) {
        fail(`[${step}] the held ${what} response carries no ${token} data, so `
          + "delivering it late could not have repainted that context and this "
          + "scenario proves nothing");
      }
    };

    // -----------------------------------------------------------------------
    // (C) Hold ALPHA's overview, then switch to Bravo inside that window. The
    //     Alpha body lands after Bravo has already painted, and must not put
    //     Alpha back on screen.
    // -----------------------------------------------------------------------
    holdState.endpoint = "overview";
    holdState.taken = false;
    const alphaPaint = gotoView(page, "standings", `${L}/C standings-under-alpha`)
      .catch(() => { /* deliberately still in flight; asserted after release */ });
    await new Promise((r) => setTimeout(r, 500));   // let the read be issued + held

    await keyboardSwitchTo(page, bravoValue, `${L}/C keyboard-switch`);

    // Bravo must paint while Alpha's overview is still held.
    await page.waitForFunction(() => {
      const el = document.getElementById("content");
      return el && /Bravo/.test(el.textContent || "")
        && !document.querySelector("#content .skeleton");
    }, null, { timeout: 20000 }).catch(() => fail(
      `[${L}/C] Bravo never painted after the keyboard switch`));

    await new Promise((r) => setTimeout(r, HOLD_MS + 1500));   // let it land
    await alphaPaint;
    assertHeld(`${L}/C`, "overview", "Alpha");

    // -----------------------------------------------------------------------
    // (D) The stale Alpha overview has now been delivered. Bravo must still be
    //     on screen, on all three views -- and Roster must have dropped the
    //     Alpha game selected back in (B) rather than carrying it across.
    // -----------------------------------------------------------------------
    await assertAllThree(page, "Bravo", `${L}/D bravo-after-switch`);

    // Back to Roster specifically (assertAllThree finishes on Standings) for
    // the one check that is about the PREVIOUS selection rather than the
    // current one.
    await gotoView(page, "roster", `${L}/D roster`);
    const afterRoster = await readRoster(page, `${L}/D roster`);
    if (afterRoster.pickerValue === staleGameId
        || (afterRoster.pickerOptions || []).some((o) => o.value === staleGameId)) {
      fail(`[${L}/D] the Roster still holds Alpha's game ${staleGameId} after `
        + `switching to Bravo (picker value "${afterRoster.pickerValue}", options `
        + `${JSON.stringify(afterRoster.pickerOptions)})`);
    }

    // Force an UNRELATED repaint the way ordinary actions do, then re-check: a
    // stale response that corrupted shared state would surface on the NEXT
    // render rather than immediately (the #369 failure mode).
    await gotoView(page, "dashboard", `${L}/D dashboard-roundtrip`);
    await assertAllThree(page, "Bravo", `${L}/D bravo-after-roundtrip`);

    // -----------------------------------------------------------------------
    // (E) SYMMETRY, and the standings half of the hold. Leave Standings so the
    //     next visit issues a fresh read, arm the hold on STANDINGS this time
    //     (the overview passes straight through, which is what lets the
    //     standings request be issued at all), come back to Standings under
    //     Bravo, and switch to Alpha inside that window.
    //
    //     Alpha's data must come back in full -- proving nothing was destroyed,
    //     only scoped, which is the other half of every negative assertion
    //     above -- and Bravo's late standings body must not repaint its rows.
    // -----------------------------------------------------------------------
    await gotoView(page, "games", `${L}/E leave-standings`);
    holdState.endpoint = "standings";
    holdState.taken = false;
    const bravoPaint = gotoView(page, "standings", `${L}/E standings-under-bravo`)
      .catch(() => { /* deliberately still in flight; asserted after release */ });
    await new Promise((r) => setTimeout(r, 500));

    await keyboardSwitchTo(page, alphaValue, `${L}/E keyboard-switch-back`);
    await page.waitForFunction(() => {
      const el = document.getElementById("content");
      return el && /Alpha/.test(el.textContent || "")
        && !document.querySelector("#content .skeleton");
    }, null, { timeout: 20000 }).catch(() => fail(
      `[${L}/E] Alpha never repainted after switching back`));

    await new Promise((r) => setTimeout(r, HOLD_MS + 1500));
    await bravoPaint;
    assertHeld(`${L}/E`, "standings", "Bravo");

    await assertAllThree(page, "Alpha", `${L}/E alpha-again`);
    await page.unroute(/\/api\/(demo\/overview|standings\/)/);

    if (errors.length) {
      throw new Error(`[${L}] console/page errors:\n${errors.join("\n")}`);
    }
    console.log(`[${L}] OK — a keyboard context switch re-filters Games, Roster `
      + `and Standings in both directions; a held old-context response cannot `
      + `repaint the new one.`);
  } catch (error) {
    error.message = `${error.message}\n--- server output ---\n${out}`;
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
      process.env.SMOKE_CHROMIUM_PATH
        ? { executablePath: process.env.SMOKE_CHROMIUM_PATH } : {});
    for (const viewport of VIEWPORTS) {
      await checkScopeTruth(browser, viewport);
    }
    console.log("Context scope-truth browser journey passed.");
  } catch (error) {
    console.error("Context scope-truth browser journey FAILED.");
    console.error(error && error.stack ? error.stack : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
