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
//   3. one authorized primary action reached via a real, BOUNDED Tab/
//      Shift+Tab traversal from a defined starting point -- never
//      page.focus() (it can jump past the real tab order and never
//      triggers :focus-visible) -- with the landed node's exact identity
//      and a real, browser-computed visible focus indicator asserted
//      before Enter, then the persisted effect asserted after;
//   4. an unauthorized action absent or disabled (nav tabs AND, for the
//      roles it applies to, the Setup Records "+ New" controls);
//   5. direct navigation (calling the app's own switchTab() the way a
//      malicious/curious console user would, not a URL route -- this SPA
//      has none) into a hidden view renders no privileged data/controls;
//   6. a real unauthorized mutation request is rejected by the server (403
//      "forbidden"), with a snapshot of BOTH the exact affected business
//      resource AND its persisted audit boundary (SetupAuditLog via
//      /api/demo/overview's setup_audit, or the per-game AuditLog via
//      /api/games/{id}/board's audit array) taken through an authorized
//      reader session BEFORE and AFTER the probe and required to be
//      byte-identical -- not merely a 403 status, which an orchestration
//      bug could still return after a real write or a recorded audit
//      event with the business resource left untouched;
//   7. zero unexpected browser console/page errors AND zero unexpected
//      HTTP responses >=400 -- each negative probe's own exact
//      method/path/403 is individually registered and consumed, so an
//      unrelated 404/500 (or a probe answering something other than the
//      expected 403) cannot hide behind a blanket "ignore resource-load
//      noise" filter. A self-test proves the detector actually catches an
//      unregistered failure before it is ever relied on.
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
const {
  installContextFixture, selectProgram, selectProgramSeason,
} = require("./context-fixture.js");

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

// Real keyboard-only reachability + activation (#345 exact-head review:
// page.focus() bypasses the actual Tab order entirely and never triggers
// :focus-visible, so it proved neither "reachable by Tab" nor "visible
// focus indicator" -- both required legs of this journey's own claim).
//
// Starts from a DEFINED, reproducible point. `withinDialog: false` (the
// default) blurs whatever currently has focus so the very next Tab press is
// the document's own first tab stop -- the same convention
// accessibility-foundations.js's own skip-link check uses. `withinDialog:
// true` instead asserts focus is ALREADY inside the currently-open
// `.drawer[role=dialog]`/`.modal[role=dialog]` (itself a defined,
// already-proven starting point -- the drawer's own documented
// auto-focus-on-open behavior) and tabs forward from there without ever
// leaving it.
//
// Bounded (not unbounded polling): a broken tabindex, a disabled control, or
// an unreachable stop placed in front of the real target must exhaust the
// bound and fail this leg, not silently pass.
async function tabToAndActivate(page, selector, label, {
  maxPresses = 200, shift = false, withinDialog = false,
} = {}) {
  if (withinDialog) {
    const startsInDialog = await page.evaluate(() => {
      const dialog = document.querySelector('.drawer[role="dialog"], .modal[role="dialog"]');
      return !!(dialog && document.activeElement && dialog.contains(document.activeElement));
    });
    if (!startsInDialog) {
      throw new Error(`${label}: expected focus already inside the open `
        + `dialog before tabbing toward ${selector}`);
    }
  } else {
    await page.evaluate(() => {
      if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
    });
  }
  let reachedAt = null;
  for (let i = 1; i <= maxPresses; i += 1) {
    await page.keyboard.press(shift ? "Shift+Tab" : "Tab");
    const hit = await page.evaluate((sel) => {
      const el = document.activeElement;
      const target = document.querySelector(sel);
      return !!(el && target && el === target);
    }, selector);
    if (hit) { reachedAt = i; break; }
  }
  if (reachedAt === null) {
    throw new Error(`${label}: could not reach ${selector} via real keyboard `
      + `Tab traversal within ${maxPresses} presses`);
  }
  // A real, browser-computed focus indicator -- not just "is the
  // activeElement", which an element can satisfy while being visually
  // indistinguishable (outline:none with no replacement). Requires either
  // the native outline or an explicit compensating box-shadow, this app's
  // own convention wherever it overrides the UA outline (.icon-btn,
  // .ctx-select, .login-field input, etc.).
  const focusStyle = await page.evaluate((sel) => {
    const el = document.querySelector(sel);
    if (!el || document.activeElement !== el) return null;
    const cs = getComputedStyle(el);
    return { outlineStyle: cs.outlineStyle, outlineWidth: cs.outlineWidth, boxShadow: cs.boxShadow };
  }, selector);
  const visible = !!focusStyle && (
    (focusStyle.outlineStyle !== "none" && focusStyle.outlineWidth !== "0px")
    || focusStyle.boxShadow !== "none"
  );
  if (!visible) {
    throw new Error(`${label}: reached ${selector} via Tab (press ${reachedAt}) `
      + `but it has no visible focus indicator: ${JSON.stringify(focusStyle)}`);
  }
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

// Precise unauthorized-response tracking (#345 exact-head review): rather
// than blanket-ignoring every "Failed to load resource" console line (which
// could hide an unrelated, genuinely unexpected 404/500), track the EXACT
// method/path/status this journey's own negative probes expect via the real
// network layer -- not console-text matching, which carries neither the
// method nor the path. Any response >=400 that doesn't match a registered
// expectation is a hard failure; a "Failed to load resource" console line is
// only ever silently absorbed once per REALIZED registered expectation.
function makeFailureTracker(page, errors) {
  const expected = []; // {method, path, status, matched}
  const unexpected = []; // "METHOD path -> status" strings
  let allowance = 0;
  page.on("response", (response) => {
    const status = response.status();
    if (status < 400) return;
    let respPath;
    try { respPath = new URL(response.url()).pathname; } catch (_) { respPath = response.url(); }
    const method = response.request().method();
    const match = expected.find((e) =>
      !e.matched && e.method === method && e.path === respPath && e.status === status);
    if (match) { match.matched = true; allowance += 1; } else {
      unexpected.push(`${method} ${respPath} -> ${status}`);
    }
  });
  page.on("console", (m) => {
    if (m.type() !== "error") return;
    if (/Failed to load resource/.test(m.text()) && allowance > 0) { allowance -= 1; return; }
    errors.push(`[console] ${m.text()}`);
  });
  return {
    expect(method, reqPath, status) { expected.push({ method, path: reqPath, status, matched: false }); },
    unexpected,
    unmatched: () => expected.filter((e) => !e.matched),
  };
}

// A real unauthorized HTTP mutation must be rejected by the SERVER (403
// "forbidden"), and BOTH the affected business resource AND the relevant
// persisted audit collection -- each read through an INDEPENDENT, authorized
// reader session, since most roles here cannot read what they're forbidden
// to write -- must be byte-identical before and after. A 403 status alone
// cannot rule out an orchestration bug that mutates (or appends an audit
// event) and THEN answers 403; checking only the business resource cannot
// rule out a defect that records the attempt as a real audit event while
// leaving that resource untouched. `snapshotPaths` names every collection
// this probe must leave byte-identical -- the exact resource AND its audit
// boundary, never just one.
async function assertForbiddenNoChange(page, reader, tracker, fail, label, mutatePath, body, snapshotPaths) {
  const paths = Array.isArray(snapshotPaths) ? snapshotPaths : [snapshotPaths];
  const befores = await Promise.all(paths.map((p) => apiGet(reader, p)));
  tracker.expect("POST", mutatePath, 403);
  const res = await apiPost(page, mutatePath, body || {});
  if (res.status !== 403 || !res.body.error || res.body.error.code !== "forbidden") {
    fail(`${label}: expected 403 "forbidden" POSTing ${mutatePath}, got `
      + `status=${res.status} body=${JSON.stringify(res.body)}`);
  }
  const afters = await Promise.all(paths.map((p) => apiGet(reader, p)));
  paths.forEach((p, i) => {
    const beforeStr = JSON.stringify(befores[i].body);
    const afterStr = JSON.stringify(afters[i].body);
    if (beforeStr !== afterStr) {
      fail(`${label}: rejected with 403 but ${p} changed anyway -- this was `
        + `a real write (or audit record), not just a hidden button. `
        + `before=${beforeStr} after=${afterStr}`);
    }
  });
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
  const tracker = makeFailureTracker(page, errors);

  const fail = (msg) => { throw new Error(`[${viewport.label}] ${msg}`); };
  const L = viewport.label;
  const suffix = viewport.port; // keep usernames unique per viewport/port

  let reader; // the independent authorized-reader context, opened once the fixture exists
  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });
    await installContextFixture(page);
    await reachDashboard(page); // signed in as the auto-provisioned League Admin ("admin"/"demo")

    // ============================================================
    // Self-test: prove the failure tracker actually catches an unregistered
    // (unexpected) HTTP failure before anything else relies on it. Without
    // this, a broken tracker (e.g. one that always grants allowance) would
    // let every OTHER assertion in this file pass for the wrong reason.
    // ============================================================
    const errorsBeforeSelfTest = errors.length;
    const bogus = await apiGet(page, "/api/does-not-exist-xyz-345");
    if (bogus.status !== 404) {
      fail(`self-test: expected the deliberately unregistered route to `
        + `404, got ${bogus.status}`);
    }
    await new Promise((r) => setTimeout(r, 150)); // let the response listener fire
    const caughtSelfTest = tracker.unexpected.some(
      (u) => u.includes("does-not-exist-xyz-345") && u.endsWith("-> 404"));
    if (!caughtSelfTest) {
      fail("self-test: the unexpected-response tracker did not catch a "
        + "deliberately unregistered 404 -- detection is broken, so every "
        + "later 'no unexpected failures' assertion in this file would be "
        + "meaningless");
    }
    // Consumed and proven -- discard this self-test's own artifacts (both
    // the tracked response and its console noise) so they don't fail the
    // real run for a reason unrelated to any role's own behavior.
    tracker.unexpected.length = 0;
    errors.length = errorsBeforeSelfTest;

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
    await installContextFixture(page);
    await reachDashboard(page);
    // #409 EXPLICIT SELECTION. The Season this leg creates THROUGH THE REAL
    // DRAWER is a PROGRAM-AXIS create, and minting the Program above did not
    // select it — so without a persisted Program-only choice the drawer
    // submit is refused and the "Done" assertion below would blame the
    // keyboard path for a context that was never stated.
    await selectProgram(page, "League Admin: Program-only selection",
      program.body.id);
    await waitForCardSettled(page);
    let s = await cardState(page);
    if (!s || s.nextTitle !== "League profile and seasons" || s.primaryLabel !== "Add Season") {
      fail(`League Admin: expected a fresh Program to recommend "Add Season" `
        + `next, got ${JSON.stringify(s)}`);
    }
    // Keyboard-reach and activate the card's own primary action via a real,
    // bounded Tab traversal from a blurred document start -- landing focus
    // inside the real Season drawer it opens.
    await tabToAndActivate(page, "[data-setup-progress-action]", "League Admin Add Season");
    await page.waitForSelector(".drawer[role=dialog]", { timeout: 10000 });
    const seasonName = `Matrix Season ${suffix}`;
    await page.fill("#f-season", seasonName);
    // The drawer's own documented open behavior already auto-focused a
    // field inside it (a defined starting point) -- tab forward from there,
    // still entirely by keyboard, to reach the real submit control.
    // ASSERT THE CREATE RESPONSE (#409). The drawer's own POST is captured and
    // checked at the point it happens, so a refusal fails HERE, naming the
    // status and body, instead of surfacing later as an absent row.
    const seasonResp = page.waitForResponse((r) =>
      r.url().endsWith("/api/v2/setup/season") && r.request().method() === "POST",
      { timeout: 10000 });
    await tabToAndActivate(page, '[data-drawer-submit="season"]',
      "League Admin submit Season", { withinDialog: true, maxPresses: 20 });
    const seasonCreated = await seasonResp;
    const seasonBody = await seasonCreated.json().catch(() => null);
    if (seasonCreated.status() !== 200 || !seasonBody || seasonBody.error || !seasonBody.id) {
      fail(`League Admin: the keyboard-submitted Season drawer was refused: `
        + `${seasonCreated.status()} ${JSON.stringify(seasonBody)}`);
    }
    if (seasonBody.program_id !== program.body.id) {
      fail(`League Admin: the Season landed in a different Program than the one `
        + `selected — selected ${program.body.id}, created under ${seasonBody.program_id}`);
    }
    await page.waitForSelector(".drawer[role=dialog]", { state: "detached", timeout: 10000 });
    const season = seasonBody;

    // ============================================================
    // Build the rest of the shared fixture (level/division/club/teams/
    // venue/rinks/ice/game/players/official) via the documented v1/v2
    // setup API -- same recipe scheduling-policy.js and home-tasks-hub.js
    // already exercise. Every entity below is uniquely named per role so
    // no role's data can satisfy another role's assertion.
    // ============================================================
    // #409 boundary 2: the v1 "level" IS the v2 League, and it plus the
    // Division, the three registrations and the venue-access grant are all
    // SEASON-OWNED and land in the Season the drawer just created. v1 is
    // guarded identically (server.py:1160 runs the same preflight), so it is
    // not a way around the rule.
    await selectProgramSeason(page, "League Admin: Program+Season",
      program.body.id, season.id);
    // Read the real, persisted Season back through the API boundary (not just
    // the drawer's own response) -- proves the mutation reached the server.
    // It runs AFTER the Season is selected because `/api/v2/setup/overview` is
    // ceilinged on the ACTIVE Season: under the Program-only selection the
    // drawer submit legitimately required, the Season list is empty, so a
    // read-back placed before the selection would report a Season that
    // demonstrably exists as missing.
    const overview1 = await apiGet(page, "/api/v2/setup/overview");
    if (!(overview1.body.seasons || []).some((sn) => sn.id === season.id)) {
      fail(`League Admin: Season "${seasonName}" not found via `
        + `GET /api/v2/setup/overview under its own selected tuple: `
        + `${JSON.stringify(overview1.body.seasons)}`);
    }
    const level = await apiPost(page, "/api/setup/level",
      { season_id: season.id, name: `Matrix League ${suffix}` });
    if (level.status !== 200 || level.body.error) {
      fail(`level create failed: ${JSON.stringify(level)}`);
    }
    // "League profile and seasons" is only Done once every Season of the
    // Program carries a grouping League (api/service.py's own league_done
    // rule) -- the keyboard-submitted Season alone isn't enough, matching
    // home-tasks-hub.js's own precedent (season THEN league before its
    // first "Done" check). Confirms the keyboard-driven mutation combines
    // with the rest of the real setup flow, not just that it persisted.
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await installContextFixture(page);
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
    // ------------------------------------------------------------------
    // A PRIOR game for the Coach's own team, with a real seated roster --
    // the source a "Copy previous roster" reads from (#427). Placed in the
    // PAST so it cannot become anybody's "next game" and the Next Game card
    // below still points at the fixture's real target game.
    //
    // Two players are seated on it while both are eligible; one is then
    // deactivated, which makes the copy below a genuinely MIXED batch: one
    // player seats, one is skipped with a reason the warning must NAME.
    // ------------------------------------------------------------------
    const priorStart = new Date(Date.now() - 10 * 24 * 3600 * 1000);
    priorStart.setUTCHours(18, 30, 0, 0);
    const priorEnd = new Date(priorStart.getTime() + 90 * 60000);
    const priorRink = await apiPost(page, "/api/v2/setup/rink",
      { venue_id: venue.body.id, name: `Prior Rink ${suffix}` });
    const priorSlot = await apiPost(page, "/api/v2/setup/ice-slot", {
      rink_id: priorRink.body.id, start_time: iso(priorStart),
      end_time: iso(priorEnd), slot_type: "game",
    });
    if (priorSlot.status !== 200 || priorSlot.body.error) {
      fail(`prior ice slot create failed: ${JSON.stringify(priorSlot)}`);
    }
    const priorGame = await apiPost(page, "/api/setup/game", {
      season_id: season.id, division_id: division.body.id,
      home_team_id: coachTeam.body.id, away_team_id: rivalTeam.body.id,
      ice_slot_id: priorSlot.body.id,
    });
    if (priorGame.status !== 200 || priorGame.body.error) {
      fail(`prior game create failed: ${JSON.stringify(priorGame)}`);
    }
    // Names fix the order the warning lists them in: the batch orders
    // candidates by (name, player_id), so "Copy Keeper" precedes
    // "Copy Skipped" in every response, on every backend.
    const keeper = await apiPost(page, "/api/v2/setup/player",
      { team_id: coachTeam.body.id, name: `Copy Keeper ${suffix}`, position: "forward" });
    const skipped = await apiPost(page, "/api/v2/setup/player",
      { team_id: coachTeam.body.id, name: `Copy Skipped ${suffix}`, position: "defense" });
    const seatPrior = await apiPost(page,
      `/api/games/${priorGame.body.id}/roster/select`,
      { player_ids: [keeper.body.id, skipped.body.id] });
    if (seatPrior.status !== 200 || seatPrior.body.error) {
      fail(`prior roster select failed: ${JSON.stringify(seatPrior)}`);
    }
    // The durable attribution the copy discovers candidates from must really
    // be on those rows -- if it were not, the copy would find nobody and the
    // whole leg below would pass vacuously.
    const priorBoard = await apiGet(page, `/api/games/${priorGame.body.id}/lineups`);
    const priorSeated = (priorBoard.body.home.players || [])
      .filter((pl) => pl.group === "selected").map((pl) => pl.id).sort();
    if (JSON.stringify(priorSeated)
        !== JSON.stringify([keeper.body.id, skipped.body.id].sort())) {
      fail(`prior game did not seat both players on the Coach's side: `
        + `${JSON.stringify(priorSeated)}`);
    }
    const deactivate = await apiPost(page,
      `/api/v2/setup/player/${skipped.body.id}/active`, { active: false });
    if (deactivate.status !== 200 || deactivate.body.error) {
      fail(`deactivate failed: ${JSON.stringify(deactivate)}`);
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
    // A SECOND official, ASSIGNED to this game (#427 final blocker, round 2).
    // Distinct from Ozzy on purpose, and for this file's own stated reason --
    // "every role gets its own distinctly-named user ... so no role's fixture
    // can accidentally satisfy another role's assertion". Ozzy proves the
    // UNASSIGNED official's Inbox empty state; assigning Ozzy would destroy
    // it. Only an ASSIGNED official passes `can_read_private_game_data`, and
    // that is the principal whose Roster tab this leg is about.
    const assignedOfficial = await apiPost(page, "/api/v2/setup/official",
      { name: `Avery Assigned ${suffix}` });

    // Seven distinct, role-scoped accounts (League Admin reuses the
    // existing seeded "admin"/"demo" login used to build this fixture).
    const PW = "matrix-account-pw";
    const mk = (role) => `matrix_${role}_${suffix}`;
    const accounts = {
      arena_manager: { username: mk("arena"), role: "arena_manager", scope: {} },
      coach: { username: mk("coach"), role: "coach", scope: { team_id: coachTeam.body.id } },
      player: { username: mk("player"), role: "player",
        scope: { player_id: priya.body.id, team_id: playerTeam.body.id } },
      guardian: { username: mk("guardian"), role: "guardian", scope: {} },
      official: { username: mk("official"), role: "official",
        scope: { official_id: official.body.id } },
      assigned_official: { username: mk("assignedofficial"), role: "official",
        scope: { official_id: assignedOfficial.body.id } },
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
    // Assign Avery to the fixture game, as the operator, so the account above
    // is a genuinely ASSIGNED official -- the only shape
    // `can_read_private_game_data` admits, and therefore the only one whose
    // Roster tab reaches the private-game family at all.
    const assign = await apiPost(page,
      `/api/games/${game.body.id}/officials/assign`,
      { official_id: assignedOfficial.body.id, role: "referee" });
    if (assign.status !== 200 || assign.body.error) {
      fail(`official assignment failed: ${JSON.stringify(assign)}`);
    }
    const link = await apiPost(page, "/api/guardians/links",
      { guardian_user_id: accounts.guardian.id, player_id: junior.body.id });
    const verify = await apiPost(page, `/api/guardians/links/${link.body.id}/verify`,
      { consent_method: "verbal_confirmed" });
    if (verify.status !== 200 || verify.body.error) {
      fail(`guardian link verify failed: ${JSON.stringify(verify)}`);
    }

    // An INDEPENDENT, authorized reader session (its own browser context),
    // used ONLY to snapshot state before/after each negative-mutation probe
    // below. Most roles under test cannot read the collections they're
    // forbidden to write, so proving zero-write requires reading through a
    // session that legitimately can -- the same authorized server boundary
    // the rest of this journey already insists on.
    const readerContext = await browser.newContext({
      viewport: { width: viewport.width, height: viewport.height },
    });
    reader = await readerContext.newPage();
    await reader.goto(base, { waitUntil: "domcontentloaded" });
    await reader.waitForLoadState("networkidle").catch(() => {});
    await loginAs(reader, "admin", "demo");

    // ============================================================
    // Explicit in-app, NO-RELOAD role switch: League Admin -> Viewer via
    // the app's own signIn() (the exact function the login form and demo
    // role-switcher call) -- never a page.goto(). Proves switching
    // identity strips every admin-only nav tab and Dashboard card from the
    // live DOM on its own, not merely on the next fresh navigation.
    // ============================================================
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await installContextFixture(page);
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
    await installContextFixture(page);
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
    await installContextFixture(page);
    await reachDashboard(page);
    // Setup is reachable (manage_arena), but only its arena-side Records
    // carry a "+ New" -- League-structure entities ("unrelated
    // administration") must show none.
    await page.click('.tab[data-tab="setup"]:not([data-setup-workflow-nav])');
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
    await installContextFixture(page);
    await reachDashboard(page);
    // Recurring ice creation, reached and driven entirely by real keyboard
    // Tab traversal: nav to Calendar, activate "Build ice", preview, commit
    // -- a REAL authorized mutation, not just a reachability check.
    // #393 PR A: ice preview/commit now require an EXPLICIT active
    // Program/Season. Without one the preview answers 409
    // `active_context_required` and [data-ib-commit] never enables, so this
    // whole keyboard leg would fail for a reason unrelated to authorization.
    // The browser does exactly this through the context bar before the Ice
    // Builder is usable.
    //
    // The read-back is asserted, not assumed: a selection that silently fell
    // back to inferred context would leave this test passing on the very
    // behaviour PR A removes.
    const ctxSet = await apiPost(page, "/api/context",
      { program_id: program.body.id, season_id: season.id });
    if (ctxSet.status !== 200) {
      fail(`Arena Manager: could not select the fixture context: `
        + `${ctxSet.status} ${JSON.stringify(ctxSet.body)}`);
    }
    const ctxRead = await apiGet(page, "/api/context");
    if (ctxRead.body.program_id !== program.body.id
        || ctxRead.body.season_id !== season.id) {
      fail(`Arena Manager: active context did not read back as the explicit `
        + `selection -- got program=${ctxRead.body.program_id} `
        + `season=${ctxRead.body.season_id}`);
    }
    await tabToAndActivate(page, '.tab[data-tab="calendar"]', "Arena Manager reach Calendar");
    await page.waitForSelector("[data-ice-builder-open]", { timeout: 10000 });
    await tabToAndActivate(page, "[data-ice-builder-open]", "Arena Manager open Build ice");
    await page.waitForSelector(".ib-wrap", { timeout: 10000 });
    await page.waitForSelector(`.ib-rink[value="${iceRink.body.id}"]`, { timeout: 10000 });
    await page.check(`.ib-rink[value="${iceRink.body.id}"]`);
    const ibFrom = new Date(Date.now() + 20 * 24 * 3600 * 1000);
    const ibTo = new Date(ibFrom.getTime() + 13 * 24 * 3600 * 1000); // spans a Tue+Thu (default weekdays)
    await page.fill("#ib-from", dateOnly(ibFrom));
    await page.fill("#ib-to", dateOnly(ibTo));
    await tabToAndActivate(page, "[data-ib-preview]", "Arena Manager preview ice");
    await page.waitForSelector("[data-ib-commit]:not([disabled])", { timeout: 10000 });
    await tabToAndActivate(page, "[data-ib-commit]", "Arena Manager commit ice");
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
    // Arena Manager does not hold. Both the whole Setup overview AND the
    // SetupAuditLog trail (/api/demo/overview's setup_audit -- a rejected
    // attempt must not be recorded as a real audit event either) must be
    // byte-identical before and after.
    await assertForbiddenNoChange(page, reader, tracker, fail, "Arena Manager",
      "/api/setup/league", { name: "Should Not Exist (arena manager)" },
      ["/api/v2/setup/overview", "/api/demo/overview"]);
    await logout(page);

    // ============================================================
    // Coach -- landing, nav, reaches roster + next-game workflow.
    // ============================================================
    await loginAs(page, accounts.coach.username, PW);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await installContextFixture(page);
    await reachDashboard(page);
    assertVisibleTabs(fail, "Coach", await visibleTabs(page), [
      "dashboard", "activity", "calendar", "games", "standings", "sheet",
      "public", "roster", "notifications",
    ]);
    await assertNoEnabledMutations(page, fail, "Coach (Setup Admin group hidden)");
    // The Next Game card (#146) links straight to Coach's own next game's
    // roster -- reached and activated by a real, bounded Tab traversal.
    await page.waitForSelector('.dash-card [data-goto="roster"]', { timeout: 10000 });
    await tabToAndActivate(page, '.dash-card [data-goto="roster"]', "Coach reach Roster");
    await waitForView(page, "roster");
    await waitForRealContent(page);
    const coachRosterText = await page.evaluate(() =>
      (document.getElementById("content").textContent || "").replace(/\s+/g, " "));
    if (!new RegExp(`Coach Team ${suffix}`, "i").test(coachRosterText)) {
      fail(`Coach: expected the Roster view to show Coach's own team `
        + `("Coach Team ${suffix}"), got: ${coachRosterText.slice(0, 200)}`);
    }

    // ============================================================
    // Coach -- the AUTHORIZED batch mutation, and the partial outcome
    // it must make visible (#427).
    //
    // Until this leg existed the Coach's only proven interactions were
    // navigational: every role in this matrix probed a FORBIDDEN mutation,
    // and Coach -- the one role that actually manages rosters -- performed
    // no authorized one at all. The owner's ruling ("show the partial-result
    // warning on desktop and 390px rather than reducing it to a copied
    // count") is a claim about THIS surface at BOTH of the viewports this
    // file already runs, so it is proven here rather than in a new spec.
    // ============================================================
    const onTarget = await page.evaluate(() => ({
      game: typeof currentGame === "string" ? currentGame : null,
      team: typeof rosterTeamId === "string" ? rosterTeamId : null,
    }));
    if (onTarget.game !== game.body.id) {
      fail(`Coach: expected the Roster view to be on the fixture's target `
        + `game ${game.body.id}, got ${onTarget.game}`);
    }
    if (onTarget.team !== coachTeam.body.id) {
      fail(`Coach: expected the Roster view to be on the Coach's own side `
        + `${coachTeam.body.id}, got ${onTarget.team}`);
    }
    // No warning before the action -- so the assertions below cannot be
    // satisfied by something that was already on screen.
    if (await page.$(".ros-partial")) {
      fail("Coach: a batch partial-result warning was present before any "
        + "batch action ran");
    }
    // A REAL keyboard activation of the real control, the same discipline
    // every other authorized mutation in this file uses.
    await page.waitForSelector('[data-act="copy"]', { timeout: 10000 });
    await tabToAndActivate(page, '[data-act="copy"]', "Coach copy previous roster");
    await page.waitForSelector(".ros-partial", { timeout: 10000 });
    const partial = await page.evaluate(() => {
      const el = document.querySelector(".ros-partial");
      return {
        text: (el.textContent || "").replace(/\s+/g, " ").trim(),
        items: Array.from(el.querySelectorAll(".rp-list li"))
          .map((li) => (li.textContent || "").replace(/\s+/g, " ").trim()),
      };
    });
    // It NAMES the skipped player and says WHY, in operator language -- not
    // a machine code, and not a bare count.
    if (!new RegExp(`Copy Skipped ${suffix}`).test(partial.text)) {
      fail(`Coach: the partial-result warning must NAME the skipped player `
        + `("Copy Skipped ${suffix}"), got: ${partial.text}`);
    }
    if (!/no longer an active player/i.test(partial.text)) {
      fail(`Coach: the partial-result warning must give the skipped player's `
        + `reason in operator language, got: ${partial.text}`);
    }
    if (/player_inactive/.test(partial.text)) {
      fail(`Coach: the warning leaked the raw machine reason code instead of `
        + `its operator wording: ${partial.text}`);
    }
    if (partial.items.length !== 1) {
      fail(`Coach: expected exactly one skipped player listed, got `
        + `${JSON.stringify(partial.items)}`);
    }
    // ...and the ELIGIBLE player really was seated: a partial success, not a
    // refusal dressed up as a warning.
    const afterCopy = await apiGet(page, `/api/games/${game.body.id}/lineups`);
    const seatedNow = (afterCopy.body.home.players || [])
      .filter((pl) => pl.group === "selected").map((pl) => pl.id);
    if (JSON.stringify(seatedNow) !== JSON.stringify([keeper.body.id])) {
      fail(`Coach: expected the copy to seat exactly the eligible player, `
        + `got ${JSON.stringify(seatedNow)}`);
    }
    // The live region spoke the outcome ONCE (#toast-root is the single
    // sitewide role="status" region).
    const spoken = await page.evaluate(() => {
      const root = document.getElementById("toast-root");
      return { hidden: !!root.hidden, regions: document.querySelectorAll('[role="status"]').length,
        text: (root.textContent || "").replace(/\s+/g, " ").trim() };
    });
    if (spoken.hidden || !/could not be added/i.test(spoken.text)) {
      fail(`Coach: the partial outcome was not announced in the live region, `
        + `got ${JSON.stringify(spoken)}`);
    }
    if (spoken.regions !== 1) {
      fail(`Coach: expected exactly one role="status" live region so the `
        + `outcome is spoken once, found ${spoken.regions}`);
    }
    // ------------------------------------------------------------
    // ZERO-SEAT: the other half of the ruling. An authorized admin
    // (the independent reader session) deactivates the one player who
    // seated, so every candidate on the prior roster is now ineligible.
    // The copy must SUCCEED, seat nobody, write nothing, and say so.
    // ------------------------------------------------------------
    const deactivateKeeper = await apiPost(reader,
      `/api/v2/setup/player/${keeper.body.id}/active`, { active: false });
    if (deactivateKeeper.status !== 200 || deactivateKeeper.body.error) {
      fail(`Coach: could not deactivate the remaining player: `
        + `${JSON.stringify(deactivateKeeper)}`);
    }
    await tabToAndActivate(page, '[data-act="copy"]', "Coach copy again (zero seat)");
    await page.waitForFunction(
      () => {
        const el = document.querySelector(".ros-partial");
        return !!el && el.querySelectorAll(".rp-list li").length === 2;
      }, { timeout: 10000 });
    const zero = await page.evaluate(() => {
      const el = document.querySelector(".ros-partial");
      return (el.textContent || "").replace(/\s+/g, " ").trim();
    });
    if (!/No players were added/i.test(zero)) {
      fail(`Coach: expected the ZERO-SEAT warning, got: ${zero}`);
    }
    for (const who of [`Copy Keeper ${suffix}`, `Copy Skipped ${suffix}`]) {
      if (!new RegExp(who).test(zero)) {
        fail(`Coach: the zero-seat warning must name every candidate `
          + `("${who}"), got: ${zero}`);
      }
    }
    // No roster writes: the already-seated row is untouched and nothing new
    // appeared.
    const afterZero = await apiGet(page, `/api/games/${game.body.id}/lineups`);
    const seatedAfterZero = (afterZero.body.home.players || [])
      .filter((pl) => pl.group === "selected").map((pl) => pl.id);
    if (JSON.stringify(seatedAfterZero) !== JSON.stringify(seatedNow)) {
      fail(`Coach: a zero-seat copy must write no roster rows; lineup went `
        + `from ${JSON.stringify(seatedNow)} to `
        + `${JSON.stringify(seatedAfterZero)}`);
    }
    // The whole warning fits the viewport -- at 390px as well as at
    // desktop -- with no horizontal overflow. No new breakpoint exists or
    // may exist (e2e/breakpoint-contract.js), so 390 is covered by the
    // existing 480px rule and by the block's own wrapping.
    const overflow = await page.evaluate(() => {
      const el = document.querySelector(".ros-partial");
      return {
        docScroll: document.documentElement.scrollWidth,
        docClient: document.documentElement.clientWidth,
        elRight: Math.ceil(el.getBoundingClientRect().right),
        elScroll: el.scrollWidth, elClient: el.clientWidth,
      };
    });
    if (overflow.docScroll > overflow.docClient) {
      fail(`Coach [${L}]: the partial-result warning pushed the page into a `
        + `horizontal scroll: ${JSON.stringify(overflow)}`);
    }
    if (overflow.elScroll > overflow.elClient + 1) {
      fail(`Coach [${L}]: the partial-result warning overflows its own box `
        + `(text not wrapping): ${JSON.stringify(overflow)}`);
    }
    if (overflow.elRight > overflow.docClient) {
      fail(`Coach [${L}]: the partial-result warning extends past the `
        + `viewport: ${JSON.stringify(overflow)}`);
    }

    // ============================================================
    // Coach -- the GAME SHEET, own side rendered and OPPONENT REDACTED
    // (#427 blocker, owner ruling comment 5394947899).
    //
    // /lineups used to hand either Coach BOTH sides' private candidate,
    // availability and substitute state, and this file already fetched
    // /lineups and /board without ever asserting WHICH players came back.
    // Nothing in e2e/ opened the Game Sheet at all -- zero hits for
    // switchTab("sheet"), .gs-row or .gs-side across the whole directory --
    // so the screen the ruling is about had no journey coverage.
    //
    // Extended here rather than added as a new spec file on purpose: the
    // journey list is enumerated BY HAND three times in
    // .github/workflows/hockey-scheduler-ci.yml (a `node --check` line, a
    // shard string, and e2e/package.json), so a new file silently skips CI
    // until three separate edits land. This file already logs in as a real
    // authenticated Coach with scope.team_id, already runs at BOTH viewports
    // (desktop 1440x900 and phone 390x844), and is already registered.
    // ============================================================
    // A real keyboard activation of the real nav tab, the same discipline
    // every other reachability claim in this file uses.
    await tabToAndActivate(page, '.tab[data-tab="sheet"]', "Coach reach Game Sheet");
    await waitForView(page, "sheet");
    await waitForRealContent(page);
    await page.waitForSelector(".game-sheet .gs-grid", { timeout: 10000 });

    // The sheet render fires GET /api/officials for its assign control.
    // That route carries no operator gate, so a Coach gets 200 -- asserted
    // rather than assumed, because this file's failure tracker fails the
    // whole run on any unregistered HTTP >= 400 and a silent 403 here would
    // surface as an unrelated-looking failure at the end of the journey.
    const officialsForCoach = await apiGet(page, "/api/officials");
    if (officialsForCoach.status !== 200) {
      fail(`Coach [${L}]: the Game Sheet's own /api/officials fetch returned `
        + `${officialsForCoach.status} -- the sheet cannot render without it `
        + `and this file's tracker fails on unregistered >= 400 responses`);
    }

    const sheet = await page.evaluate(() => {
      const sides = Array.from(document.querySelectorAll(".gs-grid > section.gs-side"));
      const read = (el) => ({
        team: (el.querySelector(".gs-side-team") || {}).textContent || "",
        label: (el.querySelector(".gs-side-label") || {}).textContent || "",
        restricted: !!el.querySelector("[data-restricted]"),
        restrictedClass: el.classList.contains("gs-side-restricted"),
        rows: Array.from(el.querySelectorAll(".gs-roster > .gs-row")).map((r) => ({
          num: (r.querySelector(".gs-num") || {}).textContent || "",
          name: (r.querySelector(".gs-name") || {}).textContent || "",
          pos: (r.querySelector(".pos-tag") || {}).className || "",
        })),
        text: (el.textContent || "").replace(/\s+/g, " ").trim(),
      });
      return {
        count: sides.length,
        sides: sides.map(read),
        assign: document.querySelectorAll(".gs-assign").length,
        offSlots: document.querySelectorAll(".gs-off-slot").length,
      };
    });
    if (sheet.count !== 2) {
      fail(`Coach [${L}]: the Game Sheet must still show BOTH sides -- which `
        + `team you are playing is public, only their lineup is not -- got `
        + `${sheet.count} side section(s)`);
    }
    const [ownSide, oppSide] = sheet.sides;
    // OWN SIDE: real, named, seated rows.
    if (!new RegExp(`Coach Team ${suffix}`).test(ownSide.team)) {
      fail(`Coach [${L}]: expected the first sheet column to be the Coach's `
        + `own team ("Coach Team ${suffix}"), got "${ownSide.team}"`);
    }
    if (ownSide.restricted) {
      fail(`Coach [${L}]: the Coach's OWN side was redacted on the Game Sheet`);
    }
    const ownNames = ownSide.rows.map((r) => r.name);
    if (ownNames.length !== 1 || !new RegExp(`Copy Keeper ${suffix}`).test(ownNames[0])) {
      fail(`Coach [${L}]: expected the own side's submitted lineup to be the `
        + `one seated player ("Copy Keeper ${suffix}"), got `
        + `${JSON.stringify(ownNames)}`);
    }
    // OPPONENT SIDE: redacted, and redacted AS SUCH.
    if (!new RegExp(`Rival Team ${suffix}`).test(oppSide.team)) {
      fail(`Coach [${L}]: the redacted opponent must keep its PUBLIC team `
        + `name so the sheet can still say who the game is against, got `
        + `"${oppSide.team}"`);
    }
    if (!oppSide.restricted || !oppSide.restrictedClass) {
      fail(`Coach [${L}]: the opponent side was not rendered as restricted: `
        + `${JSON.stringify(oppSide)}`);
    }
    if (oppSide.rows.length !== 0) {
      fail(`Coach [${L}]: the redacted opponent rendered ${oppSide.rows.length} `
        + `player row(s) -- private identities leaked onto the Game Sheet`);
    }
    // THE POINT OF THE WHOLE REPRESENTATION: redaction must not read as the
    // opponent having failed to submit a lineup, which is a different and
    // materially misleading operational claim.
    if (/No lineup submitted/i.test(oppSide.text)) {
      fail(`Coach [${L}]: the redacted opponent is displayed as "No lineup `
        + `submitted" -- an operational claim about the OPPONENT rather than `
        + `about this reader's access: ${oppSide.text}`);
    }
    if (!/Restricted/i.test(oppSide.text)) {
      fail(`Coach [${L}]: the redacted opponent does not say it is `
        + `restricted: ${oppSide.text}`);
    }
    // And no AWAY player's name appears on the page at all. `Jamie Junior`
    // is the rival team's own player -- a real body on the opponent side, so
    // this is a positive absence and not a search for a string that could
    // never have been there.
    const sheetText = await page.evaluate(() =>
      document.getElementById("content").textContent || "");
    if (new RegExp(`Jamie Junior ${suffix}`).test(sheetText)) {
      fail(`Coach [${L}]: an opponent player's name ("Jamie Junior ${suffix}") `
        + `is rendered on the Coach's Game Sheet`);
    }
    // A free "unauthorized control absent" assertion while we are here: the
    // assign control renders only for manage_schedule, which a Coach lacks,
    // but the unassigned slots themselves are part of the sheet.
    if (sheet.assign !== 0) {
      fail(`Coach [${L}]: the officials ASSIGN control is rendered for a Coach `
        + `(manage_schedule is not theirs)`);
    }
    if (sheet.offSlots === 0) {
      fail(`Coach [${L}]: the officials panel rendered no slots at all, so the `
        + `assertion above proves nothing`);
    }
    // The sheet fits the viewport -- at 390px as well as desktop. NO NEW
    // BREAKPOINT exists or may exist (e2e/breakpoint-contract.js pins the
    // four approved widths), so 390 is covered by the Game Sheet's existing
    // 720px stacking rule and the redaction block's own wrapping.
    const sheetOverflow = await page.evaluate(() => {
      const el = document.querySelector(".gs-side-restricted");
      return {
        docScroll: document.documentElement.scrollWidth,
        docClient: document.documentElement.clientWidth,
        elRight: Math.ceil(el.getBoundingClientRect().right),
        elScroll: el.scrollWidth, elClient: el.clientWidth,
      };
    });
    if (sheetOverflow.docScroll > sheetOverflow.docClient) {
      fail(`Coach [${L}]: the Game Sheet pushed the page into a horizontal `
        + `scroll: ${JSON.stringify(sheetOverflow)}`);
    }
    if (sheetOverflow.elScroll > sheetOverflow.elClient + 1
        || sheetOverflow.elRight > sheetOverflow.docClient) {
      fail(`Coach [${L}]: the restricted-opponent block overflows at this `
        + `viewport: ${JSON.stringify(sheetOverflow)}`);
    }

    // ------------------------------------------------------------
    // The same redaction on the ROSTER view, where the empty state it must
    // NOT be confused with is worded differently ("No players on the roster
    // yet"). A scoped Coach must not LAND on the redacted tab, but must be
    // able to OPEN it and read why it is closed.
    // ------------------------------------------------------------
    await page.evaluate(() => switchTab("roster"));
    await waitForView(page, "roster");
    await waitForRealContent(page);
    await page.waitForSelector(".lineup-switch .ls", { timeout: 10000 });
    const landed = await page.evaluate(() => ({
      active: (document.querySelector(".lineup-switch .ls.active") || {}).dataset,
      restrictedTabs: Array.from(document.querySelectorAll(".lineup-switch .ls.restricted"))
        .map((b) => b.dataset.side),
      panel: document.querySelectorAll("[data-restricted]").length,
    }));
    if (landed.active.side !== "home") {
      fail(`Coach [${L}]: the roster view did not land on the Coach's own `
        + `side, it landed on ${landed.active.side}`);
    }
    if (JSON.stringify(landed.restrictedTabs) !== JSON.stringify(["away"])) {
      fail(`Coach [${L}]: expected exactly the opponent tab to be marked `
        + `restricted, got ${JSON.stringify(landed.restrictedTabs)}`);
    }
    if (landed.panel !== 0) {
      fail(`Coach [${L}]: the redaction panel is showing on the Coach's own `
        + `side`);
    }
    // Opening the opponent tab deliberately explains the closure rather than
    // showing an empty roster.
    await tabToAndActivate(page, '.lineup-switch .ls[data-side="away"]',
      "Coach open the opponent lineup tab");
    await page.waitForSelector("[data-restricted]", { timeout: 10000 });
    const opened = await page.evaluate(() => {
      const el = document.querySelector(".restricted-side");
      return {
        text: (el.textContent || "").replace(/\s+/g, " ").trim(),
        rows: document.querySelectorAll("#content .card .row").length,
        emptyStates: Array.from(document.querySelectorAll("#content .empty"))
          .map((e) => (e.textContent || "").replace(/\s+/g, " ").trim()),
      };
    });
    if (!/Restricted/i.test(opened.text) || !/not shown/i.test(opened.text)) {
      fail(`Coach [${L}]: the opponent roster tab does not explain the `
        + `redaction: ${opened.text}`);
    }
    if (opened.emptyStates.some((t) => /No players on the roster yet/i.test(t))) {
      fail(`Coach [${L}]: the redacted opponent roster is displayed as an `
        + `empty roster: ${JSON.stringify(opened.emptyStates)}`);
    }
    if (opened.rows !== 0) {
      fail(`Coach [${L}]: ${opened.rows} opponent player row(s) rendered on a `
        + `restricted side`);
    }
    // Back to the Coach's own side for whatever runs after this leg.
    await tabToAndActivate(page, '.lineup-switch .ls[data-side="home"]',
      "Coach return to own lineup tab");
    await page.waitForSelector('.lineup-switch .ls[data-side="home"].active',
      { timeout: 10000 });

    // ------------------------------------------------------------
    // INELIGIBLE-BUT-VISIBLE substitute rows (#427). "Make that row
    // non-actionable except for permitted cleanup and expose/label its
    // ineligible state so the UI does not offer an add/seat action that the
    // service must reject."
    //
    // The state is real and the SERVICE side of it is pinned tri-store
    // (backend/tests/test_lineup_population_authority.py,
    // ParticipationEndingDoesNotFlipTheDurableSide, which also proves the
    // service really does refuse the seat). It cannot be REACHED from a
    // browser journey, though: ending a participation stint means writing a
    // SeasonRosterMembership status, and memberships have no HTTP surface at
    // all -- there is no route to call. So the payload is produced by
    // rewriting the real /lineups response in flight, which still drives the
    // whole genuine path from fetch through render to DOM, and the SHAPE
    // being injected is exactly the shape those Python tests pin.
    await page.route("**/api/games/*/lineups", async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      const own = body.home;
      if (own && Array.isArray(own.players)) {
        own.players.push({
          id: "e2e-ineligible", name: `Departed Sub ${suffix}`,
          position: "forward", slot_type: "skater",
          // null, not a permanent number: no seasonal value survives.
          jersey_number: null, group: "substitute", roster_status: null,
          backed_out: false, availability: "pending", sub_status: "enrolled",
          eligible: false,
        });
        own.players.push({
          id: "e2e-eligible", name: `Standing Sub ${suffix}`,
          position: "forward", slot_type: "skater", jersey_number: 9,
          group: "substitute", roster_status: null, backed_out: false,
          availability: "pending", sub_status: "enrolled", eligible: true,
        });
        // An open skater slot, so "no slot" cannot be the reason the
        // ineligible row is unactionable -- the ELIGIBLE row beside it must
        // get its Add button from the very same counts.
        own.status.open_skater_slots = Math.max(1, own.status.open_skater_slots);
        own.status.target_skaters = own.status.target_skaters
          + own.status.open_skater_slots;
      }
      await route.fulfill({ response, json: body });
    });
    // Force a real re-render through the real fetch.
    await page.evaluate(() => switchTab("games"));
    await waitForView(page, "games");
    await page.evaluate(() => switchTab("roster"));
    await waitForView(page, "roster");
    await waitForRealContent(page);
    await page.waitForSelector("[data-ineligible]", { timeout: 10000 });
    const cleanup = await page.evaluate((names) => {
      // The cleanup block is identified by the label it renders, not by a
      // position, so re-ordering the coach surface cannot silently retarget
      // this assertion at some other card.
      const title = Array.from(document.querySelectorAll("#content .section-title"))
        .find((t) => /Needs cleanup/i.test(t.textContent || ""));
      const card = title && title.nextElementSibling;
      const rows = card ? Array.from(card.querySelectorAll(".row")) : [];
      return {
        found: !!card,
        titleText: title ? (title.textContent || "").trim() : null,
        cardText: card ? (card.textContent || "").replace(/\s+/g, " ").trim() : null,
        rows: rows.map((r) => ({
          text: (r.textContent || "").replace(/\s+/g, " ").trim(),
          ineligible: !!r.querySelector("[data-ineligible]"),
          acts: Array.from(r.querySelectorAll("[data-act]")).map((b) => b.dataset.act),
        })),
        eligibleAnywhere: (document.getElementById("content").textContent || "")
          .includes(names.ok),
      };
    }, { ok: `Standing Sub ${suffix}` });
    if (!cleanup.found || cleanup.rows.length !== 1) {
      fail(`Coach [${L}]: the durably-owned enrolment whose candidate can no `
        + `longer play must stay VISIBLE to its owning Coach -- the live `
        + `outreach queue drops it, so without this block it is actionable by `
        + `nobody: ${JSON.stringify(cleanup)}`);
    }
    const stale = cleanup.rows[0];
    if (!new RegExp(`Departed Sub ${suffix}`).test(stale.text)) {
      fail(`Coach [${L}]: the cleanup block names the wrong row: `
        + `${JSON.stringify(stale)}`);
    }
    // LABELLED, so the coach can see WHY it is here.
    if (!stale.ineligible || !/Ineligible/i.test(stale.text)) {
      fail(`Coach [${L}]: the stale enrolment is not labelled ineligible: `
        + `${JSON.stringify(stale)}`);
    }
    // NON-ACTIONABLE EXCEPT FOR CLEANUP -- asserted as an exact set, so this
    // fails both if a seat control appears AND if the cleanup control the
    // ruling requires disappears. `data-act` is what the click handler
    // dispatches on, so its presence/absence is what actually decides whether
    // an action can be taken.
    if (JSON.stringify(stale.acts) !== JSON.stringify(["withdraw"])) {
      fail(`Coach [${L}]: expected the stale enrolment to offer EXACTLY the `
        + `cleanup action and no seat/add, got ${JSON.stringify(stale.acts)}`);
    }
    // ...and the block really is eligibility-driven: the ELIGIBLE row
    // injected alongside it is not swept in here. (It does not appear on this
    // screen at all -- the outreach queue is its home, and that queue is
    // built from a separate endpoint this rewrite does not touch.)
    if (cleanup.eligibleAnywhere) {
      fail(`Coach [${L}]: the eligible substitute was pulled into the cleanup `
        + `block, so the block is not keyed on eligibility: `
        + `${JSON.stringify(cleanup)}`);
    }
    // The block says, in operator language, what the coach may do about it.
    if (!/can only be removed/i.test(cleanup.cardText)) {
      fail(`Coach [${L}]: the cleanup block does not explain itself: `
        + `${cleanup.cardText}`);
    }
    await page.unroute("**/api/games/*/lineups");

    // ------------------------------------------------------------
    // GAME ACTIVITY (#427 final blocker). "/board: scoped callers and
    // officials must not receive game-wide notifications, audit, or
    // audit_count... audit_count must not survive as a covert cardinality
    // oracle over omitted rows."
    //
    // The SERVER side is pinned tri-store over real authenticated HTTP
    // (backend/tests/test_private_game_sibling_routes.py). What can only be
    // proven here is the RENDERING of the two projections it now sends,
    // because both had an empty state that would have MISREPRESENTED them:
    // `notifications: []` renders as "No notifications yet." and `audit: []`
    // as "No audit entries." -- claims that THIS GAME has had no activity,
    // when the truth is that this READER is not being shown it.
    // ------------------------------------------------------------
    await page.evaluate(() => switchTab("activity"));
    await waitForView(page, "activity");
    await waitForRealContent(page);
    const ownActivity = await page.evaluate(() => {
      const titles = Array.from(document.querySelectorAll("#content .section-title"))
        .map((t) => (t.textContent || "").trim());
      const auditTitle = titles.find((t) => /^Game audit/.test(t)) || "";
      const m = /\((\d+)\)/.exec(auditTitle);
      const card = Array.from(document.querySelectorAll("#content .section-title"))
        .find((t) => /^Game audit/.test(t.textContent || ""));
      return {
        scopeNote: document.querySelectorAll("[data-activity-scope]").length,
        restricted: document.querySelectorAll("[data-restricted]").length,
        auditTitle,
        claimed: m ? Number(m[1]) : null,
        rendered: card && card.nextElementSibling
          ? card.nextElementSibling.querySelectorAll(".tl-item").length : -1,
      };
    });
    // A Coach reads their OWN side's activity, and is told so -- otherwise a
    // short list reads as a claim about the whole game.
    if (ownActivity.scopeNote !== 1) {
      fail(`Coach [${L}]: the own-side activity feed must say it is scoped to `
        + `this team, found ${ownActivity.scopeNote} scope note(s): `
        + `${JSON.stringify(ownActivity)}`);
    }
    // THE CARDINALITY ORACLE, closed at the surface the coach actually reads:
    // the number in the heading is the number of rows on the screen, never a
    // count of the whole game's log.
    if (ownActivity.claimed === null
        || ownActivity.claimed !== ownActivity.rendered) {
      fail(`Coach [${L}]: "Game audit (N)" must count the rows actually sent, `
        + `not the whole game's log: ${JSON.stringify(ownActivity)}`);
    }

    // THE WITHHELD PROJECTION. An assigned official receives all three fields
    // as null. No browser journey can BE an assigned official mid-run (this
    // page holds a real Coach session and officials are assigned through a
    // separate operator flow), so the payload is injected by rewriting the
    // real /board response in flight -- the same technique, and the same
    // justification, as the ineligible-row leg above: the fetch-to-DOM path
    // is entirely real and the SHAPE injected is exactly the one the Python
    // tests pin for an official.
    await page.route("**/api/games/*/board", async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      body.audit_scope = "withheld";
      body.notifications = null;
      body.audit = null;
      body.audit_count = null;
      await route.fulfill({ response, json: body });
    });
    await page.evaluate(() => switchTab("games"));
    await waitForView(page, "games");
    await page.evaluate(() => switchTab("activity"));
    await waitForView(page, "activity");
    await waitForRealContent(page);
    const withheldActivity = await page.evaluate(() => {
      const text = (document.getElementById("content").textContent || "")
        .replace(/\s+/g, " ");
      const titles = Array.from(document.querySelectorAll("#content .section-title"))
        .map((t) => (t.textContent || "").trim());
      return {
        restricted: document.querySelectorAll("[data-restricted]").length,
        scopeNote: document.querySelectorAll("[data-activity-scope]").length,
        auditTitle: titles.find((t) => /^Game audit/.test(t)) || "",
        saysNoNotifications: /No notifications yet\./.test(text),
        saysNoAudit: /No audit entries\./.test(text),
        saysWithheld: /Game activity not shown/.test(text),
      };
    });
    // WITHHELD IS NOT EMPTY. Both collections get the redacted treatment, and
    // the two misleading empty-state sentences must be gone -- this is the
    // assertion that fails if someone "simplifies" the server back to [].
    if (withheldActivity.restricted !== 2 || !withheldActivity.saysWithheld) {
      fail(`Coach [${L}]: a withheld activity log must render as WITHHELD in `
        + `both cards: ${JSON.stringify(withheldActivity)}`);
    }
    if (withheldActivity.saysNoNotifications || withheldActivity.saysNoAudit) {
      fail(`Coach [${L}]: withheld activity rendered as an EMPTY OPERATIONAL `
        + `STATE -- "no notifications"/"no audit entries" claims this game has `
        + `had no activity, which is a different and false statement: `
        + `${JSON.stringify(withheldActivity)}`);
    }
    // ...and no count survives in the heading, which would be the same oracle
    // in string form.
    if (/\(/.test(withheldActivity.auditTitle)) {
      fail(`Coach [${L}]: the withheld audit heading still carries a count: `
        + `${JSON.stringify(withheldActivity)}`);
    }
    if (withheldActivity.scopeNote !== 0) {
      fail(`Coach [${L}]: the own-side scope note must not appear when the `
        + `whole log is withheld: ${JSON.stringify(withheldActivity)}`);
    }
    await page.unroute("**/api/games/*/board");

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
    await installContextFixture(page);
    await reachDashboard(page);
    await assertForbiddenNoChange(page, reader, tracker, fail, "Coach",
      "/api/v2/setup/venue", { name: "Should Not Exist (coach)", organization_id: null },
      ["/api/v2/setup/overview", "/api/demo/overview"]);
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
    await tabToAndActivate(page, '[data-goto="notifications"]', "Player reach Notifications");
    await waitForView(page, "notifications");
    await page.click('.tab[data-tab="player_home"]');
    await waitForView(page, "player_home");
    await page.evaluate(() => switchTab("setup"));
    await waitForView(page, "setup");
    await page.click('[data-setup-view="records"]').catch(() => {});
    if ((await page.evaluate(() => document.querySelectorAll(".sc-new").length)) !== 0) {
      fail("Player: direct-navigating to Setup must show no \"+ New\" controls");
    }
    // Player's own build-roster probe carries no distinguishing marker a
    // string search could ever catch. Both the game's real lineups AND its
    // per-game AuditLog trail (/api/games/{id}/board's `audit` array --
    // roster_selected/availability_set events, distinct from the setup-only
    // SetupAuditLog above) must be byte-identical -- a rejected attempt
    // must not be recorded as a real audit event either.
    await assertForbiddenNoChange(page, reader, tracker, fail, "Player",
      `/api/games/${game.body.id}/build-roster`, {},
      [`/api/games/${game.body.id}/lineups`, `/api/games/${game.body.id}/board`]);
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
    // Authorized primary action reached by a real, bounded Tab traversal:
    // confirm attendance for the linked junior's real next game -- a
    // genuine authorized mutation (RESPOND_AVAILABILITY), not merely a
    // reachable button.
    const confirmSelector = `[data-g-confirm="${junior.body.id}"]`;
    await page.waitForSelector(confirmSelector, { timeout: 10000 });
    await tabToAndActivate(page, confirmSelector, "Guardian confirm attendance");
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
    // Same two-collection proof as Player's own probe above: the lineups
    // AND the per-game AuditLog trail must both stay byte-identical.
    await assertForbiddenNoChange(page, reader, tracker, fail, "Guardian",
      `/api/games/${game.body.id}/build-roster`, {},
      [`/api/games/${game.body.id}/lineups`, `/api/games/${game.body.id}/board`]);
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
    await tabToAndActivate(page, '.tab[data-tab="notifications"]', "Official reach Notifications");
    await waitForView(page, "notifications");
    await page.click('.tab[data-tab="inbox"]');
    await waitForView(page, "inbox");
    await page.evaluate(() => switchTab("users"));
    await waitForView(page, "users");
    await page.waitForSelector("#content .banner", { timeout: 10000 });
    const offUsersBypass = await page.evaluate(() =>
      (document.querySelector("#content .banner") || {}).textContent || "");
    if (!/League admins only/i.test(offUsersBypass)) {
      fail(`Official: direct-navigating to Users must show the "League `
        + `admins only" guard, got "${offUsersBypass}"`);
    }
    await assertForbiddenNoChange(page, reader, tracker, fail, "Official",
      "/api/setup/official", { name: "Should Not Exist (official)" },
      ["/api/v2/setup/overview", "/api/demo/overview"]);
    await logout(page);

    // ============================================================
    // ASSIGNED Official -- the Roster tab degrades honestly (#427 final
    // blocker, round 2).
    //
    // WHAT ONLY A BROWSER CAN PROVE HERE. The server side is pinned tri-store
    // over real authenticated HTTP (backend/tests/
    // test_private_game_sibling_routes.py). What that cannot show is that the
    // SHIPPED SCREEN was the live path: `canReadAnyPrivateGame()` admits any
    // official_id to the Roster tab, and an official's /lineups sides come
    // back `projection: "submitted_lineup"` with `restricted: false` -- so
    // every "not restricted" gate read TRUE for them and the tab fetched
    // `/availability-summary?team_id=<the side being shown>` on every render,
    // with the side toggle switching teams. Measured in exactly this browser
    // before the fix: 200, both sides, names and per-player availability.
    //
    // Unlike the two injected legs elsewhere in this file, NOTHING is faked:
    // this is a real assigned official's real session reading the real
    // endpoint. Three things are required, and they are different claims --
    // the request is not made, the withheld state is NAMED, and the two
    // FULL-shaped empty states that would misdescribe it are gone.
    // ============================================================
    await loginAs(page, accounts.assigned_official.username, PW);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await waitForView(page, "inbox");
    await waitForRealContent(page);
    const officialGameCalls = [];
    const collectGameCalls = (req) => {
      const u = new URL(req.url());
      if (/^\/api\/games\//.test(u.pathname)) {
        officialGameCalls.push(`${req.method()} ${u.pathname}${u.search}`);
      }
    };
    page.on("request", collectGameCalls);
    await page.evaluate(() => switchTab("roster"));
    await waitForView(page, "roster");
    await waitForRealContent(page);
    // The COACH sub-view explicitly: `gameView` is a module global that an
    // earlier role's journey may have left on "player", and the candidate
    // pool and substitute workflow this leg is about live only on the coach
    // body. Clicked, not assigned -- this is the control a real reader uses.
    await page.click('.seg[data-view="coach"]');
    await waitForRealContent(page);
    // The data-side toggle is the half a landing-only check would miss: it is
    // what turned one side's rollup into both.
    const sideTabs = await page.$$("[data-side]");
    if (sideTabs.length !== 2) {
      fail(`Assigned official [${L}]: expected both side tabs on the Roster `
        + `tab, found ${sideTabs.length} -- the leg below cannot prove the `
        + `toggle no longer fetches the other side's rollup`);
    }
    await page.click('[data-side="away"]');
    await waitForRealContent(page);
    page.off("request", collectGameCalls);
    const leaked = officialGameCalls.filter((c) =>
      /availability-summary|substitute-candidates|substitute-addable/.test(c));
    if (leaked.length) {
      fail(`Assigned official [${L}]: the Roster tab still fetches a route `
        + `that carries private candidate/availability state -- and which now `
        + `refuses this caller, so every render would 403: `
        + `${JSON.stringify(leaked)}`);
    }
    if (!officialGameCalls.some((c) => /\/lineups$/.test(c))) {
      fail(`Assigned official [${L}]: the Roster tab fetched no /lineups at `
        + `all, so the check above passed vacuously: `
        + `${JSON.stringify(officialGameCalls)}`);
    }
    const officialRoster = await page.evaluate(() => {
      const text = (document.getElementById("content").textContent || "")
        .replace(/\s+/g, " ");
      return {
        restricted: document.querySelectorAll("[data-restricted]").length,
        saysAvailWithheld: /Availability not shown/.test(text),
        saysWorkflowWithheld: /Candidate and substitute list not shown/.test(text),
        saysAllOnRoster: /All eligible players are on the roster or in the sub pool\./.test(text),
        saysNoSubs: /No substitutes enrolled\./.test(text),
        // The submitted lineup this reader IS entitled to. In this fixture
        // the Coach's own journey ends with nothing seated, so the honest
        // answer is the Game Sheet's "No lineup submitted" -- a true claim
        // about the SUBMITTED lineup, which is exactly what this projection
        // sends.
        saysNothingSubmitted: /No lineup submitted/.test(text),
        // ...and NOT the full-side instruction, which asserts the team has
        // no players AT ALL and tells the reader to go add some.
        saysTeamHasNoPlayers: /The home team has no players/.test(text),
      };
    });
    // WITHHELD IS NAMED...
    if (!officialRoster.saysAvailWithheld || !officialRoster.saysWorkflowWithheld
        || officialRoster.restricted < 2) {
      fail(`Assigned official [${L}]: the Roster tab must say the availability `
        + `rollup and the candidate/substitute list are WITHHELD, not render `
        + `them as absent: ${JSON.stringify(officialRoster)}`);
    }
    // ...AND NOT RENDERED AS AN EMPTY OPERATIONAL STATE. Both sentences are
    // claims about THIS TEAM's preparedness -- "everyone eligible is already
    // on the roster", "this team has enrolled no substitutes" -- and both are
    // false for a reader who is simply not being shown those populations.
    if (officialRoster.saysAllOnRoster || officialRoster.saysNoSubs) {
      fail(`Assigned official [${L}]: withheld candidate/substitute state was `
        + `rendered as an EMPTY operational claim about the team rather than `
        + `as withheld: ${JSON.stringify(officialRoster)}`);
    }
    // AND THE SAME RULE ONE LAYER UP. An empty `players` array means
    // different things on the two projections: on a full side it is the whole
    // population, on this one it is only what was SUBMITTED. The full-side
    // sentence ("The home team has no players. Add players in Setup first")
    // is a claim about another team's roster that this reader was never shown
    // -- the same mistake as the two above, made by an earlier guard.
    if (officialRoster.saysTeamHasNoPlayers || !officialRoster.saysNothingSubmitted) {
      fail(`Assigned official [${L}]: an empty SUBMITTED lineup must read as `
        + `"no lineup submitted", never as "this team has no players": `
        + `${JSON.stringify(officialRoster)}`);
    }
    // AND THE ROUTE ITSELF REFUSES, probed directly the way a console user
    // would -- so the UI gate is defence in depth, never the boundary.
    for (const teamId of [coachTeam.body.id, rivalTeam.body.id]) {
      const probePath = `/api/games/${game.body.id}/availability-summary`;
      tracker.expect("GET", probePath, 403);
      const probe = await apiGet(page, `${probePath}?team_id=${teamId}`);
      if (probe.status !== 403 || !probe.body.error
          || probe.body.error.code !== "forbidden") {
        fail(`Assigned official [${L}]: ?team_id=${teamId} must be refused, `
          + `got status=${probe.status} body=${JSON.stringify(probe.body)}`);
      }
      if ("players" in probe.body || "counts" in probe.body) {
        fail(`Assigned official [${L}]: the refusal carried a rollup shape -- `
          + `an empty summary is a claim about the team, not about the `
          + `reader: ${JSON.stringify(probe.body)}`);
      }
    }
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
      fail(`Viewer: direct-navigating to Users must show the "League `
        + `admins only" guard, got "${viewerBanner}"`);
    }
    await page.click('.tab[data-tab="standings"]');
    await waitForView(page, "standings");
    await assertForbiddenNoChange(page, reader, tracker, fail, "Viewer",
      "/api/setup/team", { club_id: club.body.id, league_id: level.body.id,
        name: "Should Not Exist (viewer)" },
      ["/api/v2/setup/overview", "/api/demo/overview"]);
    await logout(page);

    // Every registered negative-mutation expectation must have actually
    // occurred -- if a probe silently answered something other than 403
    // (and assertForbiddenNoChange's own explicit status check somehow
    // didn't catch it), an unrealized expectation here would still fail
    // the run rather than pass by omission.
    const unrealized = tracker.unmatched();
    if (unrealized.length) {
      fail(`expected negative-mutation probe(s) never occurred: ${JSON.stringify(unrealized)}`);
    }
    if (tracker.unexpected.length) {
      fail(`unexpected HTTP failure response(s) during this run:\n${tracker.unexpected.join("\n")}`);
    }
    if (errors.length) {
      fail(`console/page errors:\n${errors.join("\n")}`);
    }
    console.log(`[${L}] OK — all seven roles land on their correct `
      + `destination with exactly the right nav, reach one authorized `
      + `action via real bounded keyboard Tab traversal, cannot bypass `
      + `authorization by direct navigation, and every unauthorized `
      + `mutation is rejected by the server with zero actual change to the `
      + `affected collection.`);
  } catch (error) {
    throw new Error(`${error.message}\n--- demo server output ---\n${serverOutput}`);
  } finally {
    if (reader) await reader.context().close();
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
