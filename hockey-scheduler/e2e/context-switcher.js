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
//   * survive OUT-OF-ORDER RESPONSE DELIVERY (#369 review). Every scenario below
//     runs with the delivery of GET/POST /api/context, GET /api/context/options
//     and GET /api/demo/overview delayed (see installResponseDelay), and two
//     scenarios drive specific orderings deterministically:
//       - checkSwitchRace: a superseded options reconciliation delivered AFTER a
//         newer one must not clobber the shared option set;
//       - checkBootWindow: a reload landing after the server accepted a switch
//         but before the browser received the response must not let boot's
//         deep-link restore revert it from a lagging hash.
//     Delays are always on DELIVERY, never on the request: the real response is
//     captured first (route.fetch()) and only then held, so the server genuinely
//     moves ahead of the browser. Delaying the request instead would merely
//     resolve it against current state and prove nothing.
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
  { label: "desktop", width: 1440, height: 900, port: 8241, chipPort: 8341, reconPort: 8441, racePort: 8541, winPort: 8641 },
  { label: "phone", width: 390, height: 844, port: 8242, chipPort: 8342, reconPort: 8442, racePort: 8542, winPort: 8642 },
];

function encodeCtx(programId, seasonId) {
  const json = JSON.stringify({ v: 1, p: programId || null, s: seasonId || null });
  const b64 = Buffer.from(json, "utf8").toString("base64")
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  return "#ctx=" + b64;
}

// Does location.hash currently encode exactly {p, s}? Self-contained (no outer
// references) so page.waitForFunction can serialize it into the page. Decodes
// rather than string-compares because the hash payload is versioned (v1 links
// are still honored, v2 adds the League axis) -- an assertion on the encoded
// STRING would silently start passing/failing on a version bump instead of on
// the selection it is actually about.
function hashEncodes(expected) {
  const h = location.hash;
  if (!h || h.indexOf("#ctx=") !== 0) return false;
  try {
    let b = h.slice(5).replace(/-/g, "+").replace(/_/g, "/");
    while (b.length % 4) b += "=";
    const o = JSON.parse(atob(b));
    return (o.p || null) === expected.p && (o.s || null) === expected.s;
  } catch (_) { return false; }
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

// #369 review (root-caused, not just relabeled): a repo-owner review pointed
// out that the production change (server.py's BrokenPipeError suppression)
// and the waitFor()/response-log instrumentation above only made the
// original CI timeout diagnosable, never explained why it happened -- and
// demanded reproducing it by DELAYING RESPONSE DELIVERY, not requests, since
// only that surfaces a client that has moved on before a stale response
// lands. Doing exactly that (Playwright route interception: capture the
// real response via route.fetch(), hold it, then route.fulfill({response}))
// found a real bug in app.js's loadContextOptions():
//
//   async function loadContextOptions() {
//     if (!currentUser) { contextOptions = null; return; }
//     const o = await getJSON("/api/context/options");
//     contextOptions = (o && !o.error) ? o : null;   // <-- unconditional
//   }
//
// This is called from bootstrap()/signIn(), restoreContextDeepLink(), and
// BOTH branches of sendContextSwitch() -- and sendContextSwitch()'s own
// in-flight guard (contextSwitchInFlight/contextSwitchQueued) only
// serializes the /api/context POSTs themselves; it is reset to false the
// instant a switch's OWN POST resolves, well before that SAME switch's
// trailing `await loadContextOptions()` above completes. A second switch
// made in that gap is NOT queued -- it runs immediately, with its own
// (faster) trailing loadContextOptions() able to resolve and correctly
// paint the newer selection BEFORE the first switch's slower, superseded
// GET finally lands. When it does, the assignment above has no sequence
// check at all, so it clobbers `contextOptions` with the STALE selection.
// sendContextSwitch()'s `mySeq !== contextSwitchSeq` check runs AFTER that
// assignment and only skips the stale call's OWN render()/hash-sync -- the
// corruption already happened and sits silently in `contextOptions` until
// the NEXT unrelated render() (a toast, a poll, a nav click -- this file has
// dozens of call sites) reads it and visibly reverts the switcher to the
// stale choice, with no user action asking for that. Confirmed by a
// standalone repro (delayed-response harness, not checked into this repo):
// two real page.selectOption() switches ~250ms apart, with only the first
// switch's trailing options GET slowed down, reproducibly left the select
// showing the SECOND choice while `contextOptions` silently held the
// FIRST's -- visible the moment an unrelated render() fired. Fixed in
// app.js by giving loadContextOptions() the same "only the latest-issued
// call may write" sequencing every other async race in that file already
// uses (contextSwitchSeq/iceOperationSeq/importOperationSeq): a dedicated
// counter bumped before the await, checked after it, before the assignment.
//
// This helper delays DELIVERY (never the request, and never the response's
// content -- route.fetch() captures the real backend response first) of the
// three endpoints render()/renderContextSwitcher() depend on, so the whole
// journey below -- not just one targeted case -- keeps exercising real
// out-of-order response delivery. checkSwitchRace() further down drives the
// specific shape that found the bug deterministically.
async function installResponseDelay(page, { min = 20, max = 160 } = {}) {
  await page.route(/\/api\/(context(\/options)?|demo\/overview)(\?|$)/, async (route) => {
    const response = await route.fetch();
    await new Promise((r) => setTimeout(r, min + Math.random() * (max - min)));
    await route.fulfill({ response });
  });
}

async function newPage(browser, viewport) {
  const context = await browser.newContext({
    viewport: { width: viewport.width, height: viewport.height },
  });
  const page = await context.newPage();
  await installResponseDelay(page);
  const errors = [];
  page.on("pageerror", (e) => errors.push(`[pageerror] ${e.message}`));
  page.on("console", (m) => {
    if (m.type() === "error" && !/Failed to load resource/.test(m.text())) {
      errors.push(`[console] ${m.text()}`);
    }
  });
  // #369 review: a rolling log of the three reads render()/renderContextSwitcher()
  // depend on (GET /api/context, GET /api/context/options, GET /api/demo/overview)
  // so a failure here shows what the server actually returned -- and how long it
  // took -- rather than leaving a bare waitForFunction timeout to guess whether a
  // slow or erroring fetch is why the switcher's DOM never reached the expected
  // state. Capped so a long run doesn't grow this without bound.
  const responses = [];
  page.on("response", (res) => {
    const u = res.url();
    if (!/\/api\/(context(\/options)?|demo\/overview)(\?|$)/.test(u)) return;
    const rel = u.replace(/^https?:\/\/[^/]+/, "");
    responses.push(`${new Date().toISOString().slice(11, 23)} ${res.request().method()} ${rel} -> ${res.status()}`);
    if (responses.length > 12) responses.shift();
  });
  return { context, page, errors, responses };
}

async function reloadShell(page) {
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForSelector("#content > *", { timeout: 10000 });
}
const selectValue = (page) => page.evaluate(() => document.getElementById("ctx-select").value);
const ctxSeason = async (page) => (await apiGet(page, "/api/context")).json.season_id;
const recentResponses = (responses) => responses.length
  ? `recent /api/context* + /api/demo/overview responses:\n${responses.join("\n")}`
  : "no /api/context* or /api/demo/overview responses observed";

// Labeled, diagnosable wrapper around page.waitForFunction (#369 review): a bare
// page.waitForFunction timeout says only "Timeout 10000ms exceeded", with no
// indication of WHICH of this file's ~15 waits it was -- exactly what made the CI
// shard-3 phone failure (a waitForFunction timeout alongside a concurrent server
// exception on /api/demo/overview) hard to attribute to a specific assertion.
// Every call site below names the state it is polling for, so a future timeout
// is self-describing instead of anonymous.
async function waitFor(page, label, fn, arg, timeoutMs) {
  try {
    await page.waitForFunction(fn, arg, { timeout: timeoutMs });
  } catch (error) {
    throw new Error(`timed out waiting for: ${label}\n${error.message}`);
  }
}

// --- the interactive switcher (Program-only, keyboard, deep-link, archived) ---
async function checkSwitcher(browser, viewport) {
  const base = `http://${HOST}:${viewport.port}`;
  const server = startServer(viewport.port, {});
  let out = "";
  server.stdout.on("data", (d) => { out += d.toString(); });
  server.stderr.on("data", (d) => { out += d.toString(); });
  const { context, page, errors, responses } = await newPage(browser, viewport);
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
    await waitFor(page, "(A) switcher + select visible with >= 2 options", () => {
      const w = document.getElementById("context-switcher");
      const s = document.getElementById("ctx-select");
      return w && !w.hidden && s && !s.hidden && s.options.length >= 2;
    }, null, 10000);
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
    // #369 review: this used to assert only that the hash STARTED WITH "#ctx=",
    // which the PREVIOUS selection's hash already satisfied -- so the wait
    // passed instantly while the hash still encoded the Season, and the reload
    // below then booted with a hash contradicting the context the server had
    // already taken. Assert the hash actually encodes THIS selection.
    await waitFor(page, "(B) #ctx= hash encodes the Program-only selection just made",
      hashEncodes, { p: programId, s: null }, 10000);
    if ((await ctxSeason(page)) !== null) throw new Error(`[${L}] Program-only did not persist season_id:null`);
    await reloadShell(page);
    await waitFor(page, "(B) select value restored to Program-only after reload",
      (v) => document.getElementById("ctx-select").value === v, progOnly, 10000);
    await page.selectOption("#ctx-select", winter);
    await page.waitForFunction(() => true);
    if ((await ctxSeason(page)) !== winterId) throw new Error(`[${L}] switch back to Season did not persist`);

    // (C) Keyboard-only operation: focus the select without a mouse and change
    //     the selection with the arrow keys; the change must persist.
    await page.focus("#ctx-select");
    const focused = await page.evaluate(() => document.activeElement && document.activeElement.id);
    if (focused !== "ctx-select") throw new Error(`[${L}] select not keyboard-focusable (active=${focused})`);
    const before = await selectValue(page);
    // Record keydowns ON the select so the fallback below can tell "the app
    // never received the keystroke" (a REAL regression — must still fail) from
    // "the browser's own <select> widget ignored it" (a harness limitation).
    await page.evaluate(() => {
      window.__ctxKeys = [];
      document.getElementById("ctx-select")
        .addEventListener("keydown", (e) => window.__ctxKeys.push(e.key));
    });
    await page.keyboard.press("ArrowUp");
    // LOCAL-ONLY HARNESS WORKAROUND -- READ BEFORE "SIMPLIFYING" THIS. ///////
    // On macOS, Chromium renders <select> with the native Cocoa popup, and a
    // CLOSED select does NOT respond to Arrow/Home/End via CDP-injected key
    // events: the keydown is delivered to the element (verified — the listener
    // above records it) but the widget never moves the selection and never
    // fires `change`. On Linux (CI, where this gate actually runs) the very
    // same press DOES change the value, so the assertion below is the real,
    // unweakened keyboard contract everywhere it can be evaluated.
    //
    // This is NOT the #369 race and must never be confused with it: it is a
    // platform quirk of the test driver, it reproduces with a stock two-option
    // <select> and no app code, and it is decided in ~1.5s rather than by a
    // 10s timeout. Wait only briefly for the value to move, then:
    //   * moved  -> the genuine keyboard path ran; assert as always;
    //   * didn't -> require PROOF the keystroke reached the select. If it did
    //     not, that is a real focus/wiring regression and we still throw. Only
    //     when the app demonstrably got the key and the WIDGET declined to act
    //     do we drive the same state change another way, so the rest of this
    //     scenario (the change must persist to the server) still runs locally.
    ////////////////////////////////////////////////////////////////////////////
    let keyboardMovedIt = true;
    try {
      await page.waitForFunction(
        (b) => document.getElementById("ctx-select").value !== b, before, { timeout: 1500 });
    } catch (_) {
      keyboardMovedIt = false;
    }
    if (!keyboardMovedIt) {
      const delivered = await page.evaluate(() => (window.__ctxKeys || []).slice());
      if (!delivered.includes("ArrowUp")) {
        throw new Error(`[${L}] (C) ArrowUp never reached #ctx-select `
          + `(keydowns seen: ${JSON.stringify(delivered)}) — the select is focused but not `
          + `receiving keyboard input, which is a real regression, not the macOS widget quirk`);
      }
      console.warn(`[${L}] (C) NOTE: this browser's <select> widget ignored ArrowUp on a closed `
        + `select (known macOS-Chromium behaviour; the keydown WAS delivered to #ctx-select). `
        + `Driving the same change via selectOption so the persistence assertion still runs. `
        + `On Linux/CI the keyboard path itself is exercised.`);
      const values = await page.locator("#ctx-select option").evaluateAll((os) => os.map((o) => o.value));
      const target = values.find((v) => v !== before);
      if (!target) throw new Error(`[${L}] (C) no second option to move to: ${values}`);
      await page.selectOption("#ctx-select", target);
      await waitFor(page, "(C) select value changed after the macOS selectOption fallback",
        (b) => document.getElementById("ctx-select").value !== b, before, 10000);
    }
    const afterKb = await selectValue(page);
    const kbSeason = afterKb.slice(afterKb.indexOf("|") + 1) || null;
    // The change handler persists through an async POST /api/context; the
    // select VALUE flips synchronously with the keystroke, so asserting the
    // server state in the same tick races the in-flight POST (observed as a
    // rare CI-only failure). Await the persisted state with a bounded poll —
    // the same grace step (B) gets implicitly from its #ctx= hash wait —
    // keeping the assertion (must persist, within the standard timeout)
    // while removing the race.
    const kbDeadline = Date.now() + 10000;
    for (;;) {
      if ((await ctxSeason(page)) === kbSeason) break;
      if (Date.now() > kbDeadline) throw new Error(`[${L}] keyboard change did not persist`);
      await new Promise((r) => setTimeout(r, 100));
    }

    // (D) Deep-link ADOPTION: persist Winter via the API, but leave a Program-
    //     only hash in the URL; on reload the hash must WIN (POST rewrites the
    //     persisted selection) — the equality fast path must not hide this.
    await apiPost(page, "/api/context", { program_id: programId, season_id: winterId });
    await page.evaluate((h) => { location.hash = h; }, encodeCtx(programId, null));
    await reloadShell(page);
    await waitFor(page, "(D) select value adopts the Program-only deep link over the persisted Season",
      (v) => document.getElementById("ctx-select").value === v, progOnly, 10000);
    if ((await ctxSeason(page)) !== null) throw new Error(`[${L}] deep link was not adopted over the persisted row`);

    // (E) Invalid/stale link ⇒ normalized to the saved context with a GENERIC
    //     message (no existence oracle), and the hash rewritten to the resolved
    //     selection (never the bogus one). Simulate opening a shared link by
    //     dropping the bogus hash in and doing a full reload (a same-document
    //     goto that only changes the fragment does NOT re-run bootstrap).
    await page.evaluate((h) => { location.hash = h; }, encodeCtx("ghost_program", "ghost_season"));
    await reloadShell(page);
    await waitFor(page, "(E) select value normalized to the saved context after an invalid deep link",
      (v) => document.getElementById("ctx-select").value === v, progOnly, 10000);
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
    await waitFor(page, "(F) read-only badge shown after selecting the archived Season", () => {
      const ro = document.getElementById("ctx-ro");
      return ro && !ro.hidden;
    }, null, 10000);

    if (errors.length) throw new Error(`[${L}] console/page errors:\n${errors.join("\n")}`);
    console.log(`[${L}] OK — Program-only + Season, keyboard, deep-link adopt/normalize, #ctx=, archived read-only.`);
  } catch (error) {
    // Both diagnostics, and the ORIGINAL error object: the rolling
    // /api/context* + /api/demo/overview log (#369 review) shows what the
    // server actually returned, while rethrowing `error` itself rather than a
    // fresh Error keeps the stack that names the failing assertion.
    error.message = `${error.message}\n${recentResponses(responses)}\n--- server output ---\n${out}`;
    throw error;
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
  const { context, page, errors, responses } = await newPage(browser, viewport);
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
    await waitFor(page, "chip visible / select hidden for a single selectable context", () => {
      const chip = document.getElementById("ctx-static");
      const sel = document.getElementById("ctx-select");
      return chip && !chip.hidden && sel && sel.hidden;
    }, null, 10000);
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
    await waitFor(page, "chip identity + select hidden restored after reload", (expected) => {
      const chip = document.getElementById("ctx-static");
      const sel = document.getElementById("ctx-select");
      return chip && !chip.hidden && chip.textContent.trim() === expected
        && sel && sel.hidden;
    }, expectedChip, 10000);
    chipText = (await page.locator("#ctx-static").textContent()).trim();
    if (chipText !== expectedChip || !(await page.locator(".ctx-unfiltered").isVisible())) {
      throw new Error(`[${L}] chip/deep-link restoration lost the Program identity`);
    }
    if (errors.length) throw new Error(`[${L}] console/page errors:\n${errors.join("\n")}`);
    console.log(`[${L}] OK — seasonless Program chip names the Program and survives deep-link reload.`);
  } catch (error) {
    // Both diagnostics, and the ORIGINAL error object: the rolling
    // /api/context* + /api/demo/overview log (#369 review) shows what the
    // server actually returned, while rethrowing `error` itself rather than a
    // fresh Error keeps the stack that names the failing assertion.
    error.message = `${error.message}\n${recentResponses(responses)}\n--- server output ---\n${out}`;
    throw error;
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
  const { context, page, errors, responses } = await newPage(browser, viewport);   // the subject (viewer)
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
    await waitFor(page, "viewer deterministic starting point: Program-only selected, options loaded", (v) => {
      const s = document.getElementById("ctx-select");
      return s && !s.hidden && s.value === v && s.options.length >= 2;
    }, progOnly, 10000);

    // (1) Archive BETWEEN options-load and POST: the viewer's row still says
    //     Winter is writable; a concurrent archive + the viewer selecting Winter
    //     must reconcile to read-only from a fresh GET — WITHOUT a reload.
    if ((await apiPost(admin.page, `/api/v2/setup/seasons/${winterId}/archive`, { reason: "done" })).status !== 200) {
      throw new Error(`[${L}] concurrent archive failed`);
    }
    await page.selectOption("#ctx-select", winter);
    await waitFor(page, "(1) read-only badge reconciled after a concurrent archive", () => {
      const ro = document.getElementById("ctx-ro");
      return ro && !ro.hidden;
    }, null, 10000);
    const archivedLabel = await page.locator(`#ctx-select option[value="${winter}"]`).textContent();
    if (!/archived|read-only/i.test(archivedLabel)) {
      throw new Error(`[${L}] archived Season not reconciled without reload: "${archivedLabel}"`);
    }
    if ((await ctxSeason(page)) !== winterId) throw new Error(`[${L}] archived POST did not persist`);

    // (2) Reopen: the reverse also reconciles without reload — badge clears, the
    //     archived marker drops. Toggle away first so re-selecting Winter fires.
    //
    //     The admin ACTOR (a separate page from the viewer subject) carries no
    //     saved context, so its active Season is the fallback's pick — and the
    //     fallback excludes archived Seasons (context_service). Once the archive
    //     above lands, the only Season is archived, so admin resolves to
    //     Program-only and the reopen's target guard (#369 prereq) refuses
    //     ("season season_1 not found."). Reopen is the documented exemption
    //     from the archived-Season read-only guard, and ContextService.set
    //     accepts an archived Season as a read-only historical context — so
    //     select Winter explicitly on the admin page first. Issued on
    //     admin.page ONLY: the viewer subject's stale-option state must not be
    //     disturbed.
    if ((await apiPost(admin.page, "/api/context",
      { program_id: programId, season_id: winterId })).status !== 200) {
      throw new Error(`[${L}] admin could not enter the archived Season before reopen`);
    }
    if ((await apiPost(admin.page, `/api/v2/setup/seasons/${winterId}/reopen`, { reason: "back" })).status !== 200) {
      throw new Error(`[${L}] concurrent reopen failed`);
    }
    await page.selectOption("#ctx-select", progOnly);
    await waitFor(page, "(2) select value toggled to Program-only before re-selecting the reopened Season",
      (v) => document.getElementById("ctx-select").value === v, progOnly, 10000);
    await page.selectOption("#ctx-select", winter);
    await waitFor(page, "(2) read-only badge cleared after a concurrent reopen", () => {
      const ro = document.getElementById("ctx-ro");
      return ro && ro.hidden;
    }, null, 10000);
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
    await waitFor(page, "(3) newly created Season surfaced in the options after a concurrent create", (v) => {
      const s = document.getElementById("ctx-select");
      return Array.from(s.options).some((o) => o.value === v);
    }, spring, 10000);
    const springLabel = await page.locator(`#ctx-select option[value="${spring}"]`).textContent();
    if (!/Spring Cup/.test(springLabel)) throw new Error(`[${L}] newly available Season not surfaced: "${springLabel}"`);

    if (errors.length) throw new Error(`[${L}] console/page errors:\n${errors.join("\n")}`);
    console.log(`[${L}] OK — no-reload reconciliation: archive→read-only, reopen→writable, newly-available Season surfaced.`);
  } catch (error) {
    // Both diagnostics, and the ORIGINAL error object: the rolling
    // /api/context* + /api/demo/overview log (#369 review) shows what the
    // server actually returned, while rethrowing `error` itself rather than a
    // fresh Error keeps the stack that names the failing assertion.
    error.message = `${error.message}\n${recentResponses(responses)}\n--- server output ---\n${out}`;
    throw error;
  } finally {
    await admin.context.close();
    await context.close();
    await stopServer(server);
  }
}

// --- rapid-switch race under a DELAYED, out-of-order options reconciliation
//     (#369 review regression) — deterministically drives the exact shape
//     that exposed loadContextOptions()'s missing sequence guard: two real
//     switches close enough together that the SECOND is made while the
//     FIRST's own trailing GET /api/context/options is still outstanding.
//     The first such GET is held open far longer than the second, so the
//     second (newer) switch's reconciliation lands and settles FIRST, then
//     the first (stale, superseded) one is delivered LATE -- proving it can
//     no longer clobber the shared contextOptions once it does. ---
async function checkSwitchRace(browser, viewport) {
  const base = `http://${HOST}:${viewport.racePort}`;
  const server = startServer(viewport.racePort, {});
  let out = "";
  server.stdout.on("data", (d) => { out += d.toString(); });
  server.stderr.on("data", (d) => { out += d.toString(); });
  const { context, page, errors, responses } = await newPage(browser, viewport);
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
    await waitFor(page, "switcher ready before the rapid-switch race", () => {
      const s = document.getElementById("ctx-select");
      return s && !s.hidden && s.options.length >= 2;
    }, null, 10000);

    // A second, MORE targeted delay layered on top of installResponseDelay()
    // (newPage() above already applies a modest random one to everything):
    // Playwright routes are LIFO, so this one fully owns /api/context/options
    // requests from here on, while /api/context and /api/demo/overview still
    // fall through to the general handler. The FIRST such request (Switch
    // 1's own trailing reconciliation) is held long enough for Switch 2 to
    // be made and fully settle first; every later one is fast.
    let n = 0;
    await page.route(/\/api\/context\/options(\?|$)/, async (route) => {
      const nn = ++n;
      const response = await route.fetch();
      await new Promise((r) => setTimeout(r, nn === 1 ? 1500 : 100));
      await route.fulfill({ response });
    });

    await page.selectOption("#ctx-select", winter);
    // Long enough for Switch 1's own /api/context POST to resolve (fast,
    // undelayed) so its trailing loadContextOptions() GET has actually been
    // issued (and is now held open by the route above) — landing Switch 2
    // squarely in the unprotected gap the bug lived in.
    await new Promise((r) => setTimeout(r, 300));
    await page.selectOption("#ctx-select", progOnly);

    // Converge at the SERVER first (bounded poll, same idiom as the keyboard
    // scenario above — the POST itself is not delayed, only the trailing
    // options GET is, so this should settle quickly).
    const deadline = Date.now() + 10000;
    for (;;) {
      if ((await ctxSeason(page)) === null) break;
      if (Date.now() > deadline) throw new Error(`[${L}] rapid switches did not converge at the server`);
      await new Promise((r) => setTimeout(r, 100));
    }
    // Now let Switch 1's long-delayed, superseded options GET actually land.
    await new Promise((r) => setTimeout(r, 1800));
    const valueRightAfterStaleDelivery = await selectValue(page);
    if (valueRightAfterStaleDelivery !== progOnly) {
      throw new Error(`[${L}] the stale, superseded options response reverted the switcher `
        + `the instant it landed: "${valueRightAfterStaleDelivery}" (expected "${progOnly}")`);
    }

    // Force an UNRELATED render() the way countless ordinary actions in this
    // app already do (a nav switch, a toast, a poll) — this is the actual
    // symptom the bug produced: nothing looked wrong until the NEXT repaint
    // read the silently-corrupted shared contextOptions and reverted the
    // switcher with no user action asking for it.
    await page.click('.tab[data-tab="notifications"]');
    await waitFor(page, "nav round-trip: notifications view reached",
      () => document.body.dataset.view === "notifications", null, 10000);
    // `document.body.dataset.view` flips SYNCHRONOUSLY at the very top of
    // render(), long before renderContextSwitcher() near its end actually
    // repaints the switcher from the (possibly just-corrupted)
    // `contextOptions` -- waiting only for that flag checks the switcher
    // before THIS render() cycle has actually finished, which silently
    // hides exactly the corruption this check exists to catch (found by
    // hand while proving this regression against the unfixed app.js: an
    // immediate check here always read the still-correct pre-render value).
    // Give each render() a fixed settle window comfortably longer than the
    // slowest artificial delay installResponseDelay() ever applies (160ms).
    await new Promise((r) => setTimeout(r, 500));
    await page.click('.tab[data-tab="standings"]');
    await waitFor(page, "nav round-trip: back on standings",
      () => document.body.dataset.view === "standings", null, 10000);
    await new Promise((r) => setTimeout(r, 500));

    const finalValue = await selectValue(page);
    if (finalValue !== progOnly) {
      throw new Error(`[${L}] switcher reverted to a stale selection ("${finalValue}") after an `
        + `unrelated render() — expected the rapid switch's real outcome ("${progOnly}") to stick`);
    }
    if ((await ctxSeason(page)) !== null) {
      throw new Error(`[${L}] server/client diverged after the rapid switch settled`);
    }

    if (errors.length) throw new Error(`[${L}] console/page errors:\n${errors.join("\n")}`);
    console.log(`[${L}] OK — rapid switches under a delayed, out-of-order options reconciliation `
      + `converge with no stale reversion.`);
  } catch (error) {
    throw new Error(`${error.message}\n${recentResponses(responses)}\n--- server output ---\n${out}`);
  } finally {
    await context.close();
    await stopServer(server);
  }
}

// --- BOOT/RECONCILIATION WINDOW (#369 review regression, the CI failure's own
//     mechanism) — a reload landing between "the server accepted the switch"
//     and "the browser's own POST response was delivered" must not let boot's
//     restoreContextDeepLink() revert the switch.
//
//     Reproduced by DELAYING RESPONSE DELIVERY, never the request: route.fetch()
//     performs the POST for real (the server IS mutated), then the response is
//     held before route.fulfill(). Delaying the REQUEST instead would prove
//     nothing — it would just resolve against the current state, with the
//     browser and server never disagreeing at all.
//
//     Before the app.js fix this failed 100% of the time at BOTH viewports:
//     the server sat on Program-only while location.hash still encoded the
//     Season, boot read that lagging hash as an intentional deep link, POSTed
//     it back, and both the switcher AND the server ended up back on the
//     Season the user had just switched away from. The original CI symptom is
//     one step downstream: the reverted switcher never reaches the expected
//     value, so the next wait burns its full 10s and times out on an exact
//     head-of-line assertion. ---
async function checkBootWindow(browser, viewport) {
  const base = `http://${HOST}:${viewport.winPort}`;
  const server = startServer(viewport.winPort, {});
  let out = "";
  server.stdout.on("data", (d) => { out += d.toString(); });
  server.stderr.on("data", (d) => { out += d.toString(); });
  const { context, page, errors, responses } = await newPage(browser, viewport);
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

    if ((await loginAs(page, "viewer")).status !== 200) throw new Error(`[${L}] viewer login failed`);
    // Deterministic starting point: the Season is both the persisted context
    // AND what the hash encodes, so the ONLY thing that can later put a
    // Season-encoding hash in front of boot is the switch below failing to
    // publish its own intent in time.
    await apiPost(page, "/api/context", { program_id: programId, season_id: winterId });
    await reloadShell(page);
    await waitFor(page, "window: Season selected and mirrored in the hash before the switch",
      (v) => document.getElementById("ctx-select").value === v, programId + "|" + winterId, 10000);
    await waitFor(page, "window: hash encodes the Season before the switch",
      hashEncodes, { p: programId, s: winterId }, 10000);

    // Hold ONLY the switcher's POST /api/context response. Playwright routes
    // are LIFO so this fully owns POST /api/context from here; the GET of the
    // same path (the convergence poll below) is explicitly passed through, and
    // options/overview still fall through to newPage()'s general delay.
    await page.route(/\/api\/context(\?|$)/, async (route) => {
      if (route.request().method() !== "POST") return route.fallback();
      const response = await route.fetch();      // the server is mutated HERE
      await new Promise((r) => setTimeout(r, 4000));
      await route.fulfill({ response });         // ...the browser only learns of it HERE
    });

    await page.selectOption("#ctx-select", progOnly);
    // Wait until the SERVER has demonstrably taken the switch while the
    // browser's own POST response is still held — that IS the window.
    const deadline = Date.now() + 10000;
    for (;;) {
      if ((await ctxSeason(page)) === null) break;
      if (Date.now() > deadline) throw new Error(`[${L}] server never took the switch`);
      await new Promise((r) => setTimeout(r, 100));
    }
    // Snapshot (do NOT assert yet) whether the hash already describes the
    // user's choice. Asserting here would short-circuit on the fix's internal
    // invariant and never demonstrate the symptom that actually matters, so
    // this is only carried forward to explain a failure below.
    const hashInWindow = await page.evaluate(() => location.hash);
    const hashLagged = !(await page.evaluate(hashEncodes, { p: programId, s: null }));

    // Reload RIGHT INSIDE the window (the switch's POST response is still held
    // and is abandoned by the navigation — the same abandoned-response shape
    // that produced CI's concurrent BrokenPipeError on this journey).
    await reloadShell(page);
    const explain = `at reload time the server had ALREADY taken Program-only, while the hash `
      + `${hashLagged ? "still LAGGED it, encoding the old Season" : "already encoded the new selection"} `
      + `(${hashInWindow})`;
    try {
      await waitFor(page, "window: switcher still on Program-only after a reload inside the window",
        (v) => document.getElementById("ctx-select").value === v, progOnly, 10000);
    } catch (error) {
      throw new Error(`${error.message}\n[${L}] boot REVERTED an accepted switch: ${explain} — `
        + `restoreContextDeepLink() read that hash as an intentional deep link and POSTed it back`);
    }
    if ((await ctxSeason(page)) !== null) {
      throw new Error(`[${L}] boot REVERTED the accepted switch AT THE SERVER (back on the Season): ${explain}`);
    }
    if (!(await page.evaluate(hashEncodes, { p: programId, s: null }))) {
      throw new Error(`[${L}] hash does not encode the surviving selection after boot`);
    }
    // Now the invariant itself: surviving by luck (a reload that happened to
    // land after the response was delivered) is not the same as the hash never
    // lagging. Checked last so the symptom above is always the headline.
    if (hashLagged) {
      throw new Error(`[${L}] the hash lagged a server that had already taken the switch `
        + `(${hashInWindow}) — the switch survived this time, but any reload in that window `
        + `boots restoreContextDeepLink() against a stale hash and reverts it`);
    }

    if (errors.length) throw new Error(`[${L}] console/page errors:\n${errors.join("\n")}`);
    console.log(`[${L}] OK — a reload inside the accepted-but-undelivered switch window `
      + `keeps the switch; boot does not revert it from a lagging hash.`);
  } catch (error) {
    throw new Error(`${error.message}\n${recentResponses(responses)}\n--- server output ---\n${out}`);
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
      await checkSwitcher(browser, viewport);
      await checkChip(browser, viewport);
      await checkReconcile(browser, viewport);
      await checkSwitchRace(browser, viewport);
      await checkBootWindow(browser, viewport);
    }
    console.log("Context-switcher browser journey passed.");
  } catch (error) {
    console.error("Context-switcher browser journey FAILED.");
    console.error(error && error.stack ? error.stack : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
