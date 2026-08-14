// A SCOPED READ DISPATCHED BEFORE A SWITCH BUT ARRIVING AFTER IT IS DISCARDED,
// NOT REFUSED (#159 follow-up to #415).
//
// WHAT #415 CLOSED AND WHAT IT COULD NOT. `context-switch-server-exit.js`
// proves a switch cannot commit while a scoped read is ALREADY INSIDE the
// server. That guarantee is asserted, unchanged, by that journey and by
// `tests/test_context_switch_server_exit.py`, and nothing here trades any of it
// away. But the gate orders participants by ARRIVAL AT `do_GET`, and `app.js`
// orders them by FETCH DISPATCH. Between those two instants lies an interval
// neither process owns -- the browser's own request queue and the wire:
//
//     render() dispatches GET .../seasons/<old>/venue-candidates
//     the operator switches; cancelContextScopedReads() aborts it, the JS
//       promise SETTLES, awaitContextScopedReadSettlement() therefore RETURNS
//     POST /api/context reaches the server FIRST, takes the gate's writer slot
//       and commits
//     only THEN does the GET reach do_GET, takes a HIGHER arrival sequence,
//       correctly waits behind that writer, correctly runs against the NEW
//       tuple -- and the unchanged exact-Season ceiling correctly answers the
//       generic 404, for a question the operator already withdrew
//
//     CI RECORD: main@1de50d7 and main@e385bfb both fail browser shard 1,
//     journey setup-state-matrix, DESKTOP leg, with
//         GET /api/v2/setup/seasons/season_3/venue-candidates -> 404
//     while app.js's own ledger records that read as generation=5
//     dispatched=true. No [context-gate] line is ever logged, because no
//     timeout is involved: every component behaved correctly.
//
// AN ABORT IS NOT A TRANSPORT CANCELLATION, which is why no amount of client
// work can close this. `AbortController.abort()` settles OUR promise; it cannot
// un-send a request already dispatched, and cannot stop the server answering
// one it has not yet read. So the missing fact -- WHICH SELECTION THIS READ WAS
// RENDERED UNDER -- travels ON THE READ ITSELF, in `X-Context-Epoch`, and the
// server compares it against the epoch of the persisted row as it stands when
// the request finally arrives. Equal proceeds as today, ceiling included;
// unequal answers 204 with no body BEFORE the ceiling is evaluated; absent
// behaves exactly as it did before any of this existed.
//
// NOTHING IS RETAINED to make that comparison -- no ledger, no TTL, no
// eviction -- so an arrival delayed by an hour is judged exactly as correctly
// as one delayed by a millisecond. See
// backend/hockey_scheduler/services/context_epoch.py.
//
// HOW THE INTERVAL IS FORCED RATHER THAN RACED. `page.route` cannot do it: it
// holds a request before it is forwarded, and the journey needs the server to
// have READ the request and be about to route it. So the backend runs under
// `server-park-harness.py`, whose ARRIVAL PARK (`/arm-late`) blocks the target
// GET at the very top of `do_GET`, strictly ABOVE `CONTEXT_GATE.arrive()`. For
// as long as it is held the gate holds no ticket for that request -- the switch
// therefore waits for nothing and commits -- which is precisely a browser
// queue. No production file gains a hook; `hockey_scheduler` runs byte-
// identical to production.
//
// THE READ IS THE APP'S OWN, not a raw fetch, and that is the difference from
// the sibling journey. Only a read the app enrolled carries the epoch, so the
// whole path -- learn the epoch with the tuple, echo it on the read, compare it
// on arrival -- is exercised end to end through the shipped code.
//
// NO SLEEP CARRIES ANY PROOF IN THIS FILE. Every wait is `waitFor`, which polls
// a real condition and fails loudly if that condition never becomes true; its
// 25ms pause is the poll INTERVAL, so the proof rests on the predicate and a
// slower machine only takes more iterations. There is no `waitForTimeout`
// anywhere, and in particular none between arming the park and the assertions
// that depend on it.
//
// WHAT EACH LEG ASSERTS, per viewport (desktop 1440x900 AND phone 390x844, the
// leg CI failed on) and per venue route:
//
//  (A) THE READ IS DISPATCHED AND NOT YET ARRIVED. The harness reports
//      `late_parked`, and the app's own barrier reports the read as enrolled
//      and in flight. Both, because either alone is satisfiable by a build that
//      is not doing what this journey is about.
//  (B) THE SWITCH GOES FIRST AND COMPLETES. With the read still held, the real
//      operator switch through `#ctx-select` commits: its POST answers 200 and
//      `GET /api/context` reads the NEW Season. This is the OPPOSITE of leg (B)
//      in the sibling journey, and deliberately: there the switch must be
//      unable to commit, because the read was inside the server. Here it must
//      commit, because the read is not.
//  (C) THE LATE ARRIVAL IS DISCARDED, NOT REFUSED. Released, the handler
//      answers 204 and never reaches ApiService at all -- so the exact-Season
//      ceiling was not merely satisfied, it was never consulted, and the answer
//      can disclose nothing about the Season in either direction.
//  (D) NOTHING ON THE WIRE, AND NOTHING IN THE CONSOLE. No undeclared non-2xx
//      on the two scoped routes, and no console error.
//  (E) THE EPOCH ACTUALLY TRAVELLED, AND WAS ACTUALLY STALE. The held request
//      carried an `X-Context-Epoch` equal to the epoch `/api/context` reported
//      BEFORE the switch, and `/api/context` reports a DIFFERENT one after it.
//      Without both halves the leg could pass on a build that sends no epoch
//      (which is the control's outcome) or one whose epoch never moves (which
//      would make every stale read admissible).
//  (F) THE CLIENT ADOPTED THE NEW EPOCH. `contextEpoch` in the page equals what
//      the server now reports, so the reads of the NEXT render are echoing the
//      tuple they are actually rendered under rather than the one just left.
//  (S) THE CEILING IS UNWEAKENED. With the new Season selected, an independent
//      read of the departed SIBLING Season is still refused 404, and its body
//      is indistinguishable from a Season id that never existed.
//
// FOUR LEGS PER VIEWPORT, and two of them exist to keep the other two honest;
// see `LEGS` below for what each mode constructs.
//
//   * A CONTROL LEG, which is what makes the rest falsifiable: the identical
//     interleaving with `X-Context-Epoch` STRIPPED off the held read in flight
//     (`page.route`), leaving the gate, the ceiling, the park and the switch
//     untouched. The 404 must come back. That is simultaneously the falsifier
//     and the no-regression proof for every client that never sends an epoch:
//     absent must mean exactly what it meant on main. If it goes green, this
//     journey is measuring something other than the epoch -- for instance a
//     build that stopped refusing late reads generally, which would be a
//     widened ceiling wearing the fix's clothes.
//   * A HELD-POST LEG, which reaches the read no client-side barrier can: one
//     enrolled AFTER the switch cancelled its predecessors, by a render() run
//     while the switch's POST is held in the network. That read is never
//     aborted, so the browser actually RECEIVES the 204 -- which is the only
//     mode in which `app.js`'s own 204 branch is reachable at all, and the
//     leg asserts the app recorded it as a DISCARD rather than as data.
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
const HARNESS = path.resolve(__dirname, "server-park-harness.py");
const READY_TIMEOUT_MS = 20000;

// The same single definition of "a context-scoped read" the sibling journeys
// use, so the three can never disagree about which routes are enrolled.
const SCOPED_READ_RE =
  /\/api\/v2\/setup\/seasons\/([^/?]+)\/venue-(access|candidates)(?:\?|$)/;

const EPOCH_HEADER = "x-context-epoch";

// Each leg gets its own server and page: a request abandoned in one leg would
// otherwise settle inside the next leg's ledger.
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, basePort: 8831 },
  { label: "phone", width: 390, height: 844, basePort: 8841 },
];
// THREE MODES, and the third is not a variation on the first two.
//
//   switch     -- the shipped interleaving. The operator switches through
//                 `#ctx-select`, which CANCELS the in-flight scoped read; the
//                 read is already parked above the gate, so the switch commits
//                 and the read arrives afterwards. The client never sees the
//                 204 in this mode and cannot: it aborted that fetch, so
//                 Chromium tore the request down. The 204 is observed from
//                 INSIDE the server, which is why the harness has a status
//                 probe at all.
//   control    -- `switch` with `X-Context-Epoch` stripped off the held read in
//                 flight. The 404 must come back. Falsifier for everything
//                 else, and the no-regression proof for any client that sends
//                 no epoch.
//   held-post  -- THE READ THE CLIENT CANNOT CANCEL, which is the interleaving
//                 no client-side barrier can reach and the one a reviewer
//                 should look hardest at. The switch's POST is HELD in the
//                 network, so `cancelContextScopedReads()` has already run; a
//                 render() forced in that window enrols a NEW scoped read for
//                 the departing Season on a fresh, un-aborted controller. The
//                 POST is then released and commits while that read is parked.
//                 Nothing the client can do reaches it -- and it comes back
//                 204, which the client SEES and records as a discard.
const LEGS = [
  { kind: "venue-candidates", mode: "switch" },
  { kind: "venue-access", mode: "switch" },
  { kind: "venue-candidates", mode: "control" },
  { kind: "venue-candidates", mode: "held-post" },
];

function waitForServer(url, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const attempt = () => {
      const request = http.get(url, (response) => { response.resume(); resolve(); });
      request.setTimeout(2000, () => request.destroy(new Error("timed out")));
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

function startServer(port, controlPort) {
  return spawn(
    process.env.PYTHON || "python3",
    ["-u", HARNESS, "--host", HOST, "--port", String(port),
     "--control-port", String(controlPort)],
    { cwd: BACKEND_DIR, stdio: ["ignore", "pipe", "pipe"] });
}

// The control plane speaks from NODE, not from the page, so nothing the browser
// does can be mistaken for an observation of the server's own state.
function control(controlPort, method, path_, body) {
  const payload = body === undefined ? null : Buffer.from(JSON.stringify(body));
  return new Promise((resolve, reject) => {
    const req = http.request(
      { host: HOST, port: controlPort, path: path_, method,
        headers: payload
          ? { "Content-Type": "application/json", "Content-Length": payload.length }
          : {} },
      (res) => {
        let raw = "";
        res.on("data", (d) => { raw += d; });
        res.on("end", () => {
          try { resolve(JSON.parse(raw || "{}")); }
          catch (e) { reject(new Error(`control ${path_}: ${raw}`)); }
        });
      });
    req.setTimeout(5000, () => req.destroy(new Error(`control ${path_} timed out`)));
    req.on("error", reject);
    if (payload) req.write(payload);
    req.end();
  });
}

const apiGet = (page, url) => page.evaluate(async (u) => {
  const r = await fetch(u, { credentials: "same-origin" });
  return { status: r.status, json: await r.json().catch(() => ({})) };
}, url);

const apiPost = (page, url, body) => page.evaluate(async ([u, b]) => {
  const r = await fetch(u, {
    method: "POST", credentials: "same-origin",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(b || {}),
  });
  return { status: r.status, json: await r.json().catch(() => ({})) };
}, [url, body]);

// A CONDITION WAIT, never a sleep. It returns the moment `fn` is truthy and
// throws `describe()` if it never is, so what a leg proves is the PREDICATE.
// The 25ms below is the poll interval and nothing else: no assertion in this
// file is true because 25ms elapsed, and on a slower machine the loop simply
// runs more times. There is deliberately no `page.waitForTimeout` anywhere in
// this journey -- a fixed pause between arming the park and the assertions that
// depend on it would make the proof a race, which is the one thing the owner's
// determinism rule forbids.
async function waitFor(fn, timeoutMs, describe) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const value = await fn();
    if (value) return value;
    if (Date.now() > deadline) throw new Error(describe());
    await new Promise((r) => setTimeout(r, 25));
  }
}

async function checkLeg(browser, viewport, leg, index) {
  const port = viewport.basePort + index;
  const controlPort = port + 100;
  const base = `http://${HOST}:${port}`;
  const isControl = leg.mode === "control";
  const isHeldPost = leg.mode === "held-post";
  const L = `${viewport.label}/${leg.kind}/${leg.mode}`;
  const server = startServer(port, controlPort);
  let serverOutput = "";
  server.stdout.on("data", (d) => { serverOutput += d.toString(); });
  server.stderr.on("data", (d) => { serverOutput += d.toString(); });

  const context = await browser.newContext({
    viewport: { width: viewport.width, height: viewport.height },
  });
  const page = await context.newPage();

  const errors = [];
  const responses = [];
  const consoleErrors = [];
  const allowances = [];
  const fail = (msg) => errors.push(msg);
  const rel = (u) => u.replace(/^https?:\/\/[^/]+/, "");
  const allow = (method, url, status) =>
    allowances.push({ method, url, status, matched: false });

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
  // THE EPOCH, READ OFF THE WIRE rather than out of the page. Leg (E) needs to
  // know the read really CARRIED it; a page-side observer would be satisfied by
  // the app merely intending to send one.
  const scopedReadRequests = [];
  page.on("request", (r) => {
    if (r.method() !== "GET" || !SCOPED_READ_RE.test(r.url())) return;
    const h = r.headers();
    scopedReadRequests.push({ url: rel(r.url()), epoch: h[EPOCH_HEADER] || "" });
  });
  // What the control leg actually removed, captured inside the interception.
  const strippedEpochs = [];
  const switchPosts = [];
  page.on("request", (r) => {
    if (r.method() === "POST" && rel(r.url()) === "/api/context") {
      switchPosts.push({ at: Date.now() });
    }
  });
  // The `held-post` mode's grip on the switch. Holding the POST is what makes
  // "a render ran while the switch was pending" a CONSTRUCTION rather than a
  // race: the window is open for exactly as long as this promise is unresolved.
  //
  // ARMED LATER, NOT HERE. The fixture below makes its own `POST /api/context`
  // calls (`selectPersistedProgram`/`selectPersistedProgramSeason`), and an
  // armed-from-birth hold catches the FIRST of those and wedges the leg before
  // it has built anything. The flag is raised immediately before the switch
  // that is meant to be held, and nowhere else.
  let holdNextPost = false;
  let releaseHeldPost = () => {};
  const heldPost = new Promise((r) => { releaseHeldPost = r; });
  if (isHeldPost) {
    await page.route("**/api/context", async (route, request) => {
      if (request.method() !== "POST" || !holdNextPost) return route.continue();
      holdNextPost = false;
      await heldPost;
      return route.continue();
    });
  }

  await installContextFixture(page);

  const t0 = Date.now();
  const step = (name) => process.stderr.write(
    `[${L}] +${String(Date.now() - t0).padStart(6)}ms ${name}\n`);

  let releasedLate = false;
  try {
    step("waiting for server");
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 15000 });
    if ((await apiPost(page, "/api/auth/login",
                       { username: "admin", password: "demo" })).status !== 200) {
      throw new Error(`[${L}] admin login failed`);
    }
    step("logged in");

    // ------------------------------- fixture ----------------------------
    // TWO Seasons of the SAME Program, so the Season the read names is a
    // SIBLING of the one being switched to and nothing about the Program axis
    // can explain a refusal.
    const roots = await page.evaluate(async () => {
      const f = window.hsFixture;
      const org = await f.create("org", "/api/v2/setup/organization",
        { name: "Late Arrival Org" });
      const program = await f.create("program", "/api/v2/setup/program",
        { name: "Late Arrival Program", organization_id: org.id });
      return { org: org.id, program: program.id };
    });
    await selectPersistedProgram(page, `[${L}] Program-only bootstrap`, roots.program);
    const built = await page.evaluate(async (r) => {
      const f = window.hsFixture;
      const s1 = await f.create("season 1", "/api/v2/setup/season",
        { program_id: r.program, name: "2026-27" });
      const s2 = await f.create("season 2", "/api/v2/setup/season",
        { program_id: r.program, name: "2027-28" });
      const venue1 = await f.create("venue 1", "/api/v2/setup/venue",
        { name: "Late Arena One", organization_id: r.org });
      const venue2 = await f.create("venue 2", "/api/v2/setup/venue",
        { name: "Late Arena Two", organization_id: r.org });
      await f.create("rink 1", "/api/v2/setup/rink",
        { venue_id: venue1.id, name: "Sheet A" });
      await f.create("rink 2", "/api/v2/setup/rink",
        { venue_id: venue2.id, name: "Sheet B" });
      return { s1: s1.id, s2: s2.id, venue1: venue1.id, venue2: venue2.id };
    }, roots);
    await selectPersistedProgramSeason(page, `[${L}] Program+Season 1`,
      roots.program, built.s1);
    await page.evaluate(async (b) => {
      await window.hsFixture.create("grant venue1 to season 1",
        `/api/v2/setup/seasons/${b.s1}/venue-access`, { venue_id: b.venue1 });
    }, built);
    step("fixture built on Season 1");

    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 15000 });
    await page.waitForFunction(() => {
      const s = document.getElementById("ctx-select");
      return !!(s && !s.hidden && s.options.length >= 2);
    }, null, { timeout: 15000 });

    const oldValue = `${roots.program}|${built.s1}`;
    const newValue = `${roots.program}|${built.s2}`;
    const selectValues = await page.locator("#ctx-select option")
      .evaluateAll((os) => os.map((o) => o.value));
    if (!selectValues.includes(oldValue) || !selectValues.includes(newValue)) {
      throw new Error(`[${L}] both Seasons must be real switcher options; got `
        + JSON.stringify(selectValues));
    }

    const heldPath = `/api/v2/setup/seasons/${built.s1}/${leg.kind}`;
    // THE CONTROL, installed before anything is dispatched: strip
    // `X-Context-Epoch` off the held read IN FLIGHT, leaving the gate, the
    // ceiling, the park, the switch and every other request untouched. Scoped
    // to that exact path so the post-switch render's own reads still carry
    // theirs and the leg fails for one reason only.
    if (isControl) {
      await page.route(`**${heldPath}`, async (route, request) => {
        const headers = { ...(await request.allHeaders()) };
        // Recorded HERE, inside the interception, and not from the `request`
        // event: that event reports the headers the PAGE set, before any route
        // override, so it can neither confirm nor deny that the strip happened.
        // This is the only place both facts are visible at once — what the read
        // carried, and that we removed it.
        strippedEpochs.push(headers[EPOCH_HEADER] || "");
        delete headers[EPOCH_HEADER];
        return route.continue({ headers });
      });
    }

    // The Setup hierarchy surface is what issues the two scoped reads. Paint it
    // once unheld, so the baseline below is a real one and the held pass is a
    // RE-render rather than a first load.
    await page.click('.tab[data-tab="setup"]');
    await page.click('[data-setup-view="hierarchy"]');
    await page.waitForSelector(`#va-add-${built.s1}`, { timeout: 20000 });
    step("Setup hierarchy painted on Season 1");

    // The epoch the page is rendering UNDER, taken from the server before
    // anything moves. Leg (E) compares the held read's header against this.
    const epochBefore = (await apiGet(page, "/api/context")).json.context_epoch;
    if (!epochBefore || !/^[0-9a-f]{32}$/.test(epochBefore)) {
      fail(`[${L}] (E) /api/context carried no usable epoch before the switch `
        + `(${JSON.stringify(epochBefore)}), so no read could echo one`);
    }

    // ---- open the window, arm the park, and get the read dispatched -----
    //
    // `switch`/`control` park the read FIRST and switch afterwards: the read is
    // the one the operator's switch is about to cancel.
    //
    // `held-post` inverts it. The switch goes first and is HELD in the network,
    // so `cancelContextScopedReads()` has already run and its abort can never
    // reach what comes next; only THEN is the park armed and a render forced,
    // enrolling a read the client has no way to withdraw. The POST is released
    // below, and commits while that read is parked.
    if (isHeldPost) {
      const postsBeforeHold = switchPosts.length;
      holdNextPost = true;             // ...and only now, see the note above
      await page.selectOption("#ctx-select", newValue);
      await waitFor(() => switchPosts.length > postsBeforeHold, 20000,
        () => `[${L}] the switch's POST never reached the network, so there is `
          + `no pending-switch window to enrol a read in`);
      await control(controlPort, "POST", "/arm-late", { path: heldPath });
      // Fire-and-forget: this render() will BLOCK on the parked read, so
      // awaiting it inside the page would deadlock the leg against itself.
      await page.evaluate(() => { render(); return true; }).catch(() => {});
    } else {
      await control(controlPort, "POST", "/arm-late", { path: heldPath });
      await page.click('[data-setup-view="hierarchy"]');
    }

    // (A) DISPATCHED, NOT YET ARRIVED — two independent observations.
    const parked = await waitFor(
      async () => {
        const s = await control(controlPort, "GET", "/status");
        return s.late_parked ? s : null;
      }, 20000,
      () => `[${L}] (A) the ${leg.kind} read never reached do_GET, so there is `
        + `no late arrival to run`);
    if (parked.late_exited) {
      throw new Error(`[${L}] (A) the parked read had already left do_GET`);
    }
    const enrolled = await page.evaluate((want) => {
      if (typeof contextScopedReadsInFlight === "undefined") return null;
      return Array.from(contextScopedReadsInFlight, (e) => ({ url: e.url }))
        .filter((e) => e.url.indexOf(want) >= 0);
    }, `/seasons/${built.s1}/${leg.kind}`).catch(() => null);
    if (!enrolled || !enrolled.length) {
      fail(`[${L}] (A) the held ${leg.kind} read is not enrolled in the app's `
        + `own barrier, so this leg is holding something the app does not `
        + `consider a scoped read: ${JSON.stringify(enrolled)}`);
    }
    step("(A) read is dispatched and held ABOVE the gate");

    // ---- THE REAL SWITCH, through the shipped control -------------------
    if (isHeldPost) {
      // Already sent and held above; let it go now that the unwithdrawable
      // read exists and is parked.
      releaseHeldPost();
    } else {
      const postsBefore = switchPosts.length;
      await page.selectOption("#ctx-select", newValue);
      await waitFor(() => switchPosts.length > postsBefore, 20000,
        () => `[${L}] (B) the switch's POST never reached the network`);
    }

    // (B) THE SWITCH GOES FIRST AND COMPLETES, while the read is still held.
    //     That is what makes this the LATE-ARRIVAL interleaving rather than
    //     #415's: the writer registers, commits and leaves before the read is
    //     ever seen by the gate.
    const after = await waitFor(
      async () => {
        const seen = await apiGet(page, "/api/context");
        return (seen.status === 200 && seen.json
                && seen.json.season_id === built.s2) ? seen.json : null;
      }, 20000,
      () => `[${L}] (B) the switch never committed while the read was held`);
    const stillHeld = await control(controlPort, "GET", "/status");
    if (!stillHeld.late_parked || stillHeld.late_exited) {
      fail(`[${L}] (B) the read was no longer held when the switch committed `
        + `(${JSON.stringify(stillHeld)}); this leg measured nothing`);
    }
    step("(B) the switch committed with the read still in the queue");

    // (E) THE EPOCH TRAVELLED, AND IS NOW STALE — both halves, off the wire.
    const heldSent = scopedReadRequests.filter((r) => r.url === heldPath);
    if (!heldSent.length) {
      fail(`[${L}] (E) the held ${leg.kind} request was never observed on the `
        + `wire at all`);
    } else if (isControl) {
      // NON-VACUITY FOR THE CONTROL. It must have removed something real: a
      // build that simply stopped sending the header would otherwise "pass"
      // this leg while proving nothing, and would take the other legs' 204s
      // down with it -- but silently, and here rather than loudly.
      if (!strippedEpochs.some((e) => e === epochBefore)) {
        fail(`[${L}] (E/CONTROL) the interception removed `
          + `${JSON.stringify(strippedEpochs)}, which does not include the `
          + `epoch the page was rendered under (${epochBefore}); the control is `
          + `not controlling anything and the 404 below means nothing`);
      }
    } else if (!heldSent.some((r) => r.epoch === epochBefore)) {
      fail(`[${L}] (E) the held ${leg.kind} request carried `
        + `${JSON.stringify(heldSent.map((r) => r.epoch))} rather than the `
        + `epoch the page was rendered under (${epochBefore}); the read and `
        + `the render do not refer to the same selection`);
    }
    if (after.context_epoch === epochBefore) {
      fail(`[${L}] (E) the epoch did not move across the switch `
        + `(${epochBefore}), so nothing could ever be recognised as stale`);
    }

    // (F) THE CLIENT ADOPTED THE NEW EPOCH, so the NEXT render's reads echo the
    //     tuple they are actually rendered under. Polled, because the adoption
    //     happens in the switch's own reconciliation rather than at the instant
    //     /api/context answers.
    //
    //     SKIPPED for `held-post`, and that is not a gap: that leg's switch is
    //     still mid-reconciliation (its own render is blocked on the parked
    //     read), so the adoption has not happened YET and asserting it here
    //     would be asserting an ordering the app never promised. The property
    //     is proven by the other two modes on both viewports.
    if (!isHeldPost) {
      await waitFor(
        async () => (await page.evaluate(() =>
          (typeof contextEpoch === "undefined" ? null : contextEpoch)))
          === after.context_epoch,
        20000,
        () => `[${L}] (F) the page never adopted the new epoch; its reads would `
          + `go on echoing the selection the operator has left`);
    }
    step("(E)(F) the epoch travelled, moved, and was adopted");

    // ---- release, and let the late arrival play out ---------------------
    await control(controlPort, "POST", "/release-late");
    releasedLate = true;
    const done = await waitFor(
      async () => {
        const s = await control(controlPort, "GET", "/status");
        return s.late_exited ? s : null;
      }, 20000,
      () => `[${L}] (C) the late-arriving read never left do_GET`);

    // (C) DISCARDED, NOT REFUSED — read from inside the server, because the
    //     client aborted this fetch and may have taken the socket with it.
    if (isControl) {
      // THE FALSIFIER. Same seam, same interleaving, epoch stripped.
      if (done.late_status !== 404) {
        fail(`[${L}] (C/CONTROL) with the epoch stripped the late arrival `
          + `answered ${done.late_status}, not the 404 the ceiling owes it. This `
          + `journey is not measuring the epoch: something is discarding or `
          + `admitting late reads regardless of what the read carried.`);
      }
      if (!done.late_service_called) {
        fail(`[${L}] (C/CONTROL) the un-epoched late read never reached `
          + `ApiService, so its 404 did not come from the ceiling`);
      }
      allow("GET", heldPath, 404);
    } else {
      if (done.late_status !== 204) {
        fail(`[${L}] (C) the late-arriving ${leg.kind} read answered `
          + `${done.late_status}, not 204. A read the operator had already `
          + `superseded was evaluated as an independent post-switch read — this `
          + `is the CI failure.`);
      }
      if (done.late_service_called) {
        fail(`[${L}] (C) the discarded read reached ApiService, so the exact-`
          + `Season ceiling WAS evaluated for it; a discard must short-circuit `
          + `in front of the ceiling, not after it`);
      }
    }
    step(`(C) late arrival answered ${done.late_status}`);

    // (S) THE CEILING IS UNWEAKENED — direct server control, no client race.
    //     These raw fetches carry no epoch, which is the point: the refusal
    //     they get is the one main gives, unchanged.
    const ghost = "season_does_not_exist_999999";
    for (const kind of ["venue-candidates", "venue-access"]) {
      const siblingUrl = `/api/v2/setup/seasons/${built.s1}/${kind}`;
      const ghostUrl = `/api/v2/setup/seasons/${ghost}/${kind}`;
      const sibling = await apiGet(page, siblingUrl);
      const missing = await apiGet(page, ghostUrl);
      allow("GET", siblingUrl, 404);
      allow("GET", ghostUrl, 404);
      if (sibling.status !== 404) {
        fail(`[${L}] (S) the unselected SIBLING Season's ${kind} answered `
          + `${sibling.status} — the exact-Season ceiling has been weakened`);
      }
      if (missing.status !== 404) {
        fail(`[${L}] (S) a nonexistent Season's ${kind} answered ${missing.status}`);
      }
      if (JSON.stringify(sibling.json).replace(built.s1, "<S>")
          !== JSON.stringify(missing.json).replace(ghost, "<S>")) {
        fail(`[${L}] (S) the sibling and nonexistent refusals differ, so the `
          + `refusal is an existence oracle: `
          + `${JSON.stringify(sibling.json)} vs ${JSON.stringify(missing.json)}`);
      }
    }
    step("(S) the ceiling still refuses the unselected Season");

    // (D) THE LEDGER, RECONCILED ONCE, AT THE END.
    const matchAllowance = (method, url, status) => {
      const hit = allowances.find((a) => !a.matched && a.method === method
        && a.url === rel(url) && a.status === status);
      if (hit) { hit.matched = true; return true; }
      return false;
    };
    responses
      .filter((r) => r.status < 200 || r.status > 299)
      .forEach((r) => {
        if (matchAllowance(r.method, r.url, r.status)) return;
        fail(`[${L}] (D) unexpected ${r.status} on ${r.method} ${r.url}`);
      });
    consoleErrors.forEach((c) => {
      if (allowances.some((a) => a.url === rel(c.url))) return;
      fail(`[${L}] (D) console error: ${c.text}${c.url ? ` @ ${c.url}` : ""}`);
    });
    const scopedFailures = responses.filter(
      (r) => SCOPED_READ_RE.test(r.url) && r.status >= 400
             && !allowances.some((a) => a.url === rel(r.url)));
    if (scopedFailures.length) {
      fail(`[${L}] (D) ${scopedFailures.length} scoped-read failure(s) not `
        + `declared: ${JSON.stringify(scopedFailures)}`);
    }
    // NON-VACUITY: the app must really have recorded this read as cancelled.
    const aborts = await page.evaluate(() =>
      (typeof contextScopedReadAborts === "undefined"
        ? [] : contextScopedReadAborts.slice()));
    if (!aborts.some((a) => rel(a.url) === heldPath)) {
      fail(`[${L}] (D) the app recorded NO intentional abort for the held `
        + `${leg.kind} read, so the reconciliation above proves nothing: `
        + JSON.stringify(aborts));
    }
    // THE CLIENT'S OWN 204 BRANCH, asserted where — and ONLY where — it is
    // actually reachable. In `switch`/`control` the client ABORTED this fetch,
    // so Chromium tore the request down and no response of any kind came back
    // to the page: the abort above is classified by the `catch`, `discarded` is
    // false, and requiring otherwise would be requiring the impossible. In
    // `held-post` the read was enrolled after the cancellation, was never
    // aborted, and therefore DOES see the 204 — so the ledger must say so, and
    // must say so with the discard flag rather than as a plain abort, because
    // that is what stops an empty 2xx from being painted as "no venues".
    if (isHeldPost
        && !aborts.some((a) => rel(a.url) === heldPath && a.discarded)) {
      fail(`[${L}] (D) the app never recorded the held ${leg.kind} read as `
        + `DISCARDED, so the 204 the server sent was not recognised as one by `
        + `the page: ${JSON.stringify(aborts)}`);
    }
    if (!isHeldPost
        && aborts.some((a) => rel(a.url) === heldPath && a.discarded)) {
      fail(`[${L}] (D) the app claims it SAW a 204 for a read it had aborted, `
        + `which Chromium cannot deliver — the ledger is describing something `
        + `that did not happen: ${JSON.stringify(aborts)}`);
    }
    step("ledger reconciled");
  } finally {
    // A leg that threw between holding the POST and releasing it would leave a
    // route handler awaiting forever and the page waiting on a request that
    // never goes out — turning one assertion failure into a hang.
    releaseHeldPost();
    if (!releasedLate) {
      await control(controlPort, "POST", "/reset").catch(() => {});
    }
    await page.close().catch(() => {});
    await context.close().catch(() => {});
    await stopServer(server);
  }

  if (errors.length) {
    throw new Error(`${errors.join("\n")}\n--- server output ---\n`
      + serverOutput.slice(-4000));
  }
  console.log(`[${L}] OK`);
}

(async () => {
  const browser = await chromium.launch();
  try {
    for (const viewport of VIEWPORTS) {
      for (let i = 0; i < LEGS.length; i += 1) {
        await checkLeg(browser, viewport, LEGS[i], i);
      }
    }
  } finally {
    await browser.close();
  }
  console.log("context-switch-late-arrival: OK");
})().catch((e) => {
  console.error(e && e.message ? e.message : e);
  process.exit(1);
});
