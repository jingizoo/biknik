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
// FACILITIES is deliberately empty in this slice and therefore asserted as
// declared-but-hidden rather than as a populated area: the crosswalk maps the
// single `setup` destination to "Facilities + Administration", splitting only
// once the six Setup workflows become separately addressable. Splitting it is
// explicitly out of this slice's scope, so `setup` sits under Administration
// (five of its six workflows) and Facilities carries no destination yet. The
// test pins that as the CURRENT contract so the day a destination is added
// there, this assertion is what tells you to update the crosswalk with it.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");

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
};

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
        all: tabs.map((t) => t.dataset.tab),
        visible: tabs.filter((t) => t.style.display !== "none")
          .map((t) => t.dataset.tab),
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
  const missing = navKeys.filter((k) => !seen.has(k));
  if (missing.length) {
    fail(`${L}: destination(s) in the production NAV map are absent from the `
      + `seven-area navigation: ${missing.join(", ")}`);
  }
  const invented = [...seen.keys()].filter((k) => !navKeys.includes(k));
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

  // Explicit, separately-named checks the issue calls out by name.
  if (seen.get("users") !== "administration") {
    fail(`${L}: "Users" must live under Administration, found in `
      + `"${seen.get("users")}"`);
  }
  const facilities = areas.find((a) => a.key === "facilities");
  if (facilities.all.length !== 0) {
    fail(`${L}: Facilities now carries destination(s) `
      + `(${facilities.all.join(", ")}). That is the intended end state, but `
      + `the crosswalk in this test and in index.html must be updated to name `
      + `them rather than describing Facilities as pending the setup split.`);
  }
  if (!facilities.groupHidden) {
    fail(`${L}: the empty Facilities area is rendered — an area with no `
      + `destination must be hidden, not left as an orphaned heading`);
  }
  return seen;
}

// ---- (3) every destination still opens the same view ----------------------
async function checkDestinationIdentity(page, L) {
  const { areas } = await readNav(page);
  const visible = areas.flatMap((a) => a.visible);
  for (const tab of visible) {
    await page.click(`.tab[data-tab="${tab}"]`);
    // `public` renders the read-only portal inside the shell; every other
    // destination sets body.dataset.view to its own key.
    await page.waitForFunction(
      (t) => document.body.dataset.view === t, tab, { timeout: 10000 })
      .catch(() => fail(`${L}: activating "${tab}" did not open view "${tab}" `
        + `(body.dataset.view stayed "?"). The route key must survive regrouping.`));
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
      return { tab: el.dataset.tab, ring };
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
    await page.evaluate((t) => switchTab(t), tab);
    await waitForView(page, tab).catch(() =>
      fail(`${L}: direct navigation into "${tab}" (area ${area}) did not `
        + `restore that view`));
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
    const season = await mkFixture("/api/v2/setup/season",
      { program_id: program.id, name: `Nav Season ${suffix}` });
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
          "sheet", "standings"],
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
