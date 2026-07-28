// Automated axe-core CI gate for shell + guided Setup surfaces (#345/#359).
//
// A baseline, always-run accessibility GATE, distinct from (and not a
// replacement for) the richer behavioral journeys that already exist:
// home-tasks-hub.js already scans the Home/Tasks setup-progress card;
// shell-accessibility-coverage.js already scans login/public/forced-error/
// restricted and asserts their tab order, accessible names, and focus
// management in depth. This file does not remove or duplicate that depth --
// it owns ONE list of every surface #359 names, reaches each through the
// same real production flow, and requires: zero serious/critical axe
// violations, zero console/page errors, no dangling skip-link target (a
// skip link focusable/visible while its #content target is hidden), no
// stale page title, and no HIDDEN focused control (activeElement present
// but with an empty client-rect list). It does NOT assert manual keyboard/
// screen-reader behavior or the seven-area IA -- that is explicitly
// out of scope per #359's own "done condition".
//
// Surfaces scanned, each via a real click/navigation:
//   1. Signed-out login (real Sign-out click).
//   2. Anonymous public schedule (real "View public schedule" click).
//   3. Authenticated Home/Tasks (Dashboard).
//   4. The Setup hub (six-workflow index).
//   5-10. Each of the six Setup workflow landing screens
//      (league_season, teams, participation, roster, facilities, import).
//   11. Forced error: the public schedule's own fetch made to 502 via a
//       real click that re-triggers it (segment switch), not a synthetic
//       replay.
//   12. Restricted early-return: an Official account with zero assignments
//       lands there from one real "Roster" nav click.
//
// A SURFACES registry (not ad-hoc calls) tracks which of these were
// actually scanned -- a missing selector/route throws immediately rather
// than silently skipping, and the run asserts the full set was covered
// before declaring success. Runs at desktop (1440x900) and canonical
// 390x844. Registered in the existing browser-smoke shard arrangement; no
// new workflow, no new dependency (axe-core is already an e2e devDependency).
//
// Falsifiability (#359's own required method): a temporary, uncommitted
// production-markup edit removes an accessible name from one authenticated
// surface (the Setup hub) and one signed-out surface (the login screen);
// this journey is run against that edit to confirm the gate reports the
// EXACT violating node on both, then the edit is reverted (`git checkout`,
// confirmed clean) and the gate re-run to confirm both viewports pass again
// on the real, unmodified markup. A second falsifiability leg covers the
// scoped color-contrast fix itself (styles.css): temporarily revert ONLY
// that fix, confirm this same gate fails with the exact previously-reported
// Dashboard/Setup selectors and contrast ratio, then restore it and confirm
// both viewports pass again. The unexpected-HTTP-failure ledger has its own,
// separate self-test: an unregistered 404 must be caught by the same
// tracker the real run uses.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");
const fs = require("fs");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8351 },
  { label: "phone", width: 390, height: 844, port: 8352 },
];
const AXE_PATH = require.resolve("axe-core/axe.min.js");
const BAD_GATEWAY = { status: 502, contentType: "text/html", body: "<html><body>502 Bad Gateway</body></html>" };
const APP_TITLE = "Hockey Scheduler";

// Must match setup-workflow-hub.js's own WORKFLOWS keys exactly -- a
// renamed/removed workflow key fails this file loudly (via the coverage
// assertion at the end) rather than silently scanning fewer than six.
const WORKFLOW_KEYS = ["league_season", "teams", "participation", "roster", "facilities", "import"];

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

// Poll rather than a one-shot read: setPageTitle()/setShellTitle() run
// synchronously within their own transition, but a preceding async op this
// journey just triggered can still be settling when the next check fires.
async function waitForTitle(page, expected, fail, label) {
  await page.waitForFunction((t) => document.title === t, expected, { timeout: 10000 })
    .catch(async () => fail(`${label}: expected title "${expected}", got "${await page.title()}"`));
}

// Same "dangling skip link" defect accessibility-foundations.js/
// shell-accessibility-coverage.js already guard against: a skip link that
// is visible OR programmatically focusable while its own #content target is
// hidden is unusable at best, a false affordance at worst. Restores whatever
// had focus before the probe so it never disturbs a later assertion.
async function skipLinkDangling(page) {
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
    return (visible(sk) || becameFocused) && !visible(content);
  });
}

// A focused-but-invisible control: activeElement present, non-body, yet its
// own client-rect list is empty (display:none, zero-size, or detached).
async function hiddenFocusedControl(page) {
  return page.evaluate(() => {
    const el = document.activeElement;
    if (!el || el === document.body) return false;
    return el.getClientRects().length === 0;
  });
}

// axe-core, served from a same-origin URL (CSP is script-src 'self', so
// addScriptTag({path}) -- which inlines the file body -- is blocked); same
// technique as home-tasks-hub.js/shell-accessibility-coverage.js. Filters to
// serious/critical only, per #359's own stated bar.
async function seriousOrCriticalViolations(page, selector) {
  await page.addScriptTag({ url: "/__axe-core__.js" });
  return page.evaluate(async (sel) => {
    const root = sel ? document.querySelector(sel) : document;
    if (!root) return [{ id: "selector-not-found", impact: "critical", help: `"${sel}" not found`, nodes: [] }];
    const report = await axe.run(root, { resultTypes: ["violations"] });
    return report.violations.filter((v) => v.impact === "serious" || v.impact === "critical")
      .map((v) => ({ id: v.id, impact: v.impact, help: v.help, nodes: v.nodes.map((n) => n.target.join(" ")) }));
  }, selector);
}

// A precise error ledger, not a text-pattern blanket filter: this journey
// deliberately provokes exactly two real HTTP failures (the Restricted
// surface's own 403 scope check, a forced public 502). expectFailure() must
// be called, by exact method/URL-substring/status, immediately before each
// deliberate action; only THAT exact response is consumed. Any OTHER
// response >=400 -- an unrelated 404/500 -- fails the run. Chromium mirrors
// any >=400 response into the console as "Failed to load resource"; that is
// the SAME event the response listener already captures precisely, so it is
// not double-counted, but a genuine console.error(...) or uncaught
// exception still is.
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

// Falsifiability for the HTTP-failure ledger: an injected, UNLISTED 404 must
// still be caught by the exact same tracker the real run below uses -- no
// expectFailure() registers it, so if the tracker were a blanket filter this
// would silently vanish instead of being flagged.
async function verifyLedgerFalsifiability(browser, base, viewportLabel, fail) {
  const context = await browser.newContext();
  const page = await context.newPage();
  const { errors } = wireErrorTracking(page);
  const SENTINEL = "/api/__axe-gate-ledger-selftest__";
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
      + `was not flagged by the same tracker the real run uses -- the `
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

  // Every surface actually reached is recorded here by name -- asserted as
  // a complete set at the end, so a silently-skipped surface (a selector
  // that never appears, swallowed by a stray .catch()) fails the run.
  const scanned = new Set();

  // The one shared gate every surface below runs through: axe (serious/
  // critical only) + the four generic checks #359 requires. `selector` is
  // the root axe scans (never `document` for an authenticated surface --
  // scoping to #content/a specific surface root keeps a violation
  // elsewhere in the shell from masking which surface actually owns it).
  const scanSurface = async (name, selector, expectedTitle) => {
    scanned.add(name);
    await waitForTitle(page, expectedTitle, fail, name);
    if (await skipLinkDangling(page)) {
      fail(`${name}: skip link is focusable/visible while its #content target is hidden`);
    }
    if (await hiddenFocusedControl(page)) {
      const info = await page.evaluate(() => {
        const el = document.activeElement;
        return { tag: el.tagName, id: el.id, cls: el.className };
      });
      fail(`${name}: focus is on a hidden control -- ${JSON.stringify(info)}`);
    }
    const violations = await seriousOrCriticalViolations(page, selector);
    if (violations.length) {
      fail(`${name}: automated accessibility scan found serious/critical `
        + `violations:\n${violations.map((v) =>
          `${v.id} (${v.impact}): ${v.help} [${v.nodes.join(", ")}]`).join("\n")}`);
    }
    checkpointErrors(name);
  };

  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await verifyLedgerFalsifiability(browser, base, viewport.label, fail);

    await page.goto(base, { waitUntil: "domcontentloaded" });
    // A truly fresh boot (no Program yet) lands on the one-time Initial
    // Setup wizard, not the Dashboard -- dismiss it the same way every
    // other journey in this suite does.
    await page.waitForFunction(
      () => document.body.dataset.view === "onboarding" || document.body.dataset.view === "dashboard",
      null, { timeout: 15000 });
    if (await page.evaluate(() => document.body.dataset.view) === "onboarding") {
      await page.click('[data-onboarding-goto="dashboard"]');
    }
    await page.waitForFunction(() => document.body.dataset.view === "dashboard", null, { timeout: 10000 });
    await page.waitForSelector("#content .dash-card, #content h2, #content h3", { timeout: 15000 });
    checkpointErrors("initial authenticated boot");

    // Real fixture data so the public schedule has real content, and a real
    // unassigned Official for the Restricted surface.
    await apiPost(page, "/api/demo/load", {});
    const official = await apiPost(page, "/api/v2/setup/official", { name: "Axe Gate No-Assignment" });
    const officialUsername = `axe_gate_official_${viewport.port}`;
    const officialAcct = await apiPost(page, "/api/accounts", {
      username: officialUsername, password: "scoped-account-pw",
      role: "official", scope: { official_id: official.id },
    });
    if (officialAcct.status && officialAcct.error) {
      fail(`could not create the unassigned Official account: ${JSON.stringify(officialAcct)}`);
    }

    // ---- (3) Authenticated Home/Tasks (Dashboard) -------------------------
    // --muted:#8e8e93 (styles.css:5, 3.26:1 on white) previously failed
    // color-contrast here (.ds-label/.ds-sub/.dch-sub/.na-empty) -- fixed by
    // a scoped body[data-view="dashboard"] .main > .content override
    // reusing the same verified --muted:#6b6b70 the setup-progress card and
    // login/public screens already use (styles.css). No exclusion: this
    // rule runs unmodified.
    await scanSurface("Home/Tasks (Dashboard)", "#content", `Dashboard — ${APP_TITLE}`);

    // ---- (4)-(10) Setup hub + all six workflow landings --------------------
    await page.click('.tab[data-tab="setup"]');
    await page.waitForFunction(() => document.body.dataset.view === "setup", null, { timeout: 10000 });
    await page.click('[data-setup-view="hub"]');
    await page.waitForSelector(".swf-grid", { timeout: 10000 });
    // Same --muted fix, scoped to .swf-grid (styles.css) -- no exclusion.
    await scanSurface("Setup hub", "#content", `Setup — ${APP_TITLE}`);

    const foundCardKeys = await page.$$eval("[data-setup-workflow-card]",
      (els) => els.map((e) => e.dataset.setupWorkflowCard));
    for (const key of WORKFLOW_KEYS) {
      if (!foundCardKeys.includes(key)) {
        fail(`Setup hub: workflow "${key}" is missing from the hub -- `
          + `found ${JSON.stringify(foundCardKeys)}`);
      }
      await page.waitForSelector(`[data-setup-workflow="${key}"]`, { timeout: 10000 });
      await page.click(`[data-setup-workflow="${key}"]`);
      await page.waitForSelector(".swf-landing", { timeout: 10000 });
      // Same --muted fix, scoped to .swf-landing (styles.css) -- no exclusion.
      await scanSurface(`Setup workflow landing: ${key}`, ".swf-landing", `Setup — ${APP_TITLE}`);
      await page.click("[data-setup-workflow='']"); // back to the hub
      await page.waitForSelector(".swf-grid", { timeout: 10000 });
    }
    // All six keys unique and every one actually scanned (not a duplicate
    // key silently standing in for a missing one).
    const uniqueKeys = new Set(WORKFLOW_KEYS);
    if (uniqueKeys.size !== 6 || uniqueKeys.size !== foundCardKeys.length
        || !WORKFLOW_KEYS.every((k) => foundCardKeys.includes(k))) {
      fail(`Setup workflow keys are not exactly six unique, matching keys -- `
        + `expected ${JSON.stringify(WORKFLOW_KEYS)}, hub has `
        + `${JSON.stringify(foundCardKeys)}`);
    }

    const publicSchedule = await apiGet(page, "/api/public/schedule");
    const expectedPublicHeading = publicSchedule.league_name || "Program";

    // ---- (1) Signed-out login screen, via a real Sign-out click -----------
    // Proves this scan runs OUTSIDE the authenticated shell: body carries
    // "signed-out" and the shell console itself is hidden.
    await page.click("#signout-btn");
    await page.waitForFunction(() => document.body.classList.contains("signed-out"),
      null, { timeout: 10000 });
    const loginOutsideShell = await page.evaluate(() => ({
      signedOut: document.body.classList.contains("signed-out"),
      shellHidden: (() => {
        const shell = document.querySelector(".web");
        return !shell || shell.getClientRects().length === 0;
      })(),
    }));
    if (!loginOutsideShell.signedOut || !loginOutsideShell.shellHidden) {
      fail(`login screen: expected to be outside the authenticated shell, `
        + `got ${JSON.stringify(loginOutsideShell)}`);
    }
    await scanSurface("Signed-out login", "#login-screen", `Sign in — ${APP_TITLE}`);

    // ---- (2) Anonymous public schedule, via a real "View public schedule"
    // click -- also proves this scan runs outside the authenticated shell.
    await page.click("#guest-public-link");
    await page.waitForFunction(() => !document.getElementById("public-screen").hidden,
      null, { timeout: 10000 });
    await page.waitForSelector(".hero h2", { timeout: 10000 });
    const publicOutsideShell = await page.evaluate(() => {
      const shell = document.querySelector(".web");
      return !shell || shell.getClientRects().length === 0;
    });
    if (!publicOutsideShell) fail("public schedule: expected the authenticated shell to stay hidden");
    const publicHeadingOk = await page.evaluate((expected) => {
      const h = document.querySelector(".hero h2");
      return !!(h && h.textContent.trim() === expected);
    }, expectedPublicHeading);
    if (!publicHeadingOk) fail(`public schedule: expected the real "${expectedPublicHeading}" heading`);
    await scanSurface("Anonymous public schedule", "#public-screen", `Public Schedule — ${APP_TITLE}`);

    // ---- (11) Forced error: the public schedule's own fetch, via a real
    // click that re-triggers it (segment switch) --------------------------
    expectFailure("GET", "/api/public/schedule", 502);
    await page.route("**/api/public/schedule", (route) => route.fulfill(BAD_GATEWAY));
    await page.click('[data-public-tab="standings"]');
    await page.waitForSelector("#public-retry-btn", { timeout: 10000 });
    await scanSurface("Forced public-schedule error (502)", "#public-screen", `Public Schedule — ${APP_TITLE}`);
    await page.unroute("**/api/public/schedule");
    await page.click("#public-retry-btn");
    await page.waitForFunction(() => !document.querySelector("#public-retry-btn")
      && !document.querySelector("#public-content .skeleton"), null, { timeout: 10000 });
    await page.waitForSelector(".hero h2", { timeout: 10000 });
    checkpointErrors("forced error recovery");

    // ---- (12) Restricted early-return, via a real "Roster" nav click ------
    await page.click("#public-signin-link");
    await page.waitForFunction(() => !document.getElementById("login-screen").hidden,
      null, { timeout: 10000 });
    checkpointErrors("public -> staff sign-in (return for Restricted)");
    await page.fill("#login-user", officialUsername);
    await page.fill("#login-pass", "scoped-account-pw");
    await page.click(".login-submit");
    await page.waitForFunction(() => !document.body.classList.contains("signed-out"),
      null, { timeout: 10000 });
    // A fresh navigation, not the in-app no-reload transition: signing OUT
    // just before this (an anonymous/no-permission pseudo-identity) already
    // bounced the SPA's internal view state to "standings" -- its own
    // landing for a role with no ops-console access -- and none of
    // setUser()'s later per-role redirects (Official -> inbox, Player ->
    // player_home, etc.) fire from "standings", only from "dashboard". A
    // real reload re-derives the view from this account's own session via
    // bootstrap() instead of inheriting that leftover state.
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForFunction(() => document.body.dataset.view === "inbox", null, { timeout: 10000 });
    checkpointErrors("official sign-in");
    const rosterTab = await page.$('.tab[data-tab="roster"]');
    if (!rosterTab) fail("restricted: could not find the Roster nav tab");
    expectFailure("GET", "/lineups", 403);
    await rosterTab.click();
    await page.waitForFunction(() => document.body.dataset.view === "roster", null, { timeout: 10000 });
    await page.waitForSelector("#content .banner.neutral h2", { timeout: 10000 });
    const restrictedInsideShell = await page.evaluate(() => {
      const content = document.getElementById("content");
      return !!(content && content.getClientRects().length > 0);
    });
    if (!restrictedInsideShell) fail("restricted: expected #content visible in the authenticated shell");
    await scanSurface("Restricted early-return", "#content", `Roster — ${APP_TITLE}`);

    // The ledger must have been exercised, not just registered.
    const unconsumed = expectedFailures.filter((f) => !f.consumed);
    if (unconsumed.length) {
      fail(`expected deliberate failure(s) never occurred: ${JSON.stringify(unconsumed)}`);
    }
    // Every named surface was actually scanned -- 12 total: login, public,
    // Home/Tasks, the hub, six workflow landings, forced error, restricted.
    const expectedSurfaceCount = 1 + 1 + 1 + 1 + WORKFLOW_KEYS.length + 1 + 1;
    if (scanned.size !== expectedSurfaceCount) {
      fail(`expected exactly ${expectedSurfaceCount} surfaces scanned, got `
        + `${scanned.size}: ${JSON.stringify(Array.from(scanned))}`);
    }
    if (errors.length) {
      fail(`browser errors:\n${errors.join("\n")}`);
    }
    console.log(`[${viewport.label}] OK — zero serious/critical axe violations, `
      + `zero unexpected console/page/HTTP errors, no dangling skip-link `
      + `target, no stale page title, and no hidden focused control across `
      + `all ${scanned.size} surfaces: signed-out login, the anonymous `
      + `public schedule, authenticated Home/Tasks, the Setup hub, all six `
      + `Setup workflow landings, a forced public-schedule 502, and the `
      + `Restricted early-return for an unassigned Official.`);
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
    console.log("Setup accessibility axe gate browser journey passed.");
  } finally {
    await browser.close();
  }
})().catch((e) => {
  console.error("Setup accessibility axe gate browser journey FAILED.");
  console.error(e.message);
  process.exit(1);
});
