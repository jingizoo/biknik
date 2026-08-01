// Setup workflow hard-prerequisite floors (#365 review round 3) — a Setup
// card must consume the COMPLETE ordered set of server-asserted floors, not
// one of them.
//
// THE DEFECT THIS EXISTS FOR, verbatim from the review: "an archived selected
// Season with an active grant exposes a dead-end Add Ice primary. […] The
// progress row reports `prerequisites: [{key: 'venue_access', met: true}]`;
// the card settles READY/TODO with Venues=1, Rinks=1, `blockedBecause: null`,
// and primary Add Ice. But the Season is read-only and the backend's own
// `_workflow_prerequisite_gap` rejects Facilities with `season_archived`."
//
// Round 2 hand-picked ONE asserted capability fact (venue_access) and treated
// it as the WHOLE workflow capability. The fix derives the complete ordered
// set from the same server computation `_workflow_prerequisite_gap` refuses
// with — for Facilities: selected Season present, that Season ACTIVE, then
// venue access; for Participation: selected Season present, that Season
// ACTIVE, then team/league eligibility — and the per-card effective-action
// model consumes them FAIL-CLOSED under its existing card + tuple +
// generation identity.
//
// WHAT IT ASSERTS, at desktop and canonical 390x844, and why each leg is a
// real risk rather than markup presence:
//
//  (A) FACILITIES, ARCHIVED SEASON, LIVE GRANT — the reviewer's own
//      reproduction. Deliberately NON-VACUOUS twice over: the Venue and Rink
//      counts are NON-ZERO (so this is not an EMPTY card, which offers no
//      "Add Ice" anyway and would prove nothing) AND the SeasonVenueAccess
//      grant is ACTIVE (so round 2's own floor asserts met and cannot be what
//      is blocking). What remains is exactly the new hole: a read-only Season.
//        * EXACT TUPLE IDENTITY — card id + this Program + this Season +
//          League, matching the live context tuple.
//        * NO "Add Ice" CONTROL, NO ROUTE TO THE BUILDER, AND NO ICE-PREVIEW
//          REQUEST — the button's absence is the weak claim; that no path
//          reached the Ice Availability Builder at all is the strong one.
//        * ROLE-CORRECT REOPEN/GUIDANCE — League Admin (MANAGE_SETUP, which
//          is exactly what /api/v2/setup/seasons/<id>/reopen requires) gets
//          the REAL reopen path and nothing else; Arena Manager (MANAGE_ARENA
//          only) gets NO mutation control at all plus guidance naming the
//          league admin. Offering an Arena Manager a reopen button would be a
//          403 in waiting — the same dead end one click along.
//
//  (B) RECOVERY THROUGH THE REAL ENTRY POINT — the reopen is performed from
//      that control (a card-scoped confirmation with the reason #159
//      requires), not a raw fetch, and the SAME card must advance to "Add
//      Ice" on its OWN refresh: no page reload, no adjacent card's generation
//      or committed model touched. Without this leg the withdrawal above
//      could pass by withdrawing "Add Ice" forever.
//
//  (C) PARTICIPATION, ARCHIVED SEASON — otherwise valid League + Division +
//      Team, so its eligibility floor asserts met and the archived Season is
//      the only blocker. "Register Team" must be withdrawn, and the offered
//      action must be the reopen path.
//
//  (D) PARTICIPATION, TEAM/LEAGUE ELIGIBILITY — an ACTIVE Season whose
//      visible Teams all fail the backend's rule-7 floor. Non-vacuous: the
//      Teams really are visible and the Divisions count is non-zero, so the
//      card is READY with real records. "Register Team" must remain withdrawn
//      until the REAL blocker is repaired — which the leg then does, by
//      creating a Team in a league this Season actually runs.
//
// Fails on any browser console/page error.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8393 },
  { label: "phone", width: 390, height: 844, port: 8394 },
];

// The one route that proves a dead end was actually reachable.
const ICE_PREVIEW = "/api/setup/ice-availability/preview";

function waitForServer(url, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const tick = () => {
      const req = http.get(url, (res) => { res.resume(); resolve(); });
      req.setTimeout(2000, () => req.destroy(new Error("request timed out")));
      req.on("error", () => {
        if (Date.now() > deadline) reject(new Error(`server never came up at ${url}`));
        else setTimeout(tick, 200);
      });
    };
    tick();
  });
}

function stopServer(server) {
  return new Promise((resolve) => {
    if (!server || server.exitCode !== null) return resolve();
    server.once("exit", () => resolve());
    server.kill("SIGTERM");
    setTimeout(() => { try { server.kill("SIGKILL"); } catch (e) {} resolve(); }, 3000);
  });
}

async function apiPost(page, path_, body) {
  return page.evaluate(async ([p, b]) => (await fetch(p, {
    method: "POST", credentials: "same-origin",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(b || {}),
  })).json(), [path_, body || {}]);
}

async function apiGet(page, path_) {
  return page.evaluate(async (p) =>
    (await fetch(p, { credentials: "same-origin" })).json(), path_);
}

async function loginAs(page, username, password) {
  const res = await apiPost(page, "/api/auth/login", { username, password });
  if (!res || res.error) {
    throw new Error(`login as ${username} failed: ${JSON.stringify(res)}`);
  }
}

// A fresh page load in the given context. `contextOptions` is seeded once per
// page load and never re-polled by render(), so a raw /api/context POST needs
// this to take effect — and the leftover "#ctx=" deep link has to go first, or
// bootstrap() faithfully POSTs the PREVIOUS selection straight back over it.
async function reenter(page, base) {
  await page.evaluate(() => history.replaceState(
    null, "", location.pathname + location.search));
  await page.goto(base, { waitUntil: "domcontentloaded" });
  await page.waitForSelector("#content > *", { timeout: 15000 });
}

// Open a workflow LANDING through a real, permission-gated entry point: the
// Setup hub's own "Open …" button, which is the transition every role uses.
async function openLanding(page, key, step) {
  await page.click('.tab[data-tab="setup"]:not([data-setup-workflow-nav])');
  await page.waitForSelector(`[data-setup-workflow="${key}"]`, { timeout: 15000 })
    .catch(() => { throw new Error(`[${step}] the Setup hub never offered "${key}"`); });
  await page.click(`[data-setup-workflow="${key}"]`);
  await page.waitForSelector(`[data-setup-workflow-landing="${key}"]`,
    { timeout: 15000 }).catch(() => {
      throw new Error(`[${step}] the ${key} landing never rendered`);
    });
  // Actions and copy mean nothing until the card has SETTLED: LOADING
  // withdraws every action group by design, so sampling mid-flight would read
  // zero controls and blame the wrong thing.
  await page.waitForFunction((k) =>
    readCardState(`setup/${k}`).state !== "loading", key, { timeout: 15000 });
}

// Everything the assertions below need, read from the card's own committed
// MODEL plus the DOM it produced — never re-derived in the test.
async function readLanding(page, key) {
  return page.evaluate((k) => {
    const root = document.querySelector(`[data-setup-workflow-landing="${k}"]`);
    const entry = readCardState(`setup/${k}`);
    const box = root && root.querySelector("[data-setup-landing-actions]");
    const note = root && root.querySelector(".swf-card-blocked, .swf-card-empty");
    const sel = (contextOptions && contextOptions.selected) || {};
    return {
      state: entry.state,
      status: entry.status || null,
      identity: entry.identity || null,
      selected: { program_id: sel.program_id || null, season_id: sel.season_id || null,
                  league_id: sel.league_id || null },
      stats: (entry.stats || []).map((s) => ({ label: s.label, n: s.n })),
      blockedBecause: entry.blockedBecause || null,
      blockedAdvice: entry.blockedAdvice || null,
      // `undefined` (never derived) is a DIFFERENT answer from `null` (derived,
      // and this role can resolve nothing) -- the Arena Manager leg turns on
      // exactly that distinction, so it must not be flattened here.
      effective: entry.effective === undefined ? "undefined"
        : entry.effective === null ? null : entry.effective.label,
      buttons: box ? Array.from(box.querySelectorAll("button"))
        .map((b) => b.textContent.trim()) : [],
      // Routes by their WIRING, not merely by label: the model could
      // substitute a differently-labelled control that still opened the
      // builder / the register deep link.
      goRoutes: root ? Array.from(root.querySelectorAll("[data-setup-workflow-go]"))
        .map((b) => b.dataset.setupWorkflowGo) : null,
      note: note ? note.textContent.replace(/\s+/g, " ").trim() : null,
    };
  }, key);
}

// The ASSERTED prerequisite rows this card actually received, in order,
// straight off the live progress read. The whole contract is that the client
// holds the SERVER's complete ordered set, so the journey checks the set
// itself and not only its consequences.
async function readAssertedRows(page, key) {
  return page.evaluate(async (k) => {
    const pr = await (await fetch("/api/v2/setup/progress",
                                  { credentials: "same-origin" })).json();
    const row = ((pr && pr.workflows) || []).find((w) => w.key === k);
    return { rows: (row && row.prerequisites) || null,
             nextBlocked: pr && pr.next_blocked ? pr.next_blocked.key : null };
  }, key);
}

function fail(msg) { throw new Error(msg); }

// Tuple identity, asserted identically everywhere. Without it every claim
// below could be satisfied by a model bound to a neighbouring tuple.
function assertIdentity(got, L, step, key, fx) {
  const id = got.identity;
  if (!id) fail(`[${L}/${step}] the ${key} card carries no identity at all`);
  const want = { card: `setup/${key}`, program_id: fx.program,
                 season_id: fx.season, league_id: null };
  for (const k of Object.keys(want)) {
    if (id[k] !== want[k]) {
      fail(`[${L}/${step}] the card's committed identity.${k} is `
        + `${JSON.stringify(id[k])}, expected ${JSON.stringify(want[k])} — the `
        + `model is bound to a different tuple than the one on screen `
        + `(identity ${JSON.stringify(id)}, selected ${JSON.stringify(got.selected)})`);
    }
  }
  if (!(id.generation > 0)) {
    fail(`[${L}/${step}] the card's identity carries no generation: ${JSON.stringify(id)}`);
  }
  if (id.program_id !== got.selected.program_id
      || id.season_id !== got.selected.season_id
      || id.league_id !== got.selected.league_id) {
    fail(`[${L}/${step}] the card's identity ${JSON.stringify(id)} disagrees with `
      + `the live context tuple ${JSON.stringify(got.selected)}`);
  }
}

// The ordered asserted set, with the non-vacuity requirement built in: the
// floors named in `met` must really be MET, or a later assertion could be
// passing because of the WRONG blocker.
function assertOrderedRows(rows, L, step, key, wantOrder, met, unmet, reason) {
  if (!rows) {
    fail(`[${L}/${step}] ${key} published no prerequisites at all — the client `
      + `has nothing to consume fail-closed`);
  }
  const keys = rows.map((p) => p.key);
  if (JSON.stringify(keys) !== JSON.stringify(wantOrder)) {
    fail(`[${L}/${step}] ${key} published ${JSON.stringify(keys)}, expected the `
      + `COMPLETE ordered set ${JSON.stringify(wantOrder)} — a client can only `
      + `be as complete as the set it is handed`);
  }
  const byKey = {};
  rows.forEach((p) => { byKey[p.key] = p; });
  met.forEach((k) => {
    if (byKey[k].met !== true) {
      fail(`[${L}/${step}] ${key}/${k} asserts met=${JSON.stringify(byKey[k].met)}, `
        + `but this fixture requires it MET — otherwise the blocker asserted `
        + `below could be the wrong one and the leg proves nothing `
        + `(${JSON.stringify(rows)})`);
    }
  });
  if (byKey[unmet].met !== false) {
    fail(`[${L}/${step}] ${key}/${unmet} asserts met=${JSON.stringify(byKey[unmet].met)}, `
      + `expected false — this is the floor the fixture exists to create `
      + `(${JSON.stringify(rows)})`);
  }
  if (byKey[unmet].reason !== reason) {
    fail(`[${L}/${step}] ${key}/${unmet} names reason `
      + `${JSON.stringify(byKey[unmet].reason)}, expected "${reason}"`);
  }
  return byKey;
}

// The shared blocked-card contract: the model's own blocker sentence is on
// screen, the declared primary is gone, and the offered control is exactly
// what this role may execute (or nothing at all).
function assertBlockedCard(got, L, step, key, want) {
  if (got.state !== want.state) {
    fail(`[${L}/${step}] the ${key} card reads "${got.state}", expected `
      + `${want.state.toUpperCase()} — this fixture exists to assert a card with `
      + `REAL records that still cannot act (stats ${JSON.stringify(got.stats)})`);
  }
  if (got.status !== "todo") {
    fail(`[${L}/${step}] expected the backend to still call ${key} todo, got `
      + `"${got.status}"`);
  }
  const byLabel = {};
  got.stats.forEach((s) => { byLabel[s.label] = s.n; });
  for (const label of want.nonZero) {
    if (!(byLabel[label] > 0)) {
      fail(`[${L}/${step}] the card reports ${label}=${byLabel[label]} — a zero `
        + `count makes every assertion here vacuous, because an EMPTY card `
        + `withdraws the same controls for an entirely different reason `
        + `(stats ${JSON.stringify(got.stats)})`);
    }
  }
  if (!got.blockedBecause) {
    fail(`[${L}/${step}] the ${key} card settled with blockedBecause: null while `
      + `a hard prerequisite is unmet — THE fail-open this journey exists for `
      + `(stats ${JSON.stringify(got.stats)}, buttons ${JSON.stringify(got.buttons)})`);
  }
  if (!want.saysRe.test(got.blockedBecause)) {
    fail(`[${L}/${step}] the blocker sentence does not describe the real blocker `
      + `(${want.saysRe}): "${got.blockedBecause}"`);
  }
  if (got.blockedBecause.indexOf(want.seasonName) === -1) {
    fail(`[${L}/${step}] the blocker sentence does not name the SELECTED season `
      + `("${want.seasonName}"), so it is not a scoped claim: "${got.blockedBecause}"`);
  }
  if (!got.note || got.note.indexOf(got.blockedBecause) === -1) {
    fail(`[${L}/${step}] the card body does not carry the model's own blocker `
      + `sentence ("${got.blockedBecause}"); read "${got.note}"`);
  }
  if (got.buttons.some((b) => want.withdrawnRe.test(b))) {
    fail(`[${L}/${step}] the landing still offers the declared primary `
      + `(${want.withdrawnRe}): ${JSON.stringify(got.buttons)}`);
  }
  if ((got.goRoutes || []).indexOf(want.withdrawnGo) !== -1) {
    fail(`[${L}/${step}] the landing still wires a control to "${want.withdrawnGo}" `
      + `while a hard prerequisite is unmet: ${JSON.stringify(got.goRoutes)}`);
  }
  if (want.action === null) {
    if (got.effective !== null) {
      fail(`[${L}/${step}] a role that cannot resolve this blocker was handed the `
        + `effective action ${JSON.stringify(got.effective)} — a mutation control `
        + `they cannot execute is a second dead end`);
    }
    if (got.buttons.length !== 0) {
      fail(`[${L}/${step}] a blocked landing this role cannot resolve must offer NO `
        + `mutation control at all, got ${JSON.stringify(got.buttons)}`);
    }
    if (!/league admin/i.test(got.note)) {
      fail(`[${L}/${step}] with no action offered the copy must say a league admin `
        + `has to resolve it; read "${got.note}"`);
    }
  } else {
    if (got.effective !== want.action) {
      fail(`[${L}/${step}] the effective action is ${JSON.stringify(got.effective)}, `
        + `expected "${want.action}"`);
    }
    if (got.buttons.length !== 1 || got.buttons[0] !== want.action) {
      fail(`[${L}/${step}] while a prerequisite is missing the landing must expose `
        + `EXACTLY the one action that resolves it, got ${JSON.stringify(got.buttons)}`);
    }
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
  const errors = [];
  page.on("pageerror", (e) => errors.push(`[pageerror] ${e.message}`));
  page.on("console", (m) => { if (m.type() === "error") errors.push(`[console] ${m.text()}`); });
  let icePreviews = [];
  page.on("request", (r) => {
    if (r.url().indexOf(ICE_PREVIEW) !== -1) icePreviews.push(r.url());
  });
  const L = viewport.label;
  const noPreviewsSince = (step) => {
    if (icePreviews.length) {
      fail(`[${L}/${step}] ${icePreviews.length} ice-availability preview request(s) `
        + `were issued while the Facilities card was blocked: `
        + `${JSON.stringify(icePreviews)}`);
    }
  };

  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 15000 });
    await loginAs(page, "admin", "demo");
    const loadStatus = await page.evaluate(() => fetch("/api/demo/load", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" }, body: "{}",
    }).then((r) => r.status));
    if (loadStatus !== 200) fail(`[${L}] demo load failed (status ${loadStatus})`);

    // ============ (A) FACILITIES: ARCHIVED SEASON, LIVE GRANT ============
    // The reviewer's reproduction, built through public APIs exactly as
    // described: Program + active Season + Venue + Rink, grant the Venue to
    // that Season, ARCHIVE the selected Season, reopen Facilities.
    const a = await page.evaluate(async () => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      const org = await post("/api/v2/setup/organization", { name: "PF Org" });
      const program = await post("/api/v2/setup/program",
        { name: "PF Archived Program", country: "US",
          operator_organization_id: org.id });
      const season = await post("/api/v2/setup/season",
        { program_id: program.id, name: "PF Archived Season" });
      await post("/api/context", { program_id: program.id, season_id: season.id });
      const venue = await post("/api/v2/setup/venue",
        { name: "PF Venue", organization_id: org.id });
      const rink = await post("/api/v2/setup/rink",
        { venue_id: venue.id, name: "PF Rink" });
      const access = await post(`/api/v2/setup/seasons/${season.id}/venue-access`,
        { venue_id: venue.id });
      const archived = await post(`/api/v2/setup/seasons/${season.id}/archive`,
        { reason: "round 3 fixture" });
      return { program: program.id, season: season.id, seasonName: season.name,
               venue: venue.id, rink: rink.id, access: access.id,
               archived: !!archived && !archived.error };
    });
    for (const k of ["program", "season", "venue", "rink", "access"]) {
      if (!a[k]) fail(`[${L}] fixture (A) failed to create ${k}: ${JSON.stringify(a)}`);
    }
    if (!a.archived) fail(`[${L}] fixture (A) never archived the season: ${JSON.stringify(a)}`);

    // NON-VACUITY, at the source: the grant really is ACTIVE. Without this the
    // whole leg could be passing on round 2's venue-access floor instead.
    const aHistory = await apiGet(page, `/api/v2/setup/seasons/${a.season}/venue-access`);
    const live = (aHistory.venue_access || []).filter((r) => r.active);
    if (live.length !== 1 || live[0].venue_id !== a.venue) {
      fail(`[${L}] fixture (A) must hold exactly one ACTIVE grant for the venue, `
        + `got ${JSON.stringify(aHistory)}`);
    }

    await reenter(page, base);
    icePreviews = [];
    await openLanding(page, "facilities", `${L}/A/admin`);

    const aRows = await readAssertedRows(page, "facilities");
    assertOrderedRows(aRows.rows, L, "A/admin", "facilities",
      ["season_selected", "season_active", "venue_access"],
      ["season_selected", "venue_access"], "season_active", "season_archived");
    // The hole the per-workflow rows exist to close: for League Admin the
    // roll-up's single `next_blocked` slot is owned by an EARLIER workflow (or
    // by nothing), so it cannot carry this fact.
    if (aRows.nextBlocked === "facilities") {
      fail(`[${L}/A/admin] next_blocked names facilities, so this fixture is not `
        + `reproducing the reported hole (an earlier workflow must own that slot)`);
    }

    const aAdmin = await readLanding(page, "facilities");
    assertIdentity(aAdmin, L, "A/admin", "facilities", a);
    assertBlockedCard(aAdmin, L, "A/admin", "facilities", {
      state: "ready", nonZero: ["Venues", "Rinks"], seasonName: a.seasonName,
      saysRe: /archived/i, withdrawnRe: /add ice/i, withdrawnGo: "facilities",
      action: "Reopen this season" });
    noPreviewsSince("A/admin");

    // -- the same fixture, as the role that cannot resolve it ---------------
    // Reopening a Season is MANAGE_SETUP; an Arena Manager holds MANAGE_ARENA
    // only, so a reopen control here would be a 403 in waiting.
    await apiPost(page, "/api/auth/logout", {});
    await loginAs(page, "arena", "demo");
    await apiPost(page, "/api/context", { program_id: a.program, season_id: a.season });
    await reenter(page, base);
    icePreviews = [];
    await openLanding(page, "facilities", `${L}/A/arena`);
    const arenaRows = await readAssertedRows(page, "facilities");
    if (JSON.stringify(arenaRows.rows) !== JSON.stringify(aRows.rows)) {
      fail(`[${L}/A/arena] the asserted rows differ by role — they are statements `
        + `about the Season's data, not about the caller: `
        + `${JSON.stringify(arenaRows.rows)} vs ${JSON.stringify(aRows.rows)}`);
    }
    const aArena = await readLanding(page, "facilities");
    assertIdentity(aArena, L, "A/arena", "facilities", a);
    assertBlockedCard(aArena, L, "A/arena", "facilities", {
      state: "ready", nonZero: ["Venues", "Rinks"], seasonName: a.seasonName,
      saysRe: /archived/i, withdrawnRe: /add ice/i, withdrawnGo: "facilities",
      action: null });
    noPreviewsSince("A/arena");

    // ============ (B) RECOVERY THROUGH THE REAL REOPEN PATH ==============
    await apiPost(page, "/api/auth/logout", {});
    await loginAs(page, "admin", "demo");
    await apiPost(page, "/api/context", { program_id: a.program, season_id: a.season });
    await reenter(page, base);
    icePreviews = [];
    await openLanding(page, "facilities", `${L}/B/before`);
    assertBlockedCard(await readLanding(page, "facilities"), L, "B/before", "facilities", {
      state: "ready", nonZero: ["Venues", "Rinks"], seasonName: a.seasonName,
      saysRe: /archived/i, withdrawnRe: /add ice/i, withdrawnGo: "facilities",
      action: "Reopen this season" });

    // A sentinel that survives everything EXCEPT a document reload, and a
    // snapshot of every OTHER card, so "advanced on its own refresh, with no
    // reload and no adjacent-card mutation" is asserted rather than assumed.
    await page.evaluate(() => { window.__pfNoReload = "sentinel"; });
    const before = await page.evaluate(() => ({
      gens: Object.assign({}, cardGenerations),
      others: Object.keys(cardStates).filter((k) => k !== "setup/facilities")
        .reduce((acc, k) => { acc[k] = JSON.stringify(cardStates[k]); return acc; }, {}),
    }));

    // The reopen itself, through the card's own control -- not a raw fetch.
    // It is a card-scoped confirmation, because #159 requires a non-empty
    // reason that is recorded in the audit trail.
    await page.click('[data-setup-landing-actions="facilities"] .act.primary');
    await page.waitForSelector('[data-setup-card-confirm-reason="facilities"]',
      { timeout: 10000 }).catch(() => fail(`[${L}/B] the reopen control did not open `
        + `a confirmation with the reason field #159 requires`));
    // A blank reason must NOT fire the write: the route rejects it, and
    // inventing one on the operator's behalf would put a fabricated sentence
    // in the audit trail.
    const blankAttempts = [];
    page.on("request", function blankWatch(r) {
      if (r.url().indexOf("/reopen") !== -1) blankAttempts.push(r.url());
    });
    await page.click("[data-setup-card-confirm-yes]");
    await page.waitForTimeout(300);
    if (blankAttempts.length) {
      fail(`[${L}/B] a blank reason still fired the reopen write: `
        + `${JSON.stringify(blankAttempts)}`);
    }
    const stillConfirming = await page.evaluate(() =>
      readCardState("setup/facilities").state);
    if (stillConfirming !== "confirm") {
      fail(`[${L}/B] a blank reason left the card in "${stillConfirming}" — the `
        + `confirmation must stay open so the operator can supply one`);
    }

    await page.fill('[data-setup-card-confirm-reason="facilities"]',
                    "season restarted for the spring split");
    const reopenResp = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/seasons/${a.season}/reopen`
      && r.request().method() === "POST");
    await page.click("[data-setup-card-confirm-yes]");
    const reopenBody = await (await reopenResp).json();
    if (!reopenBody || reopenBody.error) {
      fail(`[${L}/B] the reopen through the card's own control failed: `
        + `${JSON.stringify(reopenBody)}`);
    }
    // The card's OWN refresh is what the reopen triggers, so wait for that
    // settle rather than re-navigating (a navigation would re-derive
    // everything and prove nothing about this card's own path).
    await page.waitForFunction(() =>
      readCardState("setup/facilities").state !== "loading", null, { timeout: 15000 });

    const after = await readLanding(page, "facilities");
    if (after.blockedBecause !== null) {
      fail(`[${L}/B] the Facilities card still reports "${after.blockedBecause}" after `
        + `the season was reopened through the real entry point`);
    }
    if (after.effective !== "Add Ice") {
      fail(`[${L}/B] the card did not advance to its declared primary; effective `
        + `action is ${JSON.stringify(after.effective)}`);
    }
    if ((after.goRoutes || []).indexOf("facilities") === -1) {
      fail(`[${L}/B] no control is wired to the Ice Availability Builder once the `
        + `season is active again: ${JSON.stringify(after.goRoutes)}`);
    }
    if (after.buttons.length < 2) {
      fail(`[${L}/B] an unblocked landing keeps its demoted actions, got `
        + `${JSON.stringify(after.buttons)}`);
    }
    const sentinel = await page.evaluate(() => window.__pfNoReload || null);
    if (sentinel !== "sentinel") {
      fail(`[${L}/B] the page reloaded during the recovery leg — the card is `
        + `required to advance without one`);
    }
    const afterGens = await page.evaluate(() => ({
      gens: Object.assign({}, cardGenerations),
      others: Object.keys(cardStates).filter((k) => k !== "setup/facilities")
        .reduce((acc, k) => { acc[k] = JSON.stringify(cardStates[k]); return acc; }, {}),
    }));
    if (!(afterGens.gens["setup/facilities"] > before.gens["setup/facilities"])) {
      fail(`[${L}/B] the reopen issued no new generation for this card `
        + `(${before.gens["setup/facilities"]} -> `
        + `${afterGens.gens["setup/facilities"]}), so it asserted nothing`);
    }
    for (const key of Object.keys(before.others)) {
      if (before.gens[key] !== afterGens.gens[key]) {
        fail(`[${L}/B] the reopen moved "${key}"'s generation (${before.gens[key]} `
          + `-> ${afterGens.gens[key]}) — the recovery must replace only its own `
          + `card's generation`);
      }
      if (before.others[key] !== afterGens.others[key]) {
        fail(`[${L}/B] the reopen mutated adjacent card "${key}"'s committed model`);
      }
    }

    // ============ (C) PARTICIPATION: ARCHIVED SEASON =====================
    // Otherwise valid League + Division + Team, so the eligibility floor
    // asserts met and the archived Season is the only blocker left.
    const c = await page.evaluate(async () => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      const program = await post("/api/v2/setup/program",
        { name: "PF Part Program", country: "US" });
      const season = await post("/api/v2/setup/season",
        { program_id: program.id, name: "PF Part Season" });
      const league = await post("/api/v2/setup/league",
        { season_id: season.id, name: "PF League" });
      await post("/api/context", { program_id: program.id, season_id: season.id });
      const club = await post("/api/v2/setup/club", { name: "PF Club" });
      const team = await post("/api/v2/setup/team",
        { club_id: club.id, league_id: league.id, name: "PF Team" });
      const division = await post("/api/v2/setup/division",
        { season_id: season.id, league_id: league.id, name: "PF Division" });
      const archived = await post(`/api/v2/setup/seasons/${season.id}/archive`,
        { reason: "round 3 fixture" });
      return { program: program.id, season: season.id, seasonName: season.name,
               league: league.id, team: team.id, division: division.id,
               archived: !!archived && !archived.error };
    });
    for (const k of ["program", "season", "league", "team", "division"]) {
      if (!c[k]) fail(`[${L}] fixture (C) failed to create ${k}: ${JSON.stringify(c)}`);
    }
    if (!c.archived) fail(`[${L}] fixture (C) never archived the season`);

    await reenter(page, base);
    await openLanding(page, "participation", `${L}/C`);
    const cRows = await readAssertedRows(page, "participation");
    assertOrderedRows(cRows.rows, L, "C", "participation",
      ["season_selected", "season_active", "team_league_eligible"],
      ["season_selected", "team_league_eligible"], "season_active",
      "season_archived");
    const cGot = await readLanding(page, "participation");
    assertIdentity(cGot, L, "C", "participation", c);
    assertBlockedCard(cGot, L, "C", "participation", {
      state: "ready", nonZero: ["Divisions"], seasonName: c.seasonName,
      saysRe: /archived/i, withdrawnRe: /register team/i,
      withdrawnGo: "participation", action: "Reopen this season" });

    // ============ (D) PARTICIPATION: TEAM/LEAGUE ELIGIBILITY =============
    // An ACTIVE Season whose visible Teams all fail rule 7. Non-vacuous: the
    // Divisions count is non-zero and the Teams really exist -- they are just
    // permanently bound to another League.
    const d = await page.evaluate(async () => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      const program = await post("/api/v2/setup/program",
        { name: "PF Rule7 Program", country: "US" });
      const seasonA = await post("/api/v2/setup/season",
        { program_id: program.id, name: "PF Rule7 A" });
      const leagueA = await post("/api/v2/setup/league",
        { season_id: seasonA.id, name: "PF Rule7 League A" });
      await post("/api/context", { program_id: program.id, season_id: seasonA.id });
      const club = await post("/api/v2/setup/club", { name: "PF Rule7 Club" });
      const team = await post("/api/v2/setup/team",
        { club_id: club.id, league_id: leagueA.id, name: "PF Rule7 Team A" });
      const seasonB = await post("/api/v2/setup/season",
        { program_id: program.id, name: "PF Rule7 B" });
      const leagueB = await post("/api/v2/setup/league",
        { season_id: seasonB.id, name: "PF Rule7 League B" });
      await post("/api/context", { program_id: program.id, season_id: seasonB.id });
      const division = await post("/api/v2/setup/division",
        { season_id: seasonB.id, league_id: leagueB.id, name: "PF Rule7 Division" });
      return { program: program.id, season: seasonB.id, seasonName: seasonB.name,
               club: club.id, leagueB: leagueB.id, team: team.id,
               division: division.id };
    });
    for (const k of ["program", "season", "leagueB", "team", "division"]) {
      if (!d[k]) fail(`[${L}] fixture (D) failed to create ${k}: ${JSON.stringify(d)}`);
    }
    // Non-vacuity at the source: the Team really is visible to this caller,
    // and really is bound to a League this Season does not run.
    const dTeams = await apiGet(page, "/api/v2/setup/overview");
    if (!(dTeams.teams || []).some((t) => t.id === d.team)) {
      fail(`[${L}] fixture (D)'s Team is not visible in the scoped overview, so `
        + `"the visible Teams all fail the floor" is not what is being tested: `
        + `${JSON.stringify((dTeams.teams || []).map((t) => t.id))}`);
    }

    await reenter(page, base);
    await openLanding(page, "participation", `${L}/D`);
    const dRows = await readAssertedRows(page, "participation");
    assertOrderedRows(dRows.rows, L, "D", "participation",
      ["season_selected", "season_active", "team_league_eligible"],
      ["season_selected", "season_active"], "team_league_eligible",
      "team_league_mismatch");
    const dGot = await readLanding(page, "participation");
    assertIdentity(dGot, L, "D", "participation", d);
    assertBlockedCard(dGot, L, "D", "participation", {
      state: "ready", nonZero: ["Divisions"], seasonName: d.seasonName,
      saysRe: /eligible|league/i, withdrawnRe: /register team/i,
      withdrawnGo: "participation",
      action: "Set up a team in this season's league" });

    // ...and it stays withdrawn until the REAL blocker is repaired. Adding a
    // Team under a league this Season actually runs is the repair; nothing
    // else about the card changes.
    const repaired = await apiPost(page, "/api/v2/setup/team",
      { club_id: d.club, league_id: d.leagueB, name: "PF Rule7 Team B" });
    if (!repaired || repaired.error) {
      fail(`[${L}/D] could not repair the real blocker: ${JSON.stringify(repaired)}`);
    }
    await page.evaluate(() => retrySetupWorkflowCard("participation"));
    await page.waitForFunction(() =>
      readCardState("setup/participation").state !== "loading", null, { timeout: 15000 });
    const dAfter = await readLanding(page, "participation");
    if (dAfter.blockedBecause !== null) {
      fail(`[${L}/D] the participation card still reports "${dAfter.blockedBecause}" `
        + `after an eligible team was created`);
    }
    if (dAfter.effective !== "Register Team") {
      fail(`[${L}/D] the card did not advance to "Register Team"; effective action `
        + `is ${JSON.stringify(dAfter.effective)}`);
    }

    if (errors.length) fail(`[${L}] browser errors:\n${errors.join("\n")}`);
    console.log(`[${L}] OK — an ARCHIVED selected Season with a LIVE venue-access `
      + `grant and non-zero visible Venues/Rinks publishes the COMPLETE ordered `
      + `floor set (season_selected, season_active, venue_access) with `
      + `venue_access MET, and the Facilities card settles READY+todo under its `
      + `exact card/program/season/league identity with no "Add Ice" control, no `
      + `route to the Ice Availability Builder and no ice-preview request at all `
      + `— while next_blocked names an earlier workflow and could not have `
      + `carried the fact. League Admin is offered exactly one control, the real `
      + `reopen path (which refuses a blank reason rather than inventing one); `
      + `Arena Manager, who cannot reopen a Season, gets no mutation control and `
      + `explicit guidance. Reopening through that control advances the SAME card `
      + `to "Add Ice" on its own refresh, with no page reload and no adjacent `
      + `card's generation or committed model touched. Participation asserts its `
      + `own complete set for both an archived Season and a rule-7 eligibility `
      + `failure, keeping "Register Team" withdrawn until the real blocker is `
      + `repaired.`);
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
  const browser = await chromium.launch();
  try {
    for (const viewport of VIEWPORTS) await checkViewport(browser, viewport);
    console.log("Setup workflow prerequisite-floor browser journey passed.");
  } catch (e) {
    console.error("Setup workflow prerequisite-floor browser journey FAILED.");
    console.error(e.message);
    process.exitCode = 1;
  } finally {
    await browser.close();
  }
})();
