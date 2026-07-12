// API-error-resilience browser journey.
//
// A proxy 5xx (e.g. Render's 502 "Bad Gateway" HTML page), an empty body, or a
// dropped connection returns a non-JSON response. The client must surface a
// clear, retryable error instead of throwing an uncaught SyntaxError and hanging
// — across POST actions, public GET reads, and startup bootstrap. Each scenario
// forces a non-JSON 502 via request interception and asserts recovery with zero
// uncaught page errors.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");

const HOST = "127.0.0.1";
const PORT = process.env.RESILIENCE_PORT ? Number(process.env.RESILIENCE_PORT) : 8185;
const BASE = `http://${HOST}:${PORT}`;
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
const BAD_GATEWAY = { status: 502, contentType: "text/html", body: "<html><body>502 Bad Gateway</body></html>" };

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

async function newPage(browser) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();
  const pageErrors = [];
  page.on("pageerror", (e) => pageErrors.push(e.message));
  return { context, page, pageErrors };
}

const login = (page, u, pw) => page.evaluate(async ([u, pw]) => (await fetch("/api/auth/login", {
  method: "POST", credentials: "same-origin",
  headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username: u, password: pw }),
})).status, [u, pw]);

// (1) A POST that hits a non-JSON 502 shows the drawer error and stays usable.
async function checkPostError(browser) {
  const { context, page, pageErrors } = await newPage(browser);
  try {
    await page.goto(BASE, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });
    if ((await login(page, "admin", "demo")) !== 200) throw new Error("admin login failed");
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });
    await page.waitForTimeout(600);
    await page.route("**/api/setup/organization", (r) => r.fulfill(BAD_GATEWAY));
    await page.locator('[data-onboarding-drawer="organization"]').first().click();
    await page.waitForSelector("#f-org", { timeout: 10000 });
    await page.fill("#f-org", "Sophia");
    await page.click('[data-drawer-submit="organization"]');
    await page.waitForSelector(".drawer-err", { timeout: 10000 });
    const err = await page.locator(".drawer-err").innerText();
    if (!/unavailable|could not/i.test(err)) throw new Error(`POST error not surfaced: ${err}`);
    if ((await page.locator("#f-org").count()) !== 1) throw new Error("drawer closed — not retryable");
    if (pageErrors.length) throw new Error(`uncaught page errors: ${pageErrors.join("; ")}`);
    console.log("[post] OK — 502 shows a retryable drawer error, no uncaught errors.");
  } finally { await context.close(); }
}

// (2) A public GET (guest schedule) that 502s shows the error + Retry UI.
async function checkPublicGetError(browser) {
  const { context, page, pageErrors } = await newPage(browser);
  try {
    await page.route("**/api/public/schedule", (r) => r.fulfill(BAD_GATEWAY));
    await page.addInitScript(() => { try { localStorage.setItem("hs_signed_out", "1"); } catch (_) {} });
    await page.goto(`${BASE}#public`, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#public-retry-btn", { timeout: 10000 });
    const banner = await page.locator("#public-content .banner.alert").innerText();
    if (!/could not load/i.test(banner)) throw new Error(`public error not surfaced: ${banner}`);
    if (pageErrors.length) throw new Error(`uncaught page errors: ${pageErrors.join("; ")}`);
    console.log("[public-get] OK — 502 shows error + Retry, no uncaught errors.");
  } finally { await context.close(); }
}

// (3) A bootstrap identity call that 502s shows an outage, not a bare sign-in.
async function checkBootstrapError(browser) {
  const { context, page, pageErrors } = await newPage(browser);
  try {
    await page.route("**/api/auth/roles", (r) => r.fulfill(BAD_GATEWAY));
    await page.goto(BASE, { waitUntil: "domcontentloaded" });
    await page.waitForFunction(() => {
      const e = document.getElementById("login-error");
      return e && !e.hidden && /unavailable/i.test(e.textContent || "");
    }, null, { timeout: 10000 });
    if (pageErrors.length) throw new Error(`uncaught page errors: ${pageErrors.join("; ")}`);
    console.log("[bootstrap] OK — startup 502 shows an outage message, not a silent sign-out.");
  } finally { await context.close(); }
}

async function main() {
  const server = spawn(
    process.env.PYTHON || "python3",
    ["-u", "-m", "hockey_scheduler.web.server", "--host", HOST, "--port", String(PORT)],
    { cwd: BACKEND_DIR, stdio: ["ignore", "pipe", "pipe"] });
  let serverOutput = "";
  server.stdout.on("data", (d) => { serverOutput += d.toString(); });
  server.stderr.on("data", (d) => { serverOutput += d.toString(); });

  let browser, failed = false;
  try {
    await waitForServer(`${BASE}/api/health`, READY_TIMEOUT_MS);
    browser = await chromium.launch(
      process.env.SMOKE_CHROMIUM_PATH ? { executablePath: process.env.SMOKE_CHROMIUM_PATH } : {});
    await checkPostError(browser);
    await checkPublicGetError(browser);
    await checkBootstrapError(browser);
    console.log("API-error-resilience browser journey passed.");
  } catch (err) {
    failed = true;
    console.error("API-error-resilience browser journey FAILED.");
    console.error(err && err.message ? err.message : err);
    console.error("--- server output ---\n" + serverOutput);
  } finally {
    if (browser) await browser.close();
    await stopServer(server);
  }
  process.exit(failed ? 1 : 0);
}

main();
