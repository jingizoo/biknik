// Operator account-drawer coach-scope browser journey (#266 acceptance).
//
// The backend fails closed on coach scope and rejects malformed account
// payloads (test_coach_scope_fail_closed.py). This journey is the operator-UI
// half of the acceptance: at desktop AND 390px, the Administration → Users
// create-account drawer must
//   * reveal a Team selector when the role is Coach (and hide it for a role
//     that needs no scope, e.g. Viewer);
//   * send the team inside `scope` so a Coach is created bound to a real team
//     and appears in the accounts list;
// which is exactly the shape the hardened backend requires — the drawer can
// never silently create the unscoped Coach the fix now refuses.
//
// It also carries the #215 MODULE-STATE half of the superseded-render contract
// (see the final leg): this is the journey that already sits on the Users view
// as a League Admin, which is where `usersSelected` — a module-level selection
// a click handler reads back — can be torn away from the selection on screen.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const fs = require("fs");
const http = require("http");
const path = require("path");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8225 },
  { label: "phone", width: 390, height: 844, port: 8226 },
];

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

function login(page, username, password) {
  return page.evaluate(async ([u, p]) => {
    const r = await fetch("/api/auth/login", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: u, password: p }),
    });
    return r.status;
  }, [username, password]);
}

function loadDemo(page) {
  return page.evaluate(async () => {
    const r = await fetch("/api/demo/load", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" }, body: "{}",
    });
    return r.status;
  });
}

// A login performed from THIS process, on its own connection with its own
// (discarded) cookie jar — never through the browser, whose cookie is the
// League Admin's and must not be replaced. Used only to give a freshly created
// account a real, revocable session server-side.
function apiLogin(port, username, password) {
  const body = JSON.stringify({ username, password });
  return new Promise((resolve, reject) => {
    const req = http.request(
      { host: HOST, port, path: "/api/auth/login", method: "POST",
        headers: { "Content-Type": "application/json",
                   "Content-Length": Buffer.byteLength(body) } },
      (res) => { res.resume(); res.on("end", () => resolve(res.statusCode)); });
    req.setTimeout(5000, () => req.destroy(new Error("login timed out")));
    req.on("error", reject);
    req.end(body);
  });
}

// Poll a Node-side predicate to a deadline. A BARRIER, not a sleep: every
// caller below waits on a condition that a specific step has actually
// happened, so nothing in the leg depends on how fast the machine is.
async function waitUntil(predicate, message, timeoutMs = 15000) {
  const deadline = Date.now() + timeoutMs;
  while (!predicate()) {
    if (Date.now() > deadline) throw new Error(message);
    await new Promise((r) => setTimeout(r, 20));
  }
}

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
  return { context, page, errors };
}

async function selectRole(page, role) {
  await page.selectOption("#new-account-role", role);
}

// --- The #215 per-pass render() completion barrier -----------------------
//
// (The same barrier is spelled out in e2e/demo-lifecycle.js; each journey is a
// standalone script, so it is stated in both rather than shared.)
//
// The superseded-render leg below holds one response so that a SPECIFIC
// render() pass is suspended mid-flight while a newer pass paints over it.
// Before that leg may assert anything, it has to know that THAT pass has
// ENDED — either stood down at a supersession guard or run all the way
// through its remaining reads.
//
// Nothing at the document level can say that, and this barrier exists because
// the obvious candidates are all wrong here:
//   * `networkidle` is a LOAD-LIFECYCLE flag, not a settle signal. Once the
//     document has fired it, waitForLoadState returns immediately — with the
//     held request still held.
//   * every delivery-shaped barrier (waitForResponse, route.fulfill()
//     returning, the `requestfinished` count this leg already keeps) fires
//     strictly BEFORE the page's own `await fetch(...)` continuation resumes.
//     They prove the answer ARRIVED, which is exactly what this leg still
//     wants them for — but not that it was USED, and here the superseded pass
//     goes on to make three further real reads before it ends.
//   * an in-flight-request counter or a microtask flush is a quiet-period
//     detector, i.e. a fixed delay in costume: it cannot tell "the pass stood
//     down" from "the pass has not dispatched its next read yet", and that gap
//     is real — a microtask hop plus a JSON parse sits between the held
//     response resolving and the next fetch being issued.
//   * a MutationObserver watches for an EFFECT the FIXED build must never
//     produce, so on the build under test it could only ever time out.
//
// So the barrier observes the INVOCATION, not any effect of it. app.js is a
// classic script, so its top-level `async function render()` is a writable own
// property of window and every one of its internal call sites resolves through
// that binding. Wrapping it here — from the test, with the shipped app left
// untouched — numbers each invocation and stamps the number in a `finally`,
// which runs exactly when that invocation's promise settles: after a guard
// `return`, or after its last line. The wrapper mentions nothing the fix
// introduced (no `renderPass`, no `myRenderPass`), so it holds identically on
// a build with the per-await guards removed, where the superseded pass instead
// tears module state and then reads on.
//
// Identifying WHICH pass is held needs no guesswork and no heuristic: a pass
// that is suspended on the held response is, by definition, in flight at the
// moment the response is held. So the leg snapshots the in-flight set inside
// its own route handler — necessarily before the release — and the barrier
// waits for every id in that snapshot. The held pass is provably a member, so
// the barrier cannot resolve before it has ended. The snapshot is NOT always a
// singleton, and assuming otherwise is exactly the mistake to avoid here: the
// rebind above leaves its own re-render in flight, and it enters AFTER the
// pass this leg holds. Waiting for those extra passes as well is strictly
// stronger and costs nothing — none of them is blocked on anything held here.
async function armRenderPassWatch(page) {
  const wrapped = await page.evaluate(() => {
    if (window.__renderPassWatch) return true;
    if (typeof window.render !== "function") return false;
    const watch = { entered: 0, ended: Object.create(null) };
    const inner = window.render;
    window.__renderPassWatch = watch;
    window.render = async function watchedRender(...args) {
      const id = ++watch.entered;
      try {
        return await inner.apply(this, args);
      } finally {
        watch.ended[id] = Date.now();
      }
    };
    return window.render !== inner;
  });
  if (!wrapped) {
    throw new Error("#215: render() could not be wrapped, so no leg can await a "
      + "specific pass's completion — fix the wrapper rather than falling back "
      + "to a load-state event or a pause, both of which pass vacuously here");
  }
}

// Every pass that has been entered and has not yet ended.
function renderPassesInFlight(page) {
  return page.evaluate(() => {
    const w = window.__renderPassWatch;
    const live = [];
    for (let id = 1; id <= w.entered; id += 1) if (!w.ended[id]) live.push(id);
    return live;
  });
}

// Resolves only once EVERY pass in `passIds` has settled — among them the one
// suspended on the held response. Returns how long it actually had to wait, so
// a failing assertion can report that the superseded pass had finished before
// it looked, rather than leaving open the possibility that the two raced.
async function awaitRenderPassesEnd(page, passIds, label) {
  const startedWaiting = Date.now();
  await page.waitForFunction(
    (ids) => ids.every((id) => !!(window.__renderPassWatch
      && window.__renderPassWatch.ended[id])),
    passIds, { timeout: 15000 },
  ).catch(() => {
    throw new Error(`${label} render pass(es) #${passIds.join(", #")} never `
      + `ended: the held response was released but those invocations have not `
      + `settled, so nothing can be asserted about what they did`);
  });
  return Date.now() - startedWaiting;
}

// STRUCTURAL half of the #215 module-state contract, next to the behavioural
// half below.
//
// The fix is per-await by design (app.js, `renderPass`) -- a token recheck in
// render()'s own frame after every await, before the response is applied to
// any module-level name. That shape is exact, and its one weakness is
// completeness: render() is ~750 lines with 39 awaits, and ONE added later
// without its guard silently re-opens the tear for whatever that response
// writes. The journey below can only catch the instance it drives
// (`usersSelected`); this catches every future one, at the cost of reading a
// file.
//
// Deliberately a source-shape assertion and not a behavioural one, because
// there is no behaviour to assert until someone writes the code that breaks:
// the point is to fail on the OMISSION, at the moment it is introduced.
function auditRenderPassGuards() {
  const appJs = path.resolve(
    BACKEND_DIR, "hockey_scheduler", "web", "static", "app.js");
  const lines = fs.readFileSync(appJs, "utf-8").split("\n");
  const start = lines.indexOf("async function render() {");
  const guardEnd = lines.indexOf("  } catch (e) {", start + 1);
  if (start < 0 || guardEnd < 0) {
    throw new Error("render()'s try block could not be located in app.js — this "
      + "audit needs updating, not deleting");
  }
  const GUARD = "if (renderPass !== myRenderPass) return;";
  const unguarded = [];
  let total = 0;
  let i = start;
  while (i <= guardEnd) {
    const trimmed = lines[i].trim();
    if (lines[i].includes("await ") && !trimmed.startsWith("//")
        && !trimmed.startsWith("*")) {
      total += 1;
      let end = i;                       // statements wrap over several lines
      while (!lines[end].trimEnd().endsWith(";")) end += 1;
      let next = end + 1;                // ...and a comment may sit in between
      while (lines[next].trim().startsWith("//") || lines[next].trim() === "") {
        next += 1;
      }
      if (!lines[next].trim().startsWith(GUARD)) {
        unguarded.push(`  app.js:${i + 1}: ${trimmed}`);
      }
      i = end + 1;
      continue;
    }
    i += 1;
  }
  if (unguarded.length) {
    throw new Error(
      `#215: ${unguarded.length} of ${total} awaits in render() are not followed `
      + `by \`${GUARD}\`. A superseded pass resuming there would apply its stale `
      + `response to module-level state that the newer pass's DOM does not `
      + `agree with, and the next click would read it:\n${unguarded.join("\n")}`);
  }
  console.log(`OK — all ${total} awaits in render() recheck renderPass before `
    + `applying their response.`);
}

async function checkViewport(browser, viewport) {
  const base = `http://${HOST}:${viewport.port}`;
  const server = startServer(viewport.port, {});
  let out = "";
  server.stdout.on("data", (d) => { out += d.toString(); });
  server.stderr.on("data", (d) => { out += d.toString(); });
  const { context, page, errors } = await newPage(browser, viewport);
  const L = viewport.label;
  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    if ((await login(page, "admin", "demo")) !== 200) {
      throw new Error(`[${L}] admin login failed`);
    }
    if ((await loadDemo(page)) !== 200) throw new Error(`[${L}] demo load failed`);
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });

    // Administration → Users.
    await page.waitForFunction(
      () => { const t = document.querySelector('.tab[data-tab="users"]');
              return t && t.offsetParent !== null; },
      null, { timeout: 10000 });
    await page.click('.tab[data-tab="users"]');
    await page.waitForSelector("#new-account-role", { timeout: 10000 });

    // A scope-free role shows NO team selector.
    await selectRole(page, "viewer");
    if (await page.locator("#new-account-team").count()) {
      throw new Error(`[${L}] viewer role wrongly showed a Team selector`);
    }

    // Coach reveals a populated Team selector — the drawer collects team scope.
    await selectRole(page, "coach");
    await page.waitForSelector("#new-account-team", { timeout: 5000 });
    const teamOptions = await page.locator("#new-account-team option").count();
    if (teamOptions < 1) throw new Error(`[${L}] coach Team selector had no options`);

    // Create a coach bound to a real team and confirm it lands in the list.
    const username = "browser_coach";
    await page.fill("#new-account-username", username);
    await page.fill("#new-account-password", "temp-pw");
    // Pick the first real team option.
    const teamId = await page.locator("#new-account-team option").first().getAttribute("value");
    if (!teamId) throw new Error(`[${L}] no real team option to bind the coach to`);
    await page.selectOption("#new-account-team", teamId);
    await page.click("[data-account-create]");
    await page.waitForFunction(
      (name) => Array.from(document.querySelectorAll("[data-user-sessions] .row-main"))
        .some((el) => el.textContent.trim() === name),
      username, { timeout: 10000 });

    // --- Rebind an existing coach's team (the #266 remediation path) --------
    // The just-created coach is auto-selected, so its Coach-team-scope panel is
    // showing. Rebind it to a DIFFERENT team and confirm the change persists.
    await page.waitForSelector("[data-rebind-scope]", { timeout: 10000 });
    const rebindValues = await page.locator("#rebind-team option")
      .evaluateAll((os) => os.map((o) => o.value).filter(Boolean));
    const otherTeam = rebindValues.find((v) => v !== teamId);
    if (!otherTeam) throw new Error(`[${L}] need a second team to test rebind`);
    await page.selectOption("#rebind-team", otherTeam);
    await page.click("[data-rebind-scope]");
    // After the audited rebind + re-render, the panel reflects the new team.
    await page.waitForFunction(
      (t) => { const s = document.querySelector("#rebind-team"); return s && s.value === t; },
      otherTeam, { timeout: 10000 });

    // --- REGRESSION (#215): a SUPERSEDED render() MUST NOT LEAVE MODULE ----
    //     STATE BEHIND.
    //
    // render() claims a monotonic `renderPass` and a superseded pass stands
    // down at its DOM boundaries. That protects the DOM — and, on its own,
    // introduces a failure the pre-token code could not produce: the winner
    // owns the screen while the loser's ~25 module-level writes still land
    // last, so the DOM and module state disagree. Every click handler reads
    // MODULE state, not the DOM, so the next click acts on the loser's value.
    //
    // `usersSelected` is the sharpest instance. render() clears it outright
    // when the account it names is absent from the accounts payload THAT PASS
    // fetched (app.js, the `view === "users"` block), and the Revoke button's
    // handler composes its URL from `usersSelected` while its session id comes
    // from the DOM. Torn, the operator sees an account selected with its live
    // session listed under it, clicks Revoke, and the app posts to
    // /api/accounts/null/... — a 404, and the session stays signed in.
    //
    // FORCED, not raced, so this leg is deterministic where the flake was not:
    // exactly ONE GET /api/accounts is captured and held. `route.fetch()` runs
    // that request FOR REAL at hold time, so what the loser eventually applies
    // is the server's own genuine answer from before the account below existed
    // — a slow response, not a fabricated one. Nothing else is faked: same
    // server, same app.js, same click path.
    const accountsUrl = `${base}/api/accounts`;
    const staleUser = "stale_render_viewer";
    const stalePassword = "temp-pw-2";
    let holdAccounts = false;
    let capturedAt = 0;
    let deliveredAt = 0;
    let heldReachedPage = 0;
    let heldPasses = null;
    let released = false;
    let releaseHeld = null;
    const heldReleased = new Promise((r) => { releaseHeld = r; });
    page.on("requestfinished", (r) => {
      if (released && r.method() === "GET" && r.url() === accountsUrl) {
        heldReachedPage += 1;
      }
    });
    // Armed BEFORE the pass that will be held is started, because the barrier
    // observes that invocation from its entry (see armRenderPassWatch).
    await armRenderPassWatch(page);
    await page.route(accountsUrl, async (route) => {
      if (!holdAccounts || route.request().method() !== "GET") {
        return route.continue();
      }
      holdAccounts = false;                 // hold exactly one: the loser's
      // Armed here, with the request still held: whichever pass is suspended
      // on it is in flight at this instant and so is in this snapshot.
      heldPasses = await renderPassesInFlight(page);
      const real = await route.fetch();     // the genuine pre-create answer
      capturedAt = Date.now();
      await heldReleased;
      await route.fulfill({ response: real });
      deliveredAt = Date.now();
    });

    // (a) The pass that will LOSE. It blanks #content synchronously, then
    //     suspends on the held GET /api/accounts.
    holdAccounts = true;
    await page.evaluate(() => switchTab("users"));
    await waitUntil(() => capturedAt > 0,
      `[${L}] the superseded pass never issued its GET /api/accounts`);
    if (!heldPasses || !heldPasses.length) {
      throw new Error(`[${L}] the held GET /api/accounts belonged to no render() `
        + `pass, so this leg is not exercising a superseded render at all`);
    }

    // (b) A newer, unheld pass repaints the surface the loser blanked, so the
    //     create form is on screen for the real click below.
    await page.evaluate(() => switchTab("users"));
    await page.waitForSelector("#new-account-role", { timeout: 10000 });

    // (c) Create a scope-free account through the REAL form. Its own success
    //     handler sets the module selection to the new account and re-renders
    //     — this is the pass that WINS and owns the DOM from here on.
    await selectRole(page, "viewer");
    await page.fill("#new-account-username", staleUser);
    await page.fill("#new-account-password", stalePassword);
    await page.click("[data-account-create]");
    await page.waitForFunction(
      (name) => Array.from(document.querySelectorAll("[data-user-sessions]"))
        .some((b) => b.classList.contains("active")
          && b.querySelector(".row-main").textContent.trim() === name),
      staleUser, { timeout: 10000 });
    const staleUserId = await page.$eval(
      "[data-user-sessions].active", (b) => b.dataset.userSessions);

    // (d) Give it a real session to revoke, minted from THIS process so the
    //     browser's League-Admin cookie is untouched, then select it through
    //     the UI so the Sessions panel and its Revoke button are painted.
    if ((await apiLogin(viewport.port, staleUser, stalePassword)) !== 200) {
      throw new Error(`[${L}] could not sign the new account in to create a session`);
    }
    await page.click(`[data-user-sessions="${staleUserId}"]`);
    await page.waitForSelector("[data-revoke-session]", { timeout: 10000 });
    const sessionId = await page.$eval(
      "[data-revoke-session]", (b) => b.dataset.revokeSession);

    // Non-vacuity, part 1: the winner must have painted WHILE the loser was
    // still suspended. If the hold had already been let go by now the two
    // passes never overlapped and everything below would pass for the wrong
    // reason.
    if (deliveredAt !== 0) {
      throw new Error(`[${L}] the superseded pass was released before the newer `
        + `one painted — the interleaving this leg is about never happened`);
    }

    // (e) Release the loser and let it resume. It applies its response — or,
    //     once the fix is in, discards it at the guard that now sits between
    //     the response and its use. The fulfil + requestfinished checks stay,
    //     but as NON-VACUITY only: they prove the held answer really reached
    //     the page, and neither is a settle signal. The settle signal is the
    //     per-pass completion barrier below, which resolves when that
    //     invocation ends — after its three follow-on reads, on a build whose
    //     per-await guards have been removed.
    const stillSuspended = (await renderPassesInFlight(page))
      .filter((id) => heldPasses.includes(id));
    released = true;
    releaseHeld();
    // THE settle signal, awaited FIRST so that it — and not the incidental
    // duration of the node-side polls below — is what orders this leg. The
    // superseded invocation itself has ended.
    const losingPassWaitMs = await awaitRenderPassesEnd(page, heldPasses, `[${L}]`);
    // Non-vacuity, checked after the barrier because both are monotone facts
    // about the past, not orderings: once the response has been delivered and
    // has reached the page it stays that way, and a pass suspended on that
    // response cannot have ended without it. A fix that "worked" only because
    // the loser never got its answer back would prove nothing at all, so the
    // leg still insists on both.
    await waitUntil(() => deliveredAt > 0,
      `[${L}] the held accounts response was never delivered`);
    await waitUntil(() => heldReachedPage >= 1,
      `[${L}] the superseded pass's accounts response never reached the page, `
        + `so this leg proved nothing`);
    const barrier = `the superseded pass (render #${(stillSuspended.length
      ? stillSuspended : heldPasses).join(", #")}, suspended on the held `
      + `GET /api/accounts right up to the release) had already ended `
      + `${losingPassWaitMs}ms after release, so this is what it left behind, `
      + `not a race`;

    // (f) The winner still owns the DOM — the `renderPass` DOM guarantee.
    const painted = await page.evaluate((id) => {
      const row = document.querySelector(`[data-user-sessions="${id}"]`);
      const revoke = document.querySelector("[data-revoke-session]");
      return { selectedOnScreen: !!(row && row.classList.contains("active")),
               revokeOnScreen: !!revoke };
    }, staleUserId);
    if (!painted.selectedOnScreen || !painted.revokeOnScreen) {
      throw new Error(`[${L}] a superseded render() repainted the Users view: `
        + `${JSON.stringify(painted)} — ${barrier}`);
    }

    // (g) THE TEAR, asserted where a user meets it: a real click on a real
    //     handler that reads module state. The URL it composes must name the
    //     account that is selected ON SCREEN.
    const [revokeRequest] = await Promise.all([
      page.waitForRequest((r) => r.method() === "POST"
        && /\/sessions\/[^/]+\/revoke$/.test(r.url()), { timeout: 10000 }),
      page.click("[data-revoke-session]"),
    ]);
    const expectedRevokeUrl =
      `${base}/api/accounts/${staleUserId}/sessions/${sessionId}/revoke`;
    if (revokeRequest.url() !== expectedRevokeUrl) {
      throw new Error(`[${L}] a superseded render() left its own module state `
        + `behind: the Users view shows ${staleUser} selected with its session `
        + `listed, but Revoke acted on the superseded pass's value — expected `
        + `POST ${expectedRevokeUrl}, got POST ${revokeRequest.url()} — ${barrier}`);
    }
    // ...and the outcome the operator actually cares about: the session is
    // really signed out, not left active behind a 404 nobody surfaced.
    await page.waitForFunction(
      (id) => { const b = document.querySelector(`[data-revoke-session="${id}"]`);
                return !b; },
      sessionId, { timeout: 10000 });
    const stillActive = await page.evaluate(async ([u, s]) => {
      const r = await fetch(u, { credentials: "same-origin" });
      const j = await r.json();
      return (j.sessions || []).some((x) => x.id === s && x.status === "active");
    }, [`/api/accounts/${staleUserId}/sessions`, sessionId]);
    if (stillActive) {
      throw new Error(`[${L}] Revoke reported no error but the session is still `
        + `active — the click acted on a superseded pass's account id`);
    }
    await page.unroute(accountsUrl);

    if (errors.length) {
      throw new Error(`[${L}] console/page errors:\n${errors.join("\n")}`);
    }
    console.log(`[${L}] OK — Team selector gated to Coach; coach created with team scope, `
      + `then rebound to another team; and a superseded render() leaves no module state behind.`);
  } catch (error) {
    throw new Error(`${error.message}\n--- server output ---\n${out}`);
  } finally {
    await context.close();
    await stopServer(server);
  }
}

async function main() {
  let browser;
  try {
    auditRenderPassGuards();
    browser = await chromium.launch(
      process.env.SMOKE_CHROMIUM_PATH ? { executablePath: process.env.SMOKE_CHROMIUM_PATH } : {});
    for (const viewport of VIEWPORTS) await checkViewport(browser, viewport);
    console.log("Coach-scope operator-drawer browser journey passed.");
  } catch (error) {
    console.error("Coach-scope operator-drawer browser journey FAILED.");
    console.error(error && error.message ? error.message : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
