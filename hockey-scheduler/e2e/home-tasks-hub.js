// Home/Tasks hub setup-progress card (#204/#330; #331 review rounds 1-3).
//
// checkViewport (League Admin, single Program, desktop + 390px): a League
// Admin whose Program has nothing configured yet lands on the existing
// Initial Setup wizard (#174) first — dismissing it reaches Dashboard,
// which now leads with a "Continue setup" primary card naming the actual
// next incomplete Setup workflow (league profile/seasons -> permanent
// teams -> season participation/divisions -> clubs/players/staff ->
// venues/rinks/ice -> imports/onboarding), with the other five listed
// below as a non-competing secondary list, each with an accessible
// (visible-text, not icon-only) Done/To do/Optional status. The primary
// action opens that workflow's REAL entry point (a create drawer, the Ice
// Availability Builder, the exact Register control for the active Season,
// or the relevant tab) — never a generic Setup landing, and leaving the
// Ice Availability Builder via any path other than its own Cancel/commit
// (e.g. Home) never leaves it live for a later "Go to Schedule" to reopen.
// Once every REQUIRED workflow is done the card shows the required success
// state with a keyboard-operable Schedule link, alongside a secondary
// Import action so the always-optional sixth workflow stays reachable even
// then. The card's own fetch loads independently of the rest of the
// Dashboard (a loading skeleton in its own slot, never blocking the rest
// of the page) and an automated accessibility scan (color-contrast
// included, no suppression) gates its states. A failed progress fetch
// shows an error with a working Retry, and out-of-order responses (e.g. a
// slow fetch resolving after a faster, newer one already rendered) never
// clobber the fresher result.
//
// checkRoleScenarios (League Admin vs Arena Manager, desktop + 390px):
// registering a team into one Season never reads or submits a DIFFERENT
// Season's own "Register" row, even when both share the same permanent
// League; Arena Manager's `workflows`/`next` are redacted to only what
// MANAGE_ARENA can manage, both when that's genuinely executable and when
// it's blocked on an unmet Season prerequisite (no Season resolved, or the
// resolved one archived) — a blocked workflow surfaces actionable
// guidance, never a CTA that would just fail; and the complete state's
// secondary Import action (MANAGE_SETUP-only) is visible to League Admin
// but withheld from Arena Manager, while both keep the one "Go to
// Schedule" primary action.
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
  // The loading state's own heading (#331 review round 5 finding 5) --
  // previously heading-less and so structurally unscannable by both
  // cardState() and the axe helper below; now a first-class state.
  "Setup progress",
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
// color-contrast is enabled (#331 review round 3 finding 4 explicitly
// rejected suppressing it — reusing a sitewide convention doesn't waive
// WCAG AA for THIS card's own surface): the card's CSS now carries scoped
// .sp-card overrides (styles.css) for every color axe actually flagged
// here (--muted, button.primary, the "Done"/"To do" badges,
// .banner.alert), verified with a real scan, not assumed compliant. Only
// heading-order stays disabled, and still by verified rationale, not
// omission: it flags this card's <h3>, but app.js's OTHER pre-existing
// Dashboard cards ("Needs Attention", "Quick Links", the games/standings
// cards) all render <h3> as their own first heading too, with no page-
// level <h1>/<h2> before any of them -- a sitewide document-structure gap,
// not something unique to this card. A real heading-hierarchy pass across
// the app is #113's job. Every other rule (labels, ARIA, roles, keyboard,
// landmarks, etc.) stays enabled, so a violation genuinely new to this
// card's own markup still fails this gate.
async function assertCardHasNoA11yViolations(page, viewportLabel, stateLabel) {
  await page.addScriptTag({ url: "/__axe-core__.js" });
  const result = await page.evaluate(async (headings) => {
    const h3 = Array.from(document.querySelectorAll(".dash-card h3")).find(
      (el) => headings.some((h) => el.textContent.trim().startsWith(h)));
    const card = h3 && h3.closest(".dash-card");
    if (!card) return { notFound: true };
    const report = await axe.run(card, {
      resultTypes: ["violations"],
      rules: { "heading-order": { enabled: false } },
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
    // #331 review round 2 finding 3: the card now loads independently of
    // the rest of the Dashboard, so ".dash-stats" existing no longer means
    // the card's own fetch has resolved too -- check its slot specifically.
    const slot = document.getElementById("sp-card-slot");
    if (slot) return !slot.querySelector(".skeleton");
    return !!document.querySelector(".dash-stats");  // no slot at all (no permission) -- Dashboard itself settled
  }, null, { timeout: 10000 });
}

// Waits specifically for the complete-state heading, rather than relying on
// waitForCardSettled(): that only proves the card's OWN loading skeleton is
// gone, which is already true before a retry/reload even starts if the
// PRIOR state (e.g. the error state) had no skeleton of its own either --
// this waits for the actual expected outcome instead of a proxy for it.
async function waitForCompleteHeading(page) {
  await page.waitForFunction((headings) => {
    const h3 = Array.from(document.querySelectorAll(".dash-card h3")).find(
      (el) => headings.some((h) => el.textContent.trim().startsWith(h)));
    return !!(h3 && h3.textContent.includes("All setup steps complete"));
  }, KNOWN_CARD_HEADINGS, { timeout: 10000 });
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

async function apiGet(page, p) {
  return page.evaluate((p) =>
    fetch(p, { credentials: "same-origin" }).then((r) => r.json()), p);
}

async function loginAs(page, username, password) {
  const res = await apiPost(page, "/api/auth/login", { username, password });
  if (res && res.error) throw new Error(`login as ${username} failed: ${JSON.stringify(res)}`);
}

// switchTab() kicks off render() without awaiting it (documented above at
// focusContentHeading), so a still-in-flight render from an EARLIER
// navigation (its own chain of sequential getJSON calls, e.g. the Setup
// view's players/hierarchy/per-program/per-season fetches) can still be
// running well after a LATER action (a click handler's own render(), a
// toast) has already settled -- waiting on any one specific visible signal
// doesn't prove every render initiated so far has finished. Logging out
// while one of those is still resolving invalidates its session mid-flight
// and produces real, but test-harness-only, 401s (Chromium logs them as
// console resource-load errors indistinguishable from a genuine bug) --
// wait for the network to actually go quiet first.
async function logout(page) {
  await page.waitForLoadState("networkidle").catch(() => {});
  await apiPost(page, "/api/auth/logout", {});
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
    // The loading state is now a real, heading-bearing state of its own
    // (#331 review round 5 finding 5), so it can transiently match
    // cardState()'s heading search too -- wait past it before asserting
    // the SETTLED "no card" state below (previously masked: a heading-less
    // loading skeleton never matched cardState() either, so this check
    // "worked" even without waiting for settlement, for the wrong reason).
    await waitForCardSettled(page);

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
    // route directly to the REAL Register control for the active Season,
    // not merely land "somewhere in Setup" (#331 review round 2 finding 4:
    // the generic content-region landing this used to fall back to was not
    // enough). Proven with a genuine click-through: select the team the UI
    // already offers, then activate the control via the KEYBOARD from where
    // focus actually landed -- both confirming it's focused and that it's
    // immediately actionable, not just visible.
    await page.click("[data-setup-progress-action]");
    await page.waitForFunction(
      () => document.body.dataset.view === "setup", null, { timeout: 10000 });
    // The Setup hierarchy tree loads its own overview data after the view
    // switch -- wait for the exact Register control for this Season, not
    // just the moment the view flips or any generic tree content.
    await page.waitForFunction(
      (seasonId) => !!document.querySelector(
        `[data-reg-add][data-reg-add-season="${seasonId}"]`),
      ids.seasonId, { timeout: 10000 });
    // Poll rather than a single immediate check: focusParticipationRegister
    // Control() does its own bounded polling in app.js (the register control
    // may not exist in the DOM the instant the view flips), so the test must
    // give it the same room rather than racing it.
    try {
      await page.waitForFunction(
        (seasonId) => {
          const active = document.activeElement;
          return !!(active && active.getAttribute("data-reg-add-season") === seasonId);
        }, ids.seasonId, { timeout: 5000 });
    } catch (e) {
      const active = await page.evaluate(() =>
        (document.activeElement || {}).outerHTML || null);
      fail(`expected focus on the Register control for this season, `
        + `got focus on: ${active}`);
    }
    const regFocus = await page.evaluate((seasonId) => {
      const active = document.activeElement;
      return { disabled: !!(active && active.disabled) };
    }, ids.seasonId);
    if (regFocus.disabled) {
      fail("expected the focused Register control to be immediately actionable, not disabled");
    }
    await page.selectOption(`#reg-team-${ids.seasonId}-${ids.leagueId}`, ids.teamId);
    // Re-focus: selecting an option in Chromium can shift focus to the
    // <select> itself -- the assertion above already proved the button
    // received focus on arrival, this proves it (and only it, no click)
    // completes the registration.
    await page.focus(`[data-reg-add][data-reg-add-season="${ids.seasonId}"]`);
    await page.keyboard.press("Enter");
    // A successful registration removes this add-control entirely (the
    // team we just registered was the only one available) -- wait for that
    // rather than a fixed delay.
    await page.waitForFunction(
      (seasonId) => !document.querySelector(
        `[data-reg-add][data-reg-add-season="${seasonId}"]`),
      ids.seasonId, { timeout: 10000 });
    await page.click('.tab[data-tab="dashboard"]');
    await reachDashboard(page);

    // (6) Add a player -> roster becomes next; its primary action opens the
    // Player create drawer directly. (Team registration above already
    // happened via the real UI in step 5.)
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

    // (7) Add a player -> facilities becomes next but BLOCKED (#331 review
    // round 5 finding 1): an active Season alone is not enough -- with no
    // Rink holding granted venue access yet, the Ice Availability
    // Builder's own preview would provably yield zero slots (every
    // requested rink lands in venue_access_missing), so `next` must stay
    // blocked rather than offer a dead-end "Add Ice" CTA.
    await apiPost(page, "/api/v2/setup/player",
      { team_id: ids.teamId, name: "Vince Skater", position: "forward" });
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await reachDashboard(page);
    await waitForCardSettled(page);
    s = await cardState(page);
    if (!s || s.nextTitle !== "Venues, rinks and ice" || s.primaryLabel !== null) {
      fail(`after player, no venue access yet: expected facilities `
        + `blocked (no enabled CTA), got ${JSON.stringify(s)}`);
    }

    // Granting venue access (with no ice slot yet) makes facilities
    // genuinely SAFE -- `next` finally names it with an enabled "Add Ice"
    // CTA whose primary action opens the REAL Ice Availability Builder
    // specifically, which renders from the Calendar view, not Setup
    // (#331 review round 1 finding 3 — this is the exact destination an
    // Arena Manager needs to be able to execute).
    const venue = await apiPost(page, "/api/v2/setup/venue",
      { name: "Arena", organization_id: null });
    const rink = await apiPost(page, "/api/v2/setup/rink",
      { venue_id: venue.id, name: "Rink 1" });
    await apiPost(page, `/api/v2/setup/seasons/${ids.seasonId}/venue-access`,
      { venue_id: venue.id });
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await reachDashboard(page);
    await waitForCardSettled(page);
    s = await cardState(page);
    if (!s || s.nextTitle !== "Venues, rinks and ice") {
      fail(`after venue access granted: expected next = "Venues, rinks `
        + `and ice", got ${JSON.stringify(s)}`);
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

    // (8) Add the actual ice slot, reaching full completion -> the
    // required success state with a keyboard-operable Schedule link.
    await apiPost(page, "/api/v2/setup/ice-slot", {
      rink_id: rink.id, start_time: "2026-09-01T18:30:00+00:00",
      end_time: "2026-09-01T20:00:00+00:00", slot_type: "game",
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
    // "Imports and onboarding" stays reachable even once every required
    // workflow is done (#331 review round 2 finding 2) -- a secondary
    // action alongside the one primary "Go to Schedule", proven keyboard-
    // operable the same way: focus it directly and activate with Enter.
    const importSecondaryLabel = await page.evaluate(() =>
      ((document.querySelector('[data-setup-progress-action="import"]') || {})
        .textContent || "").trim());
    if (importSecondaryLabel !== "Import data") {
      fail(`expected a secondary "Import data" action in the complete state, `
        + `got "${importSecondaryLabel}"`);
    }
    await page.focus('[data-setup-progress-action="import"]');
    await page.keyboard.press("Enter");
    await page.waitForFunction(
      () => document.body.dataset.view === "import", null, { timeout: 10000 });
    await page.click('.tab[data-tab="dashboard"]');
    await reachDashboard(page);
    await waitForCardSettled(page);

    // (8.4) Stale Ice Builder (#331 review round 3 finding 3): opening the
    // Ice Availability Builder (any entry point -- the "🧊 Build ice"
    // button on Calendar here, same as the setup-progress card's own
    // facilities CTA earlier) and leaving it via Home, with NO reload in
    // between, must not leave it live for a later "Go to Schedule" to
    // reopen. renderCalendar() shows the builder first thing whenever
    // `iceBuilder` is still set -- switchTab() must clear it on any
    // departure from the calendar view, not just its own explicit Cancel.
    await page.click('.tab[data-tab="calendar"]');
    await page.waitForSelector("[data-ice-builder-open]", { timeout: 10000 });
    await page.click("[data-ice-builder-open]");
    await page.waitForSelector(".ib-wrap", { timeout: 10000 });
    await page.click('.tab[data-tab="dashboard"]');
    await reachDashboard(page);
    await waitForCardSettled(page);
    s = await cardState(page);
    if (!s || s.heading !== "✓ All setup steps complete") {
      fail(`expected to land back on the complete-state card after leaving `
        + `a just-opened Ice Builder via Home, got ${JSON.stringify(s)}`);
    }

    // Keyboard-operable: Tab to it and activate with Enter, not just click.
    // The real calendar/schedule UI must render, not the stale builder the
    // step above just opened and left open (#331 review round 3 finding 3 —
    // the pre-fix code only checked `body.dataset.view === "calendar"`,
    // which is equally true whether the real calendar OR the stale builder
    // renders, since both live under that same view).
    await page.evaluate(() => {
      const b = Array.from(document.querySelectorAll(".dash-card .act.primary"))
        .find((x) => x.textContent.trim() === "Go to Schedule");
      b.focus();
    });
    await page.keyboard.press("Enter");
    await page.waitForFunction(
      () => document.body.dataset.view === "calendar", null, { timeout: 10000 });
    try {
      await page.waitForSelector(".cal-controls", { timeout: 10000 });
    } catch {
      fail("expected Go to Schedule to render the real Calendar UI");
    }
    const goToScheduleState = await page.evaluate(() => ({
      ibWrapPresent: !!document.querySelector(".ib-wrap"),
      calControlsPresent: !!document.querySelector(".cal-controls"),
    }));
    if (goToScheduleState.ibWrapPresent || !goToScheduleState.calControlsPresent) {
      fail(`Go to Schedule reopened the stale Ice Builder instead of the `
        + `real calendar: ${JSON.stringify(goToScheduleState)}`);
    }
    await page.click('.tab[data-tab="dashboard"]');
    await reachDashboard(page);

    // (8.5) Per-card loading boundary (#331 review round 2 finding 3): a
    // slow setup-progress response must never block the rest of the
    // Dashboard from painting -- only the card's own slot shows a loading
    // placeholder while its fetch is still in flight. route.continue()
    // (not .fulfill()) lets the real backend answer for real once released,
    // so this exercises the actual complete-state response, not a mock.
    let releaseDelay;
    const delayPromise = new Promise((resolve) => { releaseDelay = resolve; });
    await page.route("**/api/v2/setup/progress", async (route) => {
      await delayPromise;
      await route.continue();
    });
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await reachDashboard(page);
    // The rest of the Dashboard (unrelated to this card) must already be
    // painted and present while the card's own fetch is still deliberately
    // held open.
    await page.waitForSelector(".dash-stats", { timeout: 10000 });
    const midFlight = await page.evaluate(() => {
      const slot = document.getElementById("sp-card-slot");
      return { slotShowsSkeleton: !!(slot && slot.querySelector(".skeleton")),
        dashboardReady: !!document.querySelector(".dash-stats"),
        // #331 review round 5 finding 5: the loading state must be a real,
        // announced, scannable status region, not silent/structurally
        // excluded -- role/aria-live persist on #sp-card-slot itself
        // (render()'s wrapper), aria-busy flips true the moment a fetch
        // starts.
        slotRole: slot && slot.getAttribute("role"),
        slotAriaLive: slot && slot.getAttribute("aria-live"),
        slotAriaBusy: slot && slot.getAttribute("aria-busy"),
      };
    });
    if (!midFlight.slotShowsSkeleton || !midFlight.dashboardReady) {
      fail(`expected the card's own loading skeleton while the rest of the `
        + `Dashboard is already ready, got ${JSON.stringify(midFlight)}`);
    }
    if (midFlight.slotRole !== "status" || midFlight.slotAriaLive !== "polite"
        || midFlight.slotAriaBusy !== "true") {
      fail(`expected #sp-card-slot to carry role="status" `
        + `aria-live="polite" aria-busy="true" while loading, got `
        + `${JSON.stringify(midFlight)}`);
    }
    // Loading is now a first-class, heading-bearing state (#331 review
    // round 5 finding 5) -- scannable by the SAME axe helper every other
    // state already uses, not structurally excluded from the gate.
    await assertCardHasNoA11yViolations(page, viewport.label, "loading state");
    releaseDelay();
    await page.unroute("**/api/v2/setup/progress");
    await waitForCompleteHeading(page);
    // loading -> success (#331 review round 5 finding 5): aria-busy must
    // clear once real content has landed, so a screen reader is told the
    // region has settled, not left permanently "busy".
    const settledBusy = await page.evaluate(() =>
      (document.getElementById("sp-card-slot") || {}).getAttribute
        && document.getElementById("sp-card-slot").getAttribute("aria-busy"));
    if (settledBusy !== "false") {
      fail(`expected aria-busy="false" once the complete state settled, `
        + `got ${JSON.stringify(settledBusy)}`);
    }

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
    // loading -> error (#331 review round 5 finding 5): role="alert" gives
    // the failure an ASSERTIVE announcement distinct from the outer
    // #sp-card-slot's own polite live region, and aria-busy must have
    // cleared once the (failed) fetch settled -- an error is not "still
    // busy".
    const errorSemantics = await page.evaluate(() => ({
      bannerRole: (document.querySelector(".dash-card .banner.alert") || {})
        .getAttribute && document.querySelector(".dash-card .banner.alert").getAttribute("role"),
      slotAriaBusy: (document.getElementById("sp-card-slot") || {}).getAttribute
        && document.getElementById("sp-card-slot").getAttribute("aria-busy"),
    }));
    if (errorSemantics.bannerRole !== "alert" || errorSemantics.slotAriaBusy !== "false") {
      fail(`expected the error banner to carry role="alert" and `
        + `aria-busy="false" once settled, got ${JSON.stringify(errorSemantics)}`);
    }
    await page.unroute("**/api/v2/setup/progress");
    // Re-route to a deliberately delayed (but successful) response so the
    // retry's OWN in-flight fetch can be observed carrying aria-busy="true"
    // again -- not just the very first load (#331 review round 5 finding 5).
    let releaseRetryDelay;
    const retryDelayPromise = new Promise((resolve) => { releaseRetryDelay = resolve; });
    await page.route("**/api/v2/setup/progress", async (route) => {
      await retryDelayPromise;
      await route.continue();
    });
    await page.click("[data-setup-progress-retry]");
    await page.waitForFunction(
      () => (document.getElementById("sp-card-slot") || {}).getAttribute
        && document.getElementById("sp-card-slot").getAttribute("aria-busy") === "true",
      null, { timeout: 5000 });
    releaseRetryDelay();
    await page.unroute("**/api/v2/setup/progress");
    // waitForCardSettled's "no .skeleton" check is already true on the
    // error state BEFORE this click (the error state has no skeleton of its
    // own -- retry re-fetches without showing one), so it would resolve
    // immediately without ever waiting for the retry's own fetch to finish.
    // Wait for the specific expected outcome instead.
    await waitForCompleteHeading(page);
    s = await cardState(page);
    if (!s || s.heading !== "✓ All setup steps complete") {
      fail(`expected Retry to recover the real (complete) state, got ${JSON.stringify(s)}`);
    }
    // error -> retry-success (#331 review round 5 finding 5): aria-busy
    // must have cleared again once the retry's own fetch settled.
    const retrySettledBusy = await page.evaluate(() =>
      (document.getElementById("sp-card-slot") || {}).getAttribute
        && document.getElementById("sp-card-slot").getAttribute("aria-busy"));
    if (retrySettledBusy !== "false") {
      fail(`expected aria-busy="false" once the retry's success settled, `
        + `got ${JSON.stringify(retrySettledBusy)}`);
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

const ROLE_VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8305 },
  { label: "phone", width: 390, height: 844, port: 8306 },
];

// #331 review round 3: role-aware executability + redaction (finding 1),
// the Season-scoped Register control fix (finding 2), and Import's role-
// gated visibility in the complete state (finding 5). A separate journey
// from checkViewport above (which stays League-Admin-only end to end) so
// each gets its own clean, minimally-scoped fixture rather than retrofitting
// role switches into that already-long single-role flow.
async function checkRoleScenarios(browser, viewport) {
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
  const axeSource = fs.readFileSync(AXE_PATH, "utf8");
  await page.route("**/__axe-core__.js", (route) => route.fulfill({
    status: 200, contentType: "application/javascript", body: axeSource,
  }));

  const fail = (msg) => { throw new Error(`[${viewport.label}] ${msg}`); };
  const freshLoad = async () => {
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await reachDashboard(page);
    await waitForCardSettled(page);
  };

  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });
    await reachDashboard(page);  // signed in as the auto-provisioned League Admin
    // A fresh boot only seeds the "admin" account -- "arena" (and the other
    // demo personas) are UserAccount rows /api/demo/load builds, same as
    // permanent-teams.js's own Arena Manager coverage. Harmless alongside
    // this file's own Program-scoped fixtures below: every scenario
    // explicitly selects its own created Program via /api/context, so the
    // extra seeded demo data is never in scope for any assertion here.
    const loadStatus = await page.evaluate(() => fetch("/api/demo/load", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" }, body: "{}",
    }).then((r) => r.status));
    if (loadStatus !== 200) fail(`demo load (as admin) failed (status ${loadStatus})`);

    // ---- (A) Program A: two Seasons sharing one permanent League (#331
    // review round 3 finding 2) -- mirrors season-participation.js's own
    // lg1/lg2-bootstrap fixture. Team A is registered into s1 directly (a
    // pre-existing row this scenario must prove stays untouched); s2's
    // LeagueSeason is bootstrapped the same way season-participation.js
    // does (register then remove), leaving BOTH Team A and Team B open in
    // s2's own "Register" row.
    const a = await page.evaluate(async () => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      const program = await post("/api/v2/setup/program",
        { name: "Round3F2 Program", country: "US" });
      const s1 = await post("/api/v2/setup/season", { program_id: program.id, name: "S1" });
      const league = await post("/api/v2/setup/league", { season_id: s1.id, name: "Shared League" });
      const club = await post("/api/v2/setup/club", { name: "Club" });
      const teamA = await post("/api/v2/setup/team",
        { league_id: league.id, club_id: club.id, name: "Team A" });
      const teamB = await post("/api/v2/setup/team",
        { league_id: league.id, club_id: club.id, name: "Team B" });
      const regA = await post(`/api/v2/setup/seasons/${s1.id}/team-registrations`,
        { team_id: teamA.id, league_id: league.id, division_id: null });
      const s2 = await post("/api/v2/setup/season", { program_id: program.id, name: "S2" });
      const boot = await post(`/api/v2/setup/seasons/${s2.id}/team-registrations`,
        { team_id: teamA.id, league_id: league.id, division_id: null });
      await post(`/api/v2/setup/season-team-registration/${boot.id}/remove`, {});
      return { program: program.id, s1: s1.id, s2: s2.id, league: league.id,
        teamA: teamA.id, teamB: teamB.id, regA: regA.id };
    });
    await apiPost(page, "/api/context", { program_id: a.program, season_id: a.s2 });
    await freshLoad();

    let s = await cardState(page);
    if (!s || s.nextTitle !== "Season participation and divisions") {
      fail(`Program A / season s2: expected next = "Season participation `
        + `and divisions", got ${JSON.stringify(s)}`);
    }
    await page.click("[data-setup-progress-action]");
    await page.waitForFunction(
      (seasonId) => !!document.querySelector(
        `[data-reg-add][data-reg-add-season="${seasonId}"]`),
      a.s2, { timeout: 10000 });
    try {
      await page.waitForFunction(
        (seasonId) => {
          const active = document.activeElement;
          return !!(active && active.getAttribute("data-reg-add-season") === seasonId);
        }, a.s2, { timeout: 5000 });
    } catch (e) {
      const active = await page.evaluate(() => (document.activeElement || {}).outerHTML || null);
      fail(`expected focus on s2's Register control, got focus on: ${active}`);
    }
    // Select Team B specifically in s2's OWN team-scoped control (the id is
    // now keyed by Season+League, #331 review round 3 finding 2 -- a
    // League-only id would have collided with s1's own row for this same
    // League).
    await page.selectOption(`#reg-team-${a.s2}-${a.league}`, a.teamB);
    // Re-focus, then submit purely via the keyboard (matches the reviewer's
    // required regression: "keyboard-submit its focused row").
    await page.focus(`[data-reg-add][data-reg-add-season="${a.s2}"]`);
    const regResp = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/seasons/${a.s2}/team-registrations`
      && r.request().method() === "POST");
    await page.keyboard.press("Enter");
    if ((await regResp).status() !== 200) {
      fail("expected the keyboard-submitted s2 registration to succeed");
    }
    // The click handler's own `await render()` (re-fetching the hierarchy
    // tree to redraw with the new registration) is still in flight after
    // the POST response alone -- wait for its toast, which render() itself
    // paints, so nothing from this handler is still pending before the
    // role switch below logs this session out.
    await page.waitForFunction(
      () => (document.querySelector("#toast-root .toast-msg") || {}).textContent
        === "Team registered for the season.", null, { timeout: 10000 });

    const [s1After, s2After] = await Promise.all([
      apiGet(page, `/api/v2/setup/seasons/${a.s1}/team-registrations`),
      apiGet(page, `/api/v2/setup/seasons/${a.s2}/team-registrations`),
    ]);
    const activeTeamIds = (regs) => (regs.registrations || [])
      .filter((r) => r.active).map((r) => r.team_id).sort();
    if (JSON.stringify(activeTeamIds(s1After)) !== JSON.stringify([a.teamA])) {
      fail(`s1's pre-existing registration was disturbed by submitting s2's `
        + `row: ${JSON.stringify(s1After)}`);
    }
    if (JSON.stringify(activeTeamIds(s2After)) !== JSON.stringify([a.teamB])) {
      fail(`s2 must receive exactly Team B (and only Team B) from its own `
        + `focused row, got ${JSON.stringify(s2After)}`);
    }

    // ---- (A2) Same Program A / season s2, viewed as Arena Manager: proves
    // BOTH halves of finding 1 together in a genuinely executable (not
    // blocked) context -- `next` still correctly recommends facilities
    // (season s2 is active, not archived, AND has a Rink with granted
    // venue access -- #331 review round 5 finding 1's own gate, satisfied
    // here as admin since Arena Manager cannot grant it themselves), and
    // the response's `workflows` is redacted to just that one entry, never
    // the League-Admin-only league_season/teams/participation/roster
    // detail this Program now carries (participation just went done above
    // -- an exact count Arena Manager must never receive).
    const a2Venue = await apiPost(page, "/api/v2/setup/venue",
      { name: "A2 Arena", organization_id: null });
    await apiPost(page, "/api/v2/setup/rink",
      { venue_id: a2Venue.id, name: "A2 Rink" });
    await apiPost(page, `/api/v2/setup/seasons/${a.s2}/venue-access`,
      { venue_id: a2Venue.id });

    await logout(page);
    await loginAs(page, "arena", "demo");
    await apiPost(page, "/api/context", { program_id: a.program, season_id: a.s2 });
    await freshLoad();
    s = await cardState(page);
    if (!s || s.nextTitle !== "Venues, rinks and ice" || s.primaryKey !== "facilities") {
      fail(`Arena Manager on Program A: expected an executable facilities `
        + `next action, got ${JSON.stringify(s)}`);
    }
    if (s.rows.length !== 1 || s.rows[0].title !== "Venues, rinks and ice") {
      fail(`Arena Manager must see only the one workflow their role can `
        + `manage, got ${JSON.stringify(s.rows)}`);
    }
    const arenaProgOnA = await apiGet(page, "/api/v2/setup/progress");
    if (arenaProgOnA.workflows.length !== 1
        || arenaProgOnA.workflows[0].key !== "facilities") {
      fail(`Arena Manager's raw /api/v2/setup/progress must redact every `
        + `League-Admin-only workflow, got ${JSON.stringify(arenaProgOnA.workflows)}`);
    }
    await logout(page);
    await loginAs(page, "admin", "demo");

    // ---- (B) Program B: no Season at all, Arena Manager (#331 review
    // round 3 finding 1's executability half). The only permitted workflow
    // (facilities) is not safe -- the real Ice Builder commit fails
    // season_missing with nothing to attach ice to -- so the card must show
    // actionable guidance, not a CTA that would silently fail, and no
    // primary action at all.
    const b = await page.evaluate(async () => (await fetch("/api/v2/setup/program", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: "Round3F1 Program", country: "US" }),
    })).json());
    await logout(page);
    await loginAs(page, "arena", "demo");
    await apiPost(page, "/api/context", { program_id: b.id, season_id: null });
    await freshLoad();
    s = await cardState(page);
    if (!s || s.heading !== "Continue setup") {
      fail(`Arena Manager, no-Season Program: expected the guidance card, `
        + `got ${JSON.stringify(s)}`);
    }
    if (s.nextTitle !== "Venues, rinks and ice" || !/[Ss]eason/.test(s.nextDetail)) {
      fail(`expected facilities-blocked guidance naming the missing Season, `
        + `got ${JSON.stringify(s)}`);
    }
    if (s.primaryLabel !== null) {
      fail(`a blocked workflow must never render a clickable primary action `
        + `that would just fail, got primary "${s.primaryLabel}"`);
    }
    if (await page.$("[data-setup-progress-action]")) {
      fail("expected no data-setup-progress-action control at all in the "
        + "blocked-guidance state");
    }
    await assertCardHasNoA11yViolations(page, viewport.label, "blocked-guidance state");
    await logout(page);
    await loginAs(page, "admin", "demo");

    // ---- (B2) Program B, League Admin, still no Season selected (#331
    // review round 4): league_season/teams are already done, so
    // "participation" is the first todo workflow -- it must be blocked by
    // season_missing exactly like facilities was for Arena Manager above,
    // not handed out as an enabled "Register Team" CTA that
    // focusParticipationRegisterControl() cannot bind/focus without an
    // exact selected Season.
    const b2Season = await apiPost(page, "/api/v2/setup/season",
      { program_id: b.id, name: "Fall" });
    const b2League = await apiPost(page, "/api/v2/setup/league",
      { season_id: b2Season.id, name: "League" });
    const b2Club = await apiPost(page, "/api/v2/setup/club", { name: "Club" });
    await apiPost(page, "/api/v2/setup/team",
      { league_id: b2League.id, club_id: b2Club.id, name: "Team" });
    await apiPost(page, "/api/context", { program_id: b.id, season_id: null });
    await freshLoad();
    s = await cardState(page);
    if (!s || s.heading !== "Continue setup") {
      fail(`League Admin, Program-only (participation): expected the `
        + `guidance card, got ${JSON.stringify(s)}`);
    }
    if (s.nextTitle !== "Season participation and divisions"
        || !/[Ss]eason/.test(s.nextDetail)) {
      fail(`expected participation-blocked guidance naming the missing `
        + `Season, got ${JSON.stringify(s)}`);
    }
    if (s.primaryLabel !== null) {
      fail(`Program-only participation must never render a clickable `
        + `Register Team action that would land unbound/unfocused, got `
        + `primary "${s.primaryLabel}"`);
    }
    await assertCardHasNoA11yViolations(page, viewport.label,
      "blocked-guidance state (participation, Program-only)");
    await logout(page);
    await loginAs(page, "admin", "demo");

    // ---- (C) Program C, driven fully complete by League Admin: the
    // complete-state secondary Import action must be visible/reachable for
    // League Admin (MANAGE_SETUP) but withheld from Arena Manager (#331
    // review round 3 finding 5) -- Arena Manager was routed to a MANAGE_
    // SETUP-only surface they cannot use before this fix. Both keep the one
    // "Go to Schedule" primary action regardless of role.
    const cIds = await page.evaluate(async () => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      const program = await post("/api/v2/setup/program",
        { name: "Round3F5 Program", country: "US" });
      const season = await post("/api/v2/setup/season",
        { program_id: program.id, name: "Fall" });
      const league = await post("/api/v2/setup/league",
        { season_id: season.id, name: "League" });
      const club = await post("/api/v2/setup/club", { name: "Club" });
      const team = await post("/api/v2/setup/team",
        { league_id: league.id, club_id: club.id, name: "Team" });
      await post(`/api/v2/setup/seasons/${season.id}/team-registrations`,
        { team_id: team.id, league_id: league.id, division_id: null });
      await post("/api/v2/setup/player",
        { team_id: team.id, name: "P", position: "forward" });
      const venue = await post("/api/v2/setup/venue", { name: "V", organization_id: null });
      const rink = await post("/api/v2/setup/rink", { venue_id: venue.id, name: "R" });
      await post("/api/v2/setup/ice-slot", { rink_id: rink.id,
        start_time: "2026-09-01T18:30:00+00:00", end_time: "2026-09-01T20:00:00+00:00",
        slot_type: "game" });
      await post(`/api/v2/setup/seasons/${season.id}/venue-access`, { venue_id: venue.id });
      return { program: program.id, season: season.id };
    });
    await apiPost(page, "/api/context", { program_id: cIds.program, season_id: cIds.season });
    await freshLoad();
    s = await cardState(page);
    if (!s || s.heading !== "✓ All setup steps complete") {
      fail(`Program C, League Admin: expected the complete state, got ${JSON.stringify(s)}`);
    }
    let importBtn = await page.$('[data-setup-progress-action="import"]');
    if (!importBtn) fail("League Admin must see the complete-state Import action");

    await logout(page);
    await loginAs(page, "arena", "demo");
    await apiPost(page, "/api/context", { program_id: cIds.program, season_id: cIds.season });
    // #331 review round 5 finding 3: Arena Manager can never truthfully
    // verify a whole-Program "complete" claim from their own narrower
    // view -- `complete` reads `null` for them even though the Program
    // (and their own visible facilities workflow) is genuinely, fully
    // done, and the card renders NOTHING rather than the success banner
    // it hands League Admin above (a claim Arena Manager cannot verify)
    // or a stale/misleading "nothing more for you" card. The Arena
    // Calendar stays reachable the ordinary way, via the main nav tab --
    // this card's own "Go to Schedule" shortcut simply has nothing left
    // to say once there is nothing to recommend and nothing to verify.
    const cArenaProgress = await apiGet(page, "/api/v2/setup/progress");
    if (cArenaProgress.complete !== null) {
      fail(`Program C, Arena Manager: expected complete: null (never a `
        + `real True/False from a partial view), got `
        + `${JSON.stringify(cArenaProgress)}`);
    }
    if (cArenaProgress.next !== null || cArenaProgress.next_blocked !== null) {
      fail(`Program C, Arena Manager: expected both next and next_blocked `
        + `null once their own visible facilities workflow is done, got `
        + `${JSON.stringify(cArenaProgress)}`);
    }
    await freshLoad();
    s = await cardState(page);
    if (s !== null) {
      fail(`Program C, Arena Manager: expected NO setup-progress card `
        + `(nothing left to recommend, nothing left to verify), got `
        + `${JSON.stringify(s)}`);
    }
    if (await page.$("#sp-card-slot .dash-card")) {
      fail("expected #sp-card-slot to render no card at all in this state");
    }

    // ---- (D) Two Programs, A created first / B active (#331 review round
    // 5 finding 4): every hub-driven create-drawer/Ice-Builder destination
    // must seed its parent field from the ACTIVE Program/Season -- these
    // fields' own option lists span every Program, unfiltered, so left
    // unseeded they fall back to whichever entity happens to sort first
    // GLOBALLY, which is Program A's here by construction (created, hence
    // id-ordered, first). A silent wrong default would let a real submit
    // create data under A while the operator is acting from B.
    await logout(page);
    await loginAs(page, "admin", "demo");
    const d = await page.evaluate(async () => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      // Program A: built out fully so its League/Team/Season are all
      // plausible (and, pre-fix, actual) wrong defaults.
      const progA = await post("/api/v2/setup/program",
        { name: "Round5F4 Program A", country: "US" });
      const seasonA = await post("/api/v2/setup/season",
        { program_id: progA.id, name: "Season A" });
      const leagueA = await post("/api/v2/setup/league",
        { season_id: seasonA.id, name: "League A" });
      const clubA = await post("/api/v2/setup/club", { name: "Club A" });
      const teamA = await post("/api/v2/setup/team",
        { league_id: leagueA.id, club_id: clubA.id, name: "Team A" });
      // Program B: created SECOND, starts with nothing -- built up one
      // workflow at a time via the REAL hub CTA below, never an API
      // shortcut, so each drawer's actual pre-fill is exercised.
      const progB = await post("/api/v2/setup/program",
        { name: "Round5F4 Program B", country: "US" });
      return { progA: progA.id, seasonA: seasonA.id, leagueA: leagueA.id,
        teamA: teamA.id, progB: progB.id };
    });
    await apiPost(page, "/api/context", { program_id: d.progB, season_id: null });
    await freshLoad();

    // -- Add Season: the Program select must default to the ACTIVE Program
    // B, never fall through to Program A.
    s = await cardState(page);
    if (!s || s.nextTitle !== "League profile and seasons") {
      fail(`Program B fresh: expected next = "League profile and seasons", `
        + `got ${JSON.stringify(s)}`);
    }
    await page.click("[data-setup-progress-action]");
    await page.waitForSelector("#f-season-league", { timeout: 10000 });
    let selected = await page.$eval("#f-season-league", (el) => el.value);
    if (selected !== d.progB) {
      fail(`Add Season: expected the Program select to default to the `
        + `ACTIVE Program B (${d.progB}), got ${selected} (Program A is `
        + `${d.progA})`);
    }
    await page.fill("#f-season", "Season B");
    const seasonResp = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/season` && r.request().method() === "POST");
    await page.click('[data-drawer-submit="season"]');
    const seasonB = await (await seasonResp).json();
    if (seasonB.program_id !== d.progB) {
      fail(`Add Season submit created "Season B" under the wrong Program `
        + `-- got ${JSON.stringify(seasonB)}`);
    }
    await page.waitForFunction(() => !document.querySelector(".drawer"),
      null, { timeout: 10000 });
    // Select the just-created Season B as the active context -- without
    // this, participation stays blocked on season_missing (round 4's own
    // gate) ahead of "teams"/"roster" in the fixed order, masking the
    // rest of this scenario.
    await apiPost(page, "/api/context", { program_id: d.progB, season_id: seasonB.id });

    // -- Add League for Season B (a plain API call -- not part of this
    // bug) so "league_season" flips done and "teams" becomes next.
    const leagueB = await apiPost(page, "/api/v2/setup/league",
      { season_id: seasonB.id, name: "League B" });
    await freshLoad();

    // -- Add Team: the Permanent league select must default to Program
    // B's own League B, never League A.
    s = await cardState(page);
    if (!s || s.nextTitle !== "Permanent teams") {
      fail(`Program B / Season B: expected next = "Permanent teams", got `
        + `${JSON.stringify(s)}`);
    }
    await page.click("[data-setup-progress-action]");
    await page.waitForSelector("#f-team-perm-league", { timeout: 10000 });
    selected = await page.$eval("#f-team-perm-league", (el) => el.value);
    if (selected !== leagueB.id) {
      fail(`Add Team: expected the Permanent league select to default to `
        + `Program B's own League B (${leagueB.id}), got ${selected} `
        + `(Program A's League A is ${d.leagueA})`);
    }
    await page.fill("#f-team", "Team B");
    const teamResp = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/team` && r.request().method() === "POST");
    await page.click('[data-drawer-submit="team"]');
    await teamResp;
    await page.waitForFunction(() => !document.querySelector(".drawer"),
      null, { timeout: 10000 });
    const overviewAfterTeam = await apiGet(page, "/api/v2/setup/overview");
    const teamB = (overviewAfterTeam.teams || []).find(
      (t) => t.name === "Team B" && t.program_id === d.progB);
    if (!teamB) {
      fail(`Add Team submit did not create "Team B" under the active `
        + `Program B -- got teams: ${JSON.stringify(overviewAfterTeam.teams)}`);
    }
    // Register Team B into Season B (a plain API call -- not part of this
    // bug) so "participation" flips done and "roster" becomes next.
    await apiPost(page,
      `/api/v2/setup/seasons/${seasonB.id}/team-registrations`,
      { team_id: teamB.id, league_id: leagueB.id });

    // -- Add Player: the Team select must default to Program B's own Team
    // B, never Program A's Team A.
    await freshLoad();
    s = await cardState(page);
    if (!s || s.nextTitle !== "Clubs, players and staff") {
      fail(`Program B / Team B: expected next = "Clubs, players and `
        + `staff", got ${JSON.stringify(s)}`);
    }
    await page.click("[data-setup-progress-action]");
    await page.waitForSelector("#f-player-team", { timeout: 10000 });
    selected = await page.$eval("#f-player-team", (el) => el.value);
    if (selected !== teamB.id) {
      fail(`Add Player: expected the Team select to default to Program `
        + `B's own Team B (${teamB.id}), got ${selected} (Program A's `
        + `Team A is ${d.teamA})`);
    }
    await page.fill("#f-player-name", "Player B");
    const playerResp = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/player` && r.request().method() === "POST");
    await page.click('[data-drawer-submit="player"]');
    const playerB = await (await playerResp).json();
    if (playerB.team_id !== teamB.id) {
      fail(`Add Player submit created "Player B" under the wrong Team -- `
        + `got ${JSON.stringify(playerB)}`);
    }
    await page.waitForFunction(() => !document.querySelector(".drawer"),
      null, { timeout: 10000 });

    // -- Add Ice (facilities): the Ice Builder's own Season select must
    // default to the ACTIVE Season B, never Season A, even though A's
    // Season also has status "active" and sorts first in the global
    // seasons list defaultIceForm() reads from.
    await apiPost(page, "/api/context", { program_id: d.progB, season_id: seasonB.id });
    await page.click('.tab[data-tab="calendar"]');
    await page.waitForFunction(
      () => document.body.dataset.view === "calendar", null, { timeout: 10000 });
    await page.click("[data-ice-builder-open]");
    await page.waitForSelector("#ib-season", { timeout: 10000 });
    selected = await page.$eval("#ib-season", (el) => el.value);
    if (selected !== seasonB.id) {
      fail(`Add Ice: expected the Ice Builder's Season select to default `
        + `to the ACTIVE Season B (${seasonB.id}), got ${selected} `
        + `(Season A is ${d.seasonA})`);
    }

    // ---- (E) Two Programs, fail-closed + race-safe drawer seeding (#331
    // review round 6): a hierarchy-fetch failure, or the active Program
    // changing while that fetch is still in flight, must never open the
    // drawer with a stale/empty seed -- that re-triggers the exact
    // wrong-Program fallback (D) above just closed. Scoped to "team" only
    // (not "player" too): both kinds share the identical async/race-
    // guarding code in contextSeededDrawerValues() before their own
    // branch, so covering "team" exhaustively covers the actual fix.
    await logout(page);
    await loginAs(page, "admin", "demo");
    const e = await page.evaluate(async () => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      const progE1 = await post("/api/v2/setup/program",
        { name: "Round6 Program E1", country: "US" });
      const seasonE1 = await post("/api/v2/setup/season",
        { program_id: progE1.id, name: "Season E1" });
      const leagueE1 = await post("/api/v2/setup/league",
        { season_id: seasonE1.id, name: "League E1" });
      const progE2 = await post("/api/v2/setup/program",
        { name: "Round6 Program E2", country: "US" });
      const seasonE2 = await post("/api/v2/setup/season",
        { program_id: progE2.id, name: "Season E2" });
      const leagueE2 = await post("/api/v2/setup/league",
        { season_id: seasonE2.id, name: "League E2" });
      return { progE1: progE1.id, leagueE1: leagueE1.id,
        progE2: progE2.id, leagueE2: leagueE2.id };
    });

    // -- (E1) Hierarchy 500: no drawer, an error toast, no wrong-default
    // write; a retry (once unmocked) succeeds correctly scoped.
    await apiPost(page, "/api/context", { program_id: e.progE2, season_id: null });
    await page.route("**/api/v2/setup/hierarchy", (route) => route.fulfill({
      status: 500, contentType: "application/json",
      body: JSON.stringify({ error: { code: "server_unavailable",
        message: "The server is temporarily unavailable." } }),
    }));
    await freshLoad();
    s = await cardState(page);
    if (!s || s.nextTitle !== "Permanent teams") {
      fail(`Program E2: expected next = "Permanent teams", got ${JSON.stringify(s)}`);
    }
    await page.click("[data-setup-progress-action]");
    await page.waitForFunction(
      () => (document.getElementById("toast-root") || {}).classList
        && document.getElementById("toast-root").classList.contains("error"),
      null, { timeout: 10000 });
    if (await page.$(".drawer")) {
      fail("expected NO drawer to open when the hierarchy fetch fails");
    }
    await page.unroute("**/api/v2/setup/hierarchy");
    await page.click("[data-setup-progress-action]");
    await page.waitForSelector("#f-team-perm-league", { timeout: 10000 });
    let retrySelected = await page.$eval("#f-team-perm-league", (el) => el.value);
    if (retrySelected !== e.leagueE2) {
      fail(`retry: expected the Permanent league select to default to `
        + `Program E2's own League (${e.leagueE2}), got ${retrySelected}`);
    }
    // The retry's successfully-opened drawer must land keyboard focus, not
    // just the correct value (#331 review round 6 comment 2's explicit
    // "include keyboard focus after retry"). This is pre-existing,
    // unconditional behavior (the drawer-wiring autofocus at app.js's
    // "if (drawer) { const first = ... .focus(); }") -- asserted here for
    // the first time on THIS specific path since it was never previously
    // exercised by a genuine fetch-failure-then-retry drawer open.
    const retryFocusId = await page.evaluate(() => (document.activeElement || {}).id);
    if (retryFocusId !== "f-team-club") {
      fail(`retry: expected keyboard focus on the drawer's first field `
        + `(#f-team-club), got #${retryFocusId}`);
    }
    await page.keyboard.press("Escape");
    await page.waitForFunction(() => !document.querySelector(".drawer"),
      null, { timeout: 10000 });
    // The deliberately-injected hierarchy 500 above is expected to log a
    // browser resource-load console error the same way checkViewport's own
    // (9) does for /api/v2/setup/progress -- require at least one, proving
    // the mock actually fired, then drop every matching message so any
    // other, genuinely unexpected error still fails the gate below.
    const unexpectedE1 = errors.filter((e) => !/responded with a status of 500/.test(e));
    if (unexpectedE1.length === errors.length) {
      fail("expected the deliberately failed hierarchy request to log a "
        + `resource error, got:\n${errors.join("\n")}`);
    }
    errors.length = 0;
    errors.push(...unexpectedE1);
    // The successful retry's own goToSetupWorkflow() call switchTab()'d to
    // "setup" to host the drawer (E1's earlier, FAILED attempt never did --
    // it stayed on Home/Tasks) -- Escape only dismisses the drawer overlay,
    // it does not navigate back. #sp-card-slot, and the CTA (E2) clicks
    // next, exist only on the Dashboard, so land back there first.
    await freshLoad();
    s = await cardState(page);
    if (!s || s.nextTitle !== "Permanent teams") {
      fail(`Program E2 after returning to Dashboard: expected next = `
        + `"Permanent teams", got ${JSON.stringify(s)}`);
    }

    // -- (E2) Delayed hierarchy response + a Program switch mid-flight:
    // the stale (Program E2) seed must never be applied once the context
    // has already moved to Program E1 by the time it resolves.
    let releaseHierarchy;
    const hierarchyDelay = new Promise((resolve) => { releaseHierarchy = resolve; });
    await page.route("**/api/v2/setup/hierarchy", async (route) => {
      await hierarchyDelay;
      await route.continue();
    });
    await page.click("[data-setup-progress-action]");
    // Give the click's own handler a moment to actually start the fetch
    // (and lose the race deliberately) before switching context out from
    // under it. The switch itself MUST go through the real context-switcher
    // control (context-switcher.js's own established idiom), not a bare
    // apiPost: contextSeededDrawerValues()'s stillCurrent recheck reads the
    // CLIENT-side `contextOptions.selected`, which setActiveContext() (the
    // switcher's onchange handler) updates as part of handling the pick --
    // an apiPost bypasses that entirely and only moves the SERVER'S active
    // context, leaving the client's own cached selection (and so the
    // mismatch check) none the wiser, which would silently defeat this
    // exact regression.
    await new Promise((r) => setTimeout(r, 200));
    const switchValue = `${e.progE1}|`;
    await page.selectOption("#ctx-select", switchValue);
    await page.waitForFunction((v) => document.getElementById("ctx-select").value === v,
      switchValue, { timeout: 10000 });
    releaseHierarchy();
    await page.unroute("**/api/v2/setup/hierarchy");
    await page.waitForFunction(
      () => (document.getElementById("toast-root") || {}).classList
        && document.getElementById("toast-root").classList.contains("error"),
      null, { timeout: 10000 });
    if (await page.$(".drawer")) {
      fail("expected NO drawer to open with a stale seed after the active "
        + "Program changed mid-flight");
    }
    // A fresh click, now that the context switch has fully landed, opens
    // correctly scoped to the CURRENT Program (E1) and its OWN League --
    // proof the drawer never silently seeded from E2's now-stale one.
    await freshLoad();
    s = await cardState(page);
    if (!s || s.nextTitle !== "Permanent teams") {
      fail(`Program E1: expected next = "Permanent teams", got ${JSON.stringify(s)}`);
    }
    await page.click("[data-setup-progress-action]");
    await page.waitForSelector("#f-team-perm-league", { timeout: 10000 });
    const e1Selected = await page.$eval("#f-team-perm-league", (el) => el.value);
    if (e1Selected !== e.leagueE1) {
      fail(`Program E1's drawer must default to E1's own League `
        + `(${e.leagueE1}), never E2's stale one (${e.leagueE2}), got `
        + `${e1Selected}`);
    }

    // -- (E3) A second, faster click supersedes an in-flight first one
    // (drawerSeedFetchSeq, #331 review round 6): the SAME stale-response
    // guard idiom as loadSetupProgressCard()'s own setupProgressFetchSeq
    // (checkViewport's (10) above), applied here to drawer seeding. Neither
    // E1 nor E2 exercises this specific trigger (a rapid re-click, not a
    // fetch error or a Program switch), so it needs its own regression.
    await page.keyboard.press("Escape");
    await page.waitForFunction(() => !document.querySelector(".drawer"),
      null, { timeout: 10000 });
    await freshLoad();
    const e3 = await page.evaluate(async () => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      const progE3 = await post("/api/v2/setup/program",
        { name: "Round6 Program E3", country: "US" });
      const seasonE3 = await post("/api/v2/setup/season",
        { program_id: progE3.id, name: "Season E3" });
      const leagueE3 = await post("/api/v2/setup/league",
        { season_id: seasonE3.id, name: "League E3" });
      return { progE3: progE3.id, leagueE3: leagueE3.id };
    });
    // The switcher's own <option> list comes from contextOptions, loaded at
    // boot -- Program E3 was just created via a bare fetch, bypassing that,
    // so a reload is required before its option even exists to select.
    await freshLoad();
    const switchToE3 = `${e3.progE3}|`;
    await page.selectOption("#ctx-select", switchToE3);
    await page.waitForFunction((v) => document.getElementById("ctx-select").value === v,
      switchToE3, { timeout: 10000 });
    await freshLoad();
    s = await cardState(page);
    if (!s || s.nextTitle !== "Permanent teams") {
      fail(`Program E3: expected next = "Permanent teams", got ${JSON.stringify(s)}`);
    }
    // The FIRST intercepted hierarchy request is held, then answered with a
    // FABRICATED, otherwise-impossible League id -- unambiguous proof of
    // which click's own result actually painted, since (unlike E1/E2) both
    // clicks would otherwise resolve identical, indistinguishable real data.
    // The second request is passed straight through, real and fast, so it
    // both wins the race AND is the one that must be seen.
    let hierarchyReqCount = 0;
    await page.route("**/api/v2/setup/hierarchy", async (route) => {
      hierarchyReqCount += 1;
      if (hierarchyReqCount !== 1) return route.continue();
      await new Promise((r) => setTimeout(r, 1500));
      await route.fulfill({
        status: 200, contentType: "application/json",
        body: JSON.stringify({ programs: [{ id: e3.progE3,
          leagues: [{ id: "STALE-LEAGUE-MUST-NOT-APPEAR", name: "stale" }] }] }),
      });
    });
    await page.click("[data-setup-progress-action]");
    // Re-click immediately, before the first request's own delay elapses --
    // its own hierarchy request is unmocked (fast), so it wins the race and
    // must be the one that actually paints the drawer.
    await page.click("[data-setup-progress-action]");
    await page.waitForSelector("#f-team-perm-league", { timeout: 10000 });
    const e3Selected = await page.$eval("#f-team-perm-league", (el) => el.value);
    if (e3Selected !== e3.leagueE3) {
      fail(`Program E3: expected the second (winning) click's own result `
        + `to seed the drawer from League E3 (${e3.leagueE3}), got `
        + `${e3Selected}`);
    }
    // Give the stale first click's own delayed response its full window to
    // land and confirm it did NOT reopen/reset/clobber what the second,
    // superseding click already painted.
    await new Promise((r) => setTimeout(r, 1700));
    const stillSelected = await page.$eval("#f-team-perm-league", (el) => el.value);
    if (stillSelected !== e3.leagueE3) {
      fail(`a late-arriving, superseded first click's own result clobbered `
        + `the drawer the second click already painted, got `
        + `${stillSelected}`);
    }
    await page.unroute("**/api/v2/setup/hierarchy");
    await page.keyboard.press("Escape");
    await page.waitForFunction(() => !document.querySelector(".drawer"),
      null, { timeout: 10000 });

    // ---- (F) Hub Import destination must bind to, and rebind on, the
    // active Program/Season (#331 review round 7 finding 1):
    // importState.seasonId used to default from the GLOBAL first Season
    // and persist across a Program switch untouched -- Commit sends it
    // verbatim, so a League Admin acting from Program F2 could silently
    // import into Program F1.
    //
    // logout()'s own networkidle wait (see its comment) is a single
    // sample -- it can read "quiet" a moment before a just-scheduled
    // fetch (e.g. the Setup view's own render() after this drawer's
    // close) actually reaches the network layer. Step E's drawer-close
    // sequence directly above is the first place in this file that lands
    // on the Setup view via a drawer-close rather than a plain click, so
    // give it an explicit second look: settle once, a short buffer for
    // anything about to fire to actually start, then settle again.
    await page.waitForLoadState("networkidle").catch(() => {});
    await new Promise((r) => setTimeout(r, 500));
    await page.waitForLoadState("networkidle").catch(() => {});
    await logout(page);
    await loginAs(page, "admin", "demo");
    const f = await page.evaluate(async () => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      // Program F1: driven fully complete (same recipe as step C above) so
      // its Home/Tasks card genuinely offers the real complete-card
      // "Import data" action this scenario must click through, not a
      // shortcut via the separate always-on Import nav tab.
      const progF1 = await post("/api/v2/setup/program",
        { name: "Round7F1 Program", country: "US" });
      const seasonF1 = await post("/api/v2/setup/season",
        { program_id: progF1.id, name: "Season F1" });
      const leagueF1 = await post("/api/v2/setup/league",
        { season_id: seasonF1.id, name: "League F1" });
      const clubF1 = await post("/api/v2/setup/club", { name: "Club F1" });
      const teamF1 = await post("/api/v2/setup/team",
        { league_id: leagueF1.id, club_id: clubF1.id, name: "Team F1" });
      await post(`/api/v2/setup/seasons/${seasonF1.id}/team-registrations`,
        { team_id: teamF1.id, league_id: leagueF1.id, division_id: null });
      await post("/api/v2/setup/player",
        { team_id: teamF1.id, name: "P", position: "forward" });
      const venueF1 = await post("/api/v2/setup/venue",
        { name: "VF1", organization_id: null });
      const rinkF1 = await post("/api/v2/setup/rink",
        { venue_id: venueF1.id, name: "RF1" });
      await post("/api/v2/setup/ice-slot", { rink_id: rinkF1.id,
        start_time: "2026-09-01T18:30:00+00:00", end_time: "2026-09-01T20:00:00+00:00",
        slot_type: "game" });
      await post(`/api/v2/setup/seasons/${seasonF1.id}/venue-access`,
        { venue_id: venueF1.id });
      // Program F2: just needs its own Season to import into.
      const progF2 = await post("/api/v2/setup/program",
        { name: "Round7F2 Program", country: "US" });
      const seasonF2 = await post("/api/v2/setup/season",
        { program_id: progF2.id, name: "Season F2" });
      return { progF1: progF1.id, seasonF1: seasonF1.id,
        progF2: progF2.id, seasonF2: seasonF2.id };
    });
    await apiPost(page, "/api/context", { program_id: f.progF1, season_id: f.seasonF1 });
    await freshLoad();
    s = await cardState(page);
    if (!s || s.heading !== "✓ All setup steps complete") {
      fail(`Program F1: expected the complete state, got ${JSON.stringify(s)}`);
    }
    // hierarchy-import.js monkey-patches render() and, the FIRST time
    // view === "import" is ever reached this session, fires its own
    // unawaited follow-up render() (after fetching
    // /api/import/hierarchy-codes) right after the first paint -- which
    // briefly wipes #content back to a loading skeleton before
    // repainting. This is the first visit to Import in this whole
    // checkRoleScenarios run, so it WILL fire; wait for that specific
    // fetch explicitly (registered before the triggering click, so a
    // fast response can't be missed) rather than racing a value-poll
    // against an unbounded second render.
    const hierarchyCodesResp = page.waitForResponse((r) =>
      r.url() === `${base}/api/import/hierarchy-codes` && r.request().method() === "GET")
      .catch(() => null);
    await page.click('[data-setup-progress-action="import"]');
    await hierarchyCodesResp;
    await page.waitForFunction(
      (v) => (document.getElementById("import-season") || {}).value === v,
      f.seasonF1, { timeout: 10000 });
    const importSeasonSelected = await page.$eval("#import-season", (el) => el.value);
    if (importSeasonSelected !== f.seasonF1) {
      fail(`Import: expected the Season select to default to the ACTIVE `
        + `Season F1 (${f.seasonF1}), got ${importSeasonSelected}`);
    }
    // -- Validate then Commit against F1, entirely through the real UI,
    // and assert the ACTUAL POSTED season_id belongs to F1.
    await page.click("[data-import-sample]");
    const validateResp = page.waitForResponse((r) =>
      r.url() === `${base}/api/import/dry-run` && r.request().method() === "POST");
    await page.click("[data-import-validate]");
    await validateResp;
    await page.waitForFunction(() => {
      const btn = document.querySelector("[data-import-commit]");
      return !!(btn && !btn.disabled);
    }, null, { timeout: 10000 });
    const commitResp = page.waitForResponse((r) =>
      r.url() === `${base}/api/import/commit/teams-players` && r.request().method() === "POST");
    await page.click("[data-import-commit]");
    const commitReq = await commitResp;
    const committedBody = JSON.parse(commitReq.request().postData());
    if (committedBody.season_id !== f.seasonF1) {
      fail(`Import commit: expected season_id to be the active Season F1 `
        + `(${f.seasonF1}), got ${committedBody.season_id}`);
    }
    const commitResult = await commitReq.json();
    if (!commitResult || !commitResult.committed) {
      fail(`Import commit against F1 unexpectedly failed: `
        + `${JSON.stringify(commitResult)}`);
    }

    // -- Re-validate (fresh sample, fresh report) so a real, currently-
    // committable state exists, THEN switch F1->F2 via the REAL context
    // switcher while Import is still open -- contextRevision only bumps
    // from setActiveContext(), so a bare apiPost here would leave the
    // client's own importState none the wiser, the same gap round 6's
    // own drawer-seeding regression already had to guard against.
    await page.click("[data-import-sample]");
    const revalidateResp = page.waitForResponse((r) =>
      r.url() === `${base}/api/import/dry-run` && r.request().method() === "POST");
    await page.click("[data-import-validate]");
    await revalidateResp;
    await page.waitForFunction(() => {
      const btn = document.querySelector("[data-import-commit]");
      return !!(btn && !btn.disabled);
    }, null, { timeout: 10000 });
    const switchToF2 = `${f.progF2}|${f.seasonF2}`;
    await page.selectOption("#ctx-select", switchToF2);
    // Wait for the Import view's OWN re-render, not #ctx-select's value --
    // the switcher and #content are painted by the same render() call but
    // not necessarily at the same instant within it, so #ctx-select
    // settling first doesn't prove renderImport() has repainted yet too
    // (this exact gap is what broke the first pass at this scenario).
    await page.waitForFunction(
      (v) => (document.getElementById("import-season") || {}).value === v,
      f.seasonF2, { timeout: 10000 });
    const commitStillEnabled = await page.evaluate(() => {
      const btn = document.querySelector("[data-import-commit]");
      return btn ? !btn.disabled : null;
    });
    if (commitStillEnabled) {
      fail("expected F1's prior validated report to be invalidated after "
        + "switching to Program F2, not still committable");
    }

    // -- Switch to a Program-ONLY context (no Season selected at all) --
    // the explicit "fail closed when no valid in-context Season resolves"
    // requirement, distinct from rebinding Season-to-Season above: the
    // field must show no real selection (never a silent global-first
    // fallback disguised as one), and Commit must stay disabled.
    const switchToF2ProgramOnly = `${f.progF2}|`;
    await page.selectOption("#ctx-select", switchToF2ProgramOnly);
    await page.waitForFunction(
      (v) => document.getElementById("ctx-select").value === v,
      switchToF2ProgramOnly, { timeout: 10000 });
    await page.waitForFunction(
      () => (document.getElementById("import-season") || {}).value === "",
      null, { timeout: 10000 });
    const noSeasonCommitEnabled = await page.evaluate(() => {
      const btn = document.querySelector("[data-import-commit]");
      return btn ? !btn.disabled : null;
    });
    if (noSeasonCommitEnabled) {
      fail("expected Commit to stay disabled with no Season actively "
        + "selected, not fall back to a global default");
    }

    // ---- (G) An open Ice Builder must rebind to, and not silently
    // retain state across, a context switch (#331 review round 7 finding
    // 2): iceBuilder.form/preview were cached once and setActiveContext()
    // never touched them, so a live A preview/commit could target A's
    // Season under a B context header.
    const g = await page.evaluate(async () => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      // Explicit Season dates (#158's own generation range requirement) --
      // the builder's form leaves start_date/end_date blank, meaning to
      // fall back to the Season's own range; a Season created with no
      // dates of its own has none to fall back to, and the real preview
      // endpoint correctly REJECTS that (missing_start_date), which would
      // read here as "no [data-ib-commit]" for an unrelated reason.
      const progG1 = await post("/api/v2/setup/program",
        { name: "Round7G1 Program", country: "US" });
      const seasonG1 = await post("/api/v2/setup/season", { program_id: progG1.id,
        name: "Season G1", start_date: "2026-09-01", end_date: "2027-03-31" });
      const venueG1 = await post("/api/v2/setup/venue",
        { name: "VG1", organization_id: null });
      const rinkG1 = await post("/api/v2/setup/rink",
        { venue_id: venueG1.id, name: "RG1" });
      await post(`/api/v2/setup/seasons/${seasonG1.id}/venue-access`,
        { venue_id: venueG1.id });
      const progG2 = await post("/api/v2/setup/program",
        { name: "Round7G2 Program", country: "US" });
      const seasonG2 = await post("/api/v2/setup/season", { program_id: progG2.id,
        name: "Season G2", start_date: "2026-09-01", end_date: "2027-03-31" });
      const venueG2 = await post("/api/v2/setup/venue",
        { name: "VG2", organization_id: null });
      const rinkG2 = await post("/api/v2/setup/rink",
        { venue_id: venueG2.id, name: "RG2" });
      await post(`/api/v2/setup/seasons/${seasonG2.id}/venue-access`,
        { venue_id: venueG2.id });
      return { progG1: progG1.id, seasonG1: seasonG1.id, rinkG1: rinkG1.id,
        progG2: progG2.id, seasonG2: seasonG2.id, rinkG2: rinkG2.id };
    });
    await apiPost(page, "/api/context", { program_id: g.progG1, season_id: g.seasonG1 });
    await freshLoad();
    await page.click('.tab[data-tab="calendar"]');
    await page.waitForFunction(() => document.body.dataset.view === "calendar",
      null, { timeout: 10000 });
    await page.waitForSelector("[data-ice-builder-open]", { timeout: 10000 });
    await page.click("[data-ice-builder-open]");
    // Polls the field's value directly (see the identical Import-view
    // comment above) rather than a one-shot waitForSelector+$eval pair.
    await page.waitForFunction(
      (v) => (document.getElementById("ib-season") || {}).value === v,
      g.seasonG1, { timeout: 10000 }).catch(() => {});
    const ibSeasonSelected = await page.$eval("#ib-season", (el) => el.value)
      .catch(() => null);
    if (ibSeasonSelected !== g.seasonG1) {
      fail(`Ice Builder: expected the Season select to default to the `
        + `ACTIVE Season G1 (${g.seasonG1}), got ${ibSeasonSelected}`);
    }
    // -- Select G1's own rink and preview under A.
    await page.click(`.ib-rink[value="${g.rinkG1}"]`);
    const previewResp = page.waitForResponse((r) =>
      r.url() === `${base}/api/setup/ice-availability/preview` && r.request().method() === "POST");
    await page.click("[data-ib-preview]");
    await previewResp;
    await page.waitForSelector("[data-ib-commit]", { timeout: 10000 });
    // -- Switch to Program G2 mid-flight, while the builder is still open
    // with a live A preview -- via the REAL context switcher.
    const switchToG2 = `${g.progG2}|${g.seasonG2}`;
    await page.selectOption("#ctx-select", switchToG2);
    // Wait for the Ice Builder's OWN re-render, not #ctx-select's value --
    // same gap as Import above: the switcher and #content are painted by
    // the same render() call but not necessarily at the same instant.
    await page.waitForFunction(
      (v) => (document.getElementById("ib-season") || {}).value === v,
      g.seasonG2, { timeout: 10000 });
    if (await page.$("[data-ib-commit]")) {
      fail("expected A's stale preview/Create action to be gone after "
        + "switching to Program G2 mid-flight, not still committable");
    }
    // -- G1's own rink checkbox must not survive -- the WHOLE form
    // reinitialized, not just season_id.
    const g1RinkStillChecked = await page.evaluate((rinkId) => {
      const el = document.querySelector(`.ib-rink[value="${rinkId}"]`);
      return el ? el.checked : null;
    }, g.rinkG1);
    if (g1RinkStillChecked) {
      fail("expected Program G1's own rink selection to be cleared, not "
        + "carried over, after switching to Program G2");
    }
    // -- A fresh preview+create now targets G2 only, all the way through.
    await page.click(`.ib-rink[value="${g.rinkG2}"]`);
    const previewResp2 = page.waitForResponse((r) =>
      r.url() === `${base}/api/setup/ice-availability/preview` && r.request().method() === "POST");
    await page.click("[data-ib-preview]");
    const previewBody2 = JSON.parse((await previewResp2).request().postData());
    if (previewBody2.season_id !== g.seasonG2) {
      fail(`Ice Builder: expected the fresh preview to target Season G2 `
        + `(${g.seasonG2}), got ${previewBody2.season_id}`);
    }
    await page.waitForSelector("[data-ib-commit]", { timeout: 10000 });
    const commitResp2 = page.waitForResponse((r) =>
      r.url() === `${base}/api/setup/ice-availability/commit` && r.request().method() === "POST");
    await page.click("[data-ib-commit]");
    const commitReq2 = await commitResp2;
    const commitBody2 = JSON.parse(commitReq2.request().postData());
    if (commitBody2.season_id !== g.seasonG2) {
      fail(`Ice Builder: expected the eventual commit to target Season `
        + `G2 (${g.seasonG2}), got ${commitBody2.season_id}`);
    }
    const commitResult2 = await commitReq2.json();
    if (!commitResult2 || !commitResult2.totals || !commitResult2.totals.created) {
      fail(`Ice Builder commit against G2 unexpectedly failed: `
        + `${JSON.stringify(commitResult2)}`);
    }

    // ---- (H) contextSeededDrawerValues()'s own in-flight-hierarchy-fetch
    // race must close the moment a switch is ATTEMPTED, not once it's
    // CONFIRMED (#331 review round 8): its stillCurrent check used to
    // compare against contextOptions.selected.program_id, which
    // setActiveContext() doesn't update until ITS OWN /api/context POST
    // succeeds -- a switch merely INITIATED (not yet confirmed) while this
    // fetch was in flight used to still read as "unchanged" and let a
    // stale seed win. Proven with BOTH possible response orders, since
    // neither may open a drawer with H1's seed once H2 was even attempted.
    const h = await page.evaluate(async () => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      const progH1 = await post("/api/v2/setup/program",
        { name: "Round8H1 Program", country: "US" });
      const seasonH1 = await post("/api/v2/setup/season",
        { program_id: progH1.id, name: "Season H1" });
      const leagueH1 = await post("/api/v2/setup/league",
        { season_id: seasonH1.id, name: "League H1" });
      const progH2 = await post("/api/v2/setup/program",
        { name: "Round8H2 Program", country: "US" });
      return { progH1: progH1.id, seasonH1: seasonH1.id,
        leagueH1: leagueH1.id, progH2: progH2.id };
    });
    const switchToH2 = `${h.progH2}|`;
    const openH1TeamDrawer = async () => {
      await apiPost(page, "/api/context", { program_id: h.progH1, season_id: h.seasonH1 });
      await freshLoad();
      s = await cardState(page);
      if (!s || s.nextTitle !== "Permanent teams") {
        fail(`(H) Program H1: expected next = "Permanent teams", got ${JSON.stringify(s)}`);
      }
    };

    // Sub-case 1: the hierarchy fetch resolves BEFORE the context POST.
    await openH1TeamDrawer();
    let releaseHierarchyH1, releaseContextH1;
    const hierarchyHoldH1 = new Promise((resolve) => { releaseHierarchyH1 = resolve; });
    const contextHoldH1 = new Promise((resolve) => { releaseContextH1 = resolve; });
    await page.route("**/api/v2/setup/hierarchy", async (route) => { await hierarchyHoldH1; await route.continue(); });
    await page.route("**/api/context", async (route) => { await contextHoldH1; await route.continue(); });
    await page.click("[data-setup-progress-action]");
    await new Promise((r) => setTimeout(r, 200));  // let the click's own fetch actually start
    await page.selectOption("#ctx-select", switchToH2);
    await new Promise((r) => setTimeout(r, 200));  // let setActiveContext's synchronous Phase 1 run
    releaseHierarchyH1();
    await new Promise((r) => setTimeout(r, 200));
    releaseContextH1();
    await page.unroute("**/api/v2/setup/hierarchy");
    await page.unroute("**/api/context");
    await page.waitForFunction((v) => document.getElementById("ctx-select").value === v,
      switchToH2, { timeout: 10000 });
    // #ctx-select's own value is an EARLY signal within a render() cycle
    // (renderContextSwitcher() runs partway through it, same gap steps
    // F/G/K's own comments document elsewhere in this file) -- but here BOTH
    // goToSetupWorkflow()'s own switchTab("setup")-triggered render() and
    // setActiveContext()'s own completion render() can be in flight
    // together, and only the LAST one to actually finish painting decides
    // what #content shows. Checking .drawer right after the switcher's own
    // value settles caught this mid-flight and missed a real stale-drawer
    // paint entirely in testing -- wait for the network to actually go
    // quiet (both renders' own fetch chains fully drained) before asserting.
    await page.waitForLoadState("networkidle").catch(() => {});
    if (await page.$(".drawer")) {
      fail("(H, hierarchy-first) expected NO drawer to open with H1's stale "
        + "seed after a switch to H2 was merely ATTEMPTED while the seed "
        + "fetch was still in flight");
    }

    // Sub-case 2: the context POST resolves BEFORE the hierarchy fetch.
    await openH1TeamDrawer();
    let releaseHierarchyH2b, releaseContextH2b;
    const hierarchyHoldH2b = new Promise((resolve) => { releaseHierarchyH2b = resolve; });
    const contextHoldH2b = new Promise((resolve) => { releaseContextH2b = resolve; });
    await page.route("**/api/v2/setup/hierarchy", async (route) => { await hierarchyHoldH2b; await route.continue(); });
    await page.route("**/api/context", async (route) => { await contextHoldH2b; await route.continue(); });
    await page.click("[data-setup-progress-action]");
    await new Promise((r) => setTimeout(r, 200));
    await page.selectOption("#ctx-select", switchToH2);
    await new Promise((r) => setTimeout(r, 200));
    releaseContextH2b();
    await new Promise((r) => setTimeout(r, 200));
    releaseHierarchyH2b();
    await page.unroute("**/api/v2/setup/hierarchy");
    await page.unroute("**/api/context");
    await page.waitForFunction((v) => document.getElementById("ctx-select").value === v,
      switchToH2, { timeout: 10000 });
    // Same reasoning as sub-case 1 above: wait for the network (both
    // renders' own fetch chains) to actually go quiet, not a fixed delay,
    // before asserting against #content.
    await page.waitForLoadState("networkidle").catch(() => {});
    if (await page.$(".drawer")) {
      fail("(H, context-first) expected NO drawer to open with H1's stale "
        + "seed after H2 was already confirmed by the time the seed fetch "
        + "resolved");
    }

    // ---- (I) An ALREADY-OPEN, already-seeded drawer must not survive a
    // context switch reached via keyboard (#331 review round 8): with no
    // dialog focus trap (#113, tracked separately as post-critical), a
    // keyboard user can tab out of an open drawer to the switcher behind
    // it -- exactly how this finding is reachable without a mouse. h.progH2
    // already exists as a selectable #ctx-select option (created in H's own
    // fixture above, before any freshLoad in this scenario), so no extra
    // reload is needed here to make it choosable while the drawer is open.
    await openH1TeamDrawer();
    await page.click("[data-setup-progress-action]");
    await page.waitForSelector("#f-team-perm-league", { timeout: 10000 });
    const iSelected = await page.$eval("#f-team-perm-league", (el) => el.value);
    if (iSelected !== h.leagueH1) {
      fail(`(I) drawer must default to H1's own League (${h.leagueH1}), got ${iSelected}`);
    }
    // Start with focus INSIDE the drawer (proving it was really there), then
    // land it on the switcher -- standing in for "tabbed out, no trap
    // stopped it" without a brittle full Tab-order walk through this
    // drawer's exact field count.
    await page.focus("#f-team-perm-league");
    await page.focus("#ctx-select");
    await page.selectOption("#ctx-select", switchToH2);
    await page.waitForFunction((v) => document.getElementById("ctx-select").value === v,
      switchToH2, { timeout: 10000 });
    if (await page.$(".drawer") || await page.$(".drawer-scrim")) {
      fail("(I) expected the open drawer to be DISMISSED immediately after "
        + "a keyboard-driven context switch, not still present");
    }
    const focusedAfterI = await page.evaluate(() =>
      document.activeElement && document.activeElement.id);
    if (focusedAfterI !== "ctx-select") {
      fail(`(I) expected focus to land/stay on #ctx-select after the switch `
        + `dismissed the drawer (never left stranded in removed content), `
        + `got ${JSON.stringify(focusedAfterI)}`);
    }

    // ---- (J) Import Commit must be uncommittable the INSTANT a context
    // switch is attempted, not once its own /api/context POST resolves
    // (#331 review round 8): validatedKey used to stay live for the WHOLE
    // round trip, so a Commit landing in that window would still target J1
    // even though the switcher already shows J2.
    const j = await page.evaluate(async () => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      const progJ1 = await post("/api/v2/setup/program",
        { name: "Round8J1 Program", country: "US" });
      const seasonJ1 = await post("/api/v2/setup/season",
        { program_id: progJ1.id, name: "Season J1" });
      const progJ2 = await post("/api/v2/setup/program",
        { name: "Round8J2 Program", country: "US" });
      const seasonJ2 = await post("/api/v2/setup/season",
        { program_id: progJ2.id, name: "Season J2" });
      return { progJ1: progJ1.id, seasonJ1: seasonJ1.id,
        progJ2: progJ2.id, seasonJ2: seasonJ2.id };
    });
    await apiPost(page, "/api/context", { program_id: j.progJ1, season_id: j.seasonJ1 });
    await freshLoad();
    // hierarchy-import.js's own one-time-PER-PAGE-LOAD hierarchy-codes race
    // (see seedImportAndIceForM()'s identical comment further below) --
    // this Import visit is the first since the freshLoad() just above, so
    // it applies here too.
    const jHierarchyCodesResp = page.waitForResponse((r) =>
      r.url() === `${base}/api/import/hierarchy-codes` && r.request().method() === "GET")
      .catch(() => null);
    await page.click('.tab[data-tab="import"]');
    await jHierarchyCodesResp;
    await page.waitForFunction(
      (v) => (document.getElementById("import-season") || {}).value === v,
      j.seasonJ1, { timeout: 10000 });
    await page.click("[data-import-sample]");
    const jValidateResp = page.waitForResponse((r) =>
      r.url() === `${base}/api/import/dry-run` && r.request().method() === "POST");
    await page.click("[data-import-validate]");
    await jValidateResp;
    await page.waitForFunction(() => {
      const btn = document.querySelector("[data-import-commit]");
      return !!(btn && !btn.disabled);
    }, null, { timeout: 10000 });

    let releaseContextJ;
    const contextHoldJ = new Promise((resolve) => { releaseContextJ = resolve; });
    await page.route("**/api/context", async (route) => { await contextHoldJ; await route.continue(); });
    const switchToJ2 = `${j.progJ2}|${j.seasonJ2}`;
    await page.selectOption("#ctx-select", switchToJ2);
    await new Promise((r) => setTimeout(r, 200));  // let Phase 1's synchronous clear actually run

    const seenCommitsJ = [];
    const trackJ = (req) => {
      if (req.url() === `${base}/api/import/commit/teams-players` && req.method() === "POST") {
        seenCommitsJ.push(req.url());
      }
    };
    page.on("request", trackJ);
    await page.click("[data-import-commit]").catch(() => {});
    await new Promise((r) => setTimeout(r, 300));
    page.off("request", trackJ);
    if (seenCommitsJ.length) {
      fail("(J) expected no Import commit request while the switch to J2 "
        + "was still pending");
    }
    releaseContextJ();
    await page.unroute("**/api/context");
    await page.waitForFunction(
      (v) => (document.getElementById("import-season") || {}).value === v,
      j.seasonJ2, { timeout: 10000 });
    const jCommitStillEnabled = await page.evaluate(() => {
      const btn = document.querySelector("[data-import-commit]");
      return btn ? !btn.disabled : null;
    });
    if (jCommitStillEnabled) {
      fail("(J) expected Commit to require fresh J2 validation once the "
        + "switch settled, not stay enabled from J1's stale review");
    }

    // ---- (K) Ice Builder Create must be uncommittable the INSTANT a
    // context switch is attempted too (#331 review round 8) -- the
    // identical gap as Import's own Commit above, closed the same way
    // (clearing iceBuilder.preview synchronously, before
    // setActiveContext()'s POST). Season dates are explicit (#158's own
    // generation-range requirement, learned in round 7): a Season with
    // neither its own dates nor the builder's own date fields set is
    // correctly rejected by the backend, which would read here as "no
    // [data-ib-commit]" for an unrelated reason.
    const k = await page.evaluate(async () => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      const progK1 = await post("/api/v2/setup/program",
        { name: "Round8K1 Program", country: "US" });
      const seasonK1 = await post("/api/v2/setup/season", { program_id: progK1.id,
        name: "Season K1", start_date: "2026-09-01", end_date: "2027-03-31" });
      const venueK1 = await post("/api/v2/setup/venue",
        { name: "VK1", organization_id: null });
      const rinkK1 = await post("/api/v2/setup/rink",
        { venue_id: venueK1.id, name: "RK1" });
      await post(`/api/v2/setup/seasons/${seasonK1.id}/venue-access`,
        { venue_id: venueK1.id });
      const progK2 = await post("/api/v2/setup/program",
        { name: "Round8K2 Program", country: "US" });
      const seasonK2 = await post("/api/v2/setup/season", { program_id: progK2.id,
        name: "Season K2", start_date: "2026-09-01", end_date: "2027-03-31" });
      return { progK1: progK1.id, seasonK1: seasonK1.id, rinkK1: rinkK1.id,
        progK2: progK2.id, seasonK2: seasonK2.id };
    });
    await apiPost(page, "/api/context", { program_id: k.progK1, season_id: k.seasonK1 });
    await freshLoad();
    await page.click('.tab[data-tab="calendar"]');
    await page.waitForFunction(() => document.body.dataset.view === "calendar",
      null, { timeout: 10000 });
    await page.waitForSelector("[data-ice-builder-open]", { timeout: 10000 });
    await page.click("[data-ice-builder-open]");
    await page.waitForFunction(
      (v) => (document.getElementById("ib-season") || {}).value === v,
      k.seasonK1, { timeout: 10000 });
    await page.click(`.ib-rink[value="${k.rinkK1}"]`);
    const kPreviewResp = page.waitForResponse((r) =>
      r.url() === `${base}/api/setup/ice-availability/preview` && r.request().method() === "POST");
    await page.click("[data-ib-preview]");
    await kPreviewResp;
    await page.waitForSelector("[data-ib-commit]", { timeout: 10000 });

    let releaseContextK;
    const contextHoldK = new Promise((resolve) => { releaseContextK = resolve; });
    await page.route("**/api/context", async (route) => { await contextHoldK; await route.continue(); });
    const switchToK2 = `${k.progK2}|${k.seasonK2}`;
    await page.selectOption("#ctx-select", switchToK2);
    await new Promise((r) => setTimeout(r, 200));

    const seenCreatesK = [];
    const trackK = (req) => {
      if (req.url() === `${base}/api/setup/ice-availability/commit` && req.method() === "POST") {
        seenCreatesK.push(req.url());
      }
    };
    page.on("request", trackK);
    await page.click("[data-ib-commit]").catch(() => {});
    await new Promise((r) => setTimeout(r, 300));
    page.off("request", trackK);
    if (seenCreatesK.length) {
      fail("(K) expected no Ice Builder create request while the switch to "
        + "K2 was still pending");
    }
    releaseContextK();
    await page.unroute("**/api/context");
    await page.waitForFunction(
      (v) => (document.getElementById("ib-season") || {}).value === v,
      k.seasonK2, { timeout: 10000 });
    if (await page.$("[data-ib-commit]")) {
      fail("(K) expected Create to require a fresh K2 preview once the "
        + "switch settled, not stay committable from K1's stale one");
    }
    // -- Program-only context remains fail-closed, re-confirmed here under
    // a PREVIOUSLY-live builder (step G's own coverage only opened it
    // fresh): select K2's Program-only entry (no Season) and the builder's
    // Season select must show no real selection (#331 review round 8's own
    // defaultIceForm()/renderIceBuilder() fix).
    const switchToK2ProgramOnly = `${k.progK2}|`;
    await page.selectOption("#ctx-select", switchToK2ProgramOnly);
    // Wait for the Ice Builder's OWN re-render, not #ctx-select's value --
    // the switcher and #content are painted by the same render() call but
    // not necessarily at the same instant within it (same gap documented
    // throughout steps F/G above).
    await page.waitForFunction(
      () => (document.getElementById("ib-season") || {}).value === "",
      null, { timeout: 10000 });
    const ibSeasonAfterProgramOnly = await page.$eval("#ib-season", (el) => el.value).catch(() => null);
    if (ibSeasonAfterProgramOnly) {
      fail(`(K) expected no Season selected in the builder under a `
        + `Program-only context, got ${ibSeasonAfterProgramOnly}`);
    }

    // ---- (L) Rapid A->B->C with DELIBERATELY REVERSED response order:
    // only the LATEST switch attempt may ever apply its own
    // POST/refresh/render result (#331 review round 8's contextSwitchSeq),
    // regardless of which response happens to arrive first over the wire.
    const l = await page.evaluate(async () => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      const progL1 = await post("/api/v2/setup/program", { name: "Round8L1 Program", country: "US" });
      const progL2 = await post("/api/v2/setup/program", { name: "Round8L2 Program", country: "US" });
      const progL3 = await post("/api/v2/setup/program", { name: "Round8L3 Program", country: "US" });
      return { progL1: progL1.id, progL2: progL2.id, progL3: progL3.id };
    });
    await apiPost(page, "/api/context", { program_id: l.progL1, season_id: null });
    await freshLoad();
    const holdsL = {}; const releasersL = {};
    [l.progL1, l.progL2, l.progL3].forEach((pid) => {
      holdsL[pid] = new Promise((resolve) => { releasersL[pid] = resolve; });
    });
    await page.route("**/api/context", async (route) => {
      let pid = null;
      try { pid = JSON.parse(route.request().postData() || "{}").program_id; } catch (_) {}
      if (holdsL[pid]) await holdsL[pid];
      await route.continue();
    });
    const switchToL1 = `${l.progL1}|`;
    const switchToL2 = `${l.progL2}|`;
    const switchToL3 = `${l.progL3}|`;
    await page.selectOption("#ctx-select", switchToL2);
    await page.selectOption("#ctx-select", switchToL1);
    await page.selectOption("#ctx-select", switchToL3);
    // Reversed release order: L3 (the LATEST attempt) resolves first, then
    // L1, then L2 (the two SUPERSEDED attempts) -- deliberately the
    // opposite of the order they were initiated in.
    releasersL[l.progL3]();
    await new Promise((r) => setTimeout(r, 150));
    releasersL[l.progL1]();
    await new Promise((r) => setTimeout(r, 150));
    releasersL[l.progL2]();
    await page.unroute("**/api/context");
    await page.waitForFunction((v) => document.getElementById("ctx-select").value === v,
      switchToL3, { timeout: 10000 });
    await new Promise((r) => setTimeout(r, 300));  // let any superseded response's own handler return
    const lFinalValue = await page.$eval("#ctx-select", (el) => el.value);
    if (lFinalValue !== switchToL3) {
      fail(`(L) expected the switcher to settle on L3 (${switchToL3}) `
        + `regardless of response order, got ${lFinalValue}`);
    }
    const lFinalHash = await page.evaluate(() => location.hash);
    if (lFinalHash.indexOf(encodeURIComponent(l.progL3).slice(0, 6)) === -1
        && lFinalHash.indexOf(l.progL3) === -1) {
      // The hash is base64url-encoded JSON, not the raw id -- decode it
      // properly rather than substring-matching the id, which would never
      // actually appear verbatim.
      const decoded = await page.evaluate(() => {
        try {
          const b64 = location.hash.slice(5).replace(/-/g, "+").replace(/_/g, "/");
          return JSON.parse(atob(b64));
        } catch (_) { return null; }
      });
      if (!decoded || decoded.p !== l.progL3) {
        fail(`(L) expected the URL hash to encode L3 (${l.progL3}), got `
          + `${JSON.stringify(decoded)} from ${lFinalHash}`);
      }
    }

    // ---- (M) A no-reload IDENTITY switch must clear the SAME
    // context-scoped mutation state a Program/Season switch does (#331
    // review round 8): resetTransientUiState() -- already the
    // identity-transition hook setUser() calls -- used to leave
    // importState and iceBuilder completely untouched, so a persona swap
    // (the demo role-switch dropdown) or a sign-out immediately followed by
    // a different sign-in could hand the NEXT identity an in-progress paste
    // (real player names/emails), a validated report, or a live Ice
    // preview/Create action the FIRST identity never meant to share.
    const m = await page.evaluate(async () => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      const progM1 = await post("/api/v2/setup/program",
        { name: "Round8M1 Program", country: "US" });
      const seasonM1 = await post("/api/v2/setup/season", { program_id: progM1.id,
        name: "Season M1", start_date: "2026-09-01", end_date: "2027-03-31" });
      const venueM1 = await post("/api/v2/setup/venue",
        { name: "VM1", organization_id: null });
      const rinkM1 = await post("/api/v2/setup/rink",
        { venue_id: venueM1.id, name: "RM1" });
      await post(`/api/v2/setup/seasons/${seasonM1.id}/venue-access`,
        { venue_id: venueM1.id });
      return { progM1: progM1.id, seasonM1: seasonM1.id, rinkM1: rinkM1.id };
    });
    const seedImportAndIceForM = async () => {
      // hierarchy-import.js monkey-patches render() and, the FIRST time
      // view === "import" is reached AFTER EACH PAGE LOAD -- not just once
      // ever in the whole run, as step F's own original comment assumed;
      // hierarchyExistingCodes is a plain JS variable, reset by every
      // freshLoad() -- fires its own unawaited follow-up render() (after
      // fetching /api/import/hierarchy-codes) right after the first paint,
      // which briefly wipes #content back to a loading skeleton before
      // repainting. seedImportAndIceForM() is always called right after its
      // own freshLoad() above, so this IS always such a first visit. Wait
      // for that specific fetch explicitly (registered before the
      // triggering click) rather than racing a value-poll against an
      // unbounded second render.
      const hierarchyCodesResp = page.waitForResponse((r) =>
        r.url() === `${base}/api/import/hierarchy-codes` && r.request().method() === "GET")
        .catch(() => null);
      await page.click('.tab[data-tab="import"]');
      await hierarchyCodesResp;
      await page.waitForSelector("[data-import-sample]", { timeout: 10000 });
      const fieldId = await page.evaluate(() => {
        const el = document.querySelector('textarea[id^="import-"]');
        return el ? el.id : null;
      });
      if (!fieldId) fail("(M) expected at least one Import sheet textarea to seed a paste into");
      await page.fill(`#${fieldId}`, "name,position\nAlice PII-Example,forward\nalice-pii@example.test,forward");
      await page.click('.tab[data-tab="calendar"]');
      await page.waitForFunction(() => document.body.dataset.view === "calendar",
        null, { timeout: 10000 });
      await page.waitForSelector("[data-ice-builder-open]", { timeout: 10000 });
      await page.click("[data-ice-builder-open]");
      await page.waitForFunction(
        (v) => (document.getElementById("ib-season") || {}).value === v,
        m.seasonM1, { timeout: 10000 });
      await page.click(`.ib-rink[value="${m.rinkM1}"]`);
      const previewResp = page.waitForResponse((r) =>
        r.url() === `${base}/api/setup/ice-availability/preview` && r.request().method() === "POST");
      await page.click("[data-ib-preview]");
      await previewResp;
      await page.waitForSelector("[data-ib-commit]", { timeout: 10000 });
    };
    const assertMClearedFor = async (label) => {
      await page.waitForFunction(() => !document.querySelector(".skeleton"),
        null, { timeout: 10000 });
      if (await page.$("[data-ib-commit]") || await page.$(".ib-form")) {
        fail(`(M, ${label}) expected the Ice Builder to be fully closed for `
          + `the newly signed-in identity, not still showing the prior `
          + `identity's open form/preview`);
      }
      await page.click('.tab[data-tab="import"]');
      await page.waitForSelector("[data-import-sample]", { timeout: 10000 });
      const textAfter = await page.evaluate(() => {
        const el = document.querySelector('textarea[id^="import-"]');
        return el ? el.value : null;
      });
      if (textAfter) {
        fail(`(M, ${label}) expected Import's pasted text to be gone for `
          + `the newly signed-in identity, got: ${JSON.stringify(textAfter)}`);
      }
    };

    // -- Leg 1: the in-app persona switch (demo role-switch dropdown), no
    // sign-out, no reload -- "arena" also carries manage_arena, so this is
    // the actual disclosure risk (same-or-higher privilege seeing a prior
    // operator's live state), not merely an authorization gap redaction
    // elsewhere already covers.
    await apiPost(page, "/api/context", { program_id: m.progM1, season_id: m.seasonM1 });
    await freshLoad();
    await seedImportAndIceForM();
    await page.selectOption("#role-switch", "arena");
    await page.waitForFunction(
      () => (document.getElementById("user-name") || {}).textContent === "arena",
      null, { timeout: 10000 });
    await assertMClearedFor("persona switch");

    // -- Leg 2: a real sign-out immediately followed by a DIFFERENT
    // identity's sign-in through the actual login screen -- not this
    // file's own raw-fetch logout()/loginAs() helpers, which bypass
    // signIn()/setUser() entirely and so would prove nothing about the fix
    // under test here.
    await logout(page);
    await loginAs(page, "admin", "demo");
    await apiPost(page, "/api/context", { program_id: m.progM1, season_id: m.seasonM1 });
    await freshLoad();
    await seedImportAndIceForM();
    await page.waitForLoadState("networkidle").catch(() => {});
    await page.click("#signout-btn");
    await page.waitForSelector('[data-persona="arena"]', { state: "visible", timeout: 10000 });
    await page.click('[data-persona="arena"]');
    await page.waitForFunction(
      () => (document.getElementById("user-name") || {}).textContent === "arena",
      null, { timeout: 10000 });
    await assertMClearedFor("sign-out/sign-in");
    // Restore the admin session this file's remaining teardown expects.
    await logout(page);
    await loginAs(page, "admin", "demo");

    if (errors.length) {
      fail(`console/page errors:\n${errors.join("\n")}`);
    }
    console.log(`[${viewport.label}] OK — Register Team stays scoped to the `
      + `focused Season even when another Season shares its League, Arena `
      + `Manager's setup-progress view is redacted to only what they can `
      + `manage and gets actionable guidance instead of a dead-end CTA when `
      + `blocked, the complete-state Import action is withheld from a role `
      + `that cannot use it, hub-driven create drawers seed from the active `
      + `Program/Season rather than falling through to a global default, `
      + `and that seeding fails closed (no drawer, an actionable retry) on `
      + `a hierarchy-fetch error or a Program switch mid-flight.`);
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
    for (const viewport of ROLE_VIEWPORTS) await checkRoleScenarios(browser, viewport);
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
