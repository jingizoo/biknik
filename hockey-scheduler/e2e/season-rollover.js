// Season rollover browser journey (#180).
//
// At desktop and phone widths, a League Admin builds two permanent league teams
// registered in a source season, pre-registers one of them in the target season,
// then uses the Setup "Season rollover" panel to carry the source season's
// participation forward: the pre-registered team is shown as already registered
// (and never offered), the remaining team is selected, and — crucially — the
// commit stays disabled with an inline row error until that team is assigned a
// target-season division, so an unassigned selection can never be submitted and
// no write happens before a valid choice (review #216). The journey then
// commits and verifies the carried team lands in the chosen target division,
// the already-registered team is untouched, and no permanent Team is
// duplicated. Fails on any browser console/page error.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8143 },
  { label: "phone", width: 390, height: 844, port: 8144 },
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
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });

    // Build a source season with two registered permanent teams and a target
    // season with two divisions; pre-register one team in the target so the
    // rollover has both an eligible team (Bears) and an already-registered one
    // (Lions). create_team derives each team's permanent league from its
    // division; the registrations are then made through the setup routes.
    const ids = await page.evaluate(async () => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      const league = await post("/api/setup/league", { name: "Rollover League" });
      const src = await post("/api/setup/season", { league_id: league.id, name: "2026-27" });
      const dst = await post("/api/setup/season", { league_id: league.id, name: "2027-28" });
      // A third season in the same league with NO divisions — the rollover must
      // refuse it as a target (review #216, point 3).
      const empty = await post("/api/setup/season", { league_id: league.id, name: "2028-29" });
      const dSrc = await post("/api/setup/division", { season_id: src.id, name: "Source Div" });
      const dstA = await post("/api/setup/division", { season_id: dst.id, name: "Target A" });
      const dstB = await post("/api/setup/division", { season_id: dst.id, name: "Target B" });
      const club = await post("/api/setup/club", { name: "Rollover Club" });
      const lions = await post("/api/setup/team",
        { club_id: club.id, division_id: dSrc.id, name: "Lions" });
      const bears = await post("/api/setup/team",
        { club_id: club.id, division_id: dSrc.id, name: "Bears" });
      // Both teams play the source season.
      await post(`/api/setup/seasons/${src.id}/team-registrations`,
        { team_id: lions.id, division_id: dSrc.id });
      await post(`/api/setup/seasons/${src.id}/team-registrations`,
        { team_id: bears.id, division_id: dSrc.id });
      // Lions is already in the target season (Target A) — the rollover must
      // skip it, not duplicate it.
      await post(`/api/setup/seasons/${dst.id}/team-registrations`,
        { team_id: lions.id, division_id: dstA.id });
      return { league: league.id, src: src.id, dst: dst.id, empty: empty.id,
        dstA: dstA.id, dstB: dstB.id, lions: lions.id, bears: bears.id };
    });

    // Open Setup and reach the Season rollover panel.
    await page.click('.tab[data-tab="setup"]');
    await page.waitForFunction(
      () => !!document.querySelector("[data-rollover-from]"), null, { timeout: 15000 });

    // The demo seed may include other multi-season leagues, so pick ours
    // explicitly when the league selector is present.
    if (await page.$("[data-rollover-league]")) {
      await page.selectOption("[data-rollover-league]", ids.league);
    }
    await page.selectOption("[data-rollover-from]", ids.src);

    // Point a rollover at the divisionless season: the panel blocks it with a
    // clear message and offers no commit path (review #216, point 3).
    await page.selectOption("[data-rollover-to]", ids.empty);
    await page.waitForFunction(
      () => /no divisions yet/i.test(document.querySelector("#content").textContent),
      null, { timeout: 10000 });
    if (await page.$("[data-rollover-commit]")) {
      throw new Error(`[${viewport.label}] a divisionless target season still offered a commit control`);
    }

    await page.selectOption("[data-rollover-to]", ids.dst);

    // Preview: Bears is eligible (a pick checkbox), Lions is not offered (it's
    // already registered in the target season).
    await page.waitForFunction(
      (b) => !!document.querySelector(`[data-rollover-pick="${b}"]`),
      ids.bears, { timeout: 15000 });
    if (await page.$(`[data-rollover-pick="${ids.lions}"]`)) {
      throw new Error(`[${viewport.label}] Lions was offered for rollover despite already being registered`);
    }

    // Commit is disabled before any team is chosen (target has two divisions,
    // so nothing is preselected).
    if (!(await page.$eval("[data-rollover-commit]", (b) => b.disabled))) {
      throw new Error(`[${viewport.label}] commit was enabled with nothing selected`);
    }

    // Check Bears but leave its division unassigned: the row shows an inline
    // error and commit stays disabled — an unassigned team cannot be submitted
    // (review #216).
    await page.check(`[data-rollover-pick="${ids.bears}"]`);
    await page.waitForFunction((b) => {
      const row = document.querySelector(`[data-rollover-pick="${b}"]`).closest(".reg-row");
      const err = row.querySelector(".ro-row-err");
      const commit = document.querySelector("[data-rollover-commit]");
      return err && !err.hidden && commit && commit.disabled;
    }, ids.bears, { timeout: 10000 });

    // Zero writes so far: the target season still holds only the pre-registered
    // Lions. (No rollover request could have been sent from the disabled path.)
    const before = await page.evaluate(async (i) => {
      const get = async (p) => (await fetch(p, { credentials: "same-origin" })).json();
      const regs = (await get(`/api/setup/seasons/${i.dst}/team-registrations`)).registrations;
      return regs.filter((r) => r.active).length;
    }, ids);
    if (before !== 1) {
      throw new Error(`[${viewport.label}] target season was written before a valid commit: ${before} active regs`);
    }

    // Assign Bears to Target B — the inline error clears and commit enables.
    await page.selectOption(`[data-rollover-div="${ids.bears}"]`, ids.dstB);
    await page.waitForFunction(() => {
      const commit = document.querySelector("[data-rollover-commit]");
      return commit && !commit.disabled;
    }, null, { timeout: 10000 });

    const resp = page.waitForResponse((r) =>
      r.url() === `${base}/api/setup/seasons/${ids.dst}/roll-forward`
      && r.request().method() === "POST");
    await page.click("[data-rollover-commit]");
    if ((await resp).status() !== 200) {
      throw new Error(`[${viewport.label}] roll-forward returned non-200`);
    }
    await page.getByRole("heading", { name: "Rollover complete" }).waitFor();

    // Verify the resulting target-season registrations: Bears now active in
    // Target B, Lions still active in Target A (untouched), and the permanent
    // Team count in the league is unchanged (no team was copied).
    const state = await page.evaluate(async (i) => {
      const get = async (p) => (await fetch(p, { credentials: "same-origin" })).json();
      const regs = (await get(`/api/setup/seasons/${i.dst}/team-registrations`)).registrations;
      const teams = (await get(`/api/setup/leagues/${i.league}/teams`)).teams;
      const active = regs.filter((r) => r.active);
      const bears = active.find((r) => r.team_id === i.bears);
      const lions = active.find((r) => r.team_id === i.lions);
      return {
        teamCount: teams.length,
        bearsDiv: bears && bears.division_id,
        lionsDiv: lions && lions.division_id,
        activeCount: active.length,
      };
    }, ids);
    if (state.teamCount !== 2) {
      throw new Error(`[${viewport.label}] expected 2 permanent teams, got ${state.teamCount}`);
    }
    if (state.bearsDiv !== ids.dstB) {
      throw new Error(`[${viewport.label}] Bears not carried into Target B: ${JSON.stringify(state)}`);
    }
    if (state.lionsDiv !== ids.dstA) {
      throw new Error(`[${viewport.label}] Lions' existing registration was altered: ${JSON.stringify(state)}`);
    }
    if (state.activeCount !== 2) {
      throw new Error(`[${viewport.label}] expected 2 active target registrations, got ${state.activeCount}`);
    }

    if (errors.length) {
      throw new Error(`[${viewport.label}] console/page errors:\n${errors.join("\n")}`);
    }
    console.log(`[${viewport.label}] OK — eligible team carried forward, already-registered team skipped.`);
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
    console.log("Season rollover browser journey passed.");
  } catch (error) {
    console.error("Season rollover browser journey FAILED.");
    console.error(error && error.message ? error.message : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
