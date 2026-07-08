// Browser smoke gate for CI (#138). Boots the demo server, loads the app at
// desktop + phone viewports, walks a few core nav tabs as the zero-friction
// demo admin, and fails loudly on any console/page error or missing element.
// This is a smoke gate, not full E2E coverage — see issue #138's non-goals.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const path = require("path");
const http = require("http");

const HOST = "127.0.0.1";
const PORT = process.env.SMOKE_PORT ? Number(process.env.SMOKE_PORT) : 8123;
const BASE = `http://${HOST}:${PORT}`;
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const SERVER_READY_TIMEOUT_MS = 15000;

const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900 },
  { label: "phone", width: 390, height: 844 },
];

// Tabs visible to the demo's zero-friction League Admin auto-login — enough
// to catch a render-path regression without trying to cover every screen.
const CORE_TABS = ["dashboard", "games", "users", "public"];

function waitForServer(url, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  const perRequestTimeoutMs = 2000;
  return new Promise((resolve, reject) => {
    const attempt = () => {
      const req = http.get(url, (res) => {
        res.resume();
        resolve();
      });
      // A connection that's accepted but never answered (the server process
      // wedged between bind() and actually serving) would otherwise hang
      // this request forever and never reach the retry/deadline logic below.
      req.setTimeout(perRequestTimeoutMs, () => req.destroy(new Error("request timed out")));
      req.on("error", () => {
        if (Date.now() > deadline) {
          reject(new Error(`Server never came up at ${url}`));
        } else {
          setTimeout(attempt, 300);
        }
      });
    };
    attempt();
  });
}

async function settle(page) {
  // A tab click's render() is async; waiting on network idle (capped short,
  // since this app polls in the background and may never go fully idle)
  // gives it a real chance to finish before the next click, instead of a
  // blind sleep that can either race a slow render or over-wait a fast one.
  await page.waitForLoadState("networkidle", { timeout: 800 }).catch(() => {});
  await page.waitForTimeout(150);
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
  const ctx = await browser.newContext({
    viewport: { width: vp.width, height: vp.height },
  });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (err) => errors.push(`[pageerror] ${err.message}`));
  page.on("console", (msg) => {
    if (msg.type() === "error") errors.push(`[console] ${msg.text()}`);
  });

  await page.goto(BASE, { waitUntil: "domcontentloaded" });
  // Both the nav markup and #login-screen's `hidden` attribute are part of
  // the static shell from first paint, so neither is a reliable signal that
  // bootstrap()'s async auto-login actually finished. #content starts empty
  // in the shell and is only ever populated by a completed render() call —
  // wait for that instead of a fixed sleep, or tabs get clicked before the
  // session cookie is set and trigger spurious 401s on session-scoped fetches.
  await page.waitForSelector("#content > *", { timeout: 10000 });

  for (const tab of CORE_TABS) {
    const btn = page.locator(`button[data-tab="${tab}"]`);
    if ((await btn.count()) === 0) {
      throw new Error(`[${vp.label}] expected nav tab "${tab}" not found`);
    }
    await btn.click();
    await settle(page);
  }

  await ctx.close();
  if (errors.length) {
    throw new Error(`[${vp.label}] console/page errors:\n${errors.join("\n")}`);
  }
  console.log(`[${vp.label}] OK — walked ${CORE_TABS.length} tabs, zero console errors.`);
}

async function main() {
  const server = spawn(
    process.env.PYTHON || "python3",
    ["-u", "-m", "hockey_scheduler.web.server", "--host", HOST, "--port", String(PORT)],
    { cwd: BACKEND_DIR, stdio: ["ignore", "pipe", "pipe"] },
  );
  let serverOutput = "";
  server.stdout.on("data", (d) => { serverOutput += d.toString(); });
  server.stderr.on("data", (d) => { serverOutput += d.toString(); });

  let browser;
  let failed = false;
  try {
    await waitForServer(BASE, SERVER_READY_TIMEOUT_MS);
    // SMOKE_CHROMIUM_PATH is an escape hatch for environments with a
    // pre-installed browser under a non-standard path; CI leaves it unset
    // and gets whatever `playwright install` fetched for this package version.
    browser = await chromium.launch(
      process.env.SMOKE_CHROMIUM_PATH
        ? { executablePath: process.env.SMOKE_CHROMIUM_PATH }
        : {},
    );
    for (const vp of VIEWPORTS) {
      await checkViewport(browser, vp);
    }
    console.log("Smoke test passed.");
  } catch (err) {
    failed = true;
    console.error("Smoke test FAILED.");
    console.error(err && err.message ? err.message : err);
    console.error("--- demo server output ---");
    console.error(serverOutput);
  } finally {
    if (browser) await browser.close();
    await stopServer(server);
  }
  process.exit(failed ? 1 : 0);
}

main();
