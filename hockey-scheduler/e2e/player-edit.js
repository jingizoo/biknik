// Operator Player-edit drawer browser journey (#268 acceptance).
//
// The backend adds an audited, MANAGE_SETUP-gated in-place Player edit
// (test_player_edit.py). This journey is the operator-UI half: at desktop AND
// 390px, a League Admin in Setup > Records
//   * builds a minimal chain and a Player (with an email) through the real
//     create drawers;
//   * opens the Player row's pencil (edit) control and confirms the drawer
//     opens in EDIT mode — the Team select is present but LOCKED (reassignment
//     stays the separate "Move" action) and the name/email are prefilled from
//     the record (proving the operator read carries the email round-trip);
//   * corrects name, position, jersey, shooting hand, and email and Saves,
//     asserting the row reflects the new name and jersey with no id change;
//   * re-opens the drawer and confirms the new email prefilled — i.e. the edit
//     persisted and is readable only on this operator surface.
// Fails on any browser console/page error.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8251 },
  { label: "phone", width: 390, height: 844, port: 8252 },
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

  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });
    await page.click('.tab[data-tab="setup"]');
    await page.click('[data-setup-view="records"]');
    await page.waitForSelector(".setup-card", { timeout: 10000 });

    // Minimal chain + one Player with an email, all through the real drawers.
    const org = await createViaDrawer("organization",
      { "f-org": "Edit Org" }, "/api/v2/setup/organization");
    const program = await createViaDrawer("league",
      { "f-league": "Edit Program", "f-league-org": org.id }, "/api/v2/setup/program");
    const season = await createViaDrawer("season",
      { "f-season-league": program.id, "f-season": "2026-27" }, "/api/v2/setup/season");
    const league = await createViaDrawer("level",
      { "f-level-season": season.id, "f-level": "Edit League" }, "/api/v2/setup/league");
    await createViaDrawer("division",
      { "f-div-league": league.id, "f-div": "Gold" }, "/api/v2/setup/division");
    const team = await createViaDrawer("team",
      { "f-team-perm-league": league.id, "f-team": "Edit Team" }, "/api/v2/setup/team");
    const player = await createViaDrawer("player", {
      "f-player-team": team.id, "f-player-name": "Typo Name",
      "f-player-position": "forward", "f-player-jersey": "17",
      "f-player-email": "typo@example.com",
    }, "/api/v2/setup/player");

    // Open the edit drawer from the Player row's pencil control.
    await page.click(`[data-edit="player"][data-edit-id="${player.id}"]`);
    await page.waitForSelector(".drawer[role=dialog]", { timeout: 10000 });

    // Edit mode: heading says Edit, Team is present but LOCKED, name/email
    // prefilled from the record.
    const heading = await page.textContent(".drawer-title");
    if (!/edit/i.test(heading)) throw new Error(`[${L}] drawer not in edit mode: "${heading}"`);
    const teamDisabled = await page.$eval("#f-player-team", (el) => el.disabled);
    if (!teamDisabled) throw new Error(`[${L}] Team select should be locked on edit`);
    const nameVal = await page.$eval("#f-player-name", (el) => el.value);
    const emailVal = await page.$eval("#f-player-email", (el) => el.value);
    if (nameVal !== "Typo Name" || emailVal !== "typo@example.com") {
      throw new Error(`[${L}] edit drawer not prefilled: name="${nameVal}" email="${emailVal}"`);
    }

    // Correct name, position, jersey, shooting hand, and email; Save.
    await page.fill("#f-player-name", "Fixed Name");
    await page.selectOption("#f-player-position", "defense");
    await page.selectOption("#f-player-shoots", "L");
    await page.fill("#f-player-jersey", "8");
    await page.fill("#f-player-email", "fixed@example.com");
    const editResp = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/player/${player.id}/update`
      && r.request().method() === "POST");
    await page.click('[data-drawer-submit="player"]');
    const body = await (await editResp).json();
    if (body.error) throw new Error(`[${L}] edit failed: ${JSON.stringify(body.error)}`);
    if (body.id !== player.id) throw new Error(`[${L}] player id changed on edit`);
    await page.waitForFunction(() => !document.querySelector(".drawer[role=dialog]"),
      null, { timeout: 10000 });

    // The row reflects the corrected name + jersey.
    await page.waitForFunction(
      (id) => {
        const btn = document.querySelector(`[data-edit="player"][data-edit-id="${id}"]`);
        if (!btn) return false;
        const li = btn.closest(".li");
        return li && /Fixed Name/.test(li.textContent) && /#8/.test(li.textContent);
      }, player.id, { timeout: 10000 });

    // Re-open: the new email is prefilled (operator read carries it round-trip).
    await page.click(`[data-edit="player"][data-edit-id="${player.id}"]`);
    await page.waitForSelector(".drawer[role=dialog]", { timeout: 10000 });
    const reEmail = await page.$eval("#f-player-email", (el) => el.value);
    if (reEmail !== "fixed@example.com") {
      throw new Error(`[${L}] edited email not persisted/prefilled: "${reEmail}"`);
    }
    await page.click(".drawer-x");   // the always-visible header close control

    if (errors.length) throw new Error(`[${L}] console/page errors:\n${errors.join("\n")}`);
    console.log(`[${L}] OK — Player edited in place (locked Team, prefilled fields, email round-trip).`);
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
    console.log("Player-edit operator-drawer browser journey passed.");
  } catch (error) {
    console.error("Player-edit operator-drawer browser journey FAILED.");
    console.error(error && error.message ? error.message : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
