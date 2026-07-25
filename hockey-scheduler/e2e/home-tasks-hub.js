// Home/Tasks hub setup-progress card (#204/#330; #331 review round 1).
//
// At desktop and 390px: a League Admin whose Program has nothing configured
// yet lands on the existing Initial Setup wizard (#174) first — dismissing
// it reaches Dashboard, which now leads with a "Continue setup" primary
// card naming the actual next incomplete Setup workflow (league profile/
// seasons -> permanent teams -> season participation/divisions -> clubs/
// players/staff -> venues/rinks/ice -> imports/onboarding), with the other
// five listed below as a non-competing secondary list, each with an
// accessible (visible-text, not icon-only) Done/To do status. The primary
// action opens that workflow's REAL entry point (a create drawer, the Ice
// Availability Builder, or the relevant tab) — never a generic Setup
// landing. Once every workflow is done the card shows the required success
// state with a keyboard-operable Schedule link. A failed progress fetch
// shows an error with a working Retry, and out-of-order responses (e.g. a
// slow fetch resolving after a faster, newer one already rendered) never
// clobber the fresher result.
//
// Fails on any browser console/page error.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");
const fs = require("fs");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8295 },
  { label: "phone", width: 390, height: 844, port: 8296 },
];
// The setup-progress card's three possible headings (renderSetupProgressCard
// in app.js) -- shared by cardState() and the automated accessibility scan
// below so both target exactly this card, never any other .dash-card on the
// Dashboard (e.g. "Needs Attention").
const KNOWN_CARD_HEADINGS = [
  "Continue setup",
  "✓ All setup steps complete",
  "Setup progress unavailable",
];
const AXE_PATH = require.resolve("axe-core/axe.min.js");

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

// Read the rendered Home/Tasks card into a plain object for assertions.
function cardState(page) {
  return page.evaluate((headings) => {
    const h3 = Array.from(document.querySelectorAll(".dash-card h3")).find(
      (el) => headings.some((h) => el.textContent.trim().startsWith(h))
    );
    if (!h3) return null;
    const card = h3.closest(".dash-card");
    const primary = card.querySelector(".act.primary");
    const rows = Array.from(card.querySelectorAll(".li")).map((li) => ({
      title: (li.querySelector(".li-title") || {}).textContent || "",
      statusText: (li.querySelector(".badge") || {}).textContent || "",
    }));
    return {
      heading: h3.textContent,
      nextTitle: (card.querySelector(".na-title") || {}).textContent || "",
      nextDetail: (card.querySelector(".na-sub") || {}).textContent || "",
      primaryLabel: primary ? primary.textContent.trim() : null,
      primaryKey: primary ? primary.dataset.setupProgressAction || null : null,
      hasRetry: !!card.querySelector("[data-setup-progress-retry]"),
      rows,
    };
  }, KNOWN_CARD_HEADINGS);
}

// Automated accessibility gate (#331 review round 1 finding 4), scoped to
// exactly the setup-progress card rather than the whole page: the rest of
// the app is #113's separately-tracked, much larger sitewide gate (focus
// trap, skip link, page titling), not this round's scope. Runs axe-core
// against the live card element for whichever of its three states is
// currently rendered, so this exercises the real DOM the other assertions
// just verified rather than a static markup snapshot.
//
// color-contrast and heading-order are disabled deliberately, not by
// omission: both fire here purely from conventions this card reuses as-is
// rather than anything its own new markup introduces -- verified directly
// against the rendered violations, not assumed. color-contrast flags
// `.li-sub`/`.na-sub`/`.section-title` (all `color: var(--muted)`,
// styles.css:492 and siblings) and `.act.primary` (the standing "one
// primary action" button convention, operator-ux-requirements.md #4) --
// the SAME classes every other existing screen's text and primary buttons
// already use in production. heading-order flags this card's <h3>, but
// app.js's OTHER pre-existing Dashboard cards ("Needs Attention", "Quick
// Links", the games/standings cards) all render <h3> as their own first
// heading too, with no page-level <h1>/<h2> before any of them -- a
// sitewide document-structure gap, not something unique to this card. A
// real color-token or heading-hierarchy pass across the app is #113's job.
// Every other rule (labels, ARIA, roles, keyboard, landmarks, etc.) stays
// enabled, so a violation genuinely new to this card's own markup still
// fails this gate.
async function assertCardHasNoA11yViolations(page, viewportLabel, stateLabel) {
  await page.addScriptTag({ url: "/__axe-core__.js" });
  const result = await page.evaluate(async (headings) => {
    const h3 = Array.from(document.querySelectorAll(".dash-card h3")).find(
      (el) => headings.some((h) => el.textContent.trim().startsWith(h)));
    const card = h3 && h3.closest(".dash-card");
    if (!card) return { notFound: true };
    const report = await axe.run(card, {
      resultTypes: ["violations"],
      rules: { "color-contrast": { enabled: false },
        "heading-order": { enabled: false } },
    });
    return { violations: report.violations };
  }, KNOWN_CARD_HEADINGS);
  if (result.notFound) {
    throw new Error(`[${viewportLabel}] ${stateLabel}: setup-progress card `
      + "not found to scan for accessibility violations");
  }
  if (result.violations.length) {
    const details = result.violations.map((v) => `${v.id} (${v.impact}): `
      + `${v.help} [${v.nodes.map((n) => n.target.join(" ")).join(", ")}]`
    ).join("\n");
    throw new Error(`[${viewportLabel}] ${stateLabel}: automated `
      + `accessibility scan found violations:\n${details}`);
  }
}

async function reachDashboard(page) {
  await page.waitForFunction(
    () => document.body.dataset.view === "onboarding"
      || document.body.dataset.view === "dashboard", null, { timeout: 10000 });
  if (await page.evaluate(() => document.body.dataset.view) === "onboarding") {
    await page.click('[data-onboarding-goto="dashboard"]');
  }
  await page.waitForFunction(
    () => document.body.dataset.view === "dashboard", null, { timeout: 10000 });
}

// Wait past the Dashboard's loading skeleton for the real card (any h3) or,
// once setup is fully done, for the plain Dashboard content with no card.
async function waitForCardSettled(page) {
  await page.waitForFunction(() => {
    if (document.querySelector(".dash-card h3")) return true;
    return !!document.querySelector(".dash-stats");  // settled with no card
  }, null, { timeout: 10000 });
}

// Destination focus management (#331 review round 1 finding 4): a non-drawer
// destination (Ice Builder, the Setup hierarchy tree, Import) has no auto-
// focus of its own, so goToSetupWorkflow calls focusContentHeading(), which
// lands keyboard focus on the destination's own heading via a tabindex="-1"
// element inside #content. Polls rather than checking once immediately:
// focusContentHeading() schedules via setTimeout(0), which races the
// destination view's own async render (its overview fetch) -- if that race
// is lost, the focused element the timeout finds gets detached when the
// content is later replaced, and focus resets to <body> for good, so a
// bounded poll surfaces that failure clearly instead of racily passing.
async function assertFocusLandedInContent(page) {
  await page.waitForFunction(() => {
    const active = document.activeElement;
    const content = document.getElementById("content");
    return !!(active && active.getAttribute("tabindex") === "-1"
      && content && content.contains(active));
  }, null, { timeout: 10000 });
}

async function apiPost(page, p, body) {
  return page.evaluate(async (arg) => (await fetch(arg.p, {
    method: "POST", credentials: "same-origin",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(arg.body),
  })).json(), { p, body });
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
  // The app enforces a strict script-src 'self' CSP, which blocks
  // page.addScriptTag({path}) (it inlines the file as a <script> body) --
  // serve axe-core from a same-origin URL instead so the injected <script
  // src> satisfies 'self'. Registered once; persists across this page's
  // later page.goto() navigations.
  const axeSource = fs.readFileSync(AXE_PATH, "utf8");
  await page.route("**/__axe-core__.js", (route) => route.fulfill({
    status: 200, contentType: "application/javascript", body: axeSource,
  }));

  const fail = (msg) => { throw new Error(`[${viewport.label}] ${msg}`); };

  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });
    await reachDashboard(page);

    // (1) No Program exists at all yet -> no card (bootstrapping the very
    // first Program is the onboarding wizard's job, not this card's).
    if (await cardState(page) !== null) {
      fail("expected no setup-progress card before any Program exists");
    }

    await page.evaluate(async () => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      await post("/api/v2/setup/program", { name: "Riverside Hockey", country: "US" });
    });
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await reachDashboard(page);
    await waitForCardSettled(page);

    // (2) A Program with nothing else configured -> five workflows todo,
    // primary action names the first one, each row shows accessible
    // (visible-text) status, not just an icon (#331 review round 1
    // finding 4). "Imports and onboarding" reads "Optional", never "To do"
    // or "Done" -- it has no real completion signal of its own and must
    // never be invented from the other five's state (#331 review round 1
    // finding 5).
    let s = await cardState(page);
    if (!s) fail("setup-progress card did not render for a fresh, unconfigured Program");
    if (s.nextTitle !== "League profile and seasons") {
      fail(`expected next = "League profile and seasons", got ${JSON.stringify(s)}`);
    }
    if (s.primaryLabel !== "Add Season") {
      fail(`expected primary action "Add Season", got ${JSON.stringify(s)}`);
    }
    const importRow = s.rows.find((r) => r.title === "Imports and onboarding");
    if (s.rows.length !== 6 || !importRow || importRow.statusText !== "Optional"
        || s.rows.some((r) => r.title !== "Imports and onboarding" && r.statusText !== "To do")) {
      fail(`expected five "To do" rows plus an "Optional" import row, `
        + `got ${JSON.stringify(s.rows)}`);
    }
    await assertCardHasNoA11yViolations(page, viewport.label,
      "normal 'Continue setup' state");

    // "Imports and onboarding" stays reachable the whole time via its own
    // persistent nav tab (#331 review round 1 finding 5) even though it can
    // never be the card's primary action -- confirm the destination is real.
    await page.click('.tab[data-tab="import"]');
    await page.waitForSelector("[data-import-type]", { timeout: 10000 });
    await page.click('.tab[data-tab="dashboard"]');
    await reachDashboard(page);

    // (3) The primary action for "league profile and seasons" opens the
    // Season create drawer directly (#331 review round 1 finding 3) — not
    // just a generic landing on Setup — with focus already on its first
    // field (existing drawer-open behavior).
    await page.click("[data-setup-progress-action]");
    await page.waitForSelector(".drawer[role=dialog]", { timeout: 10000 });
    const seasonDrawerOk = await page.evaluate(() => {
      const d = document.querySelector(".drawer[role=dialog]");
      const active = document.activeElement;
      return {
        title: (d.querySelector("h2, .drawer-head") || {}).textContent || "",
        focusInsideDrawer: !!(active && d.contains(active)),
      };
    });
    if (!/season/i.test(seasonDrawerOk.title)) {
      fail(`expected the Season drawer to open, got title "${seasonDrawerOk.title}"`);
    }
    if (!seasonDrawerOk.focusInsideDrawer) {
      fail("expected focus to land inside the opened Season drawer");
    }
    await page.keyboard.press("Escape");
    await page.click('.tab[data-tab="dashboard"]');
    await reachDashboard(page);

    // (4) Build league/season/team via the documented API and confirm the
    // card advances to "teams" done / "participation" next.
    const ids = await page.evaluate(async () => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      const overview = await (await fetch("/api/v2/setup/overview",
        { credentials: "same-origin" })).json();
      const program = overview.programs[0];
      const season = await post("/api/v2/setup/season",
        { program_id: program.id, name: "Fall 2026" });
      const league = await post("/api/v2/setup/league",
        { season_id: season.id, name: "Adult League" });
      const club = await post("/api/v2/setup/club", { name: "Club" });
      const team = await post("/api/v2/setup/team",
        { club_id: club.id, league_id: league.id, name: "Team A" });
      return { programId: program.id, seasonId: season.id, leagueId: league.id,
        teamId: team.id };
    });
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await reachDashboard(page);
    await waitForCardSettled(page);
    s = await cardState(page);
    if (!s || s.nextTitle !== "Season participation and divisions") {
      fail(`after season/league/team: expected next = "Season participation `
        + `and divisions", got ${JSON.stringify(s)}`);
    }
    if (s.rows.find((r) => r.title === "League profile and seasons").statusText !== "Done"
        || s.rows.find((r) => r.title === "Permanent teams").statusText !== "Done") {
      fail(`after season/league/team: expected first two rows Done, got ${JSON.stringify(s.rows)}`);
    }

    // (5) "Season participation" has no dedicated drawer (it's an inline
    // action inside the Setup hierarchy tree) — its primary action must
    // still land precisely on that tree, not merely "somewhere in Setup".
    await page.click("[data-setup-progress-action]");
    await page.waitForFunction(
      () => document.body.dataset.view === "setup", null, { timeout: 10000 });
    // The Setup hierarchy tree loads its own overview data after the view
    // switch — wait for the real tree, not the moment view flips.
    await page.waitForFunction(
      () => !!document.querySelector("[data-reg-add]")
        || document.body.textContent.includes("Season participation"),
      null, { timeout: 10000 });
    // Destination focus management (#331 review round 1 finding 4): a plain
    // view switch with no drawer of its own to auto-focus must still land
    // keyboard focus somewhere real, not leave it on the (now-gone) card.
    await assertFocusLandedInContent(page);
    await page.click('.tab[data-tab="dashboard"]');
    await reachDashboard(page);

    // (6) Register the team, add a player -> roster becomes next; its
    // primary action opens the Player create drawer directly.
    await apiPost(page, `/api/v2/setup/seasons/${ids.seasonId}/team-registrations`,
      { team_id: ids.teamId, league_id: ids.leagueId });
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await reachDashboard(page);
    await waitForCardSettled(page);
    s = await cardState(page);
    if (!s || s.nextTitle !== "Clubs, players and staff") {
      fail(`after registration: expected next = "Clubs, players and staff", `
        + `got ${JSON.stringify(s)}`);
    }
    await page.click("[data-setup-progress-action]");
    await page.waitForSelector(".drawer[role=dialog]", { timeout: 10000 });
    const playerDrawerTitle = await page.evaluate(() =>
      ((document.querySelector(".drawer[role=dialog] h2, .drawer[role=dialog] .drawer-head")
        || {}).textContent) || "");
    if (!/player/i.test(playerDrawerTitle)) {
      fail(`expected the Player drawer to open, got title "${playerDrawerTitle}"`);
    }
    await page.keyboard.press("Escape");
    await page.click('.tab[data-tab="dashboard"]');
    await reachDashboard(page);

    // (7) Add a player -> facilities becomes next; its primary action opens
    // the Ice Availability Builder specifically, which renders from the
    // Calendar view, not Setup (#331 review round 1 finding 3 — this is the
    // exact destination an Arena Manager needs to be able to execute).
    await apiPost(page, "/api/v2/setup/player",
      { team_id: ids.teamId, name: "Vince Skater", position: "forward" });
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await reachDashboard(page);
    await waitForCardSettled(page);
    s = await cardState(page);
    if (!s || s.nextTitle !== "Venues, rinks and ice") {
      fail(`after player: expected next = "Venues, rinks and ice", got ${JSON.stringify(s)}`);
    }
    if (s.primaryLabel !== "Add Ice") {
      fail(`expected primary action "Add Ice", got ${JSON.stringify(s)}`);
    }
    await page.click("[data-setup-progress-action]");
    await page.waitForFunction(
      () => document.body.dataset.view === "calendar", null, { timeout: 10000 });
    // The calendar view's own async render (fetching /api/demo/overview) has
    // not necessarily painted the Ice Builder markup the instant the view
    // attribute flips -- wait for the real element, not just the view switch.
    try {
      await page.waitForSelector(".ib-wrap", { timeout: 10000 });
    } catch {
      fail("expected the Ice Availability Builder to open on the Calendar view");
    }
    // Destination focus management (#331 review round 1 finding 4): the Ice
    // Builder is not a drawer, so it gets no auto-focus for free — its own
    // heading must still receive keyboard focus explicitly.
    await assertFocusLandedInContent(page);
    await page.click('.tab[data-tab="dashboard"]');
    await reachDashboard(page);

    // (8) Finish facilities via the API, reaching full completion -> the
    // required success state with a keyboard-operable Schedule link.
    await page.evaluate(async () => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      const venue = await post("/api/v2/setup/venue",
        { name: "Arena", organization_id: null });
      const rink = await post("/api/v2/setup/rink",
        { venue_id: venue.id, name: "Rink 1" });
      await post("/api/v2/setup/ice-slot", {
        rink_id: rink.id, start_time: "2026-09-01T18:30:00+00:00",
        end_time: "2026-09-01T20:00:00+00:00", slot_type: "game" });
      return venue;
    });
    await apiPost(page, `/api/v2/setup/seasons/${ids.seasonId}/venue-access`, {
      venue_id: (await page.evaluate(() =>
        fetch("/api/v2/setup/overview", { credentials: "same-origin" })
          .then((r) => r.json()).then((ov) => ov.venues[0].id))),
    });
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await reachDashboard(page);
    await waitForCardSettled(page);
    s = await cardState(page);
    if (!s || s.heading !== "✓ All setup steps complete") {
      fail(`expected the complete-state heading, got ${JSON.stringify(s)}`);
    }
    if (s.primaryLabel !== "Go to Schedule") {
      fail(`expected the "Go to Schedule" action, got ${JSON.stringify(s)}`);
    }
    await assertCardHasNoA11yViolations(page, viewport.label, "complete state");
    // Keyboard-operable: Tab to it and activate with Enter, not just click.
    await page.evaluate(() => {
      const b = Array.from(document.querySelectorAll(".dash-card .act.primary"))
        .find((x) => x.textContent.trim() === "Go to Schedule");
      b.focus();
    });
    await page.keyboard.press("Enter");
    await page.waitForFunction(
      () => document.body.dataset.view === "calendar", null, { timeout: 10000 });
    await page.click('.tab[data-tab="dashboard"]');
    await reachDashboard(page);

    // (9) Error + retry: a failed fetch shows the error state with a
    // working Retry, not a silently-vanished card (#331 review round 1
    // finding 4).
    // A body of "{}" would be indistinguishable from a real (if odd) success
    // response -- readApiResponse() returns any successfully-parsed JSON body
    // regardless of HTTP status, matching how a real 500 actually looks on
    // this API (`{"error": {"code": ..., "message": ...}}`, e.g. server.py's
    // demo_reset_failed). Only a body shaped like that trips setupProgressError.
    await page.route("**/api/v2/setup/progress", (route) => route.fulfill({
      status: 500, contentType: "application/json",
      body: JSON.stringify({ error: { code: "server_unavailable",
        message: "The server is temporarily unavailable." } }),
    }));
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await reachDashboard(page);
    await page.waitForSelector("[data-setup-progress-retry]", { timeout: 10000 });
    await assertCardHasNoA11yViolations(page, viewport.label, "error state");
    await page.unroute("**/api/v2/setup/progress");
    await page.click("[data-setup-progress-retry]");
    await waitForCardSettled(page);
    s = await cardState(page);
    if (!s || s.heading !== "✓ All setup steps complete") {
      fail(`expected Retry to recover the real (complete) state, got ${JSON.stringify(s)}`);
    }
    // The deliberately-injected 500 above is expected to log a browser
    // resource-load console error (Chromium logs any non-2xx fetch this way
    // regardless of whether app code handles it gracefully) -- possibly more
    // than one, since app.js's bootstrap can race a second render() (see
    // reachDashboard's onboarding-vs-dashboard race). Require at least one,
    // proving the mock actually fired, then drop every matching message so
    // any other, genuinely unexpected error still fails below.
    const unexpected = errors.filter((e) => !/responded with a status of 500/.test(e));
    if (unexpected.length === errors.length) {
      fail("expected the deliberately failed setup-progress request to log "
        + `a resource error, got:\n${errors.join("\n")}`);
    }
    errors.length = 0;
    errors.push(...unexpected);

    // (10) Stale-response guard: an OLDER setup-progress request that
    // resolves AFTER a NEWER one (e.g. a slow fetch outlasting a fast
    // context switch) must never clobber the fresher, already-rendered
    // result (#331 review round 1 finding 4's "request-generation/context
    // validation" -- setupProgressFetchSeq in app.js). Route the first
    // intercepted request to a deliberately delayed STALE payload and the
    // second to an immediate FRESH one, then fire two renders back to back
    // so the delayed, older request is still in flight when the fast,
    // newer one already wins.
    let progressRequestCount = 0;
    await page.route("**/api/v2/setup/progress", async (route) => {
      progressRequestCount += 1;
      const stale = progressRequestCount === 1;
      if (stale) await new Promise((r) => setTimeout(r, 1200));
      await route.fulfill({
        status: 200, contentType: "application/json",
        body: JSON.stringify({
          program_id: stale ? "stale-prog" : "fresh-prog",
          program: { id: stale ? "stale-prog" : "fresh-prog",
            name: stale ? "Stale" : "Fresh" },
          workflows: [],
          next: { key: "league_season",
            label: stale ? "STALE-MUST-NOT-SHOW" : "FRESH-MUST-SHOW",
            detail: "x", primary_action: "Add Season" },
          complete: false,
        }),
      });
    });
    await page.evaluate(() => { switchTab("dashboard"); switchTab("dashboard"); });
    await page.waitForFunction(
      () => (document.querySelector(".dash-card .na-title") || {}).textContent
        === "FRESH-MUST-SHOW",
      null, { timeout: 5000 });
    // Give the delayed first (stale) response its full window to resolve and
    // confirm it did NOT overwrite the fresh result once it lands late.
    await new Promise((r) => setTimeout(r, 1500));
    s = await cardState(page);
    if (s.nextTitle !== "FRESH-MUST-SHOW") {
      fail(`a late-arriving stale response clobbered the fresher one, `
        + `got ${JSON.stringify(s)}`);
    }
    await page.unroute("**/api/v2/setup/progress");

    if (errors.length) {
      fail(`console/page errors:\n${errors.join("\n")}`);
    }
    console.log(`[${viewport.label}] OK — Home/Tasks hub card names the next `
      + `incomplete Setup workflow with an accessible status per row, opens `
      + `each workflow's real entry point, shows the required complete state `
      + `with a keyboard-operable Schedule link, and recovers via Retry from `
      + `a failed fetch.`);
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
    console.log("Home/Tasks hub browser journey passed.");
  } catch (error) {
    console.error("Home/Tasks hub browser journey FAILED.");
    console.error(error && error.message ? error.message : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
