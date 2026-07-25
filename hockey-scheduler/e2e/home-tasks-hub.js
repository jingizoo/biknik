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
        dashboardReady: !!document.querySelector(".dash-stats") };
    });
    if (!midFlight.slotShowsSkeleton || !midFlight.dashboardReady) {
      fail(`expected the card's own loading skeleton while the rest of the `
        + `Dashboard is already ready, got ${JSON.stringify(midFlight)}`);
    }
    releaseDelay();
    await page.unroute("**/api/v2/setup/progress");
    await waitForCompleteHeading(page);

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
    // (season s2 is active, not archived), and the response's `workflows`
    // is redacted to just that one entry, never the League-Admin-only
    // league_season/teams/participation/roster detail this Program now
    // carries (participation just went done above -- an exact count Arena
    // Manager must never receive).
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
    await freshLoad();
    s = await cardState(page);
    if (!s || s.heading !== "✓ All setup steps complete") {
      fail(`Program C, Arena Manager: expected the complete state, got ${JSON.stringify(s)}`);
    }
    if (s.primaryLabel !== "Go to Schedule") {
      fail(`Arena Manager must keep the "Go to Schedule" primary action, `
        + `got ${JSON.stringify(s)}`);
    }
    importBtn = await page.$('[data-setup-progress-action="import"]');
    if (importBtn) {
      fail("Arena Manager must never receive an enabled Import action "
        + "(MANAGE_SETUP-only) in the complete state");
    }
    await assertCardHasNoA11yViolations(page, viewport.label,
      "complete state (Arena Manager, no Import)");
    // Keyboard-operable Schedule stays reachable for Arena Manager too.
    await page.evaluate(() => {
      const b = Array.from(document.querySelectorAll(".dash-card .act.primary"))
        .find((x) => x.textContent.trim() === "Go to Schedule");
      b.focus();
    });
    await page.keyboard.press("Enter");
    await page.waitForFunction(
      () => document.body.dataset.view === "calendar", null, { timeout: 10000 });

    if (errors.length) {
      fail(`console/page errors:\n${errors.join("\n")}`);
    }
    console.log(`[${viewport.label}] OK — Register Team stays scoped to the `
      + `focused Season even when another Season shares its League, Arena `
      + `Manager's setup-progress view is redacted to only what they can `
      + `manage and gets actionable guidance instead of a dead-end CTA when `
      + `blocked, and the complete-state Import action is withheld from a `
      + `role that cannot use it.`);
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
