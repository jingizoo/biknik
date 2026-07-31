// Facilities venue-access gate (#365 review) — the Facilities card must treat
// "a Rink is VISIBLE" and "a Rink is SCHEDULABLE this Season" as different
// claims.
//
// THE DEFECT THIS EXISTS FOR, verbatim from the review: "Facilities treats a
// visible historical/ungranted Rink as schedulable and offers a dead-end Add
// Ice primary." The card's prerequisite chain asked only whether any Venue and
// Rink appeared in the scoped setup overview. That overview's contract
// DELIBERATELY includes revoked-grant history (so the cleanup section can name
// the row) and creator-owned pending rows (so create-then-link works) — both
// correct reads. So a Venue+Rink whose grant to the selected Season had been
// revoked settled READY with `blockedBecause: null` and a sole "Add Ice"
// primary, while `get_setup_progress` independently computed ZERO schedulable
// rinks and the Ice Availability Builder would refuse every one of them with
// `venue_access_missing` and generate zero slots.
//
// The fix makes "at least one Rink reachable through ACTIVE SeasonVenueAccess
// for this exact selected Season" an asserted per-workflow prerequisite on
// /api/v2/setup/progress, bound into the Facilities card model under the same
// identity (card id + context tuple + generation) as everything else. This
// journey is the regression for that, at desktop and canonical 390x844.
//
// WHAT IT ASSERTS, and why each leg is a real risk rather than markup presence:
//
//  (A) REVOKED GRANT — Venue + Rink, granted to the selected Season and then
//      revoked. The reviewer's own reproduction.
//  (B) CREATOR-OWNED PENDING — Venue + Rink that never had a grant at all,
//      reaching the payload through `pending_link_*`. A different code path
//      into the same visibility, so it needs its own fixture.
//
//  In BOTH, for BOTH roles:
//    * EXACT TUPLE IDENTITY — the committed model's identity is this card,
//      this Program, this Season, this League. Without it the assertions below
//      could be satisfied by a model bound to a neighbouring tuple's inventory,
//      which is precisely what the per-card identity discipline exists to stop.
//    * NON-VACUOUS VISIBLE COUNTS/HISTORY — the card's own Venues and Rinks
//      counts are NON-ZERO and the rows are really in the scoped overview
//      (and, for (A), the revoked grant is really in the Season's venue-access
//      history). This is what keeps the whole journey from passing against a
//      card that is merely EMPTY: an empty Facilities card also offers no "Add
//      Ice", and would prove nothing.
//    * NO "Add Ice" CONTROL AND NO ICE-PREVIEW REQUEST — not only that the
//      button is absent, but that the page issues no
//      /api/setup/ice-availability/preview at all. A withdrawn control that
//      still let some other path reach the builder would be the same dead end
//      one click further along.
//    * ROLE-CORRECT GUIDANCE/ACTION — League Admin (holds MANAGE_SETUP) gets
//      the REAL venue-access resolution path: exactly one control, which lands
//      on the selected Season's own "Allowed venues" picker with focus on it.
//      Arena Manager (MANAGE_ARENA, and explicitly NOT able to grant Season
//      venue access) gets NO mutation control at all plus explicit guidance
//      that a League Admin must set it up. Offering an Arena Manager a grant
//      action would just be a second dead end.
//
//  (C) RECOVERY — the grant is then made through that real entry point (the
//      Allow picker, not a raw fetch), and the SAME card advances to "Add Ice"
//      with its demoted actions restored, with NO page reload; then the card's
//      OWN refresh path is exercised and must leave every adjacent card's
//      generation and committed model untouched. Without this leg the
//      withdrawal above could pass by withdrawing "Add Ice" forever.
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
  { label: "desktop", width: 1440, height: 900, port: 8391 },
  { label: "phone", width: 390, height: 844, port: 8392 },
];

// The one route that proves a dead end was actually reachable. "Add Ice" opens
// the Ice Availability Builder, whose preview POST is the first thing it does
// with a rink selection.
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

// Open the Facilities LANDING through a real, permission-gated entry point.
// Deliberately the nav destination rather than the hub toggle: it is the same
// openSetupWorkflowLanding() transition for both roles, and it is the Arena
// Manager's own primary journey into this workflow.
async function openFacilitiesLanding(page, step) {
  await page.click('[data-setup-workflow-nav="facilities"]');
  await page.waitForSelector('[data-setup-workflow-landing="facilities"]',
    { timeout: 15000 }).catch(() => {
      throw new Error(`[${step}] the Facilities landing never rendered`);
    });
  // Actions and copy mean nothing until the card has SETTLED: LOADING
  // withdraws every action group by design, so sampling mid-flight would read
  // zero controls and blame the wrong thing.
  await page.waitForFunction(() =>
    readCardState("setup/facilities").state !== "loading", null, { timeout: 15000 });
}

// Everything the assertions below need, read from the card's own committed
// MODEL plus the DOM it produced — never re-derived in the test.
async function readFacilities(page) {
  return page.evaluate(() => {
    const root = document.querySelector('[data-setup-workflow-landing="facilities"]');
    const entry = readCardState("setup/facilities");
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
      // `undefined` (never derived) is a DIFFERENT answer from `null` (derived,
      // and this role can resolve nothing) -- the Arena Manager leg turns on
      // exactly that distinction, so it must not be flattened here.
      effective: entry.effective === undefined ? "undefined"
        : entry.effective === null ? null : entry.effective.label,
      // EVERY control the landing offers, not just the primaries.
      buttons: box ? Array.from(box.querySelectorAll("button"))
        .map((b) => b.textContent.trim()) : [],
      // The Add-Ice route by its wiring, not merely by its label: the model
      // could substitute a differently-labelled control that still opened the
      // builder.
      addIceRoutes: root ? root.querySelectorAll('[data-setup-workflow-go="facilities"]').length : -1,
      note: note ? note.textContent.replace(/\s+/g, " ").trim() : null,
    };
  });
}

function fail(msg) { throw new Error(msg); }

// The shared blocked-state contract, asserted identically for both fixtures and
// both roles. `want` carries only what genuinely differs by role.
async function assertBlocked(page, L, step, fx, want) {
  const got = await readFacilities(page);

  // -- exact tuple identity ------------------------------------------------
  const id = got.identity;
  if (!id) fail(`[${L}/${step}] the Facilities card carries no identity at all`);
  const wantTuple = { card: "setup/facilities", program_id: fx.program,
                      season_id: fx.season, league_id: null };
  for (const k of Object.keys(wantTuple)) {
    if (id[k] !== wantTuple[k]) {
      fail(`[${L}/${step}] the card's committed identity.${k} is `
        + `${JSON.stringify(id[k])}, expected ${JSON.stringify(wantTuple[k])} `
        + `— the model is bound to a different tuple than the one on screen `
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

  // -- non-vacuous: the rows really ARE visible ----------------------------
  // Without this the whole journey would pass against an EMPTY card, which
  // also offers no "Add Ice" and would prove nothing about schedulability.
  if (got.state !== "ready") {
    fail(`[${L}/${step}] the Facilities card reads "${got.state}", expected READY — `
      + `this fixture exists to assert a card with REAL records that is still `
      + `not schedulable (stats ${JSON.stringify(got.stats)})`);
  }
  if (got.status !== "todo") {
    fail(`[${L}/${step}] expected the backend to still call facilities todo, got `
      + `"${got.status}"`);
  }
  const byLabel = {};
  got.stats.forEach((s) => { byLabel[s.label] = s.n; });
  for (const label of ["Venues", "Rinks"]) {
    if (!(byLabel[label] > 0)) {
      fail(`[${L}/${step}] the card reports ${label}=${byLabel[label]} — the fixture `
        + `is supposed to make a Venue AND a Rink VISIBLE while leaving them `
        + `unschedulable; a zero count makes every assertion below vacuous `
        + `(stats ${JSON.stringify(got.stats)})`);
    }
  }
  const overview = await apiGet(page, "/api/v2/setup/overview");
  const seenVenue = [].concat(overview.venues || [], overview.pending_link_venues || [])
    .some((v) => v.id === want.venue);
  const seenRink = [].concat(overview.rinks || [], overview.pending_link_rinks || [])
    .some((r) => r.id === want.rink);
  if (!seenVenue || !seenRink) {
    fail(`[${L}/${step}] the fixture's Venue/Rink are not in the scoped overview `
      + `(venue ${seenVenue}, rink ${seenRink}) — the counts above came from `
      + `somewhere else, so this is not the visible-but-unschedulable state`);
  }

  // -- the blocker itself --------------------------------------------------
  if (!got.blockedBecause) {
    fail(`[${L}/${step}] the card settled with blockedBecause: null while no rink is `
      + `reachable through active venue access — THE fail-open this journey `
      + `exists for (stats ${JSON.stringify(got.stats)}, `
      + `buttons ${JSON.stringify(got.buttons)})`);
  }
  if (!/venue access/i.test(got.blockedBecause)) {
    fail(`[${L}/${step}] the blocker sentence does not name venue access: `
      + `"${got.blockedBecause}"`);
  }
  if (got.blockedBecause.indexOf(fx.seasonName) === -1) {
    fail(`[${L}/${step}] the blocker sentence does not name the SELECTED season `
      + `("${fx.seasonName}"), so it is not a scoped claim: "${got.blockedBecause}"`);
  }
  if (!got.note || got.note.indexOf(got.blockedBecause) === -1) {
    fail(`[${L}/${step}] the card body does not carry the model's own blocker `
      + `sentence ("${got.blockedBecause}"); read "${got.note}"`);
  }

  // -- no Add Ice control --------------------------------------------------
  if (got.addIceRoutes !== 0) {
    fail(`[${L}/${step}] the landing still wires ${got.addIceRoutes} control(s) to the `
      + `Ice Availability Builder while no rink is schedulable`);
  }
  if (got.buttons.some((b) => /add ice/i.test(b))) {
    fail(`[${L}/${step}] the landing still offers an "Add Ice" control: `
      + `${JSON.stringify(got.buttons)}`);
  }

  // -- role-correct guidance / action --------------------------------------
  if (want.action === null) {
    if (got.effective !== null) {
      fail(`[${L}/${step}] an Arena Manager, who cannot grant season venue access, `
        + `was handed the effective action ${JSON.stringify(got.effective)} — a `
        + `mutation control they cannot execute is a second dead end`);
    }
    if (got.buttons.length !== 0) {
      fail(`[${L}/${step}] an Arena Manager's blocked landing must offer NO mutation `
        + `control at all, got ${JSON.stringify(got.buttons)}`);
    }
    if (!/league admin/i.test(got.note)) {
      fail(`[${L}/${step}] an Arena Manager gets no action, so the copy must say a `
        + `League Admin has to grant access; read "${got.note}"`);
    }
  } else {
    if (got.effective !== want.action) {
      fail(`[${L}/${step}] the effective action is ${JSON.stringify(got.effective)}, `
        + `expected "${want.action}" — the real venue-access resolution path`);
    }
    if (got.buttons.length !== 1 || got.buttons[0] !== want.action) {
      fail(`[${L}/${step}] while a prerequisite is missing the landing must expose `
        + `EXACTLY the one action that resolves it, got ${JSON.stringify(got.buttons)}`);
    }
  }
  return got;
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
  // Recorded from the first navigation onward: "no Add Ice control" is a claim
  // about the button, "no ice-preview request" is the stronger claim that no
  // path reached the builder at all.
  let icePreviews = [];
  page.on("request", (r) => {
    if (r.url().indexOf(ICE_PREVIEW) !== -1) icePreviews.push(r.url());
  });
  const L = viewport.label;
  const noPreviewsSince = (step) => {
    if (icePreviews.length) {
      fail(`[${L}/${step}] ${icePreviews.length} ice-availability preview request(s) `
        + `were issued while the Facilities card was blocked on venue access: `
        + `${JSON.stringify(icePreviews)}`);
    }
  };

  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 15000 });
    await loginAs(page, "admin", "demo");
    // A fresh boot seeds only "admin"; the other demo personas ("arena" here)
    // are UserAccount rows /api/demo/load creates. Every fixture below selects
    // its own Program explicitly, so the seeded demo data is never in scope.
    const loadStatus = await page.evaluate(() => fetch("/api/demo/load", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" }, body: "{}",
    }).then((r) => r.status));
    if (loadStatus !== 200) fail(`[${L}] demo load failed (status ${loadStatus})`);

    // ================= (A) REVOKED GRANT ==================================
    // The reviewer's reproduction, built through public APIs exactly as they
    // described it: Program + active Season + Venue + Rink, grant the Venue,
    // then revoke that grant. The overview keeps reporting 1 Venue / 1 Rink as
    // history, which is correct — and none of it is schedulable.
    const a = await page.evaluate(async () => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      const org = await post("/api/v2/setup/organization", { name: "VAG Revoked Org" });
      const program = await post("/api/v2/setup/program",
        { name: "VAG Revoked Program", country: "US",
          operator_organization_id: org.id });
      const season = await post("/api/v2/setup/season",
        { program_id: program.id, name: "VAG Revoked Season" });
      await post("/api/context", { program_id: program.id, season_id: season.id });
      const venue = await post("/api/v2/setup/venue",
        { name: "VAG Revoked Venue", organization_id: org.id });
      const rink = await post("/api/v2/setup/rink",
        { venue_id: venue.id, name: "VAG Revoked Rink" });
      const access = await post(`/api/v2/setup/seasons/${season.id}/venue-access`,
        { venue_id: venue.id });
      const revoked = await post(
        `/api/v2/setup/season-venue-access/${access.id}/remove`, {});
      return { program: program.id, season: season.id, seasonName: season.name,
               venue: venue.id, rink: rink.id, access: access.id,
               revoked: !!revoked && !revoked.error };
    });
    for (const k of ["program", "season", "venue", "rink", "access"]) {
      if (!a[k]) fail(`[${L}] fixture (A) failed to create ${k}: ${JSON.stringify(a)}`);
    }
    if (!a.revoked) fail(`[${L}] fixture (A) never revoked the grant: ${JSON.stringify(a)}`);

    // The HISTORY half of "non-vacuous": the revoked row really is preserved
    // and really does name this Venue, which is why the overview keeps showing
    // it. If this row were gone the fixture would be an ordinary ungranted one.
    const aHistory = await apiGet(page, `/api/v2/setup/seasons/${a.season}/venue-access`);
    const revokedRow = (aHistory.venue_access || []).find((r) => r.id === a.access);
    if (!revokedRow || revokedRow.active !== false || revokedRow.venue_id !== a.venue) {
      fail(`[${L}] fixture (A) expected a preserved, INACTIVE grant row for the `
        + `venue; got ${JSON.stringify(aHistory)}`);
    }
    if ((aHistory.venue_access || []).some((r) => r.active)) {
      fail(`[${L}] fixture (A) still holds an ACTIVE grant, so nothing is blocked: `
        + `${JSON.stringify(aHistory)}`);
    }

    await reenter(page, base);
    icePreviews = [];
    await openFacilitiesLanding(page, `${L}/A/admin`);
    await assertBlocked(page, L, "A/admin", a,
      { venue: a.venue, rink: a.rink, action: "Allow a venue for this season" });
    noPreviewsSince("A/admin");

    // -- the same fixture, as the role that cannot resolve it ---------------
    await apiPost(page, "/api/auth/logout", {});
    await loginAs(page, "arena", "demo");
    await apiPost(page, "/api/context", { program_id: a.program, season_id: a.season });
    await reenter(page, base);
    icePreviews = [];
    await openFacilitiesLanding(page, `${L}/A/arena`);
    await assertBlocked(page, L, "A/arena", a,
      { venue: a.venue, rink: a.rink, action: null });
    noPreviewsSince("A/arena");

    // ================= (C) RECOVERY, through the real entry point =========
    await apiPost(page, "/api/auth/logout", {});
    await loginAs(page, "admin", "demo");
    await apiPost(page, "/api/context", { program_id: a.program, season_id: a.season });
    await reenter(page, base);
    icePreviews = [];
    await openFacilitiesLanding(page, `${L}/C/before`);
    await assertBlocked(page, L, "C/before", a,
      { venue: a.venue, rink: a.rink, action: "Allow a venue for this season" });

    // A sentinel that survives everything EXCEPT a document reload, so "the
    // card advanced without a page reload" is asserted rather than assumed.
    await page.evaluate(() => { window.__vagNoReload = "sentinel"; });

    // The single offered control must reach the REAL venue-access surface and
    // land focus on the control that actually performs the grant -- not merely
    // dump the operator at the top of the hierarchy tree to hunt for it.
    await page.click('[data-setup-landing-actions="facilities"] .act.primary');
    await page.waitForSelector(`#va-add-${a.season}`, { timeout: 15000 })
      .catch(() => fail(`[${L}/C] "Allow a venue for this season" did not reach the `
        + `selected Season's Allowed-venues picker`));
    // Focus lands through the same poll-while-rendering helper the
    // participation deep-link uses (the destination view is fetched
    // asynchronously, so the control does not exist at click time) -- so this
    // WAITS for it rather than sampling the instant the element appears, which
    // would race the very next poll tick.
    await page.waitForFunction((sid) => document.activeElement
      && document.activeElement.id === `va-add-${sid}`, a.season, { timeout: 10000 })
      .catch(async () => {
        const el = await page.evaluate(() => ({
          id: document.activeElement && document.activeElement.id,
          tag: document.activeElement && document.activeElement.tagName }));
        fail(`[${L}/C] the resolution path did not focus the Allow picker; focus is `
          + `on ${JSON.stringify(el)}`);
      });

    // The grant itself, through that picker -- not a raw fetch.
    await page.selectOption(`#va-add-${a.season}`, a.venue);
    const grantResp = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/seasons/${a.season}/venue-access`
      && r.request().method() === "POST");
    await page.click(`[data-va-add="${a.season}"]`);
    const grantBody = await (await grantResp).json();
    if (!grantBody || grantBody.error) {
      fail(`[${L}/C] the grant through the real Allow control failed: `
        + `${JSON.stringify(grantBody)}`);
    }
    // Let the grant's OWN re-render land before navigating: it repaints the
    // tree with the new active grant (its Revoke control is the proof), and
    // starting a second navigation on top of an in-flight render would only be
    // racing this test's own steps.
    await page.waitForSelector(`[data-va-revoke="${grantBody.id}"]`, { timeout: 15000 })
      .catch(() => fail(`[${L}/C] the granted venue never appeared as an active `
        + `allowed venue on the Season's own list`));

    // Back to the SAME card. No reload anywhere in this leg.
    await openFacilitiesLanding(page, `${L}/C/after`);
    const after = await readFacilities(page);
    if (after.blockedBecause !== null) {
      fail(`[${L}/C] the Facilities card still reports "${after.blockedBecause}" after `
        + `access was granted through the real entry point`);
    }
    if (after.effective !== "Add Ice") {
      fail(`[${L}/C] the card did not advance to its declared primary; effective `
        + `action is ${JSON.stringify(after.effective)}`);
    }
    if (after.addIceRoutes !== 1) {
      fail(`[${L}/C] expected exactly one control wired to the Ice Availability `
        + `Builder once access exists, got ${after.addIceRoutes}`);
    }
    if (after.buttons.length < 2) {
      fail(`[${L}/C] an unblocked landing keeps its demoted actions, got `
        + `${JSON.stringify(after.buttons)}`);
    }
    const sentinel = await page.evaluate(() => window.__vagNoReload || null);
    if (sentinel !== "sentinel") {
      fail(`[${L}/C] the page reloaded during the resolution leg — the card is `
        + `required to advance without one`);
    }

    // ...and the card's OWN refresh path is card-scoped: it must not disturb a
    // neighbour's generation or committed model.
    const beforeRefresh = await page.evaluate(() => ({
      gens: Object.assign({}, cardGenerations),
      others: Object.keys(cardStates).filter((k) => k !== "setup/facilities")
        .reduce((acc, k) => { acc[k] = JSON.stringify(cardStates[k]); return acc; }, {}),
    }));
    await page.evaluate(() => retrySetupWorkflowCard("facilities"));
    await page.waitForFunction(() =>
      readCardState("setup/facilities").state !== "loading", null, { timeout: 15000 });
    const afterRefresh = await page.evaluate(() => ({
      gens: Object.assign({}, cardGenerations),
      others: Object.keys(cardStates).filter((k) => k !== "setup/facilities")
        .reduce((acc, k) => { acc[k] = JSON.stringify(cardStates[k]); return acc; }, {}),
      effective: (readCardState("setup/facilities").effective || {}).label || null,
      blockedBecause: readCardState("setup/facilities").blockedBecause || null,
    }));
    if (!(afterRefresh.gens["setup/facilities"] > beforeRefresh.gens["setup/facilities"])) {
      fail(`[${L}/C] the card's own refresh issued no new generation `
        + `(${beforeRefresh.gens["setup/facilities"]} -> `
        + `${afterRefresh.gens["setup/facilities"]}), so it asserted nothing`);
    }
    if (afterRefresh.effective !== "Add Ice" || afterRefresh.blockedBecause !== null) {
      fail(`[${L}/C] after its own refresh the card reads effective=`
        + `${JSON.stringify(afterRefresh.effective)} / blockedBecause=`
        + `${JSON.stringify(afterRefresh.blockedBecause)}`);
    }
    for (const key of Object.keys(beforeRefresh.others)) {
      if (beforeRefresh.gens[key] !== afterRefresh.gens[key]) {
        fail(`[${L}/C] the Facilities card's own refresh moved "${key}"'s generation `
          + `(${beforeRefresh.gens[key]} -> ${afterRefresh.gens[key]}) — a per-card `
          + `refresh must replace only its own card's generation`);
      }
      if (beforeRefresh.others[key] !== afterRefresh.others[key]) {
        fail(`[${L}/C] the Facilities card's own refresh mutated adjacent card `
          + `"${key}"'s committed model`);
      }
    }

    // ================= (B) CREATOR-OWNED PENDING VENUE + RINK =============
    // A second Program, so fixture (A)'s now-granted Venue is out of scope. The
    // Venue and Rink here reach the payload through `pending_link_*` -- rows
    // with no link to ANY Program that THIS caller created -- so each role must
    // create its own pair: a pending row is deliberately invisible to everyone
    // but its creator, and a leg run against rows the signed-in role cannot see
    // would assert against an EMPTY card instead of a blocked one.
    const b = await page.evaluate(async () => {
      const post = async (p, bd) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(bd),
      })).json();
      const program = await post("/api/v2/setup/program",
        { name: "VAG Pending Program", country: "US" });
      const season = await post("/api/v2/setup/season",
        { program_id: program.id, name: "VAG Pending Season" });
      await post("/api/context", { program_id: program.id, season_id: season.id });
      const venue = await post("/api/v2/setup/venue", { name: "VAG Pending Venue Admin" });
      const rink = await post("/api/v2/setup/rink",
        { venue_id: venue.id, name: "VAG Pending Rink Admin" });
      return { program: program.id, season: season.id, seasonName: season.name,
               venue: venue.id, rink: rink.id };
    });
    for (const k of ["program", "season", "venue", "rink"]) {
      if (!b[k]) fail(`[${L}] fixture (B) failed to create ${k}: ${JSON.stringify(b)}`);
    }
    // The other half of "non-vacuous" for this fixture: there is genuinely NO
    // grant at all, so the rows are visible purely as creator-owned drafts.
    const bHistory = await apiGet(page, `/api/v2/setup/seasons/${b.season}/venue-access`);
    if ((bHistory.venue_access || []).length !== 0) {
      fail(`[${L}] fixture (B) is supposed to have no grant history at all, got `
        + `${JSON.stringify(bHistory)}`);
    }

    await reenter(page, base);
    icePreviews = [];
    await openFacilitiesLanding(page, `${L}/B/admin`);
    await assertBlocked(page, L, "B/admin", b,
      { venue: b.venue, rink: b.rink, action: "Allow a venue for this season" });
    noPreviewsSince("B/admin");

    await apiPost(page, "/api/auth/logout", {});
    await loginAs(page, "arena", "demo");
    await apiPost(page, "/api/context", { program_id: b.program, season_id: b.season });
    const bArena = await page.evaluate(async () => {
      const post = async (p, bd) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(bd),
      })).json();
      const venue = await post("/api/v2/setup/venue", { name: "VAG Pending Venue Arena" });
      const rink = await post("/api/v2/setup/rink",
        { venue_id: venue.id, name: "VAG Pending Rink Arena" });
      return { venue: venue.id, rink: rink.id };
    });
    if (!bArena.venue || !bArena.rink) {
      fail(`[${L}] fixture (B) could not create the Arena Manager's own pending `
        + `Venue/Rink: ${JSON.stringify(bArena)}`);
    }
    await reenter(page, base);
    icePreviews = [];
    await openFacilitiesLanding(page, `${L}/B/arena`);
    await assertBlocked(page, L, "B/arena", b,
      { venue: bArena.venue, rink: bArena.rink, action: null });
    noPreviewsSince("B/arena");

    if (errors.length) fail(`[${L}] browser errors:\n${errors.join("\n")}`);
    console.log(`[${L}] OK — a Venue+Rink whose grant to the selected Season was `
      + `revoked, and a creator-owned pending Venue+Rink with no grant at all, are `
      + `both VISIBLE (non-zero Venues/Rinks counts, backed by the scoped overview, `
      + `and by a preserved inactive grant row / a genuinely empty grant list) and `
      + `neither is schedulable: the Facilities card settles READY+todo under its `
      + `exact card/program/season/league identity with a blocker sentence naming `
      + `venue access AND the selected season, offers no "Add Ice" control and no `
      + `route to the Ice Availability Builder, and issues no ice-preview request `
      + `at all. League Admin is offered exactly one control -- the real `
      + `venue-access resolution path, which lands focus on the selected Season's `
      + `own Allow picker -- while Arena Manager, who cannot grant that access, is `
      + `offered no mutation control at all and told a League Admin must set it up. `
      + `Granting through that real picker advances the SAME card to "Add Ice" with `
      + `its demoted actions restored, with no page reload, and the card's own `
      + `refresh moves only its own generation and leaves every adjacent card's `
      + `committed model untouched.`);
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
    console.log("Facilities venue-access gate browser journey passed.");
  } catch (e) {
    console.error("Facilities venue-access gate browser journey FAILED.");
    console.error(e.message);
    process.exitCode = 1;
  } finally {
    await browser.close();
  }
})();
