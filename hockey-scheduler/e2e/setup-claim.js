// Production installation-claim browser check (#174 PR B).
//
// For desktop and phone viewports, start a fresh production server against its
// own durable SQLite file, claim it through /setup, and verify the one-time form
// disappears after success. Fails on browser console/page errors.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const fs = require("fs");
const http = require("http");
const os = require("os");
const path = require("path");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
const SETUP_CODE = "browser-fixture-setup-code-7d4318b20f5e4cfa";
const CLIENT_PASSWORD = "client-owned-password";
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8131 },
  { label: "phone", width: 390, height: 844, port: 8132 },
];

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
        if (Date.now() > deadline) {
          reject(new Error(`Server never came up at ${url}`));
        } else {
          setTimeout(attempt, 250);
        }
      });
    };
    attempt();
  });
}

function stopServer(server) {
  return new Promise((resolve) => {
    if (server.exitCode !== null || server.signalCode !== null) {
      resolve();
      return;
    }
    const escalate = setTimeout(() => server.kill("SIGKILL"), 3000);
    server.once("exit", () => {
      clearTimeout(escalate);
      resolve();
    });
    server.kill("SIGTERM");
  });
}

async function checkViewport(browser, vp) {
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "hockey-claim-"));
  const databasePath = path.join(tempDir, "client.sqlite");
  const base = `http://${HOST}:${vp.port}`;
  const server = spawn(
    process.env.PYTHON || "python3",
    ["-u", "-m", "hockey_scheduler.web.server", "--host", HOST,
      "--port", String(vp.port)],
    {
      cwd: BACKEND_DIR,
      env: {
        ...process.env,
        APP_MODE: "production",
        DATABASE_URL: databasePath,
        INITIAL_SETUP_CODE: SETUP_CODE,
        // The browser-claim test must never be pre-empted by an inherited
        // emergency operations bootstrap configuration.
        BOOTSTRAP_ADMIN_USER: "",
        BOOTSTRAP_ADMIN_PASSWORD: "",
        // Required in production (#159 review finding 4): serve() now fails
        // closed at startup without it rather than falling back to an
        // unkeyed context-epoch hash. Test-only value, well above the
        // 32-byte floor; never a real deployment secret.
        HS_CONTEXT_EPOCH_SECRET:
          "e2e-harness-context-epoch-secret-do-not-use-in-production",
      },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  let serverOutput = "";
  server.stdout.on("data", (d) => { serverOutput += d.toString(); });
  server.stderr.on("data", (d) => { serverOutput += d.toString(); });

  const context = await browser.newContext({
    viewport: { width: vp.width, height: vp.height },
  });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(`[pageerror] ${error.message}`));
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(`[console] ${message.text()}`);
  });

  try {
    await waitForServer(`${base}/api/bootstrap/status`, READY_TIMEOUT_MS);
    await page.goto(`${base}/setup`, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#claim-form:not([hidden])", { timeout: 10000 });
    await page.getByRole("heading", { name: "Claim this installation" }).waitFor();

    await page.fill("#setup-code", SETUP_CODE);
    await page.fill("#username", `client-admin-${vp.label}`);
    await page.fill("#password", CLIENT_PASSWORD);
    await page.fill("#password-confirm", CLIENT_PASSWORD);

    const responsePromise = page.waitForResponse((response) =>
      response.url() === `${base}/api/bootstrap/claim`
      && response.request().method() === "POST");
    await page.click("#claim-button");
    const response = await responsePromise;
    if (response.status() !== 200) {
      throw new Error(`[${vp.label}] claim returned HTTP ${response.status()}`);
    }

    await page.waitForURL(`${base}/`, { timeout: 10000 });
    await page.goto(`${base}/setup`, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#sign-in-link:not([hidden])", { timeout: 10000 });
    if (await page.locator("#claim-form:not([hidden])").count()) {
      throw new Error(`[${vp.label}] claim form remained visible after claim`);
    }
    const status = await page.evaluate(async () => {
      const response = await fetch("/api/bootstrap/status", { cache: "no-store" });
      return response.json();
    });
    if (status.claim_available !== false || status.reason !== "already_claimed") {
      throw new Error(`[${vp.label}] unexpected post-claim status: ${JSON.stringify(status)}`);
    }

    const browserStorage = await page.evaluate(() => JSON.stringify({
      local: { ...localStorage },
      session: { ...sessionStorage },
    }));
    if (browserStorage.includes(SETUP_CODE) || browserStorage.includes(CLIENT_PASSWORD)) {
      throw new Error(`[${vp.label}] a submitted secret appeared in browser storage`);
    }
    if (errors.length) {
      throw new Error(`[${vp.label}] console/page errors:\n${errors.join("\n")}`);
    }
    console.log(`[${vp.label}] OK — claimed fresh production install, form closed.`);
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
    for (const vp of VIEWPORTS) {
      await checkViewport(browser, vp);
    }
    console.log("Production claim browser test passed.");
  } catch (error) {
    console.error("Production claim browser test FAILED.");
    console.error(error && error.message ? error.message : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
