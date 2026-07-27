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

    if (errors.length) throw new Error(`[${V}] console/page errors:\n${errors.join("\n")}`);
    console.log(`[${V}] OK — blank → load → clear → reset, header state-aware.`);
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
