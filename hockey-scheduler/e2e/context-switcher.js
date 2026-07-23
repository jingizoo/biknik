// Active Program/Season context switcher browser journey (#159).
//
// The switcher is a native <select> (full keyboard/AT semantics for free) that
// consumes GET /api/context/options, GET /api/context, and POST /api/context.
// At desktop AND 390px it must:
//   * offer Program-only for EVERY authorized Program (a "no season" entry),
//     alongside each Season — so one Program + one Season exposes BOTH choices;
//   * persist a pick (incl. Program-only ⇒ season_id:null), reflect it, and
//     encode it in a structured Base64URL "#ctx=" hash;
//   * restore the saved selection on reload, ADOPT a different authorized deep
//     link (hash wins over the persisted row), and NORMALIZE an invalid/stale
//     link to the saved context with a generic message (no existence oracle);
//   * be operable by KEYBOARD alone (focus + Arrow), persisting the change;
//   * keep the persistent "display only — screens not filtered" notice visible
//     in the normal closed state (a static chip too);
//   * flag an archived Season as read-only; and render a static chip when a
//     Program has no Seasons (single option);
//   * RECONCILE a concurrent lifecycle/scope change (a Season archived, reopened,
//     or newly created between the options load and a successful POST) from a
//     fresh GET /api/context/options — WITHOUT reloading the page — so the label,
//     read-only badge and selection are never rendered from the stale pre-POST
//     rows (a second browser context makes the concurrent change);
// all with zero console/page errors.
//
// Setup that needs manage_setup runs as the League Admin; the switcher itself
// is exercised as a global VIEWER, which is also authorized for every
// Program/Season but does NOT trigger the League-Admin onboarding wizard that
// takes over #content.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8241, chipPort: 8341, reconPort: 8441 },
  { label: "phone", width: 390, height: 844, port: 8242, chipPort: 8342, reconPort: 8442 },
];

function encodeCtx(programId, seasonId) {
  const json = JSON.stringify({ v: 1, p: programId || null, s: seasonId || null });
  const b64 = Buffer.from(json, "utf8").toString("base64")
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  return "#ctx=" + b64;
}

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

// Same-origin fetches run inside the page (carry the session cookie).
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
const loginAs = (page, username) => apiPost(page, "/api/auth/login", { username, password: "demo" });

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

async function reloadShell(page) {
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForSelector("#content > *", { timeout: 10000 });
}
const selectValue = (page) => page.evaluate(() => document.getElementById("ctx-select").value);
const ctxSeason = async (page) => (await apiGet(page, "/api/context")).json.season_id;

// --- the interactive switcher (Program-only, keyboard, deep-link, archived) ---
async function checkSwitcher(browser, viewport) {
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
    if ((await loginAs(page, "admin")).status !== 200) throw new Error(`[${L}] admin login failed`);
    if ((await apiPost(page, "/api/demo/load", {})).status !== 200) throw new Error(`[${L}] demo load failed`);
    const opts = (await apiGet(page, "/api/context/options")).json;
    const programId = opts.programs[0].id;
    const winterId = opts.programs[0].seasons[0].id;
    const progOnly = programId + "|";
    const winter = programId + "|" + winterId;

    if ((await loginAs(page, "viewer")).status !== 200) throw new Error(`[${L}] viewer login failed`);
    await reloadShell(page);

    // (A) One Program + one Season exposes BOTH choices in a real <select>, and
    //     the persistent "display only" notice is visible in the closed state.
    await page.waitForFunction(() => {
      const w = document.getElementById("context-switcher");
      const s = document.getElementById("ctx-select");
      return w && !w.hidden && s && !s.hidden && s.options.length >= 2;
    }, null, { timeout: 10000 });
    const optionValues = await page.locator("#ctx-select option").evaluateAll((os) => os.map((o) => o.value));
    if (!optionValues.includes(progOnly)) throw new Error(`[${L}] Program-only option missing: ${optionValues}`);
    if (!optionValues.includes(winter)) throw new Error(`[${L}] Season option missing: ${optionValues}`);
    const note = await page.locator(".ctx-unfiltered");
    if (!(await note.isVisible()) || !/display only|not filtered/i.test(await note.textContent())) {
      throw new Error(`[${L}] persistent "display only" notice not visible`);
    }

    // (B) Select Program-only ⇒ persisted season_id:null + #ctx= hash + reload
    //     restore; then switch back to the Season.
    await page.selectOption("#ctx-select", progOnly);
    await page.waitForFunction(() => location.hash.indexOf("#ctx=") === 0, null, { timeout: 10000 });
    if ((await ctxSeason(page)) !== null) throw new Error(`[${L}] Program-only did not persist season_id:null`);
    await reloadShell(page);
    await page.waitForFunction((v) => document.getElementById("ctx-select").value === v, progOnly, { timeout: 10000 });
    await page.selectOption("#ctx-select", winter);
    await page.waitForFunction(() => true);
    if ((await ctxSeason(page)) !== winterId) throw new Error(`[${L}] switch back to Season did not persist`);

    // (C) Keyboard-only operation: focus the select without a mouse and change
    //     the selection with the arrow keys; the change must persist.
    await page.focus("#ctx-select");
    const focused = await page.evaluate(() => document.activeElement && document.activeElement.id);
    if (focused !== "ctx-select") throw new Error(`[${L}] select not keyboard-focusable (active=${focused})`);
    const before = await selectValue(page);
    await page.keyboard.press("ArrowUp");
    await page.waitForFunction((b) => document.getElementById("ctx-select").value !== b, before, { timeout: 10000 });
    const afterKb = await selectValue(page);
    const kbSeason = afterKb.slice(afterKb.indexOf("|") + 1) || null;
    if ((await ctxSeason(page)) !== kbSeason) throw new Error(`[${L}] keyboard change did not persist`);

    // (D) Deep-link ADOPTION: persist Winter via the API, but leave a Program-
    //     only hash in the URL; on reload the hash must WIN (POST rewrites the
    //     persisted selection) — the equality fast path must not hide this.
    await apiPost(page, "/api/context", { program_id: programId, season_id: winterId });
    await page.evaluate((h) => { location.hash = h; }, encodeCtx(programId, null));
    await reloadShell(page);
    await page.waitForFunction((v) => document.getElementById("ctx-select").value === v, progOnly, { timeout: 10000 });
    if ((await ctxSeason(page)) !== null) throw new Error(`[${L}] deep link was not adopted over the persisted row`);

    // (E) Invalid/stale link ⇒ normalized to the saved context with a GENERIC
    //     message (no existence oracle), and the hash rewritten to the resolved
    //     selection (never the bogus one). Simulate opening a shared link by
    //     dropping the bogus hash in and doing a full reload (a same-document
    //     goto that only changes the fragment does NOT re-run bootstrap).
    await page.evaluate((h) => { location.hash = h; }, encodeCtx("ghost_program", "ghost_season"));
    await reloadShell(page);
    await page.waitForFunction((v) => document.getElementById("ctx-select").value === v, progOnly, { timeout: 10000 });
    const toast = (await page.locator("#toast-root").textContent()) || "";
    if (!/saved context|isn't available/i.test(toast)) throw new Error(`[${L}] no generic normalize message: "${toast}"`);
    if (/ghost/i.test(toast)) throw new Error(`[${L}] normalize message leaked the bogus id`);
    if (/ghost_program/.test(await page.evaluate(() => location.hash))) throw new Error(`[${L}] bogus id left in the URL hash`);

    // (F) Archived Season ⇒ labeled read-only in the option, and the persistent
    //     read-only badge shows when it is the selection.
    if ((await loginAs(page, "admin")).status !== 200) throw new Error(`[${L}] admin re-login (archive) failed`);
    if ((await apiPost(page, `/api/v2/setup/seasons/${winterId}/archive`, { reason: "done" })).status !== 200) {
      throw new Error(`[${L}] archive failed`);
    }
    if ((await loginAs(page, "viewer")).status !== 200) throw new Error(`[${L}] viewer re-login (archive) failed`);
    await reloadShell(page);
    const winterLabel = await page.locator(`#ctx-select option[value="${winter}"]`).textContent();
    if (!/archived|read-only/i.test(winterLabel)) throw new Error(`[${L}] archived Season not flagged: "${winterLabel}"`);
    await page.selectOption("#ctx-select", winter);
    await page.waitForFunction(() => {
      const ro = document.getElementById("ctx-ro");
      return ro && !ro.hidden;
    }, null, { timeout: 10000 });

    if (errors.length) throw new Error(`[${L}] console/page errors:\n${errors.join("\n")}`);
    console.log(`[${L}] OK — Program-only + Season, keyboard, deep-link adopt/normalize, #ctx=, archived read-only.`);
  } catch (error) {
    throw new Error(`${error.message}\n--- server output ---\n${out}`);
  } finally {
    await context.close();
    await stopServer(server);
  }
}

// --- the seasonless-Program static chip (a clean world, one empty Program) ---
async function checkChip(browser, viewport) {
  const base = `http://${HOST}:${viewport.chipPort}`;
  const server = startServer(viewport.chipPort, {});
  let out = "";
  server.stdout.on("data", (d) => { out += d.toString(); });
  server.stderr.on("data", (d) => { out += d.toString(); });
  const { context, page, errors } = await newPage(browser, viewport);
  const L = viewport.label;
  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    // Clean slate (no demo): one Program with NO Seasons, plus a Viewer account
    // to observe it without the League-Admin onboarding wizard.
    if ((await loginAs(page, "admin")).status !== 200) throw new Error(`[${L}] admin login failed`);
    if ((await apiPost(page, "/api/v2/setup/program", { name: "Solo Program" })).status !== 200) {
      throw new Error(`[${L}] create program failed`);
    }
    if ((await apiPost(page, "/api/accounts", { username: "seer", password: "demo", role: "viewer" })).status !== 200) {
      throw new Error(`[${L}] create viewer account failed`);
    }
    if ((await loginAs(page, "seer")).status !== 200) throw new Error(`[${L}] seer login failed`);
    await reloadShell(page);
    // A single selectable context (Program-only) ⇒ a static CHIP, not a select,
    // and the persistent "display only" notice is still shown.
    await page.waitForFunction(() => {
      const chip = document.getElementById("ctx-static");
      const sel = document.getElementById("ctx-select");
      return chip && !chip.hidden && sel && sel.hidden;
    }, null, { timeout: 10000 });
    const expectedChip = "Solo Program · Program overview (no season)";
    let chipText = (await page.locator("#ctx-static").textContent()).trim();
    if (chipText !== expectedChip) {
      throw new Error(`[${L}] chip text must identify its Program exactly: "${chipText}"`);
    }
    const note = await page.locator(".ctx-unfiltered");
    if (!(await note.isVisible())) throw new Error(`[${L}] "display only" notice missing on the chip`);
    if (!page.url().includes("#ctx=")) {
      throw new Error(`[${L}] Program-only context was not written to the deep-link hash`);
    }

    // Reload adopts the Program-only deep link and must restore the same exact
    // visible context on both desktop and 390px, not fall back to the generic
    // no-season wording.
    await reloadShell(page);
    await page.waitForFunction((expected) => {
      const chip = document.getElementById("ctx-static");
      const sel = document.getElementById("ctx-select");
      return chip && !chip.hidden && chip.textContent.trim() === expected
        && sel && sel.hidden;
    }, expectedChip, { timeout: 10000 });
    chipText = (await page.locator("#ctx-static").textContent()).trim();
    if (chipText !== expectedChip || !(await page.locator(".ctx-unfiltered").isVisible())) {
      throw new Error(`[${L}] chip/deep-link restoration lost the Program identity`);
    }
    if (errors.length) throw new Error(`[${L}] console/page errors:\n${errors.join("\n")}`);
    console.log(`[${L}] OK — seasonless Program chip names the Program and survives deep-link reload.`);
  } catch (error) {
    throw new Error(`${error.message}\n--- server output ---\n${out}`);
  } finally {
    await context.close();
    await stopServer(server);
  }
}

// --- stale-options reconciliation (a concurrent lifecycle/scope change between
//     GET /api/context/options and a successful POST must be reconciled from a
//     FRESH options fetch before render — never rendered from the pre-POST rows,
//     and WITHOUT reloading the page) ---
async function checkReconcile(browser, viewport) {
  const base = `http://${HOST}:${viewport.reconPort}`;
  const server = startServer(viewport.reconPort, {});
  let out = "";
  server.stdout.on("data", (d) => { out += d.toString(); });
  server.stderr.on("data", (d) => { out += d.toString(); });
  const { context, page, errors } = await newPage(browser, viewport);   // the subject (viewer)
  const admin = await newPage(browser, viewport);                       // a concurrent actor
  const L = viewport.label;
  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    // Setup as League Admin on the subject page, read the ids, then hand the
    // page to the Viewer. A SEPARATE admin context makes the concurrent
    // lifecycle changes so the viewer's in-memory option set genuinely goes
    // stale (the viewer never reloads after its deterministic starting state).
    if ((await loginAs(page, "admin")).status !== 200) throw new Error(`[${L}] admin login failed`);
    if ((await apiPost(page, "/api/demo/load", {})).status !== 200) throw new Error(`[${L}] demo load failed`);
    const opts = (await apiGet(page, "/api/context/options")).json;
    const programId = opts.programs[0].id;
    const winterId = opts.programs[0].seasons[0].id;
    const winter = programId + "|" + winterId;
    const progOnly = programId + "|";

    await admin.page.goto(base, { waitUntil: "domcontentloaded" });
    if ((await loginAs(admin.page, "admin")).status !== 200) throw new Error(`[${L}] admin (actor) login failed`);

    // Viewer, deterministic starting point: Program-only, options freshly
    // loaded (Winter present + writable). No reloads past this line.
    if ((await loginAs(page, "viewer")).status !== 200) throw new Error(`[${L}] viewer login failed`);
    await apiPost(page, "/api/context", { program_id: programId, season_id: null });
    await reloadShell(page);
    await page.waitForFunction((v) => {
      const s = document.getElementById("ctx-select");
      return s && !s.hidden && s.value === v && s.options.length >= 2;
    }, progOnly, { timeout: 10000 });

    // (1) Archive BETWEEN options-load and POST: the viewer's row still says
    //     Winter is writable; a concurrent archive + the viewer selecting Winter
    //     must reconcile to read-only from a fresh GET — WITHOUT a reload.
    if ((await apiPost(admin.page, `/api/v2/setup/seasons/${winterId}/archive`, { reason: "done" })).status !== 200) {
      throw new Error(`[${L}] concurrent archive failed`);
    }
    await page.selectOption("#ctx-select", winter);
    await page.waitForFunction(() => {
      const ro = document.getElementById("ctx-ro");
      return ro && !ro.hidden;
    }, null, { timeout: 10000 });
    const archivedLabel = await page.locator(`#ctx-select option[value="${winter}"]`).textContent();
    if (!/archived|read-only/i.test(archivedLabel)) {
      throw new Error(`[${L}] archived Season not reconciled without reload: "${archivedLabel}"`);
    }
    if ((await ctxSeason(page)) !== winterId) throw new Error(`[${L}] archived POST did not persist`);

    // (2) Reopen: the reverse also reconciles without reload — badge clears, the
    //     archived marker drops. Toggle away first so re-selecting Winter fires.
    if ((await apiPost(admin.page, `/api/v2/setup/seasons/${winterId}/reopen`, { reason: "back" })).status !== 200) {
      throw new Error(`[${L}] concurrent reopen failed`);
    }
    await page.selectOption("#ctx-select", progOnly);
    await page.waitForFunction((v) => document.getElementById("ctx-select").value === v, progOnly, { timeout: 10000 });
    await page.selectOption("#ctx-select", winter);
    await page.waitForFunction(() => {
      const ro = document.getElementById("ctx-ro");
      return ro && ro.hidden;
    }, null, { timeout: 10000 });
    const reopenedLabel = await page.locator(`#ctx-select option[value="${winter}"]`).textContent();
    if (/archived|read-only/i.test(reopenedLabel)) {
      throw new Error(`[${L}] reopened Season still flagged read-only: "${reopenedLabel}"`);
    }

    // (3) A Season created AFTER options loaded must surface on the next POST's
    //     fresh fetch — still WITHOUT reload — proving `selected` is always drawn
    //     from a reconciled option set, not the stale one.
    const created = await apiPost(admin.page, "/api/v2/setup/season", { program_id: programId, name: "Spring Cup" });
    if (created.status !== 200 || !created.json.id) {
      throw new Error(`[${L}] concurrent season create failed: ${JSON.stringify(created.json)}`);
    }
    const spring = programId + "|" + created.json.id;
    await page.selectOption("#ctx-select", progOnly);
    await page.waitForFunction((v) => {
      const s = document.getElementById("ctx-select");
      return Array.from(s.options).some((o) => o.value === v);
    }, spring, { timeout: 10000 });
    const springLabel = await page.locator(`#ctx-select option[value="${spring}"]`).textContent();
    if (!/Spring Cup/.test(springLabel)) throw new Error(`[${L}] newly available Season not surfaced: "${springLabel}"`);

    if (errors.length) throw new Error(`[${L}] console/page errors:\n${errors.join("\n")}`);
    console.log(`[${L}] OK — no-reload reconciliation: archive→read-only, reopen→writable, newly-available Season surfaced.`);
  } catch (error) {
    throw new Error(`${error.message}\n--- server output ---\n${out}`);
  } finally {
    await admin.context.close();
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
      await checkSwitcher(browser, viewport);
      await checkChip(browser, viewport);
      await checkReconcile(browser, viewport);
    }
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
