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

async function waitUntil(predicate, message, timeoutMs = 15000) {
  const deadline = Date.now() + timeoutMs;
  while (!predicate()) {
    if (Date.now() > deadline) throw new Error(message);
    await new Promise((r) => setTimeout(r, 20));
  }
}

// --- The #215 per-pass render() completion barrier -----------------------
//
// The stale-render leg below holds one response so that a SPECIFIC render()
// pass is suspended mid-flight while a newer pass paints over it. Before that
// leg may assert anything, it has to know that THAT pass has ENDED — either
// stood down at a supersession guard or run all the way through its remaining
// reads and its paint.
//
// Nothing at the document level can say that, and this barrier exists because
// the obvious candidates are all wrong here:
//   * `networkidle` is a LOAD-LIFECYCLE flag, not a settle signal. Once the
//     document has fired it, waitForLoadState returns immediately — with the
//     held request still held. It proved nothing at all in this leg.
//   * every delivery-shaped barrier (waitForResponse, route.fulfill()
//     returning, a `requestfinished` count) fires strictly BEFORE the page's
//     own `await fetch(...)` continuation resumes — let alone before whatever
//     that continuation then goes on to do, which in a build without the fix
//     is one or more further real reads.
//   * an in-flight-request counter or a microtask flush is a quiet-period
//     detector, i.e. a fixed delay in costume: it cannot tell "the pass stood
//     down" from "the pass has not dispatched its next read yet".
//   * a MutationObserver watches for an EFFECT that the FIXED build must
//     never produce, so on the build under test it could only ever time out.
//
// So the barrier observes the INVOCATION, not any effect of it. app.js is a
// classic script, so its top-level `async function render()` is a writable own
// property of window and every one of its internal call sites resolves through
// that binding. Wrapping it here — from the test, with the shipped app left
// untouched — numbers each invocation and stamps the number in a `finally`,
// which runs exactly when that invocation's promise settles: after a guard
// `return`, or after the last line of a full paint. The wrapper mentions
// nothing the fix introduced (no `renderPass`, no `myRenderPass`), so it holds
// identically on a build with the fix reverted, where the superseded pass
// instead reads on and repaints.
//
// Identifying WHICH pass is held needs no guesswork and no heuristic: a pass
// that is suspended on the held response is, by definition, in flight at the
// moment the response is held. So the leg snapshots the in-flight set inside
// its own route handler — necessarily before the release — and the barrier
// waits for every id in that snapshot. The held pass is provably a member, so
// the barrier cannot resolve before it has ended. (The snapshot is not always
// a singleton: an unrelated handler's re-render that is still in flight can be
// caught in it too. Waiting for those as well is strictly stronger and costs
// nothing; they are not blocked on anything this leg holds.)
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
    const resp = page.waitForResponse((r) =>
      r.url() === `${base}${route}` && r.request().method() === "POST");
    await page.click("[data-demo-confirm]");
    if ((await resp).status() !== 200) throw new Error(`[${V}] ${action} returned non-200`);
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
    const loadResp = page.waitForResponse((r) =>
      r.url() === `${base}/api/demo/load` && r.request().method() === "POST");
    await page.click("[data-demo-load]");
    if ((await loadResp).status() !== 200) throw new Error(`[${V}] card Load returned non-200`);
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
    const reload = page.waitForResponse((r) =>
      r.url() === `${base}/api/demo/load` && r.request().method() === "POST");
    await page.click("[data-demo-load]");
    if ((await reload).status() !== 200) throw new Error(`[${V}] second Load returned non-200`);
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
    // click's render (the [data-demo-load] handler ->
    // afterDemoLifecycleChange -> render()) could still be mid-fetch when the
    // re-load's `.start-league` detached barrier above was satisfied -- that
    // barrier is met by the SYNCHRONOUS BLANK, not by the paint, so the
    // journey walked on with a render still in flight. Choosing "reset" from
    // the header menu then started a SECOND render (the #demo-dropdown
    // handler, also un-awaited). Whichever landed last won. When the older one
    // landed last, `c.innerHTML = viewHtml` plus the modal rebuild wiped the
    // filled, enabled confirm modal and replaced it with a fresh one --
    // demoConfirmModalHtml always emits an EMPTY #demo-confirm-input and a
    // `disabled` [data-demo-confirm]. Landing in the window between the enable
    // check and the click left Playwright waiting on `enabled` forever, and
    // the lifecycle POST was never issued at all.
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
    // this leg fails with the modal reading {disabled:true, value:""}.
    await runFromMenu("clear", "CLEAR", "/api/demo/clear");
    await page.waitForSelector(".start-league", { timeout: 10000 });

    let holdOverview = false;
    let heldPasses = null;
    let releaseOverview = null;
    const overviewHeld = new Promise((r) => { releaseOverview = r; });
    // Armed BEFORE the pass that will be held is started, because the barrier
    // observes that invocation from its entry (see armRenderPassWatch).
    await armRenderPassWatch(page);
    await page.route("**/api/demo/overview", async (route) => {
      if (!holdOverview) return route.continue();
      holdOverview = false;            // hold exactly one: the Load render's
      // Armed here, with the request still held: whichever pass is suspended
      // on it is in flight at this instant and so is in this snapshot.
      heldPasses = await renderPassesInFlight(page);
      await overviewHeld;
      return route.continue();
    });

    holdOverview = true;
    const [staleLoad] = await Promise.all([
      page.waitForResponse((r) =>
        r.url() === `${base}/api/demo/load` && r.request().method() === "POST"),
      page.click("[data-demo-load]"),
    ]);
    if (staleLoad.status() !== 200) {
      throw new Error(`[${V}] stale-render Load returned non-200`);
    }
    // The same barrier step (4) relies on -- satisfied by the blank, with that
    // render still awaiting the overview we are holding.
    await page.waitForSelector(".start-league", { state: "detached", timeout: 10000 });

    // Fail here, not at the barrier, if the hold never happened at all.
    await waitUntil(() => heldPasses !== null,
      `[${V}] /api/demo/overview was never held, so no render pass was superseded`);
    if (!heldPasses.length) {
      throw new Error(`[${V}] the held /api/demo/overview belonged to no render() `
        + `pass, so this leg is not exercising a superseded render at all`);
    }

    await page.click("#demo-btn");
    await page.click('[data-demo-action="reset"]');
    await page.waitForSelector(".modal.danger #demo-confirm-input", { timeout: 10000 });
    await page.fill("#demo-confirm-input", "RESET");
    await page.waitForFunction(
      () => !document.querySelector("[data-demo-confirm]").disabled, null, { timeout: 5000 });

    // Release the stale render and wait for THAT PASS to end. This is the
    // positive, per-pass barrier armed above: it resolves when the held
    // invocation's own promise settles -- whether it stood down at its DOM
    // guard (the fix) or read on and repainted (without it). It is indifferent
    // to WHETHER the pass does anything, which is the property this needs:
    // with the fix the pass's completion is unobservable by construction, and
    // without it the completion only arrives after another real read.
    const stillSuspended = (await renderPassesInFlight(page))
      .filter((id) => heldPasses.includes(id));
    releaseOverview();
    const stalePassWaitMs = await awaitRenderPassesEnd(page, heldPasses, `[${V}]`);
    const barrier = `the superseded pass (render #${(stillSuspended.length
      ? stillSuspended : heldPasses).join(", #")}, suspended on the held overview `
      + `right up to the release) had already ended ${stalePassWaitMs}ms after `
      + `release, so this is what it left behind, not a race`;

    const modalState = await page.evaluate(() => {
      const b = document.querySelector("[data-demo-confirm]");
      const i = document.querySelector("#demo-confirm-input");
      return { present: !!b && !!i, disabled: b ? b.disabled : null,
               value: i ? i.value : null };
    });
    if (!modalState.present) {
      throw new Error(`[${V}] a stale render() destroyed the confirm modal `
        + `outright — ${barrier}`);
    }
    if (modalState.disabled || modalState.value !== "RESET") {
      throw new Error(
        `[${V}] a stale render() rebuilt the confirm modal and discarded the typed `
        + `confirmation: expected {disabled:false, value:"RESET"}, got `
        + `{disabled:${modalState.disabled}, value:${JSON.stringify(modalState.value)}} `
        + `— ${barrier}`);
    }
    // And the click must still dispatch: the whole point is that the lifecycle
    // POST actually happens, which is what the flake denied.
    const [staleReset] = await Promise.all([
      page.waitForResponse((r) =>
        r.url() === `${base}/api/demo/reset` && r.request().method() === "POST"),
      page.click("[data-demo-confirm]"),
    ]);
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
