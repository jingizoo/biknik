// Seven-area navigation shell (#345) — the approved information architecture.
//
// #345 replaces the five task groups #145 introduced with the seven areas the
// requirements package's IA crosswalk names: Home/Tasks, Schedule, Teams &
// People, Facilities, Communications, Reports, Administration. That slice is a
// GROUPING/LABELLING change only, so the thing worth proving is precisely that
// nothing else moved: every destination that was reachable is still reachable,
// from the same route key, for exactly the same roles.
//
// This journey drives the REAL production navigation (no fixture nav, no
// hand-copied crosswalk) and asserts, at desktop and canonical 390x844:
//
//   1. INVENTORY. Every key in the production `NAV` map appears exactly once
//      in the rendered nav, and every rendered destination is a real `NAV`
//      key -- so a destination can neither be dropped from the IA nor invented
//      by it. Both directions matter: dropping one silently removes a screen
//      from the product, inventing one produces a control that opens nothing.
//   2. UNIQUENESS. No destination appears in two areas, and every area a
//      destination lands in is one of the seven approved keys.
//   3. IDENTITY. Activating each destination still opens the SAME view --
//      asserted against `document.body.dataset.view`, the app's own record of
//      what it rendered, not against the label that was clicked.
//   4. ROLE PARITY. All seven roles (League Admin, Arena Manager, Coach,
//      Player, Guardian, Official, Viewer) see exactly the destinations their
//      permissions allow, each in the right area, plus at least one forbidden
//      destination per restricted role proven both hidden AND non-functional
//      on direct navigation. Viewer additionally has zero enabled mutation
//      control anywhere it can reach.
//   5. KEYBOARD. A real Tab walk reaches every authorized destination across
//      all seven groups in DOM order, with a visible focus indicator, and
//      Enter activates -- at both viewports.
//   6. DEEP LINKS. Direct navigation (the app's own switchTab(), which is how
//      this SPA restores a destination -- it has no URL router) into a
//      representative destination in every populated area restores that exact
//      view, keeps the per-view title, and leaves the skip-link target intact.
//   7. NO OVERFLOW. Seven groups instead of five must not push the nav into a
//      horizontal page overflow or clip the final destination at 390x844.
//   8. Zero unexpected console/page errors throughout.
//
// FACILITIES is the crosswalk's Setup split, and it is a COMPOSITE
// destination: the same `setup` view plus a `setupWorkflow` half, so the
// Administration entry (the workflow INDEX) and the Facilities entry (the
// "Venues, rinks and ice" LANDING) share one route key while being two
// genuinely different destinations. Every identity/uniqueness assertion here
// therefore keys on `(tab, setupWorkflow)` -- `setup` vs `setup+facilities` --
// because matching on `data-tab` alone would call them the same screen. A
// dedicated leg proves the sidebar entry opens the summary-first LANDING (not
// the Calendar Ice Builder, which is that landing's own primary action), that
// its Add Ice primary still reaches the builder, and that a role without
// `manage_arena` can neither see nor directly activate it.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");
const {
  installContextFixture, selectProgram, selectProgramSeason,
} = require("./context-fixture.js");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8351 },
  { label: "phone", width: 390, height: 844, port: 8352 },
];

// The seven approved areas, in the crosswalk's own order. The area KEYS are
// the contract (`data-nav-area` in index.html); the labels are asserted too so
// a silent relabel to something outside the approved IA fails here.
const APPROVED_AREAS = [
  ["home_tasks", "Home/Tasks"],
  ["schedule", "Schedule"],
  ["teams_people", "Teams & People"],
  ["facilities", "Facilities"],
  ["communications", "Communications"],
  ["reports", "Reports"],
  ["administration", "Administration"],
];

// The expected destination -> area crosswalk, transcribed from
// docs/product/operator-ux-requirements.md § "Task-oriented navigation and
// setup". Kept as an explicit table (rather than derived from the DOM) so this
// file states what the IA SHOULD be: if production and this table disagree,
// one of them is wrong and the test says which destination.
const EXPECTED_AREA = {
  dashboard: "home_tasks", player_home: "home_tasks",
  guardian_home: "home_tasks", inbox: "home_tasks", activity: "home_tasks",
  calendar: "schedule", games: "schedule", scheduler: "schedule",
  standings: "schedule", sheet: "schedule", public: "schedule",
  roster: "teams_people",
  notifications: "communications", delivery: "communications",
  readiness: "reports",
  users: "administration", onboarding: "administration",
  import: "administration", setup: "administration",
  // The crosswalk's Setup split: the same `setup` view, distinguished by its
  // workflow half. Both are real, separately-reachable destinations.
  "setup+facilities": "facilities",
};

// A composite destination key ("setup+facilities") maps to a DIFFERENT button
// and a different activation path than a plain one. These two helpers keep
// every leg honest about that instead of assuming `data-tab` is the identity.
function selFor(key) {
  const [tab, workflow] = key.split("+");
  return workflow
    ? `.tab[data-setup-workflow-nav="${workflow}"]`
    : `.tab[data-tab="${tab}"]:not([data-setup-workflow-nav])`;
}
function viewFor(key) { return key.split("+")[0]; }

function fail(msg) { throw new Error(msg); }

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
  return page.evaluate(async (arg) => {
    const r = await fetch(arg.p, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(arg.body),
    });
    return { status: r.status, body: await r.json() };
  }, { p, body });
}

async function loginAs(page, username, password) {
  const res = await apiPost(page, "/api/auth/login", { username, password });
  if (res.status !== 200 || res.body.error) {
    fail(`login as ${username} failed: ${JSON.stringify(res)}`);
  }
}

async function logout(page) {
  await page.waitForLoadState("networkidle").catch(() => {});
  await apiPost(page, "/api/auth/logout", {});
}

async function waitForView(page, viewName) {
  await page.waitForFunction(
    (v) => document.body.dataset.view === v, viewName, { timeout: 10000 });
}

async function reachLanding(page, expected) {
  await page.waitForFunction(
    (v) => document.body.dataset.view === "onboarding"
      || document.body.dataset.view === v, expected, { timeout: 10000 });
  if (await page.evaluate(() => document.body.dataset.view) === "onboarding"
      && expected !== "onboarding") {
    await page.click('[data-onboarding-goto="dashboard"]');
    await waitForView(page, "dashboard");
  }
}

// The rendered nav, read straight from production markup: for every area, its
// key/label and the destinations currently VISIBLE inside it.
async function readNav(page) {
  return page.evaluate(() => {
    const areas = Array.from(document.querySelectorAll(".nav-group")).map((g) => {
      const label = g.querySelector(".nav-group-label");
      const tabs = Array.from(g.querySelectorAll(".tab"));
      return {
        key: g.dataset.navArea || null,
        label: label ? label.textContent.trim() : null,
        labelledBy: g.getAttribute("aria-labelledby"),
        labelId: label ? label.id : null,
        role: g.getAttribute("role"),
        groupHidden: g.style.display === "none",
        // Composite identity: two entries share data-tab="setup" (the
        // Administration workflow INDEX and the Facilities LANDING), so the
        // destination key is (tab, setupWorkflow) -- `setup` vs `setup+facilities`.
        all: tabs.map((t) => t.dataset.tab
          + (t.dataset.setupWorkflowNav ? `+${t.dataset.setupWorkflowNav}` : "")),
        visible: tabs.filter((t) => t.style.display !== "none")
          .map((t) => t.dataset.tab
            + (t.dataset.setupWorkflowNav ? `+${t.dataset.setupWorkflowNav}` : "")),
      };
    });
    return { areas, navKeys: Object.keys(NAV) };
  });
}

async function assertNoOverflow(page, label) {
  const o = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  }));
  if (o.scrollWidth > o.clientWidth) {
    fail(`${label}: horizontal page overflow — scrollWidth ${o.scrollWidth} > `
      + `clientWidth ${o.clientWidth}`);
  }
}

// ---- (1)(2) inventory + uniqueness, against the production NAV map ---------
async function checkInventoryAndUniqueness(page, L) {
  const { areas, navKeys } = await readNav(page);

  // The seven approved areas, in order, each semantically grouped and named.
  const gotAreas = areas.map((a) => [a.key, a.label]);
  if (JSON.stringify(gotAreas) !== JSON.stringify(APPROVED_AREAS)) {
    fail(`${L}: nav areas are not the seven approved ones in order.\n`
      + `  expected ${JSON.stringify(APPROVED_AREAS)}\n`
      + `  got      ${JSON.stringify(gotAreas)}`);
  }
  for (const a of areas) {
    if (a.role !== "group") fail(`${L}: area ${a.key} lacks role="group"`);
    if (!a.labelledBy || a.labelledBy !== a.labelId) {
      fail(`${L}: area ${a.key} is not aria-labelledby its own label `
        + `(aria-labelledby=${a.labelledBy}, label id=${a.labelId})`);
    }
  }

  // Every destination lands in exactly one area, and that area is approved.
  const seen = new Map();
  for (const a of areas) {
    for (const tab of a.all) {
      if (seen.has(tab)) {
        fail(`${L}: destination "${tab}" appears in TWO areas `
          + `(${seen.get(tab)} and ${a.key}) — a destination must belong to `
          + `exactly one approved area`);
      }
      seen.set(tab, a.key);
    }
  }

  // Neither direction may drift: no NAV key missing from the IA, and no
  // rendered destination that is not a real NAV key.
  // A composite destination's NAV key is its view half, so `setup` is covered
  // by either entry; strip the workflow suffix before checking NAV coverage.
  const baseKeys = new Set([...seen.keys()].map((k) => k.split("+")[0]));
  const missing = navKeys.filter((k) => !baseKeys.has(k));
  if (missing.length) {
    fail(`${L}: destination(s) in the production NAV map are absent from the `
      + `seven-area navigation: ${missing.join(", ")}`);
  }
  const invented = [...baseKeys].filter((k) => !navKeys.includes(k));
  if (invented.length) {
    fail(`${L}: navigation renders destination(s) with no NAV entry `
      + `(they would open nothing): ${invented.join(", ")}`);
  }

  // And each one is in the area the approved crosswalk puts it in.
  for (const [tab, area] of seen) {
    if (EXPECTED_AREA[tab] !== area) {
      fail(`${L}: destination "${tab}" is in area "${area}" but the approved `
        + `crosswalk places it in "${EXPECTED_AREA[tab]}"`);
    }
  }

  // Any view key carried by MORE THAN ONE nav entry makes the bare
  // `.tab[data-tab="<key>"]` selector ambiguous for every existing consumer.
  // That is not hypothetical: adding the Facilities entry broke
  // setup-workflow-hub, accessibility-foundations and role-authorization-matrix
  // exactly this way, because Facilities sorts before Administration in DOM
  // order so the bare selector silently resolved to the wrong destination.
  // Flag it here so the next composite destination is caught at authoring time
  // rather than as three unrelated-looking journey failures.
  const perView = new Map();
  for (const [dest, area] of seen) {
    const v = dest.split("+")[0];
    perView.set(v, (perView.get(v) || []).concat(`${dest} (${area})`));
  }
  const shared = [...perView].filter(([, list]) => list.length > 1);
  const KNOWN_SHARED = ["setup"];
  for (const [v, list] of shared) {
    if (!KNOWN_SHARED.includes(v)) {
      fail(`${L}: view key "${v}" is now used by ${list.length} nav entries `
        + `(${list.join(", ")}). Every journey selecting `
        + `.tab[data-tab="${v}"] becomes ambiguous — narrow those selectors `
        + `and add "${v}" to KNOWN_SHARED once done.`);
    }
  }

  // Explicit, separately-named checks the issue calls out by name.
  if (seen.get("users") !== "administration") {
    fail(`${L}: "Users" must live under Administration, found in `
      + `"${seen.get("users")}"`);
  }
  const facilities = areas.find((a) => a.key === "facilities");
  if (JSON.stringify(facilities.all) !== JSON.stringify(["setup+facilities"])) {
    fail(`${L}: Facilities must carry exactly the composite "Venues, rinks and `
      + `ice" destination, got ${JSON.stringify(facilities.all)}`);
  }
  return seen;
}

// ---- (3) every destination still opens the same view ----------------------
async function checkDestinationIdentity(page, L) {
  const { areas } = await readNav(page);
  const visible = areas.flatMap((a) => a.visible);
  for (const tab of visible) {
    await page.click(selFor(tab));
    // `public` renders the read-only portal inside the shell; every other
    // destination sets body.dataset.view to its own (view-half) key.
    await page.waitForFunction(
      (t) => document.body.dataset.view === t, viewFor(tab), { timeout: 10000 })
      .catch(() => fail(`${L}: activating "${tab}" did not open view `
        + `"${viewFor(tab)}". The route key must survive regrouping.`));
    const title = await page.title();
    if (!title || !title.includes("Hockey Scheduler")) {
      fail(`${L}: destination "${tab}" left a bad per-view title: "${title}"`);
    }
  }
  return visible;
}

// ---- (5) keyboard-only reach across all seven groups ----------------------
async function checkKeyboardReach(page, L, expectedVisible) {
  await page.evaluate(() => {
    if (document.activeElement && document.activeElement.blur) {
      document.activeElement.blur();
    }
  });
  const reached = [];
  for (let i = 0; i < 200 && reached.length < expectedVisible.length; i += 1) {
    await page.keyboard.press("Tab");
    const info = await page.evaluate(() => {
      const el = document.activeElement;
      if (!el || !el.classList || !el.classList.contains("tab")) return null;
      const cs = getComputedStyle(el);
      const ring = (cs.outlineStyle !== "none" && parseFloat(cs.outlineWidth) > 0)
        || (cs.boxShadow && cs.boxShadow !== "none");
      // Composite key, so the two data-tab="setup" entries are not deduped
      // into one and a genuinely unreachable one cannot hide behind the other.
      return {
        tab: el.dataset.tab
          + (el.dataset.setupWorkflowNav ? `+${el.dataset.setupWorkflowNav}` : ""),
        ring,
      };
    });
    if (info && !reached.includes(info.tab)) {
      if (!info.ring) {
        fail(`${L}: destination "${info.tab}" received keyboard focus with no `
          + `visible focus indicator`);
      }
      reached.push(info.tab);
    }
  }
  const missed = expectedVisible.filter((t) => !reached.includes(t));
  if (missed.length) {
    fail(`${L}: keyboard Tab never reached authorized destination(s): `
      + `${missed.join(", ")} (reached ${reached.join(", ")})`);
  }
  // Enter on a focused destination activates it, same as a click.
  await page.focus('.tab[data-tab="activity"]');
  await page.keyboard.press("Enter");
  await waitForView(page, "activity").catch(() =>
    fail(`${L}: Enter did not activate the focused destination`));
}

// ---- (6) deep-link restoration into every populated area -------------------
async function checkDeepLinks(page, L, visible) {
  // One representative destination per populated area the current role sees.
  const byArea = new Map();
  for (const tab of visible) {
    const area = EXPECTED_AREA[tab];
    if (!byArea.has(area)) byArea.set(area, tab);
  }
  for (const [area, tab] of byArea) {
    const workflow = tab.includes("+") ? tab.split("+")[1] : null;
    await page.evaluate(([t, wf]) => (wf
      ? openSetupWorkflowLanding(wf) : switchTab(t)), [viewFor(tab), workflow]);
    await waitForView(page, viewFor(tab)).catch(() =>
      fail(`${L}: direct navigation into "${tab}" (area ${area}) did not `
        + `restore that view`));
    if (workflow) {
      // render() is async, so wait for the landing to paint rather than
      // sampling the frame the view key flipped on.
      await page.waitForFunction((wf) => !!document.querySelector(
        `[data-setup-workflow-landing="${wf}"]`), workflow, { timeout: 10000 })
        .catch(() => fail(`${L}: deep-link restoration of "${tab}" reached view `
          + `"${viewFor(tab)}" but not its "${workflow}" landing — the `
          + `composite destination's workflow half was lost`));
    }
    const skip = await page.evaluate(() => {
      const link = document.getElementById("skip-link");
      const target = document.getElementById("content");
      return {
        hasLink: !!link,
        targetVisible: !!(target && target.offsetParent !== null),
      };
    });
    if (!skip.hasLink || !skip.targetVisible) {
      fail(`${L}: after deep-linking to "${tab}" the skip link or its `
        + `#content target is broken: ${JSON.stringify(skip)}`);
    }
  }
}

// ---- (4) per-role parity ---------------------------------------------------
async function checkRole(page, L, role, expect) {
  const { areas } = await readNav(page);
  const visible = areas.flatMap((a) => a.visible).sort();
  const want = [...expect.destinations].sort();
  if (JSON.stringify(visible) !== JSON.stringify(want)) {
    fail(`${L}/${role}: visible destinations changed.\n`
      + `  expected ${JSON.stringify(want)}\n  got      ${JSON.stringify(visible)}`);
  }
  // Every visible destination sits in its approved area for this role too --
  // regrouping must not move a destination for some roles and not others.
  for (const a of areas) {
    for (const tab of a.visible) {
      if (EXPECTED_AREA[tab] !== a.key) {
        fail(`${L}/${role}: "${tab}" rendered in area "${a.key}", crosswalk `
          + `says "${EXPECTED_AREA[tab]}"`);
      }
    }
  }
  // A forbidden destination is hidden AND non-functional on direct navigation.
  if (expect.forbidden) {
    const t = expect.forbidden;
    if (visible.includes(t)) {
      fail(`${L}/${role}: forbidden destination "${t}" is visible in the nav`);
    }
    await page.evaluate((v) => switchTab(v), t);
    await page.waitForTimeout(150);
    const leaked = await page.evaluate(() => ({
      view: document.body.dataset.view,
      newControls: document.querySelectorAll("#content .sc-new, #content [data-drawer]").length,
    }));
    if (leaked.newControls > 0) {
      fail(`${L}/${role}: direct navigation to "${t}" exposed `
        + `${leaked.newControls} create control(s)`);
    }
  }
  // Viewer: zero enabled mutation control anywhere it can reach.
  if (role === "viewer") {
    for (const tab of visible) {
      await page.evaluate((v) => switchTab(v), tab);
      await page.waitForTimeout(120);
      const mutations = await page.evaluate(() => Array.from(
        document.querySelectorAll(
          "#content button.act.primary, #content .sc-new, #content [data-drawer], "
          + "#content [data-del], #content [data-open-drawer]"))
        .filter((el) => !el.disabled && el.offsetParent !== null).length);
      if (mutations > 0) {
        fail(`${L}/viewer: ${mutations} enabled mutation control(s) on "${tab}"`);
      }
    }
  }
}

// ---- Facilities: the composite "Venues, rinks and ice" destination ---------
// The sidebar entry must open the SUMMARY-FIRST LANDING the IA crosswalk
// requires -- not goToSetupWorkflow("facilities"), which jumps straight to the
// Calendar Ice Availability Builder (that landing's own primary action). The
// two are easy to confuse because both are "Facilities work", which is exactly
// why this asserts the landing's own workflow marker rather than just "some
// setup screen rendered".
async function checkFacilitiesLanding(page, L, role) {
  await page.click('.tab[data-setup-workflow-nav="facilities"]');
  await page.waitForFunction(() => document.body.dataset.view === "setup"
    && !!document.querySelector('[data-setup-workflow-landing="facilities"]'),
    null, { timeout: 10000 })
    .catch(() => fail(`${L}/${role}: the Facilities nav entry did not open the `
      + `"Venues, rinks and ice" summary-first landing`));

  // focusContentHeading() polls for the painted heading, so wait for focus to
  // SETTLE inside the landing rather than sampling the instant it renders --
  // otherwise this asserts against a frame the app hasn't finished.
  await page.waitForFunction(() => {
    const el = document.querySelector('[data-setup-workflow-landing="facilities"]');
    return !!(el && el.contains(document.activeElement));
  }, null, { timeout: 5000 })
    .catch(() => fail(`${L}/${role}: focus never settled inside the Facilities `
      + `landing (focusContentHeading did not reach its heading)`));

  const landed = await page.evaluate(() => {
    const el = document.querySelector('[data-setup-workflow-landing="facilities"]');
    const h = el && el.querySelector(".swf-landing-title");
    const primary = el && el.querySelector(".swf-actions .act.primary");
    const active = Array.from(document.querySelectorAll(".tab.active"))
      .map((t) => t.dataset.tab
        + (t.dataset.setupWorkflowNav ? `+${t.dataset.setupWorkflowNav}` : ""));
    return {
      heading: h ? h.textContent.trim() : null,
      primaryLabel: primary ? primary.textContent.trim() : null,
      active,
      title: document.title,
      focusInLanding: !!(el && el.contains(document.activeElement)),
    };
  });
  if (!landed.heading || !landed.heading.includes("Venues, rinks and ice")) {
    fail(`${L}/${role}: Facilities landing heading is "${landed.heading}", `
      + `expected the "Venues, rinks and ice" workflow`);
  }
  if (landed.primaryLabel !== "Add Ice") {
    fail(`${L}/${role}: Facilities landing primary action is `
      + `"${landed.primaryLabel}", expected "Add Ice"`);
  }
  // Exactly ONE nav entry highlights: the composite one, never Administration's
  // plain Setup, even though both carry data-tab="setup".
  if (JSON.stringify(landed.active) !== JSON.stringify(["setup+facilities"])) {
    fail(`${L}/${role}: active nav after opening Facilities is `
      + `${JSON.stringify(landed.active)}, expected exactly ["setup+facilities"]`);
  }
  if (!landed.title.includes("Hockey Scheduler")) {
    fail(`${L}/${role}: Facilities landing left a bad title "${landed.title}"`);
  }
  if (!landed.focusInLanding) {
    fail(`${L}/${role}: focus was not moved into the Facilities landing`);
  }

  // The landing's own primary action still reaches the real Ice Builder.
  await page.click('[data-setup-workflow-landing="facilities"] .swf-actions .act.primary');
  await page.waitForFunction(
    () => document.body.dataset.view === "calendar", null, { timeout: 10000 })
    .catch(() => fail(`${L}/${role}: "Add Ice" did not open the Calendar Ice `
      + `Availability Builder`));

  await checkNoStaleTransientState(page, L, role);

  // Administration's plain Setup is still the workflow INDEX, not the landing.
  await page.click('.tab[data-tab="setup"]:not([data-setup-workflow-nav])');
  await page.waitForFunction(() => document.body.dataset.view === "setup",
    null, { timeout: 10000 });
  const onIndex = await page.evaluate(() => ({
    landing: !!document.querySelector("[data-setup-workflow-landing]"),
    active: Array.from(document.querySelectorAll(".tab.active"))
      .map((t) => t.dataset.tab
        + (t.dataset.setupWorkflowNav ? `+${t.dataset.setupWorkflowNav}` : "")),
  }));
  if (onIndex.landing) {
    fail(`${L}/${role}: Administration's Setup opened a workflow landing; it `
      + `must return to the workflow index`);
  }
  if (JSON.stringify(onIndex.active) !== JSON.stringify(["setup"])) {
    fail(`${L}/${role}: active nav on the Setup index is `
      + `${JSON.stringify(onIndex.active)}, expected exactly ["setup"]`);
  }
}

// Transient per-view UI state must not survive the Facilities transition.
// openSetupWorkflowLanding() deliberately bypasses switchTab(), and the first
// version of it bypassed switchTab's RESET discipline too -- so a live Ice
// Builder (Calendar) or a pending checkout confirmation (Player/Guardian Home)
// travelled into the landing and rendered as a stale overlay over a different
// destination. Sets each piece of state through the app's own module bindings,
// navigates via the real sidebar entry, and requires it cleared.
async function checkNoStaleTransientState(page, L, role) {
  const cases = [
    { name: "calendar Ice Builder", set: () => {
        iceBuilder = { form: { probe: true }, preview: null };
        wizard = { probe: true }; conflict = { probe: true };
        movingGameId = "probe-game"; pendingMove = { probe: true };
      },
      read: () => ({ iceBuilder, wizard, conflict, movingGameId, pendingMove }) },
    { name: "player-home checkout confirm", set: () => {
        checkoutConfirm = { game_id: "probe" };
        oppDetailGame = "probe"; oppDetail = { probe: true };
      },
      read: () => ({ checkoutConfirm, oppDetailGame, oppDetail }) },
    { name: "guardian-home checkout confirm", set: () => {
        gCheckout = { jid: "probe", game_id: "probe" };
        gOpp = { jid: "probe" }; gOppDetail = { probe: true };
      },
      read: () => ({ gCheckout, gOpp, gOppDetail }) },
  ];
  for (const c of cases) {
    // Start from the Setup INDEX so the only transition under test is the one
    // into the Facilities landing.
    await page.evaluate(() => switchTab("setup"));
    await page.waitForFunction(() => document.body.dataset.view === "setup",
      null, { timeout: 10000 });
    await page.evaluate(`(${c.set.toString()})()`);
    await page.click('.tab[data-setup-workflow-nav="facilities"]');
    await page.waitForFunction(() => !!document.querySelector(
      '[data-setup-workflow-landing="facilities"]'), null, { timeout: 10000 });
    const left = await page.evaluate(`(${c.read.toString()})()`);
    const stale = Object.entries(left).filter(([, v]) =>
      v !== null && v !== undefined && v !== "");
    if (stale.length) {
      fail(`${L}/${role}: ${c.name} survived the Facilities transition — `
        + `${stale.map(([k]) => k).join(", ")} still set. A destination change `
        + `must apply the same transient-state reset switchTab() does.`);
    }
  }
}

// A role WITHOUT manage_arena must neither see the composite destination nor
// be able to activate it directly -- the nav gate and the transition guard
// both have to hold, not just the one that is easier to satisfy.
async function checkFacilitiesDenied(page, L, role) {
  const visible = await page.evaluate(() => {
    const el = document.querySelector('.tab[data-setup-workflow-nav="facilities"]');
    return !!(el && el.style.display !== "none");
  });
  if (visible) {
    fail(`${L}/${role}: the Facilities destination is visible without manage_arena`);
  }
  const opened = await page.evaluate(
    () => openSetupWorkflowLanding("facilities"));
  if (opened !== false) {
    fail(`${L}/${role}: openSetupWorkflowLanding("facilities") returned `
      + `${JSON.stringify(opened)} without manage_arena — it must fail closed`);
  }
  const leaked = await page.evaluate(
    () => !!document.querySelector('[data-setup-workflow-landing="facilities"]'));
  if (leaked) {
    fail(`${L}/${role}: direct activation rendered the Facilities landing `
      + `without manage_arena`);
  }
}

async function run(browser, viewport) {
  const L = viewport.label;
  const base = `http://${HOST}:${viewport.port}`;
  const server = spawn(
    process.env.PYTHON || "python3",
    ["-u", "-m", "hockey_scheduler.web.server", "--host", HOST,
      "--port", String(viewport.port)],
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
    await installContextFixture(page);
    await reachLanding(page, "dashboard");

    // A minimal own fixture (a fresh boot seeds only the "admin" account), so
    // the scoped roles below resolve to real subjects rather than to nothing --
    // an unbound Coach/Player would hide its destinations for the wrong reason
    // and quietly weaken the parity assertions.
    const suffix = String(viewport.port);
    const mkFixture = async (p, body) => {
      const res = await apiPost(page, p, body);
      if (res.status !== 200 || res.body.error) {
        fail(`${L}: fixture ${p} failed: ${JSON.stringify(res)}`);
      }
      return res.body;
    };
    const program = await mkFixture("/api/v2/setup/program",
      { name: `Nav Program ${suffix}`, country: "US" });
    // THE EXPLICIT SELECTION (#409). Minting the Program does not select it,
    // and no create below is allowed to infer its axes from the parent it
    // names. Boundary 1 is Program-only: the Season, Team, Player, Official,
    // Venue and Rink creates are PROGRAM-AXIS. See ./context-fixture.js for
    // the axis table these two boundaries are drawn from, and for why the
    // selection is proved by the POST echo rather than by a GET that the
    // fallback resolver can satisfy on its own.
    await selectProgram(page, `${L} Program-only bootstrap`, program.id);
    const season = await mkFixture("/api/v2/setup/season",
      { program_id: program.id, name: `Nav Season ${suffix}` });
    // BOUNDARY 2 — Program+Season. Everything from the League down here
    // (League, Division, the team registration, the season venue-access grant)
    // is SEASON-OWNED and consumes both axes, against THIS Season.
    await selectProgramSeason(page, `${L} Program+Season`, program.id, season.id);
    const league = await mkFixture("/api/v2/setup/league",
      { season_id: season.id, name: `Nav League ${suffix}` });
    const division = await mkFixture("/api/v2/setup/division",
      { league_id: league.id, name: `Nav Division ${suffix}`, season_id: season.id });
    const club = await mkFixture("/api/v2/setup/club", { name: `Nav Club ${suffix}` });
    const team = await mkFixture("/api/v2/setup/team",
      { club_id: club.id, league_id: league.id, name: `Nav Team ${suffix}` });
    await mkFixture(`/api/setup/seasons/${season.id}/team-registrations`,
      { team_id: team.id, division_id: division.id });
    const player = await mkFixture("/api/v2/setup/player",
      { team_id: team.id, name: `Nav Player ${suffix}`, position: "forward" });
    const official = await mkFixture("/api/v2/setup/official",
      { name: `Nav Official ${suffix}` });
    // A Venue and a Rink, granted to this Season (#365): the Facilities leg
    // below asserts that the landing's primary action is "Add Ice" and that it
    // opens the Ice Availability Builder. "Add Ice" is only the action that
    // resolves this landing's state once a rink exists to hang ice on -- on an
    // installation with no venue at all the landing is EMPTY and its single
    // action is the one that CAN resolve that ("Add venue"), per #365's
    // dead-end ruling. Seeding the real prerequisite is what makes the "Add
    // Ice" assertion below a statement about the builder rather than about an
    // empty install.
    const org = await mkFixture("/api/v2/setup/organization",
      { name: `Nav Facility Org ${suffix}` });
    const venue = await mkFixture("/api/v2/setup/venue",
      { name: `Nav Venue ${suffix}`, organization_id: org.id });
    await mkFixture("/api/v2/setup/rink",
      { venue_id: venue.id, name: `Nav Rink ${suffix}` });
    await mkFixture(`/api/v2/setup/seasons/${season.id}/venue-access`,
      { venue_id: venue.id });

    const PW = "nav-area-pw";
    const mk = (r) => `nav_${r}_${suffix}`;
    const accounts = {
      arena_manager: { username: mk("arena"), role: "arena_manager", scope: {} },
      coach: { username: mk("coach"), role: "coach", scope: { team_id: team.id } },
      player: { username: mk("player"), role: "player",
        scope: { player_id: player.id, team_id: team.id } },
      guardian: { username: mk("guardian"), role: "guardian", scope: {} },
      official: { username: mk("official"), role: "official",
        scope: { official_id: official.id } },
      viewer: { username: mk("viewer"), role: "viewer", scope: {} },
    };
    for (const key of Object.keys(accounts)) {
      const a = accounts[key];
      const res = await apiPost(page, "/api/accounts",
        { username: a.username, password: PW, role: a.role, scope: a.scope });
      if (res.status !== 200 || res.body.error) {
        fail(`${L}: account create failed for ${a.username}: ${JSON.stringify(res)}`);
      }
    }

    // ---- League Admin: the full IA, inventory + uniqueness + identity ----
    const crosswalk = await checkInventoryAndUniqueness(page, `${L}/league_admin`);
    const adminVisible = await checkDestinationIdentity(page, `${L}/league_admin`);
    await checkKeyboardReach(page, `${L}/league_admin`, adminVisible);
    await checkDeepLinks(page, `${L}/league_admin`, adminVisible);
    await checkFacilitiesLanding(page, L, "league_admin");
    await assertNoOverflow(page, `${L}/league_admin`);

    // ---- the other six roles ----
    // Captured from the PRE-change build and asserted unchanged: this table is
    // the "a role must not gain or lose a destination by regrouping" invariant,
    // so it is deliberately the observed baseline rather than a hand-reasoned
    // guess about what each role "should" see.
    const EXPECT = {
      arena_manager: {
        destinations: ["activity", "calendar", "dashboard", "delivery", "games",
          "import", "notifications", "public", "roster", "scheduler", "setup",
          "setup+facilities", "sheet", "standings"],
        forbidden: "users",
      },
      coach: {
        destinations: ["activity", "calendar", "dashboard", "games",
          "notifications", "public", "roster", "sheet", "standings"],
        forbidden: "setup",
      },
      player: {
        destinations: ["calendar", "games", "notifications", "player_home",
          "public", "roster", "sheet", "standings"],
        forbidden: "setup",
      },
      guardian: {
        destinations: ["calendar", "games", "guardian_home", "notifications",
          "public", "standings"],
        forbidden: "setup",
      },
      official: {
        destinations: ["calendar", "games", "inbox", "notifications", "public",
          "roster", "sheet", "standings"],
        forbidden: "users",
      },
      viewer: {
        destinations: ["calendar", "games", "notifications", "public",
          "standings"],
        forbidden: "setup",
      },
    };
    const LANDING = {
      arena_manager: "dashboard", coach: "dashboard", player: "player_home",
      guardian: "guardian_home", official: "inbox", viewer: "standings",
    };
    for (const role of Object.keys(EXPECT)) {
      // Deliberately NO page load between logout and login: boot()'s
      // zero-friction demo auto-login as League Admin races a scoped login and
      // can win, leaving the previous role's nav on screen to be read as this
      // role's. Log in first, then load once and wait for the shell to actually
      // reflect this user before asserting anything about it.
      await logout(page);
      await loginAs(page, accounts[role].username, PW);
      await page.goto(base);
      await page.waitForFunction((u) => (typeof currentUser !== "undefined")
        && currentUser && currentUser.username === u,
        accounts[role].username, { timeout: 10000 });
      await reachLanding(page, LANDING[role]);
      await checkRole(page, L, role, EXPECT[role]);
      // Facilities is manage_arena-gated: Arena Manager (whose primary journey
      // this is) must reach the landing; every role without it must be denied
      // by BOTH the nav gate and the transition guard.
      if (role === "arena_manager") await checkFacilitiesLanding(page, L, role);
      else await checkFacilitiesDenied(page, L, role);
      await assertNoOverflow(page, `${L}/${role}`);
    }

    if (errors.length) {
      fail(`${L}: unexpected console/page errors:\n  ${errors.join("\n  ")}`);
    }
    console.log(`[${L}] OK — seven approved areas, ${crosswalk.size} destinations `
      + `each in exactly one area, all seven roles' visibility unchanged, `
      + `keyboard + deep-link restoration intact, no overflow.`);
  } catch (e) {
    if (serverOutput.trim()) {
      console.error("--- server output ---\n" + serverOutput.trim());
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
    for (const viewport of VIEWPORTS) await run(browser, viewport);
    console.log("Seven-area navigation browser journey passed.");
  } finally {
    await browser.close();
  }
})().catch((e) => {
  console.error("Seven-area navigation browser journey FAILED.");
  console.error(e.message);
  process.exit(1);
});
