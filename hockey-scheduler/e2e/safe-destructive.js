// Safe destructive actions browser journey (#215).
//
// At desktop and phone widths, a League Admin exercises the four destructive
// surfaces end to end:
//   1. Blocked deletion — deleting a league that still has a season/team is
//      refused with a themed dependency modal listing what's in the way, and
//      nothing is deleted.
//   2. Successful deletion — a bare team is deleted through the confirm modal.
//   3. Remove from season — the redesigned "Remove from season" action
//      deactivates a registration.
//   4. Reset demo — the header "Reset demo" button opens a confirm modal that
//      requires typing RESET, then atomically reseeds the demo.
// Fails on any browser console/page error.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8155 },
  { label: "phone", width: 390, height: 844, port: 8156 },
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

// Selects the Hierarchy sub-view via the user-visible toggle and confirms it
// took effect. Centralized so every Setup entry uses the same path and a
// later mutation cannot silently leave us on the workflow hub.
async function enterSetupHierarchy(page) {
  await page.click('[data-setup-view="hierarchy"]');
  await page.waitForFunction(() => {
    const seg = document.querySelector(".setup-viewtoggle .seg.active");
    return !!(seg && seg.dataset.setupView === "hierarchy");
  }, null, { timeout: 10000 });
}

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
  page.on("console", (m) => {
    // This journey deliberately exercises an error path (a blocked delete
    // returns HTTP 409, handled gracefully by the app's modal). Chromium logs
    // any non-2xx fetch as a "Failed to load resource" console error, so ignore
    // that network-status noise; real JS exceptions still arrive as pageerror
    // and app-level console.error messages, which stay fatal.
    if (m.type() === "error" && !/Failed to load resource/.test(m.text())) {
      errors.push(`[console] ${m.text()}`);
    }
  });

  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });

    // Build a league with a registered team (so it can't be deleted) plus a
    // bare team (which can). The demo default role is League Admin.
    const ids = await page.evaluate(async () => {
      const post = async (p, b) => {
        const r = await fetch(p, {
          method: "POST", credentials: "same-origin",
          headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
        });
        return { status: r.status, body: await r.json() };
      };
      // ASSERT EVERY CREATE BEFORE ITS ID IS USED (#409 review round). This
      // fixture used to decode each body and throw the status away, so a
      // refused create returned an id-less body whose `undefined` id was
      // threaded straight into the next parent. Nothing noticed until step (1)
      // waited ten seconds for a `.modal.blocked` that could never appear —
      // the Program had no dependents to block on because every dependent
      // create after it had been refused. Each create now fails at its own
      // line, naming itself.
      const create = async (what, p, b) => {
        const { status, body } = await post(p, b);
        if (status !== 200 || !body || body.error || !body.id) {
          throw new Error(`${what} create failed: ${status} ${JSON.stringify(body)}`);
        }
        return body;
      };
      // THE EXPLICIT SELECTION (#409 review round). A create no longer inherits
      // its axes from the parent it names, nor from the account that minted
      // that parent: a Program-axis create (Season, Team) requires the SAVED
      // Program, and a Season-owned two-axis create (League, Division,
      // registration) requires BOTH saved axes — the same Season it writes
      // into. Minting "Del League" does not select it.
      //
      // Driven through `setActiveContext` — the app's OWN switch pipeline, the
      // exact function `#ctx-select`'s onchange calls — because this journey
      // goes on to drive real UI: the Setup hierarchy tree, the Season
      // participation panel and their delete controls are all painted from
      // client state, and a raw fetch would leave the page rendering one
      // context while the server enforced another.
      //
      // The read-back is ASSERTED, not assumed: a selection that silently fell
      // back to inferred context would leave this journey green on precisely
      // the behaviour #409 removes.
      const selectContext = async (what, programId, seasonId) => {
        await setActiveContext(programId, seasonId || null);
        const r = await fetch("/api/context", { credentials: "same-origin" });
        const read = await r.json();
        if (r.status !== 200 || read.error) {
          throw new Error(`${what}: could not read back the explicit selection: `
            + `${r.status} ${JSON.stringify(read)}`);
        }
        if (read.program_id !== programId
            || (read.season_id || null) !== (seasonId || null)) {
          throw new Error(`${what}: the active context did not read back as the `
            + `explicit selection — wanted program=${programId} `
            + `season=${seasonId || null}, got program=${read.program_id} `
            + `season=${read.season_id || null}`);
        }
      };
      const league = await create("Program", "/api/setup/league", { name: "Del League" });
      // BOUNDARY 1 — the Program-only bootstrap selection. The Season below is
      // a Program-axis create.
      await selectContext("the Program-only bootstrap", league.id, null);
      const season = await create("Season", "/api/setup/season",
        { league_id: league.id, name: "Del Season" });
      // BOUNDARY 2 — the Program+Season selection. Everything from the grouping
      // League down is Season-owned and consumes BOTH axes.
      await selectContext("the Program+Season selection", league.id, season.id);
      // A grouping League (v1 "level") under the season, with the division
      // parented to it (#233 Slice B2b): the v2 Setup Season participation
      // panel only shows a registration whose League resolves — without a
      // level_id here the division/registration would be structurally
      // dangling and never render a Remove control below.
      const level = await create("League", "/api/setup/level",
        { season_id: season.id, name: "Del Level" });
      const div = await create("Division", "/api/setup/division",
        { season_id: season.id, name: "Del Div", level_id: level.id });
      const club = await create("Club", "/api/setup/club", { name: "Del Club" });
      const team = await create("Del Team", "/api/setup/team",
        { club_id: club.id, league_id: league.id, name: "Del Team" });
      await create("registration", `/api/setup/seasons/${season.id}/team-registrations`,
        { team_id: team.id, division_id: div.id });
      const bare = await create("Bare Team", "/api/setup/team",
        { club_id: club.id, league_id: league.id, name: "Bare Team" });
      return { league: league.id, season: season.id, div: div.id,
        club: club.id, team: team.id, bare: bare.id };
    });

    const getJson = (p) => page.evaluate(
      (u) => fetch(u, { credentials: "same-origin" }).then((r) => r.json()), p);

    await page.click('.tab[data-tab="setup"]');
      // Setup now LANDS on the six-workflow hub (#345 batch 2). Select the
      // Hierarchy sub-view through the real toggle -- never by setting
      // setupView directly -- and assert the segment is actually active
      // BEFORE waiting on any domain control, so a future default change
      // fails here with "wrong sub-view" instead of an opaque selector
      // timeout on a control that was simply never rendered.
      await enterSetupHierarchy(page);
    // Scoped to the Competition structure tree (#251): the Season
    // participation panel below it now renders its own delBtn for the same
    // Program/Season/League ids, so an unscoped selector would match twice.
    await page.waitForSelector(
      `#competition-structure [data-del="league"][data-del-id="${ids.league}"]`,
      { timeout: 15000 });

    // (1) Blocked deletion: league is a high-risk record, so its Delete button
    // stays disabled until DELETE is typed (#215); then the server refuses and
    // the blocked modal lists the dependents; the league survives.
    await page.click(`#competition-structure [data-del="league"][data-del-id="${ids.league}"]`);
    await page.waitForSelector(".modal.danger [data-del-confirm]", { timeout: 10000 });
    if (!(await page.$eval("[data-del-confirm]", (b) => b.disabled))) {
      throw new Error(`[${viewport.label}] high-risk delete was enabled before typing DELETE`);
    }
    await page.fill("#del-confirm", "DELETE");
    await page.waitForFunction(
      () => !document.querySelector("[data-del-confirm]").disabled, null, { timeout: 5000 });
    await page.click("[data-del-confirm]");
    await page.waitForSelector(".modal.blocked", { timeout: 10000 });
    const blockedText = await page.textContent(".modal.blocked .modal-body");
    if (!/season/i.test(blockedText) || !/team/i.test(blockedText)) {
      throw new Error(`[${viewport.label}] blocked modal missing dependency breakdown: ${blockedText}`);
    }
    if (!(await getJson(`/api/setup/hierarchy`)).leagues.some((l) => l.id === ids.league)) {
      throw new Error(`[${viewport.label}] a blocked delete removed the league anyway`);
    }
    await page.click(".modal.blocked [data-modal-close]");
    await page.waitForSelector(".modal", { state: "detached", timeout: 10000 });

    // (2) The bare team is also high-risk. Prove: Cancel sends no request; the
    // button starts disabled; invalid text can't enable it; the exact name
    // enables it; the delete then succeeds.
    let deleteRequests = 0;
    const countDelete = (r) => {
      if (r.url() === `${base}/api/v2/setup/team/${ids.bare}/delete`) deleteRequests += 1;
    };
    page.on("request", countDelete);
    // Cancel path: open, then Cancel — no request must fire.
    await page.click(`[data-del="team"][data-del-id="${ids.bare}"]`);
    await page.waitForSelector(".modal.danger [data-del-confirm]", { timeout: 10000 });
    await page.click(".modal [data-modal-close]");
    await page.waitForSelector(".modal", { state: "detached", timeout: 10000 });
    if (deleteRequests !== 0) {
      throw new Error(`[${viewport.label}] Cancel sent a delete request`);
    }
    // Delete path with the typed-confirm gate.
    await page.click(`[data-del="team"][data-del-id="${ids.bare}"]`);
    await page.waitForSelector(".modal.danger [data-del-confirm]", { timeout: 10000 });
    if (!(await page.$eval("[data-del-confirm]", (b) => b.disabled))) {
      throw new Error(`[${viewport.label}] team delete was enabled before typing DELETE`);
    }
    await page.fill("#del-confirm", "wrong name");
    if (!(await page.$eval("[data-del-confirm]", (b) => b.disabled))) {
      throw new Error(`[${viewport.label}] team delete enabled on invalid confirmation text`);
    }
    await page.fill("#del-confirm", "DELETE");
    await page.waitForFunction(
      () => !document.querySelector("[data-del-confirm]").disabled, null, { timeout: 5000 });
    const delResp = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/team/${ids.bare}/delete` && r.request().method() === "POST");
    await page.click("[data-del-confirm]");
    if ((await delResp).status() !== 200) {
      throw new Error(`[${viewport.label}] team delete returned non-200`);
    }
    await page.waitForSelector(".modal", { state: "detached", timeout: 10000 });
    page.off("request", countDelete);
    const teamsAfter = await getJson(`/api/setup/leagues/${ids.league}/teams`);
    if (teamsAfter.teams.some((t) => t.id === ids.bare)) {
      throw new Error(`[${viewport.label}] bare team was not deleted`);
    }
    if (!teamsAfter.teams.some((t) => t.id === ids.team)) {
      throw new Error(`[${viewport.label}] delete removed the wrong team`);
    }

    // (3) Remove from season: the redesigned action deactivates the
    // registration (and reads "Remove from season", not the old "Remove"). The
    // demo seed carries its own registrations, so target OUR registration by id
    // rather than the first Remove button on the page.
    const before = await getJson(`/api/setup/seasons/${ids.season}/team-registrations`);
    const reg = before.registrations.find((r) => r.team_id === ids.team && r.active);
    if (!reg) throw new Error(`[${viewport.label}] the Del Team registration is missing`);
    const removeBtn = await page.waitForSelector(
      `[data-reg-remove="${reg.id}"]`, { timeout: 10000 });
    // The action is now an icon button (#215): its intent lives in the
    // tooltip/accessible label rather than visible text.
    if (!/Remove from season/.test(await removeBtn.getAttribute("title"))
        || !/Remove/.test(await removeBtn.getAttribute("aria-label"))) {
      throw new Error(`[${viewport.label}] the season-registration icon action lacks its label`);
    }
    const rmResp = page.waitForResponse((r) =>
      r.url().endsWith(`/season-team-registration/${reg.id}/remove`)
      && r.request().method() === "POST");
    await removeBtn.click();
    await rmResp;
    const regs = await getJson(`/api/setup/seasons/${ids.season}/team-registrations`);
    if (regs.registrations.some((r) => r.team_id === ids.team && r.active)) {
      throw new Error(`[${viewport.label}] the team is still active after Remove from season`);
    }

    // (4) Reset demo: the header database-icon control is visible in demo, and
    // because data exists its dropdown offers "Reset demo data", which opens a
    // confirm modal requiring RESET and atomically reseeds — our Del League is
    // gone afterward.
    const demoBtn = await page.waitForSelector("#demo-btn", { state: "visible", timeout: 10000 });
    if (!/Reset demo data/.test(await demoBtn.getAttribute("title"))) {
      throw new Error(`[${viewport.label}] the demo control is not labeled "Reset demo data"`);
    }
    await demoBtn.click();
    const resetItem = await page.waitForSelector(
      '[data-demo-action="reset"]', { state: "visible", timeout: 10000 });
    await resetItem.click();
    await page.waitForSelector(".modal.danger #demo-confirm-input", { timeout: 10000 });
    // Commit is disabled until RESET is typed exactly.
    if (!(await page.$eval("[data-demo-confirm]", (b) => b.disabled))) {
      throw new Error(`[${viewport.label}] reset was enabled before typing RESET`);
    }
    await page.fill("#demo-confirm-input", "RESET");
    await page.waitForFunction(
      () => !document.querySelector("[data-demo-confirm]").disabled, null, { timeout: 5000 });
    const resetResp = page.waitForResponse((r) =>
      r.url() === `${base}/api/demo/reset` && r.request().method() === "POST");
    await page.click("[data-demo-confirm]");
    if ((await resetResp).status() !== 200) {
      throw new Error(`[${viewport.label}] reset returned non-200`);
    }
    await page.waitForSelector(".modal", { state: "detached", timeout: 10000 });
    const afterReset = await getJson(`/api/setup/hierarchy`);
    // Match by name, not id: reset rebuilds from a fresh store whose sequential
    // id generator restarts, so the canonical demo league legitimately reuses
    // the id our "Del League" had — the name is what proves the wipe/reseed.
    if (afterReset.leagues.some((l) => l.name === "Del League")) {
      throw new Error(`[${viewport.label}] reset did not clear the created league`);
    }
    if (!afterReset.leagues.length) {
      throw new Error(`[${viewport.label}] reset did not reseed the demo league`);
    }

    // No destructive form state is persisted to browser storage (#215): the
    // confirm value and the records we acted on live only in page memory.
    const stored = await page.evaluate(() => JSON.stringify({
      local: { ...localStorage }, session: { ...sessionStorage },
    }));
    for (const marker of ["RESET", "Del League", "Bare Team"]) {
      if (stored.includes(marker)) {
        throw new Error(`[${viewport.label}] destructive form data leaked into browser storage: ${marker}`);
      }
    }

    if (errors.length) {
      throw new Error(`[${viewport.label}] console/page errors:\n${errors.join("\n")}`);
    }
    console.log(`[${viewport.label}] OK — blocked delete, clean delete, remove-from-season, reset.`);
  } catch (error) {
    throw new Error(`${error.message}\n--- demo server output ---\n${serverOutput}`);
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
    console.log("Safe destructive actions browser journey passed.");
  } catch (error) {
    console.error("Safe destructive actions browser journey FAILED.");
    console.error(error && error.message ? error.message : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
