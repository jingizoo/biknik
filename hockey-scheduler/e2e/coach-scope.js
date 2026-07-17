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
const { chromium } = require("playwright");
const { spawn } = require("child_process");
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

    if (errors.length) {
      throw new Error(`[${L}] console/page errors:\n${errors.join("\n")}`);
    }
    console.log(`[${L}] OK — Team selector gated to Coach; coach created with team scope, then rebound to another team.`);
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
