// Cross-identity withdrawal of an UNACKNOWLEDGED context intent (#369 review).
//
// THE BUG THIS PINS
// -----------------
// #369 made sendContextSwitch() publish the operator's INTENDED
// Program/Season/League into location.hash BEFORE its POST /api/context goes
// out, so the hash can never lag a server that has already been mutated
// (context-switcher.js's checkBootWindow covers that half). The flip side is
// that an unacknowledged switch now leaves the DEPARTING identity's raw intent
// sitting in the URL, and the URL survives an identity change: there is no
// page load, so the next identity's signIn() runs restoreContextDeepLink()
// against whatever hash the previous identity left behind.
//
// resetTransientUiState() is where an identity change withdraws that phantom.
// The first version of that guard keyed off `contextSwitchInFlight`, and that
// flag is the WRONG window. sendContextSwitch() clears it the instant the POST
// resolves:
//
//     const r = await post("/api/context", {...});
//     contextSwitchInFlight = false;              // <-- already false here
//     ...
//     if (!r || r.error) {
//       toast = "That Program/Season/League isn't available.";
//       await loadContextOptions();               // <-- and the hash still
//       syncContextHash();                        //     advertises the
//     }                                           //     REJECTED intent
//
// On the FAILURE path the rejected intent therefore stays in the hash across
// that awaited reconciliation, with `contextSwitchInFlight === false` the whole
// time. An identity change landing in that window found the flag already false,
// left the phantom in the URL, and handed the NEXT identity a choice the server
// had explicitly refused for the previous one. If the arriving identity happens
// to be authorized for it, restoreContextDeepLink() POSTs it and it is silently
// persisted as the new identity's context -- a cross-identity leak carried by
// the URL, with no user action asking for it.
//
// The production fix (app.js) tracks that window with its own flag,
// `contextHashIntentPending`: set the moment intent is published into the hash,
// cleared only after a syncContextHash() has reconciled the URL with canonical
// truth, on EVERY success and failure path. resetTransientUiState() keys off
// THAT and rewinds the hash. This file is that fix's regression.
//
// TECHNIQUE
// ---------
//   * The rejected POST is fulfilled DIRECTLY with the backend's own generic
//     not-found shape (404 {"error":{"code":"not_found",...}}) -- route.fetch()
//     is deliberately NOT called for it, so the server genuinely never takes the
//     switch and "rejected" means rejected.
//   * The failure path's trailing GET /api/context/options is held by capturing
//     the REAL response first (route.fetch()) and only then delaying DELIVERY
//     until this file releases it. Delaying the REQUEST instead would merely
//     resolve it against current state and prove nothing -- the whole point is
//     that the browser is parked mid-reconciliation with the phantom live in
//     the URL.
//   * The fixture has TWO Programs each with their own Season, so A's rejected
//     intent (Program B / Season B) is different from the canonical selection
//     (Program A / Season A) on BOTH axes. A single-Program fixture cannot tell
//     "the phantom was withdrawn" from "the phantom happened to equal canonical"
//     and proves nothing.
//   * Every hash assertion decodes the Base64URL payload and compares the
//     {p, s, l} triple. The hash is versioned (v1 links are still honored, v2
//     adds the League axis), so an assertion on the encoded STRING would start
//     passing/failing on a version bump rather than on the selection it is
//     actually about.
//   * The negative is asserted DIRECTLY, not just inferred from the end state:
//     every POST /api/context request body is captured off page.on("request"),
//     and none issued after the identity change may carry A's rejected target.
//
// SHAPE OF EACH LEG
// -----------------
//   1. Identity A and identity C are both parked on the SAME server-confirmed
//      context (Program A / Season A). That is deliberate: inheriting a
//      CONFIRMED context across a persona switch is pre-existing, intended
//      deep-link behavior, so making A's confirmed context equal C's canonical
//      one removes that legitimate inheritance from the picture entirely --
//      any movement to Program B / Season B afterwards is unambiguously the
//      phantom, never the documented inheritance.
//   2. A selects Program B / Season B in the switcher. Its POST is rejected.
//   3. A's failure-path options GET is held. The window is now open, and this
//      file ASSERTS it is open (the hash really does advertise the refused
//      intent, and the server really is still on Program A / Season A) rather
//      than assuming it.
//   4. The identity changes to C mid-window -- once via the in-app persona
//      switch (#role-switch), once via a real sign-out through #signout-btn
//      followed by a real sign-in through the login screen's persona button.
//      Never via this file's raw-fetch helpers, which bypass
//      signIn()/setUser()/resetTransientUiState() and would prove nothing.
//   5. C must send no POST /api/context carrying A's rejected target, and C's
//      persisted context, URL hash and switcher must all still read Program A /
//      Season A.
//   6. A's held reconciliation is then RELEASED and must produce no late hash,
//      DOM or server change.
//
// A note on the sign-out/sign-in leg, recorded so a future reader does not
// mistake it for redundant coverage or for the leg that pins the fix: the
// #signout-btn handler ALSO drops a "#ctx=" hash with the session, so on that
// path the phantom is withdrawn twice over. Measured, not assumed -- with
// resetTransientUiState() reverted to the old contextSwitchInFlight guard (and
// again with its rewind deleted outright), the two PERSONA legs fail at both
// viewports while the two SIGN-OUT legs still pass. The sign-out leg is kept
// because it is a real identity transition that must be covered, and because it
// pins the departing-side invariant directly (step 4a below asserts the URL no
// longer advertises the refused intent while signed OUT, before any new identity
// exists to adopt it): the two mechanisms are independent, and if either is
// dropped later the remaining one must still hold. This leg is what says so.
//
// Runs at desktop AND 390px, with zero console/page errors.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");
const {
  installContextFixture, selectProgram, selectProgramSeason,
} = require("./context-fixture.js");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
// Two transitions x two viewports, each on its own server/port so a leg can
// never observe another leg's persisted context.
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, personaPort: 8701, signoutPort: 8703 },
  { label: "phone", width: 390, height: 844, personaPort: 8702, signoutPort: 8704 },
];
// A: the departing identity whose switch is refused. C: the arriving identity,
// a global Viewer and therefore genuinely authorized for A's rejected target --
// which is exactly what makes the leak silent rather than self-correcting.
const IDENTITY_A = "viewer";
const IDENTITY_C = "swapper";
// Comfortably longer than any real local round trip; this journey installs no
// blanket artificial latency (only the two deterministic, targeted holds
// above), so a late write would land well inside this.
const SETTLE_MS = 800;

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

function startServer(port, env) {
  return spawn(
    process.env.PYTHON || "python3",
    ["-u", "-m", "hockey_scheduler.web.server", "--host", HOST, "--port", String(port)],
    { cwd: BACKEND_DIR, stdio: ["ignore", "pipe", "pipe"], env: { ...process.env, ...env } });
}

// Same-origin fetches run inside the page (carry the session cookie).
function apiPost(page, url, body) {
  return page.evaluate(async ([u, b]) => {
    const r = await fetch(u, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(b || {}),
    });
    return { status: r.status, json: await r.json().catch(() => ({})) };
  }, [url, body]);
}
function apiGet(page, url) {
  return page.evaluate(async (u) => {
    const r = await fetch(u, { credentials: "same-origin" });
    return { status: r.status, json: await r.json().catch(() => ({})) };
  }, url);
}
const loginAs = (page, username) => apiPost(page, "/api/auth/login", { username, password: "demo" });

async function withTimeout(promise, ms, label) {
  let timer;
  try {
    return await Promise.race([
      promise,
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error(`timed out waiting for: ${label}`)), ms);
      }),
    ]);
  } finally { clearTimeout(timer); }
}

// Labeled, diagnosable wrapper around page.waitForFunction -- a bare timeout
// says only "Timeout 10000ms exceeded" with no indication of which assertion it
// was (the same reason context-switcher.js grew this helper).
async function waitFor(page, label, fn, arg, timeoutMs = 10000) {
  try {
    await page.waitForFunction(fn, arg, { timeout: timeoutMs });
  } catch (error) {
    throw new Error(`timed out waiting for: ${label}\n${error.message}`);
  }
}

// The DECODED "#ctx=" payload, never the raw string -- see the header note on
// the versioned hash. Returns { raw, decoded } so a failure can print both.
function readCtxHash(page) {
  return page.evaluate(() => {
    const h = location.hash || "";
    if (h.indexOf("#ctx=") !== 0) return { raw: h, decoded: null };
    try {
      let b = h.slice(5).replace(/-/g, "+").replace(/_/g, "/");
      while (b.length % 4) b += "=";
      const o = JSON.parse(atob(b));
      return { raw: h, decoded: { p: o.p || null, s: o.s || null, l: o.l || null } };
    } catch (_) { return { raw: h, decoded: null }; }
  });
}

const sameCtx = (a, b) => !!a && !!b
  && (a.p || null) === (b.p || null)
  && (a.s || null) === (b.s || null)
  && (a.l || null) === (b.l || null);

// Does a captured POST /api/context body target this exact triple?
const bodyTargets = (body, want) => !!body && typeof body === "object"
  && (body.program_id || null) === (want.p || null)
  && (body.season_id || null) === (want.s || null)
  && (body.league_id || null) === (want.l || null);

async function newPage(browser, viewport) {
  const context = await browser.newContext({
    viewport: { width: viewport.width, height: viewport.height },
  });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(`[pageerror] ${e.message}`));
  page.on("console", (m) => {
    if (m.type() === "error" && !/Failed to load resource/.test(m.text())) {
      errors.push(`[console] ${m.text()}`);
    }
  });
  // The direct negative evidence: every POST /api/context body, in order, so
  // "the arriving identity never re-sent the departing identity's refused
  // intent" is asserted on the wire rather than inferred from the end state.
  const ctxPosts = [];
  page.on("request", (req) => {
    if (req.method() !== "POST") return;
    const rel = req.url().replace(/^https?:\/\/[^/]+/, "");
    if (!/^\/api\/context(\?|$)/.test(rel)) return;
    let body = null;
    try { body = JSON.parse(req.postData() || "null"); } catch (_) { body = req.postData(); }
    ctxPosts.push(body);
  });
  return { context, page, errors, ctxPosts };
}

async function reloadShell(page) {
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForSelector("#content > *", { timeout: 10000 });
}

// --- the regression -------------------------------------------------------
//
// `transition` is "persona" (the in-app #role-switch dropdown) or "signout"
// (a real sign-out through #signout-btn followed by a real sign-in through the
// login screen). Both are no-reload identity changes that route through
// setUser() -> resetTransientUiState().
async function checkWithdrawal(browser, viewport, transition) {
  const port = transition === "persona" ? viewport.personaPort : viewport.signoutPort;
  const base = `http://${HOST}:${port}`;
  const server = startServer(port, {});
  let out = "";
  server.stdout.on("data", (d) => { out += d.toString(); });
  server.stderr.on("data", (d) => { out += d.toString(); });
  const { context, page, errors, ctxPosts } = await newPage(browser, viewport);
  const L = `${viewport.label}/${transition}`;
  let releaseHeld = () => {};
  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });

    // -- fixture: TWO Programs, each with its own Season, plus a second global
    //    Viewer account. Setup that needs manage_setup runs as the League
    //    Admin; the switcher itself is then exercised as a Viewer, which is
    //    authorized for every Program/Season but does NOT trigger the
    //    League-Admin onboarding wizard that takes over #content.
    if ((await loginAs(page, "admin")).status !== 200) throw new Error(`[${L}] admin login failed`);
    if ((await apiPost(page, "/api/demo/load", {})).status !== 200) throw new Error(`[${L}] demo load failed`);
    const seeded = (await apiGet(page, "/api/context/options")).json;
    if (!seeded || !seeded.programs || !seeded.programs.length || !seeded.programs[0].seasons.length) {
      throw new Error(`[${L}] demo fixture did not seed a Program with a Season: ${JSON.stringify(seeded)}`);
    }
    const progA = seeded.programs[0].id;
    const seasonA = seeded.programs[0].seasons[0].id;
    const nameA = `${seeded.programs[0].name} / ${seeded.programs[0].seasons[0].name}`;

    const createdProgram = await apiPost(page, "/api/v2/setup/program", { name: "Rival Program" });
    if (createdProgram.status !== 200 || !createdProgram.json.id) {
      throw new Error(`[${L}] second Program create failed: ${JSON.stringify(createdProgram.json)}`);
    }
    const progB = createdProgram.json.id;
    // #409 EXPLICIT SELECTION. This is the journey the brief calls the
    // sharpest instance of the whole boundary: the demo load above left the
    // saved row on Program A, so a Season create naming Program B is
    // PROGRAM-AXIS against the WRONG saved Program and is refused --
    // "Select a Program before creating records in it." The first Season in
    // this install succeeded only because a persona that had selected made
    // it; the second did not. That difference is precisely the inference
    // #409 removes, so the selection is stated here and read back, and the
    // journey stops depending on which account happened to mint what.
    //
    // Placed AFTER the shell is live so it runs through `setActiveContext`,
    // the same pipeline #ctx-select uses -- this journey goes on to assert
    // what the real switcher does, and a raw POST would desync the client it
    // is about to inspect. It is also well before every `ctxPosts.slice(N)`
    // marker below, so it cannot be mistaken for one of the switches under
    // test.
    await reloadShell(page);
    await installContextFixture(page);
    await selectProgram(page, `[${L}] select Rival Program before its Season`, progB);
    const createdSeason = await apiPost(page, "/api/v2/setup/season",
      { program_id: progB, name: "Rival Cup" });
    if (createdSeason.status !== 200 || !createdSeason.json.id) {
      throw new Error(`[${L}] second Season create failed: ${JSON.stringify(createdSeason.json)}`);
    }
    const seasonB = createdSeason.json.id;
    const nameB = "Rival Program / Rival Cup";
    if (progA === progB || seasonA === seasonB) {
      throw new Error(`[${L}] the two contexts must be genuinely distinguishable `
        + `(progA=${progA} progB=${progB} seasonA=${seasonA} seasonB=${seasonB})`);
    }
    if ((await apiPost(page, "/api/accounts",
      { username: IDENTITY_C, password: "demo", role: "viewer" })).status !== 200) {
      throw new Error(`[${L}] create ${IDENTITY_C} account failed`);
    }

    // Human-readable rendering for every failure message below: ids alone make
    // "which context leaked?" unreadable, which is the whole reason this
    // fixture has two of everything.
    const CANONICAL = { p: progA, s: seasonA, l: null };
    const REJECTED = { p: progB, s: seasonB, l: null };
    const describe = (c) => {
      if (!c) return "no #ctx= payload";
      if (sameCtx(c, CANONICAL)) return `${nameA} (the canonical selection)`;
      if (sameCtx(c, REJECTED)) return `${nameB} (A's REJECTED intent)`;
      return `an unexpected context ${JSON.stringify(c)}`;
    };
    const serverCtx = async () => {
      const r = (await apiGet(page, "/api/context")).json || {};
      return { p: r.program_id || null, s: r.season_id || null, l: r.league_id || null };
    };

    // -- both identities parked on the SAME server-confirmed context, so the
    //    intended cross-identity inheritance of a CONFIRMED context is a no-op
    //    here and cannot be mistaken for the phantom (see header, step 1).
    if ((await loginAs(page, IDENTITY_C)).status !== 200) throw new Error(`[${L}] ${IDENTITY_C} login failed`);
    if ((await apiPost(page, "/api/context",
      { program_id: progA, season_id: seasonA })).status !== 200) {
      throw new Error(`[${L}] seeding ${IDENTITY_C}'s canonical context failed`);
    }
    if ((await loginAs(page, IDENTITY_A)).status !== 200) throw new Error(`[${L}] ${IDENTITY_A} login failed`);
    // A's canonical seed goes through `setActiveContext`, not a raw
    // `POST /api/context` (#409). It has to: the fixture above selected Rival
    // Program in order to create its Season, and THAT selection also wrote
    // `#ctx=` into the URL. A raw seed would move only the server, leaving
    // the stale Rival hash in place — and this app deliberately lets a
    // deep-link hash win on reload, so the very next `reloadShell` would
    // re-adopt Rival and the "deterministic starting point" below would find
    // the switcher sitting on the wrong context. Driving the app's own
    // pipeline moves the hash, the client and the server together, which is
    // exactly why ./context-fixture.js insists on it.
    await selectProgramSeason(page,
      `[${L}] seeding ${IDENTITY_A}'s canonical context`, progA, seasonA);
    await reloadShell(page);

    // -- deterministic starting point: A's switcher, hash and server all agree
    //    on the canonical selection, and Program B / Season B is genuinely
    //    offered to A (so the switch below is a real user action, not a
    //    synthesized request).
    const canonicalValue = `${progA}|${seasonA}`;
    const rejectedValue = `${progB}|${seasonB}`;
    await waitFor(page, `[${L}] A's switcher to offer both contexts and sit on the canonical one`,
      (v) => {
        const s = document.getElementById("ctx-select");
        return !!s && !s.hidden && s.value === v.canonical
          && Array.from(s.options).some((o) => o.value === v.rejected);
      }, { canonical: canonicalValue, rejected: rejectedValue });
    {
      const { decoded, raw } = await readCtxHash(page);
      if (!sameCtx(decoded, CANONICAL)) {
        throw new Error(`[${L}] starting hash must encode ${nameA}, got ${describe(decoded)} (${raw})`);
      }
    }
    if (!sameCtx(await serverCtx(), CANONICAL)) {
      throw new Error(`[${L}] starting server context is not ${nameA}`);
    }
    await waitFor(page, `[${L}] the ${IDENTITY_C} option to be offered by #role-switch`,
      (u) => {
        const s = document.getElementById("role-switch");
        return !!s && Array.from(s.options).some((o) => o.value === u);
      }, IDENTITY_C);

    // -- arm the race -----------------------------------------------------
    // (i) The switcher's POST is REJECTED, once, with the backend's own generic
    //     not-found shape. route.fetch() is deliberately not called: the server
    //     must genuinely never take this switch. Every later POST /api/context
    //     falls through to the real server, so a leaked re-POST by the arriving
    //     identity really does persist and is caught at the server, not merely
    //     at the DOM.
    // (ii) The failure path's trailing GET /api/context/options is held:
    //     the real response is captured first, then DELIVERY is delayed until
    //     this file releases it, parking the app mid-reconciliation with the
    //     phantom live in the URL.
    // Playwright routes are LIFO and these two patterns are disjoint
    // (/api/context$ never matches /api/context/options), so each fully owns
    // its own endpoint from here on.
    let holdArmed = false;
    let markHeld = () => {};
    const heldStarted = new Promise((resolve) => { markHeld = resolve; });
    const heldGate = new Promise((resolve) => { releaseHeld = resolve; });
    await page.route(/\/api\/context\/options(\?|$)/, async (route) => {
      if (!holdArmed) return route.fallback();
      holdArmed = false;
      const response = await route.fetch();   // the real read completes HERE...
      markHeld();
      await heldGate;
      await route.fulfill({ response });      // ...the browser only learns of it HERE
    });
    let rejectedOnce = false;
    await page.route(/\/api\/context(\?|$)/, async (route) => {
      const request = route.request();
      if (request.method() !== "POST") return route.fallback();
      let body = null;
      try { body = JSON.parse(request.postData() || "null"); } catch (_) { body = null; }
      if (rejectedOnce || !bodyTargets(body, REJECTED)) return route.fallback();
      rejectedOnce = true;
      holdArmed = true;   // the very next options GET is the failure path's own
      await route.fulfill({
        status: 404,
        contentType: "application/json",
        body: JSON.stringify({ error: { code: "not_found",
          message: "Season not found or not accessible.",
          details: { reason: "season_not_accessible" } } }),
      });
    });

    // -- A selects Program B / Season B; the POST is refused ----------------
    const postsBeforeSwitch = ctxPosts.length;
    await page.selectOption("#ctx-select", rejectedValue);
    await withTimeout(heldStarted, 15000,
      `[${L}] A's failure-path GET /api/context/options to be captured and held `
      + `(the rejected POST never produced one)`);

    // -- the window is now OPEN, and this asserts it rather than assuming it --
    if (!ctxPosts.slice(postsBeforeSwitch).some((b) => bodyTargets(b, REJECTED))) {
      throw new Error(`[${L}] the switcher never POSTed ${nameB}, so nothing was rejected — `
        + `this harness proved nothing. Bodies seen: `
        + `${JSON.stringify(ctxPosts.slice(postsBeforeSwitch))}`);
    }
    const inWindowHash = await readCtxHash(page);
    if (!sameCtx(inWindowHash.decoded, REJECTED)) {
      throw new Error(`[${L}] the phantom window never opened: with A's rejected POST answered `
        + `and its reconciliation held, the URL should still advertise ${nameB}, but it reads `
        + `${describe(inWindowHash.decoded)} (${inWindowHash.raw}) — nothing was left for the `
        + `next identity to adopt, so this run cannot demonstrate the withdrawal`);
    }
    if (!sameCtx(await serverCtx(), CANONICAL)) {
      throw new Error(`[${L}] the refused switch mutated the server — it must not; `
        + `A's context should still be ${nameA}`);
    }

    // -- the identity changes to C, mid-window ------------------------------
    const postsBeforeIdentityChange = ctxPosts.length;
    if (transition === "persona") {
      await page.selectOption("#role-switch", IDENTITY_C);
    } else {
      await page.click("#signout-btn");
      await page.waitForSelector(`[data-persona="${IDENTITY_C}"]`,
        { state: "visible", timeout: 10000 });
      // (4a) The departing-side invariant, asserted while NO identity holds the
      // session: whatever mechanism withdrew it, the URL must no longer carry
      // the refused intent before a new identity exists to adopt it.
      const signedOutHash = await readCtxHash(page);
      if (signedOutHash.decoded && sameCtx(signedOutHash.decoded, REJECTED)) {
        throw new Error(`[${L}] after sign-out the URL still advertises ${nameB} `
          + `(${signedOutHash.raw}) — the departing identity's REFUSED intent survived the `
          + `identity boundary and is waiting for the next sign-in to adopt it`);
      }
      await page.click(`[data-persona="${IDENTITY_C}"]`);
    }
    await waitFor(page, `[${L}] the ${IDENTITY_C} sign-in to complete (#user-name)`,
      (u) => (document.getElementById("user-name") || {}).textContent === u, IDENTITY_C);
    await waitFor(page, `[${L}] ${IDENTITY_C}'s switcher to finish painting`,
      () => {
        const s = document.getElementById("ctx-select");
        return !!s && !s.hidden && s.options.length >= 2;
      }, null);

    // -- the assertions ----------------------------------------------------
    const leaked = ctxPosts.slice(postsBeforeIdentityChange)
      .filter((b) => bodyTargets(b, REJECTED));
    if (leaked.length) {
      throw new Error(`[${L}] ${IDENTITY_C} re-sent ${IDENTITY_A}'s REJECTED intent as its own: `
        + `POST /api/context ${JSON.stringify(leaked[0])} after the identity change. The phantom `
        + `${nameB} was left in the URL across the identity boundary and `
        + `restoreContextDeepLink() adopted it as a deep link — ${IDENTITY_C} is authorized for `
        + `it, so the server silently persisted a context the server had just REFUSED for `
        + `${IDENTITY_A}`);
    }
    const strays = ctxPosts.slice(postsBeforeIdentityChange);
    if (strays.length) {
      throw new Error(`[${L}] ${IDENTITY_C} sent an unexpected POST /api/context after the `
        + `identity change: ${JSON.stringify(strays)}. Its canonical context already equals the `
        + `withdrawn-to context, so boot had nothing to reconcile — the hash was rewound to `
        + `something other than canonical truth`);
    }
    const afterCtx = await serverCtx();
    if (!sameCtx(afterCtx, CANONICAL)) {
      throw new Error(`[${L}] ${IDENTITY_C}'s PERSISTED context is ${describe(afterCtx)} instead of `
        + `${nameA} — the departing identity's refused choice was silently persisted under the `
        + `new session`);
    }
    const afterHash = await readCtxHash(page);
    if (!sameCtx(afterHash.decoded, CANONICAL)) {
      throw new Error(`[${L}] ${IDENTITY_C}'s URL encodes ${describe(afterHash.decoded)} `
        + `(${afterHash.raw}) instead of ${nameA} — the URL still carries the previous `
        + `identity's unacknowledged intent`);
    }
    await waitFor(page, `[${L}] ${IDENTITY_C}'s switcher to read the canonical selection`,
      (v) => (document.getElementById("ctx-select") || {}).value === v, canonicalValue);

    // -- release A's held reconciliation: no late hash/DOM/server change ----
    const postsBeforeRelease = ctxPosts.length;
    releaseHeld();
    await new Promise((r) => setTimeout(r, SETTLE_MS));
    const lateHash = await readCtxHash(page);
    if (!sameCtx(lateHash.decoded, CANONICAL)) {
      throw new Error(`[${L}] releasing ${IDENTITY_A}'s held reconciliation rewrote the URL to `
        + `${describe(lateHash.decoded)} (${lateHash.raw}) under ${IDENTITY_C} — a superseded, `
        + `previous-identity response must reconcile nothing`);
    }
    const lateValue = await page.evaluate(() => (document.getElementById("ctx-select") || {}).value);
    if (lateValue !== canonicalValue) {
      throw new Error(`[${L}] releasing ${IDENTITY_A}'s held reconciliation repainted `
        + `${IDENTITY_C}'s switcher to "${lateValue}" (expected "${canonicalValue}")`);
    }
    const lateCtx = await serverCtx();
    if (!sameCtx(lateCtx, CANONICAL)) {
      throw new Error(`[${L}] releasing ${IDENTITY_A}'s held reconciliation moved the SERVER to `
        + `${describe(lateCtx)} under ${IDENTITY_C}`);
    }
    const latePosts = ctxPosts.slice(postsBeforeRelease);
    if (latePosts.length) {
      throw new Error(`[${L}] releasing ${IDENTITY_A}'s held reconciliation sent `
        + `${latePosts.length} late POST /api/context under ${IDENTITY_C}: `
        + `${JSON.stringify(latePosts)}`);
    }

    if (errors.length) throw new Error(`[${L}] console/page errors:\n${errors.join("\n")}`);
    console.log(`[${L}] OK — a REFUSED context intent is withdrawn from the URL at the identity `
      + `boundary: ${IDENTITY_C} never re-POSTs it, keeps ${nameA} at the server, in the URL and `
      + `in the switcher, and releasing ${IDENTITY_A}'s held reconciliation changes nothing.`);
  } catch (error) {
    throw new Error(`${error.message}\n--- server output ---\n${out}`);
  } finally {
    // Never leave a held route pending across context.close().
    releaseHeld();
    await context.close();
    await stopServer(server);
  }
}

async function main() {
  let browser;
  try {
    browser = await chromium.launch(
      process.env.SMOKE_CHROMIUM_PATH ? { executablePath: process.env.SMOKE_CHROMIUM_PATH } : {});
    for (const viewport of VIEWPORTS) {
      for (const transition of ["persona", "signout"]) {
        await checkWithdrawal(browser, viewport, transition);
      }
    }
    console.log("Context identity-withdrawal browser journey passed.");
  } catch (error) {
    console.error("Context identity-withdrawal browser journey FAILED.");
    console.error(error && error.stack ? error.stack : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
