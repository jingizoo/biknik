// THE CONTEXT-SWITCH READ SETTLEMENT BARRIER (#409, CI browser-smoke shard 1).
//
// THE DEFECT, exactly as CI recorded it -- at 390px only, with desktop green
// and a local run green, which is why a local pass was never evidence:
//
//     GET /api/v2/setup/seasons/season_3/venue-candidates -> 404
//
// THE SERVER IS CORRECT AND IS NOT TOUCHED BY THIS JOURNEY. Under #369's owner
// ruling `/venue-access` and `/venue-candidates` answer only for the EXACTLY
// persisted selected Season: a SIBLING Season of the very same Program is
// refused with the identical generic not-found as a foreign or nonexistent
// one. Leg (S) below is a DIRECT SERVER CONTROL that keeps that ceiling
// asserted here, so a client-side fix can never be mistaken for -- or quietly
// turn into -- a loosening of it.
//
// The defect is client-side. render()'s Setup pass dispatches those two reads
// for the Season selected AT DISPATCH TIME. setActiveContext() then invalidates
// generations (contextRevision, contextSwitchSeq, the Ice/Import op tokens) and
// POSTs the new context -- but it did not CANCEL reads already on the wire. If
// the POST commits first, an in-flight read for the departing Season reaches a
// server whose persisted selection has moved, and the ceiling refuses it. The
// `contextRevision !== myRenderContext` checks after each await are
// DISCARD-AFTER-ARRIVAL: they stop the stale body from being BELIEVED, which is
// a strictly later property than stopping the request from FAILING. By the time
// one runs, the 404 has been served and Chromium has already written its own
// "Failed to load resource" console error -- the same line an operator sees in
// devtools.
//
// WHY AN AbortController ALONE IS NOT THE FIX (owner constraint, 2026-08-11).
// Aborting does not UN-SEND a request the server already holds. Abort and POST
// in the same turn and the server can still be part-way through the old read
// when the new context lands: it processes it, it 404s, and the only thing that
// changed is that nobody was listening. So the switch takes a real SETTLEMENT
// BARRIER --
//
//     CANCEL the outstanding scoped reads
//       -> AWAIT THEIR FULL SETTLEMENT (abort delivered, or body fully drained)
//       -> only THEN POST the new context
//
// -- and THIS journey is written to be sensitive to the ORDER, not merely to
// the presence of an abort. Leg (B) reads, synchronously at the instant the
// switch POST is handed to the network, how many scoped reads the app still has
// outstanding. A build that aborts without waiting still shows a non-zero count
// there and fails, even though its abort ledger looks identical. That is not a
// hypothetical: the abort-without-await mutation was run against this file and
// failed leg (B) with `inFlight=1 abortsRecorded=0` -- the abort had been
// requested and had not yet been delivered, which is precisely the window.
//
// WHAT THIS JOURNEY DOES NOT PROVE, said plainly. It cannot show that the
// SERVER has finished with a read it already received; HTTP offers the client
// no such signal, and no browser test can invent one. It proves the whole of
// what CI measured and what an operator sees -- that no old-tuple scoped read
// ARRIVES as a 404, that none can paint, and that every cancellation is
// accounted for as intentional. The remaining server-side window is
// unobservable (the connection is gone before any answer could be written) and
// closing it would take a server change, which #369's ruling forbids.
//
// HOW THE OLD READ IS MADE TO LOSE THE RACE. The held request is intercepted
// BEFORE it is forwarded, so the SERVER HAS NOT SEEN IT YET; it is released only
// after the switch has been persisted. That is precisely the CI ordering (the
// read reaches the server after the context POST committed), and on an
// unbarriered build it produces the recorded 404 deterministically instead of
// once in N runs at one viewport. A hold that captured the real response FIRST
// (route.fetch(), the technique the sibling journeys use for DELIVERY delays)
// would prove nothing here: the server would have answered it under the OLD
// selection and could never have refused it.
//
// WHAT THE JOURNEY ASSERTS, per viewport and for BOTH held reads:
//
//  (A) THE HOLD IS REAL AND POST-DISPATCH. The read is observed leaving the
//      page (the app's own in-flight registry names it) before the switch is
//      made, so this is a race against a request genuinely on the wire and not
//      a request that was never issued.
//  (B) THE BARRIER ORDERED THE SWITCH. At the instant POST /api/context is
//      handed to the network -- read SYNCHRONOUSLY from inside a patched
//      window.fetch, so nothing can move between observation and dispatch --
//      the app has ZERO context-scoped reads outstanding, and has already
//      recorded at least one intentional abort. Cancel-without-await fails
//      here.
//  (C) NO OLD-TUPLE REQUEST COMPLETED AS AN UNEXPECTED 404, AND NO CONSOLE
//      ERROR. Every non-2xx response and every Chromium "Failed to load
//      resource" line is reconciled at the END of the viewport against the
//      allowances leg (S) declares for itself. Nothing else is forgiven.
//  (D) THE OLD RESPONSE COULD NOT POPULATE EITHER VENUE MAP OR REPAINT
//      CONTROLS. After the release and a full settle: `seasonVenueAccess` and
//      `seasonVenueCandidates` hold NO entry for the departed Season, and no
//      `#va-add-<oldSeason>` picker exists in the DOM.
//  (E) THE SAVED SERVER TUPLE IS THE NEW ONE -- read from `GET /api/context`
//      and from the switcher's own `contextOptions.saved`, which #411 reports
//      separately from `selected` precisely because a fallback can invent the
//      latter.
//  (F) ONLY THE NEW SELECTED SEASON'S CANDIDATE READ MAY COMPLETE AND PAINT.
//      No 2xx response to either scoped route names the departed Season after
//      the switch; the NEW Season's candidate map is populated and its picker
//      is painted, offering the real venue ids the fixture created.
//  (G) THE LEDGER RECONCILES EVERY INTENTIONAL ABORT EXPLICITLY. Four
//      independent records are kept and matched once, at the end:
//        responses    every method/URL/status the page received
//        failedReqs   requests never delivered (page.on("requestfailed"))
//        appAborts    the APP's own statement of intent -- `contextScopedReadAborts`,
//                     each entry naming method, URL, generation and whether the
//                     request had been dispatched
//        allowances   the deliberate 404s leg (S) declares, by exact method,
//                     URL and status
//      Every `requestfailed` must be explained by an appAborts entry with
//      `dispatched: true` for the same method and URL. An unexplained failed
//      delivery fails the journey, and the abort ledger must be NON-EMPTY so
//      the reconciliation cannot pass vacuously against a build that never
//      cancelled anything.
//  (S) THE SERVER CEILING IS UNCHANGED (direct control, no client involved).
//      With the NEW Season selected, a direct same-origin request for the OLD
//      Season's `venue-candidates` -- a sibling Season of the SAME Program --
//      is still refused 404, and its body is byte-identical to the refusal for
//      a Season id that does not exist at all. Same for `venue-access`. This is
//      the half that must NOT change, and it is asserted in the same run as the
//      fix so the two can never drift.
//
// Fails on any browser console/page error.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");
const {
  installContextFixture, selectPersistedProgram, selectPersistedProgramSeason,
} = require("./context-fixture.js");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;

// The two exact-selection-ceilinged Season reads, and the only routes enrolled
// in the barrier on the app side. Kept as ONE regexp used by the hold, the
// ledger and the server control, so those three can never disagree about what
// a "context-scoped read" is.
const SCOPED_READ_RE =
  /\/api\/v2\/setup\/seasons\/([^/?]+)\/venue-(access|candidates)(?:\?|$)/;

// Each (viewport x held read) combination gets its own server and its own page.
// Sharing one would let a leg's abandoned in-flight request settle inside the
// next leg's ledger, where it would read as an unexplained failed delivery.
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900,
    ports: { "venue-access": 8811, "venue-candidates": 8813 } },
  { label: "phone", width: 390, height: 844,
    ports: { "venue-access": 8812, "venue-candidates": 8814 } },
];
const HELD_READS = ["venue-access", "venue-candidates"];

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

const apiPost = (page, url, body) => page.evaluate(async ([u, b]) => {
  const r = await fetch(u, {
    method: "POST", credentials: "same-origin",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(b || {}),
  });
  return { status: r.status, json: await r.json().catch(() => ({})) };
}, [url, body]);

const apiGet = (page, url) => page.evaluate(async (u) => {
  const r = await fetch(u, { credentials: "same-origin" });
  return { status: r.status, json: await r.json().catch(() => ({})) };
}, url);

// SYNCHRONOUS OBSERVATION OF THE BARRIER'S ORDER (leg B).
//
// A page.route handler is out-of-band: by the time Node could ask the page how
// many scoped reads were outstanding, the answer would describe a later moment
// than the dispatch. So the observation is taken INSIDE the page, in a patched
// window.fetch, in the same synchronous turn that hands the POST to the
// network. `contextScopedReadsInFlight` and `contextScopedReadAborts` are
// app.js's own top-level bindings; `typeof` keeps this safe for the fetches
// that happen before app.js has been evaluated.
async function installContextPostObserver(page) {
  await page.addInitScript(() => {
    window.__switchPosts = [];
    const realFetch = window.fetch;
    window.fetch = function (input, init) {
      const url = typeof input === "string" ? input : ((input && input.url) || "");
      const method = String((init && init.method)
        || (input && input.method) || "GET").toUpperCase();
      if (method === "POST" && url.split("?")[0].replace(/^https?:\/\/[^/]+/, "")
          === "/api/context") {
        let inFlight = -1;
        let aborts = -1;
        try {
          if (typeof contextScopedReadsInFlight !== "undefined") {
            inFlight = contextScopedReadsInFlight.size;
          }
          if (typeof contextScopedReadAborts !== "undefined") {
            aborts = contextScopedReadAborts.length;
          }
        } catch (_) { /* app.js not evaluated yet */ }
        window.__switchPosts.push({ at: Date.now(), inFlight, aborts });
      }
      return realFetch.apply(this, arguments);
    };
  });
}

// Hold ONE scoped read for ONE Season, BEFORE it is forwarded to the server.
// Everything else on the two scoped routes is continued untouched, so the new
// Season's own reads are never slowed and leg (F) measures real timing.
async function installScopedReadHold(page, seasonId, kind) {
  // ONE-SHOT. Only the FIRST matching read is held; every later one -- the
  // app's own re-reads and, crucially, leg (S)'s direct server control, which
  // deliberately requests this exact URL to prove the ceiling still refuses it
  // -- goes straight through. A standing hold would park that control forever,
  // which is not a subtlety: it is the difference between measuring the race
  // once and quietly disabling the assertion that follows it.
  const state = { dispatched: [], releasers: [], releaseErrors: [], armed: true };
  await page.route(SCOPED_READ_RE, async (route, request) => {
    const url = request.url();
    const m = url.match(SCOPED_READ_RE);
    const isTarget = state.armed && request.method() === "GET" && m
      && m[1] === seasonId && `venue-${m[2]}` === kind;
    if (!isTarget) {
      return route.continue().catch(() => { /* page moved on */ });
    }
    state.armed = false;
    state.dispatched.push({ url, at: Date.now() });
    await new Promise((resolve) => state.releasers.push(resolve));
    try {
      // Forwarded to the server ONLY NOW. On an unbarriered build the switch
      // has already been persisted, so this is the request the ceiling refuses
      // -- the CI 404, made deterministic. On a barriered build the page has
      // already cancelled it, and Playwright reports that here rather than
      // delivering anything.
      await route.continue();
    } catch (e) {
      state.releaseErrors.push(String((e && e.message) || e));
    }
  });
  return state;
}

async function checkLeg(browser, viewport, heldKind) {
  const port = viewport.ports[heldKind];
  const base = `http://${HOST}:${port}`;
  const L = `${viewport.label}/${heldKind}`;
  const server = startServer(port);
  let serverOutput = "";
  server.stdout.on("data", (d) => { serverOutput += d.toString(); });
  server.stderr.on("data", (d) => { serverOutput += d.toString(); });

  const context = await browser.newContext({
    viewport: { width: viewport.width, height: viewport.height },
  });
  const page = await context.newPage();

  // ---- THE FOUR INDEPENDENT RECORDS (leg G) ----------------------------
  const errors = [];          // hard failures, reported together at the end
  const responses = [];       // { method, url, status } — every delivery
  const failedReqs = [];      // { method, url, failure } — never delivered
  const consoleErrors = [];   // { text, url } — Chromium's own resource lines
  const allowances = [];      // { method, url, status, matched } — declared 404s
  const fail = (msg) => errors.push(msg);
  // Declared with the CONCRETE url and status, never a category: an allowance
  // for the old Season's candidate route can only ever be satisfied by that
  // method, that URL and that status, and only once.
  const allow = (method, url, status) =>
    allowances.push({ method, url: `${base}${url}`, status, matched: false });

  page.on("pageerror", (e) => fail(`[${L}] [pageerror] ${e.message}`));
  page.on("console", (m) => {
    if (m.type() !== "error") return;
    const loc = m.location() || {};
    if (/Failed to load resource/.test(m.text())) {
      consoleErrors.push({ text: m.text(), url: loc.url || "" });
      return;
    }
    fail(`[${L}] [console] ${m.text()}${loc.url ? ` @ ${loc.url}` : ""}`);
  });
  page.on("response", (r) => {
    responses.push({ method: r.request().method(), url: r.url(),
                     status: r.status(), at: Date.now() });
  });
  page.on("requestfailed", (req) => {
    const f = req.failure();
    failedReqs.push({ method: req.method(), url: req.url(),
                      failure: (f && f.errorText) || "unknown", at: Date.now() });
  });

  await installContextPostObserver(page);
  await installContextFixture(page);

  // A timestamped progress line per stage. A journey that holds requests on
  // purpose can only fail by TIMING OUT, and a bare Playwright timeout names a
  // selector rather than the step -- exactly the anonymity #369's own review
  // called out. Written to stderr so the ledger on stdout stays clean.
  const t0 = Date.now();
  const step = (name) => process.stderr.write(
    `[${L}] +${String(Date.now() - t0).padStart(6)}ms ${name}\n`);

  let ledgerText = "";
  // Declared out here so the cleanup below can always drain it: a request left
  // parked in a route handler would settle inside the NEXT leg, where it would
  // read as an unexplained failed delivery against a ledger that never saw it
  // dispatched.
  let hold = { dispatched: [], releasers: [], releaseErrors: [] };
  try {
    step("server up: waiting");
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    step("server up");
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 15000 });
    if ((await apiPost(page, "/api/auth/login",
                       { username: "admin", password: "demo" })).status !== 200) {
      throw new Error(`[${L}] admin login failed`);
    }

    // ------------------------------- fixture ----------------------------
    // TWO Seasons of the SAME Program, which is the sharpest shape available:
    // the departed Season is a SIBLING of the arriving one, so nothing about
    // the Program axis can accidentally explain a refusal, and the direct
    // server control in leg (S) is proving the exact-Season ceiling rather
    // than a Program ceiling.
    //
    // TWO Venues, granted asymmetrically, so both Seasons render a real Allow
    // picker AND their two maps are distinguishable by CONTENT:
    //   S1 -> venue_access [venue1], candidates [venue2]
    //   S2 -> venue_access []      , candidates [venue1, venue2]
    // A leak of S1's data into S2's surface is therefore visible, not merely
    // countable.
    step("logged in");
    const roots = await page.evaluate(async () => {
      const f = window.hsFixture;
      const org = await f.create("org", "/api/v2/setup/organization",
        { name: "Settlement Org" });
      const program = await f.create("program", "/api/v2/setup/program",
        { name: "Settlement Program", organization_id: org.id });
      return { org: org.id, program: program.id };
    });
    step("roots created");
    await selectPersistedProgram(page, `[${L}] Program-only bootstrap`, roots.program);
    const built = await page.evaluate(async (r) => {
      const f = window.hsFixture;
      const s1 = await f.create("season 1", "/api/v2/setup/season",
        { program_id: r.program, name: "2026-27" });
      const s2 = await f.create("season 2", "/api/v2/setup/season",
        { program_id: r.program, name: "2027-28" });
      const venue1 = await f.create("venue 1", "/api/v2/setup/venue",
        { name: "Settlement Arena One", organization_id: r.org });
      const venue2 = await f.create("venue 2", "/api/v2/setup/venue",
        { name: "Settlement Arena Two", organization_id: r.org });
      await f.create("rink 1", "/api/v2/setup/rink",
        { venue_id: venue1.id, name: "Sheet A" });
      await f.create("rink 2", "/api/v2/setup/rink",
        { venue_id: venue2.id, name: "Sheet B" });
      return { s1: s1.id, s2: s2.id, venue1: venue1.id, venue2: venue2.id };
    }, roots);
    step("seasons/venues/rinks created");
    // The grant is SEASON-OWNED, so it needs both axes persisted.
    await selectPersistedProgramSeason(page, `[${L}] Program+Season 1`,
      roots.program, built.s1);
    await page.evaluate(async (b) => {
      await window.hsFixture.create("grant venue1 to season 1",
        `/api/v2/setup/seasons/${b.s1}/venue-access`, { venue_id: b.venue1 });
    }, built);

    step("grant made");
    // Reload so the shell boots on the persisted Season 1 with a clean render.
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 15000 });
    await page.waitForFunction(() => {
      const s = document.getElementById("ctx-select");
      return !!(s && !s.hidden && s.options.length >= 2);
    }, null, { timeout: 15000 });

    step("shell reloaded on Season 1");
    const oldValue = `${roots.program}|${built.s1}`;
    const newValue = `${roots.program}|${built.s2}`;
    const selectValues = await page.locator("#ctx-select option")
      .evaluateAll((os) => os.map((o) => o.value));
    if (!selectValues.includes(oldValue) || !selectValues.includes(newValue)) {
      throw new Error(`[${L}] both Seasons must be real switcher options; got `
        + JSON.stringify(selectValues));
    }
    if ((await page.evaluate(() => document.getElementById("ctx-select").value))
        !== oldValue) {
      throw new Error(`[${L}] the shell did not boot on the persisted Season 1`);
    }

    // ---- BASELINE: Setup > Hierarchy under the OLD Season, UNHELD ------
    // The hold is armed only AFTER this pass, deliberately. Holding a read
    // during the first Setup render would stall render() before it paints
    // anything, so there would be no Season block, no Allow picker and no
    // populated maps -- and leg (D)'s "the departed Season is gone" would then
    // be true of a surface that never had it. This pass establishes that the
    // departing Season's data really IS what the maps and the DOM hold, so its
    // absence after the switch is a CHANGE rather than an emptiness.
    step("entering Setup > Hierarchy (unheld)");
    await page.click('.tab[data-tab="setup"]');
    await page.click('[data-setup-view="hierarchy"]');
    await page.waitForSelector(`#va-add-${built.s1}`, { timeout: 20000 });
    step("baseline painted");
    const baseline = await page.evaluate((b) => ({
      access: Object.keys(seasonVenueAccess),
      candidates: Object.keys(seasonVenueCandidates),
      oldAccess: (seasonVenueAccess[b.s1] || []).map((a) => a.venue_id).sort(),
      oldCandidates: (seasonVenueCandidates[b.s1] || []).map((v) => v.id).sort(),
      oldPicker: !!document.getElementById(`va-add-${b.s1}`),
    }), built);
    if (!baseline.access.includes(built.s1) || !baseline.candidates.includes(built.s1)
        || !baseline.oldPicker
        || JSON.stringify(baseline.oldAccess) !== JSON.stringify([built.venue1])
        || JSON.stringify(baseline.oldCandidates) !== JSON.stringify([built.venue2])) {
      throw new Error(`[${L}] baseline: the DEPARTING Season must really hold its own `
        + `venue data and picker before the switch, or leg (D) proves nothing: `
        + JSON.stringify(baseline));
    }

    // ---- arm the hold, then re-render the SAME surface ------------------
    // Clicking the sub-view toggle is an ordinary operator action whose handler
    // is an unconditional render(), so the two scoped reads are dispatched
    // again -- this time with one of them held before it can reach the server.
    step("arming hold");
    hold = await installScopedReadHold(page, built.s1, heldKind);
    const postsBefore = (await page.evaluate(() => window.__switchPosts.slice())).length;
    await page.click('[data-setup-view="hierarchy"]');

    // (A) THE HOLD IS REAL AND POST-DISPATCH, established in TWO independent
    //     ways, and deliberately in this order.
    //
    //     THE ROUTE'S OWN OBSERVATION IS THE GATE. It is true of ANY build --
    //     including one where the reads are not enrolled in the barrier at all
    //     -- so the rest of the leg still runs there and the 404 that build
    //     actually produces is still collected and reported. Gating on the
    //     app's registry instead would make the pre-fix build fail by TIMING
    //     OUT on a missing instrument, reporting the absence of the fix rather
    //     than the defect the fix exists for.
    const dispatchDeadline = Date.now() + 20000;
    while (!hold.dispatched.length) {
      if (Date.now() > dispatchDeadline) {
        throw new Error(`[${L}] (A) the ${heldKind} read for the departing Season `
          + `was never dispatched, so there is no race to run`);
      }
      await page.waitForTimeout(25);
    }
    //     THE APP'S REGISTRY IS THE ASSERTION. The read must be enrolled in the
    //     barrier -- that is the precondition the whole fix is about -- but it
    //     is reported, not thrown, for the reason above. Read by URL rather
    //     than by a count, because the unheld sibling read is briefly
    //     outstanding too and a count could be satisfied by it.
    const enrolled = await page.evaluate((want) => {
      if (typeof contextScopedReadsInFlight === "undefined") return null;
      return Array.from(contextScopedReadsInFlight, (e) => e.url)
        .filter((u) => u.indexOf(want) >= 0);
    }, `/seasons/${built.s1}/${heldKind}`).catch(() => null);
    if (!enrolled || !enrolled.length) {
      fail(`[${L}] (A) the held ${heldKind} read is NOT enrolled in the context-`
        + `scoped read barrier (app registry: ${JSON.stringify(enrolled)}). A `
        + `read the switch does not know about cannot be cancelled or waited `
        + `for, which is exactly the pre-fix state.`);
    }

    // ---- THE REAL SWITCH, through the shipped control -------------------
    // page.selectOption fires the genuine `change` event #ctx-select's own
    // onchange listens for, which is the ONLY operator path into
    // setActiveContext for a Program/Season pick. No raw POST anywhere.
    step("held read is outstanding; switching");
    await page.selectOption("#ctx-select", newValue);
    step("selectOption returned");

    // (E) THE SAVED SERVER TUPLE MOVED. Awaited before the release, so the
    //     release genuinely happens against the NEW persisted selection --
    //     the exact ordering CI hit.
    const savedDeadline = Date.now() + 20000;
    for (;;) {
      const seen = await apiGet(page, "/api/context");
      if (seen.status === 200 && seen.json && seen.json.season_id === built.s2) break;
      if (Date.now() > savedDeadline) {
        throw new Error(`[${L}] (E) the switch never persisted; GET /api/context `
          + `still reads ${JSON.stringify(seen.json)}`);
      }
      await page.waitForTimeout(50);
    }

    step("new tuple persisted");
    // (B) THE BARRIER ORDERED THE SWITCH.
    const switchPosts = (await page.evaluate(() => window.__switchPosts.slice()))
      .slice(postsBefore);
    if (!switchPosts.length) {
      fail(`[${L}] (B) no POST /api/context was observed for the switch`);
    }
    switchPosts.forEach((p, i) => {
      if (p.inFlight !== 0) {
        fail(`[${L}] (B) POST /api/context #${i} was dispatched while `
          + `${p.inFlight} context-scoped read(s) were STILL OUTSTANDING. The `
          + `switch must cancel them and AWAIT THEIR SETTLEMENT first — an `
          + `abort that does not un-send the request is exactly what leaves the `
          + `server free to answer an old-tuple read under a new selection`);
      }
      if (p.aborts < 1) {
        fail(`[${L}] (B) POST /api/context #${i} was dispatched with `
          + `${p.aborts} recorded intentional aborts. The held read was `
          + `outstanding at the switch, so at least one cancellation must have `
          + `been recorded before the POST — zero means nothing was cancelled `
          + `and the assertion above passed vacuously`);
      }
    });

    // ---- release the held read, AFTER the new tuple is persisted --------
    step("releasing held read");
    hold.releasers.splice(0).forEach((r) => r());
    // Give the released request every chance to reach the server, be answered,
    // and have its console error written. A build without the barrier fails on
    // the ledger below because of what happens in this window.
    await page.waitForTimeout(1500);
    // Tolerant of the binding being ABSENT, for the same reason leg (A)'s gate
    // is: on a build with no barrier there is no registry to drain, and this
    // wait must not become the way that build fails.
    await page.waitForFunction(
      () => typeof contextScopedReadsInFlight === "undefined"
        || contextScopedReadsInFlight.size === 0, null, { timeout: 20000 });

    step("scoped reads settled after release");
    // (F) ONLY THE NEW SELECTED SEASON'S CANDIDATE READ MAY COMPLETE AND PAINT.
    await page.waitForFunction(
      (s2) => typeof seasonVenueCandidates !== "undefined"
        && Object.prototype.hasOwnProperty.call(seasonVenueCandidates, s2),
      built.s2, { timeout: 20000 })
      .catch(() => {
        throw new Error(`[${L}] (F) the NEW Season's candidate read never `
          + `populated its map — the barrier must not cost the arriving `
          + `Season its own read`);
      });
    await page.waitForSelector(`#va-add-${built.s2}`, { timeout: 20000 });
    const painted = await page.evaluate((b) => ({
      access: Object.keys(seasonVenueAccess),
      candidates: Object.keys(seasonVenueCandidates),
      newCandidates: (seasonVenueCandidates[b.s2] || []).map((v) => v.id).sort(),
      newPickerOptions: Array.from(
        document.querySelectorAll(`#va-add-${b.s2} option`), (o) => o.value)
        .filter(Boolean).sort(),
      oldPicker: !!document.getElementById(`va-add-${b.s1}`),
      savedSeason: (contextOptions && contextOptions.saved
        && contextOptions.saved.season_id) || null,
      selectedSeason: (contextOptions && contextOptions.selected
        && contextOptions.selected.season_id) || null,
      appAborts: typeof contextScopedReadAborts === "undefined"
        ? [] : contextScopedReadAborts.slice(),
    }), built);

    // (D) THE OLD RESPONSE COULD NOT POPULATE EITHER VENUE MAP...
    if (painted.access.includes(built.s1)) {
      fail(`[${L}] (D) seasonVenueAccess still holds the DEPARTED Season `
        + `${built.s1}: ${JSON.stringify(painted.access)}`);
    }
    if (painted.candidates.includes(built.s1)) {
      fail(`[${L}] (D) seasonVenueCandidates still holds the DEPARTED Season `
        + `${built.s1}: ${JSON.stringify(painted.candidates)}`);
    }
    // ...NOR REPAINT CONTROLS.
    if (painted.oldPicker) {
      fail(`[${L}] (D) the DEPARTED Season's Allow picker (#va-add-${built.s1}) `
        + `is still in the DOM after the switch`);
    }
    // NON-VACUOUS: the arriving Season really did get both venues offered, so
    // "no old data" is not passing because there is no data at all.
    const wantCandidates = [built.venue1, built.venue2].sort();
    if (JSON.stringify(painted.newCandidates) !== JSON.stringify(wantCandidates)) {
      fail(`[${L}] (F) the NEW Season's candidates are `
        + `${JSON.stringify(painted.newCandidates)}, expected `
        + `${JSON.stringify(wantCandidates)} — the arriving Season's own read `
        + `must complete and paint`);
    }
    if (JSON.stringify(painted.newPickerOptions) !== JSON.stringify(wantCandidates)) {
      fail(`[${L}] (F) the NEW Season's Allow picker offers `
        + `${JSON.stringify(painted.newPickerOptions)}, expected `
        + `${JSON.stringify(wantCandidates)}`);
    }
    // (E) ...and the CLIENT agrees with the server about what is SAVED.
    if (painted.savedSeason !== built.s2 || painted.selectedSeason !== built.s2) {
      fail(`[${L}] (E) the switcher reports saved=${painted.savedSeason} `
        + `selected=${painted.selectedSeason}, expected both to be ${built.s2}`);
    }

    // ---- (S) THE DIRECT SERVER CONTROL ---------------------------------
    // No client state involved: raw same-origin requests, with Season 2
    // persisted. A SIBLING Season of the SAME Program must be refused exactly
    // like a Season that does not exist. This is #369's ceiling, and fixing
    // the client must not weaken it.
    step("post-switch assertions done");
    const ghost = "season_no_such_season";
    for (const kind of ["venue-candidates", "venue-access"]) {
      const siblingPath = `/api/v2/setup/seasons/${built.s1}/${kind}`;
      const ghostPath = `/api/v2/setup/seasons/${ghost}/${kind}`;
      allow("GET", siblingPath, 404);
      allow("GET", ghostPath, 404);
      const sibling = await apiGet(page, siblingPath);
      const missing = await apiGet(page, ghostPath);
      if (sibling.status !== 404) {
        fail(`[${L}] (S) a NON-SELECTED sibling Season's ${kind} answered `
          + `${sibling.status}, not 404 — #369's exact-selection ceiling has `
          + `been weakened: ${JSON.stringify(sibling.json)}`);
      }
      if (missing.status !== 404) {
        fail(`[${L}] (S) a NONEXISTENT Season's ${kind} answered `
          + `${missing.status}, not 404`);
      }
      // GENERIC means "carries no information the caller did not already
      // have". Both refusals name the Season id THE CALLER ITSELF SUPPLIED, so
      // that token is masked out before comparing -- echoing back your own
      // input is not an oracle, and demanding byte-identity WITH the ids in
      // place would fail on a difference that discloses nothing. Everything
      // else -- status, error code, and the message around the mask -- must be
      // identical, which is what makes "exists in another Season of my own
      // Program" indistinguishable from "never existed".
      const mask = (payload, id) =>
        JSON.stringify(payload).split(id).join("<season>");
      if (mask(sibling.json, built.s1) !== mask(missing.json, ghost)) {
        fail(`[${L}] (S) the ${kind} refusal is not GENERIC — a sibling Season `
          + `answers ${JSON.stringify(sibling.json)} while a nonexistent one `
          + `answers ${JSON.stringify(missing.json)}; masking each request's `
          + `own id still leaves them different, which is an existence oracle`);
      }
    }

    // ---- (G) THE LEDGER, RECONCILED ONCE, AT THE END -------------------
    step("server control done");
    const rel = (u) => u.replace(/^https?:\/\/[^/]+/, "");
    const appAborts = painted.appAborts;
    // Every failed delivery must be explained by the app's own statement of
    // intent. Matching is by method+URL against a DISPATCHED abort, and each
    // abort explains at most one failure.
    const unexplainedFailures = [];
    const abortPool = appAborts.filter((a) => a.dispatched)
      .map((a) => ({ ...a, used: false }));
    failedReqs.forEach((f) => {
      const hit = abortPool.find((a) => !a.used && a.method === f.method
        && rel(a.url) === rel(f.url));
      if (hit) { hit.used = true; return; }
      unexplainedFailures.push(f);
    });
    if (unexplainedFailures.length) {
      fail(`[${L}] (G) request-failure ledger has entries the app never claimed `
        + `to cancel: ${JSON.stringify(unexplainedFailures)}\n`
        + `app abort ledger: ${JSON.stringify(appAborts)}`);
    }
    // NON-VACUOUS: the barrier must actually have cancelled the held read.
    if (!appAborts.some((a) => rel(a.url)
        === `/api/v2/setup/seasons/${built.s1}/${heldKind}`)) {
      fail(`[${L}] (G) the app recorded NO intentional abort for the held `
        + `${heldKind} read of the departing Season, so the reconciliation `
        + `above proves nothing: ${JSON.stringify(appAborts)}`);
    }
    // (C) NO OLD-TUPLE REQUEST COMPLETED AS AN UNEXPECTED 404.
    const nonOk = responses.filter((r) => r.status < 200 || r.status > 299);
    const unexplainedNonOk = [];
    nonOk.forEach((r) => {
      const hit = allowances.find((a) => !a.matched && a.method === r.method
        && rel(a.url) === rel(r.url) && a.status === r.status);
      if (hit) { hit.matched = true; return; }
      unexplainedNonOk.push(r);
    });
    if (unexplainedNonOk.length) {
      fail(`[${L}] (C) the page received non-2xx responses this journey never `
        + `asked for: ${JSON.stringify(unexplainedNonOk.map(
          (r) => `${r.method} ${rel(r.url)} -> ${r.status}`))}\n`
        + `THIS IS THE DEFECT'S OWN SIGNATURE when it names the departing `
        + `Season's venue-access/venue-candidates: an in-flight scoped read `
        + `reached the server after the context POST committed.`);
    }
    const unmatchedAllowances = allowances.filter((a) => !a.matched);
    if (unmatchedAllowances.length) {
      fail(`[${L}] (S) a declared 404 never happened, so the server control did `
        + `not run: ${JSON.stringify(unmatchedAllowances.map(
          (a) => `${a.method} ${rel(a.url)} -> ${a.status}`))}`);
    }
    // ...AND NO CONSOLE ERROR. Chromium writes one "Failed to load resource"
    // line per failing request; only the deliberate ones may be forgiven.
    const unexplainedConsole = consoleErrors.filter((c) => {
      const u = rel(c.url || "");
      return !allowances.some((a) => rel(a.url) === u);
    });
    if (unexplainedConsole.length) {
      fail(`[${L}] (C) unexplained console errors: `
        + JSON.stringify(unexplainedConsole));
    }

    // The ledger is PRINTED whether or not it passed — the owner asked for the
    // method/URL/status and request-failure records verbatim, and a green run
    // that shows nothing is not evidence that anything was observed.
    const scoped = responses
      .filter((r) => SCOPED_READ_RE.test(rel(r.url)) || rel(r.url) === "/api/context")
      .map((r) => `  ${r.method} ${rel(r.url)} -> ${r.status}`);
    ledgerText = [
      `[${L}] LEDGER`,
      `  context + scoped-read responses (method URL -> status):`,
      ...scoped,
      `  request failures (never delivered):`,
      ...(failedReqs.length
        ? failedReqs.map((f) => `    ${f.method} ${rel(f.url)} -> ${f.failure}`)
        : ["    (none)"]),
      `  app's intentional aborts (contextScopedReadAborts):`,
      ...appAborts.map((a) => `    ${a.method} ${rel(a.url)} `
        + `generation=${a.generation} dispatched=${a.dispatched}`),
      `  declared 404 allowances (direct server control):`,
      ...allowances.map((a) => `    ${a.method} ${rel(a.url)} -> ${a.status} `
        + `matched=${a.matched}`),
      `  POST /api/context observations (scoped reads outstanding at dispatch):`,
      ...switchPosts.map((p, i) => `    #${i} inFlight=${p.inFlight} `
        + `abortsRecorded=${p.aborts}`),
      `  route release errors (a cancelled request can no longer be forwarded):`,
      ...(hold.releaseErrors.length
        ? hold.releaseErrors.map((e) => `    ${e}`)
        : ["    (none)"]),
    ].join("\n");
  } finally {
    hold.releasers.splice(0).forEach((r) => r());
    await page.unrouteAll({ behavior: "ignoreErrors" }).catch(() => {});
    await context.close();
    await stopServer(server);
  }
  if (ledgerText) console.log(ledgerText);
  if (errors.length) {
    throw new Error(`[${L}] FAILED:\n${errors.join("\n")}\n\n`
      + `server output:\n${serverOutput.slice(-4000)}`);
  }
  console.log(`[${L}] context-switch read settlement barrier: PASSED`);
}

(async () => {
  const browser = await chromium.launch();
  try {
    for (const viewport of VIEWPORTS) {
      for (const heldKind of HELD_READS) {
        await checkLeg(browser, viewport, heldKind);
      }
    }
    console.log("Context-switch read settlement browser journey passed.");
  } catch (error) {
    console.error("Context-switch read settlement browser journey FAILED.");
    console.error(error.message);
    process.exitCode = 1;
  } finally {
    await browser.close();
  }
})();
