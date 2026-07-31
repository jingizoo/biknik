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
//     sign back in);
//   * keeps the dialog focus contract on the SUCCESS step — focus is moved into
//     the success dialog, Tab/Shift+Tab stay trapped inside it, and "Return to
//     sign-in" is reachable and activatable from the keyboard alone (#369
//     review's required regression; see checkSuccessDialogFocus below for why
//     this step in particular is the fragile one).
//
// Explicitly NOT re-tested here: the generic dialog focus lifecycle itself
// (open/close/restore across ordinary modals and drawers) — that is
// accessibility-foundations.js / shell-accessibility-coverage.js's scope. What
// is proven here is only that the factory-reset SUCCESS step, which paints
// through its own bespoke render() early return, is wired into that shared
// lifecycle like every other surface.
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
// setShellTitle("login") in app.js — the sign-in surface's own title, asserted
// verbatim so "we ended up somewhere signed-out" isn't mistaken for "we ended
// up on the sign-in screen".
const SIGN_IN_TITLE = "Sign in — Hockey Scheduler";
// Mirrors app.js's OVERLAY_SEL / FOCUSABLE_SEL (dialog focus management) and
// the visibility filter overlayFocusables() applies, so the first/last Tab
// stops asserted below are the ones the REAL trap computes rather than a
// hard-coded guess that would quietly stop matching if the modal's markup
// gained or lost a control.
const OVERLAY_SEL = ".modal, .drawer";
const FOCUSABLE_SEL = [
  "a[href]", "button:not([disabled])", "input:not([disabled])",
  "select:not([disabled])", "textarea:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
].join(",");

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

// --- #369 review: the success step's dialog focus contract ------------------
// Where document.activeElement sits relative to the topmost open overlay,
// resolved exactly the way app.js's openOverlayElement() does (last match
// wins). `index` is the position among the dialog's sequential Tab stops and
// is -1 when focus is elsewhere — including on the tabindex="-1" CONTAINER,
// which overlayFocusables() deliberately excludes and which is precisely where
// focusOverlayContainer() parks focus. Everything needed for a self-explaining
// failure message comes back in the same round trip.
function dialogFocusState(page) {
  return page.evaluate(([oSel, fSel]) => {
    const describe = (el) => {
      if (!el) return "(none)";
      if (el === document.body) return "<body>";
      const bits = Array.from(el.attributes || [])
        .filter((a) => a.name === "id" || a.name === "class"
          || a.name.slice(0, 5) === "data-")
        .map((a) => `${a.name}="${a.value}"`);
      const text = (el.textContent || "").trim().replace(/\s+/g, " ").slice(0, 32);
      return `<${el.tagName.toLowerCase()}${bits.length ? ` ${bits.join(" ")}` : ""}>${text}`;
    };
    const all = document.querySelectorAll(oSel);
    const overlay = all.length ? all[all.length - 1] : null;
    const active = document.activeElement;
    if (!overlay) return { overlay: false, active: describe(active) };
    const stops = Array.from(overlay.querySelectorAll(fSel)).filter(
      (el) => el.offsetParent !== null || el.getClientRects().length > 0);
    return {
      overlay: true,
      inside: !!(active && overlay.contains(active)),
      isContainer: active === overlay,
      index: stops.indexOf(active),
      count: stops.length,
      stops: stops.map(describe),
      active: describe(active),
    };
  }, [OVERLAY_SEL, FOCUSABLE_SEL]);
}

// Park focus on a known edge of the trap (0 = first stop, -1 = last) so the
// wrap assertions below start from a defined position.
function focusDialogStop(page, which) {
  return page.evaluate(([oSel, fSel, w]) => {
    const all = document.querySelectorAll(oSel);
    const overlay = all.length ? all[all.length - 1] : null;
    if (!overlay) return false;
    const stops = Array.from(overlay.querySelectorAll(fSel)).filter(
      (el) => el.offsetParent !== null || el.getClientRects().length > 0);
    if (!stops.length) return false;
    const el = w < 0 ? stops[stops.length - 1] : stops[w];
    el.focus();
    return document.activeElement === el;
  }, [OVERLAY_SEL, FOCUSABLE_SEL, which]);
}

// The success modal is the ONE dialog in the app that does not paint through
// the normal render() pipeline: render() has a dedicated early return for
// `modal.type === "factory-reset" && modal.step === "success"`, because the
// reset already signed this session out server-side and the pipeline's
// session-gated /api/demo/overview read would 401 and replace the success
// confirmation with an error banner the operator never asked for. That early
// return has to end with the SAME shared syncOverlayFocus() every other
// render() path ends with. When it did not, focus was left on the removed
// "Resetting…" control — i.e. it fell to <body> — so the dialog was visibly on
// screen while keyboard and screen-reader users had silently lost it. These
// assertions fail if that call is ever dropped again.
async function checkSuccessDialogFocus(page, L) {
  // (1) Focus is INSIDE the dialog. focusOverlayContainer() targets the dialog
  //     CONTAINER (tabindex="-1"), never a form control, so containment is the
  //     assertable contract — not an exact element match. The move can be
  //     established from a queued requestAnimationFrame, so poll for it rather
  //     than reading activeElement once.
  await page.waitForFunction(([oSel]) => {
    const all = document.querySelectorAll(oSel);
    const overlay = all.length ? all[all.length - 1] : null;
    return !!(overlay && document.activeElement
      && overlay.contains(document.activeElement));
  }, [OVERLAY_SEL], { timeout: 5000 }).catch(async () => {
    const s = await dialogFocusState(page);
    throw new Error(`[${L}] after a completed reset, focus is NOT inside the `
      + `success dialog (activeElement=${s.active}) — render()'s `
      + `factory-reset-success early return must still call syncOverlayFocus(), `
      + `like every other render() path does`);
  });

  const opened = await dialogFocusState(page);
  if (opened.count < 2) {
    throw new Error(`[${L}] the success dialog exposes ${opened.count} keyboard `
      + `stop(s) (${JSON.stringify(opened.stops)}) — the wrap assertions below `
      + `need at least two to mean anything; update this check if the modal's `
      + `markup legitimately changed`);
  }

  // (2a) ENTRY BOUNDARY. Focus lands on the container, which is NOT one of the
  //      sequential stops, so the very first keystroke is the one that used to
  //      escape: backward navigation from a tabindex="-1" element walks to
  //      whatever precedes it in the document, straight out of the dialog.
  //      Only assert this when focus really is on the container (that is what
  //      the lifecycle promises) so the check can never pass vacuously.
  if (opened.isContainer) {
    await page.keyboard.press("Shift+Tab");
    const entry = await dialogFocusState(page);
    if (entry.index !== entry.count - 1) {
      throw new Error(`[${L}] Shift+Tab from the focused dialog CONTAINER must `
        + `enter the trap at its LAST stop, not leave the dialog — landed on `
        + `${entry.active} (index ${entry.index}/${entry.count}, `
        + `inside=${entry.inside})`);
    }
  }

  // (2b) The trap wraps in both directions.
  if (!(await focusDialogStop(page, -1))) {
    throw new Error(`[${L}] could not focus the success dialog's last keyboard stop`);
  }
  await page.keyboard.press("Tab");
  let s = await dialogFocusState(page);
  if (s.index !== 0) {
    throw new Error(`[${L}] Tab from the LAST focusable in the success dialog `
      + `must wrap to the FIRST — landed on ${s.active} (index ${s.index}, `
      + `inside=${s.inside}, stops=${JSON.stringify(s.stops)})`);
  }
  if (!(await focusDialogStop(page, 0))) {
    throw new Error(`[${L}] could not focus the success dialog's first keyboard stop`);
  }
  await page.keyboard.press("Shift+Tab");
  s = await dialogFocusState(page);
  if (s.index !== s.count - 1) {
    throw new Error(`[${L}] Shift+Tab from the FIRST focusable in the success `
      + `dialog must wrap to the LAST — landed on ${s.active} (index `
      + `${s.index}/${s.count}, inside=${s.inside}, stops=${JSON.stringify(s.stops)})`);
  }
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
    // #369 review: the success dialog must still hold the keyboard before we
    // leave it — focus inside, Tab/Shift+Tab trapped.
    await checkSuccessDialogFocus(page, L);
    // ...and "Return to sign-in" is activated from the KEYBOARD, not a mouse
    // click: the whole point of the contract is that an operator who never
    // touches a pointer can still get off this screen.
    await page.focus("[data-fr-done]");
    await page.keyboard.press("Enter");
    await page.waitForSelector("#login-screen:not([hidden])", { timeout: 10000 });
    // The whole authenticated shell is hidden behind body.signed-out while the
    // operator is logged out — proving the sign-out actually took effect, not
    // just that a success card was shown.
    await page.waitForFunction(() => document.body.classList.contains("signed-out"),
                               null, { timeout: 10000 });
    // The keyboard activation reached the sign-in SURFACE, not merely some
    // signed-out state: setShellTitle("login") is what a screen-reader user
    // actually hears on arrival, so assert the real string.
    await page.waitForFunction((t) => document.title === t,
                               SIGN_IN_TITLE, { timeout: 10000 })
      .catch(async () => {
        const actual = await page.evaluate(() => document.title);
        throw new Error(`[${L}] keyboard-activating "Return to sign-in" must land `
          + `on the sign-in surface — expected document.title `
          + `${JSON.stringify(SIGN_IN_TITLE)}, got ${JSON.stringify(actual)}`);
      });

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
    console.log(`[${L}] OK — preview, gating, cancel, denied (no wipe), reset, `
      + `success-dialog focus trap, keyboard return to sign-in + re-login.`);
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
