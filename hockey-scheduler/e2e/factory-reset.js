// Production factory-reset Danger-zone browser journey (#256 acceptance).
//
// The backend (PR #264) already proves the reset engine, every safety-control
// rejection, cross-process locking, and forced mid-transaction rollback on
// Memory/SQLite/PostgreSQL. This journey is the browser-level evidence #256's
// last acceptance item requires — that the Administration → Danger zone UI, at
// desktop AND 390px, correctly:
//   * hides the whole Danger zone unless the deployment opt-in flag is set;
//   * shows a row-count preview before anything destructive;
//   * keeps the execute button locked until backup ack + password + the EXACT
//     typed phrase are all present (invalid confirmation never enables it);
//   * can be cancelled with zero effect;
//   * on a wrong password reports the failure inline and changes nothing
//     (the browser-observable "no partial deletion" — forced rollback itself is
//     a backend test), leaving the same challenge usable for a retry;
//   * on the correct inputs completes, signs the operator out to the login
//     screen, and leaves the installation recoverable (the preserved admin can
//     sign back in).
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, noFlagPort: 8221, flagPort: 8223 },
  { label: "phone", width: 390, height: 844, noFlagPort: 8222, flagPort: 8224 },
];
const ADMIN = "prod_admin";
const PW = "a-real-production-password";
const PHRASE = "DELETE ALL PRODUCTION DATA";

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

const PROD_ENV = {
  APP_MODE: "production",
  BOOTSTRAP_ADMIN_USER: ADMIN,
  BOOTSTRAP_ADMIN_PASSWORD: PW,
};

async function bootAsAdmin(page, base, label) {
  await page.goto(base, { waitUntil: "domcontentloaded" });
  if ((await login(page, ADMIN, PW)) !== 200) {
    throw new Error(`[${label}] admin login failed`);
  }
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForSelector("#content > *", { timeout: 10000 });
}

async function gotoReadiness(page, label) {
  // The Danger zone lives in the Pilot Readiness (Administration) view.
  await page.waitForFunction(
    () => { const t = document.querySelector('.tab[data-tab="readiness"]');
            return t && t.offsetParent !== null; },
    null, { timeout: 10000 });
  await page.click('.tab[data-tab="readiness"]');
  await page.waitForSelector(".card", { timeout: 10000 });
}

function dangerZoneVisible(page) {
  return page.locator(".card.danger-zone").isVisible().catch(() => false);
}

// --- Negative: without the opt-in flag, even a production League Admin sees
//     no Danger zone at all --------------------------------------------------
async function checkNoFlagHidesZone(browser, viewport) {
  const base = `http://${HOST}:${viewport.noFlagPort}`;
  const server = startServer(viewport.noFlagPort, PROD_ENV);  // flag deliberately unset
  let out = "";
  server.stdout.on("data", (d) => { out += d.toString(); });
  server.stderr.on("data", (d) => { out += d.toString(); });
  const { context, page, errors } = await newPage(browser, viewport);
  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await bootAsAdmin(page, base, viewport.label);
    await gotoReadiness(page, viewport.label);
    if (await dangerZoneVisible(page)) {
      throw new Error(`[${viewport.label}] Danger zone shown without the opt-in flag`);
    }
    if (errors.length) {
      throw new Error(`[${viewport.label}] no-flag console/page errors:\n${errors.join("\n")}`);
    }
    console.log(`[${viewport.label}] OK — Danger zone hidden without the flag.`);
  } catch (error) {
    throw new Error(`${error.message}\n--- no-flag server output ---\n${out}`);
  } finally {
    await context.close();
    await stopServer(server);
  }
}

// --- Full guarded flow with the flag enabled -------------------------------
async function openPreviewModal(page, label) {
  await page.click("[data-factory-reset]");
  // The preview POST resolves into the confirm step (counts + inputs).
  await page.waitForSelector("[data-fr-confirm]", { timeout: 10000 });
  await page.waitForSelector(".fr-count-total .fr-count-n", { timeout: 10000 });
}

function previewTotal(page) {
  return page.locator(".fr-count-total .fr-count-n").first().innerText()
    .then((t) => parseInt(t.trim(), 10));
}

async function checkFlagFlow(browser, viewport) {
  const base = `http://${HOST}:${viewport.flagPort}`;
  const server = startServer(viewport.flagPort, {
    ...PROD_ENV, ALLOW_PRODUCTION_FACTORY_RESET: "true" });
  let out = "";
  server.stdout.on("data", (d) => { out += d.toString(); });
  server.stderr.on("data", (d) => { out += d.toString(); });
  const { context, page, errors } = await newPage(browser, viewport);
  const L = viewport.label;
  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await bootAsAdmin(page, base, L);
    await gotoReadiness(page, L);

    if (!(await dangerZoneVisible(page))) {
      throw new Error(`[${L}] Danger zone not shown with the flag enabled`);
    }

    // --- Preview + invalid-confirmation gating ------------------------------
    await openPreviewModal(page, L);
    const total0 = await previewTotal(page);
    if (!(total0 >= 1)) throw new Error(`[${L}] preview total was not a positive count (${total0})`);
    const confirmBtn = page.locator("[data-fr-confirm]");
    if (!(await confirmBtn.isDisabled())) {
      throw new Error(`[${L}] execute button was enabled before any input`);
    }
    // Wrong phrase, everything else set → still disabled.
    await page.check("#fr-backup");
    await page.fill("#fr-password", PW);
    await page.fill("#fr-phrase", "delete all production data");  // wrong case
    if (!(await confirmBtn.isDisabled())) {
      throw new Error(`[${L}] execute enabled on a non-exact confirmation phrase`);
    }
    // Exact phrase but no backup ack → still disabled.
    await page.fill("#fr-phrase", PHRASE);
    await page.uncheck("#fr-backup");
    if (!(await confirmBtn.isDisabled())) {
      throw new Error(`[${L}] execute enabled without backup acknowledgement`);
    }
    // All three satisfied → enabled.
    await page.check("#fr-backup");
    await page.waitForFunction(
      () => { const b = document.querySelector("[data-fr-confirm]"); return b && !b.disabled; },
      null, { timeout: 5000 });

    // --- Cancel does nothing ------------------------------------------------
    await page.click(".modal-foot .act.ghost");  // the footer Cancel button
    await page.waitForSelector(".modal.danger", { state: "detached", timeout: 5000 });

    // --- Denied: wrong password → inline error, no wipe ---------------------
    await openPreviewModal(page, L);
    await page.check("#fr-backup");
    await page.fill("#fr-password", "the-wrong-password");
    await page.fill("#fr-phrase", PHRASE);
    await page.waitForFunction(
      () => { const b = document.querySelector("[data-fr-confirm]"); return b && !b.disabled; },
      null, { timeout: 5000 });
    await page.click("[data-fr-confirm]");
    await page.waitForSelector(".fr-inline-error", { timeout: 10000 });
    // The modal is still open on the confirm step — nothing was wiped.
    if (!(await page.locator("[data-fr-confirm]").count())) {
      throw new Error(`[${L}] wrong password left the confirm step`);
    }
    // Re-open a fresh preview and prove the counts are unchanged (no partial
    // deletion happened on the rejected attempt).
    await page.click(".modal-foot .act.ghost");  // the footer Cancel button
    await page.waitForSelector(".modal.danger", { state: "detached", timeout: 5000 });
    await openPreviewModal(page, L);
    const totalAfterDenied = await previewTotal(page);
    if (totalAfterDenied !== total0) {
      throw new Error(`[${L}] row count changed after a denied reset (${total0} → ${totalAfterDenied})`);
    }

    // --- Successful reset → sign-out → recoverable installation -------------
    await page.check("#fr-backup");
    await page.fill("#fr-password", PW);
    await page.fill("#fr-phrase", PHRASE);
    await page.waitForFunction(
      () => { const b = document.querySelector("[data-fr-confirm]"); return b && !b.disabled; },
      null, { timeout: 5000 });
    await page.click("[data-fr-confirm]");
    // Success step, then return to sign-in.
    await page.waitForSelector("[data-fr-done]", { timeout: 15000 });
    await page.click("[data-fr-done]");
    await page.waitForSelector("#login-screen:not([hidden])", { timeout: 10000 });
    // The whole authenticated shell is hidden behind body.signed-out while the
    // operator is logged out — proving the sign-out actually took effect, not
    // just that a success card was shown.
    await page.waitForFunction(() => document.body.classList.contains("signed-out"),
                               null, { timeout: 10000 });

    // Re-login through the REAL sign-in UI (not a raw fetch): filling the login
    // form and submitting drives signIn(), which clears hs_signed_out, restores
    // client identity/permissions, hides the login screen, and re-renders the
    // authenticated console. A raw /api/auth/login would prove the credentials
    // are accepted but not that the app returns to a usable signed-in state.
    await page.fill("#login-user", ADMIN);
    await page.fill("#login-pass", PW);
    await page.click("#login-form button[type=submit]");
    await page.waitForFunction(
      () => { const s = document.getElementById("login-screen"); return s && s.hidden; },
      null, { timeout: 10000 });
    await page.waitForFunction(() => !document.body.classList.contains("signed-out"),
                               null, { timeout: 10000 });
    // A genuinely authenticated affordance is back — the League-Admin-only
    // Readiness (Administration) tab — so the console re-rendered, not just the
    // login card vanished.
    await page.waitForFunction(
      () => { const t = document.querySelector('.tab[data-tab="readiness"]');
              return t && t.offsetParent !== null; },
      null, { timeout: 10000 });

    if (errors.length) {
      throw new Error(`[${L}] flag-flow console/page errors:\n${errors.join("\n")}`);
    }
    console.log(`[${L}] OK — preview, gating, cancel, denied (no wipe), reset + re-login.`);
  } catch (error) {
    throw new Error(`${error.message}\n--- flag server output ---\n${out}`);
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
    for (const viewport of VIEWPORTS) {
      await checkNoFlagHidesZone(browser, viewport);
      await checkFlagFlow(browser, viewport);
    }
    console.log("Factory-reset Danger-zone browser journey passed.");
  } catch (error) {
    console.error("Factory-reset Danger-zone browser journey FAILED.");
    console.error(error && error.message ? error.message : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
