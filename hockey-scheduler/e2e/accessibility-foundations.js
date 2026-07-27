// Accessibility foundations (#204/#345): the three shell-level affordances
// that were entirely absent before this issue -- verified as real keyboard
// behaviour in a real browser, not as markup presence.
//
// 1. Skip to main content. The FIRST Tab from a fresh page must land on a
//    visible "Skip to main content" link (off-screen until focused, so it
//    must actually move on screen), and activating it must move focus into
//    the #content region -- i.e. past the entire sidebar nav. Asserted by
//    counting how many Tab presses it takes to reach a content control with
//    and without the skip link, so the test proves the link SAVES traversal
//    rather than merely existing.
//
// 2. Per-view page titles. A single-page app never reloads, so document.title
//    must track the active view. Asserted on a full page boot and on the
//    Initial Setup landing (both of which reach a view WITHOUT switchTab()
//    ever running -- onboarding.js renders via its own wrapper that returns
//    before the base render), as well as across ordinary nav clicks. This app
//    has no per-view hash routing (only #public and #ctx=), so a boot rather
//    than a "#games" deep link is the real non-switchTab path.
//
// 3. Dialog focus CONTAINMENT. An open create drawer / confirm modal already
//    carried role="dialog" aria-modal="true", but aria-modal alone does not
//    constrain the keyboard. Asserted by tabbing forward past the last
//    control and backward past the first, requiring focus to stay inside the
//    dialog both times.
//
//    NOT asserted here, because it is not implemented yet: moving focus into
//    the dialog on open and restoring it to the trigger on close. Both were
//    built and then deliberately backed out of this slice -- calling .focus()
//    from the render cycle destabilised the deliberately-raced journeys in
//    home-tasks-hub.js (see the comment on syncOverlayFocus in app.js).
//    Follow-up on #345.
//
// Runs at desktop and canonical 390x844. Fails on any browser console/page
// error.

const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8331 },
  { label: "phone", width: 390, height: 844, port: 8332 },
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

async function apiPost(page, p, body) {
  return page.evaluate(async (arg) => (await fetch(arg.p, {
    method: "POST", credentials: "same-origin",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(arg.body),
  })).json(), { p, body });
}

async function loginAs(page, username, password) {
  const res = await apiPost(page, "/api/auth/login", { username, password });
  if (res && res.error) throw new Error(`login as ${username} failed: ${JSON.stringify(res)}`);
}

// Describe whatever currently has focus, for assertions and failure messages.
function activeInfo(page) {
  return page.evaluate(() => {
    const el = document.activeElement;
    if (!el) return null;
    return {
      tag: el.tagName.toLowerCase(),
      id: el.id || null,
      cls: el.className || "",
      text: (el.textContent || "").trim().slice(0, 60),
      inContent: !!(el.closest && el.closest("#content")),
      inDialog: !!(el.closest && el.closest(".modal, .drawer")),
      isSkip: el.id === "skip-link",
    };
  });
}

function fail(msg) { throw new Error(msg); }

async function checkViewport(browser, viewport) {
  const base = `http://${HOST}:${viewport.port}`;
  const server = spawn(
    process.env.PYTHON || "python3",
    ["-u", "-m", "hockey_scheduler.web.server", "--host", HOST, "--port", String(viewport.port)],
    { cwd: BACKEND_DIR, stdio: ["ignore", "pipe", "pipe"] });
  let serverOutput = "";
  server.stdout.on("data", (d) => { serverOutput += d.toString(); });
  server.stderr.on("data", (d) => { serverOutput += d.toString(); });

  const context = await browser.newContext({
    viewport: { width: viewport.width, height: viewport.height },
  });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(`[pageerror] ${e.message}`));
  page.on("console", (m) => { if (m.type() === "error") errors.push(`[console] ${m.text()}`); });

  try {
    await waitForServer(`${base}/`, READY_TIMEOUT_MS);
    await page.goto(base);
    await loginAs(page, "admin", "demo");
    await page.goto(base);
    await page.waitForSelector("#content .dash-card, #content h2, #content h3",
      { timeout: 15000 });

    // ---- (1) skip to main content ----------------------------------------
    // Focus the document body first so the very next Tab is the document's
    // FIRST tab stop, not a continuation from wherever load left focus.
    await page.evaluate(() => {
      if (document.activeElement && document.activeElement.blur) {
        document.activeElement.blur();
      }
    });
    await page.keyboard.press("Tab");
    const firstStop = await activeInfo(page);
    if (!firstStop || !firstStop.isSkip) {
      fail(`skip link: expected the FIRST tab stop to be #skip-link, got `
        + `${JSON.stringify(firstStop)}`);
    }
    // It must become visibly on-screen when focused -- an off-screen-forever
    // link is unusable for a sighted keyboard user. The reveal is a CSS
    // transition, so poll for the SETTLED box rather than reading one frame
    // mid-slide (a one-shot read here measured top:-35 of a -60 -> 0 slide).
    await page.waitForFunction(() => {
      const el = document.getElementById("skip-link");
      if (!el) return false;
      const r = el.getBoundingClientRect();
      return r.top >= 0 && r.height > 0 && r.width > 0;
    }, null, { timeout: 5000 }).catch(async () => {
      const box = await page.evaluate(() => {
        const r = document.getElementById("skip-link").getBoundingClientRect();
        return { top: r.top, height: r.height, width: r.width };
      });
      fail(`skip link: expected it to settle on-screen while focused, got `
        + `${JSON.stringify(box)}`);
    });
    const skipBox = await page.evaluate(() => {
      const r = document.getElementById("skip-link").getBoundingClientRect();
      return { top: r.top, height: r.height, width: r.width };
    });
    // WCAG 2.2 AA target size (24x24 CSS px minimum).
    if (skipBox.height < 24 || skipBox.width < 24) {
      fail(`skip link: target smaller than the 24x24 WCAG 2.2 AA minimum: `
        + `${JSON.stringify(skipBox)}`);
    }

    // Activating it must move focus into #content, skipping the nav.
    await page.keyboard.press("Enter");
    // The href="#content" jump makes #content the browser's focus target
    // only if it is focusable; assert focus actually landed inside it.
    await page.waitForFunction(() => {
      const el = document.activeElement;
      return !!(el && (el.id === "content"
        || (el.closest && el.closest("#content"))));
    }, null, { timeout: 5000 }).catch(async () => {
      const info = await activeInfo(page);
      fail(`skip link: activating it did not move focus into #content; `
        + `focus is ${JSON.stringify(info)}`);
    });

    // Prove it actually SAVES traversal: reaching content by tabbing PAST the
    // skip link (through the whole sidebar) must take strictly more stops
    // than skip-link + Enter. Measured from a fresh load of the BARE url --
    // blur() alone does not reset the browser's sequential-focus origin, and
    // a plain reload() would keep the "#content" fragment the skip link just
    // put in the address bar (which re-focuses #content on load); either way
    // the count comes back a meaningless 1.
    await page.goto(base);
    await page.waitForSelector("#content .dash-card, #content h2, #content h3",
      { timeout: 15000 });
    let tabsWithoutSkip = 0;
    let reachedContent = false;
    for (let i = 0; i < 80; i += 1) {
      await page.keyboard.press("Tab");
      tabsWithoutSkip += 1;
      const info = await activeInfo(page);
      if (info && info.inContent) { reachedContent = true; break; }
    }
    if (!reachedContent) {
      fail("skip link: never reached #content by tabbing, so the traversal "
        + "comparison could not be made");
    }
    // skip-link + Enter is 2 keystrokes; the sidebar route must cost more.
    if (tabsWithoutSkip <= 2) {
      fail(`skip link: expected tabbing past the sidebar to cost more than the `
        + `2 keystrokes the skip link costs (otherwise it proves nothing), `
        + `took ${tabsWithoutSkip}`);
    }

    // ---- (2) per-view page titles ----------------------------------------
    const titleNow = () => page.evaluate(() => document.title);
    // A fresh Program LANDS on the Initial Setup wizard, and that view is
    // rendered by onboarding.js's own render wrapper, which returns before
    // the base render() -- so it is the one destination most likely to be
    // left with the static index.html title. Assert the landing view first.
    const landingView = await page.evaluate(() => document.body.dataset.view);
    await page.waitForFunction(() => document.title !== ""
      && !/Operator Console$/.test(document.title),
      null, { timeout: 10000 }).catch(async () => {
      fail(`page title: the landing view ("${landingView}") still shows the `
        + `static index.html title "${await titleNow()}" -- render paths that `
        + `bypass the base render() must set it too`);
    });
    if (landingView === "onboarding") {
      const t = await titleNow();
      if (!/^Initial Setup —/.test(t)) {
        fail(`page title: expected the Initial Setup landing to be titled `
          + `"Initial Setup — …", got "${t}"`);
      }
    }
    // Now move to a normal view and assert it retitles.
    await page.click('.tab[data-tab="dashboard"]');
    await page.waitForFunction(() => /^Dashboard —/.test(document.title),
      null, { timeout: 10000 }).catch(async () => {
      fail(`page title: expected the Dashboard title to start with `
        + `"Dashboard —", got "${await titleNow()}"`);
    });
    // A nav click must retitle.
    await page.click('.tab[data-tab="calendar"]');
    await page.waitForFunction(() => /^Arena Calendar —/.test(document.title),
      null, { timeout: 10000 }).catch(async () => {
      fail(`page title: after opening Arena Calendar, title is `
        + `"${await titleNow()}"`);
    });
    // A full page boot reaches its landing view WITHOUT switchTab() ever
    // running, so it proves render() itself titles rather than only the nav
    // click path. (This app has no per-view hash routing -- only #public and
    // #ctx= -- so a reload, not a "#games" deep link, is the real case.)
    await page.goto(base);
    await page.waitForSelector("#content .dash-card, #content h2, #content h3",
      { timeout: 15000 });
    const bootTitle = await titleNow();
    if (/Operator Console$/.test(bootTitle) || !/—/.test(bootTitle)) {
      fail(`page title: a full page boot left the static title in place `
        + `("${bootTitle}") -- render() must title its own landing view`);
    }
    // Titles must differ per view -- a constant title would pass a single
    // check but tells a screen-reader user nothing changed.
    await page.click('.tab[data-tab="standings"]');
    await page.waitForFunction(() => /^Standings —/.test(document.title),
      null, { timeout: 10000 }).catch(async () => {
      fail(`page title: after opening Standings, title is "${await titleNow()}"`);
    });

    // ---- (3) dialog focus trapping ---------------------------------------
    // The topbar "Add Ice" shortcut opens a real create drawer.
    const trigger = await page.$('.topbar [data-open-drawer="ice-slot"]');
    if (!trigger) fail("focus containment: could not find the topbar Add Ice trigger");
    await trigger.focus();
    await trigger.click();
    await page.waitForSelector(".drawer, .modal", { timeout: 10000 });
    // Put focus inside the dialog explicitly: the app does not yet move it
    // there on open (see the header note), and containment is what this
    // asserts. Focusing the first control here is the test standing in for
    // a user who has tabbed/clicked into the dialog.
    await page.evaluate(() => {
      const d = document.querySelector(".modal, .drawer");
      const f = d && d.querySelector('a[href], button:not([disabled]), '
        + 'input:not([disabled]), select:not([disabled]), '
        + 'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])');
      if (f) f.focus();
    });
    const seeded = await activeInfo(page);
    if (!seeded || !seeded.inDialog) {
      fail(`focus containment: could not place focus inside the dialog to `
        + `begin with, got ${JSON.stringify(seeded)}`);
    }

    // Tab forward well past the number of controls in the dialog; focus must
    // never leave it. 40 is comfortably more than any dialog's control count,
    // so this wraps several times if the trap works.
    for (let i = 0; i < 40; i += 1) {
      await page.keyboard.press("Tab");
      const info = await activeInfo(page);
      if (!info || !info.inDialog) {
        fail(`focus containment: Tab #${i + 1} escaped the dialog to `
          + `${JSON.stringify(info)}`);
      }
    }
    // And backward, which is the direction a naive "focus the first element"
    // implementation gets wrong.
    for (let i = 0; i < 40; i += 1) {
      await page.keyboard.press("Shift+Tab");
      const info = await activeInfo(page);
      if (!info || !info.inDialog) {
        fail(`focus containment: Shift+Tab #${i + 1} escaped the dialog to `
          + `${JSON.stringify(info)}`);
      }
    }

    // Escape still closes it (pre-existing behaviour, kept working).
    await page.keyboard.press("Escape");
    await page.waitForFunction(() =>
      !document.querySelector(".drawer, .modal"), null, { timeout: 10000 });

    if (errors.length) {
      fail(`browser errors:\n${errors.join("\n")}`);
    }
    console.log(`[${viewport.label}] OK — skip link is the first tab stop, is `
      + `on-screen and target-size compliant when focused, and reaches content `
      + `in 2 keystrokes where tabbing the sidebar takes ${tabsWithoutSkip}; `
      + `page titles track the view on a full page boot (no switchTab), on the `
      + `onboarding landing that bypasses the base render, and across nav `
      + `clicks; an open dialog contains Tab and Shift+Tab and still closes on `
      + `Escape.`);
  } catch (e) {
    if (serverOutput.trim()) {
      console.error("--- demo server output ---\n" + serverOutput.trim());
    }
    throw e;
  } finally {
    await context.close();
    await stopServer(server);
  }
}

(async () => {
  const browser = await chromium.launch();
  try {
    for (const viewport of VIEWPORTS) await checkViewport(browser, viewport);
    console.log("Accessibility foundations browser journey passed.");
  } finally {
    await browser.close();
  }
})().catch((e) => {
  console.error("Accessibility foundations browser journey FAILED.");
  console.error(e.message);
  process.exit(1);
});
