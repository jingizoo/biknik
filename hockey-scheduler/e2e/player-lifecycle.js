// Operator Player deactivate/reactivate lifecycle browser journey (#270).
//
// The backend adds an audited, MANAGE_SETUP-gated active-state operation that
// is the supported roster exit — distinct from delete, preserving all history.
// This journey is the operator-UI half: at desktop AND 390px, a League Admin in
// Setup > Records
//   * builds a minimal chain and a Player through the real create drawers;
//   * clicks the Player row's Deactivate control and confirms a themed modal
//     whose text makes clear history is kept and this is not a delete;
//   * confirms — asserting the row now reads "inactive" and the control flips
//     to Reactivate;
//   * clicks Reactivate, confirms, and asserts the row is active again.
// Fails on any browser console/page error.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8261 },
  { label: "phone", width: 390, height: 844, port: 8262 },
];

function waitForServer(url, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const attempt = () => {
      const request = http.get(url, (r) => { r.resume(); resolve(); });
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

async function checkViewport(browser, viewport) {
  const base = `http://${HOST}:${viewport.port}`;
  const L = viewport.label;
  const server = spawn(
    process.env.PYTHON || "python3",
    ["-u", "-m", "hockey_scheduler.web.server", "--host", HOST, "--port", String(viewport.port)],
    { cwd: BACKEND_DIR, stdio: ["ignore", "pipe", "pipe"] });
  let out = "";
  server.stdout.on("data", (d) => { out += d.toString(); });
  server.stderr.on("data", (d) => { out += d.toString(); });

  const context = await browser.newContext({
    viewport: { width: viewport.width, height: viewport.height },
  });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(`[pageerror] ${e.message}`));
  page.on("console", (m) => { if (m.type() === "error") errors.push(`[console] ${m.text()}`); });

  const createViaDrawer = async (key, fields, url) => {
    await page.click(`.setup-card .sc-new[data-drawer="${key}"]`);
    await page.waitForSelector(".drawer[role=dialog]", { timeout: 10000 });
    for (const [id, value] of Object.entries(fields)) {
      const tag = await page.$eval(`#${id}`, (el) => el.tagName);
      if (tag === "SELECT") await page.selectOption(`#${id}`, value);
      else await page.fill(`#${id}`, value);
    }
    const resp = page.waitForResponse((r) =>
      r.url() === `${base}${url}` && r.request().method() === "POST");
    await page.click(`[data-drawer-submit="${key}"]`);
    const body = await (await resp).json();
    if (body.error) throw new Error(`[${L}] ${key} create failed: ${JSON.stringify(body.error)}`);
    await page.waitForFunction(() => !document.querySelector(".drawer[role=dialog]"),
      null, { timeout: 10000 });
    return body;
  };

  const rowText = (id) => page.evaluate((pid) => {
    const btn = document.querySelector(`[data-edit="player"][data-edit-id="${pid}"]`);
    const li = btn && btn.closest(".li");
    return li ? li.textContent : null;
  }, id);

  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });
    await page.click('.tab[data-tab="setup"]');
    await page.click('[data-setup-view="records"]');
    await page.waitForSelector(".setup-card", { timeout: 10000 });

    // Minimal chain + one Player, all through the real drawers.
    const org = await createViaDrawer("organization",
      { "f-org": "Life Org" }, "/api/v2/setup/organization");
    const program = await createViaDrawer("league",
      { "f-league": "Life Program", "f-league-org": org.id }, "/api/v2/setup/program");
    const season = await createViaDrawer("season",
      { "f-season-league": program.id, "f-season": "2026-27" }, "/api/v2/setup/season");
    const league = await createViaDrawer("level",
      { "f-level-season": season.id, "f-level": "Life League" }, "/api/v2/setup/league");
    await createViaDrawer("division",
      { "f-div-league": league.id, "f-div": "Gold" }, "/api/v2/setup/division");
    const team = await createViaDrawer("team",
      { "f-team-perm-league": league.id, "f-team": "Life Team" }, "/api/v2/setup/team");
    const player = await createViaDrawer("player", {
      "f-player-team": team.id, "f-player-name": "Rosa Ready",
      "f-player-position": "forward", "f-player-jersey": "17",
    }, "/api/v2/setup/player");

    const pid = player.id;
    const activeSel = `[data-player-active="${pid}"]`;
    // The row starts active (no "inactive" tag; the control offers Deactivate).
    if (/inactive/i.test(await rowText(pid))) {
      throw new Error(`[${L}] player unexpectedly starts inactive`);
    }
    const deactivateLabel = await page.getAttribute(activeSel, "aria-label");
    if (!/deactivate/i.test(deactivateLabel)) {
      throw new Error(`[${L}] expected a Deactivate control, got "${deactivateLabel}"`);
    }

    // Click Deactivate → a themed confirmation whose text makes clear the
    // history is kept and this is not a delete.
    await page.click(activeSel);
    await page.waitForSelector(".modal[role=dialog]", { timeout: 10000 });
    const modalText = await page.textContent(".modal .modal-body");
    if (!/history/i.test(modalText) || !/not a\s+delete/i.test(modalText)) {
      throw new Error(`[${L}] deactivate modal missing history/not-a-delete copy: "${modalText}"`);
    }
    const deResp = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/player/${pid}/active`
      && r.request().method() === "POST");
    await page.click("[data-player-active-confirm]");
    const deBody = await (await deResp).json();
    if (deBody.error) throw new Error(`[${L}] deactivate failed: ${JSON.stringify(deBody.error)}`);
    if (deBody.is_active !== false) throw new Error(`[${L}] player still active after deactivate`);

    // The row now reads "inactive" and the control flips to Reactivate.
    await page.waitForFunction((pid) => {
      const btn = document.querySelector(`[data-edit="player"][data-edit-id="${pid}"]`);
      const li = btn && btn.closest(".li");
      return li && /inactive/i.test(li.textContent);
    }, pid, { timeout: 10000 });
    const reLabel = await page.getAttribute(activeSel, "aria-label");
    if (!/reactivate/i.test(reLabel)) {
      throw new Error(`[${L}] expected a Reactivate control after deactivate, got "${reLabel}"`);
    }

    // Reactivate → confirm → the row is active again.
    await page.click(activeSel);
    await page.waitForSelector(".modal[role=dialog]", { timeout: 10000 });
    const reResp = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/player/${pid}/active`
      && r.request().method() === "POST");
    await page.click("[data-player-active-confirm]");
    const reBody = await (await reResp).json();
    if (reBody.error) throw new Error(`[${L}] reactivate failed: ${JSON.stringify(reBody.error)}`);
    if (reBody.is_active !== true) throw new Error(`[${L}] player still inactive after reactivate`);
    await page.waitForFunction((pid) => {
      const btn = document.querySelector(`[data-edit="player"][data-edit-id="${pid}"]`);
      const li = btn && btn.closest(".li");
      return li && !/inactive/i.test(li.textContent);
    }, pid, { timeout: 10000 });

    if (errors.length) throw new Error(`[${L}] console/page errors:\n${errors.join("\n")}`);
    console.log(`[${L}] OK — Player deactivated + reactivated in place, history kept.`);
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
    console.log("Player-lifecycle operator browser journey passed.");
  } catch (error) {
    console.error("Player-lifecycle operator browser journey FAILED.");
    console.error(error && error.message ? error.message : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
