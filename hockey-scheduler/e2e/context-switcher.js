// Active Program/Season context switcher browser journey (#159).
//
// The backend serves the authorized options (GET /api/context/options), the
// current selection (GET /api/context), and persistence (POST /api/context).
// This journey is the operator-UI half: at desktop AND 390px, the topbar
// switcher must
//   * render as a STATIC CHIP when there is only one selectable context (the
//     seeded single Program+Season), showing "Program · Season";
//   * become an interactive menu once a second Season exists, and persist a
//     pick through POST /api/context, reflect it on the button, and encode it
//     in a structured "#ctx=" URL hash;
//   * restore that saved selection on reload (deep-link restoration);
//   * flag an archived Season as read-only in the menu and on the button;
// all with zero console/page errors — exactly the "saved display context"
// contract the switcher advertises (existing screens are not filtered yet).
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8241 },
  { label: "phone", width: 390, height: 844, port: 8242 },
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

// A same-origin fetch run inside the page (carries the session cookie).
function apiPost(page, url, body) {
  return page.evaluate(async ([u, b]) => {
    const r = await fetch(u, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(b || {}),
    });
    return { status: r.status, json: await r.json().catch(() => ({})) };
  }, [url, body]);
}
function apiGet(page, url) {
  return page.evaluate(async (u) => {
    const r = await fetch(u, { credentials: "same-origin" });
    return { status: r.status, json: await r.json().catch(() => ({})) };
  }, url);
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

function loginAs(page, username) {
  return apiPost(page, "/api/auth/login", { username, password: "demo" });
}
// Reload and wait for the app shell (not the onboarding view) to settle.
async function reloadShell(page) {
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForSelector("#content > *", { timeout: 10000 });
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

    // Setup is done as the League Admin; the switcher itself is exercised as a
    // global VIEWER — also authorized for every Program/Season, but without the
    // League-Admin onboarding wizard that would take over #content.
    if ((await loginAs(page, "admin")).status !== 200) throw new Error(`[${L}] admin login failed`);
    if ((await apiPost(page, "/api/demo/load", {})).status !== 200) throw new Error(`[${L}] demo load failed`);
    const opts = (await apiGet(page, "/api/context/options")).json;
    const programId = opts.programs[0].id;

    // 1) One seeded Program + Season ⇒ the switcher is a STATIC CHIP for the viewer.
    if ((await loginAs(page, "viewer")).status !== 200) throw new Error(`[${L}] viewer login failed`);
    await reloadShell(page);
    await page.waitForFunction(() => {
      const w = document.getElementById("context-switcher");
      const b = document.getElementById("ctx-btn");
      return w && !w.hidden && b && b.textContent.trim().length > 0;
    }, null, { timeout: 10000 });
    const chip = await page.evaluate(() => {
      const b = document.getElementById("ctx-btn");
      return { text: b.textContent, disabled: b.disabled,
               isStatic: b.classList.contains("is-static") };
    });
    if (!chip.isStatic || !chip.disabled) throw new Error(`[${L}] single-option switcher should be a static chip`);
    if (!/Alpine Ice Hockey League/.test(chip.text) || !/Winter Season/.test(chip.text)) {
      throw new Error(`[${L}] chip missing Program·Season: ${chip.text}`);
    }

    // 2) Admin adds a second Season ⇒ the viewer's switcher becomes interactive.
    if ((await loginAs(page, "admin")).status !== 200) throw new Error(`[${L}] admin re-login failed`);
    const made = await apiPost(page, "/api/v2/setup/season", { program_id: programId, name: "Spring Cup" });
    if (made.status !== 200) throw new Error(`[${L}] create season failed (${made.status})`);
    const springId = made.json.id;
    if ((await loginAs(page, "viewer")).status !== 200) throw new Error(`[${L}] viewer re-login failed`);
    await reloadShell(page);
    await page.waitForFunction(() => {
      const b = document.getElementById("ctx-btn");
      return b && !b.disabled && !b.classList.contains("is-static");
    }, null, { timeout: 10000 });

    // 3) Open the menu; it explains itself and lists both Seasons.
    await page.click("#ctx-btn");
    await page.waitForFunction(() => {
      const dd = document.getElementById("ctx-dropdown");
      return dd && !dd.hidden;
    }, null, { timeout: 5000 });
    const menu = await page.evaluate(() => document.getElementById("ctx-dropdown").textContent);
    if (!/Saved display context/.test(menu)) throw new Error(`[${L}] menu missing the saved-display-context note`);
    if (!/Spring Cup/.test(menu) || !/Winter Season/.test(menu)) throw new Error(`[${L}] menu missing a Season: ${menu}`);

    // 4) Pick Spring Cup ⇒ persisted, reflected on the button, encoded in #ctx=.
    await page.click(`.ctx-item[data-ctx-s="${springId}"]`);
    await page.waitForFunction(() => {
      const b = document.getElementById("ctx-btn");
      return b && /Spring Cup/.test(b.textContent) && location.hash.indexOf("#ctx=") === 0;
    }, null, { timeout: 10000 });
    const persisted = (await apiGet(page, "/api/context")).json;
    if (persisted.season_id !== springId) throw new Error(`[${L}] POST did not persist Spring Cup (got ${persisted.season_id})`);

    // 5) Reload ⇒ the saved selection is restored (deep-link restoration).
    await reloadShell(page);
    await page.waitForFunction(() => {
      const b = document.getElementById("ctx-btn");
      return b && /Spring Cup/.test(b.textContent);
    }, null, { timeout: 10000 });

    // 6) Admin archives the selected Season ⇒ flagged read-only for the viewer.
    if ((await loginAs(page, "admin")).status !== 200) throw new Error(`[${L}] admin re-login (archive) failed`);
    const arch = await apiPost(page, `/api/v2/setup/seasons/${springId}/archive`, { reason: "done" });
    if (arch.status !== 200) throw new Error(`[${L}] archive failed (${arch.status})`);
    if ((await loginAs(page, "viewer")).status !== 200) throw new Error(`[${L}] viewer re-login (archive) failed`);
    await reloadShell(page);
    await page.waitForFunction(() => {
      const ro = document.querySelector("#ctx-btn .ctx-ro");
      return ro && /read-only/i.test(ro.textContent);
    }, null, { timeout: 10000 });
    await page.click("#ctx-btn");
    await page.waitForSelector(".ctx-item .ctx-badge", { timeout: 5000 });

    if (errors.length) throw new Error(`[${L}] console/page errors:\n${errors.join("\n")}`);
    console.log(`[${L}] OK — switcher chip→menu, persist + #ctx= hash, reload restore, archived read-only.`);
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
    console.log("Context-switcher browser journey passed.");
  } catch (error) {
    console.error("Context-switcher browser journey FAILED.");
    console.error(error && error.message ? error.message : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
