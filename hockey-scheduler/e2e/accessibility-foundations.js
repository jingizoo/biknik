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
    // Focus must ENTER the dialog on open, and specifically onto the dialog
    // CONTAINER -- not a form control, whose focus/blur/change events are what
    // destabilised the held-response journeys in the first attempt.
    await page.waitForFunction(() => {
      const el = document.activeElement;
      return !!(el && el.matches && el.matches(".modal, .drawer"));
    }, null, { timeout: 5000 }).catch(async () => {
      fail(`focus on open: expected focus to land on the dialog CONTAINER, `
        + `got ${JSON.stringify(await activeInfo(page))}`);
    });

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

    // Escape closes it AND returns focus to the element that opened it.
    await page.keyboard.press("Escape");
    await page.waitForFunction(() =>
      !document.querySelector(".drawer, .modal"), null, { timeout: 10000 });
    await page.waitForFunction(() => {
      const el = document.activeElement;
      return !!(el && el.matches
        && el.matches('.topbar [data-open-drawer="ice-slot"]'));
    }, null, { timeout: 5000 }).catch(async () => {
      fail(`focus restore (Escape): expected focus back on the Add Ice `
        + `trigger, got ${JSON.stringify(await activeInfo(page))}`);
    });

    // Close BUTTON restores too -- a different close path from Escape.
    await trigger.click();
    await page.waitForSelector(".drawer, .modal", { timeout: 10000 });
    await page.waitForFunction(() => {
      const el = document.activeElement;
      return !!(el && el.matches && el.matches(".modal, .drawer"));
    }, null, { timeout: 5000 });
    await page.click(".drawer-x[data-drawer-close]");
    await page.waitForFunction(() =>
      !document.querySelector(".drawer, .modal"), null, { timeout: 10000 });
    await page.waitForFunction(() => {
      const el = document.activeElement;
      return !!(el && el.matches
        && el.matches('.topbar [data-open-drawer="ice-slot"]'));
    }, null, { timeout: 5000 }).catch(async () => {
      fail(`focus restore (close button): expected focus back on the Add Ice `
        + `trigger, got ${JSON.stringify(await activeInfo(page))}`);
    });

    // Removed trigger: if the element that opened the dialog is gone by the
    // time it closes, focus must land on the view-level fallback rather than
    // being dropped to <body>.
    await trigger.click();
    await page.waitForSelector(".drawer, .modal", { timeout: 10000 });
    await page.evaluate(() => {
      const t = document.querySelector('.topbar [data-open-drawer="ice-slot"]');
      if (t) t.remove();
    });
    await page.keyboard.press("Escape");
    await page.waitForFunction(() =>
      !document.querySelector(".drawer, .modal"), null, { timeout: 10000 });
    // The dialog leaves the DOM as soon as innerHTML is rewritten, which is
    // BEFORE the end-of-render restore runs -- poll for focus to settle
    // rather than reading it one-shot the instant the node disappears.
    await page.waitForFunction(() => {
      const el = document.activeElement;
      return !!(el && el !== document.body);
    }, null, { timeout: 5000 }).catch(async () => {
      fail(`focus restore (removed trigger): focus was left on <body> instead `
        + `of the view fallback, got ${JSON.stringify(await activeInfo(page))}`);
    });

    // ---- (3b) re-render, no-steal, and modal-over-drawer nesting ----------
    // Everything above opens from the TOPBAR, whose trigger lives outside
    // #content and therefore survives every render. The Setup view is where
    // the harder cases are: its triggers are inside #content, so the render
    // that paints the dialog destroys them.
    await page.click('.tab[data-tab="setup"]');
    await page.waitForFunction(() => document.body.dataset.view === "setup",
      null, { timeout: 10000 });
    // Setup opens on the Hierarchy sub-view; the per-entity cards (and their
    // "+ New" / delete controls) live under Records.
    await page.click('[data-setup-view="records"]');
    const CLUB_NEW = '.setup-card .sc-new[data-drawer="club"]';
    await page.waitForSelector(CLUB_NEW, { timeout: 10000 });
    const drawerContainerFocused = (label) => page.waitForFunction(() => {
      const el = document.activeElement;
      return !!(el && el.matches && el.matches(".drawer[role=dialog]"));
    }, null, { timeout: 5000 }).catch(async () => {
      fail(`${label}: expected focus on the drawer container, got `
        + `${JSON.stringify(await activeInfo(page))}`);
    });

    await page.click(CLUB_NEW);
    await page.waitForSelector(".drawer", { timeout: 10000 });
    await drawerContainerFocused("focus on open (in-view trigger)");

    // (a) A RE-RENDER while the dialog stays open must not strand focus.
    // render() rewrites #content wholesale, so the container focus was just
    // put on is destroyed on every subsequent render -- focus falls to
    // <body>. Submitting the Club drawer with its required name empty takes
    // app.js's CLIENT-SIDE validation branch: drawerError is set and
    // render() re-runs with the drawer still open, and no request is made
    // (so this cannot log a console error and mask itself).
    await page.click('[data-drawer-submit="club"]');
    await page.waitForSelector(".drawer .drawer-err", { timeout: 10000 });
    await drawerContainerFocused("re-render while open");

    // Now complete the create for real. This both leaves a row with a delete
    // control -- the only way to open a modal OVER a drawer below, since the
    // Records cards start empty -- and exercises restore on the ordinary
    // success path: the trigger lives inside #content, so the node captured
    // at open time was destroyed by the renders since, and focus can only get
    // back to it by re-resolving the selector.
    await page.fill("#f-club", "A11y Focus Club");
    await page.click('[data-drawer-submit="club"]');
    await page.waitForFunction(() => !document.querySelector(".drawer"),
      null, { timeout: 10000 });
    await page.waitForFunction((sel) => {
      const el = document.activeElement;
      return !!(el && el.matches && el.matches(sel));
    }, CLUB_NEW, { timeout: 5000 }).catch(async () => {
      fail(`restore (in-view trigger, re-resolved after re-render): expected `
        + `focus back on ${CLUB_NEW}, got ${JSON.stringify(await activeInfo(page))}`);
    });

    // (b) Closing must NOT steal focus that is already somewhere real. This
    // is the rule home-tasks-hub's context-switch case (I) depends on: a
    // switch that dismisses an open drawer from the control the operator is
    // still on has to leave them there, not yank them back to the trigger of
    // a drawer that no longer exists. The skip link is a stable target
    // outside the dialog, inside .web.
    await page.evaluate(() => document.getElementById("skip-link").focus());
    await page.keyboard.press("Escape");
    await page.waitForFunction(() => !document.querySelector(".drawer"),
      null, { timeout: 10000 });
    const afterNoSteal = await activeInfo(page);
    if (!afterNoSteal || !afterNoSteal.isSkip) {
      fail(`close must not steal focus: focus was on #skip-link (outside the `
        + `dialog) when the drawer closed, so it had to stay there; got `
        + `${JSON.stringify(afterNoSteal)}`);
    }

    // (c) Modal over drawer. The modal is appended after the drawer, so it is
    // topmost and owns focus; closing it returns to the drawer underneath;
    // closing THAT returns to the drawer's own trigger -- proving the nested
    // open never overwrote the outermost return target.
    await page.click(CLUB_NEW);
    await page.waitForSelector(".drawer", { timeout: 10000 });
    await drawerContainerFocused("focus on open (before nesting)");
    // The drawer scrim (z-index 40) covers the rows behind it, so a real
    // mouse cannot reach a delete control while the drawer is open. Dispatch
    // the pointerdown the capture-phase trigger listener watches for, then
    // invoke the handler directly -- this drives the identical app code path
    // a click would, including the trigger capture, without fighting
    // hit-testing for a state combination the app itself allows.
    const openedNested = await page.evaluate(() => {
      const del = document.querySelector('#content [data-del="club"]');
      if (!del) return false;
      del.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
      del.click();
      return true;
    });
    if (!openedNested) {
      fail('modal over drawer: found no [data-del="club"] control in the '
        + "Records view to open a confirm modal with, though the Club create "
        + "above reported success");
    }
    await page.waitForSelector(".modal", { timeout: 10000 });
    await page.waitForFunction(() => {
      const el = document.activeElement;
      return !!(el && el.matches && el.matches(".modal[role=dialog]"));
    }, null, { timeout: 5000 }).catch(async () => {
      fail(`modal over drawer: expected the topmost dialog (the modal) to own `
        + `focus, got ${JSON.stringify(await activeInfo(page))}`);
    });
    if (!(await page.$(".drawer"))) {
      fail("modal over drawer: the drawer should still be open underneath");
    }
    // First Escape closes only the modal (app.js checks `modal` before
    // `drawer`); focus returns to the drawer still open underneath.
    await page.keyboard.press("Escape");
    await page.waitForFunction(() => !document.querySelector(".modal"),
      null, { timeout: 10000 });
    await drawerContainerFocused("modal closed, drawer still open");
    // Second Escape closes the drawer. Focus must land on the drawer's OWN
    // trigger -- not the delete control that opened the nested modal, and not
    // the view heading. The original node was destroyed by the renders above,
    // so this also exercises re-resolving the trigger by selector.
    await page.keyboard.press("Escape");
    await page.waitForFunction(() => !document.querySelector(".drawer"),
      null, { timeout: 10000 });
    await page.waitForFunction((sel) => {
      const el = document.activeElement;
      return !!(el && el.matches && el.matches(sel));
    }, CLUB_NEW, { timeout: 5000 }).catch(async () => {
      fail(`nested restore: expected focus back on the drawer's own trigger `
        + `(${CLUB_NEW}) after the modal-over-drawer sequence, got `
        + `${JSON.stringify(await activeInfo(page))}`);
    });

    // ---- (4) shell states: title + skip link follow the VISIBLE surface ---
    // #345 review blocker: the title used to be owned by a successful
    // authenticated render(), and the skip link lived outside .web -- so after
    // Sign out and on the anonymous public portal the title stayed on the
    // previous authenticated view while a focusable "Skip to main content"
    // pointed at a hidden #content. Walk the real transitions and assert both.
    const shellState = () => page.evaluate(() => {
      const sk = document.getElementById("skip-link");
      const content = document.getElementById("content");
      const visible = (el) => !!(el && el.getClientRects().length > 0);
      return {
        title: document.title,
        skipVisible: visible(sk),
        skipTargetVisible: visible(content),
        // A skip link that is reachable while its target is hidden is the
        // exact defect; capture it as one boolean for a clear failure.
        danglingSkip: visible(sk) && !visible(content),
        loginVisible: visible(document.querySelector("#login-screen")),
        publicVisible: visible(document.querySelector("#public-screen")),
      };
    });
    const expectShell = async (label, want) => {
      const got = await shellState();
      if (got.danglingSkip) {
        fail(`shell state (${label}): a focusable skip link is exposed while `
          + `its #content target is hidden -- ${JSON.stringify(got)}`);
      }
      if (got.title !== want.title) {
        fail(`shell state (${label}): expected title "${want.title}", got `
          + `"${got.title}" -- ${JSON.stringify(got)}`);
      }
      if (want.skipVisible !== undefined && got.skipVisible !== want.skipVisible) {
        fail(`shell state (${label}): expected skipVisible=${want.skipVisible}, `
          + `got ${JSON.stringify(got)}`);
      }
      return got;
    };

    // Authenticated landing, then navigate away, so the pre-signout title is
    // definitely NOT the one the signed-out screen should show.
    await page.click('.tab[data-tab="standings"]');
    await page.waitForFunction(() => /^Standings —/.test(document.title),
      null, { timeout: 10000 });
    await expectShell("authenticated Standings",
      { title: "Standings — Hockey Scheduler", skipVisible: true });

    // Sign out -> the sign-in card.
    await page.click("#signout-btn");
    await page.waitForFunction(() =>
      document.body.classList.contains("signed-out"), null, { timeout: 10000 });
    await page.waitForFunction(() => /^Sign in —/.test(document.title),
      null, { timeout: 10000 }).catch(async () => {
      fail(`shell state (signed out): title is "${await page.evaluate(() =>
        document.title)}" -- signing out must retitle to the visible surface`);
    });
    await expectShell("signed out", { title: "Sign in — Hockey Scheduler",
      skipVisible: false });

    // A FAILED login keeps the sign-in surface and its title.
    await page.fill("#login-user", "admin");
    await page.fill("#login-pass", "definitely-not-the-password");
    await page.click(".login-submit");
    await page.waitForFunction(() => {
      const e = document.getElementById("login-error");
      return !!(e && !e.hidden && e.textContent.trim());
    }, null, { timeout: 10000 });
    await expectShell("failed login", { title: "Sign in — Hockey Scheduler",
      skipVisible: false });

    // Anonymous public portal.
    await page.click("#guest-public-link");
    await page.waitForFunction(() => {
      const s = document.getElementById("public-screen");
      return !!(s && !s.hidden);
    }, null, { timeout: 10000 });
    await page.waitForFunction(() => /^Public Schedule —/.test(document.title),
      null, { timeout: 10000 }).catch(async () => {
      fail(`shell state (public): title is "${await page.evaluate(() =>
        document.title)}" -- the public portal must own its own title`);
    });
    await expectShell("anonymous public",
      { title: "Public Schedule — Hockey Scheduler", skipVisible: false });

    // NOT covered here: the public -> "Staff sign in" leg. That control is
    // visible and clickable in isolation, but becomes unclickable when this
    // leg runs at the end of the full journey (after the dialog/Escape and
    // sign-out sections), and I did not want to ship either a flaky step or a
    // fake one that dispatches the handler directly instead of clicking. The
    // blocker itself is fully covered by the four transitions above --
    // authenticated -> signed out -> failed login -> public -- each asserting
    // the title names the visible surface and no skip link points into a
    // hidden shell. Tracked as remaining coverage on #345.

    if (errors.length) {
      fail(`browser errors:\n${errors.join("\n")}`);
    }
    console.log(`[${viewport.label}] OK — skip link is the first tab stop, is `
      + `on-screen and target-size compliant when focused, and reaches content `
      + `in 2 keystrokes where tabbing the sidebar takes ${tabsWithoutSkip}; `
      + `page titles track the view on a full page boot (no switchTab), on the `
      + `onboarding landing that bypasses the base render, and across nav `
      + `clicks; an open dialog takes focus on its CONTAINER, contains Tab and `
      + `Shift+Tab, keeps focus anchored across a re-render that rebuilds it, `
      + `and restores to its trigger on Escape and on the close button -- `
      + `including an in-view trigger re-resolved after the render destroyed `
      + `it, a removed trigger falling back off <body>, a modal opened OVER a `
      + `drawer returning to the drawer and then to the drawer's own trigger, `
      + `and a close that must NOT steal focus already held outside the `
      + `dialog; and across authenticated -> sign out -> failed login -> `
      + `public portal, the title always names the visible surface and no `
      + `skip link is ever exposed pointing into a hidden shell.`);
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
