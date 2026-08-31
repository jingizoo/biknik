// Reset-demo visibility browser journey (#215 acceptance).
//
// The "Reset demo" header action must be presented ONLY to a League Admin in
// demo mode. This journey proves the two negative cases the safe-destructive
// journey doesn't, at desktop and 390px:
//   * in demo mode, a signed-in non-admin (a coach) does NOT see Reset demo,
//     while a League Admin does;
//   * in production mode, even a signed-in League Admin does NOT see it.
// The server-side authorization and production block are proven by the backend
// tests; this is the browser-visibility evidence #215 requires.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, demoPort: 8157, prodPort: 8167 },
  { label: "phone", width: 390, height: 844, demoPort: 8158, prodPort: 8168 },
];
const PROD_ADMIN = "prod_admin";
const PROD_PW = "a-real-production-password";

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

async function resetVisible(page) {
  // The demo data control is now a database-icon menu (#215); it is hidden
  // (the whole #demo-menu carries `hidden`) unless the signed-in user can
  // manage setup in demo mode.
  const menu = page.locator("#demo-menu");
  return (await menu.count()) > 0 && (await menu.isVisible());
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

async function checkViewport(browser, viewport) {
  // --- Demo mode: admin sees it, coach does not ---------------------------
  const demoBase = `http://${HOST}:${viewport.demoPort}`;
  const demo = startServer(viewport.demoPort, {});
  let demoOut = "";
  demo.stdout.on("data", (d) => { demoOut += d.toString(); });
  demo.stderr.on("data", (d) => { demoOut += d.toString(); });
  const { context: demoCtx, page: demoPage, errors: demoErrors } =
    await newPage(browser, viewport);
  try {
    await waitForServer(`${demoBase}/api/health`, READY_TIMEOUT_MS);
    await demoPage.goto(demoBase, { waitUntil: "domcontentloaded" });
    await demoPage.waitForSelector("#content > *", { timeout: 10000 });

    if ((await login(demoPage, "admin", "demo")) !== 200) {
      throw new Error(`[${viewport.label}] demo admin login failed`);
    }
    // The demo now boots as a clean slate (#215). Load the sample dataset so a
    // non-admin (coach) account exists to prove the negative case below; the
    // admin control is visible either way, but with data it reads "Reset".
    if ((await loadDemo(demoPage)) !== 200) {
      throw new Error(`[${viewport.label}] demo data load failed`);
    }
    await demoPage.reload({ waitUntil: "domcontentloaded" });
    await demoPage.waitForSelector("#content > *", { timeout: 10000 });
    await demoPage.waitForFunction(
      () => { const b = document.querySelector("#demo-menu"); return b && b.offsetParent !== null; },
      null, { timeout: 10000 });

    if ((await login(demoPage, "coach", "demo")) !== 200) {
      throw new Error(`[${viewport.label}] demo coach login failed`);
    }
    await demoPage.reload({ waitUntil: "domcontentloaded" });
    await demoPage.waitForSelector("#content > *", { timeout: 10000 });
    if (await resetVisible(demoPage)) {
      throw new Error(`[${viewport.label}] a non-admin (coach) was shown Reset demo`);
    }
    if (demoErrors.length) {
      throw new Error(`[${viewport.label}] demo console/page errors:\n${demoErrors.join("\n")}`);
    }
  } catch (error) {
    throw new Error(`${error.message}\n--- demo server output ---\n${demoOut}`);
  } finally {
    await demoCtx.close();
    await stopServer(demo);
  }

  // --- Production mode: even a League Admin does not see it ----------------
  const prodBase = `http://${HOST}:${viewport.prodPort}`;
  const prod = startServer(viewport.prodPort, {
    APP_MODE: "production",
    BOOTSTRAP_ADMIN_USER: PROD_ADMIN,
    BOOTSTRAP_ADMIN_PASSWORD: PROD_PW,
    // Required in production (#159 review finding 4): serve() now fails
    // closed at startup without it rather than falling back to an unkeyed
    // context-epoch hash. Test-only value, well above the 32-byte floor;
    // never a real deployment secret.
    HS_CONTEXT_EPOCH_SECRET:
      "e2e-harness-context-epoch-secret-do-not-use-in-production",
  });
  let prodOut = "";
  prod.stdout.on("data", (d) => { prodOut += d.toString(); });
  prod.stderr.on("data", (d) => { prodOut += d.toString(); });
  const { context: prodCtx, page: prodPage, errors: prodErrors } =
    await newPage(browser, viewport);
  try {
    await waitForServer(`${prodBase}/api/health`, READY_TIMEOUT_MS);
    await prodPage.goto(prodBase, { waitUntil: "domcontentloaded" });
    if ((await login(prodPage, PROD_ADMIN, PROD_PW)) !== 200) {
      throw new Error(`[${viewport.label}] production admin login failed`);
    }
    await prodPage.reload({ waitUntil: "domcontentloaded" });
    await prodPage.waitForSelector("#content > *", { timeout: 10000 });
    if (await resetVisible(prodPage)) {
      throw new Error(`[${viewport.label}] production presented Reset demo to an admin`);
    }
    if (prodErrors.length) {
      throw new Error(`[${viewport.label}] production console/page errors:\n${prodErrors.join("\n")}`);
    }
    console.log(`[${viewport.label}] OK — reset hidden for non-admin and in production.`);
  } catch (error) {
    throw new Error(`${error.message}\n--- production server output ---\n${prodOut}`);
  } finally {
    await prodCtx.close();
    await stopServer(prod);
  }
}

async function main() {
  let browser;
  try {
    browser = await chromium.launch(
      process.env.SMOKE_CHROMIUM_PATH ? { executablePath: process.env.SMOKE_CHROMIUM_PATH } : {});
    for (const viewport of VIEWPORTS) await checkViewport(browser, viewport);
    console.log("Reset-demo visibility browser journey passed.");
  } catch (error) {
    console.error("Reset-demo visibility browser journey FAILED.");
    console.error(error && error.message ? error.message : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
