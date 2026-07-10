// Guided Initial Setup browser journey (#174 PR D).
//
// A fresh production installation with its first League Admin signs in, lands on
// the server-derived Initial Setup screen, can leave and resume, and deep-links
// into the existing Setup drawer. Runs at desktop and 390px phone widths.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const fs = require("fs");
const http = require("http");
const os = require("os");
const path = require("path");

const BIND_HOST = "127.0.0.1";
const BROWSER_HOST = "localhost";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
const ADMIN_USER = "onboarding-admin";
const ADMIN_PASSWORD = "fixture-onboarding-password";
const PHASE = process.env.ONBOARDING_PHASE || "full";
const VIEWPORT_FILTER = process.env.ONBOARDING_VIEWPORT || "all";
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8133 },
  { label: "phone", width: 390, height: 844, port: 8134 },
].filter((viewport) => VIEWPORT_FILTER === "all" || viewport.label === VIEWPORT_FILTER);

if (!VIEWPORTS.length) {
  throw new Error(`Unknown ONBOARDING_VIEWPORT '${VIEWPORT_FILTER}'.`);
}

function waitForServer(url, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const attempt = () => {
      const req = http.get(url, (res) => {
        res.resume();
        resolve();
      });
      req.setTimeout(2000, () => req.destroy(new Error("request timed out")));
      req.on("error", () => {
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
    server.once("exit", () => {
      clearTimeout(escalate);
      resolve();
    });
    server.kill("SIGTERM");
  });
}

async function checkViewport(browser, viewport) {
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "hockey-onboarding-"));
  const databasePath = path.join(tempDir, "client.sqlite");
  const browserBase = `http://${BROWSER_HOST}:${viewport.port}`;
  const probeBase = `http://${BIND_HOST}:${viewport.port}`;
  const server = spawn(
    process.env.PYTHON || "python3",
    ["-u", "-m", "hockey_scheduler.web.server", "--host", BIND_HOST,
      "--port", String(viewport.port)],
    {
      cwd: BACKEND_DIR,
      env: {
        ...process.env,
        APP_MODE: "production",
        DATABASE_URL: databasePath,
        BOOTSTRAP_ADMIN_USER: ADMIN_USER,
        BOOTSTRAP_ADMIN_PASSWORD: ADMIN_PASSWORD,
        INITIAL_SETUP_CODE: "",
      },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  let serverOutput = "";
  server.stdout.on("data", (data) => { serverOutput += data.toString(); });
  server.stderr.on("data", (data) => { serverOutput += data.toString(); });

  const context = await browser.newContext({
    viewport: { width: viewport.width, height: viewport.height },
  });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(`[pageerror] ${error.message}`));
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(`[console] ${message.text()}`);
  });

  try {
    await waitForServer(`${probeBase}/api/health`, READY_TIMEOUT_MS);
    await page.goto(browserBase, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#login-screen:not([hidden])", { timeout: 10000 });
    await page.fill("#login-user", ADMIN_USER);
    await page.fill("#login-pass", ADMIN_PASSWORD);
    await page.click("#login-form button[type=submit]");

    await page.waitForSelector('body[data-view="onboarding"]', { timeout: 10000 });
    if (PHASE === "route") {
      console.log(`[${viewport.label}] OK — routed to Initial Setup.`);
      return;
    }

    await page.getByRole("heading", { name: "Complete the league foundation" }).waitFor();
    await page.waitForSelector('[data-onboarding-step="organization"].current');
    await page.waitForSelector('.tab[data-tab="onboarding"].active');
    await page.getByText("Progress is recalculated from saved records", { exact: false }).waitFor();
    if (PHASE === "content") {
      console.log(`[${viewport.label}] OK — rendered the expected Initial Setup content.`);
      return;
    }

    const status = await page.evaluate(async () => {
      const response = await fetch("/api/onboarding/status", {
        credentials: "same-origin",
        cache: "no-store",
      });
      return response.json();
    });
    if (status.ready_to_schedule !== false
        || !status.blocking.some((item) => item.code === "no_organization")) {
      throw new Error(`[${viewport.label}] unexpected empty-install status: ${JSON.stringify(status)}`);
    }
    if (PHASE === "landing") {
      console.log(`[${viewport.label}] OK — status matches the fresh persisted installation.`);
      return;
    }

    // Continue later is a real escape hatch, not a trap.
    await page.click('[data-onboarding-goto="dashboard"]');
    await page.waitForSelector('body[data-view="dashboard"]', { timeout: 10000 });

    // The visible nav entry resumes from persisted records.
    await page.click('.tab[data-tab="onboarding"]');
    await page.waitForSelector('body[data-view="onboarding"]', { timeout: 10000 });
    await page.waitForSelector('[data-onboarding-step="organization"].current');
    if (PHASE === "resume") {
      console.log(`[${viewport.label}] OK — left and resumed Initial Setup.`);
      return;
    }

    // The Fix action reuses the existing Setup drawer instead of a parallel form.
    await page.click('[data-onboarding-drawer="organization"]');
    await page.waitForSelector('body[data-view="setup"]', { timeout: 10000 });
    await page.waitForSelector("#f-org", { timeout: 10000 });

    if (errors.length) {
      throw new Error(`[${viewport.label}] console/page errors:\n${errors.join("\n")}`);
    }
    console.log(`[${viewport.label}] OK — auto-routed, resumable, deep-linked to Setup.`);
  } catch (error) {
    throw new Error(`${error.message}\n--- production server output ---\n${serverOutput}`);
  } finally {
    await context.close();
    await stopServer(server);
    fs.rmSync(tempDir, { recursive: true, force: true });
  }
}

async function main() {
  let browser;
  try {
    browser = await chromium.launch(
      process.env.SMOKE_CHROMIUM_PATH
        ? { executablePath: process.env.SMOKE_CHROMIUM_PATH }
        : {},
    );
    for (const viewport of VIEWPORTS) await checkViewport(browser, viewport);
    console.log(`Guided Initial Setup browser journey passed (${VIEWPORT_FILTER}/${PHASE}).`);
  } catch (error) {
    console.error(`Guided Initial Setup browser journey FAILED (${VIEWPORT_FILTER}/${PHASE}).`);
    console.error(error && error.message ? error.message : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
