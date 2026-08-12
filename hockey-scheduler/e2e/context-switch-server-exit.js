// A CONTEXT SWITCH CANNOT COMMIT UNTIL AN ALREADY-DISPATCHED EXACT-SEASON READ
// HAS EXITED THE SERVER (#159 follow-up; the residue #412 recorded as
// unclosable from the client).
//
// WHAT #412 CLOSED AND WHAT IT COULD NOT. `context-switch-read-settlement.js`
// proves the app CANCELS its outstanding scoped reads and AWAITS THEIR
// SETTLEMENT before POSTing the new context. That is a real barrier and it is
// still asserted, unchanged, by that journey. But it wrote down its own limit
// in plain words: it "cannot show that the SERVER has finished with a read it
// already received; HTTP offers the client no such signal". `entry.settled`
// resolves the instant `AbortController.abort()` fires, while the server keeps
// running the request it already accepted -- so the POST could still land
// mid-handler, and the handler would then compare its Season against the NEW
// selection and answer the CI 404:
//
//     GET /api/v2/setup/seasons/season_3/venue-candidates -> 404
//     (run 31504917446, browser shard 1, PHONE leg; the app's own abort ledger
//      records that read as dispatched:true, so enrolment was never the gap)
//
// THIS JOURNEY MEASURES THE HALF THAT NEEDED A SERVER CHANGE, and it is
// possible now for one reason: `services/context_gate.py` orders the two
// SERVER-SIDE, by arrival, so "the handler has exited" is a fact the server can
// act on even though the client can never observe it.
//
// HOW A REQUEST IS HELD *INSIDE* THE SERVER. `page.route` cannot do this -- it
// holds a request BEFORE it is forwarded, which is the opposite condition (the
// server has not seen it). So the backend is launched through
// `server-park-harness.py`, an E2E-OWNED wrapper that imports the real server
// and MONKEY-PATCHES `ApiService.get_venue_grant_candidates` /
// `list_season_venue_access` to block on command. No production file gains a
// hook -- a flag that let any caller park a request would be a denial-of-service
// affordance shipped to make a test easier. `hockey_scheduler` runs byte-
// identical to production; the harness also exposes a loopback control plane
// (`/arm`, `/status`, `/release`) so the journey can observe, rather than infer,
// that the handler is really inside.
//
// THE HELD READ IS ISSUED OUTSIDE THE APP'S BARRIER, deliberately. A read the
// app enrolled would be aborted by the switch, and the browser would then never
// see the answer -- which is precisely the blind spot. Issuing it as a raw
// same-origin fetch models the case the client barrier provably cannot cover:
// a genuinely dispatched, un-cancelled, in-server read straddling a switch. Its
// status is then an ORDINARY observation on the wire, and on a pre-gate build
// it is the recorded 404.
//
// WHAT IT ASSERTS, per viewport (desktop 1440x900 AND phone 390x844, the leg CI
// actually failed on):
//
//  (A) THE READ IS INSIDE THE SERVER. The harness reports `parked:true` -- the
//      handler has been entered. Not a client-side inference, not a timer.
//  (B) THE SWITCH CANNOT COMMIT. With the handler held, a REAL operator switch
//      is made through the shipped `#ctx-select` control. For a full second
//      afterwards, `GET /api/context` still reads the OLD Season and the
//      switch's own POST has not settled. On a pre-gate build the POST settles
//      in milliseconds and the saved tuple has already moved.
//  (C) NO 404, AND NO CONSOLE ERROR. Released, the held read completes 200 for
//      the Season it named. Two independent records agree: the page's own
//      response for that fetch, and the harness's report of what the SERVER's
//      handler produced (`outcome:"ok"`, i.e. the ceiling never refused it).
//      Every console error and every non-2xx on the two scoped routes is
//      reconciled at the end against the allowances leg (S) declares.
//  (D) THE SWITCH STILL LANDS, PROMPTLY. After the release the POST resolves
//      200 and `GET /api/context` reads the NEW Season. Ordering is not
//      cancellation.
//  (S) THE CEILING IS UNWEAKENED (direct server control, no client involved).
//      With the NEW Season selected, an independently issued read for the OLD
//      Season -- a SIBLING of the same Program -- is still refused 404, and its
//      body is byte-identical to the refusal for a Season id that does not
//      exist. Asserted in the same run as the fix so the two cannot drift.
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

// The same single definition of "a context-scoped read" the sibling journey
// uses, so the two can never disagree about which routes are enrolled.
const SCOPED_READ_RE =
  /\/api\/v2\/setup\/seasons\/([^/?]+)\/venue-(access|candidates)(?:\?|$)/;

// How long the switch is watched for while the read is held. A pre-gate build
// commits in single-digit milliseconds.
const COMMIT_WINDOW_MS = 1000;

// Each (viewport x held read) gets its own server and page: a request abandoned
// in one leg would otherwise settle inside the next leg's ledger.
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900,
    ports: { "venue-candidates": 8821, "venue-access": 8823 } },
  { label: "phone", width: 390, height: 844,
    ports: { "venue-candidates": 8822, "venue-access": 8824 } },
];
// The two reads this journey parks IN THE BROWSER. They are the two that CI
// actually failed on and the two the Setup UI issues, not the whole gate: the
// authoritative list is `CONTEXT_SCOPED_READ_ROUTES` in `web/server.py`, whose
// other two entries (the named scenario and Division standings) are covered by
// `tests/test_context_switch_server_exit.py` over authenticated HTTP on all
// three stores. The gate is store- and route-agnostic, so what this journey
// adds over those is the BROWSER, not another route.
const HELD_READS = ["venue-candidates", "venue-access"];

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

// Records when the switch's POST is DISPATCHED and when it SETTLES. Leg (B)
// needs the second of those: a switch that has already answered while the
// handler is still inside the server did not wait for it.
async function installContextPostObserver(page) {
  await page.addInitScript(() => {
    window.__ctxPosts = [];
    const realFetch = window.fetch;
    window.fetch = function (input, init) {
      const url = typeof input === "string" ? input : ((input && input.url) || "");
      const method = String((init && init.method)
        || (input && input.method) || "GET").toUpperCase();
      const isSwitch = method === "POST"
        && url.split("?")[0].replace(/^https?:\/\/[^/]+/, "") === "/api/context";
      const p = realFetch.apply(this, arguments);
      if (!isSwitch) return p;
      const record = { at: Date.now(), settledAt: null, status: null };
      window.__ctxPosts.push(record);
      return p.then((r) => {
        record.settledAt = Date.now();
        record.status = r.status;
        return r;
      }, (e) => { record.settledAt = Date.now(); record.status = "rejected"; throw e; });
    };
  });
}

// Dispatch a scoped read that the app's barrier does NOT know about, and keep
// its promise so its eventual status can be read. This is the shape the client
// barrier provably cannot cover.
async function dispatchUnenrolledRead(page, url) {
  await page.evaluate((u) => {
    window.__heldRead = { url: u, status: null, body: null, done: false };
    window.__heldReadPromise = fetch(u, { credentials: "same-origin" })
      .then(async (r) => {
        window.__heldRead.status = r.status;
        window.__heldRead.body = await r.text().catch(() => "");
        window.__heldRead.done = true;
      })
      .catch((e) => {
        window.__heldRead.status = `network:${(e && e.message) || e}`;
        window.__heldRead.done = true;
      });
  }, url);
}

async function checkLeg(browser, viewport, heldKind) {
  const port = viewport.ports[heldKind];
  const controlPort = port + 100;
  const base = `http://${HOST}:${port}`;
  const L = `${viewport.label}/${heldKind}`;
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

  await installContextPostObserver(page);
  await installContextFixture(page);

  const t0 = Date.now();
  const step = (name) => process.stderr.write(
    `[${L}] +${String(Date.now() - t0).padStart(6)}ms ${name}\n`);

  let released = false;
  try {
    step("waiting for server");
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    step("server up");
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 15000 });
    if ((await apiPost(page, "/api/auth/login",
                       { username: "admin", password: "demo" })).status !== 200) {
      throw new Error(`[${L}] admin login failed`);
    }
    step("logged in");

    // ------------------------------- fixture ----------------------------
    // TWO Seasons of the SAME Program: the departed Season is a SIBLING of the
    // arriving one, so nothing about the Program axis can explain a refusal and
    // leg (S) is proving the exact-SEASON ceiling.
    const roots = await page.evaluate(async () => {
      const f = window.hsFixture;
      const org = await f.create("org", "/api/v2/setup/organization",
        { name: "Server Exit Org" });
      const program = await f.create("program", "/api/v2/setup/program",
        { name: "Server Exit Program", organization_id: org.id });
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
        { name: "Exit Arena One", organization_id: r.org });
      const venue2 = await f.create("venue 2", "/api/v2/setup/venue",
        { name: "Exit Arena Two", organization_id: r.org });
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
    if ((await page.evaluate(() => document.getElementById("ctx-select").value))
        !== oldValue) {
      throw new Error(`[${L}] the shell did not boot on the persisted Season 1`);
    }
    step("shell on Season 1");

    // ---- arm the park and dispatch the read INTO the server -------------
    const heldUrl = `/api/v2/setup/seasons/${built.s1}/${heldKind}`;
    await control(controlPort, "POST", "/arm",
                  { kind: heldKind, season_id: built.s1 });
    await dispatchUnenrolledRead(page, heldUrl);

    // (A) THE READ IS INSIDE THE SERVER — the harness says so from inside the
    //     process. A timer would only say "some time passed".
    step("waiting for the read to enter the server");
    const parkDeadline = Date.now() + 20000;
    for (;;) {
      const status = await control(controlPort, "GET", "/status");
      if (status.parked) break;
      if (Date.now() > parkDeadline) {
        throw new Error(`[${L}] (A) the ${heldKind} read never entered the `
          + `server handler; there is no race to run. status=`
          + JSON.stringify(status));
      }
      await page.waitForTimeout(25);
    }
    step("(A) read is INSIDE the server handler");

    // ---- THE REAL SWITCH, through the shipped control -------------------
    // page.selectOption fires the genuine `change` event #ctx-select's onchange
    // listens for — the only operator path into setActiveContext. No raw POST.
    const postsBefore = (await page.evaluate(() => window.__ctxPosts.length));
    await page.selectOption("#ctx-select", newValue);
    step("switch initiated");

    // (B) THE SWITCH CANNOT COMMIT while the handler is held. Two independent
    //     observations over a full second, either of which fails a pre-gate
    //     build on its own.
    const windowEnd = Date.now() + COMMIT_WINDOW_MS;
    let sawCommitted = null;
    while (Date.now() < windowEnd) {
      const seen = await apiGet(page, "/api/context");
      if (seen.status === 200 && seen.json && seen.json.season_id !== built.s1) {
        sawCommitted = seen.json;
        break;
      }
      await page.waitForTimeout(50);
    }
    if (sawCommitted) {
      fail(`[${L}] (B) the context switch COMMITTED while a dispatched `
        + `${heldKind} read was still inside the server handler — saved tuple `
        + `is already ${JSON.stringify(sawCommitted)}. The held read will now `
        + `be judged against a selection it never saw, which is the CI 404.`);
    }
    const postsDuring = (await page.evaluate(() => window.__ctxPosts.slice()))
      .slice(postsBefore);
    if (!postsDuring.length) {
      fail(`[${L}] (B) no POST /api/context was observed for the switch`);
    }
    const settledEarly = postsDuring.filter((p) => p.settledAt !== null);
    if (settledEarly.length) {
      fail(`[${L}] (B) the switch's POST /api/context ANSWERED (`
        + `${settledEarly.map((p) => p.status).join(",")}) while the ${heldKind} `
        + `read was still inside the server — it did not wait for the handler `
        + `to exit`);
    }
    const parkedStill = await control(controlPort, "GET", "/status");
    if (!parkedStill.parked || parkedStill.exited) {
      fail(`[${L}] (B) the harness no longer reports the read as held `
        + `(${JSON.stringify(parkedStill)}); the window above measured nothing`);
    }
    step("(B) switch is blocked; saved tuple still Season 1");

    // ---- release, and let the ordering play out -------------------------
    await control(controlPort, "POST", "/release");
    released = true;
    step("released");

    // (C) THE HELD READ COMPLETED 200 — never the CI 404. Two records: the
    //     page's own response, and the SERVER's own report of what its handler
    //     produced.
    await page.waitForFunction(() => window.__heldRead && window.__heldRead.done,
                               null, { timeout: 20000 });
    const heldRead = await page.evaluate(() => window.__heldRead);
    if (heldRead.status !== 200) {
      fail(`[${L}] (C) the held ${heldKind} read for the DEPARTING Season `
        + `answered ${heldRead.status} — this is the defect. body=`
        + String(heldRead.body).slice(0, 300));
    }
    const exitDeadline = Date.now() + 20000;
    let finalStatus = null;
    for (;;) {
      finalStatus = await control(controlPort, "GET", "/status");
      if (finalStatus.exited) break;
      if (Date.now() > exitDeadline) {
        throw new Error(`[${L}] (C) the parked handler never exited: `
          + JSON.stringify(finalStatus));
      }
      await page.waitForTimeout(25);
    }
    if (finalStatus.outcome !== "ok") {
      fail(`[${L}] (C) the SERVER's own handler refused the held read `
        + `(outcome=${finalStatus.outcome}) — the exact-Season ceiling was `
        + `applied against a selection the read never saw`);
    }
    step("(C) held read answered 200 in the page and 'ok' in the server");

    // (D) THE SWITCH STILL LANDS. Ordering is not cancellation.
    const savedDeadline = Date.now() + 20000;
    for (;;) {
      const seen = await apiGet(page, "/api/context");
      if (seen.status === 200 && seen.json && seen.json.season_id === built.s2) break;
      if (Date.now() > savedDeadline) {
        throw new Error(`[${L}] (D) the switch never persisted; GET /api/context `
          + `still reads ${JSON.stringify(seen.json)}`);
      }
      await page.waitForTimeout(50);
    }
    const postsAfter = (await page.evaluate(() => window.__ctxPosts.slice()))
      .slice(postsBefore);
    postsAfter.forEach((p, i) => {
      if (p.settledAt === null) {
        fail(`[${L}] (D) the switch's POST #${i} never settled after the `
          + `release — the gate did not let it through`);
      } else if (p.status !== 200) {
        fail(`[${L}] (D) the switch's POST #${i} answered ${p.status}`);
      }
    });
    step("(D) the new tuple is persisted and the POST answered 200");

    // (S) THE CEILING IS UNWEAKENED — direct server control, no client race.
    //     A SIBLING Season of the SAME Program, independently requested with
    //     the NEW Season selected, is still refused with the generic 404, and
    //     indistinguishably from a Season that does not exist.
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

    // ---- reconcile every non-2xx and every console error ---------------
    const matchAllowance = (method, url, status) => {
      const hit = allowances.find((a) => !a.matched && a.method === method
        && a.url === url && a.status === status);
      if (hit) { hit.matched = true; return true; }
      return false;
    };
    responses
      .filter((r) => r.status >= 400)
      .forEach((r) => {
        if (matchAllowance(r.method, r.url, r.status)) return;
        fail(`[${L}] (C) unexpected ${r.status} on ${r.method} ${r.url}`);
      });
    consoleErrors.forEach((c) => {
      if (allowances.some((a) => a.url === c.url)) return;
      fail(`[${L}] (C) console error: ${c.text}${c.url ? ` @ ${c.url}` : ""}`);
    });
    const scoped404 = responses.filter(
      (r) => SCOPED_READ_RE.test(r.url) && r.status >= 400
             && !allowances.some((a) => a.url === r.url));
    if (scoped404.length) {
      fail(`[${L}] (C) ${scoped404.length} scoped-read failure(s) not declared: `
        + JSON.stringify(scoped404));
    }
    step("ledger reconciled");
  } finally {
    if (!released) {
      // A leg that threw before releasing would otherwise leave a Python thread
      // parked and the next leg's server unable to shut down cleanly.
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
      for (const heldKind of HELD_READS) {
        await checkLeg(browser, viewport, heldKind);
      }
    }
  } finally {
    await browser.close();
  }
  console.log("context-switch-server-exit: OK");
})().catch((e) => {
  console.error(e && e.message ? e.message : e);
  process.exit(1);
});
