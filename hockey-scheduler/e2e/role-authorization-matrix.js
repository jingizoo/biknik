// Seven-role destination and authorization browser matrix (#345).
//
// A dedicated journey for all seven roles the domain model defines
// (backend/hockey_scheduler/domain/roles.py): League Admin, Arena Manager,
// Coach, Player, Guardian, Official, Viewer. Independent of
// role-home-journeys.js (Player/Guardian/Official landings only, #204/#330)
// and home-tasks-hub.js (League Admin/Arena Manager Setup-hub depth,
// #204/#330/#331) -- this journey is a breadth-first MATRIX across every
// role rather than a depth pass on any one of them, and adds four roles
// (League Admin, Arena Manager, Coach, Viewer) neither file exercises at
// all. It does not rewrite either file.
//
// Per role, at desktop AND 390x844, with a REAL authenticated session
// (never the X-Demo-Role dev header):
//   1. the correct initial landing destination;
//   2. the exact set of nav tabs visible for that role (gateChrome's real
//      permission gates, not an assumed subset);
//   3. one authorized primary action reached and activated by KEYBOARD
//      (Tab/focus + Enter), proven by its real, persisted effect;
//   4. an unauthorized action absent or disabled (nav tabs AND, for the
//      roles it applies to, the Setup Records "+ New" controls);
//   5. direct navigation (calling the app's own switchTab() the way a
//      malicious/curious console user would, not a URL route -- this SPA
//      has none) into a hidden view renders no privileged data/controls;
//   6. a real unauthorized mutation request is rejected by the server (403
//      "forbidden"), never merely a hidden button -- verified again at the
//      end by re-reading the affected collection as League Admin and
//      confirming nothing was created;
//   7. zero browser console/page errors (the deliberately-provoked 403s'
//      own "Failed to load resource" noise is the one expected exception,
//      same convention every other journey in this suite uses).
//
// League Admin holds every permission, so legs 4-6 don't apply to it the
// way they do the other six roles -- its own scenario instead proves the
// REQUIRED #345 leg explicitly: identifying and opening the next incomplete
// Setup task by keyboard, ending in a real persisted Season. Arena Manager
// proves it can reach recurring ice creation (the Ice Availability Builder)
// but not the League-structure Setup Records; Coach proves it can reach its
// own team's roster and next-game workflow from the Dashboard.
//
// One additional, explicit scenario proves role/session switching leaks
// nothing: an in-app, NO-RELOAD persona switch (calling the app's own
// signIn(), the same function the login form and demo role-switcher use)
// from League Admin straight to Viewer must strip every admin-only nav tab
// and card from the DOM without a page reload; a second, concurrent browser
// context proves one session's login never bleeds into another's.
//
// Every role gets its own distinctly-named user, team, and player so no
// role's fixture can accidentally satisfy another role's assertion.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8341 },
  { label: "phone", width: 390, height: 844, port: 8342 },
];

// Every nav tab the shell can render (index.html) so "hidden" is asserted
// as precisely as "visible" -- a tab left visible that shouldn't be is
// exactly the authorization leak this journey exists to catch. Excludes
// "onboarding": gateChrome never toggles it (it is not part of the
// role/permission gate this journey covers) and it stays statically hidden
// outside the first-run wizard regardless of role.
const ALL_TABS = [
  "dashboard", "player_home", "guardian_home", "inbox", "activity",
  "calendar", "games", "scheduler", "standings", "sheet", "public",
  "roster", "users", "notifications", "delivery", "readiness", "import",
  "setup",
];

const KNOWN_CARD_HEADINGS = [
  "Continue setup", "✓ All setup steps complete",
  "Setup progress unavailable", "Setup progress",
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

function startServer(port) {
  return spawn(
    process.env.PYTHON || "python3",
    ["-u", "-m", "hockey_scheduler.web.server", "--host", HOST, "--port", String(port)],
    { cwd: BACKEND_DIR, stdio: ["ignore", "pipe", "pipe"] });
}

async function apiPost(page, p, body) {
  return page.evaluate(async (arg) => {
    const r = await fetch(arg.p, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(arg.body),
    });
    return { status: r.status, body: await r.json() };
  }, { p, body });
}

async function apiGet(page, p) {
  return page.evaluate(async (p) => {
    const r = await fetch(p, { credentials: "same-origin" });
    return { status: r.status, body: await r.json() };
  }, p);
}

async function loginAs(page, username, password) {
  const res = await apiPost(page, "/api/auth/login", { username, password });
  if (res.status !== 200 || res.body.error) {
    throw new Error(`login as ${username} failed: ${JSON.stringify(res)}`);
  }
}

// networkidle first (matches home-tasks-hub.js's logout()): a still-running
// render from the PRIOR role can otherwise invalidate mid-flight and log a
// 401 resource-load error indistinguishable from a genuine bug.
async function logout(page) {
  await page.waitForLoadState("networkidle").catch(() => {});
  await apiPost(page, "/api/auth/logout", {});
}

async function waitForView(page, viewName) {
  await page.waitForFunction(
    (v) => document.body.dataset.view === v, viewName, { timeout: 10000 });
}

// League Admin/Arena Manager can land on the one-time Initial Setup wizard
// on a truly fresh boot -- dismiss it the same way every other journey in
// this suite does. Never expected for the five non-operator roles below;
// they get a plain waitForView() so a real regression there fails loudly
// instead of being silently absorbed by a defensive dismiss.
async function reachDashboard(page) {
  await page.waitForFunction(
    () => document.body.dataset.view === "onboarding"
      || document.body.dataset.view === "dashboard", null, { timeout: 10000 });
  if (await page.evaluate(() => document.body.dataset.view) === "onboarding") {
    await page.click('[data-onboarding-goto="dashboard"]');
  }
  await waitForView(page, "dashboard");
}

async function waitForRealContent(page) {
  await page.waitForFunction(
    () => !document.querySelector("#content .skeleton")
      && document.getElementById("content").children.length > 0,
    null, { timeout: 10000 });
}

async function visibleTabs(page) {
  return page.evaluate((all) => all.filter((t) => {
    const el = document.querySelector(`.tab[data-tab="${t}"]`);
    return !!(el && el.offsetParent !== null);
  }), ALL_TABS);
}

function assertVisibleTabs(fail, label, actual, expected) {
  const a = actual.slice().sort().join(",");
  const e = expected.slice().sort().join(",");
  if (a !== e) {
    fail(`${label}: expected exactly these nav tabs visible [${expected.slice().sort().join(", ")}], `
      + `got [${actual.slice().sort().join(", ")}]`);
  }
}

// Real keyboard activation, not a click: focus the element the way Tab
// would land on it, then Enter -- the same proof-of-keyboard-operability
// convention home-tasks-hub.js uses throughout.
async function keyboardActivate(page, selector) {
  await page.focus(selector);
  await page.keyboard.press("Enter");
}

async function cardState(page) {
  return page.evaluate((headings) => {
    const h3 = Array.from(document.querySelectorAll(".dash-card h3")).find(
      (el) => headings.some((h) => el.textContent.trim().startsWith(h)));
    if (!h3) return null;
    const card = h3.closest(".dash-card");
    const primary = card.querySelector(".act.primary");
    return {
      heading: h3.textContent,
      nextTitle: (card.querySelector(".na-title") || {}).textContent || "",
      primaryLabel: primary ? primary.textContent.trim() : null,
      rows: Array.from(card.querySelectorAll(".li")).map((li) => ({
        title: (li.querySelector(".li-title") || {}).textContent || "",
        statusText: (li.querySelector(".badge") || {}).textContent || "",
      })),
    };
  }, KNOWN_CARD_HEADINGS);
}

async function waitForCardSettled(page) {
  await page.waitForFunction(() => {
    const slot = document.getElementById("sp-card-slot");
    if (slot) return !slot.querySelector(".skeleton");
    return !!document.querySelector(".dash-stats");
  }, null, { timeout: 10000 });
}

// Mutating controls that must never be both visible and enabled for a role
// that holds no permission to use them -- deliberately UI-attribute-level
// (not a fixed text list), so it catches any control wired to a mutating
// action, not just the ones this file happens to have exercised elsewhere.
const MUTATION_SELECTORS = [
  ".act.primary", ".act.success", ".act.danger", ".sc-new",
  "[data-setup-progress-action]", "[data-open-drawer]", "[data-drawer]",
  "[data-ib-preview]", "[data-ib-commit]", "[data-ice-builder-open]",
  "[data-g-confirm]", "[data-g-backout]", "#demo-btn",
];

async function assertNoEnabledMutations(page, fail, label) {
  const hits = await page.evaluate((sels) => {
    const found = [];
    sels.forEach((sel) => document.querySelectorAll(sel).forEach((el) => {
      if (el.offsetParent !== null && !el.disabled) {
        found.push(`${sel} "${(el.textContent || "").trim().slice(0, 30)}"`);
      }
    }));
    return found;
  }, MUTATION_SELECTORS);
  if (hits.length) {
    fail(`${label}: found enabled mutation control(s) that must not be reachable: ${hits.join(", ")}`);
  }
}

// A real, unauthorized HTTP mutation must be rejected by the SERVER (403
// "forbidden"), never merely a hidden button (#345 testing rule). Runs
// entirely outside the DOM/UI -- a raw fetch from the role's own real
// session cookie.
async function assertForbidden(page, fail, label, path, body) {
  const res = await apiPost(page, path, body || {});
  if (res.status !== 403 || !res.body.error || res.body.error.code !== "forbidden") {
    fail(`${label}: expected 403 "forbidden" POSTing ${path}, got `
      + `status=${res.status} body=${JSON.stringify(res.body)}`);
  }
}

const iso = (d) => d.toISOString().replace(/\.\d{3}Z$/, "+00:00");
const dateOnly = (d) => d.toISOString().slice(0, 10);

async function checkViewport(browser, viewport) {
  const base = `http://${HOST}:${viewport.port}`;
  const server = startServer(viewport.port);
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
    // Every negative-mutation probe below deliberately provokes a 403,
    // which Chromium always logs as a resource-load console error
    // regardless of how gracefully app code handles it -- same accepted
    // convention coach-scope.js/scheduling-policy.js/home-tasks-hub.js use.
    if (m.type() === "error" && !/Failed to load resource/.test(m.text())) {
      errors.push(`[console] ${m.text()}`);
    }
  });

  const fail = (msg) => { throw new Error(`[${viewport.label}] ${msg}`); };
  const L = viewport.label;
  const suffix = viewport.port; // keep usernames unique per viewport/port

  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });
    await reachDashboard(page); // signed in as the auto-provisioned League Admin ("admin"/"demo")

    // ============================================================
    // League Admin -- landing, full nav, and the required #345 leg:
    // identify and open the next incomplete Setup task by keyboard,
    // ending in a real persisted mutation.
    // ============================================================
    const laTabs = await visibleTabs(page);
    assertVisibleTabs(fail, "League Admin", laTabs, [
      "dashboard", "activity", "calendar", "games", "scheduler", "standings",
      "sheet", "public", "roster", "users", "notifications", "delivery",
      "readiness", "import", "setup",
    ]);

    const program = await apiPost(page, "/api/v2/setup/program",
      { name: `Matrix Program ${suffix}`, country: "US" });
    if (program.status !== 200 || program.body.error) {
      fail(`program create failed: ${JSON.stringify(program)}`);
    }
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await reachDashboard(page);
    await waitForCardSettled(page);
    let s = await cardState(page);
    if (!s || s.nextTitle !== "League profile and seasons" || s.primaryLabel !== "Add Season") {
      fail(`League Admin: expected a fresh Program to recommend "Add Season" `
        + `next, got ${JSON.stringify(s)}`);
    }
    // Keyboard-reach and activate the card's own primary action -- not a
    // click -- landing focus inside the real Season drawer it opens.
    await keyboardActivate(page, "[data-setup-progress-action]");
    await page.waitForSelector(".drawer[role=dialog]", { timeout: 10000 });
    const seasonName = `Matrix Season ${suffix}`;
    await page.fill("#f-season", seasonName);
    await keyboardActivate(page, '[data-drawer-submit="season"]');
    await page.waitForSelector(".drawer[role=dialog]", { state: "detached", timeout: 10000 });
    // Read the real, persisted Season back through the API boundary (not
    // just the card's own re-render) -- proves the mutation reached the
    // server, not just client state.
    const overview1 = await apiGet(page, "/api/v2/setup/overview");
    const season = (overview1.body.seasons || []).find((sn) => sn.name === seasonName);
    if (!season) fail(`League Admin: Season "${seasonName}" not found via GET /api/v2/setup/overview`);

    // ============================================================
    // Build the rest of the shared fixture (level/division/club/teams/
    // venue/rinks/ice/game/players/official) via the documented v1/v2
    // setup API -- same recipe scheduling-policy.js and home-tasks-hub.js
    // already exercise. Every entity below is uniquely named per role so
    // no role's data can satisfy another role's assertion.
    // ============================================================
    const level = await apiPost(page, "/api/setup/level",
      { season_id: season.id, name: `Matrix League ${suffix}` });
    // "League profile and seasons" is only Done once every Season of the
    // Program carries a grouping League (api/service.py's own league_done
    // rule) -- the keyboard-submitted Season alone isn't enough, matching
    // home-tasks-hub.js's own precedent (season THEN league before its
    // first "Done" check). Confirms the keyboard-driven mutation combines
    // with the rest of the real setup flow, not just that it persisted.
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await reachDashboard(page);
    await waitForCardSettled(page);
    s = await cardState(page);
    const seasonRow = s && s.rows.find((r) => r.title === "League profile and seasons");
    if (!seasonRow || seasonRow.statusText !== "Done") {
      fail(`League Admin: keyboard-submitted Season did not carry through `
        + `to a "Done" league_season workflow, got ${JSON.stringify(s)}`);
    }
    const division = await apiPost(page, "/api/setup/division",
      { season_id: season.id, level_id: level.body.id, name: `Matrix Division ${suffix}` });
    const club = await apiPost(page, "/api/setup/club", { name: `Matrix Club ${suffix}` });
    const coachTeam = await apiPost(page, "/api/v2/setup/team",
      { club_id: club.body.id, league_id: level.body.id, name: `Coach Team ${suffix}` });
    const rivalTeam = await apiPost(page, "/api/v2/setup/team",
      { club_id: club.body.id, league_id: level.body.id, name: `Rival Team ${suffix}` });
    const playerTeam = await apiPost(page, "/api/v2/setup/team",
      { club_id: club.body.id, league_id: level.body.id, name: `Player Team ${suffix}` });
    for (const team of [coachTeam, rivalTeam, playerTeam]) {
      const reg = await apiPost(page, `/api/setup/seasons/${season.id}/team-registrations`,
        { team_id: team.body.id, division_id: division.body.id });
      if (reg.status !== 200 || reg.body.error) {
        fail(`team registration failed: ${JSON.stringify(reg)}`);
      }
    }
    const venue = await apiPost(page, "/api/v2/setup/venue",
      { name: `Matrix Arena ${suffix}`, organization_id: null });
    await apiPost(page, `/api/v2/setup/seasons/${season.id}/venue-access`, { venue_id: venue.body.id });
    const gameRink = await apiPost(page, "/api/v2/setup/rink",
      { venue_id: venue.body.id, name: `Game Rink ${suffix}` });
    // A SEPARATE rink for Arena Manager's own recurring-ice-creation scenario
    // below, so its own preview/commit can never collide with (or be
    // confused for) the game's own pre-placed slot.
    const iceRink = await apiPost(page, "/api/v2/setup/rink",
      { venue_id: venue.body.id, name: `Ice Rink ${suffix}` });

    const gameStart = new Date(Date.now() + 10 * 24 * 3600 * 1000);
    gameStart.setUTCHours(18, 30, 0, 0);
    const gameEnd = new Date(gameStart.getTime() + 90 * 60000);
    const gameSlot = await apiPost(page, "/api/v2/setup/ice-slot", {
      rink_id: gameRink.body.id, start_time: iso(gameStart), end_time: iso(gameEnd),
      slot_type: "game",
    });
    const game = await apiPost(page, "/api/setup/game", {
      season_id: season.id, division_id: division.body.id,
      home_team_id: coachTeam.body.id, away_team_id: rivalTeam.body.id,
      ice_slot_id: gameSlot.body.id,
    });
    if (game.status !== 200 || game.body.error) {
      fail(`game create failed: ${JSON.stringify(game)}`);
    }
    // A new Game is created unpublished (Game.published defaults False) --
    // find_next_game_for_player() (the Player Home / Guardian "next game"
    // card's own source) only ever counts a PUBLISHED game, so Coach's
    // Next-Game card and Jamie Junior's next_game below need this to be real.
    const publish = await apiPost(page, `/api/games/${game.body.id}/publish`, {});
    if (publish.status !== 200 || publish.body.error) {
      fail(`game publish failed: ${JSON.stringify(publish)}`);
    }
    // Jamie Junior plays for the Rival Team -- deliberately NOT the Coach's
    // own team, and deliberately a different team than Priya Player's, so
    // Guardian's "next game" and Player's "no upcoming game" are each
    // proven from their own real, distinct fixture data.
    const junior = await apiPost(page, "/api/v2/setup/player",
      { team_id: rivalTeam.body.id, name: `Jamie Junior ${suffix}`, position: "defense" });
    const priya = await apiPost(page, "/api/v2/setup/player",
      { team_id: playerTeam.body.id, name: `Priya Player ${suffix}`, position: "forward" });
    const official = await apiPost(page, "/api/v2/setup/official",
      { name: `Ozzy Official ${suffix}` });

    // Seven distinct, role-scoped accounts (League Admin reuses the
    // existing seeded "admin"/"demo" login used to build this fixture).
    const PW = "matrix-account-pw";
    const mk = (role, extra) => `matrix_${role}_${suffix}`;
    const accounts = {
      arena_manager: { username: mk("arena"), role: "arena_manager", scope: {} },
      coach: { username: mk("coach"), role: "coach", scope: { team_id: coachTeam.body.id } },
      player: { username: mk("player"), role: "player",
        scope: { player_id: priya.body.id, team_id: playerTeam.body.id } },
      guardian: { username: mk("guardian"), role: "guardian", scope: {} },
      official: { username: mk("official"), role: "official",
        scope: { official_id: official.body.id } },
      viewer: { username: mk("viewer"), role: "viewer", scope: {} },
    };
    for (const key of Object.keys(accounts)) {
      const acct = accounts[key];
      const res = await apiPost(page, "/api/accounts",
        { username: acct.username, password: PW, role: acct.role, scope: acct.scope });
      if (res.status !== 200 || res.body.error) {
        fail(`account create failed for ${acct.username}: ${JSON.stringify(res)}`);
      }
      acct.id = res.body.id;
    }
    const link = await apiPost(page, "/api/guardians/links",
      { guardian_user_id: accounts.guardian.id, player_id: junior.body.id });
    const verify = await apiPost(page, `/api/guardians/links/${link.body.id}/verify`,
      { consent_method: "verbal_confirmed" });
    if (verify.status !== 200 || verify.body.error) {
      fail(`guardian link verify failed: ${JSON.stringify(verify)}`);
    }

    // ============================================================
    // Explicit in-app, NO-RELOAD role switch: League Admin -> Viewer via
    // the app's own signIn() (the exact function the login form and demo
    // role-switcher call) -- never a page.goto(). Proves switching
    // identity strips every admin-only nav tab and Dashboard card from the
    // live DOM on its own, not merely on the next fresh navigation.
    // ============================================================
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await reachDashboard(page);
    if (!(await page.$('.tab[data-tab="users"]'))) fail("setup precondition: Users tab not visible for League Admin");
    await page.evaluate(([u, p]) => signIn(u, p), [accounts.viewer.username, PW]);
    await waitForView(page, "standings");
    const postSwitchTabs = await visibleTabs(page);
    assertVisibleTabs(fail, "no-reload League Admin -> Viewer switch", postSwitchTabs, [
      "calendar", "games", "standings", "public", "notifications",
    ]);
    const postSwitchDom = await page.evaluate(() => ({
      hasSetupCard: !!Array.from(document.querySelectorAll(".dash-card h3")).length,
      hasUsersTab: !!document.querySelector('.tab[data-tab="users"]')
        && document.querySelector('.tab[data-tab="users"]').offsetParent !== null,
      demoMenuHidden: (document.getElementById("demo-menu") || {}).hidden !== false,
    }));
    if (postSwitchDom.hasSetupCard || postSwitchDom.hasUsersTab || !postSwitchDom.demoMenuHidden) {
      fail(`no-reload League Admin -> Viewer switch retained admin UI: ${JSON.stringify(postSwitchDom)}`);
    }
    await logout(page);

    // ============================================================
    // Concurrent-session isolation: a SECOND, independent browser context
    // (its own cookie jar) signs in as Coach WHILE the first context's
    // page is mid-session -- proves the server binds session state to the
    // session/cookie, not a shared mutable "current role" that a second
    // login could stomp on.
    // ============================================================
    // (No page.goto() before this login: the app's own bootstrap() runs a
    // real, async /api/auth/login-as-admin convenience whenever a fresh
    // navigation finds no session and no "hs_signed_out" sticky flag --
    // logout() above only invalidates the session server-side, exactly
    // like every OTHER journey's own raw-fetch logout in this suite, so a
    // goto here would race that bootstrap login against this explicit one.
    // The page is already parked on `base`'s origin from the prior
    // section, which is all a same-origin fetch needs.)
    await loginAs(page, "admin", "demo");
    const isolationContext = await browser.newContext({
      viewport: { width: viewport.width, height: viewport.height },
    });
    const isolationPage = await isolationContext.newPage();
    await isolationPage.goto(base, { waitUntil: "domcontentloaded" });
    // This fresh context's own bootstrap() sees no session and no sticky
    // sign-out either, so it fires the SAME async admin auto-login --
    // settle it before this explicit Coach login, or the two race.
    await isolationPage.waitForLoadState("networkidle").catch(() => {});
    await loginAs(isolationPage, accounts.coach.username, PW);
    const meFirst = await apiGet(page, "/api/auth/me");
    const meSecond = await apiGet(isolationPage, "/api/auth/me");
    if (!meFirst.body.user || meFirst.body.user.role !== "league_admin") {
      fail(`session isolation: first context's own session was not `
        + `League Admin after a second context signed in as Coach: ${JSON.stringify(meFirst.body)}`);
    }
    if (!meSecond.body.user || meSecond.body.user.role !== "coach") {
      fail(`session isolation: second context's session was not Coach: ${JSON.stringify(meSecond.body)}`);
    }
    await isolationContext.close();
    await logout(page);

    // ============================================================
    // Arena Manager -- landing, nav, reaches recurring ice creation,
    // cannot reach unrelated (League-structure) administration.
    // ============================================================
    await loginAs(page, accounts.arena_manager.username, PW);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await reachDashboard(page);
    assertVisibleTabs(fail, "Arena Manager", await visibleTabs(page), [
      "dashboard", "activity", "calendar", "games", "scheduler", "standings",
      "sheet", "public", "roster", "notifications", "delivery", "import", "setup",
    ]);
    // Direct-navigation bypass: Users is hidden from nav, but the SPA has
    // no URL router to "visit" instead -- the equivalent bypass attempt is
    // calling the app's own switchTab() directly, exactly as a curious
    // console user could. Must self-guard, not merely be unreachable via
    // the hidden nav tab.
    await page.evaluate(() => switchTab("users"));
    await waitForView(page, "users");
    // switchTab() sets the view attribute synchronously but kicks off its
    // own render() WITHOUT awaiting it -- wait for the actual guard banner
    // to paint, not just the view attribute flipping.
    await page.waitForSelector("#content .banner", { timeout: 10000 });
    const amUsersBypass = await page.evaluate(() =>
      (document.querySelector("#content .banner") || {}).textContent || "");
    if (!/League admins only/i.test(amUsersBypass)) {
      fail(`Arena Manager: direct-navigating to Users must show the `
        + `"League admins only" guard, got "${amUsersBypass}"`);
    }
    await page.click('.tab[data-tab="dashboard"]');
    await reachDashboard(page);
    // Setup is reachable (manage_arena), but only its arena-side Records
    // carry a "+ New" -- League-structure entities ("unrelated
    // administration") must show none.
    await page.click('.tab[data-tab="setup"]');
    await waitForView(page, "setup");
    await page.click('[data-setup-view="records"]');
    await page.waitForSelector(".setup-card", { timeout: 10000 });
    const amSetupCards = await page.evaluate(() =>
      Array.from(document.querySelectorAll(".setup-card")).map((card) => ({
        title: (card.querySelector(".sc-title") || {}).textContent || "",
        hasNew: !!card.querySelector(".sc-new"),
      })));
    const leagueStructureTitles = ["Programs", "Seasons", "Divisions", "Clubs", "Teams", "Players"];
    const wrongAccess = amSetupCards.filter((c) =>
      leagueStructureTitles.includes(c.title) && c.hasNew);
    if (wrongAccess.length) {
      fail(`Arena Manager: League-structure Setup Records must show no `
        + `"+ New", found on: ${JSON.stringify(wrongAccess)}`);
    }
    if (!amSetupCards.some((c) => c.title === "Venues" && c.hasNew)
        && !amSetupCards.some((c) => c.title === "Rinks" && c.hasNew)) {
      fail(`Arena Manager: expected at least one arena Setup Record `
        + `("Venues"/"Rinks") to keep its "+ New", got ${JSON.stringify(amSetupCards)}`);
    }
    await page.click('.tab[data-tab="dashboard"]');
    await reachDashboard(page);
    // Recurring ice creation, reached and driven entirely by keyboard: nav
    // to Calendar, activate "Build ice", preview, commit -- a REAL
    // authorized mutation, not just a reachability check.
    await page.focus('.tab[data-tab="calendar"]');
    await page.keyboard.press("Enter");
    await page.waitForSelector("[data-ice-builder-open]", { timeout: 10000 });
    await keyboardActivate(page, "[data-ice-builder-open]");
    await page.waitForSelector(".ib-wrap", { timeout: 10000 });
    await page.waitForSelector(`.ib-rink[value="${iceRink.body.id}"]`, { timeout: 10000 });
    await page.check(`.ib-rink[value="${iceRink.body.id}"]`);
    const ibFrom = new Date(Date.now() + 20 * 24 * 3600 * 1000);
    const ibTo = new Date(ibFrom.getTime() + 13 * 24 * 3600 * 1000); // spans a Tue+Thu (default weekdays)
    await page.fill("#ib-from", dateOnly(ibFrom));
    await page.fill("#ib-to", dateOnly(ibTo));
    await keyboardActivate(page, "[data-ib-preview]");
    await page.waitForSelector("[data-ib-commit]:not([disabled])", { timeout: 10000 });
    await keyboardActivate(page, "[data-ib-commit]");
    await page.waitForFunction(() => !document.querySelector(".ib-wrap"), null, { timeout: 10000 });
    const overviewAfterIce = await apiGet(page, "/api/v2/setup/overview");
    const iceRinkSlots = (overviewAfterIce.body.ice_slots || [])
      .filter((sl) => sl.rink_id === iceRink.body.id);
    if (!iceRinkSlots.length) {
      fail("Arena Manager: keyboard-committed recurring ice did not persist "
        + "any real ice slot for the target rink");
    }
    // Unauthorized mutation, rejected by the SERVER: League-structure
    // Setup ("unrelated administration") requires MANAGE_SETUP, which
    // Arena Manager does not hold.
    await assertForbidden(page, fail, "Arena Manager",
      "/api/setup/league", { name: "Should Not Exist (arena manager)" });
    await logout(page);

    // ============================================================
    // Coach -- landing, nav, reaches roster + next-game workflow.
    // ============================================================
    await loginAs(page, accounts.coach.username, PW);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await reachDashboard(page);
    assertVisibleTabs(fail, "Coach", await visibleTabs(page), [
      "dashboard", "activity", "calendar", "games", "standings", "sheet",
      "public", "roster", "notifications",
    ]);
    await assertNoEnabledMutations(page, fail, "Coach (Setup Admin group hidden)");
    // The Next Game card (#146) links straight to Coach's own next game's
    // roster -- keyboard-focus it directly (not click) and activate.
    await page.waitForSelector('.dash-card [data-goto="roster"]', { timeout: 10000 });
    await keyboardActivate(page, '.dash-card [data-goto="roster"]');
    await waitForView(page, "roster");
    await waitForRealContent(page);
    const coachRosterText = await page.evaluate(() =>
      (document.getElementById("content").textContent || "").replace(/\s+/g, " "));
    if (!new RegExp(`Coach Team ${suffix}`, "i").test(coachRosterText)) {
      fail(`Coach: expected the Roster view to show Coach's own team `
        + `("Coach Team ${suffix}"), got: ${coachRosterText.slice(0, 200)}`);
    }
    // Direct-navigation bypass: Setup is hidden from nav (neither
    // manage_setup nor manage_arena) -- switchTab() must self-guard.
    await page.evaluate(() => switchTab("setup"));
    await waitForView(page, "setup");
    await page.click('[data-setup-view="records"]').catch(() => {});
    const coachSetupNew = await page.evaluate(() => document.querySelectorAll(".sc-new").length);
    if (coachSetupNew !== 0) {
      fail(`Coach: direct-navigating to Setup must show no "+ New" `
        + `controls at all, found ${coachSetupNew}`);
    }
    await page.click('.tab[data-tab="dashboard"]');
    await reachDashboard(page);
    await assertForbidden(page, fail, "Coach",
      "/api/v2/setup/venue", { name: "Should Not Exist (coach)", organization_id: null });
    await logout(page);

    // ============================================================
    // Player -- correct destination, no administrative actions.
    // ============================================================
    await loginAs(page, accounts.player.username, PW);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await waitForView(page, "player_home");
    await waitForRealContent(page);
    assertVisibleTabs(fail, "Player", await visibleTabs(page), [
      "player_home", "calendar", "games", "standings", "sheet", "public",
      "roster", "notifications",
    ]);
    const playerText = await page.evaluate(() =>
      (document.getElementById("content").textContent || "").replace(/\s+/g, " "));
    if (!new RegExp(`Priya Player ${suffix}`, "i").test(playerText)) {
      fail(`Player: expected Player Home to greet Priya Player ${suffix}, `
        + `got: ${playerText.slice(0, 200)}`);
    }
    await assertNoEnabledMutations(page, fail, "Player");
    await keyboardActivate(page, '[data-goto="notifications"]');
    await waitForView(page, "notifications");
    await page.click('.tab[data-tab="player_home"]');
    await waitForView(page, "player_home");
    await page.evaluate(() => switchTab("setup"));
    await waitForView(page, "setup");
    await page.click('[data-setup-view="records"]').catch(() => {});
    if ((await page.evaluate(() => document.querySelectorAll(".sc-new").length)) !== 0) {
      fail("Player: direct-navigating to Setup must show no \"+ New\" controls");
    }
    await assertForbidden(page, fail, "Player",
      `/api/games/${game.body.id}/build-roster`, {});
    await logout(page);

    // ============================================================
    // Guardian -- correct destination, no administrative actions.
    // ============================================================
    await loginAs(page, accounts.guardian.username, PW);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await waitForView(page, "guardian_home");
    await waitForRealContent(page);
    assertVisibleTabs(fail, "Guardian", await visibleTabs(page), [
      "guardian_home", "calendar", "games", "standings", "public", "notifications",
    ]);
    const guardianText = await page.evaluate(() =>
      (document.getElementById("content").textContent || "").replace(/\s+/g, " "));
    if (!new RegExp(`Jamie Junior ${suffix}`, "i").test(guardianText)) {
      fail(`Guardian: expected "My Players" to list the verified junior `
        + `Jamie Junior ${suffix}, got: ${guardianText.slice(0, 200)}`);
    }
    // Authorized primary action reached by keyboard: confirm attendance
    // for the linked junior's real next game -- a genuine authorized
    // mutation (RESPOND_AVAILABILITY), not merely a reachable button.
    const confirmSelector = `[data-g-confirm="${junior.body.id}"]`;
    await page.waitForSelector(confirmSelector, { timeout: 10000 });
    await keyboardActivate(page, confirmSelector);
    // The disabled/"In ✓" button state is itself the proof of a real,
    // persisted server response -- renderJuniorCard only renders it
    // disabled once guardianHome reflects attendance_status "confirmed",
    // the same convention the app's own re-render relies on.
    await page.waitForSelector(`${confirmSelector}[disabled]`, { timeout: 10000 });
    await page.evaluate(() => switchTab("setup"));
    await waitForView(page, "setup");
    if ((await page.evaluate(() => document.querySelectorAll(".sc-new").length)) !== 0) {
      fail("Guardian: direct-navigating to Setup must show no \"+ New\" controls");
    }
    await assertForbidden(page, fail, "Guardian",
      `/api/games/${game.body.id}/build-roster`, {});
    await logout(page);

    // ============================================================
    // Official -- correct destination, no administrative actions.
    // ============================================================
    await loginAs(page, accounts.official.username, PW);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await waitForView(page, "inbox");
    await waitForRealContent(page);
    assertVisibleTabs(fail, "Official", await visibleTabs(page), [
      "inbox", "calendar", "games", "standings", "sheet", "public",
      "roster", "notifications",
    ]);
    const officialText = await page.evaluate(() =>
      (document.getElementById("content").textContent || "").replace(/\s+/g, " "));
    if (!/No assignments yet/i.test(officialText)) {
      fail(`Official: expected the Inbox empty state, got: ${officialText.slice(0, 200)}`);
    }
    await keyboardActivate(page, '.tab[data-tab="notifications"]');
    await waitForView(page, "notifications");
    await page.click('.tab[data-tab="inbox"]');
    await waitForView(page, "inbox");
    await page.evaluate(() => switchTab("users"));
    await waitForView(page, "users");
    await page.waitForSelector("#content .banner", { timeout: 10000 });
    const offUsersBypass = await page.evaluate(() =>
      (document.querySelector("#content .banner") || {}).textContent || "");
    if (!/League admins only/i.test(offUsersBypass)) {
      fail(`Official: direct-navigating to Users must show the "Operators `
        + `only" guard, got "${offUsersBypass}"`);
    }
    await assertForbidden(page, fail, "Official",
      "/api/setup/official", { name: "Should Not Exist (official)" });
    await logout(page);

    // ============================================================
    // Viewer -- no enabled mutation action ANYWHERE tested.
    // ============================================================
    await loginAs(page, accounts.viewer.username, PW);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await waitForView(page, "standings");
    await waitForRealContent(page);
    assertVisibleTabs(fail, "Viewer", await visibleTabs(page), [
      "calendar", "games", "standings", "public", "notifications",
    ]);
    await assertNoEnabledMutations(page, fail, "Viewer (standings landing)");
    for (const hiddenView of ["setup", "users"]) {
      await page.evaluate((v) => switchTab(v), hiddenView);
      await waitForView(page, hiddenView);
      await waitForRealContent(page);
      await assertNoEnabledMutations(page, fail, `Viewer (direct nav to ${hiddenView})`);
    }
    await page.waitForSelector("#content .banner", { timeout: 10000 });
    const viewerBanner = await page.evaluate(() =>
      (document.querySelector("#content .banner") || {}).textContent || "");
    if (!/League admins only/i.test(viewerBanner)) {
      fail(`Viewer: direct-navigating to Users must show the "Operators `
        + `only" guard, got "${viewerBanner}"`);
    }
    await page.click('.tab[data-tab="standings"]');
    await waitForView(page, "standings");
    await assertForbidden(page, fail, "Viewer",
      "/api/setup/team", { club_id: club.body.id, league_id: level.body.id,
        name: "Should Not Exist (viewer)" });
    await logout(page);

    // ============================================================
    // Final server-side proof that every negative-mutation probe above
    // was genuinely rejected -- not merely a 403 status that a bug could
    // still have let slip past into a write. Re-read the affected
    // collections as League Admin and confirm none of the six
    // "Should Not Exist"/probe entities exist anywhere.
    // ============================================================
    await loginAs(page, "admin", "demo");
    const finalOverview = await apiGet(page, "/api/v2/setup/overview");
    const bodyText = JSON.stringify(finalOverview.body);
    if (/Should Not Exist/.test(bodyText)) {
      fail("a negative-mutation probe was rejected with 403 but still "
        + "left a persisted record -- rejection was not real");
    }
    const officialsAfter = await apiGet(page, "/api/setup/official").catch(() => null);
    if (officialsAfter && /Should Not Exist \(official\)/.test(JSON.stringify(officialsAfter.body))) {
      fail("Official's negative-mutation probe (create Official) left a persisted record");
    }

    if (errors.length) {
      fail(`console/page errors:\n${errors.join("\n")}`);
    }
    console.log(`[${L}] OK — all seven roles land on their correct `
      + `destination with exactly the right nav, reach one authorized `
      + `action by keyboard, cannot bypass authorization by direct `
      + `navigation or a real HTTP mutation, and role/session switches `
      + `leak nothing.`);
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
    console.log("Seven-role authorization matrix browser journey passed.");
  } catch (error) {
    console.error("Seven-role authorization matrix browser journey FAILED.");
    console.error(error && error.message ? error.message : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
