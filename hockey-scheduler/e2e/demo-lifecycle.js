// Demo lifecycle browser journey (#215 acceptance).
//
// The demo now boots as a clean slate and is populated/emptied explicitly. At
// desktop and 390px this proves the full lifecycle a League Admin drives:
//   * first launch shows a blank setup with the "Start your competition" card, and
//     the header database-icon control reads "Load demo data";
//   * Load (from the empty-state card) builds the sample dataset, the card is
//     replaced by the league trees, and the header now reads "Reset demo data";
//   * Clear (typed CLEAR, from the header menu) returns to the blank slate;
//   * Reset (typed RESET, from the header menu) rebuilds the canonical dataset.
// The header control is a compact icon whose intent lives in its tooltip and
// accessible label; both are asserted. Fails on any browser console/page error.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8181 },
  { label: "phone", width: 390, height: 844, port: 8182 },
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

// Selects the Hierarchy sub-view via the user-visible toggle and confirms it
// took effect (#345 batch 2: Setup now lands on the six-workflow hub, so a
// journey asserting against Hierarchy controls has to navigate there). Never
// sets setupView directly, and asserts the segment is active BEFORE any
// domain control is awaited, so a future default change fails with "wrong
// sub-view" rather than an opaque selector timeout.
async function enterSetupHierarchy(page) {
  await page.click('[data-setup-view="hierarchy"]');
  await page.waitForFunction(() => {
    const seg = document.querySelector(".setup-viewtoggle .seg.active");
    return !!(seg && seg.dataset.setupView === "hierarchy");
  }, null, { timeout: 10000 });
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

  // Network trace (#215 flake diagnosis). This records BROWSER-SIDE events
  // only, and every sentence it prints is limited to what those events
  // actually prove. There is no server-side evidence here, so nothing below
  // may assert backend receipt: a Playwright `request` event proves the
  // browser EMITTED a request and nothing more.
  //
  // What the events do prove, and it is enough for the question that made
  // these diagnostics necessary:
  //
  //   * `request`  -- the browser EMITTED the request.
  //   * `response` -- the browser RECEIVED response headers for it.
  //   * `requestfinished` / `requestfailed` -- the browser finished reading
  //     the response, or the request failed at the transport with an
  //     errorText.
  //
  // Those two facts cleanly separate the two candidate causes this journey
  // was flaking between:
  //
  //   no `request`             -> the click never issued the fetch. A
  //                               FRONTEND failure, and the actual root cause
  //                               of the #215 flake.
  //   `request`, no `response` -> the request went out and nothing came back.
  //
  // The second case is deliberately NOT subdivided. From the browser, a
  // request the server received and stalled on and a request that never
  // reached the server are indistinguishable -- both are simply "emitted, no
  // response". Saying which one it is would require server-side evidence this
  // journey does not collect.
  //
  // Every netLog entry is scoped to the attempt in force when it was observed,
  // so a counter from an earlier journey step (this journey issues five POSTs
  // across three lifecycle routes, and re-visits /api/demo/load three times)
  // can never be read as evidence for the step under test.
  let currentAttempt = 0;
  const netLog = [];
  const record = (type, r, extra) => netLog.push({
    t: Date.now(), attempt: currentAttempt, type,
    method: (r.request ? r.request() : r).method(), url: r.url(), ...extra });
  page.on("request", (r) => record("request", r));
  page.on("response", (r) => record("response", r, { status: r.status() }));
  page.on("requestfinished", (r) => record("requestfinished", r));
  page.on("requestfailed", (r) => record("requestfailed", r,
    { failure: (r.failure() && r.failure().errorText) || "unknown" }));

  // Click an element and wait for its resulting POST as ONE coordinated
  // operation (Playwright's recommended pattern: page.waitForResponse
  // begins listening the instant it's called, which Promise.all makes
  // unambiguous rather than relying on call-order alone).
  const clickAndAwaitResponse = async (url, method, clickFn, label) => {
    currentAttempt += 1;
    const attempt = currentAttempt;
    try {
      const [resp] = await Promise.all([
        page.waitForResponse((r) => r.url() === url && r.request().method() === method),
        clickFn(),
      ]);
      return resp;
    } catch (err) {
      // Scoped by URL, by METHOD (a GET to the same path is not evidence
      // about the POST under test) and by ATTEMPT.
      const mine = (e) => e.attempt === attempt && e.url === url
        && e.method === method;
      const matched = netLog.filter(mine);
      const emitted = matched.some((e) => e.type === "request");
      const responseSeen = matched.some((e) => e.type === "response");
      const failed = matched.find((e) => e.type === "requestfailed");
      // Every branch states a BROWSER-SIDE observation only. Where the request
      // ended up once it left the browser is not observable from here, and is
      // not claimed.
      const diagnosis =
        !emitted
          ? "the browser NEVER EMITTED this request -- the click did not fire the fetch at all (a frontend/DOM failure)."
          : failed
            ? `the browser emitted it and the transport FAILED (${failed.failure}); no response reached the browser.`
            : !responseSeen
              ? "the browser EMITTED it but NO RESPONSE reached the browser. Browser-side "
                + "events cannot tell a server that received it and stalled apart from a "
                + "request that never arrived -- both look identical from here."
              : "the browser emitted it AND received a response for it; the wait predicate is what did not match.";
      const recent = netLog.filter((e) => e.attempt === attempt).slice(-40);
      throw new Error(
        `${label}: timed out waiting for ${method} ${url} (attempt ${attempt}).\n` +
        `Diagnosis (browser-side observation only): ${diagnosis}\n` +
        `Browser events for ${method} ${url} this attempt: ${JSON.stringify(matched)}\n` +
        `All browser network activity this attempt: ${JSON.stringify(recent)}`);
    }
  };

  const V = viewport.label;
  const getJson = (p) => page.evaluate(
    (u) => fetch(u, { credentials: "same-origin" }).then((r) => r.json()), p);
  const leagueCount = async () =>
    ((await getJson("/api/setup/hierarchy")).leagues || []).length;
  const openSetup = async () => {
    await page.click('.tab[data-tab="setup"]');
    await enterSetupHierarchy(page);
    await page.waitForSelector("#content > *", { timeout: 10000 });
  };
  const demoTitle = () => page.$eval("#demo-btn", (b) => b.getAttribute("title"));
  const demoAria = () => page.$eval("#demo-btn", (b) => b.getAttribute("aria-label"));
  const waitDemoTitle = (re) => page.waitForFunction(
    (pat) => {
      const b = document.querySelector("#demo-btn");
      return b && new RegExp(pat).test(b.getAttribute("title") || "");
    }, re, { timeout: 10000 });
  const runFromMenu = async (action, word, route) => {
    // Open the header menu, choose the action, type the confirmation word, and
    // wait for the atomic lifecycle POST to succeed.
    await page.click("#demo-btn");
    await page.click(`[data-demo-action="${action}"]`);
    await page.waitForSelector(".modal.danger #demo-confirm-input", { timeout: 10000 });
    if (!(await page.$eval("[data-demo-confirm]", (b) => b.disabled))) {
      throw new Error(`[${V}] ${action} was enabled before typing ${word}`);
    }
    await page.fill("#demo-confirm-input", word);
    await page.waitForFunction(
      () => !document.querySelector("[data-demo-confirm]").disabled, null, { timeout: 5000 });
    const resp = await clickAndAwaitResponse(
      `${base}${route}`, "POST", () => page.click("[data-demo-confirm]"),
      `[${V}] ${action}`);
    if (resp.status() !== 200) throw new Error(`[${V}] ${action} returned non-200`);
    await page.waitForSelector(".modal", { state: "detached", timeout: 10000 });
  };

  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });
    if ((await login(page, "admin", "demo")) !== 200) {
      throw new Error(`[${V}] demo admin login failed`);
    }
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });

    // (1) First launch: a clean slate. Setup opens on "Start your competition", the
    // header control is present and offers Load, and no program exists yet.
    await openSetup();
    await page.waitForSelector(".start-league", { timeout: 10000 });
    await page.waitForFunction(
      () => { const m = document.querySelector("#demo-menu"); return m && m.offsetParent !== null; },
      null, { timeout: 10000 });
    if (!/Load demo data/.test(await demoTitle()) || !/Load demo data/.test(await demoAria())) {
      throw new Error(`[${V}] empty demo header control is not labeled "Load demo data"`);
    }
    if (await leagueCount() !== 0) throw new Error(`[${V}] first launch was not blank`);

    // (2) Load from the empty-state card: the sample dataset appears, the card
    // is replaced by league trees, and the header flips to Reset.
    const loadResp = await clickAndAwaitResponse(
      `${base}/api/demo/load`, "POST", () => page.click("[data-demo-load]"),
      `[${V}] card Load`);
    if (loadResp.status() !== 200) throw new Error(`[${V}] card Load returned non-200`);
    await page.waitForSelector(".start-league", { state: "detached", timeout: 10000 });
    if (await leagueCount() === 0) throw new Error(`[${V}] Load did not build the dataset`);
    await waitDemoTitle("Reset demo data").catch(() => {
      throw new Error(`[${V}] header did not flip to "Reset demo data" after Load`);
    });

    // The sample dataset demonstrates the client's own naming convention
    // (issue #245): Gold/Silver/Diamond are DIVISIONS within a League
    // ("Adult League"), never Leagues themselves.
    const hv = await getJson("/api/v2/setup/hierarchy");
    const adultLeague = hv.programs.flatMap((p) => p.seasons).flatMap((s) => s.leagues || [])
      .find((lv) => lv.name === "Adult League");
    if (!adultLeague) throw new Error(`[${V}] demo data has no "Adult League" League`);
    const adultDivisionNames = new Set((adultLeague.divisions || []).map((d) => d.name));
    for (const name of ["Gold", "Silver", "Diamond"]) {
      if (!adultDivisionNames.has(name)) {
        throw new Error(`[${V}] "Adult League" is missing its "${name}" Division (got ${
          JSON.stringify([...adultDivisionNames])})`);
      }
    }
    const leagueNames = hv.programs.flatMap((p) => p.seasons).flatMap((s) => s.leagues || []).map((lv) => lv.name);
    if (leagueNames.some((n) => ["Gold", "Silver", "Diamond"].includes(n))) {
      throw new Error(`[${V}] Gold/Silver/Diamond appear as League names, not just Division names (got ${
        JSON.stringify(leagueNames)})`);
    }
    // The populated Setup exposes icon row actions with accessible labels — the
    // "Remove from season" control is an icon carrying its intent in title/aria.
    const remove = await page.$("[data-reg-remove]");
    if (remove) {
      if (!/Remove from season/.test(await remove.getAttribute("title"))
          || !/Remove/.test(await remove.getAttribute("aria-label"))) {
        throw new Error(`[${V}] the Remove-from-season icon lacks its tooltip/label`);
      }
    }

    // (3) Clear from the header menu (typed CLEAR): back to the blank slate.
    await runFromMenu("clear", "CLEAR", "/api/demo/clear");
    await page.waitForSelector(".start-league", { timeout: 10000 });
    if (await leagueCount() !== 0) throw new Error(`[${V}] Clear did not empty the demo`);
    await waitDemoTitle("Load demo data").catch(() => {
      throw new Error(`[${V}] header did not return to "Load demo data" after Clear`);
    });

    // Re-load so Reset has a populated dataset to rebuild from.
    const reload = await clickAndAwaitResponse(
      `${base}/api/demo/load`, "POST", () => page.click("[data-demo-load]"),
      `[${V}] second Load`);
    if (reload.status() !== 200) throw new Error(`[${V}] second Load returned non-200`);
    await page.waitForSelector(".start-league", { state: "detached", timeout: 10000 });

    // (4) Reset from the header menu (typed RESET): the canonical dataset.
    await runFromMenu("reset", "RESET", "/api/demo/reset");
    if (await leagueCount() === 0) throw new Error(`[${V}] Reset did not rebuild the dataset`);
    await waitDemoTitle("Reset demo data").catch(() => {
      throw new Error(`[${V}] header is not "Reset demo data" over a populated dataset`);
    });

    // (5) REGRESSION, the browser-shard-3 flake this journey used to produce:
    //     A STALE render() MUST NOT DESTROY A MODAL A NEWER render() PAINTED.
    //
    // What used to happen, non-deterministically, in step (4) above. render()
    // is `async`, blanks #content SYNCHRONOUSLY and only then awaits its fetch
    // chain, and almost every caller fires it without awaiting. So the Load
    // click's render (app.js:11277 -> afterDemoLifecycleChange -> render())
    // could still be mid-fetch when line 256's `.start-league` detached
    // barrier was satisfied -- that barrier is met by the SYNCHRONOUS BLANK,
    // not by the paint, so the journey walked on with a render still in
    // flight. Opening the header menu then started a SECOND render
    // (app.js:13206, also un-awaited). Whichever landed last won. When the
    // older one landed last, `c.innerHTML = viewHtml` plus the modal rebuild
    // wiped the filled, enabled confirm modal and replaced it with a fresh one
    // -- demoConfirmModalHtml always emits an EMPTY #demo-confirm-input and a
    // `disabled` [data-demo-confirm]. Landing in the window between the
    // enable check and the click left Playwright waiting on `enabled`
    // forever, and the lifecycle POST was never issued at all: the CI symptom
    // was a bare `page.waitForResponse: Timeout 30000ms exceeded`.
    //
    // Here that interleaving is FORCED rather than raced, so this leg is
    // deterministic where the flake was not: exactly ONE /api/demo/overview --
    // the Load-triggered render's own -- is held until the journey has typed
    // RESET and seen the button go enabled, then released. In CI nothing holds
    // it; it is simply the slower of two in-flight renders. Nothing else is
    // faked: same server, same app.js, same click path.
    //
    // The fix under test is app.js's `renderPass` token: a superseded render
    // stands down at its DOM boundaries instead of painting. Revert it and
    // this leg fails with the modal reading {disabled:true, inputValue:""}.
    await runFromMenu("clear", "CLEAR", "/api/demo/clear");
    await page.waitForSelector(".start-league", { timeout: 10000 });

    let holdOverview = false;
    let releaseOverview = null;
    const overviewHeld = new Promise((r) => { releaseOverview = r; });
    await page.route("**/api/demo/overview", async (route) => {
      if (!holdOverview) return route.continue();
      holdOverview = false;            // hold exactly one: the Load render's
      await overviewHeld;
      return route.continue();
    });

    holdOverview = true;
    const staleLoad = await clickAndAwaitResponse(
      `${base}/api/demo/load`, "POST", () => page.click("[data-demo-load]"),
      `[${V}] stale-render Load`);
    if (staleLoad.status() !== 200) {
      throw new Error(`[${V}] stale-render Load returned non-200`);
    }
    // The same barrier step (4) relies on -- satisfied by the blank, with that
    // render still awaiting the overview we are holding.
    await page.waitForSelector(".start-league", { state: "detached", timeout: 10000 });

    await page.click("#demo-btn");
    await page.click('[data-demo-action="reset"]');
    await page.waitForSelector(".modal.danger #demo-confirm-input", { timeout: 10000 });
    await page.fill("#demo-confirm-input", "RESET");
    await page.waitForFunction(
      () => !document.querySelector("[data-demo-confirm]").disabled, null, { timeout: 5000 });

    // Release the stale render and let it run all the way to its DOM
    // boundary. networkidle (not a fixed pause) is the settle signal: it means
    // that render's whole fetch chain has finished, so it has already either
    // painted or stood down -- there is nothing further in flight to land.
    releaseOverview();
    await page.waitForLoadState("networkidle");

    const modalState = await page.evaluate(() => {
      const b = document.querySelector("[data-demo-confirm]");
      const i = document.querySelector("#demo-confirm-input");
      return { present: !!b && !!i, disabled: b ? b.disabled : null,
               value: i ? i.value : null };
    });
    if (!modalState.present) {
      throw new Error(`[${V}] a stale render() destroyed the confirm modal outright`);
    }
    if (modalState.disabled || modalState.value !== "RESET") {
      throw new Error(
        `[${V}] a stale render() rebuilt the confirm modal and discarded the typed `
        + `confirmation: expected {disabled:false, value:"RESET"}, got `
        + `{disabled:${modalState.disabled}, value:${JSON.stringify(modalState.value)}}`);
    }
    // And the click must still dispatch: the whole point is that the POST
    // actually leaves the browser, which is what the flake denied.
    const staleReset = await clickAndAwaitResponse(
      `${base}/api/demo/reset`, "POST", () => page.click("[data-demo-confirm]"),
      `[${V}] reset after a stale render landed`);
    if (staleReset.status() !== 200) {
      throw new Error(`[${V}] reset after a stale render returned non-200`);
    }
    await page.waitForSelector(".modal", { state: "detached", timeout: 10000 });
    if (await leagueCount() === 0) {
      throw new Error(`[${V}] reset after a stale render did not rebuild the dataset`);
    }
    await page.unroute("**/api/demo/overview");

    if (errors.length) throw new Error(`[${V}] console/page errors:\n${errors.join("\n")}`);
    console.log(`[${V}] OK — blank → load → clear → reset, header state-aware, `
      + `and a stale render() cannot destroy the confirm modal.`);
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
    console.log("Demo lifecycle browser journey passed.");
  } catch (error) {
    console.error("Demo lifecycle browser journey FAILED.");
    console.error(error && error.message ? error.message : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
