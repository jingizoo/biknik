// THE FIRST-RUN BOOTSTRAP, driven by SHIPPED CONTROLS ONLY (#411, defect 1).
//
// WHAT WAS BROKEN. On an installation holding exactly ONE Program and ZERO
// Seasons, `contextEntries()` yields ONE entry, `renderContextSwitcher()`'s
// `single` branch hid `#ctx-select` behind a non-interactive chip, and
// `setActiveContext()` -- the only thing that PERSISTS an ActiveContext row --
// was wired to that select's `onchange` and the League select's. Both hidden.
// So there was no control on the page able to satisfy the 409 every
// Program-axis create answered with ("Select a Program before creating records
// in it"), while `GET /api/context` cheerfully reported the Program as selected
// because the FALLBACK resolver names it. The product told the operator the
// thing was done and then refused every write that depended on it.
//
// WHAT THIS JOURNEY PROVES, and the ONE rule it is written around:
//
//     RENDERING MUST NEVER PERSIST. The owner's ruling is explicit that the fix
//     may not auto-select on render -- that would launder the fallback back
//     into mutation authority, which is exactly what #409 removed. So the proof
//     that nothing auto-persists is taken FROM THE WIRE, not from the DOM: a
//     `page.on("request")` listener records every request the page issues, and
//     the count of `POST /api/context` must be ZERO right up to the instant of
//     a deliberate mouse click or keypress. A DOM assertion ("the chip looks
//     unselected") would prove nothing about what was sent.
//
// NON-VACUITY. "Zero POSTs" is also what a page that never booted would report,
// so every case first asserts the page DID issue its context reads (`GET
// /api/context` and `GET /api/context/options`) during boot. The zero is then a
// statement about a live context pipeline that chose not to write.
//
// NO FIXTURE. `context-fixture.js` is deliberately NOT installed here. Its own
// header records that `selectProgram()` issues a selection the shipped UI could
// not, and that three journeys accept that trade while this exact gap stays
// open. This journey is the one that closes it, so it may not use the helper
// that hides it: every selection below comes from a real click or a real
// keystroke on a real control.
//
// SIX CASES: two viewports x three activation paths, each on its own port with
// its own freshly-booted (empty) server, so every case is a genuine first run
// rather than a continuation of the previous one.
//
//     desktop 1440x900 / phone 390x844   x   mouse click / Tab+Enter / Tab+Space
//
// Enter and Space are pressed for real, after Tab has walked focus to the
// control from the top of the document -- never `element.click()` from JS,
// which would prove only that a handler exists and nothing about whether a
// keyboard operator can reach or fire it.
//
// The two "click" cases additionally carry the ZERO-Program and MULTI-Program
// states, so all three inventory shapes are covered at both widths.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const fs = require("fs");
const http = require("http");
const path = require("path");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const AXE_PATH = require.resolve("axe-core/axe.min.js");
const READY_TIMEOUT_MS = 15000;
const TZ = "America/Toronto";

const CASES = [
  { label: "desktop", width: 1440, height: 900, port: 8411, mode: "click", full: true },
  { label: "desktop", width: 1440, height: 900, port: 8413, mode: "enter" },
  { label: "desktop", width: 1440, height: 900, port: 8415, mode: "space" },
  { label: "phone", width: 390, height: 844, port: 8412, mode: "click", full: true },
  { label: "phone", width: 390, height: 844, port: 8414, mode: "enter" },
  { label: "phone", width: 390, height: 844, port: 8416, mode: "space" },
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

// ---------------------------------------------------------------------------
// The REAL keyboard switch through a native <select>, for the MULTI-Program
// state. Copied deliberately from context-scope-truth.js (which took it from
// context-switcher.js) and carrying the same guarantee: the macOS fallback only
// ever engages after PROVING the keystroke reached the element, so a genuine
// focus/wiring regression still fails here instead of being papered over.
// ---------------------------------------------------------------------------
async function keyboardSwitchTo(page, targetValue, step, fail) {
  await page.focus("#ctx-select");
  const focused = await page.evaluate(
    () => document.activeElement && document.activeElement.id);
  if (focused !== "ctx-select") {
    fail(`[${step}] #ctx-select is not keyboard-focusable (active="${focused}")`);
  }
  const plan = await page.evaluate((target) => {
    const s = document.getElementById("ctx-select");
    const values = Array.from(s.options).map((o) => o.value);
    return { from: values.indexOf(s.value), to: values.indexOf(target), values };
  }, targetValue);
  if (plan.to < 0) {
    fail(`[${step}] "${targetValue}" is not an option in #ctx-select `
      + `(options: ${JSON.stringify(plan.values)})`);
  }
  if (plan.from === plan.to) {
    fail(`[${step}] #ctx-select is already on "${targetValue}" — this switch `
      + "would prove nothing");
  }
  await page.evaluate(() => {
    window.__bootKeys = [];
    document.getElementById("ctx-select")
      .addEventListener("keydown", (e) => window.__bootKeys.push(e.key));
  });
  const key = plan.to > plan.from ? "ArrowDown" : "ArrowUp";
  for (let i = 0; i < Math.abs(plan.to - plan.from); i += 1) {
    await page.keyboard.press(key);
  }
  let movedByKeyboard = true;
  try {
    await page.waitForFunction((v) =>
      document.getElementById("ctx-select").value === v,
    targetValue, { timeout: 1500 });
  } catch (_) {
    movedByKeyboard = false;
  }
  if (!movedByKeyboard) {
    const delivered = await page.evaluate(() => (window.__bootKeys || []).slice());
    if (!delivered.includes(key)) {
      fail(`[${step}] ${key} never reached #ctx-select (keydowns seen: `
        + `${JSON.stringify(delivered)}) — the select is focused but not `
        + "receiving keyboard input, which is a real regression, not the "
        + "macOS widget quirk");
    }
    console.warn(`[${step}] NOTE: this browser's <select> widget ignored ${key} `
      + "on a closed select (known macOS-Chromium behaviour; the keydown WAS "
      + "delivered). Driving the same change via selectOption so the rest of "
      + "this scenario still runs. On Linux/CI the keyboard path is exercised.");
    await page.selectOption("#ctx-select", targetValue);
  }
  return movedByKeyboard;
}

async function runCase(browser, testCase) {
  const base = `http://${HOST}:${testCase.port}`;
  const L = `${testCase.label}/${testCase.mode}`;
  const server = spawn(
    process.env.PYTHON || "python3",
    ["-u", "-m", "hockey_scheduler.web.server", "--host", HOST,
     "--port", String(testCase.port)],
    { cwd: BACKEND_DIR, stdio: ["ignore", "pipe", "pipe"] });
  let out = "";
  server.stdout.on("data", (d) => { out += d.toString(); });
  server.stderr.on("data", (d) => { out += d.toString(); });

  const context = await browser.newContext({
    viewport: { width: testCase.width, height: testCase.height },
  });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(`[pageerror] ${e.message}`));
  page.on("console", (m) => { if (m.type() === "error") errors.push(`[console] ${m.text()}`); });

  // ---- THE WIRE LEDGER. Attached BEFORE the first navigation, so nothing the
  // page does is unobserved. Every request is kept (for diagnostics) and the
  // context POSTs are kept separately with a timestamp, which is what the
  // "nothing persisted before the activation" assertions read.
  const wire = [];
  const contextPosts = [];
  page.on("request", (r) => {
    let pathname = r.url();
    try { pathname = new URL(r.url()).pathname; } catch (_) { /* keep raw */ }
    const entry = { method: r.method(), path: pathname, at: Date.now() };
    wire.push(entry);
    if (entry.method === "POST" && pathname === "/api/context") {
      contextPosts.push(Object.assign({ body: r.postData() }, entry));
    }
  });
  const wireDump = () => wire
    .filter((e) => e.path.startsWith("/api/"))
    .map((e) => `    ${e.method} ${e.path}`).join("\n");
  const fail = (message) => {
    throw new Error(`${message}\n--- API traffic this page issued ---\n${wireDump()}`);
  };
  // THE assertion this whole journey exists for.
  const assertNoContextPost = (step) => {
    if (contextPosts.length) {
      fail(`[${L}] ${step}: ${contextPosts.length} POST /api/context already `
        + "went over the wire before any deliberate activation — the render "
        + "path is persisting a selection on its own, which turns the fallback "
        + "back into mutation authority (bodies: "
        + `${JSON.stringify(contextPosts.map((p) => p.body))})`);
    }
  };

  // The Records view is where the real Program/Season drawers live, and a
  // reload always lands on the default tab, so every post-reload drawer step
  // goes back through the shipped navigation rather than a restored URL.
  const gotoRecords = async () => {
    await page.click('.tab[data-tab="setup"]');
    await page.click('[data-setup-view="records"]');
    await page.waitForSelector(".setup-card", { timeout: 10000 });
  };
  const reloadFresh = async (step) => {
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });
    assertNoContextPost(step);
  };

  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    // FRESHNESS GUARD. Every claim in this case is about a FIRST-RUN install,
    // and `waitForServer` cannot tell this case's own server from a stranger
    // already bound to the port — an orphan from an aborted earlier run binds
    // first, this spawn dies with "Address already in use", and the case then
    // measures somebody else's populated store while reporting nonsense about
    // the switcher. Two checks, so the diagnosis is at the top rather than
    // twelve assertions later: the child must still be alive, and the store it
    // is serving must be empty.
    if (server.exitCode !== null || server.signalCode !== null) {
      throw new Error(`[${L}] the server for port ${testCase.port} exited `
        + `immediately (code ${server.exitCode}, signal ${server.signalCode}) `
        + "— something else is bound to that port");
    }
    const inventory = await new Promise((resolve, reject) => {
      http.get(`${base}/api/v2/setup/hierarchy`, (r) => {
        let body = "";
        r.on("data", (d) => { body += d; });
        r.on("end", () => { try { resolve(JSON.parse(body || "{}")); }
          catch (e) { reject(e); } });
      }).on("error", reject);
    });
    if ((inventory.programs || []).length) {
      throw new Error(`[${L}] the server answering on ${base} already holds `
        + `${inventory.programs.length} Program(s), so this is NOT a fresh `
        + "install — most likely an orphaned server from an earlier run is "
        + `still bound to port ${testCase.port}`);
    }
    // axe-core from a same-origin URL (CSP is script-src 'self').
    const axeSource = fs.readFileSync(AXE_PATH, "utf8");
    await page.route("**/__axe-core__.js", (route) => route.fulfill({
      status: 200, contentType: "application/javascript", body: axeSource,
    }));

    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });

    // --- NON-VACUITY. The page must actually have run its context pipeline,
    // or "zero POSTs" would be the trivially true report of a page that never
    // booted. Asserted BEFORE anything below reads the API by hand.
    const bootReads = wire.filter(
      (e) => e.method === "GET" && e.path.indexOf("/api/context") === 0);
    if (!bootReads.length) {
      fail(`[${L}] the page never read /api/context* on boot, so a zero POST `
        + "count says nothing about whether rendering persists");
    }

    // ================= STATE 1: ZERO Programs =================
    // The switcher has nothing to show and no control may claim otherwise.
    const zero = await page.evaluate(() => {
      const wrap = document.getElementById("context-switcher");
      const btn = document.getElementById("ctx-confirm");
      return {
        wrapHidden: !!(wrap && wrap.hidden),
        confirmExists: !!btn,
        confirmHidden: !!(btn && btn.hidden),
        interactive: wrap ? Array.from(
          wrap.querySelectorAll("button, select, input, a[href], [tabindex]"))
          .filter((el) => !el.hidden && !el.disabled
            && el.getAttribute("tabindex") !== "-1").length : -1,
      };
    });
    if (!zero.confirmExists) fail(`[${L}] #ctx-confirm is not in the document`);
    if (!zero.wrapHidden || !zero.confirmHidden) {
      fail(`[${L}] zero-Program state: the switcher is not fully withdrawn `
        + `(${JSON.stringify(zero)})`);
    }
    const zeroCtx = await page.evaluate(async () =>
      (await fetch("/api/context", { credentials: "same-origin" })).json());
    if (zeroCtx.program_id !== null) {
      fail(`[${L}] zero-Program state: /api/context named a Program `
        + `(${JSON.stringify(zeroCtx)})`);
    }
    assertNoContextPost("zero-Program state");
    console.log(`[${L}] zero Programs — switcher withdrawn, 0 POST /api/context`);

    // ================= reach STATE 2 through the real Records drawers =======
    await gotoRecords();

    const programName = "Alpine Program";
    await page.click('.setup-card .sc-new[data-drawer="league"]');
    await page.waitForSelector(".drawer[role=dialog]", { timeout: 10000 });
    await page.fill("#f-league", programName);
    await page.fill("#f-league-tz", TZ);
    const progResp = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/program` && r.request().method() === "POST");
    await page.click('[data-drawer-submit="league"]');
    const progBody = await (await progResp).json();
    if (progBody.error) fail(`[${L}] program create failed: ${JSON.stringify(progBody)}`);
    const programId = progBody.id;
    await page.waitForFunction(() => !document.querySelector(".drawer[role=dialog]"),
      null, { timeout: 10000 });
    // Creating a Program is a ZERO-AXIS root create — it needs no selection and
    // must not silently make one on the operator's behalf either.
    assertNoContextPost("immediately after the Program create");

    // ================= STATE 2: exactly ONE Program, ZERO Seasons ===========
    await page.waitForFunction(() => {
      const b = document.getElementById("ctx-confirm");
      return b && !b.hidden;
    }, null, { timeout: 10000 }).catch(() => fail(
      `[${L}] the single-Program selection control never appeared — this is `
      + "the dead-end state #411 exists to fix"));
    const single = await page.evaluate(() => {
      const wrap = document.getElementById("context-switcher");
      const select = document.getElementById("ctx-select");
      const chip = document.getElementById("ctx-static");
      const btn = document.getElementById("ctx-confirm");
      const style = window.getComputedStyle(btn);
      return {
        wrapHidden: !!wrap.hidden,
        selectHidden: !!select.hidden,
        selectOptions: select.options.length,
        chipHidden: !!chip.hidden,
        chipText: chip.textContent,
        tag: btn.tagName,
        type: btn.getAttribute("type"),
        disabled: !!btn.disabled,
        text: (btn.textContent || "").trim(),
        ariaLabel: btn.getAttribute("aria-label"),
        display: style.display,
        visibility: style.visibility,
        rect: btn.getBoundingClientRect().width,
      };
    });
    if (single.wrapHidden || !single.selectHidden || single.chipHidden) {
      fail(`[${L}] this is not the collapsed single-entry state: `
        + JSON.stringify(single));
    }
    if (single.tag !== "BUTTON" || single.type !== "button" || single.disabled) {
      fail(`[${L}] #ctx-confirm is not an enabled native button: `
        + JSON.stringify(single));
    }
    if (single.display === "none" || single.visibility === "hidden"
        || single.rect <= 0) {
      fail(`[${L}] #ctx-confirm is not actually visible: ${JSON.stringify(single)}`);
    }
    // Accessible naming: the button names the Program, and its VISIBLE text is
    // contained in its accessible name (WCAG 2.5.3 label-in-name), so speech
    // input matches what a sighted operator reads aloud.
    const accName = single.ariaLabel || single.text;
    if (!accName || accName.indexOf(programName) < 0) {
      fail(`[${L}] #ctx-confirm's accessible name does not identify the `
        + `Program: ${JSON.stringify(single)}`);
    }
    if (accName.indexOf(single.text) < 0) {
      fail(`[${L}] #ctx-confirm's visible text "${single.text}" is not part of `
        + `its accessible name "${accName}" (WCAG 2.5.3)`);
    }
    if (single.chipText.indexOf(programName) < 0) {
      fail(`[${L}] the chip no longer identifies the Program: `
        + JSON.stringify(single));
    }
    assertNoContextPost("single-Program state, control painted");

    // ---- THE REFRESH-BEFORE-ACTIVATION FALSIFIER (owner-added). ------------
    // The wire ledger above only sees one page lifetime, so on its own it
    // cannot rule out DELAYED or BACKGROUND persistence: a debounced timer, a
    // requestIdleCallback, a `beforeunload`/`pagehide` sendBeacon, or a write
    // deferred to the next navigation would all report "zero POSTs so far" and
    // then land anyway. So: RELOAD without ever touching the control, let the
    // page go fully idle, and then ask the SERVER whether a row now exists.
    //
    // Two things make this a real falsifier rather than a restatement:
    //   * the probe is issued through `context.request`, i.e. OUTSIDE the page
    //     — same cookie jar, same session, but no page JS involved, so a broken
    //     page cannot answer on the server's behalf (and, because it bypasses
    //     `page.on("request")`, the probe cannot pollute the ledger it checks);
    //   * it reads `saved`, the persisted-row report, NOT `selected`, which the
    //     fallback resolver fills in with this very Program either way. An
    //     assertion on `selected` would be green in both worlds.
    const savedFromServer = async (step) => {
      const r = await context.request.get(`${base}/api/context/options`);
      if (!r.ok()) fail(`[${L}] ${step}: /api/context/options answered ${r.status()}`);
      return (await r.json()).saved || {};
    };
    const preReloadSaved = await savedFromServer("before the pre-activation reload");
    if (preReloadSaved.program_id !== null) {
      fail(`[${L}] a row was persisted with no activation at all: `
        + JSON.stringify(preReloadSaved));
    }
    await reloadFresh("after a full page reload in the single-Program state");
    await page.waitForFunction(() => {
      const b = document.getElementById("ctx-confirm");
      return b && !b.hidden;
    }, null, { timeout: 10000 }).catch(() => fail(
      `[${L}] the selection control did not survive a reload`));
    // Let every deferred mechanism have its chance: the network must be quiet
    // (nothing still in flight) AND a fixed settle window must elapse, so a
    // short debounce or an idle callback fires BEFORE the probe below, not
    // after it.
    await page.waitForLoadState("networkidle").catch(() => {});
    await page.waitForTimeout(2000);
    const afterReloadSaved = await savedFromServer(
      "after reloading without activating");
    if (afterReloadSaved.program_id !== null
        || afterReloadSaved.season_id !== null) {
      fail(`[${L}] REFRESH-BEFORE-ACTIVATION FALSIFIER TRIPPED: reloading the `
        + "page without ever activating the control left a saved ActiveContext "
        + `row behind (${JSON.stringify(afterReloadSaved)}) — persistence is `
        + "happening on render, unload or navigation rather than on the "
        + "operator's deliberate act");
    }
    assertNoContextPost("after the post-reload settle window");
    console.log(`[${L}] reloaded WITHOUT activating, settled 2s, server-side `
      + `saved={program:${afterReloadSaved.program_id},`
      + `season:${afterReloadSaved.season_id}} — no ActiveContext row; `
      + `POST /api/context count ${contextPosts.length}`);
    await gotoRecords();

    // The switcher's own report must agree with the WRITE authority: the
    // fallback names the Program under `selected`, and `saved` is still null.
    const beforeOpts = await page.evaluate(async () =>
      (await fetch("/api/context/options", { credentials: "same-origin" })).json());
    if (beforeOpts.selected.program_id !== programId) {
      fail(`[${L}] the fallback did not name the only Program, so the `
        + `coincidence under test is absent: ${JSON.stringify(beforeOpts.selected)}`);
    }
    if (beforeOpts.saved.program_id !== null) {
      fail(`[${L}] a selection is already persisted before any activation: `
        + JSON.stringify(beforeOpts.saved));
    }

    // ---- OMISSION KEEPS THE CREATE AT 409. Not activating the control leaves
    // the Program-axis create refused, which is what makes the activation below
    // load-bearing rather than decorative.
    await page.click('.setup-card .sc-new[data-drawer="season"]');
    await page.waitForSelector(".drawer[role=dialog]", { timeout: 10000 });
    await page.selectOption("#f-season-league", programId);
    await page.fill("#f-season", "2026-27");
    await page.fill("#f-season-start", "2026-09-15");
    await page.fill("#f-season-end", "2027-03-15");
    const refusal = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/season` && r.request().method() === "POST");
    await page.click('[data-drawer-submit="season"]');
    const refusalResp = await refusal;
    const refusalBody = await refusalResp.json();
    if (refusalResp.status() !== 409
        || !refusalBody.error || refusalBody.error.code !== "active_context_required") {
      fail(`[${L}] omitting the selection did NOT keep the Season create at `
        + `409 active_context_required: ${refusalResp.status()} `
        + JSON.stringify(refusalBody));
    }
    assertNoContextPost("after the refused Season create");
    await page.keyboard.press("Escape");
    await page.waitForFunction(() => !document.querySelector(".drawer[role=dialog]"),
      null, { timeout: 10000 });
    console.log(`[${L}] omitted activation → Season create still 409 `
      + `${refusalBody.error.code}; POST /api/context count still `
      + `${contextPosts.length}`);

    // ---- ACCESSIBILITY of the state that carries the control. The whole
    // switcher is scanned, and the result is SPLIT rather than thresholded:
    // anything landing on the control this issue adds (#ctx-confirm, and the
    // chip it hands focus to) fails here, and anything landing on the switcher
    // chrome that was already there is REPORTED, in full, instead of being
    // quietly absorbed by a scan narrowed to dodge it. Both halves are printed,
    // so a pre-existing defect cannot hide behind this journey's green.
    await page.addScriptTag({ url: "/__axe-core__.js" });
    const axeNodes = await page.evaluate(async () => {
      const r = await window.axe.run("#context-switcher",
        { resultTypes: ["violations"] });
      const out = [];
      // `target` is axe's own CSS selector for the offending node, which is
      // what the split below classifies on. (axe only hands back a live
      // `element` when run with `elementRef`, so there is no DOM object here to
      // ask — the selector is the honest discriminator, not a fallback.)
      r.violations.forEach((v) => v.nodes.forEach((n) => out.push({
        id: v.id, impact: v.impact, target: (n.target || []).join(" "),
        summary: (n.failureSummary || "").split("\n")[1] || "",
      })));
      return out;
    });
    const bad = (n) => n.impact === "serious" || n.impact === "critical";
    const isTheControl = (n) => /ctx-confirm|ctx-static/.test(n.target);
    const onTheControl = axeNodes.filter((n) => bad(n) && isTheControl(n));
    if (onTheControl.length) {
      fail(`[${L}] axe found serious/critical violations ON THE NEW CONTROL: `
        + JSON.stringify(onTheControl, null, 1));
    }
    const preExisting = axeNodes.filter((n) => bad(n) && !isTheControl(n));
    if (preExisting.length) {
      console.warn(`[${L}] PRE-EXISTING (not introduced by #411, not fixed `
        + `here) serious/critical axe findings elsewhere in #context-switcher: `
        + preExisting.map((n) => `${n.id} @ ${n.target} — ${n.summary.trim()}`)
          .join("; "));
    }

    // ================= THE DELIBERATE ACTIVATION ===========================
    // Reload first, for ONE reason: the Tab walk below is a claim about the
    // SHIPPED tab order, and that claim is only honest if focus starts where a
    // fresh page puts it (<body>). Closing the drawer above restored focus to
    // the control that opened it, deep in the content — tabbing forward from
    // there would never reach a topbar control at all. It also buys one more
    // observation of the property under test: another full render pass, still
    // zero context POSTs.
    await reloadFresh("after the pre-activation reload");
    await page.waitForFunction(() => {
      const b = document.getElementById("ctx-confirm");
      return b && !b.hidden;
    }, null, { timeout: 10000 }).catch(() => fail(
      `[${L}] the selection control is not present to be activated`));

    let tabs = 0;
    if (testCase.mode === "click") {
      await page.click("#ctx-confirm");
    } else {
      // REAL keyboard reach: Tab from the top of the document until focus is on
      // the control. Focus starts on <body> after the reload above, so this
      // walks the shipped tab order rather than being placed there by script.
      for (tabs = 1; tabs <= 40; tabs += 1) {
        await page.keyboard.press("Tab");
        const id = await page.evaluate(
          () => document.activeElement && document.activeElement.id);
        if (id === "ctx-confirm") break;
        if (tabs === 40) {
          fail(`[${L}] #ctx-confirm was never reached by Tab in 40 presses — `
            + "it is not in the keyboard tab order");
        }
      }
      assertNoContextPost("after tabbing onto the control (focus is not activation)");
      await page.keyboard.press(testCase.mode === "enter" ? "Enter" : "Space");
    }

    // ---- THE WIRE, FIRST. This is the assertion the whole journey turns on,
    // so it is the one that reports when the control is inert — a later
    // assertion failing first would name a symptom (nothing announced) instead
    // of the cause (nothing sent). Bounded wait, because the POST is issued
    // asynchronously from the handler.
    const sent = await (async () => {
      const deadline = Date.now() + 10000;
      while (Date.now() < deadline) {
        if (contextPosts.length) return true;
        await new Promise((res) => setTimeout(res, 50));
      }
      return false;
    })();
    if (!sent) {
      fail(`[${L}] the ${testCase.mode} activation issued NO POST /api/context `
        + "— the control does not persist anything");
    }

    // ---- The live region speaks, and focus lands somewhere. Asserted next,
    // because a non-error toast auto-clears itself after 4s (updateToast) and
    // the read-backs below poll the server; checking speech after them would be
    // a race this journey has no business running.
    await page.waitForFunction((name) => {
      const t = document.querySelector(".toast-msg");
      return !!(t && t.textContent.indexOf(name) >= 0);
    }, programName, { timeout: 10000 }).catch(() => fail(
      `[${L}] the selection was never announced in the one sitewide live `
      + "region (#toast-root)"));
    const afterUi = await page.evaluate(() => ({
      focus: document.activeElement && document.activeElement.id,
      toast: (document.querySelector(".toast-msg") || {}).textContent || "",
      toastRootHidden: !!(document.getElementById("toast-root") || {}).hidden,
    }));
    if (afterUi.toastRootHidden) {
      fail(`[${L}] the live region is hidden while holding the announcement: `
        + JSON.stringify(afterUi));
    }
    if (testCase.mode !== "click" && afterUi.focus !== "ctx-static") {
      fail(`[${L}] keyboard activation dropped focus (now on `
        + `"${afterUi.focus}") instead of moving it to the surviving chip — a `
        + "control that hides itself must hand focus somewhere deliberate");
    }

    // The FIRST context POST in this run's whole wire ledger is this activation,
    // and it is the ONLY one: a control that fired twice would be persisting
    // something the operator did not ask for a second time.
    if (contextPosts.length !== 1) {
      fail(`[${L}] the activation issued ${contextPosts.length} context POSTs, `
        + `expected exactly one: ${JSON.stringify(contextPosts.map((p) => p.body))}`);
    }
    const posted = JSON.parse(contextPosts[0].body || "{}");
    if (posted.program_id !== programId || (posted.season_id || null) !== null) {
      fail(`[${L}] the activation persisted the wrong tuple: `
        + JSON.stringify(posted));
    }

    // ---- READ IT BACK from the authority, not from the fallback.
    await page.waitForFunction(async (pid) => {
      const o = await (await fetch("/api/context/options",
        { credentials: "same-origin" })).json();
      return o && o.saved && o.saved.program_id === pid;
    }, programId, { timeout: 10000 }).catch(() => fail(
      `[${L}] the activation did not read back as a PERSISTED selection`));
    const afterCtx = await page.evaluate(async () =>
      (await fetch("/api/context", { credentials: "same-origin" })).json());
    if (afterCtx.program_id !== programId) {
      fail(`[${L}] /api/context does not report the selection: `
        + JSON.stringify(afterCtx));
    }

    // ---- The UI states the same fact: the offer is withdrawn, because it has
    // been taken. The chip survives as the statement of what is selected.
    await page.waitForFunction(() => {
      const b = document.getElementById("ctx-confirm");
      const c = document.getElementById("ctx-static");
      return b && c && b.hidden && !c.hidden;
    }, null, { timeout: 10000 }).catch(() => fail(
      `[${L}] #ctx-confirm still offers to select an already-selected Program`));

    // ---- REFRESH PRESERVES IT, and does not re-persist it.
    const postsBeforeReload = contextPosts.length;
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });
    await page.waitForFunction(() => {
      const b = document.getElementById("ctx-confirm");
      const c = document.getElementById("ctx-static");
      return b && c && !c.hidden;
    }, null, { timeout: 10000 });
    const reloaded = await page.evaluate(async () => {
      const o = await (await fetch("/api/context/options",
        { credentials: "same-origin" })).json();
      const b = document.getElementById("ctx-confirm");
      return { saved: o.saved, selected: o.selected, confirmHidden: !!b.hidden };
    });
    if (reloaded.saved.program_id !== programId) {
      fail(`[${L}] the explicit selection did not survive a refresh: `
        + JSON.stringify(reloaded));
    }
    if (!reloaded.confirmHidden) {
      fail(`[${L}] after a refresh the control offers to re-select an already `
        + `persisted Program — display disagreeing with authority again: `
        + JSON.stringify(reloaded));
    }
    if (contextPosts.length !== postsBeforeReload) {
      fail(`[${L}] the refresh itself issued `
        + `${contextPosts.length - postsBeforeReload} more POST /api/context — `
        + "loading a page must never write a selection");
    }

    // ---- AND THE CREATE THE REFUSAL NAMED NOW SUCCEEDS, from the same UI.
    await gotoRecords();
    await page.click('.setup-card .sc-new[data-drawer="season"]');
    await page.waitForSelector(".drawer[role=dialog]", { timeout: 10000 });
    await page.selectOption("#f-season-league", programId);
    await page.fill("#f-season", "2026-27");
    await page.fill("#f-season-start", "2026-09-15");
    await page.fill("#f-season-end", "2027-03-15");
    const created = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/season` && r.request().method() === "POST");
    await page.click('[data-drawer-submit="season"]');
    const createdResp = await created;
    const createdBody = await createdResp.json();
    if (createdResp.status() !== 200 || !createdBody.id) {
      fail(`[${L}] the Season create still failed AFTER the explicit `
        + `selection: ${createdResp.status()} ${JSON.stringify(createdBody)}`);
    }
    await page.waitForFunction(() => !document.querySelector(".drawer[role=dialog]"),
      null, { timeout: 10000 });
    console.log(`[${L}] activation via ${testCase.mode}`
      + (tabs ? ` (${tabs} Tab presses to reach it)` : "")
      + ` → 1 POST /api/context, saved.program_id=${programId}, `
      + `Season create 200 ${createdBody.id}`);

    // ================= STATE 3: MULTI-Program (the "full" cases) ============
    if (testCase.full) {
      await page.click('.setup-card .sc-new[data-drawer="league"]');
      await page.waitForSelector(".drawer[role=dialog]", { timeout: 10000 });
      await page.fill("#f-league", "Beta Program");
      await page.fill("#f-league-tz", TZ);
      const p2Resp = page.waitForResponse((r) =>
        r.url() === `${base}/api/v2/setup/program` && r.request().method() === "POST");
      await page.click('[data-drawer-submit="league"]');
      const p2 = await (await p2Resp).json();
      if (p2.error) fail(`[${L}] second program create failed: ${JSON.stringify(p2)}`);
      await page.waitForFunction(() => !document.querySelector(".drawer[role=dialog]"),
        null, { timeout: 10000 });
      await page.waitForFunction(() => {
        const s = document.getElementById("ctx-select");
        return s && !s.hidden && s.options.length >= 3;
      }, null, { timeout: 10000 }).catch(() => fail(
        `[${L}] the multi-Program <select> never rendered`));
      const multi = await page.evaluate(() => ({
        selectHidden: !!document.getElementById("ctx-select").hidden,
        confirmHidden: !!document.getElementById("ctx-confirm").hidden,
        chipHidden: !!document.getElementById("ctx-static").hidden,
        options: Array.from(document.getElementById("ctx-select").options)
          .map((o) => o.value),
      }));
      if (multi.selectHidden || !multi.confirmHidden || !multi.chipHidden) {
        fail(`[${L}] multi-Program state is malformed: ${JSON.stringify(multi)}`);
      }
      const postsBeforeSwitch = contextPosts.length;
      const byKeyboard = await keyboardSwitchTo(page, `${p2.id}|`,
        `${L} multi-Program`, fail);
      await page.waitForFunction(async (pid) => {
        const o = await (await fetch("/api/context/options",
          { credentials: "same-origin" })).json();
        return o && o.saved && o.saved.program_id === pid;
      }, p2.id, { timeout: 10000 }).catch(() => fail(
        `[${L}] the multi-Program switch did not persist`));
      if (contextPosts.length <= postsBeforeSwitch) {
        fail(`[${L}] the multi-Program switch issued no POST /api/context`);
      }
      console.log(`[${L}] multi-Program — <select> renders, control withdrawn, `
        + `switch persisted (${byKeyboard ? "arrow keys" : "selectOption fallback"})`);
    }

    // The only tolerated console noise is the browser's own resource log for
    // the deliberate 409 above; anything else is a real page error.
    const unexpected = errors.filter(
      (e) => !/\b409\b|Failed to load resource/i.test(e));
    if (unexpected.length) {
      fail(`[${L}] unexpected console/page errors:\n${unexpected.join("\n")}`);
    }
    console.log(`[${L}] OK`);
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
    for (const testCase of CASES) await runCase(browser, testCase);
    console.log("First-run Program selection browser journey passed.");
  } catch (error) {
    console.error("First-run Program selection browser journey FAILED.");
    console.error(error && error.message ? error.message : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
