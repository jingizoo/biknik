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
const fs = require("fs");
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

function assertSessionCookieTransitionWiring() {
  const appSource = fs.readFileSync(
    path.join(BACKEND_DIR, "hockey_scheduler", "web", "static", "app.js"), "utf8");
  const callWindows = [];
  let cursor = 0;
  while ((cursor = appSource.indexOf("sessionCookiePost(", cursor)) !== -1) {
    callWindows.push(appSource.slice(cursor, cursor + 320));
    cursor += "sessionCookiePost(".length;
  }
  const expected = new Map([
    // Explicit and bootstrap auto-login share adoptSignIn(), so the literal
    // transport route must have exactly one source of truth.
    ["/api/auth/login", 1],
    ["/api/auth/logout", 1],
    ["/api/demo/load", 2],
    ["/api/demo/reset", 1],
    ["/api/demo/clear", 1],
    ["/api/admin/factory-reset/execute", 1],
  ]);
  expected.forEach((count, route) => {
    const actual = callWindows.filter((window) => window.includes(route)).length;
    if (actual !== count) {
      throw new Error(`session-cookie route ${route} must be wired through `
        + `sessionCookiePost exactly ${count} time(s), found ${actual}`);
    }
  });
  if (!appSource.includes('return adoptSignIn("admin", DEMO_PASSWORD,')) {
    throw new Error("bootstrap auto-login must reuse the serialized adoptSignIn path");
  }
  const identityBoundCalls = (appSource.match(/runIdentityBoundSessionTransition\(/g) || []).length - 1;
  if (identityBoundCalls !== 4) {
    throw new Error(`the two Demo Load handlers, Reset/Clear handler, and `
      + `factory execute must all occupy the identity-bound auth queue; found `
      + `${identityBoundCalls} call site(s)`);
  }
}

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

function bounded(promise, label, timeoutMs = 10000) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${label} timed out`)), timeoutMs);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
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
    if (slot) {
      // A render may carry the prior settled live-region node while the new
      // card request is already LOADING behind it. DOM-only settlement would
      // return immediately in that state and let a test expire the cookie
      // underneath the still-live authenticated GET. Require the card model's
      // own generation to have settled as well as the visible skeleton.
      const entry = readCardState("home/setup-progress");
      return !slot.querySelector(".skeleton")
        && entry.state !== "loading" && entry.state !== "pending"
        && entry.state !== "stale";
    }
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
  const consoleFailures = []; // realized expected failures, exact path/status
  const inFlight = new Set();
  page.on("request", (request) => inFlight.add(request));
  page.on("requestfinished", (request) => inFlight.delete(request));
  page.on("requestfailed", (request) => inFlight.delete(request));
  page.on("response", (response) => {
    const status = response.status();
    if (status < 400) return;
    let respPath;
    try { respPath = new URL(response.url()).pathname; } catch (_) { respPath = response.url(); }
    const method = response.request().method();
    const match = expected.find((e) =>
      !e.matched && e.method === method && e.path === respPath && e.status === status);
    if (match) { match.matched = true; consoleFailures.push(match); } else {
      unexpected.push(`${method} ${respPath} -> ${status}`);
    }
  });
  page.on("console", (m) => {
    if (m.type() !== "error") return;
    const message = m.text();
    const location = m.location();
    let consolePath = "";
    try { consolePath = new URL(location.url).pathname; } catch (_) { consolePath = location.url || ""; }
    const statusMatch = message.match(/status(?: of)?\s+(\d{3})/i);
    const consoleStatus = statusMatch ? Number(statusMatch[1]) : null;
    const expectedIndex = /Failed to load resource/.test(message)
      ? consoleFailures.findIndex((entry) => entry.path === consolePath
          && (consoleStatus == null || entry.status === consoleStatus))
      : -1;
    if (expectedIndex !== -1) { consoleFailures.splice(expectedIndex, 1); return; }
    errors.push(`[console] ${message} @ ${location.url || "unknown"}`);
  });
  return {
    expect(method, reqPath, status) { expected.push({ method, path: reqPath, status, matched: false }); },
    unexpected,
    unmatched: () => expected.filter((e) => !e.matched),
    async waitForIdle(timeoutMs = 10000, quietMs = 100) {
      const deadline = Date.now() + timeoutMs;
      let quietSince = null;
      while (Date.now() < deadline) {
        if (inFlight.size === 0) {
          if (quietSince == null) quietSince = Date.now();
          if (Date.now() - quietSince >= quietMs) return;
        } else {
          quietSince = null;
        }
        await new Promise((resolve) => setTimeout(resolve, 25));
      }
      const pending = Array.from(inFlight, (request) => {
        let pathname;
        try { pathname = new URL(request.url()).pathname; } catch (_) { pathname = request.url(); }
        return `${request.method()} ${pathname}`;
      });
      throw new Error(`network did not become idle: ${pending.join(", ")}`);
    },
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

    // Every demo lifecycle response replaces the initiating Admin's session
    // cookie. Start from an empty store, then hold the REAL Load response while
    // requesting Viewer sign-in through the app. Viewer must stay queued until
    // Load has applied its Admin cookie and completed its local refresh; after
    // that Viewer wins at the cookie, model, permissions, shell, and render.
    const initialClear = await apiPost(page, "/api/demo/clear", { confirm: "CLEAR" });
    if (initialClear.status !== 200 || initialClear.body.error) {
      fail(`session-lifecycle setup could not clear demo data: `
        + `${JSON.stringify(initialClear)}`);
    }
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await installContextFixture(page);
    await reachDashboard(page);
    await page.waitForSelector("#demo-btn", { state: "visible", timeout: 10000 });
    await page.click("#demo-btn");
    await page.waitForSelector('[data-demo-action="load"]',
      { state: "visible", timeout: 10000 });
    let releaseHeldDemoLoad;
    let markHeldDemoLoad;
    const heldDemoLoadRelease = new Promise((resolve) => {
      releaseHeldDemoLoad = resolve;
    });
    const heldDemoLoad = new Promise((resolve) => {
      markHeldDemoLoad = resolve;
    });
    let demoLoadRequests = 0;
    const queuedViewerLogins = [];
    const holdDemoLoadResponse = async (route) => {
      demoLoadRequests += 1;
      const response = await route.fetch();
      markHeldDemoLoad();
      await heldDemoLoadRelease;
      await route.fulfill({ response });
    };
    const observeLifecycleViewerLogin = async (route) => {
      queuedViewerLogins.push(route.request().postDataJSON().username);
      const response = await route.fetch();
      await route.fulfill({ response });
    };
    await page.route("**/api/demo/load", holdDemoLoadResponse);
    await page.route("**/api/auth/login", observeLifecycleViewerLogin);
    await page.click('[data-demo-action="load"]');
    await heldDemoLoad;
    // The menu item is now hidden, so drive its DOM click directly. A second
    // lifecycle intent must be refused synchronously rather than queuing a
    // second store replacement that supersedes the first one's reconciliation.
    await page.evaluate(() =>
      document.querySelector('[data-demo-action="load"]').click());
    await page.waitForTimeout(100);
    if (demoLoadRequests !== 1) {
      fail(`double-clicking Demo Load dispatched ${demoLoadRequests} store `
        + `replacements before the first response settled`);
    }
    await page.evaluate(([u, p]) => {
      window.__viewerAfterDemoLoad = signIn(u, p);
    }, ["viewer", "demo"]);
    await page.waitForTimeout(100);
    if (queuedViewerLogins.length !== 0) {
      fail(`Viewer login escaped the session-transition queue while Demo Load `
        + `could still set an Admin cookie: ${JSON.stringify(queuedViewerLogins)}`);
    }
    releaseHeldDemoLoad();
    const viewerAfterDemoLoad = await page.evaluate(async () => {
      const result = await window.__viewerAfterDemoLoad;
      delete window.__viewerAfterDemoLoad;
      return result;
    });
    await page.unroute("**/api/demo/load", holdDemoLoadResponse);
    await page.unroute("**/api/auth/login", observeLifecycleViewerLogin);
    if (!viewerAfterDemoLoad) fail("Viewer sign-in after held Demo Load failed");
    const lifecycleBoundary = await page.evaluate(async () => {
      const me = await (await fetch("/api/auth/me", { credentials: "same-origin" })).json();
      return {
        me: me.user && { username: me.user.username, role: me.user.role },
        current: currentUser && { username: currentUser.username, role: currentUser.role },
        currentRole,
        sidebar: (document.getElementById("user-name").textContent || "").trim(),
        manageUsers: hasPerm("manage_users"),
        usersTabHidden: document.querySelector('.tab[data-tab="users"]').style.display === "none",
      };
    });
    if (demoLoadRequests !== 1
        || JSON.stringify(queuedViewerLogins) !== JSON.stringify(["viewer"])
        || lifecycleBoundary.me.username !== "viewer"
        || lifecycleBoundary.current.username !== "viewer"
        || lifecycleBoundary.sidebar !== "viewer"
        || lifecycleBoundary.currentRole !== "viewer"
        || lifecycleBoundary.manageUsers
        || !lifecycleBoundary.usersTabHidden) {
      fail(`held Demo Load left cookie, currentUser, permissions, or shell `
        + `inconsistent: ${JSON.stringify({ demoLoadRequests,
          queuedViewerLogins, lifecycleBoundary })}`);
    }
    const adminAfterLifecycle = await page.evaluate(async () => signIn("admin", "demo"));
    if (!adminAfterLifecycle) fail("Admin restore after session-lifecycle test failed");
    await page.evaluate(() => switchTab("dashboard"));
    await reachDashboard(page);

    // The superseded Load above deliberately skips its UI tail, so it cannot
    // prove the successful path rebuilds the context directory that the
    // session-generation quarantine destroys. Drive a non-superseded Reset
    // through the real menu/modal and require the post-reset options read,
    // tuple+epoch adoption, visible switcher, and authenticated render.
    await page.click("#demo-btn");
    await page.click('[data-demo-action="reset"]');
    await page.waitForSelector('#demo-confirm-input', { timeout: 10000 });
    await page.fill('#demo-confirm-input', "RESET");
    const directResetResponse = page.waitForResponse((response) =>
      response.url().endsWith("/api/demo/reset")
        && response.request().method() === "POST", { timeout: 10000 });
    const directResetContext = page.waitForResponse((response) =>
      response.url().endsWith("/api/context/options")
        && response.request().method() === "GET", { timeout: 10000 });
    await page.click('[data-demo-confirm]');
    const [directResetResult, directResetContextResult] = await Promise.all([
      directResetResponse, directResetContext,
    ]);
    await page.waitForFunction(() => contextOptions && contextEpoch
      && !document.getElementById("context-switcher").hidden
      && document.getElementById("login-screen").hidden,
    null, { timeout: 10000 });
    const directResetState = await page.evaluate(async () => {
      const me = await (await fetch("/api/auth/me", { credentials: "same-origin" })).json();
      const selected = contextOptions && contextOptions.selected;
      return {
        me: me.user && me.user.username,
        current: currentUser && currentUser.username,
        contextEpoch,
        selected,
        programs: contextOptions && contextOptions.programs
          ? contextOptions.programs.length : 0,
        switcherHidden: document.getElementById("context-switcher").hidden,
        loginHidden: document.getElementById("login-screen").hidden,
        signedOutClass: document.body.classList.contains("signed-out"),
      };
    });
    if (directResetResult.status() !== 200
        || directResetContextResult.status() !== 200
        || directResetState.me !== "admin" || directResetState.current !== "admin"
        || !directResetState.contextEpoch || !directResetState.selected
        || directResetState.programs < 1 || directResetState.switcherHidden
        || !directResetState.loginHidden || directResetState.signedOutClass) {
      fail(`successful Demo Reset did not rebuild the quarantined context and `
        + `authenticated shell: ${JSON.stringify({ resetStatus: directResetResult.status(),
          contextStatus: directResetContextResult.status(), directResetState })}`);
    }
    await page.evaluate(() => switchTab("dashboard"));
    await reachDashboard(page);

    // Login's cookie/adoption phase must release the auth queue before its
    // context/render tail. Hold that tail, then Sign out: logout must reach
    // the server before the held context response is released, and the stale
    // login continuation must not repaint after logout wins.
    let releaseSignInContext;
    let markSignInContextHeld;
    const signInContextRelease = new Promise((resolve) => {
      releaseSignInContext = resolve;
    });
    const signInContextHeld = new Promise((resolve) => {
      markSignInContextHeld = resolve;
    });
    let reconcileLogoutRequests = 0;
    const holdSignInContext = async (route) => {
      const response = await route.fetch();
      markSignInContextHeld();
      await signInContextRelease;
      await route.fulfill({ response });
    };
    const observeReconcileLogout = async (route) => {
      reconcileLogoutRequests += 1;
      const response = await route.fetch();
      await route.fulfill({ response });
    };
    await page.route("**/api/context/options", holdSignInContext);
    await page.route("**/api/auth/logout", observeReconcileLogout);
    await page.evaluate(() => {
      window.__heldContextSignIn = signIn("viewer", "demo")
        .catch((error) => ({ error: error && error.name }));
    });
    await signInContextHeld;
    await page.evaluate(() => {
      window.__logoutDuringSignInTail = document.getElementById("signout-btn").onclick()
        .catch((error) => ({ error: error && error.name }));
    });
    await page.waitForTimeout(150);
    const logoutRequestsBeforeContextRelease = reconcileLogoutRequests;
    releaseSignInContext();
    const signInLogoutOutcomes = await page.evaluate(async () => {
      const outcomes = await Promise.all([
        window.__heldContextSignIn, window.__logoutDuringSignInTail,
      ]);
      delete window.__heldContextSignIn;
      delete window.__logoutDuringSignInTail;
      return outcomes;
    });
    await page.unroute("**/api/context/options", holdSignInContext);
    await page.unroute("**/api/auth/logout", observeReconcileLogout);
    const signedOutAfterHeldContext = await page.evaluate(async () => {
      const me = await (await fetch("/api/auth/me", { credentials: "same-origin" })).json();
      return {
        me: me.user || null,
        current: currentUser,
        loginVisible: !document.getElementById("login-screen").hidden,
        shellSignedOut: document.body.classList.contains("signed-out"),
        sidebar: (document.getElementById("user-name").textContent || "").trim(),
        manageUsers: hasPerm("manage_users"),
      };
    });
    if (logoutRequestsBeforeContextRelease !== 1
        || reconcileLogoutRequests !== 1
        || signedOutAfterHeldContext.me !== null
        || signedOutAfterHeldContext.current !== null
        || !signedOutAfterHeldContext.loginVisible
        || !signedOutAfterHeldContext.shellSignedOut
        || signedOutAfterHeldContext.sidebar !== "Signed out"
        || signedOutAfterHeldContext.manageUsers) {
      fail(`held post-login context starved Sign out or repainted a privileged `
        + `identity afterward: ${JSON.stringify({ logoutRequestsBeforeContextRelease,
          reconcileLogoutRequests, signInLogoutOutcomes, signedOutAfterHeldContext })}`);
    }
    const adminAfterHeldContext = await page.evaluate(() => signIn("admin", "demo"));
    if (!adminAfterHeldContext) fail("Admin restore after held sign-in context failed");
    await page.evaluate(() => switchTab("dashboard"));
    await reachDashboard(page);

    // A successful sign-in is not fully adopted until its context/deep-link/
    // render tail settles. Hold Viewer's first context read, then let a focus
    // /auth/me verdict confirm the SAME Viewer signature. The newer canonical
    // verdict must still cancel that first adoption and issue a fresh context
    // rebuild; signature equality cannot lease a response across an unresolved
    // adoption window (the same mechanism protects server-side A→B→A).
    let releaseFirstViewerContext;
    let markFirstViewerContextHeld;
    const firstViewerContextRelease = new Promise((resolve) => {
      releaseFirstViewerContext = resolve;
    });
    const firstViewerContextHeld = new Promise((resolve) => {
      markFirstViewerContextHeld = resolve;
    });
    let releaseSecondViewerContext;
    let markSecondViewerContextHeld;
    const secondViewerContextRelease = new Promise((resolve) => {
      releaseSecondViewerContext = resolve;
    });
    const secondViewerContextHeld = new Promise((resolve) => {
      markSecondViewerContextHeld = resolve;
    });
    let viewerContextReads = 0;
    const holdViewerAdoptionContexts = async (route) => {
      viewerContextReads += 1;
      const response = await route.fetch();
      const captured = await response.json();
      if (viewerContextReads === 1) {
        markFirstViewerContextHeld();
        await firstViewerContextRelease;
        try { await route.fulfill({ response, json: captured }); }
        catch (_) { /* canonical R2 cancelled the first adoption read. */ }
        return;
      }
      if (viewerContextReads !== 2) {
        fail(`same-signature Viewer adoption issued an unexpected context read `
          + `${viewerContextReads}`);
      }
      markSecondViewerContextHeld();
      await secondViewerContextRelease;
      await route.fulfill({ response, json: captured });
    };
    await page.route("**/api/context/options", holdViewerAdoptionContexts);
    await page.evaluate(() => {
      window.__sameSignatureViewerSignIn = signIn("viewer", "demo");
    });
    await bounded(firstViewerContextHeld, "first Viewer adoption context");
    await page.evaluate(() => window.dispatchEvent(new Event("focus")));
    await bounded(secondViewerContextHeld, "fresh same-signature Viewer context");
    releaseFirstViewerContext();
    const firstViewerAdoptionOutcome = await page.evaluate(async () => {
      const outcome = await window.__sameSignatureViewerSignIn;
      delete window.__sameSignatureViewerSignIn;
      return outcome;
    });
    const viewerBetweenAdoptions = await page.evaluate(() => ({
      current: currentUser && currentUser.username,
      contextOptions,
      loginVisible: !document.getElementById("login-screen").hidden,
      shellSignedOut: document.body.classList.contains("signed-out"),
    }));
    if (firstViewerAdoptionOutcome !== false || viewerContextReads !== 2
        || viewerBetweenAdoptions.current !== "viewer"
        || viewerBetweenAdoptions.contextOptions !== null
        || !viewerBetweenAdoptions.loginVisible
        || !viewerBetweenAdoptions.shellSignedOut) {
      releaseSecondViewerContext();
      fail(`same-signature canonical verdict did not cancel the first Viewer `
        + `adoption tail: ${JSON.stringify({ firstViewerAdoptionOutcome,
          viewerContextReads, viewerBetweenAdoptions })}`);
    }
    releaseSecondViewerContext();
    await page.waitForFunction(() => currentUser
      && currentUser.username === "viewer"
      && contextOptions && contextEpoch
      && !resumeSessionValidationInFlight
      && document.getElementById("login-screen").hidden,
    null, { timeout: 10000 });
    await page.unroute("**/api/context/options", holdViewerAdoptionContexts);
    const finalViewerAdoption = await page.evaluate(() => ({
      current: currentUser && currentUser.username,
      contextReady: !!(contextOptions && contextEpoch),
      loginHidden: document.getElementById("login-screen").hidden,
      manageUsers: hasPerm("manage_users"),
    }));
    if (viewerContextReads !== 2 || finalViewerAdoption.current !== "viewer"
        || !finalViewerAdoption.contextReady
        || !finalViewerAdoption.loginHidden
        || finalViewerAdoption.manageUsers) {
      fail(`fresh same-signature Viewer rebuild did not become authoritative: `
        + `${JSON.stringify({ viewerContextReads, finalViewerAdoption })}`);
    }
    const adminAfterViewerAdoptionLease = await page.evaluate(() =>
      signIn("admin", "demo"));
    if (!adminAfterViewerAdoptionLease) {
      fail("Admin restore after same-signature Viewer adoption test failed");
    }
    await page.evaluate(() => switchTab("dashboard"));
    await reachDashboard(page);

    // A failed logout must not claim success over a still-live privileged
    // cookie. Force the exact 500 shape, then require the server cookie,
    // currentUser, permissions, and visible shell all to remain Admin.
    const forceLogoutFailure = async (route) => {
      await route.fulfill({
        status: 500,
        contentType: "application/json",
        body: JSON.stringify({ error: {
          code: "forced_logout_failure",
          message: "Forced logout failure for session-boundary test",
        } }),
      });
    };
    tracker.expect("POST", "/api/auth/logout", 500);
    await page.route("**/api/auth/logout", forceLogoutFailure);
    const failedLogoutOutcome = await page.evaluate(() =>
      document.getElementById("signout-btn").onclick());
    await page.unroute("**/api/auth/logout", forceLogoutFailure);
    const afterFailedLogout = await page.evaluate(async () => {
      const me = await (await fetch("/api/auth/me", { credentials: "same-origin" })).json();
      return {
        me: me.user && { username: me.user.username, role: me.user.role },
        current: currentUser && { username: currentUser.username, role: currentUser.role },
        loginHidden: document.getElementById("login-screen").hidden,
        shellSignedOut: document.body.classList.contains("signed-out"),
        sidebar: (document.getElementById("user-name").textContent || "").trim(),
        manageUsers: hasPerm("manage_users"),
        toast,
      };
    });
    if (failedLogoutOutcome !== false
        || !afterFailedLogout.me || afterFailedLogout.me.username !== "admin"
        || !afterFailedLogout.current || afterFailedLogout.current.username !== "admin"
        || !afterFailedLogout.loginHidden || afterFailedLogout.shellSignedOut
        || afterFailedLogout.sidebar !== "admin" || !afterFailedLogout.manageUsers
        || !/Forced logout failure/.test(afterFailedLogout.toast)) {
      fail(`failed logout claimed success or desynchronized the live Admin `
        + `session: ${JSON.stringify({ failedLogoutOutcome, afterFailedLogout })}`);
    }

    // A cookie belongs to the browser context, not to one document. Prove a
    // real peer-tab login synchronously strips this tab's already-painted
    // Admin state before its canonical /auth/me reconciliation is released,
    // then converges both models to the cookie's Viewer identity.
    const peerErrors = [];
    const peerPage = await context.newPage();
    peerPage.on("pageerror", (error) => peerErrors.push(`[pageerror] ${error.message}`));
    const peerTracker = makeFailureTracker(peerPage, peerErrors);
    await peerPage.goto(base, { waitUntil: "domcontentloaded" });
    await peerPage.waitForFunction(() => currentUser
      && currentUser.username === "admin"
      && document.getElementById("login-screen").hidden,
    null, { timeout: 10000 });
    const crossTabSentinel = `Cross-tab Admin private sentinel ${suffix}`;
    await page.evaluate((sentinel) => {
      officialsPool = [{ id: "cross-tab-private-official", name: sentinel }];
      usersState = { accounts: [{ id: "cross-tab-private-account",
        username: sentinel, role: "league_admin", active: true }], sessions: [] };
      document.getElementById("content").innerHTML =
        `<section data-cross-tab-private>${sentinel}</section>`;
    }, crossTabSentinel);
    let releaseCrossTabMe;
    let markCrossTabMeHeld;
    const crossTabMeRelease = new Promise((resolve) => {
      releaseCrossTabMe = resolve;
    });
    const crossTabMeHeld = new Promise((resolve) => {
      markCrossTabMeHeld = resolve;
    });
    let crossTabMeRequests = 0;
    const holdCrossTabMe = async (route) => {
      crossTabMeRequests += 1;
      const response = await route.fetch();
      markCrossTabMeHeld();
      await crossTabMeRelease;
      await route.fulfill({ response });
    };
    await page.route("**/api/auth/me", holdCrossTabMe);
    await peerPage.evaluate(() => {
      window.__peerViewerSignIn = signIn("viewer", "demo");
    });
    await crossTabMeHeld;
    const crossTabQuarantine = await page.evaluate((sentinel) => ({
      current: currentUser,
      epoch: uiIdentityEpoch,
      officials: officialsPool.length,
      accounts: usersState.accounts.length,
      contextOptions,
      contextEpoch,
      hasSentinel: document.documentElement.textContent.includes(sentinel),
      contentChildren: document.getElementById("content").children.length,
      loginVisible: !document.getElementById("login-screen").hidden,
      shellSignedOut: document.body.classList.contains("signed-out"),
      manageUsers: hasPerm("manage_users"),
    }), crossTabSentinel);
    if (crossTabQuarantine.current !== null
        || crossTabQuarantine.officials !== 0
        || crossTabQuarantine.accounts !== 0
        || crossTabQuarantine.contextOptions !== null
        || crossTabQuarantine.contextEpoch !== null
        || crossTabQuarantine.hasSentinel
        || crossTabQuarantine.contentChildren !== 0
        || !crossTabQuarantine.loginVisible
        || !crossTabQuarantine.shellSignedOut
        || crossTabQuarantine.manageUsers) {
      fail(`peer-tab Viewer login left Admin state painted before canonical `
        + `/auth/me reconciliation: ${JSON.stringify(crossTabQuarantine)}`);
    }
    releaseCrossTabMe();
    const peerViewerResult = await peerPage.evaluate(async () => {
      const result = await window.__peerViewerSignIn;
      delete window.__peerViewerSignIn;
      return result;
    });
    await page.unroute("**/api/auth/me", holdCrossTabMe);
    await page.waitForFunction(() => currentUser && currentUser.role === "viewer"
      && document.getElementById("login-screen").hidden,
    null, { timeout: 10000 });
    const crossTabViewer = await page.evaluate(async () => {
      const me = await (await fetch("/api/auth/me", { credentials: "same-origin" })).json();
      return {
        me: me.user && me.user.role,
        current: currentUser && currentUser.role,
        hasAdmin: hasPerm("manage_users"),
        usersVisible: document.querySelector('.tab[data-tab="users"]').offsetParent !== null,
      };
    });
    if (!peerViewerResult || crossTabMeRequests !== 1
        || crossTabViewer.me !== "viewer" || crossTabViewer.current !== "viewer"
        || crossTabViewer.hasAdmin || crossTabViewer.usersVisible) {
      fail(`peer-tab session reconciliation did not converge both tabs to `
        + `Viewer: ${JSON.stringify({ peerViewerResult, crossTabMeRequests,
          crossTabViewer })}`);
    }

    // Web Locks must serialize the COMPLETE mutation/adoption phase across
    // tabs. Hold A's same-Viewer login response, start B's Admin login, and
    // prove B cannot even dispatch until A settles. Hold B in turn so A's
    // same-username boundary can be inspected before the different-identity
    // mutation masks it; then require the later Admin mutation to win in the
    // cookie and both in-memory models.
    const sameUserSentinel = `Same-user stale private sentinel ${suffix}`;
    const sameUserEpoch = await page.evaluate((sentinel) => {
      document.getElementById("content").innerHTML =
        `<section data-same-user-private>${sentinel}</section>`;
      officialsPool = [{ id: "same-user-private", name: sentinel }];
      return uiIdentityEpoch;
    }, sameUserSentinel);
    let releaseViewerLogin;
    let markViewerLoginHeld;
    const viewerLoginRelease = new Promise((resolve) => { releaseViewerLogin = resolve; });
    const viewerLoginHeld = new Promise((resolve) => { markViewerLoginHeld = resolve; });
    const holdViewerLogin = async (route) => {
      if (route.request().postDataJSON().username !== "viewer") return route.continue();
      const response = await route.fetch();
      markViewerLoginHeld();
      await viewerLoginRelease;
      await route.fulfill({ response });
    };
    let releasePeerAdminLogin;
    let markPeerAdminLoginHeld;
    const peerAdminLoginRelease = new Promise((resolve) => {
      releasePeerAdminLogin = resolve;
    });
    const peerAdminLoginHeld = new Promise((resolve) => {
      markPeerAdminLoginHeld = resolve;
    });
    let peerAdminLoginRequests = 0;
    const holdPeerAdminLogin = async (route) => {
      if (route.request().postDataJSON().username !== "admin") return route.continue();
      peerAdminLoginRequests += 1;
      const response = await route.fetch();
      markPeerAdminLoginHeld();
      await peerAdminLoginRelease;
      await route.fulfill({ response });
    };
    await page.route("**/api/auth/login", holdViewerLogin);
    await peerPage.route("**/api/auth/login", holdPeerAdminLogin);
    await page.evaluate(() => { window.__sameViewerLogin = signIn("viewer", "demo"); });
    await viewerLoginHeld;
    await peerPage.evaluate(() => { window.__laterAdminLogin = signIn("admin", "demo"); });
    await page.waitForTimeout(150);
    if (peerAdminLoginRequests !== 0) {
      fail(`origin-wide session lock allowed the later Admin login to dispatch `
        + `while the Viewer mutation was unresolved`);
    }
    releaseViewerLogin();
    await peerAdminLoginHeld;
    const sameViewerResult = await page.evaluate(async () => {
      const result = await window.__sameViewerLogin;
      delete window.__sameViewerLogin;
      return {
        result,
        epoch: uiIdentityEpoch,
        current: currentUser && currentUser.role,
        officials: officialsPool.length,
        hasSentinel: document.documentElement.textContent.includes(
          document.querySelector("[data-same-user-private]")?.textContent || "__gone__"),
        privateNode: !!document.querySelector("[data-same-user-private]"),
        setUserAcceptsForce: /forceSessionBoundary/.test(setUser.toString()),
        adoptUsesForce: /setUser\(r\.user, true\)/.test(adoptSignIn.toString()),
      };
    });
    if (!sameViewerResult.result || sameViewerResult.epoch <= sameUserEpoch
        || sameViewerResult.current !== "viewer"
        || sameViewerResult.officials !== 0 || sameViewerResult.privateNode) {
      fail(`same-username login failed to establish a fresh session boundary: `
        + `${JSON.stringify({ sameUserEpoch, sameViewerResult })}`);
    }
    releasePeerAdminLogin();
    const laterAdminResult = await peerPage.evaluate(async () => {
      const result = await window.__laterAdminLogin;
      delete window.__laterAdminLogin;
      return result;
    });
    await page.unroute("**/api/auth/login", holdViewerLogin);
    await peerPage.unroute("**/api/auth/login", holdPeerAdminLogin);
    await page.waitForFunction(() => currentUser && currentUser.username === "admin"
      && document.getElementById("login-screen").hidden
      && !externalSessionReconcileInFlight,
    null, { timeout: 10000 });
    const orderedSessionState = await Promise.all([page, peerPage].map((p) =>
      p.evaluate(async () => {
        const me = await (await fetch("/api/auth/me", { credentials: "same-origin" })).json();
        return {
          me: me.user && me.user.username,
          current: currentUser && currentUser.username,
          manageUsers: hasPerm("manage_users"),
        };
      })));
    if (!laterAdminResult || peerAdminLoginRequests !== 1
        || orderedSessionState.some((state) => state.me !== "admin"
          || state.current !== "admin" || !state.manageUsers)) {
      fail(`serialized cross-tab login order did not converge to the later `
        + `Admin mutation: ${JSON.stringify({ laterAdminResult,
          peerAdminLoginRequests, orderedSessionState })}`);
    }
    await peerTracker.waitForIdle();
    if (peerTracker.unexpected.length || peerTracker.unmatched().length
        || peerErrors.length) {
      fail(`peer-tab journey produced untracked failures: ${JSON.stringify({
        unexpected: peerTracker.unexpected,
        unmatched: peerTracker.unmatched(), errors: peerErrors })}`);
    }
    await peerPage.close();

    // A browser can resolve fetch() at response HEADERS while response.text()
    // remains blocked on a streaming body. Route interception normally holds
    // both together, so drive the real transport contract with synthetic
    // Response streams. For a 401 the old Admin model remains authoritative,
    // yet Sign out must still acquire the origin lock and dispatch; for a 200
    // the successful-header boundary must synchronously quarantine Admin even
    // before the body, and Sign out must likewise not wait for that body.
    for (const streamStatus of [401, 200]) {
      const streamSentinel = `stream-${streamStatus}-private-${suffix}`;
      await page.evaluate(({ status, sentinel, viewer }) => {
        document.getElementById("content").innerHTML =
          `<section data-stream-private>${sentinel}</section>`;
        officialsPool = [{ id: `stream-${status}`, name: sentinel }];
        const originalFetch = window.fetch;
        window.__streamAuth = {
          originalFetch, headers: false, logoutDispatched: false,
          release: null, login: null, logout: null,
        };
        window.fetch = async (input, init = {}) => {
          const url = new URL(typeof input === "string" ? input : input.url,
            location.href);
          if (url.pathname === "/api/auth/login"
              && init.method === "POST"
              && JSON.parse(init.body || "{}").password === "stream-stall") {
            const payload = status === 200
              ? { user: { username: viewer, role: "viewer", scope: {},
                  label: "Stream Viewer" } }
              : { error: { code: "invalid_credentials",
                  message: "Synthetic streamed refusal" } };
            const bytes = new TextEncoder().encode(JSON.stringify(payload));
            const stream = new ReadableStream({
              start(controller) {
                window.__streamAuth.release = () => {
                  controller.enqueue(bytes);
                  controller.close();
                };
              },
            });
            window.__streamAuth.headers = true;
            return new Response(stream, {
              status,
              headers: { "Content-Type": "application/json" },
            });
          }
          if (url.pathname === "/api/auth/logout" && init.method === "POST") {
            window.__streamAuth.logoutDispatched = true;
          }
          return originalFetch(input, init);
        };
        window.__streamAuth.login = signIn(viewer, "stream-stall");
      }, { status: streamStatus, sentinel: streamSentinel,
        viewer: `matrix_stream_viewer_${suffix}` });
      await page.waitForFunction(() => window.__streamAuth
        && window.__streamAuth.headers && window.__streamAuth.release,
      null, { timeout: 10000 });
      if (streamStatus === 200) {
        const atHeaders = await page.evaluate((sentinel) => ({
          current: currentUser,
          officials: officialsPool.length,
          privateNode: !!document.querySelector("[data-stream-private]"),
          hasSentinel: document.documentElement.textContent.includes(sentinel),
          loginVisible: !document.getElementById("login-screen").hidden,
          shellSignedOut: document.body.classList.contains("signed-out"),
          manageUsers: hasPerm("manage_users"),
        }), streamSentinel);
        if (atHeaders.current !== null || atHeaders.officials !== 0
            || atHeaders.privateNode || atHeaders.hasSentinel
            || !atHeaders.loginVisible || !atHeaders.shellSignedOut
            || atHeaders.manageUsers) {
          await page.evaluate(() => window.__streamAuth.release());
          fail(`successful login headers retained private Admin state while `
            + `the body streamed: ${JSON.stringify(atHeaders)}`);
        }
      }
      await page.evaluate(() => {
        window.__streamAuth.logout = signOut();
      });
      await page.waitForTimeout(150);
      const logoutDispatchedBeforeBody = await page.evaluate(() =>
        window.__streamAuth.logoutDispatched);
      await page.evaluate(() => window.__streamAuth.release());
      const streamedOutcomes = await page.evaluate(async () => {
        const values = await Promise.all([
          window.__streamAuth.login, window.__streamAuth.logout,
        ]);
        window.fetch = window.__streamAuth.originalFetch;
        delete window.__streamAuth;
        return values;
      });
      if (!logoutDispatchedBeforeBody) {
        fail(`a streamed ${streamStatus} login body held the origin lock and `
          + `starved Sign out: ${JSON.stringify(streamedOutcomes)}`);
      }
      const afterStream = await page.evaluate(async () => {
        const response = await fetch("/api/auth/me", { credentials: "same-origin" });
        const body = await response.json();
        return { status: response.status, server: body.user || null,
          current: currentUser, login: !document.getElementById("login-screen").hidden,
          admin: hasPerm("manage_users") };
      });
      if (streamedOutcomes[1] !== true || afterStream.server !== null
          || afterStream.current !== null || !afterStream.login
          || afterStream.admin) {
        fail(`streamed ${streamStatus} login tail repainted after Sign out: `
          + `${JSON.stringify({ streamedOutcomes, afterStream })}`);
      }
      const restoreAfterStream = await page.evaluate(() => signIn("admin", "demo"));
      if (!restoreAfterStream) fail(`Admin restore after streamed ${streamStatus} test failed`);
      await page.evaluate(async () => {
        switchTab("dashboard");
        // switchTab intentionally starts render() without returning its
        // promise. Await a newer pass before injecting the next leg's private
        // sentinel so an ordinary late Dashboard repaint cannot masquerade as
        // stale-generation quarantine.
        await render();
      });
      await reachDashboard(page);
      await waitForCardSettled(page);
      await tracker.waitForIdle();
    }

    // Storage eviction can land after a real successful login's 2xx headers
    // installed the Viewer cookie and quarantined Admin, but before the body
    // supplies the user model. Hold only that body, erase the published
    // generation, and focus. The valid Viewer cookie must be recoverable from
    // quarantine even though currentUser is null; fresh signed-out pages
    // (live===observed==='') must not receive this exception.
    await page.evaluate(() => {
      const originalFetch = window.fetch;
      window.__evictedLoginBody = {
        originalFetch, headers: false, release: null, outcome: null,
      };
      window.fetch = async (input, init = {}) => {
        const url = new URL(typeof input === "string" ? input : input.url,
          location.href);
        if (url.pathname === "/api/auth/login" && init.method === "POST"
            && JSON.parse(init.body || "{}").username === "viewer") {
          const response = await originalFetch(input, init);
          const bytes = new TextEncoder().encode(await response.text());
          const stream = new ReadableStream({
            start(controller) {
              window.__evictedLoginBody.release = () => {
                controller.enqueue(bytes);
                controller.close();
              };
            },
          });
          window.__evictedLoginBody.headers = true;
          return new Response(stream, {
            status: response.status,
            headers: { "Content-Type": "application/json" },
          });
        }
        return originalFetch(input, init);
      };
      window.__evictedLoginBody.outcome = signIn("viewer", "demo");
    });
    await page.waitForFunction(() => window.__evictedLoginBody
      && window.__evictedLoginBody.headers
      && window.__evictedLoginBody.release,
    null, { timeout: 10000 });
    const evictedLoginAtHeaders = await page.evaluate(() => ({
      current: currentUser,
      observed: observedSessionMutationToken,
      live: readSessionMutationToken(),
      loginVisible: !document.getElementById("login-screen").hidden,
      admin: hasPerm("manage_users"),
    }));
    if (evictedLoginAtHeaders.current !== null
        || !evictedLoginAtHeaders.observed
        || evictedLoginAtHeaders.observed !== evictedLoginAtHeaders.live
        || !evictedLoginAtHeaders.loginVisible
        || evictedLoginAtHeaders.admin) {
      await page.evaluate(() => window.__evictedLoginBody.release());
      fail(`successful Viewer headers did not establish quarantine before the `
        + `held body: ${JSON.stringify(evictedLoginAtHeaders)}`);
    }
    await page.evaluate((key) => {
      localStorage.removeItem(key);
      window.dispatchEvent(new Event("focus"));
    }, "hs_session_mutation_v1");
    await page.waitForFunction(() => currentUser && currentUser.username === "viewer"
      && observedSessionMutationToken === ""
      && readSessionMutationToken() === ""
      && !resumeSessionValidationInFlight
      && document.getElementById("login-screen").hidden,
    null, { timeout: 10000 });
    const evictedLoginOutcome = await page.evaluate(async () => {
      window.__evictedLoginBody.release();
      const result = await window.__evictedLoginBody.outcome;
      const meResponse = await window.__evictedLoginBody.originalFetch.call(window,
        "/api/auth/me", { credentials: "same-origin" });
      const me = await meResponse.json();
      const state = {
        result,
        me: me.user && me.user.username,
        current: currentUser && currentUser.username,
        observed: observedSessionMutationToken,
        live: readSessionMutationToken(),
        manageUsers: hasPerm("manage_users"),
      };
      window.fetch = window.__evictedLoginBody.originalFetch;
      delete window.__evictedLoginBody;
      return state;
    });
    if (evictedLoginOutcome.result !== false
        || evictedLoginOutcome.me !== "viewer"
        || evictedLoginOutcome.current !== "viewer"
        || evictedLoginOutcome.observed !== ""
        || evictedLoginOutcome.live !== ""
        || evictedLoginOutcome.manageUsers) {
      fail(`storage eviction during a held successful login body stranded or `
        + `mis-adopted the valid cookie: ${JSON.stringify(evictedLoginOutcome)}`);
    }
    const restoreAfterEvictedLogin = await page.evaluate(() => signIn("admin", "demo"));
    if (!restoreAfterEvictedLogin) fail("Admin restore after evicted login body failed");
    await page.evaluate(async () => {
      switchTab("dashboard");
      await render();
    });
    await reachDashboard(page);
    await waitForCardSettled(page);
    await tracker.waitForIdle();

    // The same empty-generation recovery must settle an authoritative
    // anonymous verdict, not only a surviving identity. Otherwise every later
    // focus repeats /auth/me and a refused login's error tail is discarded by
    // observed=old/live='' forever.
    const signedOutBeforeAnonymousEviction = await page.evaluate(() => signOut());
    if (!signedOutBeforeAnonymousEviction) {
      fail("anonymous eviction setup could not sign Admin out");
    }
    await page.evaluate((key) => {
      localStorage.removeItem(key);
      window.dispatchEvent(new Event("focus"));
    }, "hs_session_mutation_v1");
    await page.waitForFunction(() => currentUser === null
      && observedSessionMutationToken === ""
      && readSessionMutationToken() === ""
      && !resumeSessionValidationInFlight,
    null, { timeout: 10000 });
    let settledAnonymousFocusReads = 0;
    const countSettledAnonymousFocus = async (route) => {
      settledAnonymousFocusReads += 1;
      await route.continue();
    };
    await page.route("**/api/auth/me", countSettledAnonymousFocus);
    await page.evaluate(() => window.dispatchEvent(new Event("focus")));
    await page.waitForTimeout(150);
    await page.unroute("**/api/auth/me", countSettledAnonymousFocus);
    tracker.expect("POST", "/api/auth/login", 401);
    const invalidAfterAnonymousEviction = await page.evaluate(async () => {
      const outcome = await signIn("admin", "definitely-wrong-password");
      const error = document.getElementById("login-error");
      return {
        outcome,
        current: currentUser,
        observed: observedSessionMutationToken,
        live: readSessionMutationToken(),
        loginVisible: !document.getElementById("login-screen").hidden,
        errorVisible: !!(error && !error.hidden),
        errorText: error ? error.textContent : "",
      };
    });
    if (settledAnonymousFocusReads !== 0
        || invalidAfterAnonymousEviction.outcome !== false
        || invalidAfterAnonymousEviction.current !== null
        || invalidAfterAnonymousEviction.observed !== ""
        || invalidAfterAnonymousEviction.live !== ""
        || !invalidAfterAnonymousEviction.loginVisible
        || !invalidAfterAnonymousEviction.errorVisible
        || !/invalid|credential|password/i.test(
          invalidAfterAnonymousEviction.errorText)) {
      fail(`anonymous empty-generation recovery repeated or suppressed the `
        + `next login result: ${JSON.stringify({ settledAnonymousFocusReads,
          invalidAfterAnonymousEviction })}`);
    }
    const restoreAfterAnonymousEviction = await page.evaluate(() =>
      signIn("admin", "demo"));
    if (!restoreAfterAnonymousEviction) {
      fail("Admin restore after anonymous empty-generation recovery failed");
    }
    await page.evaluate(async () => {
      switchTab("dashboard");
      await render();
    });
    await reachDashboard(page);
    await waitForCardSettled(page);
    await tracker.waitForIdle();

    // A stale storage event must be ignored at synchronous entry once this tab
    // has already adopted the live T2 generation: no quarantine, no /auth/me,
    // and no private DOM loss. Then persist T3 without delivering its event and
    // prove an old-shell identity-bound action is refused before dispatch.
    const staleEventToken = `matrix-stale-event-t1-${suffix}`;
    const adoptedToken = `matrix-adopted-t2-${suffix}`;
    let staleEventMeRequests = 0;
    const countStaleEventMe = async (route) => {
      staleEventMeRequests += 1;
      await route.continue();
    };
    await page.route("**/api/auth/me", countStaleEventMe);
    const staleEventSentinel = `Already adopted T2 sentinel ${suffix}`;
    await page.evaluate(({ key, adopted, stale, sentinel }) => {
      localStorage.setItem(key, adopted);
      observedSessionMutationToken = adopted;
      officialsPool = [{ id: "adopted-t2-private", name: sentinel }];
      document.getElementById("content").innerHTML =
        `<section data-adopted-t2-private>${sentinel}</section>`;
      reconcileExternalSessionMutation(stale);
    }, { key: "hs_session_mutation_v1", adopted: adoptedToken,
      stale: staleEventToken, sentinel: staleEventSentinel });
    await page.waitForTimeout(100);
    const afterStaleEvent = await page.evaluate((sentinel) => ({
      current: currentUser && currentUser.username,
      observed: observedSessionMutationToken,
      live: readSessionMutationToken(),
      officials: officialsPool.length,
      privateNode: !!document.querySelector("[data-adopted-t2-private]"),
      hasSentinel: document.documentElement.textContent.includes(sentinel),
      loginHidden: document.getElementById("login-screen").hidden,
      manageUsers: hasPerm("manage_users"),
    }), staleEventSentinel);
    await page.unroute("**/api/auth/me", countStaleEventMe);
    if (staleEventMeRequests !== 0 || afterStaleEvent.current !== "admin"
        || afterStaleEvent.observed !== adoptedToken
        || afterStaleEvent.live !== adoptedToken
        || afterStaleEvent.officials !== 1 || !afterStaleEvent.privateNode
        || !afterStaleEvent.hasSentinel || !afterStaleEvent.loginHidden
        || !afterStaleEvent.manageUsers) {
      fail(`a delayed T1 storage event regressed an already-adopted T2 tab: `
        + `${JSON.stringify({ staleEventMeRequests, afterStaleEvent })}`);
    }

    // Explicit authentication is allowed to proceed under the cookie that
    // won the preceding origin-lock slot even when this tab's storage event
    // task is late. That exception must not preserve stale private Admin DOM
    // until the logout response arrives, though. Publish an unseen peer token,
    // hold both canonical reconciliation and logout before their headers, and
    // prove lock entry synchronously quarantines while logout still dispatches.
    const explicitLagToken = `matrix-explicit-lag-${suffix}`;
    const explicitLagSentinel = `Explicit lag private sentinel ${suffix}`;
    await page.evaluate(({ key, token, sentinel }) => {
      officialsPool = [{ id: "explicit-lag-private", name: sentinel }];
      document.getElementById("content").innerHTML =
        `<section data-explicit-lag-private>${sentinel}</section>`;
      const originalFetch = window.fetch;
      const refused = () => new Response(JSON.stringify({
        error: { code: "session_missing", message: "Synthetic refusal" },
      }), { status: 401, headers: { "Content-Type": "application/json" } });
      window.__explicitLag = {
        originalFetch, logoutDispatched: false, meReleased: false,
        releaseLogout: null, releaseMe: null, outcome: null,
      };
      window.fetch = (input, init = {}) => {
        const url = new URL(typeof input === "string" ? input : input.url,
          location.href);
        if (url.pathname === "/api/auth/me") {
          if (window.__explicitLag.meReleased) return Promise.resolve(refused());
          return new Promise((resolve) => {
            window.__explicitLag.releaseMe = () => {
              window.__explicitLag.meReleased = true;
              resolve(refused());
            };
          });
        }
        if (url.pathname === "/api/auth/logout" && init.method === "POST") {
          window.__explicitLag.logoutDispatched = true;
          return new Promise((resolve) => {
            window.__explicitLag.releaseLogout = () => resolve(refused());
          });
        }
        return originalFetch(input, init);
      };
      localStorage.setItem(key, token); // same-document: no storage event
      window.__explicitLag.outcome = signOut();
    }, { key: "hs_session_mutation_v1", token: explicitLagToken,
      sentinel: explicitLagSentinel });
    await page.waitForFunction(() => window.__explicitLag
      && window.__explicitLag.logoutDispatched
      && window.__explicitLag.releaseLogout
      && window.__explicitLag.releaseMe,
    null, { timeout: 10000 });
    const explicitLagAtEntry = await page.evaluate((sentinel) => ({
      current: currentUser,
      officials: officialsPool.length,
      privateNode: !!document.querySelector("[data-explicit-lag-private]"),
      hasSentinel: document.documentElement.textContent.includes(sentinel),
      loginVisible: !document.getElementById("login-screen").hidden,
      shellSignedOut: document.body.classList.contains("signed-out"),
      manageUsers: hasPerm("manage_users"),
      observed: observedSessionMutationToken,
      live: readSessionMutationToken(),
    }), explicitLagSentinel);
    if (explicitLagAtEntry.current !== null
        || explicitLagAtEntry.officials !== 0
        || explicitLagAtEntry.privateNode || explicitLagAtEntry.hasSentinel
        || !explicitLagAtEntry.loginVisible
        || !explicitLagAtEntry.shellSignedOut
        || explicitLagAtEntry.manageUsers
        || explicitLagAtEntry.observed !== explicitLagToken
        || explicitLagAtEntry.live !== explicitLagToken) {
      await page.evaluate(() => {
        window.__explicitLag.releaseLogout();
        window.__explicitLag.releaseMe();
      });
      fail(`explicit logout did not quarantine an unseen peer generation at `
        + `lock entry: ${JSON.stringify(explicitLagAtEntry)}`);
    }
    const explicitLagOutcome = await page.evaluate(async () => {
      window.__explicitLag.releaseLogout();
      window.__explicitLag.releaseMe();
      const outcome = await window.__explicitLag.outcome;
      window.fetch = window.__explicitLag.originalFetch;
      delete window.__explicitLag;
      return outcome;
    });
    if (explicitLagOutcome !== false) {
      fail(`synthetic refused logout unexpectedly succeeded after explicit `
        + `generation reconciliation: ${JSON.stringify(explicitLagOutcome)}`);
    }
    const restoreAfterExplicitLag = await page.evaluate(() => signIn("admin", "demo"));
    if (!restoreAfterExplicitLag) fail("Admin restore after explicit-generation lag failed");
    await page.evaluate(async () => {
      switchTab("dashboard");
      await render();
    });
    await reachDashboard(page);
    await waitForCardSettled(page);
    await tracker.waitForIdle();

    const unseenToken = `matrix-unseen-t3-${suffix}`;
    const unseenDispatch = await page.evaluate(async ({ key, token }) => {
      localStorage.setItem(key, token); // same-document: no storage event
      let dispatched = 0;
      const outcome = await runIdentityBoundSessionTransition(async () => {
        dispatched += 1;
        return { resultPromise: Promise.resolve({ ok: true }), boundary: null };
      }).then(() => ({ ok: true })).catch((error) => ({
        error: error && error.name,
      }));
      return { outcome, dispatched };
    }, { key: "hs_session_mutation_v1", token: unseenToken });
    if (unseenDispatch.dispatched !== 0
        || unseenDispatch.outcome.error !== "IdentitySupersededError") {
      fail(`an old-shell identity-bound action dispatched under an unseen peer `
        + `generation: ${JSON.stringify(unseenDispatch)}`);
    }
    await page.waitForFunction(() => currentUser && currentUser.username === "admin"
      && observedSessionMutationToken === readSessionMutationToken()
      && document.getElementById("login-screen").hidden,
    null, { timeout: 10000 });

    // A queued resume must not silently adopt a NEW nonempty generation just
    // because /auth/me reports the same username. Hold R1, queue another focus,
    // advance live storage to T4 without its event, then require the rerun to
    // take the full synchronous identity boundary before R2 is delivered.
    await page.waitForFunction(() => !resumeSessionValidationInFlight
      && !externalSessionReconcileInFlight,
    null, { timeout: 10000 });
    await tracker.waitForIdle();
    const queuedPeerToken = `matrix-queued-peer-t4-${suffix}`;
    const queuedPeerSentinel = `Queued peer private sentinel ${suffix}`;
    let queuedPeerMeRequests = 0;
    let releaseQueuedPeerFirst;
    let releaseQueuedPeerSecond;
    let markQueuedPeerFirstHeld;
    let markQueuedPeerSecondHeld;
    const queuedPeerFirstRelease = new Promise((resolve) => {
      releaseQueuedPeerFirst = resolve;
    });
    const queuedPeerSecondRelease = new Promise((resolve) => {
      releaseQueuedPeerSecond = resolve;
    });
    const queuedPeerFirstHeld = new Promise((resolve) => {
      markQueuedPeerFirstHeld = resolve;
    });
    const queuedPeerSecondHeld = new Promise((resolve) => {
      markQueuedPeerSecondHeld = resolve;
    });
    const holdQueuedPeerMe = async (route) => {
      queuedPeerMeRequests += 1;
      if (queuedPeerMeRequests > 2) return route.continue();
      const response = await route.fetch();
      if (queuedPeerMeRequests === 1) {
        markQueuedPeerFirstHeld();
        await queuedPeerFirstRelease;
      } else {
        markQueuedPeerSecondHeld();
        await queuedPeerSecondRelease;
      }
      await route.fulfill({ response });
    };
    await page.route("**/api/auth/me", holdQueuedPeerMe);
    await page.evaluate((sentinel) => {
      officialsPool = [{ id: "queued-peer-private", name: sentinel }];
      document.getElementById("content").innerHTML =
        `<section data-queued-peer-private>${sentinel}</section>`;
      window.dispatchEvent(new Event("focus"));
    }, queuedPeerSentinel);
    await queuedPeerFirstHeld;
    await page.evaluate(() => window.dispatchEvent(new Event("focus")));
    await page.evaluate(({ key, token }) => {
      localStorage.setItem(key, token); // event delivery is deliberately late
    }, { key: "hs_session_mutation_v1", token: queuedPeerToken });
    releaseQueuedPeerFirst();
    await queuedPeerSecondHeld;
    const queuedPeerBoundary = await page.evaluate((sentinel) => ({
      current: currentUser,
      officials: officialsPool.length,
      privateNode: !!document.querySelector("[data-queued-peer-private]"),
      hasSentinel: document.documentElement.textContent.includes(sentinel),
      loginVisible: !document.getElementById("login-screen").hidden,
      shellSignedOut: document.body.classList.contains("signed-out"),
      manageUsers: hasPerm("manage_users"),
      observed: observedSessionMutationToken,
      live: readSessionMutationToken(),
    }), queuedPeerSentinel);
    if (queuedPeerBoundary.current !== null
        || queuedPeerBoundary.officials !== 0
        || queuedPeerBoundary.privateNode || queuedPeerBoundary.hasSentinel
        || !queuedPeerBoundary.loginVisible
        || !queuedPeerBoundary.shellSignedOut
        || queuedPeerBoundary.manageUsers
        || queuedPeerBoundary.observed !== queuedPeerToken
        || queuedPeerBoundary.live !== queuedPeerToken) {
      releaseQueuedPeerSecond();
      fail(`a queued resume silently adopted a nonempty peer generation: `
        + `${JSON.stringify(queuedPeerBoundary)}`);
    }
    releaseQueuedPeerSecond();
    await page.waitForFunction(() => currentUser && currentUser.username === "admin"
      && observedSessionMutationToken === readSessionMutationToken()
      && !resumeSessionValidationInFlight
      && !externalSessionReconcileInFlight
      && document.getElementById("login-screen").hidden,
    null, { timeout: 10000 });
    await page.evaluate((token) => reconcileExternalSessionMutation(token),
      queuedPeerToken); // deliver the now-stale event; it must be a no-op
    const afterQueuedPeerEvent = await page.evaluate((sentinel) => ({
      current: currentUser && currentUser.username,
      hasSentinel: document.documentElement.textContent.includes(sentinel),
      privateNode: !!document.querySelector("[data-queued-peer-private]"),
      observed: observedSessionMutationToken,
      live: readSessionMutationToken(),
    }), queuedPeerSentinel);
    await page.unroute("**/api/auth/me", holdQueuedPeerMe);
    if (queuedPeerMeRequests !== 2
        || afterQueuedPeerEvent.current !== "admin"
        || afterQueuedPeerEvent.hasSentinel || afterQueuedPeerEvent.privateNode
        || afterQueuedPeerEvent.observed !== queuedPeerToken
        || afterQueuedPeerEvent.live !== queuedPeerToken) {
      fail(`nonempty peer-generation boundary did not converge exactly once: `
        + `${JSON.stringify({ queuedPeerMeRequests, afterQueuedPeerEvent })}`);
    }

    // Storage eviction is not a cookie verdict. A canonical same-identity
    // focus read must adopt the now-empty generation and restore operation
    // liveness; otherwise every later authenticated preflight fails forever.
    // Make the coalescing window exact: hold an older T3 resume response,
    // evict storage and focus again, then require the pending rerun to adopt
    // the empty generation after the obsolete response discards itself.
    await page.waitForFunction(() => !resumeSessionValidationInFlight,
    null, { timeout: 10000 });
    await tracker.waitForIdle();
    let storageEvictionMeRequests = 0;
    let releasePreEvictionMe;
    let markPreEvictionMeHeld;
    const preEvictionMeRelease = new Promise((resolve) => {
      releasePreEvictionMe = resolve;
    });
    const preEvictionMeHeld = new Promise((resolve) => {
      markPreEvictionMeHeld = resolve;
    });
    const holdPreEvictionMe = async (route) => {
      storageEvictionMeRequests += 1;
      if (storageEvictionMeRequests !== 1) return route.continue();
      const response = await route.fetch();
      markPreEvictionMeHeld();
      await preEvictionMeRelease;
      await route.fulfill({ response });
    };
    await page.route("**/api/auth/me", holdPreEvictionMe);
    await page.evaluate(() => window.dispatchEvent(new Event("focus")));
    await preEvictionMeHeld;
    await page.evaluate((key) => {
      localStorage.removeItem(key);
      window.dispatchEvent(new Event("focus"));
    }, "hs_session_mutation_v1");
    releasePreEvictionMe();
    await page.waitForFunction(() => !resumeSessionValidationInFlight
      && observedSessionMutationToken === ""
      && readSessionMutationToken() === "",
    null, { timeout: 10000 });
    await page.unroute("**/api/auth/me", holdPreEvictionMe);
    if (storageEvictionMeRequests !== 2) {
      fail(`storage eviction during an older resume did not schedule exactly `
        + `one canonical rerun: ${storageEvictionMeRequests}`);
    }
    const afterStorageEviction = await page.evaluate(async () => {
      const status = await getJSON("/api/status");
      return { status: status && status.store, current: currentUser && currentUser.username,
        admin: hasPerm("manage_users") };
    });
    if (!afterStorageEviction.status || afterStorageEviction.current !== "admin"
        || !afterStorageEviction.admin) {
      fail(`same-identity canonical recovery did not restore liveness after `
        + `storage eviction: ${JSON.stringify(afterStorageEviction)}`);
    }

    // Stronger overlap: external T5 reconciliation has already set a
    // speculative Admin model, but its guarded context read is held. Evict
    // storage and hold the resume read too; when the stale context response
    // tears the speculative model down, the queued canonical retry must be
    // allowed to start from quarantine, adopt empty, and rebuild fully.
    const rebuildEvictionToken = `matrix-rebuild-eviction-t5-${suffix}`;
    let rebuildEvictionContextRequests = 0;
    let releaseRebuildEvictionContext;
    let markRebuildEvictionContextHeld;
    const rebuildEvictionContextRelease = new Promise((resolve) => {
      releaseRebuildEvictionContext = resolve;
    });
    const rebuildEvictionContextHeld = new Promise((resolve) => {
      markRebuildEvictionContextHeld = resolve;
    });
    const holdRebuildEvictionContext = async (route) => {
      rebuildEvictionContextRequests += 1;
      if (rebuildEvictionContextRequests !== 1) return route.continue();
      const response = await route.fetch();
      markRebuildEvictionContextHeld();
      await rebuildEvictionContextRelease;
      await route.fulfill({ response });
    };
    await page.route("**/api/context/options", holdRebuildEvictionContext);
    await page.evaluate(({ key, token }) => {
      localStorage.setItem(key, token);
      reconcileExternalSessionMutation(token);
    }, { key: "hs_session_mutation_v1", token: rebuildEvictionToken });
    await rebuildEvictionContextHeld;
    let rebuildEvictionMeRequests = 0;
    let releaseRebuildEvictionMe;
    let markRebuildEvictionMeHeld;
    const rebuildEvictionMeRelease = new Promise((resolve) => {
      releaseRebuildEvictionMe = resolve;
    });
    const rebuildEvictionMeHeld = new Promise((resolve) => {
      markRebuildEvictionMeHeld = resolve;
    });
    const holdRebuildEvictionMe = async (route) => {
      rebuildEvictionMeRequests += 1;
      if (rebuildEvictionMeRequests !== 1) return route.continue();
      const response = await route.fetch();
      markRebuildEvictionMeHeld();
      await rebuildEvictionMeRelease;
      await route.fulfill({ response });
    };
    await page.route("**/api/auth/me", holdRebuildEvictionMe);
    await page.evaluate((key) => {
      localStorage.removeItem(key);
      window.dispatchEvent(new Event("focus"));
    }, "hs_session_mutation_v1");
    await rebuildEvictionMeHeld;
    releaseRebuildEvictionContext();
    await page.waitForFunction(() => currentUser === null
      && resumeSessionValidationPendingFromQuarantine,
    null, { timeout: 10000 });
    releaseRebuildEvictionMe();
    await page.waitForFunction(() => currentUser && currentUser.username === "admin"
      && observedSessionMutationToken === ""
      && readSessionMutationToken() === ""
      && !resumeSessionValidationInFlight
      && !externalSessionReconcileInFlight
      && document.getElementById("login-screen").hidden,
    null, { timeout: 10000 });
    await page.unroute("**/api/auth/me", holdRebuildEvictionMe);
    await page.unroute("**/api/context/options", holdRebuildEvictionContext);
    const afterRebuildEviction = await page.evaluate(async () => {
      const status = await getJSON("/api/status");
      return {
        status: status && status.store,
        current: currentUser && currentUser.username,
        admin: hasPerm("manage_users"),
        observed: observedSessionMutationToken,
        live: readSessionMutationToken(),
      };
    });
    if (rebuildEvictionMeRequests !== 2
        || rebuildEvictionContextRequests !== 2
        || !afterRebuildEviction.status
        || afterRebuildEviction.current !== "admin"
        || !afterRebuildEviction.admin
        || afterRebuildEviction.observed !== ""
        || afterRebuildEviction.live !== "") {
      fail(`storage eviction during speculative context rebuild did not `
        + `converge through one quarantine retry: ${JSON.stringify({
          rebuildEvictionMeRequests, rebuildEvictionContextRequests,
          afterRebuildEviction })}`);
    }
    await tracker.waitForIdle();

    // A suspended tab may receive storage notifications late and out of
    // order. Capture an Admin /auth/me response for token T1, advance the
    // persisted generation to T2 WITHOUT delivering T2's storage event to
    // this document, then release T1. The live persisted token—not merely
    // the last event this tab observed—must veto that stale Admin body.
    const delayedTokenSentinel = `Delayed storage Admin sentinel ${suffix}`;
    const delayedTokenOne = `matrix-delayed-t1-${suffix}`;
    const delayedTokenTwo = `matrix-delayed-t2-${suffix}`;
    await page.evaluate((sentinel) => {
      officialsPool = [{ id: "delayed-storage-private", name: sentinel }];
      document.getElementById("content").innerHTML =
        `<section data-delayed-storage-private>${sentinel}</section>`;
    }, delayedTokenSentinel);
    let releaseDelayedTokenMe;
    let markDelayedTokenMeHeld;
    let markDelayedTokenMeDelivered;
    const delayedTokenMeRelease = new Promise((resolve) => {
      releaseDelayedTokenMe = resolve;
    });
    const delayedTokenMeHeld = new Promise((resolve) => {
      markDelayedTokenMeHeld = resolve;
    });
    const delayedTokenMeDelivered = new Promise((resolve) => {
      markDelayedTokenMeDelivered = resolve;
    });
    let delayedTokenMeRequests = 0;
    const holdDelayedTokenMe = async (route) => {
      delayedTokenMeRequests += 1;
      if (delayedTokenMeRequests !== 1) return route.continue();
      const response = await route.fetch();
      markDelayedTokenMeHeld();
      await delayedTokenMeRelease;
      await route.fulfill({ response });
      markDelayedTokenMeDelivered();
    };
    await page.route("**/api/auth/me", holdDelayedTokenMe);
    await page.evaluate(({ key, token }) => {
      localStorage.setItem(key, token);
      reconcileExternalSessionMutation(token);
    }, { key: "hs_session_mutation_v1", token: delayedTokenOne });
    await delayedTokenMeHeld;
    await page.evaluate(({ key, token }) => {
      // Same-document writes deliberately emit no storage event. This is the
      // exact window where a delayed T1 response used to look current to the
      // per-document counters even though origin state had advanced to T2.
      localStorage.setItem(key, token);
    }, { key: "hs_session_mutation_v1", token: delayedTokenTwo });
    releaseDelayedTokenMe();
    await delayedTokenMeDelivered;
    await page.waitForTimeout(50);
    const delayedTokenBoundary = await page.evaluate((sentinel) => ({
      current: currentUser,
      officials: officialsPool.length,
      hasSentinel: document.documentElement.textContent.includes(sentinel),
      privateNode: !!document.querySelector("[data-delayed-storage-private]"),
      loginVisible: !document.getElementById("login-screen").hidden,
      shellSignedOut: document.body.classList.contains("signed-out"),
      manageUsers: hasPerm("manage_users"),
    }), delayedTokenSentinel);
    if (delayedTokenMeRequests !== 1
        || delayedTokenBoundary.current !== null
        || delayedTokenBoundary.officials !== 0
        || delayedTokenBoundary.hasSentinel
        || delayedTokenBoundary.privateNode
        || !delayedTokenBoundary.loginVisible
        || !delayedTokenBoundary.shellSignedOut
        || delayedTokenBoundary.manageUsers) {
      fail(`a delayed T1 storage response crossed the live T2 generation: `
        + `${JSON.stringify({ delayedTokenMeRequests, delayedTokenBoundary })}`);
    }
    await page.unroute("**/api/auth/me", holdDelayedTokenMe);
    await page.evaluate((token) => reconcileExternalSessionMutation(token), delayedTokenTwo);
    await page.waitForFunction(() => currentUser && currentUser.username === "admin"
      && document.getElementById("login-screen").hidden
      && !externalSessionReconcileInFlight,
    null, { timeout: 10000 });
    await page.waitForFunction(() => !resumeSessionValidationInFlight,
      null, { timeout: 10000 });
    await page.evaluate(async () => {
      switchTab("dashboard");
      // switchTab intentionally fire-and-forgets render(). Await a second,
      // newer pass so the first is superseded and every sequential
      // authenticated read settles before the cookie is removed below.
      await render();
    });
    await reachDashboard(page);
    await waitForCardSettled(page);
    // Playwright's load-state "networkidle" is historical once the document
    // has loaded; it can return while a fire-and-forget render request is
    // still draining. Wait on the journey's live request ledger so the cookie
    // removal below cannot misclassify an older authenticated request's 401 as
    // a focus-revalidation failure.
    await tracker.waitForIdle();

    // No storage token is available for a remote revoke/natural expiry. A
    // focus/visibility resume must still re-read canonical identity and purge
    // the stale private tree when that session is gone.
    const resumeSentinel = `Remote-expiry private sentinel ${suffix}`;
    await page.evaluate((sentinel) => {
      // This leg is about the resume read, not an older Dashboard pass. Claim
      // a newer render generation before clearing the cookie so any pass that
      // wakes after the request ledger's quiet window exits at its next guard
      // instead of issuing a setup/notification read under the test's
      // deliberately expired cookie.
      renderPass += 1;
      document.getElementById("content").innerHTML =
        `<section data-resume-private>${sentinel}</section>`;
      officialsPool = [{ id: "resume-private", name: sentinel }];
    }, resumeSentinel);
    // Expire the cookie outside app.js: no sessionCookiePost(), no storage
    // token, exactly the shape of browser expiry or revocation elsewhere.
    await context.clearCookies();
    const rawLoggedOutMe = await context.request.get(`${base}/api/auth/me`);
    const rawLoggedOutBody = await rawLoggedOutMe.json();
    if (!rawLoggedOutMe.ok() || rawLoggedOutBody.user != null) {
      fail(`focus-revalidation setup left a live session: `
        + `${rawLoggedOutMe.status()} ${JSON.stringify(rawLoggedOutBody)}`);
    }
    await page.bringToFront();
    await page.evaluate(() => window.dispatchEvent(new Event("focus")));
    await page.waitForFunction(() => currentUser === null
      && !document.getElementById("login-screen").hidden,
    null, { timeout: 10000 });
    const resumedAfterRemoteLogout = await page.evaluate((sentinel) => ({
      current: currentUser,
      officials: officialsPool.length,
      hasSentinel: document.documentElement.textContent.includes(sentinel),
      privateNode: !!document.querySelector("[data-resume-private]"),
      loginVisible: !document.getElementById("login-screen").hidden,
      shellSignedOut: document.body.classList.contains("signed-out"),
      manageUsers: hasPerm("manage_users"),
    }), resumeSentinel);
    if (resumedAfterRemoteLogout.current !== null
        || resumedAfterRemoteLogout.officials !== 0
        || resumedAfterRemoteLogout.hasSentinel
        || resumedAfterRemoteLogout.privateNode
        || !resumedAfterRemoteLogout.loginVisible
        || !resumedAfterRemoteLogout.shellSignedOut
        || resumedAfterRemoteLogout.manageUsers) {
      fail(`focus/visibility fallback retained private Admin state after a `
        + `no-token session loss: ${JSON.stringify(resumedAfterRemoteLogout)}`);
    }
    if (tracker.unexpected.length) {
      fail(`resume produced unexpected HTTP failure(s): ${JSON.stringify({
        unexpected: tracker.unexpected })}`);
    }
    const adminAfterCrossTab = await page.evaluate(() => signIn("admin", "demo"));
    if (!adminAfterCrossTab) fail("Admin restore after cross-tab tests failed");
    await page.evaluate(() => switchTab("dashboard"));
    await reachDashboard(page);

    // Bootstrap's /api/auth/me read starts before the static shell is fully
    // reconciled. In a fresh cookie jar, capture a real Admin me response,
    // complete a real logout while it is held, then deliver the stale body.
    // Bootstrap must refuse it even though currentUser was null on both sides
    // of the logout (the identity epoch alone cannot see that transition).
    const bootstrapContext = await browser.newContext({
      viewport: { width: viewport.width, height: viewport.height },
    });
    const bootstrapLoginResponse = await bootstrapContext.request.post(
      `${base}/api/auth/login`, { data: { username: "admin", password: "demo" } });
    const bootstrapLoginBody = await bootstrapLoginResponse.json();
    if (!bootstrapLoginResponse.ok() || !bootstrapLoginBody.user) {
      await bootstrapContext.close();
      fail(`bootstrap-race Admin setup failed: ${JSON.stringify(bootstrapLoginBody)}`);
    }
    const bootstrapPage = await bootstrapContext.newPage();
    let releaseBootstrapMe;
    let markBootstrapMeHeld;
    let markBootstrapMeDelivered;
    const bootstrapMeRelease = new Promise((resolve) => {
      releaseBootstrapMe = resolve;
    });
    const bootstrapMeHeld = new Promise((resolve) => {
      markBootstrapMeHeld = resolve;
    });
    const bootstrapMeDelivered = new Promise((resolve) => {
      markBootstrapMeDelivered = resolve;
    });
    let bootstrapMeRequests = 0;
    const holdBootstrapMe = async (route) => {
      bootstrapMeRequests += 1;
      if (bootstrapMeRequests !== 1) return route.continue();
      const response = await route.fetch();
      markBootstrapMeHeld();
      await bootstrapMeRelease;
      await route.fulfill({ response });
      markBootstrapMeDelivered();
    };
    await bootstrapPage.route("**/api/auth/me", holdBootstrapMe);
    await bootstrapPage.goto(base, { waitUntil: "domcontentloaded" });
    await bootstrapMeHeld;
    await bootstrapPage.waitForFunction(() =>
      typeof document.getElementById("signout-btn").onclick === "function");
    const bootstrapLogoutOutcome = await bootstrapPage.evaluate(() =>
      document.getElementById("signout-btn").onclick());
    const meBeforeStaleBootstrap = await bootstrapContext.request.get(`${base}/api/auth/me`);
    const meBeforeStaleBootstrapBody = await meBeforeStaleBootstrap.json();
    releaseBootstrapMe();
    await bootstrapMeDelivered;
    await bootstrapPage.waitForTimeout(150);
    const staleBootstrapBoundary = await bootstrapPage.evaluate(() => ({
      current: currentUser,
      loginVisible: !document.getElementById("login-screen").hidden,
      shellSignedOut: document.body.classList.contains("signed-out"),
      sidebar: (document.getElementById("user-name").textContent || "").trim(),
      manageUsers: hasPerm("manage_users"),
      usersTabHidden: document.querySelector('.tab[data-tab="users"]').style.display === "none",
    }));
    const meAfterStaleBootstrap = await bootstrapContext.request.get(`${base}/api/auth/me`);
    const meAfterStaleBootstrapBody = await meAfterStaleBootstrap.json();
    await bootstrapPage.unroute("**/api/auth/me", holdBootstrapMe);
    await bootstrapContext.close();
    if (bootstrapLogoutOutcome !== true || bootstrapMeRequests !== 1
        || meBeforeStaleBootstrapBody.user != null
        || meAfterStaleBootstrapBody.user != null
        || staleBootstrapBoundary.current !== null
        || !staleBootstrapBoundary.loginVisible
        || !staleBootstrapBoundary.shellSignedOut
        || staleBootstrapBoundary.sidebar !== "Signed out"
        || staleBootstrapBoundary.manageUsers
        || !staleBootstrapBoundary.usersTabHidden) {
      fail(`stale bootstrap /api/auth/me restored Admin after a completed `
        + `logout: ${JSON.stringify({ bootstrapLogoutOutcome,
          meBeforeStaleBootstrapBody, meAfterStaleBootstrapBody,
          staleBootstrapBoundary, bootstrapMeRequests })}`);
    }

    // A current canonical Viewer with malformed or incomplete role metadata
    // must take the same wall as the stale-bootstrap races below. First prove
    // a 200/non-JSON catalog independently recovers; then prove a valid but
    // persistently missing Viewer row never fails open or paints Dashboard.
    {
      const malformedRolesContext = await browser.newContext({
        viewport: { width: viewport.width, height: viewport.height },
      });
      const seededViewer = await malformedRolesContext.request.post(
        `${base}/api/auth/login`, {
          data: { username: "viewer", password: "demo" },
        });
      if (seededViewer.status() !== 200) {
        fail(`could not seed Viewer for malformed role bootstrap: `
          + `${seededViewer.status()}`);
      }
      const malformedRolesPage = await malformedRolesContext.newPage();
      let releaseRecoveredRoles;
      let markRecoveredRolesHeld;
      const recoveredRolesRelease = new Promise((resolve) => {
        releaseRecoveredRoles = resolve;
      });
      const recoveredRolesHeld = new Promise((resolve) => {
        markRecoveredRolesHeld = resolve;
      });
      let malformedRoleRequests = 0;
      let malformedOverviewRequests = 0;
      const malformedThenHoldRecovery = async (route) => {
        malformedRoleRequests += 1;
        if (malformedRoleRequests === 1) {
          return route.fulfill({ status: 200, contentType: "text/html",
            body: "not json" });
        }
        if (malformedRoleRequests === 2) {
          markRecoveredRolesHeld();
          await recoveredRolesRelease;
        }
        await route.continue();
      };
      const countMalformedOverview = async (route) => {
        malformedOverviewRequests += 1;
        await route.continue();
      };
      await malformedRolesPage.route("**/api/auth/roles", malformedThenHoldRecovery);
      await malformedRolesPage.route("**/api/demo/overview", countMalformedOverview);
      await malformedRolesPage.goto(base, { waitUntil: "domcontentloaded" });
      await recoveredRolesHeld;
      const malformedBeforeRecovery = await malformedRolesPage.evaluate(() => ({
        user: currentUser && currentUser.username,
        roles: roleCatalog.length,
        waitingForRoles: sessionAwaitingRoleMetadata,
        loginVisible: !document.getElementById("login-screen").hidden,
        shellSignedOut: document.body.classList.contains("signed-out"),
        dashboardPainted: !!document.querySelector(".dash-grid"),
        manageUsers: hasPerm("manage_users"),
      }));
      const malformedOverviewBeforeRecovery = malformedOverviewRequests;
      releaseRecoveredRoles();
      await malformedRolesPage.waitForFunction(() =>
        currentUser && currentUser.username === "viewer"
          && roleCatalog.length > 0 && view === "standings"
          && !sessionAwaitingRoleMetadata
          && document.getElementById("login-screen").hidden
          && !!document.querySelector(".st-toolbar"), null, { timeout: 10000 });
      const malformedAfterRecovery = await malformedRolesPage.evaluate(() => ({
        user: currentUser && currentUser.username,
        view,
        waitingForRoles: sessionAwaitingRoleMetadata,
        loginVisible: !document.getElementById("login-screen").hidden,
        dashboardPainted: !!document.querySelector(".dash-grid"),
        manageUsers: hasPerm("manage_users"),
      }));
      await malformedRolesPage.unroute("**/api/auth/roles", malformedThenHoldRecovery);
      await malformedRolesPage.unroute("**/api/demo/overview", countMalformedOverview);
      await malformedRolesContext.close();
      if (malformedRoleRequests !== 2
          || malformedBeforeRecovery.user !== "viewer"
          || malformedBeforeRecovery.roles !== 0
          || !malformedBeforeRecovery.waitingForRoles
          || !malformedBeforeRecovery.loginVisible
          || !malformedBeforeRecovery.shellSignedOut
          || malformedBeforeRecovery.dashboardPainted
          || malformedBeforeRecovery.manageUsers
          || malformedOverviewBeforeRecovery !== 0
          || malformedAfterRecovery.user !== "viewer"
          || malformedAfterRecovery.view !== "standings"
          || malformedAfterRecovery.waitingForRoles
          || malformedAfterRecovery.loginVisible
          || malformedAfterRecovery.dashboardPainted
          || malformedAfterRecovery.manageUsers) {
        fail(`malformed current-bootstrap roles escaped Viewer quarantine: `
          + `${JSON.stringify({ malformedRoleRequests,
            malformedOverviewBeforeRecovery, malformedBeforeRecovery,
            malformedAfterRecovery })}`);
      }
    }

    {
      const missingRoleContext = await browser.newContext({
        viewport: { width: viewport.width, height: viewport.height },
      });
      const seededViewer = await missingRoleContext.request.post(
        `${base}/api/auth/login`, {
          data: { username: "viewer", password: "demo" },
        });
      if (seededViewer.status() !== 200) {
        fail(`could not seed Viewer for missing role bootstrap: `
          + `${seededViewer.status()}`);
      }
      const missingRolePage = await missingRoleContext.newPage();
      let missingRoleRequests = 0;
      let missingRoleOverviewRequests = 0;
      const omitEveryRole = async (route) => {
        missingRoleRequests += 1;
        await route.fulfill({ status: 200, contentType: "application/json",
          body: JSON.stringify({ roles: [] }) });
      };
      const countMissingRoleOverview = async (route) => {
        missingRoleOverviewRequests += 1;
        await route.continue();
      };
      await missingRolePage.route("**/api/auth/roles", omitEveryRole);
      await missingRolePage.route("**/api/demo/overview", countMissingRoleOverview);
      await missingRolePage.goto(base, { waitUntil: "domcontentloaded" });
      await missingRolePage.waitForFunction(() =>
        currentUser && currentUser.username === "viewer"
          && sessionAwaitingRoleMetadata
          && !document.getElementById("login-screen").hidden
          && (document.getElementById("login-error").textContent || "")
            .includes("role is unavailable"), null, { timeout: 10000 });
      const missingRoleBoundary = await missingRolePage.evaluate(() => ({
        user: currentUser && currentUser.username,
        roles: roleCatalog.length,
        waitingForRoles: sessionAwaitingRoleMetadata,
        loginVisible: !document.getElementById("login-screen").hidden,
        shellSignedOut: document.body.classList.contains("signed-out"),
        dashboardPainted: !!document.querySelector(".dash-grid"),
        manageUsers: hasPerm("manage_users"),
      }));
      await missingRolePage.unroute("**/api/auth/roles", omitEveryRole);
      await missingRolePage.unroute("**/api/demo/overview", countMissingRoleOverview);
      await missingRoleContext.close();
      if (missingRoleRequests !== 2
          || missingRoleOverviewRequests !== 0
          || missingRoleBoundary.user !== "viewer"
          || missingRoleBoundary.roles !== 0
          || !missingRoleBoundary.waitingForRoles
          || !missingRoleBoundary.loginVisible
          || !missingRoleBoundary.shellSignedOut
          || missingRoleBoundary.dashboardPainted
          || missingRoleBoundary.manageUsers) {
        fail(`missing current Viewer role failed open: ${JSON.stringify({
          missingRoleRequests, missingRoleOverviewRequests,
          missingRoleBoundary })}`);
      }
    }

    // The sign-in form is usable while bootstrap's public metadata reads are
    // still in flight.  The winning identity must remain behind the login wall
    // until its role row exists: otherwise a Viewer starts on the default
    // Dashboard, renders the operator overview, and is redirected only after
    // that private paint.  Exercise the held request succeeding, returning a
    // 502, and aborting at the network layer. A recovery read must follow a
    // failed bootstrap, while a refused action must stay refused instead of
    // falling into demo auto-login.
    for (const bootstrapAuthCase of [
      { label: "admin-accepted-502", username: "admin", password: "demo",
        accepted: true, failure: "response", finalView: "dashboard" },
      { label: "admin-accepted-network", username: "admin", password: "demo",
        accepted: true, failure: "network", finalView: "dashboard" },
      { label: "viewer-accepted-200", username: "viewer", password: "demo",
        accepted: true, failure: "success", finalView: "standings" },
      { label: "viewer-first-200-recovery-502", username: "viewer",
        password: "demo", accepted: true, failure: "success",
        recoveryFailure: "response", finalView: "standings" },
      { label: "viewer-first-200-recovery-network", username: "viewer",
        password: "demo", accepted: true, failure: "success",
        recoveryFailure: "network", finalView: "standings" },
      { label: "viewer-first-200-recovery-malformed", username: "viewer",
        password: "demo", accepted: true, failure: "success",
        recoveryFailure: "malformed", finalView: "standings" },
      { label: "viewer-recovery-before-stale-malformed", username: "viewer",
        password: "demo", accepted: true, failure: "malformed",
        releaseRecoveryFirst: true, finalView: "standings" },
      { label: "viewer-stale-valid-rescues-hung-recovery", username: "viewer",
        password: "demo", accepted: true, failure: "success",
        recoveryFailure: "network", releaseFirstBeforeRecovery: true,
        finalView: "standings" },
      { label: "viewer-published-rebuild-canonical-refresh", username: "viewer",
        password: "demo", accepted: true, failure: "success",
        recoveryFailure: "network", releaseFirstBeforeRecovery: true,
        sameSignatureDuringPublishedRebuild: true,
        finalView: "standings" },
      { label: "viewer-accepted-502", username: "viewer", password: "demo",
        accepted: true, failure: "response", finalView: "standings" },
      { label: "viewer-accepted-network", username: "viewer", password: "demo",
        accepted: true, failure: "network", finalView: "standings" },
      { label: "admin-refused-502", username: "admin",
        password: "definitely-wrong-password", accepted: false,
        failure: "response", finalView: null },
    ]) {
      const recoveryContext = await browser.newContext({
        viewport: { width: viewport.width, height: viewport.height },
      });
      const recoveryPage = await recoveryContext.newPage();
      let releaseFirstRoles;
      let markFirstRolesHeld;
      let releaseRecoveryRoles;
      let markRecoveryRolesHeld;
      const firstRolesRelease = new Promise((resolve) => {
        releaseFirstRoles = resolve;
      });
      const firstRolesHeld = new Promise((resolve) => {
        markFirstRolesHeld = resolve;
      });
      const recoveryRolesRelease = new Promise((resolve) => {
        releaseRecoveryRoles = resolve;
      });
      const recoveryRolesHeld = new Promise((resolve) => {
        markRecoveryRolesHeld = resolve;
      });
      let rolesRequests = 0;
      let loginRequests = 0;
      let overviewRequests = 0;
      let releaseFirstPublishedContext;
      let markFirstPublishedContextHeld;
      let releaseSecondPublishedContext;
      let markSecondPublishedContextHeld;
      const firstPublishedContextRelease = new Promise((resolve) => {
        releaseFirstPublishedContext = resolve;
      });
      const firstPublishedContextHeld = new Promise((resolve) => {
        markFirstPublishedContextHeld = resolve;
      });
      const secondPublishedContextRelease = new Promise((resolve) => {
        releaseSecondPublishedContext = resolve;
      });
      const secondPublishedContextHeld = new Promise((resolve) => {
        markSecondPublishedContextHeld = resolve;
      });
      let publishedContextReads = 0;
      const failFirstRoles = async (route) => {
        rolesRequests += 1;
        if (rolesRequests === 2 && bootstrapAuthCase.accepted) {
          markRecoveryRolesHeld();
          await recoveryRolesRelease;
          if (bootstrapAuthCase.recoveryFailure === "network") {
            return route.abort("failed");
          }
          if (bootstrapAuthCase.recoveryFailure === "response") {
            return route.fulfill({ status: 502, contentType: "text/html",
              body: "Bad Gateway" });
          }
          if (bootstrapAuthCase.recoveryFailure === "malformed") {
            return route.fulfill({ status: 200, contentType: "text/html",
              body: "not json" });
          }
          return route.continue();
        }
        if (rolesRequests !== 1) return route.continue();
        markFirstRolesHeld();
        await firstRolesRelease;
        if (bootstrapAuthCase.failure === "success") {
          return route.continue();
        }
        if (bootstrapAuthCase.failure === "network") {
          return route.abort("failed");
        }
        if (bootstrapAuthCase.failure === "malformed") {
          return route.fulfill({ status: 200, contentType: "text/html",
            body: "not json" });
        }
        await route.fulfill({ status: 502, contentType: "text/html",
          body: "Bad Gateway" });
      };
      const countRecoveryLogin = async (route) => {
        loginRequests += 1;
        await route.continue();
      };
      const countOverview = async (route) => {
        overviewRequests += 1;
        await route.continue();
      };
      const holdPublishedRebuildContexts = async (route) => {
        publishedContextReads += 1;
        const response = await route.fetch();
        if (publishedContextReads === 1) {
          markFirstPublishedContextHeld();
          await firstPublishedContextRelease;
          try { await route.fulfill({ response }); }
          catch (_) { /* newer canonical verdict cancelled this read */ }
          return;
        }
        if (publishedContextReads !== 2) {
          fail(`published-metadata rebuild issued unexpected context read `
            + `${publishedContextReads}`);
        }
        markSecondPublishedContextHeld();
        await secondPublishedContextRelease;
        await route.fulfill({ response });
      };
      await recoveryPage.route("**/api/auth/roles", failFirstRoles);
      await recoveryPage.route("**/api/auth/login", countRecoveryLogin);
      await recoveryPage.route("**/api/demo/overview", countOverview);
      if (bootstrapAuthCase.sameSignatureDuringPublishedRebuild) {
        await recoveryPage.route(
          "**/api/context/options", holdPublishedRebuildContexts);
      }
      await recoveryPage.goto(base, { waitUntil: "domcontentloaded" });
      await firstRolesHeld;
      const authOutcome = await bounded(recoveryPage.evaluate(async ({ username, password }) =>
        signIn(username, password), {
        username: bootstrapAuthCase.username,
        password: bootstrapAuthCase.password,
      }),
      `bootstrap ${bootstrapAuthCase.label} explicit sign-in`);
      if (bootstrapAuthCase.accepted) await recoveryRolesHeld;
      const beforeMetadataRecovery = await recoveryPage.evaluate(() => ({
        user: currentUser && currentUser.username,
        roles: roleCatalog.length,
        waitingForRoles: sessionAwaitingRoleMetadata,
        loginVisible: !document.getElementById("login-screen").hidden,
        shellSignedOut: document.body.classList.contains("signed-out"),
        loginError: (document.getElementById("login-error").textContent || "").trim(),
        dashboardPainted: !!document.querySelector(".dash-grid"),
        operatorActivityPainted: (document.getElementById("content").textContent || "")
          .includes("Operator setup ("),
      }));
      const overviewRequestsBeforeRecovery = overviewRequests;
      let recoveredWhileDedicatedHeld = null;
      if (bootstrapAuthCase.releaseRecoveryFirst) {
        releaseRecoveryRoles();
        await recoveryPage.waitForFunction(({ username, finalView }) =>
          currentUser && currentUser.username === username
            && roleCatalog.length > 0 && view === finalView
            && !sessionAwaitingRoleMetadata
            && document.getElementById("login-screen").hidden,
        { username: bootstrapAuthCase.username,
          finalView: bootstrapAuthCase.finalView }, { timeout: 10000 });
        releaseFirstRoles();
        await recoveryPage.waitForTimeout(100);
      } else if (bootstrapAuthCase.releaseFirstBeforeRecovery) {
        // The independent recovery is deliberately held forever while the
        // older startup catalog succeeds. Valid public metadata must restore
        // the exact quarantined identity immediately, without awaiting that
        // hung lane. Then fail the old lane and prove it cannot re-wall or
        // otherwise regress the already-adopted session.
        releaseFirstRoles();
        if (bootstrapAuthCase.sameSignatureDuringPublishedRebuild) {
          await bounded(firstPublishedContextHeld,
            "published-metadata Viewer context");
          const publishedTailTracked = await recoveryPage.evaluate(() =>
            !!canonicalSessionRebuildInFlight);
          await recoveryPage.evaluate(() =>
            window.dispatchEvent(new Event("focus")));
          await bounded(secondPublishedContextHeld,
            "fresh same-signature published-metadata Viewer context");
          if (!publishedTailTracked || publishedContextReads !== 2) {
            releaseFirstPublishedContext();
            releaseSecondPublishedContext();
            fail(`published-metadata adoption was not a cancellable canonical `
              + `tail: ${JSON.stringify({ publishedTailTracked,
                publishedContextReads })}`);
          }
          releaseFirstPublishedContext();
          releaseSecondPublishedContext();
        }
        await recoveryPage.waitForFunction(({ username, finalView }) =>
          currentUser && currentUser.username === username
            && roleCatalog.length > 0 && view === finalView
            && !sessionAwaitingRoleMetadata
            && document.getElementById("login-screen").hidden,
        { username: bootstrapAuthCase.username,
          finalView: bootstrapAuthCase.finalView }, { timeout: 10000 });
        recoveredWhileDedicatedHeld = await recoveryPage.evaluate(() => ({
          user: currentUser && currentUser.username,
          view,
          roles: roleCatalog.length,
          envMode: envStatus && envStatus.app_mode,
          loginVisible: !document.getElementById("login-screen").hidden,
          dashboardPainted: !!document.querySelector(".dash-grid"),
        }));
        releaseRecoveryRoles();
        await recoveryPage.waitForFunction(() =>
          roleMetadataRecoveryFlight === null, null, { timeout: 10000 });
      } else {
        releaseFirstRoles();
        if (bootstrapAuthCase.accepted) releaseRecoveryRoles();
      }
      await recoveryPage.waitForFunction(({ accepted, username, finalView }) =>
        roleCatalog.length > 0
          && (accepted
            ? currentUser && currentUser.username === username
              && view === finalView
              && !sessionAwaitingRoleMetadata
              && document.getElementById("login-screen").hidden
              && (finalView === "dashboard"
                ? !!document.querySelector(".dash-grid")
                : !!document.querySelector(".st-toolbar"))
            : currentUser === null
              && !document.getElementById("login-screen").hidden),
      { accepted: bootstrapAuthCase.accepted,
        username: bootstrapAuthCase.username,
        finalView: bootstrapAuthCase.finalView }, { timeout: 10000 });
      const recoveredBootstrap = await recoveryPage.evaluate(() => ({
        user: currentUser && currentUser.username,
        roles: roleCatalog.length,
        view,
        dashboardActive: Array.from(
          document.querySelectorAll('.tab[data-tab="dashboard"]'))
          .some((tab) => tab.classList.contains("active")),
        standingsActive: Array.from(
          document.querySelectorAll('.tab[data-tab="standings"]'))
          .some((tab) => tab.classList.contains("active")),
        manageSchedule: hasPerm("manage_schedule"),
        manageUsers: hasPerm("manage_users"),
        waitingForRoles: sessionAwaitingRoleMetadata,
        envMode: envStatus && envStatus.app_mode,
        loginVisible: !document.getElementById("login-screen").hidden,
        loginError: (document.getElementById("login-error").textContent || "").trim(),
        dashboardPainted: !!document.querySelector(".dash-grid"),
      }));
      const canonicalAfterRecovery = await recoveryContext.request.get(
        `${base}/api/auth/me`);
      const canonicalAfterRecoveryBody = await canonicalAfterRecovery.json();
      await recoveryPage.unroute("**/api/auth/roles", failFirstRoles);
      await recoveryPage.unroute("**/api/auth/login", countRecoveryLogin);
      await recoveryPage.unroute("**/api/demo/overview", countOverview);
      if (bootstrapAuthCase.sameSignatureDuringPublishedRebuild) {
        await recoveryPage.unroute(
          "**/api/context/options", holdPublishedRebuildContexts);
      }
      await recoveryContext.close();
      const expectedRolesRequests = 2;
      const acceptedMetadataWallFailed = bootstrapAuthCase.accepted
        && (beforeMetadataRecovery.user !== bootstrapAuthCase.username
          || beforeMetadataRecovery.roles !== 0
          || !beforeMetadataRecovery.waitingForRoles
          || !beforeMetadataRecovery.loginVisible
          || !beforeMetadataRecovery.shellSignedOut
          || beforeMetadataRecovery.dashboardPainted
          || beforeMetadataRecovery.operatorActivityPainted
          || overviewRequestsBeforeRecovery !== 0);
      const acceptedFinalFailed = bootstrapAuthCase.accepted
        && (recoveredBootstrap.user !== bootstrapAuthCase.username
          || recoveredBootstrap.view !== bootstrapAuthCase.finalView
          || recoveredBootstrap.waitingForRoles
          || !recoveredBootstrap.envMode
          || recoveredBootstrap.loginVisible
          || canonicalAfterRecoveryBody.user?.username !== bootstrapAuthCase.username
          || (bootstrapAuthCase.finalView === "dashboard"
            ? !recoveredBootstrap.dashboardActive
              || !recoveredBootstrap.manageSchedule
              || !recoveredBootstrap.manageUsers
              || !recoveredBootstrap.dashboardPainted
            : !recoveredBootstrap.standingsActive
              || recoveredBootstrap.manageSchedule
              || recoveredBootstrap.manageUsers
              || recoveredBootstrap.dashboardPainted));
      const heldRecoveryRescueFailed =
        bootstrapAuthCase.releaseFirstBeforeRecovery
        && (!recoveredWhileDedicatedHeld
          || recoveredWhileDedicatedHeld.user !== bootstrapAuthCase.username
          || recoveredWhileDedicatedHeld.view !== bootstrapAuthCase.finalView
          || recoveredWhileDedicatedHeld.roles === 0
          || !recoveredWhileDedicatedHeld.envMode
          || recoveredWhileDedicatedHeld.loginVisible
          || recoveredWhileDedicatedHeld.dashboardPainted);
      if (rolesRequests !== expectedRolesRequests || loginRequests !== 1
          || authOutcome !== bootstrapAuthCase.accepted
          || recoveredBootstrap.roles === 0
          || acceptedMetadataWallFailed
          || acceptedFinalFailed
          || heldRecoveryRescueFailed
          || (bootstrapAuthCase.sameSignatureDuringPublishedRebuild
            && publishedContextReads !== 2)
          || (!bootstrapAuthCase.accepted
            && (recoveredBootstrap.user !== null
              || !recoveredBootstrap.loginVisible
              || !beforeMetadataRecovery.loginError
              || recoveredBootstrap.loginError !== beforeMetadataRecovery.loginError
              || canonicalAfterRecoveryBody.user != null))) {
        fail(`stale bootstrap metadata failure did not recover after the `
          + `${bootstrapAuthCase.label} explicit login: ${JSON.stringify({
            authOutcome, rolesRequests, expectedRolesRequests, loginRequests,
            overviewRequestsBeforeRecovery, publishedContextReads,
            beforeMetadataRecovery,
            recoveredWhileDedicatedHeld, recoveredBootstrap,
            canonicalAfterRecoveryBody })}`);
      }
    }

    // Canonical recovery after a refused mutation must obey the same metadata
    // wall as a successful login. Start with a real Viewer cookie, hold both
    // the startup catalog and the independent recovery catalog, then mistype
    // another account's password. The still-valid Viewer session may be
    // rediscovered, but it may not render the default Dashboard before its
    // role row arrives; afterward it must land on Standings and retain the
    // refusal as an error toast.
    {
      const viewerRecoveryContext = await browser.newContext({
        viewport: { width: viewport.width, height: viewport.height },
      });
      const viewerLogin = await viewerRecoveryContext.request.post(
        `${base}/api/auth/login`, {
          data: { username: "viewer", password: "demo" },
        });
      if (viewerLogin.status() !== 200) {
        fail(`could not seed Viewer cookie for metadata recovery: `
          + `${viewerLogin.status()}`);
      }
      const viewerRecoveryPage = await viewerRecoveryContext.newPage();
      let releaseViewerRoles;
      let markViewerStartupRolesHeld;
      let markViewerRecoveryRolesHeld;
      const viewerRolesRelease = new Promise((resolve) => {
        releaseViewerRoles = resolve;
      });
      const viewerStartupRolesHeld = new Promise((resolve) => {
        markViewerStartupRolesHeld = resolve;
      });
      const viewerRecoveryRolesHeld = new Promise((resolve) => {
        markViewerRecoveryRolesHeld = resolve;
      });
      let viewerRolesRequests = 0;
      let viewerOverviewRequests = 0;
      const holdViewerRoles = async (route) => {
        viewerRolesRequests += 1;
        if (viewerRolesRequests === 1) markViewerStartupRolesHeld();
        if (viewerRolesRequests === 2) markViewerRecoveryRolesHeld();
        await viewerRolesRelease;
        await route.continue();
      };
      const countViewerOverview = async (route) => {
        viewerOverviewRequests += 1;
        await route.continue();
      };
      await viewerRecoveryPage.route("**/api/auth/roles", holdViewerRoles);
      await viewerRecoveryPage.route("**/api/demo/overview", countViewerOverview);
      await viewerRecoveryPage.goto(base, { waitUntil: "domcontentloaded" });
      await viewerStartupRolesHeld;
      const refusedViewerSwitch = bounded(viewerRecoveryPage.evaluate(() =>
        signIn("admin", "definitely-wrong-password")),
      "preauthenticated Viewer refused login");
      await viewerRecoveryRolesHeld;
      const viewerBeforeRoles = await viewerRecoveryPage.evaluate(() => ({
        user: currentUser && currentUser.username,
        view,
        waitingForRoles: sessionAwaitingRoleMetadata,
        loginVisible: !document.getElementById("login-screen").hidden,
        shellSignedOut: document.body.classList.contains("signed-out"),
        loginError: (document.getElementById("login-error").textContent || "").trim(),
        dashboardPainted: !!document.querySelector(".dash-grid"),
        operatorActivityPainted: (document.getElementById("content").textContent || "")
          .includes("Operator setup ("),
      }));
      const viewerOverviewBeforeRoles = viewerOverviewRequests;
      releaseViewerRoles();
      const refusedViewerOutcome = await refusedViewerSwitch;
      await viewerRecoveryPage.waitForFunction(() =>
        currentUser && currentUser.username === "viewer"
          && roleCatalog.length > 0 && view === "standings"
          && !sessionAwaitingRoleMetadata
          && document.getElementById("login-screen").hidden
          && !!document.querySelector(".st-toolbar"), null, { timeout: 10000 });
      const viewerAfterRoles = await viewerRecoveryPage.evaluate(() => ({
        user: currentUser && currentUser.username,
        view,
        waitingForRoles: sessionAwaitingRoleMetadata,
        loginVisible: !document.getElementById("login-screen").hidden,
        manageUsers: hasPerm("manage_users"),
        dashboardPainted: !!document.querySelector(".dash-grid"),
        toast,
        toastIsError,
      }));
      const viewerCanonical = await viewerRecoveryContext.request.get(
        `${base}/api/auth/me`);
      const viewerCanonicalBody = await viewerCanonical.json();
      await viewerRecoveryPage.unroute("**/api/auth/roles", holdViewerRoles);
      await viewerRecoveryPage.unroute("**/api/demo/overview", countViewerOverview);
      await viewerRecoveryContext.close();
      if (refusedViewerOutcome !== false || viewerRolesRequests !== 2
          || viewerBeforeRoles.user !== "viewer"
          || !viewerBeforeRoles.waitingForRoles
          || !viewerBeforeRoles.loginVisible
          || !viewerBeforeRoles.shellSignedOut
          || !viewerBeforeRoles.loginError
          || viewerBeforeRoles.dashboardPainted
          || viewerBeforeRoles.operatorActivityPainted
          || viewerOverviewBeforeRoles !== 0
          || viewerAfterRoles.user !== "viewer"
          || viewerAfterRoles.view !== "standings"
          || viewerAfterRoles.waitingForRoles
          || viewerAfterRoles.loginVisible
          || viewerAfterRoles.manageUsers
          || viewerAfterRoles.dashboardPainted
          || !viewerAfterRoles.toastIsError
          || viewerAfterRoles.toast !== viewerBeforeRoles.loginError
          || viewerCanonicalBody.user?.username !== "viewer") {
        fail(`preauthenticated Viewer escaped metadata quarantine after a `
          + `refused login: ${JSON.stringify({ refusedViewerOutcome,
            viewerRolesRequests, viewerOverviewBeforeRoles, viewerBeforeRoles,
            viewerAfterRoles, viewerCanonicalBody })}`);
      }
    }

    // A completed current-bootstrap outage has no old request left to rescue a
    // later successful login. Once the endpoint recovers, explicit auth must
    // start its own public-metadata read and converge without a page reload.
    {
      const outageRecoveryContext = await browser.newContext({
        viewport: { width: viewport.width, height: viewport.height },
      });
      const outageRecoveryPage = await outageRecoveryContext.newPage();
      let outageRoleRequests = 0;
      const failOnlyInitialRoles = async (route) => {
        outageRoleRequests += 1;
        if (outageRoleRequests === 1) {
          return route.fulfill({ status: 502, contentType: "text/html",
            body: "Bad Gateway" });
        }
        await route.continue();
      };
      await outageRecoveryPage.route("**/api/auth/roles", failOnlyInitialRoles);
      await outageRecoveryPage.goto(base, { waitUntil: "domcontentloaded" });
      await outageRecoveryPage.waitForFunction(() =>
        !document.getElementById("login-screen").hidden
          && (document.getElementById("login-error").textContent || "")
            .includes("temporarily unavailable"), null, { timeout: 10000 });
      const outageLogin = await bounded(outageRecoveryPage.evaluate(() =>
        signIn("viewer", "demo")), "login after completed bootstrap outage");
      await outageRecoveryPage.waitForFunction(() =>
        currentUser && currentUser.username === "viewer"
          && roleCatalog.length > 0 && view === "standings"
          && !sessionAwaitingRoleMetadata
          && document.getElementById("login-screen").hidden
          && !!document.querySelector(".st-toolbar"), null, { timeout: 10000 });
      const outageRecovered = await outageRecoveryPage.evaluate(() => ({
        user: currentUser && currentUser.username,
        view,
        roles: roleCatalog.length,
        waitingForRoles: sessionAwaitingRoleMetadata,
        loginVisible: !document.getElementById("login-screen").hidden,
        manageUsers: hasPerm("manage_users"),
        dashboardPainted: !!document.querySelector(".dash-grid"),
      }));
      await outageRecoveryPage.unroute("**/api/auth/roles", failOnlyInitialRoles);
      await outageRecoveryContext.close();
      if (outageLogin !== true || outageRoleRequests !== 2
          || outageRecovered.user !== "viewer"
          || outageRecovered.view !== "standings"
          || outageRecovered.roles === 0
          || outageRecovered.waitingForRoles
          || outageRecovered.loginVisible
          || outageRecovered.manageUsers
          || outageRecovered.dashboardPainted) {
        fail(`explicit login did not recover independently after completed `
          + `bootstrap outage: ${JSON.stringify({ outageLogin,
            outageRoleRequests, outageRecovered })}`);
      }
    }

    // Intent-before-headers: the passive demo login must be cancelled before
    // its response can install a cookie. A newer refused manual login wins as
    // an anonymous result; releasing the old transport afterward is a no-op.
    {
      const autoAbortContext = await browser.newContext({
        viewport: { width: viewport.width, height: viewport.height },
      });
      const autoAbortPage = await autoAbortContext.newPage();
      await autoAbortPage.addInitScript(() => {
        const nativeFetch = window.fetch.bind(window);
        let firstLogin = true;
        window.__releasePassiveAutoBeforeHeaders = null;
        window.__passiveAutoBeforeHeadersHeld = false;
        window.__passiveAutoBeforeHeadersAborted = false;
        window.fetch = (input, init = {}) => {
          const url = typeof input === "string" ? input : input.url;
          if (firstLogin && url.endsWith("/api/auth/login")) {
            firstLogin = false;
            window.__passiveAutoBeforeHeadersHeld = true;
            return new Promise((resolve, reject) => {
              let settled = false;
              const abort = () => {
                if (settled) return;
                settled = true;
                window.__passiveAutoBeforeHeadersAborted = true;
                reject(new DOMException("Aborted", "AbortError"));
              };
              if (init.signal) {
                if (init.signal.aborted) abort();
                else init.signal.addEventListener("abort", abort, { once: true });
              }
              window.__releasePassiveAutoBeforeHeaders = () => {
                if (settled) return;
                settled = true;
                nativeFetch(input, init).then(resolve, reject);
              };
            });
          }
          return nativeFetch(input, init);
        };
      });
      await autoAbortPage.goto(base, { waitUntil: "domcontentloaded" });
      await autoAbortPage.waitForFunction(() =>
        window.__passiveAutoBeforeHeadersHeld, null, { timeout: 10000 });
      const refusedAfterPassive = await bounded(autoAbortPage.evaluate(() =>
        signIn("viewer", "definitely-wrong-password")),
      "manual refusal superseding passive auto-login before headers");
      await autoAbortPage.evaluate(() => {
        if (window.__releasePassiveAutoBeforeHeaders) {
          window.__releasePassiveAutoBeforeHeaders();
        }
      });
      await autoAbortPage.waitForTimeout(100);
      const afterPassiveAbort = await autoAbortPage.evaluate(() => ({
        aborted: window.__passiveAutoBeforeHeadersAborted,
        user: currentUser && currentUser.username,
        loginVisible: !document.getElementById("login-screen").hidden,
        shellSignedOut: document.body.classList.contains("signed-out"),
        manageUsers: hasPerm("manage_users"),
        dashboardPainted: !!document.querySelector(".dash-grid"),
      }));
      const afterPassiveAbortMe = await autoAbortContext.request.get(
        `${base}/api/auth/me`);
      const afterPassiveAbortBody = await afterPassiveAbortMe.json();
      await autoAbortContext.close();
      if (refusedAfterPassive !== false || !afterPassiveAbort.aborted
          || afterPassiveAbort.user !== null
          || !afterPassiveAbort.loginVisible
          || !afterPassiveAbort.shellSignedOut
          || afterPassiveAbort.manageUsers
          || afterPassiveAbort.dashboardPainted
          || afterPassiveAbortBody.user != null) {
        fail(`passive auto-login installed or painted Admin after a newer `
          + `manual refusal: ${JSON.stringify({ refusedAfterPassive,
            afterPassiveAbort, afterPassiveAbortBody })}`);
      }
    }

    // Headers-before-body: a passive refusal has already released the cookie
    // lock, but its body remains delayed. A newer successful Viewer login must
    // keep both its identity and clean toast when that stale error drains.
    {
      const autoBodyContext = await browser.newContext({
        viewport: { width: viewport.width, height: viewport.height },
      });
      const autoBodyPage = await autoBodyContext.newPage();
      await autoBodyPage.addInitScript(() => {
        const nativeFetch = window.fetch.bind(window);
        let firstLogin = true;
        let releaseBody;
        window.__passiveAutoBodyHeld = false;
        window.__releasePassiveAutoBody = () => {
          if (releaseBody) releaseBody();
        };
        window.fetch = (input, init = {}) => {
          const url = typeof input === "string" ? input : input.url;
          if (firstLogin && url.endsWith("/api/auth/login")) {
            firstLogin = false;
            return Promise.resolve({
              ok: false,
              status: 401,
              text: () => new Promise((resolve) => {
                releaseBody = () => resolve(JSON.stringify({
                  error: { code: "invalid_credentials",
                    message: "STALE PASSIVE LOGIN REFUSAL" },
                }));
                window.__passiveAutoBodyHeld = true;
              }),
            });
          }
          return nativeFetch(input, init);
        };
      });
      await autoBodyPage.goto(base, { waitUntil: "domcontentloaded" });
      await autoBodyPage.waitForFunction(() => window.__passiveAutoBodyHeld,
        null, { timeout: 10000 });
      const viewerAfterPassiveBody = await bounded(autoBodyPage.evaluate(() =>
        signIn("viewer", "demo")),
      "Viewer success while passive auto-login body is held");
      await autoBodyPage.waitForFunction(() =>
        currentUser && currentUser.username === "viewer"
          && view === "standings"
          && document.getElementById("login-screen").hidden,
      null, { timeout: 10000 });
      await autoBodyPage.evaluate(() => window.__releasePassiveAutoBody());
      await autoBodyPage.waitForTimeout(100);
      const afterPassiveBody = await autoBodyPage.evaluate(() => ({
        user: currentUser && currentUser.username,
        view,
        loginVisible: !document.getElementById("login-screen").hidden,
        manageUsers: hasPerm("manage_users"),
        dashboardPainted: !!document.querySelector(".dash-grid"),
        toast,
        toastIsError,
      }));
      const afterPassiveBodyMe = await autoBodyContext.request.get(
        `${base}/api/auth/me`);
      const afterPassiveBodyCanonical = await afterPassiveBodyMe.json();
      await autoBodyContext.close();
      if (viewerAfterPassiveBody !== true
          || afterPassiveBody.user !== "viewer"
          || afterPassiveBody.view !== "standings"
          || afterPassiveBody.loginVisible
          || afterPassiveBody.manageUsers
          || afterPassiveBody.dashboardPainted
          || afterPassiveBody.toast.includes("STALE PASSIVE")
          || afterPassiveBody.toastIsError
          || afterPassiveBodyCanonical.user?.username !== "viewer") {
        fail(`stale passive auto-login body overwrote a newer Viewer success: `
          + `${JSON.stringify({ viewerAfterPassiveBody, afterPassiveBody,
            afterPassiveBodyCanonical })}`);
      }
    }

    // A demo-store replacement keeps the Admin username but changes both the
    // session cookie and the entire backing store. Prove that it still creates
    // a full identity/session boundary: hold an ordinary pre-reset POST with a
    // private response, supersede the reset's UI continuation with a newer
    // same-Admin login that fails, then deliver the old response. The reset's
    // cookie phase must have invalidated it unconditionally even though no
    // username change and no winning lifecycle render followed.
    const staleResetRosterName = `Pre-reset roster sentinel ${suffix}`;
    let releasePreResetRoster;
    let markPreResetRosterHeld;
    const preResetRosterRelease = new Promise((resolve) => {
      releasePreResetRoster = resolve;
    });
    const preResetRosterHeld = new Promise((resolve) => {
      markPreResetRosterHeld = resolve;
    });
    const preResetRosterUrl = "**/api/games/pre-reset-held-game/roster/copy-previous";
    const holdPreResetRoster = async (route) => {
      markPreResetRosterHeld();
      await preResetRosterRelease;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ team_id: "pre-reset-team",
          source: "copy_previous_roster", seated: [], deferred: [],
          skipped: [{ player_id: "pre-reset-player", name: staleResetRosterName,
            reason: "player_inactive" }] }),
      });
    };
    let releaseHeldDemoReset;
    let markHeldDemoReset;
    const heldDemoResetRelease = new Promise((resolve) => {
      releaseHeldDemoReset = resolve;
    });
    const heldDemoReset = new Promise((resolve) => {
      markHeldDemoReset = resolve;
    });
    const holdDemoResetResponse = async (route) => {
      const response = await route.fetch();
      markHeldDemoReset();
      await heldDemoResetRelease;
      await route.fulfill({ response });
    };
    await page.route(preResetRosterUrl, holdPreResetRoster);
    await page.route("**/api/demo/reset", holdDemoResetResponse);
    await page.evaluate(() => {
      currentGame = "pre-reset-held-game";
      rosterTeamId = "pre-reset-team";
      window.__preResetRoster = rosterAction("copy")
        .then(() => ({ ok: true }))
        .catch((error) => ({ error: error && error.name }));
    });
    await preResetRosterHeld;
    await page.evaluate(() => {
      window.__heldDemoReset = runIdentityBoundSessionTransition(() =>
        sessionCookiePost("/api/demo/reset", { confirm: "RESET" }));
    });
    await heldDemoReset;
    tracker.expect("POST", "/api/auth/login", 401);
    await page.evaluate(() => {
      // A failed persona switch is an expected authentication refusal, not a
      // successful session boundary. The old Admin session must remain both
      // real and visibly adopted.
      window.__failedAdminAfterReset = signIn("admin", "definitely-wrong-password");
    });
    releaseHeldDemoReset();
    const resetAndFailedLogin = await page.evaluate(async () => {
      const values = await Promise.all([
        window.__heldDemoReset, window.__failedAdminAfterReset,
      ]);
      delete window.__heldDemoReset;
      delete window.__failedAdminAfterReset;
      return values;
    });
    releasePreResetRoster();
    const preResetRosterOutcome = await page.evaluate(async () => {
      const value = await window.__preResetRoster;
      delete window.__preResetRoster;
      return value;
    });
    await page.unroute(preResetRosterUrl, holdPreResetRoster);
    await page.unroute("**/api/demo/reset", holdDemoResetResponse);
    const afterSessionReplacement = await page.evaluate(async (sentinel) => {
      const me = await (await fetch("/api/auth/me", { credentials: "same-origin" })).json();
      return {
        me: me.user && me.user.username,
        user: currentUser && currentUser.username,
        epoch: uiIdentityEpoch,
        batch: rosterBatch,
        hasSentinel: (document.documentElement.textContent || "").includes(sentinel),
        loginHidden: document.getElementById("login-screen").hidden,
        signedOutClass: document.body.classList.contains("signed-out"),
        manageUsers: hasPerm("manage_users"),
      };
    }, staleResetRosterName);
    if (!preResetRosterOutcome
        || preResetRosterOutcome.error !== "IdentitySupersededError"
        || !resetAndFailedLogin || resetAndFailedLogin[1] !== false
        || afterSessionReplacement.me !== "admin"
        || afterSessionReplacement.user !== "admin"
        || afterSessionReplacement.batch !== null
        || afterSessionReplacement.hasSentinel
        || !afterSessionReplacement.loginHidden
        || afterSessionReplacement.signedOutClass
        || !afterSessionReplacement.manageUsers) {
      fail(`successful same-Admin session/store replacement admitted a `
        + `pre-reset mutation response: ${JSON.stringify({ resetAndFailedLogin,
          preResetRosterOutcome, afterSessionReplacement })}`);
    }
    const adminAfterReplacement = await page.evaluate(() => signIn("admin", "demo"));
    if (!adminAfterReplacement) fail("Admin restore after session replacement failed");
    await page.evaluate(() => switchTab("dashboard"));
    await reachDashboard(page);

    // Both asynchronous Setup drawer entry points seed parent values from a
    // privileged hierarchy read. Hold each read across Admin -> Viewer and
    // require the departing continuation to disappear silently: no drawer,
    // no stale error toast, and no navigation into Setup under Viewer.
    for (const drawerVariant of ["workflow", "direct"]) {
      let releaseDrawerSeed;
      let markDrawerSeedHeld;
      const drawerSeedRelease = new Promise((resolve) => {
        releaseDrawerSeed = resolve;
      });
      const drawerSeedHeld = new Promise((resolve) => {
        markDrawerSeedHeld = resolve;
      });
      let drawerHierarchyRequests = 0;
      const holdDrawerSeed = async (route) => {
        drawerHierarchyRequests += 1;
        if (drawerHierarchyRequests !== 1) return route.continue();
        const response = await route.fetch();
        markDrawerSeedHeld();
        await drawerSeedRelease;
        await route.fulfill({ response });
      };
      await page.route("**/api/v2/setup/hierarchy", holdDrawerSeed);
      await page.evaluate((variant) => {
        toast = ""; drawer = null; drawerValues = {};
        window.__heldDrawerSeed = (variant === "workflow"
          ? goToSetupWorkflow("teams")
          : openSetupWorkflowDrawer("team"))
          .then(() => ({ ok: true }))
          .catch((error) => ({ error: error && error.name }));
      }, drawerVariant);
      await drawerSeedHeld;
      const viewerDuringDrawerSeed = await page.evaluate(() => signIn("viewer", "demo"));
      if (!viewerDuringDrawerSeed) {
        fail(`${drawerVariant} drawer seed: Admin -> Viewer sign-in failed`);
      }
      releaseDrawerSeed();
      const drawerSeedOutcome = await page.evaluate(async () => {
        const outcome = await window.__heldDrawerSeed;
        delete window.__heldDrawerSeed;
        return outcome;
      });
      await page.unroute("**/api/v2/setup/hierarchy", holdDrawerSeed);
      const staleDrawerBoundary = await page.evaluate(async () => {
        const me = await (await fetch("/api/auth/me", { credentials: "same-origin" })).json();
        return {
          me: me.user && { username: me.user.username, role: me.user.role },
          current: currentUser && { username: currentUser.username, role: currentUser.role },
          view,
          drawer,
          drawerValues,
          drawerDom: document.querySelectorAll(".drawer, .drawer-scrim").length,
          toast,
        };
      });
      if (!drawerSeedOutcome
          || drawerSeedOutcome.error !== "IdentitySupersededError"
          || drawerHierarchyRequests !== 1
          || !staleDrawerBoundary.me || staleDrawerBoundary.me.role !== "viewer"
          || !staleDrawerBoundary.current || staleDrawerBoundary.current.role !== "viewer"
          || staleDrawerBoundary.view === "setup"
          || staleDrawerBoundary.drawer !== null
          || Object.keys(staleDrawerBoundary.drawerValues || {}).length !== 0
          || staleDrawerBoundary.drawerDom !== 0
          || /Couldn't load what's needed/.test(staleDrawerBoundary.toast || "")) {
        fail(`${drawerVariant} hierarchy seed crossed Admin -> Viewer: `
          + `${JSON.stringify({ drawerSeedOutcome, drawerHierarchyRequests,
            staleDrawerBoundary })}`);
      }
      const restoredAdmin = await page.evaluate(() => signIn("admin", "demo"));
      if (!restoredAdmin) fail(`${drawerVariant} drawer seed: Admin restore failed`);
      await page.evaluate(() => switchTab("dashboard"));
      await reachDashboard(page);
    }

    // ============================================================
    // Self-test: prove the failure tracker actually catches an unregistered
    // (unexpected) HTTP failure before anything else relies on it. Without
    // this, a broken tracker (e.g. one that always grants allowance) would
    // let every OTHER assertion in this file pass for the wrong reason.
    // ============================================================
    const errorsBeforeSelfTest = errors.length;
    const unexpectedBeforeSelfTest = tracker.unexpected.length;
    if (unexpectedBeforeSelfTest) {
      fail(`unexpected HTTP failure(s) occurred before the tracker self-test: `
        + `${JSON.stringify(tracker.unexpected)}`);
    }
    const bogus = await apiGet(page, "/api/does-not-exist-xyz-345");
    if (bogus.status !== 404) {
      fail(`self-test: expected the deliberately unregistered route to `
        + `404, got ${bogus.status}`);
    }
    await new Promise((r) => setTimeout(r, 150)); // let the response listener fire
    const selfTestUnexpected = tracker.unexpected.slice(unexpectedBeforeSelfTest);
    const exactSelfTestUnexpected = "GET /api/does-not-exist-xyz-345 -> 404";
    if (selfTestUnexpected.length !== 1
        || selfTestUnexpected[0] !== exactSelfTestUnexpected) {
      fail("self-test: the unexpected-response tracker did not catch a "
        + "deliberately unregistered 404 -- detection is broken, so every "
        + "later 'no unexpected failures' assertion in this file would be "
        + `meaningless; delta=${JSON.stringify(selfTestUnexpected)}`);
    }
    const selfTestErrors = errors.slice(errorsBeforeSelfTest);
    const exactSelfTestConsole = selfTestErrors.filter((entry) =>
      /^\[console\] Failed to load resource/.test(entry)
        && /\/api\/does-not-exist-xyz-345(?:\s|$)/.test(entry)
        && /(?:404|Not Found)/i.test(entry));
    if (exactSelfTestConsole.length !== selfTestErrors.length
        || selfTestErrors.length > 1) {
      fail(`tracker self-test produced unrelated console/page errors: `
        + `${JSON.stringify(selfTestErrors)}`);
    }
    // Consumed and proven -- discard this self-test's own artifacts (both
    // the tracked response and its console noise) so they don't fail the
    // real run for a reason unrelated to any role's own behavior.
    tracker.unexpected.splice(unexpectedBeforeSelfTest, 1);
    errors.splice(errorsBeforeSelfTest, selfTestErrors.length);

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
    // A superseded pass may briefly leave the intentionally-labelled stale
    // card on screen. The leg below needs the fresh Program's actionable card,
    // not merely any non-skeleton terminal state.
    await page.waitForSelector("[data-setup-progress-action]", { timeout: 10000 });
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
    // A second real League Admin is reserved for the password-restoration
    // ownership race below. It is not an eighth role case; its only purpose
    // is to replace one privileged identity with another that can render the
    // same Users form, so DOM shape cannot accidentally hide a secret leak.
    const secondAdmin = {
      username: mk("secondadmin"), role: "league_admin", scope: {},
    };
    const secondAdminCreate = await apiPost(page, "/api/accounts", {
      username: secondAdmin.username, password: PW,
      role: secondAdmin.role, scope: secondAdmin.scope,
    });
    if (secondAdminCreate.status !== 200 || secondAdminCreate.body.error) {
      fail(`second Admin account create failed: ${JSON.stringify(secondAdminCreate)}`);
    }
    secondAdmin.id = secondAdminCreate.body.id;
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

    // Keep the synthetic cookie-revocation authority race in its own browser
    // context.  This journey has already driven many deliberate overlapping
    // renders on `page`; inheriting one of those requests would make an
    // out-of-band cookie clear manufacture an unrelated 401.  A fresh context
    // gives this control its own complete request ledger without weakening the
    // global unexpected-response detector or granting the app a fake verdict.
    const revocationContext = await browser.newContext({
      viewport: { width: viewport.width, height: viewport.height },
    });
    try {
      const context = revocationContext;
      const page = await context.newPage();
      const errors = [];
      page.on("pageerror", (e) => errors.push(`[pageerror] ${e.message}`));
      const tracker = makeFailureTracker(page, errors);

    // ============================================================
    // Explicit in-app, NO-RELOAD role switch: League Admin -> Viewer via
    // the app's own signIn() (the exact function the login form and demo
    // role-switcher call) -- never a page.goto(). Proves switching
    // identity strips every admin-only nav tab and Dashboard card from the
    // live DOM on its own, not merely on the next fresh navigation.
    // ============================================================
    // Install on about:blank so the sole application document starts with the
    // fixture already present; no throwaway boot can leave requests behind in
    // this control's otherwise isolated ledger.
    await installContextFixture(page);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await reachDashboard(page);
    if (!(await page.$('.tab[data-tab="users"]'))) fail("setup precondition: Users tab not visible for League Admin");

    // A second unverified login CLICK is not an authority verdict. Revoke the
    // browser cookie out of band while Admin remains painted, let failed login
    // #1 obtain an authoritative anonymous /auth/me body, then hold login #2
    // after its click has advanced local intent but before it can answer. The
    // newer click must not suppress #1's teardown: private state must be gone
    // while #2 is still unresolved.
    let releaseAnonymousR1;
    let markAnonymousR1Held;
    const anonymousR1Release = new Promise((resolve) => {
      releaseAnonymousR1 = resolve;
    });
    const anonymousR1Held = new Promise((resolve) => {
      markAnonymousR1Held = resolve;
    });
    let anonymousMeReads = 0;
    const holdAnonymousR1 = async (route) => {
      anonymousMeReads += 1;
      const response = await route.fetch();
      const captured = await response.json();
      if (anonymousMeReads === 1) {
        if (captured.user !== null) {
          fail(`revoked-cookie R1 was not anonymous: ${JSON.stringify(captured)}`);
        }
        markAnonymousR1Held();
        await anonymousR1Release;
      }
      await route.fulfill({ response, json: captured });
    };
    let releaseSecondInvalidLogin;
    let markSecondInvalidLoginHeld;
    const secondInvalidLoginRelease = new Promise((resolve) => {
      releaseSecondInvalidLogin = resolve;
    });
    const secondInvalidLoginHeld = new Promise((resolve) => {
      markSecondInvalidLoginHeld = resolve;
    });
    let invalidLoginPosts = 0;
    const holdSecondInvalidLogin = async (route) => {
      invalidLoginPosts += 1;
      if (invalidLoginPosts === 2) {
        markSecondInvalidLoginHeld();
        await secondInvalidLoginRelease;
      }
      const response = await route.fetch();
      await route.fulfill({ response });
    };
    // This leg expires the cookie outside app.js. Settle Dashboard's awaited
    // reads and its fire-and-forget Home/Tasks card first, then invalidate any
    // dormant render continuation immediately before expiry. Otherwise an old
    // notification/progress GET can legitimately reach the server after the
    // synthetic clear and contaminate the authority test with an unrelated
    // 401 whose endpoint depends only on viewport timing.
    // `reachDashboard` observes the destination, not completion of the
    // fire-and-forget bootstrap render that produced it. Drain that one pass;
    // starting a second render here would only supersede the first while its
    // ordinary overview/progress requests remained live on the wire.
    await tracker.waitForIdle(30000);
    await waitForCardSettled(page);
    await tracker.waitForIdle();
    await page.evaluate(async () => {
      renderPass += 1;
      cancelContextScopedReads();
      await awaitContextScopedReadSettlement();
    });
    // Install the holds only after the precondition barrier. Registering the
    // /auth/me hold earlier would capture an ordinary authenticated bootstrap
    // or focus read and manufacture a permanently pending request before the
    // cookie this control is meant to revoke has even been cleared.
    tracker.expect("POST", "/api/auth/login", 401);
    tracker.expect("POST", "/api/auth/login", 401);
    await page.route("**/api/auth/me", holdAnonymousR1);
    await page.route("**/api/auth/login", holdSecondInvalidLogin);
    await context.clearCookies();
    await page.evaluate(() => {
      window.__firstInvalidAfterRevocationSettled = false;
      window.__firstInvalidAfterRevocation = signIn(
        "admin", "definitely-wrong-password")
        .finally(() => { window.__firstInvalidAfterRevocationSettled = true; });
    });
    await bounded(anonymousR1Held, "revoked-cookie anonymous R1");
    await page.evaluate(() => {
      window.__secondInvalidAfterRevocationSettled = false;
      window.__secondInvalidAfterRevocation = signIn(
        "admin", "definitely-wrong-password")
        .finally(() => { window.__secondInvalidAfterRevocationSettled = true; });
    });
    await bounded(secondInvalidLoginHeld, "second invalid login dispatch");
    releaseAnonymousR1();
    await page.waitForFunction(() => currentUser === null
      && contextOptions === null
      && !document.getElementById("login-screen").hidden,
    null, { timeout: 10000 });
    const anonymousBeforeSecondSettles = await page.evaluate(() => ({
      current: currentUser,
      contextOptions,
      firstSettled: window.__firstInvalidAfterRevocationSettled,
      secondSettled: window.__secondInvalidAfterRevocationSettled,
      loginVisible: !document.getElementById("login-screen").hidden,
      shellSignedOut: document.body.classList.contains("signed-out"),
      manageUsers: hasPerm("manage_users"),
      privateText: (document.getElementById("content").textContent || "").trim(),
    }));
    if (anonymousBeforeSecondSettles.current !== null
        || anonymousBeforeSecondSettles.contextOptions !== null
        || !anonymousBeforeSecondSettles.firstSettled
        || anonymousBeforeSecondSettles.secondSettled
        || !anonymousBeforeSecondSettles.loginVisible
        || !anonymousBeforeSecondSettles.shellSignedOut
        || anonymousBeforeSecondSettles.manageUsers
        || anonymousBeforeSecondSettles.privateText) {
      releaseSecondInvalidLogin();
      fail(`newer unverified login intent suppressed anonymous teardown: `
        + `${JSON.stringify(anonymousBeforeSecondSettles)}`);
    }
    releaseSecondInvalidLogin();
    const invalidRevocationOutcomes = await page.evaluate(async () => {
      const outcomes = await Promise.all([
        window.__firstInvalidAfterRevocation,
        window.__secondInvalidAfterRevocation,
      ]);
      delete window.__firstInvalidAfterRevocation;
      delete window.__secondInvalidAfterRevocation;
      delete window.__firstInvalidAfterRevocationSettled;
      delete window.__secondInvalidAfterRevocationSettled;
      return outcomes;
    });
    await page.unroute("**/api/auth/login", holdSecondInvalidLogin);
    await page.unroute("**/api/auth/me", holdAnonymousR1);
    const anonymousAfterBothFailures = await apiGet(page, "/api/auth/me");
    if (invalidLoginPosts !== 2 || anonymousMeReads !== 2
        || JSON.stringify(invalidRevocationOutcomes) !== JSON.stringify([false, false])
        || anonymousAfterBothFailures.status !== 200
        || anonymousAfterBothFailures.body.user !== null) {
      fail(`revoked-cookie two-click control did not settle anonymously: `
        + `${JSON.stringify({ invalidLoginPosts, anonymousMeReads,
          invalidRevocationOutcomes, anonymousAfterBothFailures })}`);
    }
      const missingRevocationFailures = tracker.unmatched();
      if (missingRevocationFailures.length) {
        fail(`revoked-cookie control did not realize expected failure(s): `
          + `${JSON.stringify(missingRevocationFailures)}`);
      }
      if (tracker.unexpected.length) {
        fail(`unexpected HTTP failure response(s) in revoked-cookie control:\n`
          + `${tracker.unexpected.join("\n")}`);
      }
      if (errors.length) {
        fail(`console/page errors in revoked-cookie control:\n${errors.join("\n")}`);
      }
    } finally {
      await revocationContext.close();
    }

    // Re-enter the main journey under its original Admin identity.  The
    // isolated control above cleared only its own cookie jar.
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await installContextFixture(page);
    await reachDashboard(page);

    // Canonical authority reads can overlap without any cookie/generation
    // change when an administrator rebinds a live account's scope. Exercise
    // the ABA ordering directly against the real backend: R1 captures Coach B
    // and is held; the account moves back to Coach A; a refused login then
    // triggers R2, whose canonical /auth/me verdict confirms the already-
    // painted Coach A. R2 must still supersede R1 even though its identity
    // signature equals the current model. Releasing R1 afterwards must never
    // regress the client to Team B while the unchanged cookie authorizes A.
    const scopeA = coachTeam.body.id;
    const scopeB = rivalTeam.body.id;
    const coachForScopeAba = await page.evaluate(([username, password]) =>
      signIn(username, password), [accounts.coach.username, PW]);
    if (!coachForScopeAba) fail("scope-ABA setup could not sign in as Coach A");
    await page.waitForFunction((teamId) => currentUser
      && currentUser.role === "coach"
      && currentUser.scope && currentUser.scope.team_id === teamId,
    scopeA, { timeout: 10000 });
    const scopeAbaSessionToken = await page.evaluate(() =>
      readSessionMutationToken());
    if (!scopeAbaSessionToken) {
      fail("scope-ABA setup did not establish a published session generation");
    }
    const rebindToB = await apiPost(reader,
      `/api/accounts/${accounts.coach.id}/scope`,
      { scope: { team_id: scopeB } });
    if (rebindToB.status !== 200 || rebindToB.body.error) {
      fail(`scope-ABA setup could not rebind Coach A -> B: ${JSON.stringify(rebindToB)}`);
    }
    let releaseScopeAbaR1;
    let markScopeAbaR1Held;
    const scopeAbaR1Release = new Promise((resolve) => {
      releaseScopeAbaR1 = resolve;
    });
    const scopeAbaR1Held = new Promise((resolve) => {
      markScopeAbaR1Held = resolve;
    });
    let scopeAbaMeReads = 0;
    const scopeAbaMeScopes = [];
    const holdScopeAbaR1 = async (route) => {
      scopeAbaMeReads += 1;
      const response = await route.fetch();
      const captured = await response.json();
      scopeAbaMeScopes.push(captured.user && captured.user.scope
        ? captured.user.scope.team_id : null);
      if (scopeAbaMeReads !== 1) {
        await route.fulfill({ response, json: captured });
        return;
      }
      if (!captured.user || !captured.user.scope
          || captured.user.scope.team_id !== scopeB) {
        fail(`scope-ABA R1 did not capture Coach B: ${JSON.stringify(captured)}`);
      }
      markScopeAbaR1Held();
      await scopeAbaR1Release;
      try { await route.fulfill({ response, json: captured }); }
      catch (_) { /* R2 correctly aborted this obsolete route. */ }
    };
    await page.route("**/api/auth/me", holdScopeAbaR1);
    await page.evaluate(() => window.dispatchEvent(new Event("focus")));
    await scopeAbaR1Held;
    const rebindBackToA = await apiPost(reader,
      `/api/accounts/${accounts.coach.id}/scope`,
      { scope: { team_id: scopeA } });
    if (rebindBackToA.status !== 200 || rebindBackToA.body.error) {
      fail(`scope-ABA setup could not rebind Coach B -> A: ${JSON.stringify(rebindBackToA)}`);
    }
    tracker.expect("POST", "/api/auth/login", 401);
    const refusedScopeAbaLogin = await page.evaluate(([username, password]) =>
      signIn(username, password),
    [accounts.coach.username, "definitely-wrong-password"]);
    const afterNewerScopeA = await page.evaluate(() => ({
      current: currentUser && currentUser.username,
      role: currentUser && currentUser.role,
      team: currentUser && currentUser.scope && currentUser.scope.team_id,
      loginHidden: document.getElementById("login-screen").hidden,
    }));
    if (refusedScopeAbaLogin !== false
        || afterNewerScopeA.current !== accounts.coach.username
        || afterNewerScopeA.role !== "coach"
        || afterNewerScopeA.team !== scopeA
        || !afterNewerScopeA.loginHidden) {
      fail(`newer canonical Coach A verdict did not preserve the live model: `
        + `${JSON.stringify({ refusedScopeAbaLogin, afterNewerScopeA })}`);
    }
    releaseScopeAbaR1();
    await page.waitForFunction(() => !resumeSessionValidationInFlight,
    null, { timeout: 10000 });
    await page.unroute("**/api/auth/me", holdScopeAbaR1);
    const scopeAbaCanonical = await apiGet(page, "/api/auth/me");
    const afterStaleScopeB = await page.evaluate(() => ({
      current: currentUser && currentUser.username,
      role: currentUser && currentUser.role,
      team: currentUser && currentUser.scope && currentUser.scope.team_id,
      loginHidden: document.getElementById("login-screen").hidden,
      sessionToken: readSessionMutationToken(),
    }));
    if (scopeAbaMeReads !== 2
        || JSON.stringify(scopeAbaMeScopes) !== JSON.stringify([scopeB, scopeA])
        || !scopeAbaCanonical.body.user
        || scopeAbaCanonical.body.user.scope.team_id !== scopeA
        || afterStaleScopeB.current !== accounts.coach.username
        || afterStaleScopeB.role !== "coach"
        || afterStaleScopeB.team !== scopeA
        || !afterStaleScopeB.loginHidden
        || afterStaleScopeB.sessionToken !== scopeAbaSessionToken) {
      fail(`stale Coach B /api/auth/me verdict crossed the same-token scope `
        + `ABA boundary: ${JSON.stringify({ scopeAbaMeReads,
          scopeAbaMeScopes, canonical: scopeAbaCanonical.body,
          afterStaleScopeB })}`);
    }

    // Negative control for the single-winner read lane and strict classifier:
    // hold a valid Coach-B R1, then dispatch R2 with a malformed successful
    // body. R2 must abort R1 immediately, but `{}` is not an anonymous verdict
    // and must claim nothing itself. Because R2 discarded the only pending
    // read that could expose the rebind, it must quarantine the painted Coach
    // A shell and automatically dispatch a fresh R3. Hold R3 to prove the
    // quarantine is real, then let it recover the server's Coach-B authority.
    const invalidNewerRebindToB = await apiPost(reader,
      `/api/accounts/${accounts.coach.id}/scope`,
      { scope: { team_id: scopeB } });
    if (invalidNewerRebindToB.status !== 200
        || invalidNewerRebindToB.body.error) {
      fail(`invalid-newer setup could not rebind Coach A -> B: `
        + `${JSON.stringify(invalidNewerRebindToB)}`);
    }
    let releaseValidOlderB;
    let markValidOlderBHeld;
    const validOlderBRelease = new Promise((resolve) => {
      releaseValidOlderB = resolve;
    });
    const validOlderBHeld = new Promise((resolve) => {
      markValidOlderBHeld = resolve;
    });
    let releaseInvalidNewerRecovery;
    let markInvalidNewerRecoveryHeld;
    const invalidNewerRecoveryRelease = new Promise((resolve) => {
      releaseInvalidNewerRecovery = resolve;
    });
    const invalidNewerRecoveryHeld = new Promise((resolve) => {
      markInvalidNewerRecoveryHeld = resolve;
    });
    let invalidNewerMeReads = 0;
    const validOlderInvalidNewer = async (route) => {
      invalidNewerMeReads += 1;
      if (invalidNewerMeReads === 1) {
        const response = await route.fetch();
        const captured = await response.json();
        if (!captured.user || !captured.user.scope
            || captured.user.scope.team_id !== scopeB) {
          fail(`invalid-newer R1 did not capture Coach B: `
            + `${JSON.stringify(captured)}`);
        }
        markValidOlderBHeld();
        await validOlderBRelease;
        try { await route.fulfill({ response, json: captured }); }
        catch (_) { /* R2 correctly aborted this obsolete route. */ }
        return;
      }
      if (invalidNewerMeReads === 2) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({}),
        });
        return;
      }
      const response = await route.fetch();
      const captured = await response.json();
      if (!captured.user || !captured.user.scope
          || captured.user.scope.team_id !== scopeB) {
        fail(`invalid-newer R3 did not capture Coach B: `
          + `${JSON.stringify(captured)}`);
      }
      markInvalidNewerRecoveryHeld();
      await invalidNewerRecoveryRelease;
      await route.fulfill({ response, json: captured });
    };
    await page.route("**/api/auth/me", validOlderInvalidNewer);
    await page.evaluate(() => window.dispatchEvent(new Event("focus")));
    await validOlderBHeld;
    tracker.expect("POST", "/api/auth/login", 401);
    const refusedDuringInvalidNewer = await page.evaluate(([username, password]) =>
      signIn(username, password),
    [accounts.coach.username, "definitely-wrong-password"]);
    await bounded(invalidNewerRecoveryHeld, "malformed-R2 quarantine recovery");
    const quarantinedAfterMalformedR2 = await page.evaluate(() => ({
      current: currentUser,
      contextOptions,
      loginVisible: !document.getElementById("login-screen").hidden,
      shellSignedOut: document.body.classList.contains("signed-out"),
      manageUsers: hasPerm("manage_users"),
      privateText: (document.getElementById("content").textContent || "").trim(),
    }));
    releaseValidOlderB();
    if (refusedDuringInvalidNewer !== false
        || invalidNewerMeReads !== 3
        || quarantinedAfterMalformedR2.current !== null
        || quarantinedAfterMalformedR2.contextOptions !== null
        || !quarantinedAfterMalformedR2.loginVisible
        || !quarantinedAfterMalformedR2.shellSignedOut
        || quarantinedAfterMalformedR2.manageUsers
        || quarantinedAfterMalformedR2.privateText) {
      releaseInvalidNewerRecovery();
      fail(`malformed newer /api/auth/me retained private state before its `
        + `recovery verdict: ${JSON.stringify({ refusedDuringInvalidNewer,
          invalidNewerMeReads, quarantinedAfterMalformedR2 })}`);
    }
    releaseInvalidNewerRecovery();
    await page.waitForFunction((teamId) => currentUser
      && currentUser.role === "coach"
      && currentUser.scope && currentUser.scope.team_id === teamId
      && !resumeSessionValidationInFlight
      && document.getElementById("login-screen").hidden,
    scopeB, { timeout: 10000 });
    await page.unroute("**/api/auth/me", validOlderInvalidNewer);
    const afterMalformedR2 = await page.evaluate(() => ({
      current: currentUser && currentUser.username,
      team: currentUser && currentUser.scope && currentUser.scope.team_id,
      sessionToken: readSessionMutationToken(),
    }));
    const invalidNewerCanonical = await apiGet(page, "/api/auth/me");
    if (afterMalformedR2.current !== accounts.coach.username
        || afterMalformedR2.team !== scopeB
        || afterMalformedR2.sessionToken !== scopeAbaSessionToken
        || !invalidNewerCanonical.body.user
        || invalidNewerCanonical.body.user.scope.team_id !== scopeB) {
      fail(`fresh canonical recovery did not rebuild Coach B after malformed `
        + `R2 quarantine: ${JSON.stringify({ refusedDuringInvalidNewer,
          invalidNewerMeReads, afterMalformedR2,
          canonical: invalidNewerCanonical.body })}`);
    }
    const invalidNewerRebindBackToA = await apiPost(reader,
      `/api/accounts/${accounts.coach.id}/scope`,
      { scope: { team_id: scopeA } });
    if (invalidNewerRebindBackToA.status !== 200
        || invalidNewerRebindBackToA.body.error) {
      fail(`invalid-newer cleanup could not rebind Coach B -> A: `
        + `${JSON.stringify(invalidNewerRebindBackToA)}`);
    }
    await page.evaluate(() => window.dispatchEvent(new Event("focus")));
    await page.waitForFunction((teamId) => currentUser
      && currentUser.scope && currentUser.scope.team_id === teamId
      && !resumeSessionValidationInFlight,
    scopeA, { timeout: 10000 });
    const adminAfterScopeAba = await page.evaluate(() => signIn("admin", "demo"));
    if (!adminAfterScopeAba) fail("Admin restore after scope-ABA test failed");
    await page.evaluate(() => switchTab("dashboard"));
    await reachDashboard(page);

    // Revoking a session is ambiguous at the response boundary: the server
    // cannot tell the browser whether the selected row is this tab's cookie.
    // Exercise BOTH outcomes through the real Users UI. In either case the
    // successful POST must first destroy the Admin model/DOM while canonical
    // /auth/me is held. Only after that read may an unaffected current
    // session rebuild; revoking this tab's own session must remain signed out.
    const accountInventory = await apiGet(page, "/api/accounts");
    const adminAccount = (accountInventory.body.user_accounts || [])
      .find((account) => account.username === "admin");
    if (!adminAccount) fail("session-revoke setup could not find the Admin account");
    const sessionsBeforeFreshLogin = await apiGet(
      page, `/api/accounts/${adminAccount.id}/sessions`);
    const priorAdminSessionIds = new Set(
      (sessionsBeforeFreshLogin.body.sessions || []).map((session) => session.id));
    const freshAdminLogin = await page.evaluate(() => signIn("admin", "demo"));
    if (!freshAdminLogin) fail("session-revoke setup could not establish a fresh Admin session");
    const sessionsAfterFreshLogin = await apiGet(
      page, `/api/accounts/${adminAccount.id}/sessions`);
    const freshAdminSessions = (sessionsAfterFreshLogin.body.sessions || [])
      .filter((session) => session.status === "active"
        && !priorAdminSessionIds.has(session.id));
    const currentAdminSession = freshAdminSessions[0];
    if (freshAdminSessions.length !== 1) {
      fail(`session-revoke setup could not identify this tab's fresh session: `
        + `${JSON.stringify(sessionsAfterFreshLogin.body)}`);
    }

    const otherAdminUserAgent = `matrix-other-admin-${suffix}`;
    const otherAdminContext = await browser.newContext({
      viewport: { width: viewport.width, height: viewport.height },
      userAgent: otherAdminUserAgent,
    });
    const otherAdminLoginResponse = await otherAdminContext.request.post(
      `${base}/api/auth/login`, { data: { username: "admin", password: "demo" } });
    const otherAdminLoginBody = await otherAdminLoginResponse.json();
    if (!otherAdminLoginResponse.ok() || !otherAdminLoginBody.user) {
      await otherAdminContext.close();
      fail(`session-revoke setup could not create the other Admin session: `
        + `${JSON.stringify(otherAdminLoginBody)}`);
    }
    const sessionsWithOther = await apiGet(
      page, `/api/accounts/${adminAccount.id}/sessions`);
    const otherAdminSessions = (sessionsWithOther.body.sessions || [])
      .filter((session) => session.status === "active"
        && session.user_agent === otherAdminUserAgent);
    const otherAdminSession = otherAdminSessions[0];
    if (otherAdminSessions.length !== 1
        || otherAdminSession.id === currentAdminSession.id) {
      await otherAdminContext.close();
      fail(`session-revoke setup could not identify the other Admin session: `
        + `${JSON.stringify(sessionsWithOther.body)}`);
    }

    const selectAdminSession = async (sessionId) => {
      await page.evaluate(() => switchTab("users"));
      await waitForView(page, "users");
      await waitForRealContent(page);
      await page.click(`[data-user-sessions="${adminAccount.id}"]`);
      await page.waitForSelector(`[data-revoke-session="${sessionId}"]`,
        { state: "visible", timeout: 10000 });
      await page.waitForFunction(() => !resumeSessionValidationInFlight,
        null, { timeout: 10000 });
      await page.waitForLoadState("networkidle").catch(() => {});
    };
    const heldRevokeBoundary = async (sessionId, expectSelfRevoked) => {
      await selectAdminSession(sessionId);
      let releaseRevokeMe;
      let markRevokeMeHeld;
      let markRevokeMeDelivered;
      const revokeMeRelease = new Promise((resolve) => { releaseRevokeMe = resolve; });
      const revokeMeHeld = new Promise((resolve) => { markRevokeMeHeld = resolve; });
      const revokeMeDelivered = new Promise((resolve) => {
        markRevokeMeDelivered = resolve;
      });
      let revokeMeRequests = 0;
      const holdRevokeMe = async (route) => {
        revokeMeRequests += 1;
        if (revokeMeRequests !== 1) return route.continue();
        const response = await route.fetch();
        markRevokeMeHeld();
        await revokeMeRelease;
        await route.fulfill({ response });
        markRevokeMeDelivered();
      };
      if (expectSelfRevoked) tracker.expect("GET", "/api/auth/me", 401);
      await page.route("**/api/auth/me", holdRevokeMe);
      const revokeResponsePromise = page.waitForResponse((response) => {
        const url = new URL(response.url());
        return response.request().method() === "POST"
          && url.pathname === `/api/accounts/${adminAccount.id}/sessions/${sessionId}/revoke`;
      });
      await page.click(`[data-revoke-session="${sessionId}"]`);
      const revokeResponse = await revokeResponsePromise;
      const revokeBody = await revokeResponse.json();
      const revokeBodyBlob = JSON.stringify(revokeBody);
      if (revokeResponse.status() !== 200
          || revokeBody.id !== sessionId || revokeBody.status !== "revoked"
          || !revokeBody.revoked_at
          || /token_hash|token/i.test(revokeBodyBlob)) {
        fail(`session revoke returned an invalid or sensitive contract: `
          + `${JSON.stringify({ sessionId, status: revokeResponse.status(), revokeBody })}`);
      }
      await revokeMeHeld;
      const quarantine = await page.evaluate(() => ({
        current: currentUser,
        accounts: usersState.accounts.length,
        sessions: usersState.sessions.length,
        contentChildren: document.getElementById("content").children.length,
        loginVisible: !document.getElementById("login-screen").hidden,
        shellSignedOut: document.body.classList.contains("signed-out"),
        manageUsers: hasPerm("manage_users"),
      }));
      if (quarantine.current !== null
          || quarantine.accounts !== 0 || quarantine.sessions !== 0
          || quarantine.contentChildren !== 0
          || !quarantine.loginVisible || !quarantine.shellSignedOut
          || quarantine.manageUsers) {
        fail(`session revoke exposed Admin state while /auth/me was held: `
          + `${JSON.stringify({ sessionId, expectSelfRevoked, quarantine })}`);
      }
      releaseRevokeMe();
      await revokeMeDelivered;
      if (expectSelfRevoked) {
        await page.waitForFunction(() => currentUser === null
          && !document.getElementById("login-screen").hidden,
        null, { timeout: 10000 });
      } else {
        await page.waitForFunction(() => currentUser && currentUser.username === "admin"
          && document.getElementById("login-screen").hidden,
        null, { timeout: 10000 });
      }
      await page.unroute("**/api/auth/me", holdRevokeMe);
      const canonicalResponse = await context.request.get(`${base}/api/auth/me`);
      const canonicalBody = await canonicalResponse.json();
      const settled = await page.evaluate(() => ({
        current: currentUser && currentUser.username,
        loginVisible: !document.getElementById("login-screen").hidden,
        shellSignedOut: document.body.classList.contains("signed-out"),
        sidebar: (document.getElementById("user-name").textContent || "").trim(),
        manageUsers: hasPerm("manage_users"),
        sticky: localStorage.getItem("hs_signed_out"),
        contextOptions,
        contextEpoch,
        hash: location.hash,
        toast,
      }));
      settled.status = canonicalResponse.status();
      settled.server = canonicalBody.user ? canonicalBody.user.username : null;
      if (expectSelfRevoked
          ? settled.status !== 401 || settled.server !== null
            || settled.current !== null || !settled.loginVisible
            || !settled.shellSignedOut || settled.sidebar !== "Signed out"
            || settled.manageUsers || settled.sticky !== "1"
            || settled.contextOptions !== null || settled.contextEpoch !== null
            || /^#ctx=/.test(settled.hash || "")
          : settled.status !== 200 || settled.server !== "admin"
            || settled.current !== "admin" || settled.loginVisible
            || settled.shellSignedOut || !settled.manageUsers
            || settled.toast !== "Session revoked.") {
        fail(`session revoke settled to the wrong canonical identity: `
          + `${JSON.stringify({ sessionId, expectSelfRevoked, revokeMeRequests,
            settled })}`);
      }
    };

    await heldRevokeBoundary(otherAdminSession.id, false);
    const otherSessionMe = await otherAdminContext.request.get(`${base}/api/auth/me`);
    const sessionsAfterOtherRevoke = await apiGet(
      page, `/api/accounts/${adminAccount.id}/sessions`);
    const otherSessionRow = (sessionsAfterOtherRevoke.body.sessions || [])
      .find((session) => session.id === otherAdminSession.id);
    if (otherSessionMe.status() !== 401
        || !otherSessionRow || otherSessionRow.status !== "revoked"
        || !(sessionsAfterOtherRevoke.body.sessions || []).some((session) =>
          session.id === currentAdminSession.id && session.status === "active")) {
      await otherAdminContext.close();
      fail(`other-session revoke did not revoke only its target: `
        + `${JSON.stringify({ otherStatus: otherSessionMe.status(), otherSessionRow })}`);
    }
    await otherAdminContext.close();
    await heldRevokeBoundary(currentAdminSession.id, true);
    const sessionsAfterSelfRevoke = await apiGet(
      reader, `/api/accounts/${adminAccount.id}/sessions`);
    const selfSessionRow = (sessionsAfterSelfRevoke.body.sessions || [])
      .find((session) => session.id === currentAdminSession.id);
    if (!selfSessionRow || selfSessionRow.status !== "revoked") {
      fail(`self-session revoke did not persist the exact target row: `
        + `${JSON.stringify(selfSessionRow)}`);
    }
    const adminAfterSelfRevoke = await page.evaluate(() => signIn("admin", "demo"));
    if (!adminAfterSelfRevoke) fail("Admin restore after self-session revoke failed");
    await page.evaluate(() => switchTab("dashboard"));
    await reachDashboard(page);

    // The create-account password is deliberately closure-only. Pause the
    // role-change handler AFTER its real render has completed but BEFORE its
    // password-restoration continuation, replace Admin A with Admin B (same
    // privileged Users form), then release A. The old secret must not be
    // written into B's same-id password field.
    const passwordRaceSecret = `Departing-Admin-secret-${suffix}`;
    await page.evaluate(() => switchTab("users"));
    await waitForView(page, "users");
    await waitForRealContent(page);
    await page.waitForSelector("#new-account-password", { state: "visible" });
    await page.evaluate((secret) => {
      const originalRender = render;
      let release;
      let markHeld;
      const released = new Promise((resolve) => { release = resolve; });
      const held = new Promise((resolve) => { markHeld = resolve; });
      window.__passwordRestoreRace = {
        originalRender, release, held, rolePromise: null,
      };
      let holdNextCompletedRender = true;
      render = async (...args) => {
        const result = await originalRender(...args);
        if (holdNextCompletedRender) {
          holdNextCompletedRender = false;
          markHeld();
          await released;
        }
        return result;
      };
      document.getElementById("new-account-password").value = secret;
      const role = document.getElementById("new-account-role");
      role.value = "coach";
      window.__passwordRestoreRace.rolePromise = role.onchange();
    }, passwordRaceSecret);
    await page.evaluate(() => window.__passwordRestoreRace.held);
    const secondAdminLogin = await page.evaluate(([username, password]) =>
      signIn(username, password), [secondAdmin.username, PW]);
    if (!secondAdminLogin) fail("password race: second Admin sign-in failed");
    const passwordBeforeRelease = await page.evaluate(() => ({
      username: currentUser && currentUser.username,
      view: document.body.dataset.view,
      password: (document.getElementById("new-account-password") || {}).value || "",
    }));
    if (passwordBeforeRelease.username !== secondAdmin.username
        || passwordBeforeRelease.view !== "users"
        || passwordBeforeRelease.password !== "") {
      fail(`password race did not establish a clean replacement Admin form: `
        + `${JSON.stringify(passwordBeforeRelease)}`);
    }
    await page.evaluate(async () => {
      const race = window.__passwordRestoreRace;
      race.release();
      await race.rolePromise;
      render = race.originalRender;
      delete window.__passwordRestoreRace;
    });
    const passwordAfterRelease = await page.evaluate(() => ({
      username: currentUser && currentUser.username,
      password: (document.getElementById("new-account-password") || {}).value || "",
    }));
    if (passwordAfterRelease.username !== secondAdmin.username
        || passwordAfterRelease.password !== "") {
      fail(`departing Admin password was restored into the replacement identity: `
        + `${JSON.stringify(passwordAfterRelease)}`);
    }
    const adminAfterPasswordRace = await page.evaluate(() => signIn("admin", "demo"));
    if (!adminAfterPasswordRace) fail("Admin restore after password race failed");
    await page.evaluate(() => switchTab("dashboard"));
    await reachDashboard(page);

    // Populate a real operator-only staff directory before the no-reload
    // identity transition.  The lower role cannot fetch this pool, so the
    // reset boundary must destroy it rather than merely hide its controls.
    await page.evaluate(() => switchTab("sheet"));
    await waitForView(page, "sheet");
    await waitForRealContent(page);
    await page.waitForFunction(() => officialsPool.length > 0, null,
      { timeout: 10000 });
    const privateOfficial = {
      id: official.body.id,
      name: `Ozzy Official ${suffix}`,
    };
    const adminOfficialPool = await page.evaluate(() =>
      officialsPool.map((entry) => ({ id: entry.id, name: entry.name })));
    if (!adminOfficialPool.some((entry) => entry.id === privateOfficial.id
        && entry.name === privateOfficial.name)) {
      fail(`setup precondition: League Admin's directory did not contain the `
        + `distinctive private Official: ${JSON.stringify(adminOfficialPool)}`);
    }

    // Seed every identity-owned store whose renderer can otherwise retain a
    // prior value after the arriving request fails. These values are kept out
    // of the DOM so the painted-Game-Sheet assertion below remains an
    // independent boundary; the held context lets us inspect the stores after
    // setUser(Viewer) but before Viewer has any opportunity to overwrite them.
    const identitySentinels = {
      skipped: `Departing roster candidate ${suffix}`,
      notification: `Departing notification ${suffix}`,
      destination: `departing-${suffix}@example.invalid`,
      token: `departing-device-token-${suffix}`,
      modal: `Departing reset detail ${suffix}`,
      state: `Departing identity state ${suffix}`,
      password: `Departing-password-${suffix}`,
    };
    const departingEpoch = await page.evaluate((s) => {
      rosterBatch = {
        game_id: currentGame,
        team_id: rosterTeamId,
        source: "copy_previous_roster",
        seated: [],
        skipped: [{ player_id: "departing-player", name: s.skipped,
          reason: "player_inactive" }],
        deferred: [],
      };
      notifState = {
        notifications: [{ id: "departing-notification", title: s.notification,
          message: s.notification }],
        unread: 7,
      };
      contactForm = { recipient_ref: "official:departing", channel: "email",
        destination: s.destination, label: s.destination };
      tokenForm = { recipient_ref: "official:departing", provider: "fcm",
        token: s.token, label: s.token };
      modal = { type: "blocked", kind: "team", id: "departing-team",
        name: s.modal, error: s.modal };
      deliveryState = {
        contacts: [{ id: "departing-contact", destination: s.state }],
        overview: { marker: s.state },
        deviceTokens: [{ id: "departing-token", token: s.state }],
      };
      usersState = {
        accounts: [{ id: "departing-account", username: s.state }],
        sessions: [{ id: "departing-session", marker: s.state }],
      };
      guardianLinkForm = { guardian_user_id: s.state, player_id: s.state };
      notifPrefs = { marker: s.state };
      feedTokens = [{ id: "departing-feed", token: s.state }];
      officialAvailability = [{ id: "departing-window", marker: s.state }];
      availSummary = { marker: s.state };
      subCandidates = { marker: s.state };
      addableSubs = { marker: s.state };
      dashAvailability = { marker: s.state };
      dashSubQueue = { marker: s.state };
      rescheduleRequests = { marker: s.state };
      guardianHome = { marker: s.state };
      readinessCheck = { marker: s.state };
      leagueTeams = { departing: [{ id: "departing-team", name: s.state }] };
      permLeaguesByProgram = { departing: [{ id: "departing-league", name: s.state }] };
      allPermLeagues = [{ id: "departing-league", name: s.state }];
      teamPermLeague = { departing: { id: "departing-league", name: s.state } };
      seasonRegs = { departing: [{ id: s.state }] };
      seasonVenueAccess = { departing: [{ id: s.state }] };
      seasonVenueCandidates = { departing: [{ id: "departing-venue", name: s.state }] };
      leagueDivisions = { departing: [{ id: "departing-division", name: s.state }] };
      rollover = { programId: s.state, fromSeasonId: s.state,
        toSeasonId: s.state, result: { marker: s.state } };
      schedulerState.preview = { marker: s.state };
      schedulerState.drafts = [{ id: "departing-draft", marker: s.state }];
      schedulerState.summary = { marker: s.state };
      schedulerState.formatRefusal = { marker: s.state };
      wizard = { marker: s.state };
      drawer = null;
      drawerError = s.state;
      drawerValues = { player_email: `${s.state}@example.invalid` };
      movingGameId = "departing-game";
      conflict = { ok: false, title: s.state, lines: [s.state] };
      pendingMove = { gid: "departing-game", slotId: s.state,
        willUnpublish: false, willUnlock: false };
      pendingReassign = { kind: "team", parent: "league", id: "departing-team",
        name: s.state, curId: "departing-league", seasonId: "departing-season",
        programId: "departing-program" };
      homeCardPaintedHtml = `<p>${s.state}</p>`;
      hierarchyImportState.sheets.players_csv = `player_code,email\nP1,${s.state}@example.invalid`;
      hierarchyImportState.report = { ok: true, marker: s.state };
      hierarchyImportState.validatedKey = JSON.stringify(hierarchyImportState.sheets);
      hierarchyExistingCodes = { programs: [{ code: s.state }], leagues: [], venues: [] };
      onboardingStatus = { ready_to_schedule: false,
        steps: [{ key: "departing", label: s.state, status: "todo" }] };
      updateOnboardingBadge(onboardingStatus);
      overlayReturnFocus = document.getElementById("off-pick");
      overlayReturnSelector = "#off-pick";
      lastActivatedTrigger = document.getElementById("off-pick");
      document.getElementById("login-user").value = "departing-admin";
      document.getElementById("login-pass").value = s.password;
      const contextConfirm = document.getElementById("ctx-confirm");
      contextConfirm.dataset.ctxProgram = "departing-program";
      contextConfirm.dataset.ctxSeason = "departing-season";
      contextConfirm.dataset.ctxProgramName = s.state;
      contextConfirm.setAttribute("aria-label", `Select ${s.state} as active Program`);
      updateNotifBadge();
      return uiIdentityEpoch;
    }, identitySentinels);
    const departingShell = await page.evaluate(() => ({
      context: (document.getElementById("context-switcher").textContent || "").trim(),
      breadcrumb: (document.getElementById("breadcrumb").textContent || "").trim(),
      badge: (document.getElementById("notif-badge").textContent || "").trim(),
      user: (document.getElementById("user-name").textContent || "").trim(),
      confirm: (() => {
        const el = document.getElementById("ctx-confirm");
        return { program: el.dataset.ctxProgram, season: el.dataset.ctxSeason,
          name: el.dataset.ctxProgramName, aria: el.getAttribute("aria-label") };
      })(),
    }));
    if (!departingShell.context.includes(seasonName)
        || !departingShell.context.includes(`Matrix League ${suffix}`)
        || !departingShell.breadcrumb.includes(`Matrix Program ${suffix}`)
        || !departingShell.breadcrumb.includes(seasonName)
        || departingShell.confirm.program !== "departing-program"
        || departingShell.confirm.season !== "departing-season"
        || departingShell.confirm.name !== identitySentinels.state
        || !departingShell.confirm.aria.includes(identitySentinels.state)
        || departingShell.badge !== "7"
        || departingShell.user !== "admin") {
      fail(`setup precondition: the departing shell was not non-vacuously `
        + `painted with private context/identity/badge state: `
        + `${JSON.stringify(departingShell)}`);
    }

    // First hold the arriving identity's context read WITHOUT starting
    // another admin render. This leaves the fully-painted Game Sheet as the
    // departing DOM and proves the identity boundary removes its private
    // picker synchronously, before signIn() reaches its first await.
    let releasePaintedViewerContext;
    let markPaintedViewerContextHeld;
    const paintedViewerContextRelease = new Promise((resolve) => {
      releasePaintedViewerContext = resolve;
    });
    const paintedViewerContextHeld = new Promise((resolve) => {
      markPaintedViewerContextHeld = resolve;
    });
    const holdPaintedViewerContext = async (route) => {
      const response = await route.fetch();
      markPaintedViewerContextHeld();
      await paintedViewerContextRelease;
      await route.fulfill({ response });
    };
    await page.route("**/api/context/options", holdPaintedViewerContext);
    await page.evaluate(([u, p]) => {
      window.__officialPrivacyPaintedSwitch = signIn(u, p);
    }, [accounts.viewer.username, PW]);
    await paintedViewerContextHeld;
    const paintedBoundary = await page.evaluate((privateOfficial) => {
      const content = document.getElementById("content");
      const options = Array.from(document.querySelectorAll("#off-pick option"));
      return {
        role: currentRole,
        username: currentUser && currentUser.username,
        officials: officialsPool.map((entry) => ({ id: entry.id, name: entry.name })),
        hasOfficialPicker: !!document.getElementById("off-pick"),
        hasAssignControl: !!document.querySelector(".gs-assign"),
        hasPrivateOfficialOption: options.some((option) =>
          option.value === privateOfficial.id
          || (option.textContent || "").includes(privateOfficial.name)),
        hasPrivateOfficialText: !!content
          && (content.textContent || "").includes(privateOfficial.name),
        rosterBatch,
        notifications: notifState.notifications,
        unread: notifState.unread,
        contactForm,
        tokenForm,
        modal,
        epoch: uiIdentityEpoch,
        contextHidden: document.getElementById("context-switcher").hidden,
        contextText: (document.getElementById("context-switcher").textContent || "").trim(),
        contextConfirm: (() => {
          const confirm = document.getElementById("ctx-confirm");
          return {
            program: confirm.dataset.ctxProgram,
            season: confirm.dataset.ctxSeason,
            programName: confirm.dataset.ctxProgramName,
            ariaLabel: confirm.getAttribute("aria-label"),
          };
        })(),
        breadcrumb: (document.getElementById("breadcrumb").textContent || "").trim(),
        badgeText: (document.getElementById("notif-badge").textContent || "").trim(),
        badgeDisplay: document.getElementById("notif-badge").style.display,
        userName: (document.getElementById("user-name").textContent || "").trim(),
        userRole: (document.getElementById("user-role").textContent || "").trim(),
        scopeChip: (document.getElementById("scope-chip").textContent || "").trim(),
        contextOptions,
        contextEpoch,
        retainedIdentityState: JSON.stringify({
          deliveryState, usersState, guardianLinkForm, notifPrefs, feedTokens,
          officialAvailability, availSummary, subCandidates, addableSubs,
          dashAvailability, dashSubQueue, rescheduleRequests, guardianHome,
          readinessCheck, leagueTeams, permLeaguesByProgram, allPermLeagues,
          teamPermLeague, seasonRegs, seasonVenueAccess, seasonVenueCandidates,
          leagueDivisions, rollover, schedulerState, wizard,
          movingGameId, conflict, pendingMove, pendingReassign,
          drawer, drawerError, drawerValues,
          homeCardPaintedHtml, hierarchyImportState, hierarchyExistingCodes,
          onboardingStatus,
        }),
        onboardingBadge: (document.getElementById("onboarding-badge").textContent || "").trim(),
        onboardingBadgeHidden: document.getElementById("onboarding-badge").hidden,
        overlayReturnFocus,
        overlayReturnSelector,
        lastActivatedTrigger,
        loginUsername: document.getElementById("login-user").value,
        loginPassword: document.getElementById("login-pass").value,
      };
    }, privateOfficial);
    if (paintedBoundary.role !== "viewer"
        || paintedBoundary.username !== accounts.viewer.username
        || paintedBoundary.officials.length !== 0
        || paintedBoundary.hasOfficialPicker
        || paintedBoundary.hasAssignControl
        || paintedBoundary.hasPrivateOfficialOption
        || paintedBoundary.hasPrivateOfficialText
        || paintedBoundary.rosterBatch !== null
        || paintedBoundary.notifications.length !== 0
        || paintedBoundary.unread !== 0
        || JSON.stringify(paintedBoundary.contactForm) !== JSON.stringify({
          recipient_ref: "", channel: "email", destination: "", label: "",
        })
        || JSON.stringify(paintedBoundary.tokenForm) !== JSON.stringify({
          recipient_ref: "", provider: "fcm", token: "", label: "",
        })
        || paintedBoundary.modal !== null
        || paintedBoundary.epoch <= departingEpoch
        || !paintedBoundary.contextHidden
        || paintedBoundary.contextText.includes(`Matrix Program ${suffix}`)
        || paintedBoundary.contextText.includes(seasonName)
        || paintedBoundary.contextText.includes(`Matrix League ${suffix}`)
        || paintedBoundary.contextConfirm.program !== undefined
        || paintedBoundary.contextConfirm.season !== undefined
        || paintedBoundary.contextConfirm.programName !== undefined
        || paintedBoundary.contextConfirm.ariaLabel !== null
        || paintedBoundary.breadcrumb !== ""
        || paintedBoundary.badgeText !== ""
        || paintedBoundary.badgeDisplay !== "none"
        || paintedBoundary.userName !== accounts.viewer.username
        || !/viewer/i.test(paintedBoundary.userRole)
        || paintedBoundary.scopeChip !== ""
        || paintedBoundary.contextOptions !== null
        || paintedBoundary.contextEpoch !== null
        || paintedBoundary.retainedIdentityState.includes(identitySentinels.state)
        || paintedBoundary.onboardingBadge !== ""
        || !paintedBoundary.onboardingBadgeHidden
        || paintedBoundary.overlayReturnFocus !== null
        || paintedBoundary.overlayReturnSelector !== null
        || paintedBoundary.lastActivatedTrigger !== null
        || paintedBoundary.loginUsername !== ""
        || paintedBoundary.loginPassword !== "") {
      fail(`identity boundary did not synchronously remove the private Official `
        + `state, transient operator stores, and painted controls before `
        + `Viewer context reconciliation (sentinels ${JSON.stringify(identitySentinels)}): `
        + `${JSON.stringify(paintedBoundary)}`);
    }
    // The feed intentionally retains its previous value on an API error, so
    // force the arriving Viewer down that branch. A missing reset would now
    // repaint the departing Admin's unread count even though every successful
    // response path looks correct.
    const failViewerNotifications = async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ error: {
          code: "forced_notification_failure",
          message: "Forced notification failure for identity-boundary test",
        } }),
      });
    };
    await page.route("**/api/notifications", failViewerNotifications);
    releasePaintedViewerContext();
    await page.evaluate(async () => {
      await window.__officialPrivacyPaintedSwitch;
      delete window.__officialPrivacyPaintedSwitch;
    });
    await page.unroute("**/api/context/options", holdPaintedViewerContext);
    await waitForView(page, "standings");
    await waitForRealContent(page);
    const failedNotificationBoundary = await page.evaluate((sentinel) => ({
      notifications: notifState.notifications,
      unread: notifState.unread,
      badge: (document.getElementById("notif-badge").textContent || "").trim(),
      badgeDisplay: document.getElementById("notif-badge").style.display,
      hasSentinel: (document.documentElement.textContent || "").includes(sentinel),
    }), identitySentinels.notification);
    await page.unroute("**/api/notifications", failViewerNotifications);
    if (failedNotificationBoundary.notifications.length !== 0
        || failedNotificationBoundary.unread !== 0
        || failedNotificationBoundary.badge !== ""
        || failedNotificationBoundary.badgeDisplay !== "none"
        || failedNotificationBoundary.hasSentinel) {
      fail(`Viewer notification failure retained or repainted the departing `
        + `Admin feed: ${JSON.stringify(failedNotificationBoundary)}`);
    }

    // Return to a fully-authorized, fully-painted Game Sheet for the separate
    // stale-response serialization below.
    await page.evaluate(async ([u, p]) => { await signIn(u, p); }, ["admin", "demo"]);
    await page.evaluate(() => switchTab("sheet"));
    await waitForView(page, "sheet");
    await waitForRealContent(page);
    await page.waitForFunction(() => officialsPool.length > 0, null,
      { timeout: 10000 });

    // Header sign-out and the successful factory-reset exit both call
    // setUser(null) directly; neither reaches signIn()'s later calendar
    // cleanup. Seed the four pending calendar/setup intents with private text
    // and cross that exact boundary synchronously so the regression cannot be
    // satisfied by signIn() cleaning them up a few statements later.
    const directSignOutBoundary = await page.evaluate((sentinel) => {
      movingGameId = "departing-direct-game";
      conflict = { ok: false, title: sentinel, lines: [sentinel] };
      pendingMove = { gid: "departing-direct-game", slotId: sentinel,
        willUnpublish: true, willUnlock: true };
      pendingReassign = { kind: "team", parent: "league", id: "departing-team",
        name: sentinel, curId: "departing-league", seasonId: "departing-season",
        programId: "departing-program" };
      setUser(null);
      return { movingGameId, conflict, pendingMove, pendingReassign,
        retained: JSON.stringify({ movingGameId, conflict, pendingMove, pendingReassign }) };
    }, identitySentinels.state);
    if (directSignOutBoundary.movingGameId !== null
        || directSignOutBoundary.conflict !== null
        || directSignOutBoundary.pendingMove !== null
        || directSignOutBoundary.pendingReassign !== null
        || directSignOutBoundary.retained.includes(identitySentinels.state)) {
      fail(`direct setUser(null) retained private calendar/setup intent state: `
        + `${JSON.stringify(directSignOutBoundary)}`);
    }
    const afterDirectSignOut = await page.evaluate(async ([u, p]) => signIn(u, p),
      ["admin", "demo"]);
    if (!afterDirectSignOut) fail("admin re-sign-in after direct setUser(null) failed");
    await page.evaluate(() => switchTab("sheet"));
    await waitForView(page, "sheet");
    await waitForRealContent(page);

    // Factory-reset preview has a classic ABA shape: an Admin's old preview
    // can be held, identity can change away and back to the same Admin, and a
    // fresh preview can win first. Principal equality alone cannot distinguish
    // those requests; the monotone identity epoch must keep the first response
    // from replacing the second modal after the round trip.
    let releaseDepartingFactoryPreview;
    let markDepartingFactoryPreviewHeld;
    const departingFactoryPreviewRelease = new Promise((resolve) => {
      releaseDepartingFactoryPreview = resolve;
    });
    const departingFactoryPreviewHeld = new Promise((resolve) => {
      markDepartingFactoryPreviewHeld = resolve;
    });
    let factoryPreviewRequests = 0;
    const holdDepartingFactoryPreview = async (route) => {
      factoryPreviewRequests += 1;
      if (factoryPreviewRequests === 1) {
        markDepartingFactoryPreviewHeld();
        await departingFactoryPreviewRelease;
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ counts: { departing_only_rows: 31 },
            challenge_token: "departing-token" }),
        });
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ counts: { arriving_only_rows: 47 },
          challenge_token: "arriving-token" }),
      });
    };
    await page.route("**/api/admin/factory-reset/preview", holdDepartingFactoryPreview);
    await page.evaluate(() => {
      window.__departingFactoryPreview = startFactoryReset()
        .then(() => ({ ok: true }))
        .catch((error) => ({ error: error && error.name }));
    });
    await departingFactoryPreviewHeld;
    const factoryViewerSwitch = await page.evaluate(async ([u, p]) => signIn(u, p),
      [accounts.viewer.username, PW]);
    if (!factoryViewerSwitch) fail("factory-preview Admin -> Viewer sign-in failed");
    const factoryAdminReturn = await page.evaluate(async ([u, p]) => signIn(u, p),
      ["admin", "demo"]);
    if (!factoryAdminReturn) fail("factory-preview Viewer -> Admin sign-in failed");
    await page.evaluate(() => startFactoryReset());
    await page.waitForFunction(() => modal && modal.type === "factory-reset"
      && modal.step === "confirm" && modal.token === "arriving-token", null,
      { timeout: 10000 });
    await page.waitForSelector("[data-fr-counts]", { timeout: 10000 });
    const arrivingFactoryPreview = await page.evaluate(() => ({
      modal: { step: modal.step, token: modal.token, counts: modal.counts },
      text: (document.querySelector("[data-fr-counts]")?.textContent || "")
        .replace(/\s+/g, " ").trim(),
    }));
    releaseDepartingFactoryPreview();
    const departingFactoryOutcome = await page.evaluate(async () => {
      const outcome = await window.__departingFactoryPreview;
      delete window.__departingFactoryPreview;
      return outcome;
    });
    await page.waitForFunction(() => modal && modal.token === "arriving-token", null,
      { timeout: 10000 });
    const finalFactoryPreview = await page.evaluate(() => ({
      modal: { step: modal.step, token: modal.token, counts: modal.counts },
      text: (document.querySelector("[data-fr-counts]")?.textContent || "")
        .replace(/\s+/g, " ").trim(),
    }));
    await page.unroute("**/api/admin/factory-reset/preview", holdDepartingFactoryPreview);
    if (factoryPreviewRequests !== 2
        || arrivingFactoryPreview.modal.token !== "arriving-token"
        || arrivingFactoryPreview.modal.counts.arriving_only_rows !== 47
        || !arrivingFactoryPreview.text.includes("arriving_only_rows")
        || !arrivingFactoryPreview.text.includes("47")
        || finalFactoryPreview.modal.token !== "arriving-token"
        || finalFactoryPreview.modal.counts.arriving_only_rows !== 47
        || finalFactoryPreview.modal.counts.departing_only_rows != null
        || finalFactoryPreview.text.includes("departing_only_rows")
        || finalFactoryPreview.text.includes("31")) {
      fail(`held factory-reset preview crossed an Admin -> Viewer -> Admin ABA `
        + `identity boundary (old outcome ${JSON.stringify(departingFactoryOutcome)}): `
        + `${JSON.stringify({ arrivingFactoryPreview, finalFactoryPreview,
          factoryPreviewRequests })}`);
    }

    // Auth writes are the one class that cannot use the generic POST epoch
    // cancellation: each response itself sets the cookie identity. Pin the
    // stronger contract instead — transitions are serialized, so a second
    // persona login is not even dispatched while the first can still set a
    // cookie, and the final cookie/UI/render all describe the last intent.
    let releaseFirstAuthLogin;
    let markFirstAuthLoginHeld;
    const firstAuthLoginRelease = new Promise((resolve) => {
      releaseFirstAuthLogin = resolve;
    });
    const firstAuthLoginHeld = new Promise((resolve) => {
      markFirstAuthLoginHeld = resolve;
    });
    const authLoginUsers = [];
    const serializeAuthLogins = async (route) => {
      const body = route.request().postDataJSON();
      authLoginUsers.push(body.username);
      if (authLoginUsers.length === 1) {
        markFirstAuthLoginHeld();
        await firstAuthLoginRelease;
      }
      const response = await route.fetch();
      await route.fulfill({ response });
    };
    await page.route("**/api/auth/login", serializeAuthLogins);
    await page.evaluate(([viewer, officialUser, password]) => {
      window.__serializedAuthFirst = signIn(viewer, password)
        .catch((error) => ({ error: error && error.name }));
      window.__serializedAuthSecond = signIn(officialUser, password)
        .catch((error) => ({ error: error && error.name }));
    }, [accounts.viewer.username, accounts.assigned_official.username, PW]);
    await firstAuthLoginHeld;
    await page.waitForTimeout(100);
    if (JSON.stringify(authLoginUsers) !== JSON.stringify([accounts.viewer.username])) {
      fail(`competing persona login escaped auth serialization before the `
        + `first response settled: ${JSON.stringify(authLoginUsers)}`);
    }
    releaseFirstAuthLogin();
    const serializedAuthOutcomes = await page.evaluate(async () => {
      const outcomes = await Promise.all([
        window.__serializedAuthFirst, window.__serializedAuthSecond,
      ]);
      delete window.__serializedAuthFirst;
      delete window.__serializedAuthSecond;
      return outcomes;
    });
    await page.unroute("**/api/auth/login", serializeAuthLogins);
    await page.evaluate(() => switchTab("inbox"));
    await waitForView(page, "inbox");
    await waitForRealContent(page);
    const serializedAuthBoundary = await page.evaluate(async () => {
      const me = await (await fetch("/api/auth/me", { credentials: "same-origin" })).json();
      return {
        me: me.user && { username: me.user.username, role: me.user.role },
        current: currentUser && { username: currentUser.username, role: currentUser.role },
        currentRole,
        sidebar: (document.getElementById("user-name").textContent || "").trim(),
        usersPerm: hasPerm("manage_users"),
        usersTabHidden: document.querySelector('.tab[data-tab="users"]').style.display === "none",
        view,
      };
    });
    if (JSON.stringify(authLoginUsers) !== JSON.stringify([
      accounts.viewer.username, accounts.assigned_official.username,
    ])
        || serializedAuthBoundary.me.username !== accounts.assigned_official.username
        || serializedAuthBoundary.current.username !== accounts.assigned_official.username
        || serializedAuthBoundary.sidebar !== accounts.assigned_official.username
        || serializedAuthBoundary.currentRole !== "official"
        || serializedAuthBoundary.usersPerm
        || !serializedAuthBoundary.usersTabHidden
        || serializedAuthBoundary.view !== "inbox") {
      fail(`serialized persona logins left cookie, currentUser, permissions, or `
        + `rendered identity inconsistent (outcomes `
        + `${JSON.stringify(serializedAuthOutcomes)}): `
        + `${JSON.stringify({ authLoginUsers, serializedAuthBoundary })}`);
    }

    // The same queue owns logout. Hold the server logout and immediately ask
    // for an Admin login: that login must remain undispatched until logout has
    // set the anonymous cookie and completed its local setUser(null), after
    // which Admin must win consistently at every observable layer.
    let releaseQueuedLogout;
    let markQueuedLogoutHeld;
    const queuedLogoutRelease = new Promise((resolve) => {
      releaseQueuedLogout = resolve;
    });
    const queuedLogoutHeld = new Promise((resolve) => {
      markQueuedLogoutHeld = resolve;
    });
    let queuedLogoutRequests = 0;
    const queuedLoginUsers = [];
    const holdQueuedLogout = async (route) => {
      queuedLogoutRequests += 1;
      markQueuedLogoutHeld();
      await queuedLogoutRelease;
      const response = await route.fetch();
      await route.fulfill({ response });
    };
    const observeQueuedLogin = async (route) => {
      queuedLoginUsers.push(route.request().postDataJSON().username);
      const response = await route.fetch();
      await route.fulfill({ response });
    };
    await page.route("**/api/auth/logout", holdQueuedLogout);
    await page.route("**/api/auth/login", observeQueuedLogin);
    await page.evaluate(() => {
      window.__queuedLogout = document.getElementById("signout-btn").onclick()
        .catch((error) => ({ error: error && error.name }));
      window.__queuedLoginAfterLogout = signIn("admin", "demo")
        .catch((error) => ({ error: error && error.name }));
    });
    await queuedLogoutHeld;
    await page.waitForTimeout(100);
    if (queuedLoginUsers.length !== 0) {
      fail(`login was dispatched while the prior logout could still change `
        + `the session cookie: ${JSON.stringify(queuedLoginUsers)}`);
    }
    releaseQueuedLogout();
    const queuedLogoutOutcomes = await page.evaluate(async () => {
      const outcomes = await Promise.all([
        window.__queuedLogout, window.__queuedLoginAfterLogout,
      ]);
      delete window.__queuedLogout;
      delete window.__queuedLoginAfterLogout;
      return outcomes;
    });
    await page.unroute("**/api/auth/logout", holdQueuedLogout);
    await page.unroute("**/api/auth/login", observeQueuedLogin);
    const queuedLogoutBoundary = await page.evaluate(async () => {
      const me = await (await fetch("/api/auth/me", { credentials: "same-origin" })).json();
      return {
        me: me.user && { username: me.user.username, role: me.user.role },
        current: currentUser && { username: currentUser.username, role: currentUser.role },
        currentRole,
        sidebar: (document.getElementById("user-name").textContent || "").trim(),
        usersPerm: hasPerm("manage_users"),
        usersTabHidden: document.querySelector('.tab[data-tab="users"]').style.display === "none",
      };
    });
    if (queuedLogoutRequests !== 1
        || JSON.stringify(queuedLoginUsers) !== JSON.stringify(["admin"])
        || queuedLogoutBoundary.me.username !== "admin"
        || queuedLogoutBoundary.current.username !== "admin"
        || queuedLogoutBoundary.sidebar !== "admin"
        || queuedLogoutBoundary.currentRole !== "league_admin"
        || !queuedLogoutBoundary.usersPerm
        || queuedLogoutBoundary.usersTabHidden) {
      fail(`logout/login serialization left cookie, currentUser, permissions, `
        + `or sidebar inconsistent (outcomes ${JSON.stringify(queuedLogoutOutcomes)}): `
        + `${JSON.stringify({ queuedLogoutRequests, queuedLoginUsers,
          queuedLogoutBoundary })}`);
    }

    // A context-epoch resync is also identity-owned. Reproduce the three-way
    // ownership race: hold Admin resync A; switch to Viewer (allowing its
    // ordinary options load through); start and hold Viewer resync B; then
    // release A. A's finally must not clear B's flight token and admit a third
    // Viewer resync C while B is still outstanding.
    const resyncReleases = [];
    const resyncHeldResolvers = [];
    const resyncHeld = [0, 1, 2].map((index) => new Promise((resolve) => {
      resyncHeldResolvers[index] = resolve;
    }));
    [0, 1, 2].forEach((index) => {
      resyncReleases[index] = null;
    });
    const resyncReleasePromises = [0, 1, 2].map((index) => new Promise((resolve) => {
      resyncReleases[index] = resolve;
    }));
    let resyncOptionRequests = 0;
    const holdResyncOptions = async (route) => {
      resyncOptionRequests += 1;
      const requestIndex = resyncOptionRequests - 1;
      const response = await route.fetch();
      if (requestIndex < 3) {
        resyncHeldResolvers[requestIndex]();
        await resyncReleasePromises[requestIndex];
      }
      await route.fulfill({ response });
    };
    await page.route("**/api/context/options", holdResyncOptions);
    await page.evaluate(() => requestContextEpochResync());
    await resyncHeld[0];
    await page.evaluate(([u, p]) => {
      window.__resyncViewerSwitch = signIn(u, p);
    }, [accounts.viewer.username, PW]);
    await resyncHeld[1];
    resyncReleases[1]();
    const resyncViewerSwitch = await page.evaluate(async () => {
      const result = await window.__resyncViewerSwitch;
      delete window.__resyncViewerSwitch;
      return result;
    });
    if (!resyncViewerSwitch) fail("context-resync Admin -> Viewer sign-in failed");
    const revisionBeforeViewerResync = await page.evaluate(() => contextRevision);
    await page.evaluate(() => requestContextEpochResync());
    await resyncHeld[2];
    resyncReleases[0]();
    await page.waitForTimeout(100);
    await page.evaluate(() => requestContextEpochResync());
    await page.waitForTimeout(100);
    const heldViewerResyncBoundary = await page.evaluate(() => ({
      inFlight: !!contextEpochResyncInFlight,
      revision: contextRevision,
    }));
    if (resyncOptionRequests !== 3 || !heldViewerResyncBoundary.inFlight
        || heldViewerResyncBoundary.revision !== revisionBeforeViewerResync) {
      fail(`departing context resync cleared or superseded the arriving `
        + `identity's owned resync: ${JSON.stringify({ resyncOptionRequests,
          revisionBeforeViewerResync, heldViewerResyncBoundary })}`);
    }
    resyncReleases[2]();
    try {
      await page.waitForFunction(() => !contextEpochResyncInFlight, null,
        { timeout: 10000 });
    } catch (_) {
      const stuckResync = await page.evaluate(() => ({
        inFlight: contextEpochResyncInFlight,
        identityEpoch: uiIdentityEpoch,
        optionsLoadSeq: contextOptionsLoadSeq,
        revision: contextRevision,
      }));
      fail(`Viewer context resync did not settle after its held options `
        + `response was released: ${JSON.stringify({ resyncOptionRequests,
          stuckResync })}`);
    }
    const completedViewerResync = await page.evaluate(() => ({
      revision: contextRevision,
      role: currentRole,
      username: currentUser && currentUser.username,
    }));
    await page.unroute("**/api/context/options", holdResyncOptions);
    if (resyncOptionRequests !== 3
        || completedViewerResync.revision !== revisionBeforeViewerResync + 1
        || completedViewerResync.role !== "viewer"
        || completedViewerResync.username !== accounts.viewer.username) {
      fail(`owned Viewer context resync did not converge exactly once after `
        + `the departing Admin resync settled: ${JSON.stringify({
          resyncOptionRequests, revisionBeforeViewerResync,
          completedViewerResync })}`);
    }
    const afterResyncAdmin = await page.evaluate(async ([u, p]) => signIn(u, p),
      ["admin", "demo"]);
    if (!afterResyncAdmin) fail("admin restore after context-resync ownership test failed");
    await page.evaluate(() => { modal = null; switchTab("sheet"); });
    await waitForView(page, "sheet");
    await waitForRealContent(page);
    try {
      await page.waitForFunction(() => officialsPool.length > 0, null,
        { timeout: 10000 });
    } catch (_) {
      const missingOfficials = await page.evaluate(() => ({
        role: currentRole,
        user: currentUser && currentUser.username,
        view,
        officials: officialsPool,
        content: (document.getElementById("content").textContent || "")
          .replace(/\s+/g, " ").trim().slice(0, 500),
        contextOptions,
      }));
      fail(`Admin restore after auth/context race did not repopulate the `
        + `Official directory: ${JSON.stringify(missingOfficials)}`);
    }

    // A simple post-switch assertion only proves a pool that has ALREADY
    // landed is cleared. Exercise the harder serialization: a privileged
    // render gets a real 200 directory response, browser delivery is held,
    // setUser(Viewer) clears the pool, and Viewer context reconciliation is
    // then held before its first render. Releasing the old response in that
    // exact window must not let the departing render write the private
    // directory back.
    let releaseStaleOfficial;
    let markStaleOfficialHeld;
    let markStaleOfficialDelivered;
    const staleOfficialRelease = new Promise((resolve) => {
      releaseStaleOfficial = resolve;
    });
    const staleOfficialHeld = new Promise((resolve) => {
      markStaleOfficialHeld = resolve;
    });
    const staleOfficialDelivered = new Promise((resolve) => {
      markStaleOfficialDelivered = resolve;
    });
    let heldOfficialBody = null;
    let officialDirectoryRequests = 0;
    const holdStaleOfficial = async (route) => {
      const request = route.request();
      const requestPath = new URL(request.url()).pathname;
      if (request.method() !== "GET" || requestPath !== "/api/officials") {
        await route.continue();
        return;
      }
      officialDirectoryRequests += 1;
      const response = await route.fetch();
      heldOfficialBody = await response.json();
      markStaleOfficialHeld();
      await staleOfficialRelease;
      await route.fulfill({ response, json: heldOfficialBody });
      markStaleOfficialDelivered();
    };

    let releaseViewerContext;
    let markViewerContextHeld;
    const viewerContextRelease = new Promise((resolve) => {
      releaseViewerContext = resolve;
    });
    const viewerContextHeld = new Promise((resolve) => {
      markViewerContextHeld = resolve;
    });
    const holdViewerContext = async (route) => {
      const response = await route.fetch();
      markViewerContextHeld();
      await viewerContextRelease;
      await route.fulfill({ response });
    };

    await page.route("**/api/officials", holdStaleOfficial);
    await page.evaluate(() => {
      window.__officialPrivacyStaleRender = render();
    });
    await staleOfficialHeld;
    if (!heldOfficialBody || !Array.isArray(heldOfficialBody.officials)
        || !heldOfficialBody.officials.some((entry) =>
          entry.id === privateOfficial.id && entry.name === privateOfficial.name)) {
      fail(`held response was not a non-vacuous private Official directory: `
        + `${JSON.stringify(heldOfficialBody)}`);
    }

    await page.route("**/api/context/options", holdViewerContext);
    await page.evaluate(([u, p]) => {
      window.__officialPrivacySwitch = signIn(u, p);
    }, [accounts.viewer.username, PW]);
    await viewerContextHeld;
    const afterViewerReset = await page.evaluate((privateOfficial) => {
      const content = document.getElementById("content");
      const options = Array.from(document.querySelectorAll("#off-pick option"));
      return {
        role: currentRole,
        username: currentUser && currentUser.username,
        officials: officialsPool.map((entry) => ({ id: entry.id, name: entry.name })),
        hasOfficialPicker: !!document.getElementById("off-pick"),
        hasAssignControl: !!document.querySelector(".gs-assign"),
        hasPrivateOfficialOption: options.some((option) =>
          option.value === privateOfficial.id
          || (option.textContent || "").includes(privateOfficial.name)),
        hasPrivateOfficialText: !!content
          && (content.textContent || "").includes(privateOfficial.name),
      };
    }, privateOfficial);
    if (afterViewerReset.role !== "viewer"
        || afterViewerReset.username !== accounts.viewer.username
        || afterViewerReset.officials.length !== 0
        || afterViewerReset.hasOfficialPicker
        || afterViewerReset.hasAssignControl
        || afterViewerReset.hasPrivateOfficialOption
        || afterViewerReset.hasPrivateOfficialText) {
      fail(`identity boundary did not synchronously remove the private Official `
        + `state and painted controls before Viewer context reconciliation: `
        + `${JSON.stringify(afterViewerReset)}`);
    }

    releaseStaleOfficial();
    await staleOfficialDelivered;
    // Await the exact old render promise rather than a timer/frame proxy: the
    // assertion below must run only after that pass either observes the new
    // identity token and returns or attempts its stale module-state commit.
    await page.evaluate(async () => {
      await window.__officialPrivacyStaleRender;
      delete window.__officialPrivacyStaleRender;
    });
    const afterStaleDelivery = await page.evaluate(() => ({
      role: currentRole,
      officials: officialsPool.map((entry) => ({ id: entry.id, name: entry.name })),
    }));
    if (afterStaleDelivery.role !== "viewer"
        || afterStaleDelivery.officials.length !== 0) {
      fail(`departing League Admin render repopulated the private Official `
        + `directory under Viewer: ${JSON.stringify(afterStaleDelivery)}`);
    }

    releaseViewerContext();
    await page.evaluate(async () => {
      await window.__officialPrivacySwitch;
      delete window.__officialPrivacySwitch;
    });
    await page.unroute("**/api/context/options", holdViewerContext);
    await page.unroute("**/api/officials", holdStaleOfficial);
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
      officialsPoolSize: officialsPool.length,
    }));
    if (postSwitchDom.hasSetupCard || postSwitchDom.hasUsersTab
        || !postSwitchDom.demoMenuHidden || postSwitchDom.officialsPoolSize !== 0
        || officialDirectoryRequests !== 1) {
      fail(`no-reload League Admin -> Viewer switch retained admin UI or `
        + `requested the global directory as Viewer: state=${JSON.stringify(postSwitchDom)} `
        + `directoryRequests=${officialDirectoryRequests}`);
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
    // The Official directory follows MANAGE_SCHEDULE, not MANAGE_SETUP:
    // Arena Manager is the load-bearing role that separates those two
    // permissions.  Drive a real-session Game Sheet render at both viewports
    // and require the actual pool plus its assign control, so narrowing the
    // client gate to League Admin cannot pass behind the server's correct
    // role matrix.
    await page.evaluate(() => switchTab("sheet"));
    await waitForView(page, "sheet");
    await waitForRealContent(page);
    await page.waitForSelector(".game-sheet .gs-grid", { timeout: 10000 });
    const amDirectory = await apiGet(page, "/api/officials");
    const amSheet = await page.evaluate(() => ({
      poolIds: officialsPool.map((official) => official.id).sort(),
      assignControls: document.querySelectorAll(".gs-assign").length,
      officialSlots: document.querySelectorAll(".gs-off-slot").length,
    }));
    const amExpectedPoolIds = ((amDirectory.body || {}).officials || [])
      .map((official) => official.id).sort();
    if (amDirectory.status !== 200 || !amExpectedPoolIds.length
        || JSON.stringify(amSheet.poolIds) !== JSON.stringify(amExpectedPoolIds)) {
      fail(`Arena Manager [${L}]: Game Sheet did not load the exact `
        + `MANAGE_SCHEDULE Official pool: response=${JSON.stringify(amDirectory)} `
        + `sheet=${JSON.stringify(amSheet)}`);
    }
    if (amSheet.assignControls !== 1 || amSheet.officialSlots < 1) {
      fail(`Arena Manager [${L}]: the authorized Official assign surface is `
        + `missing or vacuous: ${JSON.stringify(amSheet)}`);
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
    await selectProgramSeason(page, "Arena Manager: fixture context",
      program.body.id, season.id);
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
    try {
      await page.waitForSelector("[data-ib-commit]:not([disabled])", { timeout: 10000 });
    } catch (_) {
      const icePreviewFailure = await page.evaluate(() => ({
        toast,
        toastIsError,
        identity: currentUser && { username: currentUser.username, role: currentUser.role },
        contextOptions,
        contextEpoch,
        iceBuilder,
        text: (document.querySelector(".ib-wrap")?.textContent || "")
          .replace(/\s+/g, " ").trim(),
      }));
      fail(`Arena Manager ice preview did not enable commit: `
        + `${JSON.stringify(icePreviewFailure)}`);
    }
    const iceCommitResponse = page.waitForResponse((response) =>
      response.url().endsWith("/api/setup/ice-availability/commit")
        && response.request().method() === "POST", { timeout: 10000 });
    await tabToAndActivate(page, "[data-ib-commit]", "Arena Manager commit ice");
    const committedIce = await iceCommitResponse;
    const committedIceBody = await committedIce.json().catch(() => null);
    if (committedIce.status() !== 200 || !committedIceBody
        || committedIceBody.error || !(committedIceBody.totals.created > 0)) {
      fail(`Arena Manager recurring-ice commit did not create slots: `
        + `${committedIce.status()} ${JSON.stringify(committedIceBody)}`);
    }
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
    // ------------------------------------------------------------
    // THE COACH'S OWN SIDE IS NAMED IN THE SCHEDULE ROW (#205, round 4) --
    // the positive half of the withheld-render leg the assigned official
    // runs below, and it is here so that leg cannot pass because the field
    // is simply broken for everybody. This Coach IS in this game, so they
    // are entitled to a value, the value is THEIR side's, and the screen
    // renders a real checklist state rather than the withheld marker.
    // ------------------------------------------------------------
    const coachOverview = await apiGet(page, "/api/demo/overview");
    if (coachOverview.status !== 200 || coachOverview.body.error) {
      fail(`Coach [${L}]: /api/demo/overview failed: `
        + `${JSON.stringify(coachOverview).slice(0, 200)}`);
    }
    const coachRow = (coachOverview.body.schedule || [])
      .find((g) => g.game_id === game.body.id);
    if (!coachRow) {
      fail(`Coach [${L}]: the fixture game is absent from this Coach's `
        + `schedule, so nothing here is being asserted`);
    }
    if (coachRow.roster_status_restricted !== false
        || coachRow.roster_status_team_id !== coachTeam.body.id
        || typeof coachRow.roster_status !== "string") {
      fail(`Coach [${L}]: the schedule row must carry THIS Coach's own side's `
        + `roster status (team ${coachTeam.body.id}), got `
        + `${JSON.stringify(coachRow)}`);
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
    // A rosterBatch contains real candidate names and survives re-renders by
    // design. Switch, without reloading, to an assigned Official who may read
    // this same game's submitted lineup but may not inherit the Coach's batch
    // outcome. Keeping the game/team axes equal makes this non-vacuous: the
    // renderer's ordinary game/side guard would otherwise hide the stale row.
    const officialSwitch = await page.evaluate(async ([u, p]) => signIn(u, p),
      [accounts.assigned_official.username, PW]);
    if (!officialSwitch) fail("Coach -> assigned Official no-reload sign-in failed");
    await waitForView(page, "roster");
    await waitForRealContent(page);
    const officialRosterBoundary = await page.evaluate((skippedName) => ({
      role: currentRole,
      username: currentUser && currentUser.username,
      game: currentGame,
      team: rosterTeamId,
      batch: rosterBatch,
      hasPartial: !!document.querySelector(".ros-partial"),
      hasSkippedName: (document.getElementById("content").textContent || "")
        .includes(skippedName),
    }), `Copy Skipped ${suffix}`);
    if (officialRosterBoundary.role !== "official"
        || officialRosterBoundary.username !== accounts.assigned_official.username
        || officialRosterBoundary.game !== game.body.id
        || officialRosterBoundary.team !== coachTeam.body.id
        || officialRosterBoundary.batch !== null
        || officialRosterBoundary.hasPartial
        || officialRosterBoundary.hasSkippedName) {
      fail(`Coach batch result crossed the no-reload identity boundary into `
        + `the assigned Official's same-game/same-team Roster: `
        + `${JSON.stringify(officialRosterBoundary)}`);
    }

    const coachReturn = await page.evaluate(async ([u, p]) => signIn(u, p),
      [accounts.coach.username, PW]);
    if (!coachReturn) fail("assigned Official -> Coach no-reload sign-in failed");
    await waitForView(page, "roster");
    await waitForRealContent(page);
    const coachReturnTarget = await page.evaluate(() => ({
      game: currentGame,
      team: rosterTeamId,
      batch: rosterBatch,
    }));
    if (coachReturnTarget.game !== game.body.id
        || coachReturnTarget.team !== coachTeam.body.id
        || coachReturnTarget.batch !== null) {
      fail(`Coach did not return to a fresh target Roster after the identity `
        + `round trip: ${JSON.stringify(coachReturnTarget)}`);
    }

    // Hold an authenticated mutation response itself, not just a read. The
    // browser can send this POST as Coach, switch to the assigned Official,
    // then receive the Coach response under the Official's live session. A
    // transport-level identity check must cancel the old continuation before
    // recordRosterBatch(), toast, or render() can publish its private names.
    const staleRosterName = `Held departing roster candidate ${suffix}`;
    let releaseHeldRosterCopy;
    let markHeldRosterCopy;
    const heldRosterCopyRelease = new Promise((resolve) => {
      releaseHeldRosterCopy = resolve;
    });
    const heldRosterCopy = new Promise((resolve) => {
      markHeldRosterCopy = resolve;
    });
    const holdRosterCopyResponse = async (route) => {
      markHeldRosterCopy();
      await heldRosterCopyRelease;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ team_id: coachTeam.body.id,
          source: "copy_previous_roster", seated: [], deferred: [],
          skipped: [{ player_id: "held-departing-player", name: staleRosterName,
            reason: "player_inactive" }] }),
      });
    };
    const rosterCopyUrl = `**/api/games/${game.body.id}/roster/copy-previous`;
    await page.route(rosterCopyUrl, holdRosterCopyResponse);
    await page.evaluate(() => {
      window.__heldRosterCopy = rosterAction("copy")
        .then(() => ({ ok: true }))
        .catch((error) => ({ error: error && error.name }));
    });
    await heldRosterCopy;
    const heldRosterOfficialSwitch = await page.evaluate(async ([u, p]) => signIn(u, p),
      [accounts.assigned_official.username, PW]);
    if (!heldRosterOfficialSwitch) {
      fail("held roster POST Coach -> assigned Official sign-in failed");
    }
    await waitForView(page, "roster");
    await waitForRealContent(page);
    releaseHeldRosterCopy();
    const heldRosterOutcome = await page.evaluate(async () => {
      const outcome = await window.__heldRosterCopy;
      delete window.__heldRosterCopy;
      return outcome;
    });
    await page.unroute(rosterCopyUrl, holdRosterCopyResponse);
    const heldRosterBoundary = await page.evaluate((sentinel) => ({
      role: currentRole,
      username: currentUser && currentUser.username,
      batch: rosterBatch,
      toast,
      hasPartial: !!document.querySelector(".ros-partial"),
      hasSentinel: (document.documentElement.textContent || "").includes(sentinel),
    }), staleRosterName);
    if (heldRosterBoundary.role !== "official"
        || heldRosterBoundary.username !== accounts.assigned_official.username
        || heldRosterBoundary.batch !== null
        || heldRosterBoundary.hasPartial
        || heldRosterBoundary.hasSentinel
        || heldRosterBoundary.toast.includes(staleRosterName)) {
      fail(`held Coach roster POST response crossed into the assigned Official `
        + `(old outcome ${JSON.stringify(heldRosterOutcome)}): `
        + `${JSON.stringify(heldRosterBoundary)}`);
    }
    const afterHeldRosterCoach = await page.evaluate(async ([u, p]) => signIn(u, p),
      [accounts.coach.username, PW]);
    if (!afterHeldRosterCoach) fail("assigned Official -> Coach after held roster POST failed");
    await waitForView(page, "roster");
    await waitForRealContent(page);
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
    // The installation-wide assignment pool is MANAGE_SCHEDULE-only.  Pin
    // the server refusal first (and register that exact expected 403 with the
    // journey's global failure tracker), then observe the real Game Sheet
    // render and prove it does not make the forbidden request at all.
    tracker.expect("GET", "/api/officials", 403);
    const officialsForCoach = await apiGet(page, "/api/officials");
    if (officialsForCoach.status !== 403
        || !officialsForCoach.body.error
        || officialsForCoach.body.error.code !== "forbidden"
        || officialsForCoach.body.error.details.role !== "coach"
        || officialsForCoach.body.error.details.required !== "manage_schedule") {
      fail(`Coach [${L}]: /api/officials did not enforce the exact `
        + `MANAGE_SCHEDULE refusal: ${JSON.stringify(officialsForCoach)}`);
    }

    const sheetPoolRequests = [];
    const observeOfficialPool = (request) => {
      let requestPath = request.url();
      try { requestPath = new URL(request.url()).pathname; } catch (_) {}
      if (request.method() === "GET" && requestPath === "/api/officials") {
        sheetPoolRequests.push(request.url());
      }
    };
    page.on("request", observeOfficialPool);
    // A real keyboard activation of the real nav tab, the same discipline
    // every other reachability claim in this file uses.
    await tabToAndActivate(page, '.tab[data-tab="sheet"]', "Coach reach Game Sheet");
    await waitForView(page, "sheet");
    await waitForRealContent(page);
    await page.waitForSelector(".game-sheet .gs-grid", { timeout: 10000 });
    page.off("request", observeOfficialPool);
    if (sheetPoolRequests.length) {
      fail(`Coach [${L}]: Game Sheet requested the private global officials `
        + `pool ${sheetPoolRequests.length} time(s)`);
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
    // A CLIENT SIDE HINT IS INERT ON THE TWO WORKFLOW LEAVES (#427 final
    // blocker, round 3).
    //
    // The Roster tab really does send `?team_id=` on both of these, on every
    // render, with the side toggle choosing the value -- so "the hint is
    // ignored" is a claim about a parameter this SHIPPED SCREEN supplies,
    // not a theoretical one, and it is worth proving from the same session
    // that supplies it. Until round 3 these two answered a hinted call
    // DIFFERENTLY from an un-hinted one (403 for the opponent's id) while
    // the contract shipped in round 2 listed them among the routes where a
    // hint is ignored.
    //
    // Byte equality against the un-hinted response, not "200 and looks
    // right": that is the only assertion that cannot be satisfied by a
    // second, differently-narrowed answer.
    // ------------------------------------------------------------
    for (const leaf of ["substitute-candidates", "substitute-addable"]) {
      const path = `/api/games/${game.body.id}/${leaf}`;
      const plain = await apiGet(page, path);
      if (plain.status !== 200 || plain.body.error) {
        fail(`Coach [${L}]: un-hinted GET ${leaf} failed, so the hint `
          + `comparison below would prove nothing: `
          + `${JSON.stringify(plain)}`);
      }
      if (plain.body.team_id !== coachTeam.body.id) {
        fail(`Coach [${L}]: ${leaf} answered for ${plain.body.team_id} `
          + `rather than this Coach's own team ${coachTeam.body.id}`);
      }
      for (const hinted of [rivalTeam.body.id, coachTeam.body.id]) {
        const probe = await apiGet(page, `${path}?team_id=${hinted}`);
        if (probe.status !== 200 || probe.body.error) {
          fail(`Coach [${L}]: ${leaf}?team_id=${hinted} was REFUSED rather `
            + `than answered identically to the un-hinted call -- a 403 that `
            + `appears only for the opponent's id is a side selector by `
            + `another name: ${JSON.stringify(probe)}`);
        }
        if (JSON.stringify(probe.body) !== JSON.stringify(plain.body)) {
          fail(`Coach [${L}]: ${leaf}?team_id=${hinted} changed the answer -- `
            + `a client hint selected a side: `
            + `${JSON.stringify(probe.body)} vs ${JSON.stringify(plain.body)}`);
        }
      }
    }

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

    // ------------------------------------------------------------
    // THE GAME SHEET HOLDS ONLY ROWS THAT OCCUPY A SLOT (#427 round 2,
    // blocker 3), probed from the assigned official's own real session.
    //
    // `_lineup_rows` keeps a seated player in the `selected` GROUP after they
    // have gone unavailable, flagged `backed_out: true`, so their own coach
    // can still see the row for cleanup. The official's projection filtered
    // on that group alone, so a referee's Game Sheet listed a player who is
    // not playing -- that side's roster HISTORY rather than its current
    // sheet. All three routes share one helper, so all three are probed.
    //
    // The fixture is built through the OPERATOR reader (the same authorized
    // boundary this file already uses to snapshot state), and read back
    // through the OFFICIAL's session. Nothing is faked, and nothing is
    // asserted about a row this reader is not entitled to at all.
    // ------------------------------------------------------------
    const sheetOn = await apiPost(reader, "/api/v2/setup/player",
      { team_id: coachTeam.body.id, name: `Sheet Playing ${suffix}`, position: "forward" });
    const sheetOff = await apiPost(reader, "/api/v2/setup/player",
      { team_id: coachTeam.body.id, name: `Sheet Backed Out ${suffix}`, position: "defense" });
    const seatSheet = await apiPost(reader,
      `/api/games/${game.body.id}/roster/select`,
      { player_ids: [sheetOn.body.id, sheetOff.body.id] });
    if (seatSheet.status !== 200 || seatSheet.body.error) {
      fail(`sheet seat failed: ${JSON.stringify(seatSheet)}`);
    }
    const backOut = await apiPost(reader,
      `/api/games/${game.body.id}/availability`,
      { player_id: sheetOff.body.id, availability_status: "unavailable" });
    if (backOut.status !== 200 || backOut.body.error) {
      fail(`sheet back-out failed: ${JSON.stringify(backOut)}`);
    }
    // The PREMISE, read through the operator: one row occupies its slot and
    // the other does not, and BOTH are still in the `selected` display group.
    // Without this the probes below could pass because the row was never
    // created at all.
    const fullSide = await apiGet(reader, `/api/games/${game.body.id}/lineups`);
    const fullRows = (fullSide.body.home.players || [])
      .filter((pl) => pl.id === sheetOn.body.id || pl.id === sheetOff.body.id);
    if (fullRows.length !== 2
        || !fullRows.every((pl) => pl.group === "selected")
        || fullRows.filter((pl) => pl.backed_out === true).length !== 1) {
      fail(`Assigned official [${L}]: the sheet fixture is not the shape this `
        + `leg is about (two 'selected' rows, exactly one backed out): `
        + `${JSON.stringify(fullRows)}`);
    }
    const sheetProbes = {
      board: (b) => b.players || [],
      lineups: (b) => (b.home.players || []),
      roster: (b) => (b || []).filter((r) => r.team_id === coachTeam.body.id),
    };
    for (const route of Object.keys(sheetProbes)) {
      const res = await apiGet(page, `/api/games/${game.body.id}/${route}`);
      if (res.status !== 200) {
        fail(`Assigned official [${L}]: /${route} answered ${res.status}: `
          + `${JSON.stringify(res.body)}`);
      }
      const rows = sheetProbes[route](res.body);
      const ids = rows.map((r) => r.id);
      if (!ids.includes(sheetOn.body.id)) {
        fail(`Assigned official [${L}]: /${route} dropped a row that DOES `
          + `occupy a slot: ${JSON.stringify(rows)}`);
      }
      if (ids.includes(sheetOff.body.id)) {
        fail(`Assigned official [${L}]: /${route} carried a backed-out row `
          + `into the Game Sheet -- a player who no longer occupies a slot, `
          + `which is that side's roster history and not the current sheet: `
          + `${JSON.stringify(rows)}`);
      }
      if (rows.some((r) => r.backed_out !== false)) {
        fail(`Assigned official [${L}]: /${route} returned a row whose `
          + `backed_out is not false: ${JSON.stringify(rows)}`);
      }
    }

    // ------------------------------------------------------------
    // THE GAMES LIST RENDERS THE WITHHELD ROSTER STATUS AS WITHHELD
    // (#205, round 4).
    //
    // WHAT ONLY A BROWSER CAN PROVE. The server side -- that
    // /api/demo/overview omits `schedule[].roster_status` for a caller
    // entitled to no side -- is pinned tri-store over real authenticated
    // HTTP (backend/tests/test_overview_schedule_side.py). What that cannot
    // show is what the SHIPPED SCREEN does with the omission, and that is
    // the whole reason the field carries an explicit marker rather than
    // just disappearing: every consumer of it asks
    // `["roster_confirmed","locked"].includes(g.roster_status)`, which a
    // missing key answers `false` -- so a naive omission renders as the
    // badge "Roster open" and the checklist line "Roster -- Not confirmed".
    // That is restricted data displayed as an EMPTY OPERATIONAL STATE, and
    // it is a false claim about the other team rather than a true one about
    // this reader.
    //
    // The Games tab is where it shows: `gateChrome()` never hides it, so an
    // official reaches this list, and before the fix its rows carried the
    // HOME side's real private status for every game in the Program --
    // including games this official was never assigned to.
    //
    // Nothing is faked: a real assigned official's real session, the real
    // route, the real render.
    // ------------------------------------------------------------
    const officialOverview = await apiGet(page, "/api/demo/overview");
    if (officialOverview.status !== 200 || officialOverview.body.error) {
      fail(`Assigned official [${L}]: /api/demo/overview failed, so the `
        + `render assertions below would prove nothing: `
        + `${JSON.stringify(officialOverview).slice(0, 200)}`);
    }
    const officialRow = (officialOverview.body.schedule || [])
      .find((g) => g.game_id === game.body.id);
    if (!officialRow) {
      fail(`Assigned official [${L}]: the fixture game is absent from this `
        + `official's schedule, so nothing below is being asserted`);
    }
    if ("roster_status" in officialRow) {
      fail(`Assigned official [${L}]: schedule[].roster_status was served to `
        + `a caller entitled to no side of this game -- `
        + `${JSON.stringify(officialRow.roster_status)}`);
    }
    if (officialRow.roster_status_restricted !== true
        || officialRow.roster_status_team_id !== null) {
      fail(`Assigned official [${L}]: the withheld row carries no explicit `
        + `marker, so the screen has nothing to distinguish it from a real `
        + `unconfirmed roster: ${JSON.stringify(officialRow)}`);
    }
    await page.evaluate(() => switchTab("games"));
    await waitForView(page, "games");
    await waitForRealContent(page);
    await page.waitForSelector(".games-row", { timeout: 10000 });
    // Expand THIS game's row -- clicked by its own game id, so re-ordering
    // the list cannot silently retarget the assertion at another game.
    await page.click(`.games-row[data-games-toggle="${game.body.id}"]`);
    await page.waitForSelector(".games-detail", { timeout: 10000 });
    const officialGames = await page.evaluate((gid) => {
      const head = document.querySelector(`.games-row[data-games-toggle="${gid}"]`);
      const detail = head && head.nextElementSibling;
      const checks = Array.from(
        (detail || document).querySelectorAll(".games-detail .check"));
      const roster = checks.find((c) => {
        const lbl = c.querySelector(".lbl");
        return lbl && lbl.textContent.trim() === "Roster";
      });
      return {
        badge: head ? (head.querySelector(".pill") || {}).textContent : null,
        rosterClass: roster ? roster.className : null,
        rosterMeta: roster
          ? ((roster.querySelector(".meta") || {}).textContent || "").trim()
          : null,
        detailText: (detail ? detail.textContent : "").replace(/\s+/g, " "),
      };
    }, game.body.id);
    if (!officialGames.rosterClass) {
      fail(`Assigned official [${L}]: no "Roster" checklist line rendered, so `
        + `the withheld state has no proven rendering: `
        + `${JSON.stringify(officialGames)}`);
    }
    // THE THIRD STATE, and it is the point: `check todo` is the rendering of
    // "this roster is not confirmed", which is exactly the false claim.
    if (!/\bunknown\b/.test(officialGames.rosterClass)
        || /\btodo\b/.test(officialGames.rosterClass)
        || /\bok\b/.test(officialGames.rosterClass)) {
      fail(`Assigned official [${L}]: a withheld roster status rendered as a `
        + `done/to-do checklist state ("${officialGames.rosterClass}") -- `
        + `restricted data must never be shown as an empty operational `
        + `state`);
    }
    if (!/not shown/i.test(officialGames.rosterMeta || "")) {
      fail(`Assigned official [${L}]: the withheld roster line does not say `
        + `it was withheld, got "${officialGames.rosterMeta}"`);
    }
    if (/not confirmed/i.test(officialGames.detailText)
        || /Draft — no players/i.test(officialGames.detailText)) {
      fail(`Assigned official [${L}]: the game row asserts an operational `
        + `roster state this reader was never shown: `
        + `${officialGames.detailText.slice(0, 200)}`);
    }
    // And the row's own badge claims neither readiness nor an open roster.
    if (/Roster open/i.test(officialGames.badge || "")
        || /^\s*Ready\s*$/i.test(officialGames.badge || "")) {
      fail(`Assigned official [${L}]: the row badge "${officialGames.badge}" `
        + `states a readiness verdict whose roster half was withheld`);
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
    const detail = error && error.stack ? error.stack : error.message;
    throw new Error(`${detail}\n--- demo server output ---\n${serverOutput}`);
  } finally {
    if (reader) await reader.context().close();
    await context.close();
    await stopServer(server);
  }
}

async function main() {
  let browser;
  try {
    assertSessionCookieTransitionWiring();
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
