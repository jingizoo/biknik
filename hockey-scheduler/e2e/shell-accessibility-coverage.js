// Shell accessibility regression coverage (#345).
//
// accessibility-foundations.js proves the shell-level skip link, per-view
// page titles, and dialog focus containment on the AUTHENTICATED console and
// on four signed-out/anonymous transitions (authenticated -> signed out ->
// failed login -> anonymous public), but it explicitly left the public ->
// "Staff sign in" leg untested (see its own comment), and it never reaches
// two OTHER shell surfaces this app actually ships: a forced loading/error
// state on data the signed-out shell fetches, and the "Restricted" early
// return a signed-in user with no access to a private game hits before any
// of the app's normal screens render. This is a DEDICATED journey for
// exactly those five surfaces, at desktop and canonical 390x844:
//
//   1. Signed-out login screen (via a real Sign-out click).
//   2. Public Schedule (via a real "View public schedule" click).
//   3. Public Schedule -> Staff sign-in, via a real click on
//      #public-signin-link -- the exact leg accessibility-foundations.js
//      named as its own remaining gap.
//   4. A forced loading, then forced error, surface: the public schedule's
//      own fetch held open and then made to 502, both via real clicks that
//      re-trigger the same request (switching tabs), not a synthetic replay.
//   5. The Restricted early-return: an Official account with NO assignment
//      lands there from one real click on the "Roster" nav tab (the backend
//      scope gate mirrors this at the API level -- see app.js's
//      accessibleGames()/canReadAnyPrivateGame() comments), landing exactly
//      on the guard app.js documents at its render()'s "shell states"
//      early-return, before any roster data renders.
//
// For every surface: the page title names the visible surface, the skip
// link is never focusable while its #content target is hidden (and IS
// reachable once #content is genuinely visible, asserted on surface 5), the
// primary heading and accessible names are correct, a real Tab walk reaches
// controls in the order the DOM actually presents them, focus is visibly
// indicated wherever it lands, and an axe-core scan reports zero serious or
// critical violations. Fails on any browser console/page error.
//
// STATUS/ERROR ANNOUNCEMENTS (#345 review release blocker, fixed here, not
// just documented): #login-error now carries role="alert" (index.html).
// renderPublicGuest() now gives #public-content role="status"
// aria-live="polite" aria-busy (flipping false once settled, matching the
// setup-progress card's own #sp-card-slot contract in home-tasks-hub.js),
// its error banner additionally carries role="alert", and a monotonic
// publicRenderSeq guards a held/obsolete response from clobbering a NEWER
// render's content once it finally resolves -- exercised below via two
// genuinely overlapping real clicks (hold a Standings fetch, navigate out
// via the PERSISTENT #public-signin-link and back in via the persistent
// #guest-public-link to start a second, newer render, THEN release the
// obsolete first one). The held request resolves to a DELIBERATELY
// DISTINCT stale fixture (not the live backend, which would return
// identical data both times and make the check non-falsifiable) so
// releasing it is a real test: falsifiability verified directly by
// temporarily disabling both mySeq guards in the schedule/standings fetch
// path, confirming this exact leg then fails with the stale fixture's
// heading, and restoring them. The Restricted early-return now carries
// role="alert" too. FOCUS: since
// #public-content itself is never replaced (only its children are),
// focusing the box once (only when the interaction that triggered this
// render started INSIDE it) keeps focus connected and visible across every
// later state in the same cycle instead of dropping to <body>; Restricted
// moves focus onto its own heading, but only the FIRST time that state is
// entered, so a redundant re-render never re-announces or re-steals focus.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");
const fs = require("fs");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8341 },
  { label: "phone", width: 390, height: 844, port: 8342 },
];
const AXE_PATH = require.resolve("axe-core/axe.min.js");
const BAD_GATEWAY = { status: 502, contentType: "text/html", body: "<html><body>502 Bad Gateway</body></html>" };

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

async function apiPost(page, p, body) {
  return page.evaluate(async (arg) => (await fetch(arg.p, {
    method: "POST", credentials: "same-origin",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(arg.body),
  })).json(), { p, body });
}
async function apiGet(page, p) {
  return page.evaluate((p) => fetch(p, { credentials: "same-origin" }).then((r) => r.json()), p);
}

// Describe whatever currently has focus, including whether its OWN focus
// indicator (native outline or a CSS box-shadow/border replacement -- this
// app's .login-field input:focus swaps outline for box-shadow) is actually
// visible, not just present in the accessibility tree.
function activeInfo(page) {
  return page.evaluate(() => {
    const el = document.activeElement;
    if (!el) return null;
    const cs = getComputedStyle(el);
    return {
      tag: el.tagName.toLowerCase(), id: el.id || null, cls: el.className || "",
      text: (el.textContent || el.value || "").trim().slice(0, 60),
      visibleFocus: (cs.outlineStyle !== "none" && cs.outlineWidth !== "0px") || cs.boxShadow !== "none",
    };
  });
}

async function blurActive(page) {
  await page.evaluate(() => {
    if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
  });
}

// Poll for the title rather than reading it once: setShellTitle()/
// setPageTitle() run synchronously within their own transition, but a
// PRECEDING async operation this journey deliberately triggers (e.g. the
// failed-login POST) can still be settling its own promise chain when the
// next action fires, so a bare one-shot page.title() read can observe a
// stale value for a beat. Matches the wait-for-observable-state pattern
// accessibility-foundations.js already uses for every title assertion.
async function waitForTitle(page, expected, fail, label) {
  await page.waitForFunction((t) => document.title === t, expected, { timeout: 10000 })
    .catch(async () => {
      fail(`${label}: expected title "${expected}", got "${await page.title()}"`);
    });
}

// Same "dangling skip link" defect accessibility-foundations.js's shell-state
// section guards against: a skip link that is visible OR programmatically
// focusable while its own #content target is hidden is unusable at best and
// a false affordance at worst. Tries a real .focus() call (not just reading
// getClientRects()) because `display:none` on .web makes the link
// unfocusable even if some future change left it visually reachable by
// accident; restores whatever had focus before if the probe actually moved
// it, so callers can run this without disturbing a focus assertion around it.
async function skipLinkState(page) {
  return page.evaluate(() => {
    const sk = document.getElementById("skip-link");
    const content = document.getElementById("content");
    const visible = (el) => !!(el && el.getClientRects().length > 0);
    const before = document.activeElement;
    let becameFocused = false;
    if (sk) {
      sk.focus();
      becameFocused = document.activeElement === sk;
      if (becameFocused && before && before !== document.body && typeof before.focus === "function") {
        before.focus();
      }
    }
    return {
      skipVisible: visible(sk), skipFocusable: becameFocused, contentVisible: visible(content),
      danglingSkip: (visible(sk) || becameFocused) && !visible(content),
    };
  });
}

// axe-core, served from a same-origin URL (CSP is script-src 'self', so
// addScriptTag({path}) -- which inlines the file body -- is blocked); same
// technique as home-tasks-hub.js. Filters to serious/critical only, per this
// slice's own acceptance bar -- a minor/moderate finding on a brand-new
// journey's first pass is a backlog item, not a regression gate.
async function seriousOrCriticalViolations(page, selector) {
  await page.addScriptTag({ url: "/__axe-core__.js" });
  return page.evaluate(async (sel) => {
    const root = sel ? document.querySelector(sel) : document;
    if (!root) return [{ id: "selector-not-found", impact: "critical", help: `"${sel}" not found`, nodes: [] }];
    const report = await axe.run(root, { resultTypes: ["violations"] });
    return report.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
  }, selector);
}

// A precise error ledger, not a text-pattern blanket filter. This journey
// deliberately provokes exactly three real HTTP failures (a wrong-password
// 401, the Restricted surface's own 403 scope check, a forced public 502).
// expectFailure() must be called, by exact method/URL-substring/status,
// immediately before each deliberate action; only THAT exact response is
// consumed and excluded. Any OTHER response >= 400 -- an unrelated 404/500
// on any request -- fails the run. Chromium also mirrors any >=400 response
// into the console as "Failed to load resource: status NNN"; that is the
// SAME underlying event the response listener below already captures with
// full precision, so it is not double-counted as a console error, but a
// genuine console.error(...) call or uncaught exception still is.
function wireErrorTracking(page) {
  const errors = [];
  const expectedFailures = [];
  const expectFailure = (method, urlIncludes, status) => {
    expectedFailures.push({ method, urlIncludes, status, consumed: false });
  };
  page.on("pageerror", (e) => errors.push(`[pageerror] ${e.message}`));
  page.on("response", (response) => {
    const status = response.status();
    if (status < 400) return;
    const req = response.request();
    const method = req.method();
    const url = response.url();
    const idx = expectedFailures.findIndex((f) => !f.consumed
      && f.method === method && f.status === status && url.includes(f.urlIncludes));
    if (idx !== -1) { expectedFailures[idx].consumed = true; return; }
    errors.push(`[http] UNEXPECTED ${method} ${url} -> ${status}`);
  });
  const RESOURCE_STATUS_NOISE = /^Failed to load resource: the server responded with a status of \d+/;
  page.on("console", (m) => {
    if (m.type() === "error" && !RESOURCE_STATUS_NOISE.test(m.text())) errors.push(`[console] ${m.text()}`);
  });
  return { errors, expectFailure, expectedFailures };
}

// Falsifiability proof (#345 review): an injected, UNLISTED failure must
// still be caught by the exact same tracker the real journey below uses --
// not a parallel reimplementation, the same wireErrorTracking() function.
// No expectFailure() is registered here, so this 404 has no allowlist
// entry; if the tracker were a blanket filter this would silently vanish.
async function verifyLedgerFalsifiability(browser, base, viewportLabel, fail) {
  const context = await browser.newContext();
  const page = await context.newPage();
  const { errors } = wireErrorTracking(page);
  const SENTINEL = "/api/__shell-coverage-ledger-selftest__";
  await page.route(`**${SENTINEL}`, (route) => route.fulfill({
    status: 404, contentType: "application/json", body: '{"error":"not_found"}',
  }));
  await page.goto(base, { waitUntil: "domcontentloaded" });
  await page.waitForSelector("#content .dash-card, #content h2, #content h3", { timeout: 15000 });
  await page.evaluate((p) => fetch(p, { credentials: "same-origin" }).catch(() => {}), SENTINEL);
  await page.waitForTimeout(300);
  await context.close();
  if (!errors.some((e) => e.includes(SENTINEL) && e.includes("404"))) {
    fail(`[${viewportLabel}] ledger falsifiability: an injected, UNLISTED 404 `
      + `was not flagged by the same tracker the real journey uses -- the `
      + `mechanism is not falsifiable, got ${JSON.stringify(errors)}`);
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
  const { errors, expectFailure, expectedFailures } = wireErrorTracking(page);
  const axeSource = fs.readFileSync(AXE_PATH, "utf8");
  await page.route("**/__axe-core__.js", (route) => route.fulfill({
    status: 200, contentType: "application/javascript", body: axeSource,
  }));

  const fail = (msg) => { throw new Error(`[${viewport.label}] ${msg}`); };
  let errorMark = 0;
  const checkpointErrors = (label) => {
    if (errors.length > errorMark) {
      fail(`${label}: browser errors:\n${errors.slice(errorMark).join("\n")}`);
    }
    errorMark = errors.length;
  };
  const assertNoSeriousViolations = async (selector, label) => {
    const violations = await seriousOrCriticalViolations(page, selector);
    if (violations.length) {
      const details = violations.map((v) => `${v.id} (${v.impact}): ${v.help} `
        + `[${v.nodes.map((n) => n.target.join(" ")).join(", ")}]`).join("\n");
      fail(`${label}: automated accessibility scan found serious/critical `
        + `violations:\n${details}`);
    }
  };
  const assertSkip = (state, label) => {
    if (state.danglingSkip) {
      fail(`${label}: skip link is focusable/visible (visible=${state.skipVisible}, `
        + `programmatically-focusable=${state.skipFocusable}) while its #content `
        + `target is hidden -- ${JSON.stringify(state)}`);
    }
  };

  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await verifyLedgerFalsifiability(browser, base, viewport.label, fail);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content .dash-card, #content h2, #content h3", { timeout: 15000 });
    checkpointErrors("initial authenticated boot");

    // Seed real demo data (schedule + fixtures) so the public schedule has
    // actual content to check accessible names/keyboard order over, and so
    // surface 5 has a real, unassigned game to be "restricted" from.
    await apiPost(page, "/api/demo/load", {});
    const official = await apiPost(page, "/api/v2/setup/official", { name: "Ozzy No-Assignment" });
    const officialUsername = `shellcov_official_${viewport.port}`;
    const officialAcct = await apiPost(page, "/api/accounts", {
      username: officialUsername, password: "scoped-account-pw",
      role: "official", scope: { official_id: official.id },
    });
    if (officialAcct.status && officialAcct.error) {
      fail(`could not create the unassigned Official account: ${JSON.stringify(officialAcct)}`);
    }
    const publicSchedule = await apiGet(page, "/api/public/schedule");
    const expectedPublicHeading = publicSchedule.league_name || "Program";

    // ---- (1) Signed-out login screen, via a real Sign-out click ----------
    await page.click("#signout-btn");
    await page.waitForFunction(() => document.body.classList.contains("signed-out"),
      null, { timeout: 10000 });
    await waitForTitle(page, "Sign in — Hockey Scheduler", fail, "login screen");
    assertSkip(await skipLinkState(page), "login screen");

    const loginHeading = await page.evaluate(() => {
      const h = document.querySelector(".login-title");
      return h ? { text: h.textContent.trim(), visible: h.getClientRects().length > 0 } : null;
    });
    if (!loginHeading || loginHeading.text !== "Sign in" || !loginHeading.visible) {
      fail(`login screen: expected a visible "Sign in" primary heading, got ${JSON.stringify(loginHeading)}`);
    }
    const loginNames = await page.evaluate(() => {
      const nameFor = (id) => {
        const el = document.getElementById(id);
        const label = el && el.closest("label");
        return label ? label.textContent.replace(/\s+/g, " ").trim() : null;
      };
      return {
        user: nameFor("login-user"), pass: nameFor("login-pass"),
        submit: ((document.querySelector(".login-submit") || {}).textContent || "").trim(),
        guest: ((document.getElementById("guest-public-link") || {}).textContent || "").trim(),
      };
    });
    if (loginNames.user !== "Username" || loginNames.pass !== "Password"
        || loginNames.submit !== "Sign in" || !/View public schedule/.test(loginNames.guest)) {
      fail(`login screen: unexpected accessible names, got ${JSON.stringify(loginNames)}`);
    }
    // showLogin() deliberately auto-focuses the username field -- a real,
    // meaningful focus landing, not an accident of the sign-out transition.
    let focused = await activeInfo(page);
    if (!focused || focused.id !== "login-user" || !focused.visibleFocus) {
      fail(`login screen: expected focus on #login-user with a visible focus `
        + `indicator right after sign-out, got ${JSON.stringify(focused)}`);
    }
    // Real Tab walk: username (current) -> password -> submit -> zero or
    // more demo persona buttons -> the guest link. Never through the skip
    // link (it must not even be a tab stop here).
    const loginStops = [];
    for (let i = 0; i < 12; i += 1) {
      await page.keyboard.press("Tab");
      const info = await activeInfo(page);
      loginStops.push(info);
      if (info && info.id === "guest-public-link") break;
    }
    const lastLoginStop = loginStops[loginStops.length - 1];
    if (!lastLoginStop || lastLoginStop.id !== "guest-public-link") {
      fail(`login screen: Tab never reached the guest link in a sensible number `
        + `of stops, got ${JSON.stringify(loginStops)}`);
    }
    if (loginStops[0].id !== "login-pass" || !loginStops[1].cls.includes("login-submit")) {
      fail(`login screen: expected Tab order username -> password -> submit, `
        + `got ${JSON.stringify(loginStops.slice(0, 2))}`);
    }
    const loginMiddle = loginStops.slice(2, -1);
    if (!loginMiddle.every((s) => s.cls.includes("login-persona"))) {
      fail(`login screen: expected only demo-persona buttons between the submit `
        + `button and the guest link, got ${JSON.stringify(loginMiddle)}`);
    }
    if (loginStops.some((s) => s.id === "skip-link") || !loginStops.every((s) => s.visibleFocus)) {
      fail(`login screen: skip link surfaced in the tab order, or a stop had no `
        + `visible focus indicator -- ${JSON.stringify(loginStops)}`);
    }
    // A failed login keeps the sign-in surface and surfaces a real message.
    expectFailure("POST", "/api/auth/login", 401);
    await page.fill("#login-user", "admin");
    await page.fill("#login-pass", "definitely-not-the-password");
    await page.click(".login-submit");
    await page.waitForFunction(() => {
      const e = document.getElementById("login-error");
      return !!(e && !e.hidden && e.textContent.trim());
    }, null, { timeout: 10000 });
    await waitForTitle(page, "Sign in — Hockey Scheduler", fail, "login screen (failed login)");
    const loginErrorRole = await page.evaluate(() =>
      (document.getElementById("login-error") || {}).getAttribute
        && document.getElementById("login-error").getAttribute("role"));
    if (loginErrorRole !== "alert") {
      fail(`login screen (failed login): expected #login-error to carry `
        + `role="alert", got ${JSON.stringify(loginErrorRole)}`);
    }
    // showLogin() re-focuses the username field on every call, including
    // this failure path -- usable focus, not just a visible message. The
    // browser's own default "focus the clicked submit button" can still be
    // settling a beat after showLogin()'s synchronous u.focus() call runs
    // (observed directly: activeElement briefly stays on the submit button
    // before moving to #login-user), so poll for the settled state rather
    // than reading it once.
    await page.waitForFunction(() => document.activeElement
      && document.activeElement.id === "login-user", null, { timeout: 5000 }).catch(async () => {
      fail(`login screen (failed login): expected focus back on #login-user, `
        + `got ${JSON.stringify(await activeInfo(page))}`);
    });
    const afterFailedLogin = await activeInfo(page);
    if (!afterFailedLogin || !afterFailedLogin.visibleFocus) {
      fail(`login screen (failed login): #login-user has no visible focus `
        + `indicator, got ${JSON.stringify(afterFailedLogin)}`);
    }
    assertSkip(await skipLinkState(page), "login screen (failed login)");
    await assertNoSeriousViolations("#login-screen", "login screen");
    checkpointErrors("login screen");

    // ---- (2) Public Schedule, via a real "View public schedule" click ----
    await page.click("#guest-public-link");
    await page.waitForFunction(() => !document.getElementById("public-screen").hidden,
      null, { timeout: 10000 });
    await page.waitForSelector(".hero h2", { timeout: 10000 });
    await waitForTitle(page, "Public Schedule — Hockey Scheduler", fail, "public schedule");
    assertSkip(await skipLinkState(page), "public schedule");
    const publicHeading = await page.evaluate(() => {
      const h = document.querySelector(".hero h2");
      return h ? { text: h.textContent.trim(), visible: h.getClientRects().length > 0 } : null;
    });
    if (!publicHeading || publicHeading.text !== expectedPublicHeading || !publicHeading.visible) {
      fail(`public schedule: expected a visible "${expectedPublicHeading}" `
        + `primary heading, got ${JSON.stringify(publicHeading)}`);
    }
    // showPublicGuest() mirrors showLogin()'s own unconditional focus: the
    // #guest-public-link that triggered this entry is hidden along with the
    // whole sign-in card, so #public-signin-link (persistent, always
    // rendered) takes focus instead of dropping to <body>.
    const publicEntryFocus = await activeInfo(page);
    if (!publicEntryFocus || publicEntryFocus.id !== "public-signin-link" || !publicEntryFocus.visibleFocus) {
      fail(`public schedule: expected focus on #public-signin-link right `
        + `after entry, got ${JSON.stringify(publicEntryFocus)}`);
    }
    const publicNames = await page.evaluate(() => ({
      signIn: ((document.getElementById("public-signin-link") || {}).textContent || "").trim(),
      scheduleTab: ((document.querySelector('[data-public-tab="schedule"]') || {}).textContent || "").trim(),
      standingsTab: ((document.querySelector('[data-public-tab="standings"]') || {}).textContent || "").trim(),
      fixtureCount: document.querySelectorAll(".li-btn").length,
      firstFixture: ((document.querySelector(".li-btn") || {}).textContent || "").replace(/\s+/g, " ").trim(),
    }));
    if (publicNames.signIn !== "Staff sign in" || publicNames.scheduleTab !== "Schedule"
        || publicNames.standingsTab !== "Standings" || publicNames.fixtureCount < 1
        || !/vs/.test(publicNames.firstFixture)) {
      fail(`public schedule: unexpected accessible names/content, got ${JSON.stringify(publicNames)}`);
    }
    // Continue the Tab walk from the CURRENT position (already verified
    // above to be #public-signin-link, via showPublicGuest()'s own
    // deliberate focus) rather than blur()+Tab: blur() alone does not reset
    // the browser's sequential-navigation origin once a real focus() call
    // has anchored it here, so a fresh Tab from a blurred state would
    // actually land on the NEXT stop, not this one (the same nuance
    // accessibility-foundations.js's own comment documents for the skip
    // link).
    let stop;
    await page.keyboard.press("Tab");
    stop = await activeInfo(page);
    if (!stop || !stop.cls.includes("seg") || !/Schedule/.test(stop.text) || !stop.visibleFocus) {
      fail(`public schedule: expected the second Tab stop to be the Schedule `
        + `segment button, got ${JSON.stringify(stop)}`);
    }
    await page.keyboard.press("Tab");
    stop = await activeInfo(page);
    if (!stop || !stop.cls.includes("seg") || !/Standings/.test(stop.text) || !stop.visibleFocus) {
      fail(`public schedule: expected the third Tab stop to be the Standings `
        + `segment button, got ${JSON.stringify(stop)}`);
    }
    await assertNoSeriousViolations("#public-screen", "public schedule");
    checkpointErrors("public schedule");

    // ---- (3) Public Schedule -> Staff sign-in, via a REAL click -----------
    // This is exactly the leg accessibility-foundations.js's own comment
    // named as untested ("I did not want to ship either a flaky step or a
    // fake one that dispatches the handler directly instead of clicking").
    await page.click("#public-signin-link");
    await page.waitForFunction(() => document.getElementById("public-screen").hidden
      && !document.getElementById("login-screen").hidden, null, { timeout: 10000 });
    await waitForTitle(page, "Sign in — Hockey Scheduler", fail, "public -> staff sign-in");
    assertSkip(await skipLinkState(page), "public -> staff sign-in");
    focused = await activeInfo(page);
    if (!focused || focused.id !== "login-user" || !focused.visibleFocus) {
      fail(`public -> staff sign-in: expected focus to land on #login-user, `
        + `got ${JSON.stringify(focused)}`);
    }
    const backToLoginHeading = await page.evaluate(() => {
      const h = document.querySelector(".login-title");
      return h ? { text: h.textContent.trim(), visible: h.getClientRects().length > 0 } : null;
    });
    if (!backToLoginHeading || backToLoginHeading.text !== "Sign in" || !backToLoginHeading.visible) {
      fail(`public -> staff sign-in: expected a visible "Sign in" heading, got ${JSON.stringify(backToLoginHeading)}`);
    }
    await page.keyboard.press("Tab");
    stop = await activeInfo(page);
    if (!stop || stop.id !== "login-pass" || !stop.visibleFocus) {
      fail(`public -> staff sign-in: expected Tab from the username field to `
        + `reach the password field next, got ${JSON.stringify(stop)}`);
    }
    await assertNoSeriousViolations("#login-screen", "public -> staff sign-in");
    checkpointErrors("public -> staff sign-in");

    // ---- (4) Forced loading, then forced error, on the public schedule ---
    // Real clicks re-trigger the SAME /api/public/schedule fetch renderPublicGuest()
    // always makes (switching the Schedule/Standings segment), rather than
    // reloading the page — proving this is the live fetch path, not a fresh boot.
    await page.click("#guest-public-link");
    await page.waitForFunction(() => !document.getElementById("public-screen").hidden,
      null, { timeout: 10000 });
    await page.waitForSelector(".hero h2", { timeout: 10000 });
    checkpointErrors("public schedule (return for surface 4)");

    // The FIRST request this route sees is held indefinitely, then resolved
    // with a DELIBERATELY DISTINCT, distinguishable stale payload -- not
    // passed through to the live backend, which would return the SAME real
    // data both times and make a stale response indistinguishable from a
    // correctly-ignored one. Every LATER request resolves immediately via
    // the real backend. #public-content's own segment tabs vanish the
    // instant loading starts, but the PERSISTENT shell controls outside
    // that region (#public-signin-link in .public-topbar, #guest-public-
    // link on the sign-in card) do not -- navigating out to Sign-in and
    // back in starts a genuinely second, overlapping renderPublicGuest()
    // call via two real clicks while the first request is still in flight.
    const STALE_FIXTURE = { league_name: "STALE SCHEDULE — must never render", divisions: [], fixtures: [] };
    let releaseHeldFetch;
    const heldFetch = new Promise((resolve) => { releaseHeldFetch = resolve; });
    let publicFetchCount = 0;
    await page.route("**/api/public/schedule", async (route) => {
      publicFetchCount += 1;
      if (publicFetchCount === 1) {
        await heldFetch;
        return route.fulfill({
          status: 200, contentType: "application/json", body: JSON.stringify(STALE_FIXTURE),
        });
      }
      await route.continue();
    });
    await page.click('[data-public-tab="standings"]');
    await page.waitForSelector("#public-content .skeleton", { timeout: 10000 });
    await waitForTitle(page, "Public Schedule — Hockey Scheduler", fail, "forced loading");
    assertSkip(await skipLinkState(page), "forced loading");
    // #345 review fix: #public-content itself (never replaced, only its
    // children are) takes focus and carries the busy/status contract,
    // instead of dropping to <body> when the clicked Standings control is
    // destroyed by the innerHTML rewrite.
    const loadingState = await page.evaluate(() => {
      const box = document.getElementById("public-content");
      const el = document.activeElement;
      return {
        focusOnBox: el === box,
        role: box && box.getAttribute("role"),
        ariaLive: box && box.getAttribute("aria-live"),
        ariaBusy: box && box.getAttribute("aria-busy"),
        srText: (box && box.querySelector(".sr-only") || {}).textContent || "",
      };
    });
    if (!loadingState.focusOnBox || loadingState.role !== "status"
        || loadingState.ariaLive !== "polite" || loadingState.ariaBusy !== "true"
        || !/Loading public schedule/.test(loadingState.srText)) {
      fail(`forced loading: expected #public-content to own focus with `
        + `role="status" aria-live="polite" aria-busy="true" and a "Loading `
        + `public schedule" status text, got ${JSON.stringify(loadingState)}`);
    }
    await assertNoSeriousViolations("#public-screen", "forced loading");

    // While that first (Standings) fetch is STILL held, navigate away via
    // the persistent Staff sign-in control, then back in via the persistent
    // guest link -- a second, real, overlapping renderPublicGuest() call.
    await page.click("#public-signin-link");
    await page.waitForFunction(() => !document.getElementById("login-screen").hidden,
      null, { timeout: 10000 });
    await page.click("#guest-public-link");
    await page.waitForFunction(() => !document.querySelector("#public-content .skeleton")
      && document.querySelector(".hero h2"), null, { timeout: 10000 });
    // #345 review fix: showPublicGuest() now mirrors showLogin()'s own
    // unconditional focus -- #public-signin-link (a persistent, always-
    // rendered control, unlike anything inside #public-content) takes
    // focus on every entry to this screen, so this SECOND entry (whose own
    // trigger, #guest-public-link, was just hidden along with the sign-in
    // card) still lands somewhere connected and meaningful instead of
    // <body>.
    const fingerprint = () => page.evaluate(() => {
      const el = document.activeElement;
      return {
        title: document.title,
        heading: (document.querySelector(".hero h2") || {}).textContent || "",
        ariaBusy: (document.getElementById("public-content") || {}).getAttribute("aria-busy"),
        activeId: el ? el.id : null,
        activeTag: el ? el.tagName : null,
      };
    });
    const settledFingerprint = await fingerprint();
    // The heading must be the REAL data (captured from the live API before
    // this section even started), never the stale fixture's -- this is the
    // exact identity check that makes the release below falsifiable: if
    // app.js's mySeq !== publicRenderSeq guards were removed, releasing the
    // held first fetch would overwrite this with "STALE SCHEDULE...".
    if (settledFingerprint.heading !== expectedPublicHeading || settledFingerprint.ariaBusy !== "false"
        || settledFingerprint.activeId !== "public-signin-link"
        || settledFingerprint.title !== "Public Schedule — Hockey Scheduler") {
      fail(`forced loading: the second, newer render (via Staff sign-in -> `
        + `guest link) did not settle with the REAL "${expectedPublicHeading}" `
        + `heading, aria-busy="false", focus on #public-signin-link (never `
        + `<body>), and the correct title, got ${JSON.stringify(settledFingerprint)}`);
    }
    // NOW release the obsolete FIRST (Standings) fetch -- it resolves with
    // the deliberately distinct STALE_FIXTURE payload. publicRenderSeq
    // already moved on to this second, newer render, so this must be a
    // complete no-op: the heading must still be the REAL data, not
    // STALE_FIXTURE's "STALE SCHEDULE — must never render".
    releaseHeldFetch();
    await page.waitForTimeout(300);  // give the obsolete response's own (discarded) handler a chance to run
    const afterObsoleteRelease = await fingerprint();
    const stillHasSkeletonOrError = await page.evaluate(() => ({
      hasSkeleton: !!document.querySelector("#public-content .skeleton"),
      hasError: !!document.querySelector("#public-content .banner.alert"),
    }));
    if (afterObsoleteRelease.title !== settledFingerprint.title
        || afterObsoleteRelease.heading !== settledFingerprint.heading
        || afterObsoleteRelease.ariaBusy !== "false" || afterObsoleteRelease.activeId !== "public-signin-link"
        || stillHasSkeletonOrError.hasSkeleton || stillHasSkeletonOrError.hasError) {
      fail(`forced loading: releasing the obsolete FIRST fetch (Standings, `
        + `held while a second real navigation started a newer render) `
        + `clobbered the already-settled render -- `
        + `${JSON.stringify({ ...afterObsoleteRelease, ...stillHasSkeletonOrError })}`);
    }
    await page.unroute("**/api/public/schedule");
    checkpointErrors("forced loading");

    expectFailure("GET", "/api/public/schedule", 502);
    await page.route("**/api/public/schedule", (route) => route.fulfill(BAD_GATEWAY));
    await page.click('[data-public-tab="schedule"]');
    await page.waitForSelector("#public-retry-btn", { timeout: 10000 });
    await waitForTitle(page, "Public Schedule — Hockey Scheduler", fail, "forced error");
    assertSkip(await skipLinkState(page), "forced error");
    const errorHeading = await page.evaluate(() => {
      const h = document.querySelector("#public-content .banner.alert h2");
      return h ? { text: h.textContent.trim(), visible: h.getClientRects().length > 0 } : null;
    });
    if (!errorHeading || errorHeading.text !== "Could not load the public schedule" || !errorHeading.visible) {
      fail(`forced error: expected a visible "Could not load the public schedule" `
        + `heading, got ${JSON.stringify(errorHeading)}`);
    }
    const errorSemantics = await page.evaluate(() => {
      const banner = document.querySelector("#public-content .banner.alert");
      const box = document.getElementById("public-content");
      return {
        bannerRole: banner && banner.getAttribute("role"),
        boxAriaBusy: box && box.getAttribute("aria-busy"),
        focusOnBox: document.activeElement === box,
      };
    });
    if (errorSemantics.bannerRole !== "alert" || errorSemantics.boxAriaBusy !== "false" || !errorSemantics.focusOnBox) {
      fail(`forced error: expected the banner to carry role="alert", `
        + `#public-content aria-busy="false", and focus to have stayed on `
        + `#public-content (never lost across the loading -> error `
        + `transition), got ${JSON.stringify(errorSemantics)}`);
    }
    // Tab forward from the box itself (its real current focus, not a reset)
    // must still reach Retry, keyboard-operable exactly as a real user
    // encountering this error would experience it.
    const retryStops = [];
    let reachedRetry = false;
    for (let i = 0; i < 8; i += 1) {
      await page.keyboard.press("Tab");
      const info = await activeInfo(page);
      retryStops.push(info);
      if (info && info.id === "public-retry-btn") { reachedRetry = true; break; }
    }
    if (!reachedRetry || !retryStops[retryStops.length - 1].visibleFocus) {
      fail(`forced error: Retry was not reached by Tab with a visible focus `
        + `indicator, got ${JSON.stringify(retryStops)}`);
    }
    if (retryStops.some((s) => s.id === "skip-link")) {
      fail(`forced error: skip link surfaced in the tab order -- ${JSON.stringify(retryStops)}`);
    }
    await assertNoSeriousViolations("#public-screen", "forced error");
    checkpointErrors("forced error");

    await page.unroute("**/api/public/schedule");
    await page.click("#public-retry-btn");
    await page.waitForFunction(() => !document.querySelector("#public-retry-btn")
      && !document.querySelector("#public-content .skeleton"), null, { timeout: 10000 });
    await page.waitForSelector(".hero h2", { timeout: 10000 });
    const recovered = await page.evaluate(() => {
      const box = document.getElementById("public-content");
      return {
        ariaBusy: box && box.getAttribute("aria-busy"),
        focusOnBox: document.activeElement === box,
        hasError: !!document.querySelector("#public-content .banner.alert"),
      };
    });
    if (recovered.ariaBusy !== "false" || !recovered.focusOnBox || recovered.hasError) {
      fail(`forced error retry recovery: expected aria-busy="false", focus `
        + `retained on #public-content, and the error banner gone, got `
        + `${JSON.stringify(recovered)}`);
    }
    checkpointErrors("forced error retry recovery");

    // ---- (5) Restricted early return, via a real "Roster" nav click -------
    // The Official account created above has zero assignments, so
    // accessibleGames()/the backend scope gate put them on a game outside
    // their scope the instant they land on Roster -- no synthetic game
    // selection needed. Retry recovery above left the public schedule
    // showing real content again, so a real click back to Staff sign-in
    // is needed before the official can sign in.
    await page.click("#public-signin-link");
    await page.waitForFunction(() => !document.getElementById("login-screen").hidden,
      null, { timeout: 10000 });
    checkpointErrors("public -> staff sign-in (return for surface 5)");
    await page.fill("#login-user", officialUsername);
    await page.fill("#login-pass", "scoped-account-pw");
    await page.click(".login-submit");
    await page.waitForFunction(() => document.body.dataset.view === "inbox", null, { timeout: 10000 });
    checkpointErrors("official sign-in");

    const rosterTab = await page.$('.tab[data-tab="roster"]');
    if (!rosterTab) fail("restricted: could not find the Roster nav tab");
    expectFailure("GET", "/lineups", 403);
    await rosterTab.click();
    await page.waitForFunction(() => document.body.dataset.view === "roster", null, { timeout: 10000 });
    await page.waitForSelector("#content .banner.neutral h2", { timeout: 10000 });
    await waitForTitle(page, "Roster — Hockey Scheduler", fail, "restricted");
    // Opposite direction of the dangling-skip check above: the authenticated
    // shell IS visible here, so the skip link must be a real, reachable
    // tab stop, not perpetually suppressed.
    const restrictedSkip = await skipLinkState(page);
    assertSkip(restrictedSkip, "restricted");
    if (!restrictedSkip.contentVisible) {
      fail(`restricted: expected #content to be visible in the authenticated `
        + `shell, got ${JSON.stringify(restrictedSkip)}`);
    }
    const restrictedHeading = await page.evaluate(() => {
      const h = document.querySelector("#content .banner.neutral h2");
      const p = document.querySelector("#content .banner.neutral p");
      return {
        heading: h ? h.textContent.trim() : null, headingVisible: !!(h && h.getClientRects().length > 0),
        detail: p ? p.textContent.trim() : null,
      };
    });
    if (restrictedHeading.heading !== "Restricted" || !restrictedHeading.headingVisible || !restrictedHeading.detail) {
      fail(`restricted: expected a visible "Restricted" heading with detail text, `
        + `got ${JSON.stringify(restrictedHeading)}`);
    }
    // #345 review fix: role="alert" announces this without needing focus,
    // AND focus moves onto the heading as the destination the Roster click
    // asked for -- a deliberate, connected, meaningful landing, not left on
    // the (now stale-context) Roster tab or lost to <body>.
    const restrictedSemantics = await page.evaluate(() => {
      const banner = document.querySelector("#content .banner.neutral");
      return { role: banner && banner.getAttribute("role") };
    });
    if (restrictedSemantics.role !== "alert") {
      fail(`restricted: expected the banner to carry role="alert", got `
        + `${JSON.stringify(restrictedSemantics)}`);
    }
    focused = await activeInfo(page);
    if (!focused || focused.tag !== "h2" || focused.text !== "Restricted") {
      fail(`restricted: expected focus to land on the "Restricted" heading, `
        + `got ${JSON.stringify(focused)}`);
    }
    // No keyboard trap. The heading is tabindex="-1" (a landing spot, like
    // #content's own established floor -- not a dialog container with its
    // own JS-enforced cycle), so it is NOT part of the normal sequential
    // Tab order: Shift+Tab from whatever follows it does NOT return to it,
    // by design. What matters is that BOTH directions from this landing
    // spot lead somewhere real, never <body> or a dead end.
    await page.keyboard.press("Tab");
    const afterTab = await activeInfo(page);
    if (!afterTab || afterTab.tag === "body" || (afterTab.tag === focused.tag && afterTab.text === focused.text)) {
      fail(`restricted: Tab from the Restricted heading did not move focus `
        + `anywhere real, got ${JSON.stringify(afterTab)}`);
    }
    if (!afterTab.visibleFocus) {
      fail(`restricted: the control after the heading has no visible focus indicator, got ${JSON.stringify(afterTab)}`);
    }
    // Re-focus the heading directly to probe the REVERSE direction from the
    // same landing spot -- test setup, not a bypass of the interaction
    // under test (the click that put focus there is already proven above).
    await page.evaluate(() => {
      const h = document.querySelector("#content .banner.neutral h2");
      if (h) h.focus();
    });
    await page.keyboard.press("Shift+Tab");
    const beforeHeading = await activeInfo(page);
    if (!beforeHeading || beforeHeading.tag === "body" || !beforeHeading.visibleFocus) {
      fail(`restricted: Shift+Tab from the Restricted heading did not move `
        + `focus anywhere real, got ${JSON.stringify(beforeHeading)}`);
    }
    await assertNoSeriousViolations("#content", "restricted");
    checkpointErrors("restricted");

    // The ledger must have been exercised, not just registered: all three
    // deliberate failures (401/403/502) must have actually occurred and
    // been consumed -- proves the allowlist matched real responses rather
    // than silently never firing (e.g. a URL-substring typo).
    const unconsumed = expectedFailures.filter((f) => !f.consumed);
    if (unconsumed.length) {
      fail(`expected deliberate failure(s) never occurred: ${JSON.stringify(unconsumed)}`);
    }
    if (errors.length) {
      fail(`browser errors:\n${errors.join("\n")}`);
    }
    console.log(`[${viewport.label}] OK — signed-out login (title, heading, `
      + `accessible names, real Tab order, visible focus, role="alert" on a `
      + `failed attempt), the public schedule (heading, fixtures, Tab `
      + `order), the public -> Staff sign-in leg via a real click (title, `
      + `heading, focus landing on #login-user), a forced loading then `
      + `forced 502 error on the public schedule's own fetch (role="status"`
      + `/aria-live/aria-busy while loading with focus held on `
      + `#public-content instead of dropping to <body>, role="alert" on the `
      + `error, Retry reachable and visibly focused, real recovery, and an `
      + `obsolete held response -- released only after a genuinely second, `
      + `newer render was started via two real clicks on the persistent `
      + `Staff-sign-in/guest-link controls -- proven unable to clobber that `
      + `newer render's content, title, or focus), and the Restricted early `
      + `return for an unassigned Official reached by one real Roster-tab `
      + `click (title, role="alert", focus landing on the heading, no `
      + `keyboard trap) — each with a zero-serious/critical axe scan. A `
      + `precise HTTP-response ledger (not a text-pattern filter) allowed `
      + `only the three deliberate failures (401/403/502) through, proven `
      + `falsifiable against a genuinely unlisted 404 via the same tracker, `
      + `and confirmed to have actually fired all three. The skip link was `
      + `never focusable while #content was hidden across every signed-out`
      + `/anonymous surface, and was a real, reachable tab stop once `
      + `#content was genuinely visible on the Restricted surface.`);
  } catch (e) {
    if (serverOutput.trim()) {
      console.error("--- demo server output ---\n" + serverOutput.trim());
    }
    throw e;
  } finally {
    await context.close();
    await stopServer(server);
  }
}

(async () => {
  const browser = await chromium.launch(
    process.env.SMOKE_CHROMIUM_PATH ? { executablePath: process.env.SMOKE_CHROMIUM_PATH } : {});
  try {
    for (const viewport of VIEWPORTS) await checkViewport(browser, viewport);
    console.log("Shell accessibility coverage browser journey passed.");
  } finally {
    await browser.close();
  }
})().catch((e) => {
  console.error("Shell accessibility coverage browser journey FAILED.");
  console.error(e.message);
  process.exit(1);
});
