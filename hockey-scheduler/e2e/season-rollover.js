// Season rollover browser journey (#180, cut to v2 canonical #233 Slice B2b).
//
// At desktop and phone widths, a League Admin builds two permanent program
// teams registered in a source season, pre-registers one of them in the
// target season, then uses the Setup "Season rollover" panel to carry the
// source season's participation forward: the pre-registered team is shown as
// already registered (and never offered), the remaining team is selected, and
// — crucially — the commit stays disabled with an inline row error until that
// team is assigned a target-season League (Division stays optional in v2), so
// an unassigned selection can never be submitted and no write happens before a
// valid choice (review #216, re-scoped to League for v2). The journey then
// commits and verifies the carried team lands in the chosen target
// league/division, the already-registered team is untouched, and no permanent
// Team is duplicated. Fails on any browser console/page error.
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

    // Build three permanent teams, each under its OWN permanent League (#283
    // Slice E rules 2, 3, 7): a Team is created with a REQUIRED league_id and
    // may only ever register into — and be rolled into — that same permanent
    // League. Lions→lgA, Bears→lgB, Wolves→lgC. All three play the source
    // season (registered into their own League). Lions is ALSO pre-registered
    // in the target (under its own permanent League) so the rollover has both
    // an eligible team (Bears) and an already-registered one (Lions, skipped).
    //
    // lgA/lgB are created against the TARGET season first (and given a target
    // Division while each is still bound to a single season — create_division
    // under a League that spans seasons is ambiguous), so Bears' permanent
    // League already participates in the target with a Division to carry into.
    // lgC is created against the SOURCE season ONLY, so Wolves' permanent
    // League does NOT yet participate in the target — its rollover row offers
    // just "No division", and the commit binds lgC's target LeagueSeason on
    // the fly (covers a carried selection whose stored division_id is null).
    // A fourth, league-less season lets the panel refuse a target with no
    // Leagues (review #216, point 3 — re-scoped to League).
    const ids = await page.evaluate(async () => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      const program = await post("/api/v2/setup/program", { name: "Rollover Program" });
      const src = await post("/api/v2/setup/season", { program_id: program.id, name: "2026-27" });
      const dst = await post("/api/v2/setup/season", { program_id: program.id, name: "2027-28" });
      // A fourth season in the same program with NO leagues — the rollover must
      // refuse it as a target (review #216, point 3 — re-scoped to League).
      const empty = await post("/api/v2/setup/season", { program_id: program.id, name: "2028-29" });
      // Permanent Leagues. lgA/lgB bound to the target first so each can get a
      // target Division before it also spans the source season (below).
      const lgA = await post("/api/v2/setup/league", { season_id: dst.id, name: "Adult League" });
      const dstA = await post("/api/v2/setup/division", { league_id: lgA.id, name: "Gold" });
      const lgB = await post("/api/v2/setup/league", { season_id: dst.id, name: "Junior League" });
      const dstB = await post("/api/v2/setup/division", { league_id: lgB.id, name: "Silver" });
      // lgC lives in the SOURCE season only — no target participation yet.
      const lgC = await post("/api/v2/setup/league", { season_id: src.id, name: "Youth League" });
      const club = await post("/api/v2/setup/club", { name: "Rollover Club" });
      // Each Team is created under its permanent League (league_id REQUIRED).
      const lions = await post("/api/v2/setup/team",
        { league_id: lgA.id, club_id: club.id, name: "Lions" });
      const bears = await post("/api/v2/setup/team",
        { league_id: lgB.id, club_id: club.id, name: "Bears" });
      // Carried in a SEPARATE second rollover round below with only its
      // (fixed, permanent) League and Division left at "No division" — covers
      // a rollover selection whose stored division_id is actually null.
      const wolves = await post("/api/v2/setup/team",
        { league_id: lgC.id, club_id: club.id, name: "Wolves" });
      // All three teams play the source season, each into its OWN permanent
      // League (rule 7). Registering binds lgA/lgB to the source season too;
      // the source Division is left null (not asserted below).
      await post(`/api/v2/setup/seasons/${src.id}/team-registrations`,
        { team_id: lions.id, league_id: lgA.id, division_id: null });
      await post(`/api/v2/setup/seasons/${src.id}/team-registrations`,
        { team_id: bears.id, league_id: lgB.id, division_id: null });
      await post(`/api/v2/setup/seasons/${src.id}/team-registrations`,
        { team_id: wolves.id, league_id: lgC.id, division_id: null });
      // Lions is already in the target season under its OWN permanent League
      // (lgA + Gold) — the rollover must skip it, not duplicate it.
      await post(`/api/v2/setup/seasons/${dst.id}/team-registrations`,
        { team_id: lions.id, league_id: lgA.id, division_id: dstA.id });
      return { program: program.id, src: src.id, dst: dst.id, empty: empty.id,
        lgA: lgA.id, lgB: lgB.id, lgC: lgC.id,
        dstA: dstA.id, dstB: dstB.id, lions: lions.id, bears: bears.id, wolves: wolves.id };
    });

    // Open Setup and reach the Season rollover panel.
    await page.click('.tab[data-tab="setup"]');
    await page.waitForFunction(
      () => !!document.querySelector("[data-rollover-from]"), null, { timeout: 15000 });

    // The demo seed may include other multi-season programs, so pick ours
    // explicitly when the program selector is present.
    if (await page.$("[data-rollover-program]")) {
      await page.selectOption("[data-rollover-program]", ids.program);
    }
    await page.selectOption("[data-rollover-from]", ids.src);

    // Point a rollover at the league-less season: the panel blocks it with a
    // clear message and offers no commit path (review #216, point 3).
    await page.selectOption("[data-rollover-to]", ids.empty);
    await page.waitForFunction(
      () => /no leagues yet/i.test(document.querySelector("#content").textContent),
      null, { timeout: 10000 });
    if (await page.$("[data-rollover-commit]")) {
      throw new Error(`[${viewport.label}] a league-less target season still offered a commit control`);
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

    // #283 Slice E: a Team only ever rolls into its OWN permanent League, so
    // Bears' target-League control is FIXED to lgB (a single-option select
    // preselected to the team's permanent League — never a free picker). Assert
    // that fixed value rather than selecting a different League.
    const bearsLeagueSel = await page.$eval(`[data-rollover-league="${ids.bears}"]`,
      (sel) => ({ value: sel.value, optionValues: Array.from(sel.options).map((o) => o.value) }));
    if (bearsLeagueSel.value !== ids.lgB) {
      throw new Error(`[${viewport.label}] Bears' target League wasn't fixed to its permanent League `
        + `lgB: ${JSON.stringify(bearsLeagueSel)}`);
    }
    if (bearsLeagueSel.optionValues.filter(Boolean).some((v) => v !== ids.lgB)) {
      throw new Error(`[${viewport.label}] Bears' League select offered a League other than its `
        + `permanent one: ${JSON.stringify(bearsLeagueSel.optionValues)}`);
    }

    // Commit is disabled before any team is chosen.
    if (!(await page.$eval("[data-rollover-commit]", (b) => b.disabled))) {
      throw new Error(`[${viewport.label}] commit was enabled with nothing selected`);
    }

    // Zero writes so far: the target season still holds only the pre-registered
    // Lions — merely opening the preview writes nothing.
    const beforeCheck = await page.evaluate(async (i) => {
      const get = async (p) => (await fetch(p, { credentials: "same-origin" })).json();
      const regs = (await get(`/api/v2/setup/seasons/${i.dst}/team-registrations`)).registrations;
      return regs.filter((r) => r.active).length;
    }, ids);
    if (beforeCheck !== 1) {
      throw new Error(`[${viewport.label}] target season was written before any commit: ${beforeCheck} active regs`);
    }

    // Check Bears: its League is already fixed to lgB, so the commit gate is
    // satisfied immediately (no unassigned-League row error to clear) and the
    // button enables even with Division left at its optional "No division"
    // default.
    await page.check(`[data-rollover-pick="${ids.bears}"]`);
    await page.waitForFunction(() => {
      const commit = document.querySelector("[data-rollover-commit]");
      return commit && !commit.disabled;
    }, null, { timeout: 10000 });
    // The inline "no permanent league" row error stays hidden — Bears HAS one.
    const bearsRowErrHidden = await page.evaluate((b) => {
      const row = document.querySelector(`[data-rollover-pick="${b}"]`).closest(".reg-row");
      const err = row.querySelector(".ro-row-err");
      return !err || err.hidden;
    }, ids.bears);
    if (!bearsRowErrHidden) {
      throw new Error(`[${viewport.label}] Bears (with a permanent League) showed a row error`);
    }

    // Checking a box still writes nothing before the explicit commit.
    const before = await page.evaluate(async (i) => {
      const get = async (p) => (await fetch(p, { credentials: "same-origin" })).json();
      const regs = (await get(`/api/v2/setup/seasons/${i.dst}/team-registrations`)).registrations;
      return regs.filter((r) => r.active).length;
    }, ids);
    if (before !== 1) {
      throw new Error(`[${viewport.label}] target season was written before a valid commit: ${before} active regs`);
    }

    // Bears' permanent League (lgB) already participates in the target with a
    // Division (Silver), so pick it — commit stays enabled (Division optional).
    await page.selectOption(`[data-rollover-div="${ids.bears}"]`, ids.dstB);
    if (await page.$eval("[data-rollover-commit]", (b) => b.disabled)) {
      throw new Error(`[${viewport.label}] commit disabled after picking an optional division`);
    }

    const resp = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/seasons/${ids.dst}/roll-forward`
      && r.request().method() === "POST");
    await page.click("[data-rollover-commit]");
    if ((await resp).status() !== 200) {
      throw new Error(`[${viewport.label}] roll-forward returned non-200`);
    }
    await page.getByRole("heading", { name: "Rollover complete" }).waitFor();

    // Verify the resulting target-season registrations: Bears now active under
    // the target League in Target B, Lions still active in Target A
    // (untouched), and the permanent Team count in the program is unchanged
    // (no team was copied — Wolves is the 3rd permanent team, created above
    // for the second rollover round below, not carried yet).
    const state = await page.evaluate(async (i) => {
      const get = async (p) => (await fetch(p, { credentials: "same-origin" })).json();
      const regs = (await get(`/api/v2/setup/seasons/${i.dst}/team-registrations`)).registrations;
      const teams = (await get(`/api/v2/setup/programs/${i.program}/teams`)).teams;
      const active = regs.filter((r) => r.active);
      const bears = active.find((r) => r.team_id === i.bears);
      const lions = active.find((r) => r.team_id === i.lions);
      return {
        teamCount: teams.length,
        bearsLeague: bears && bears.league_id,
        bearsDiv: bears && bears.division_id,
        lionsDiv: lions && lions.division_id,
        activeCount: active.length,
      };
    }, ids);
    if (state.teamCount !== 3) {
      throw new Error(`[${viewport.label}] expected 3 permanent teams, got ${state.teamCount}`);
    }
    if (state.bearsLeague !== ids.lgB) {
      throw new Error(`[${viewport.label}] Bears not carried into its permanent League: ${JSON.stringify(state)}`);
    }
    if (state.bearsDiv !== ids.dstB) {
      throw new Error(`[${viewport.label}] Bears not carried into Silver: ${JSON.stringify(state)}`);
    }
    if (state.lionsDiv !== ids.dstA) {
      throw new Error(`[${viewport.label}] Lions' existing registration was altered: ${JSON.stringify(state)}`);
    }
    if (state.activeCount !== 2) {
      throw new Error(`[${viewport.label}] expected 2 active target registrations, got ${state.activeCount}`);
    }

    // (4) Rollover with division_id: null — a SEPARATE second round carries
    // Wolves (still eligible: registered in the source season, not yet in
    // the target) with only a target League chosen; its Division select is
    // left at the default "No division". The picker persists alongside the
    // "Rollover complete" banner from the first commit above, so no reset
    // step is needed before checking Wolves.
    await page.waitForFunction(
      (w) => !!document.querySelector(`[data-rollover-pick="${w}"]`),
      ids.wolves, { timeout: 15000 });
    await page.check(`[data-rollover-pick="${ids.wolves}"]`);
    // Wolves' target League is FIXED to its permanent League (lgC), which does
    // NOT yet participate in the target season — so the row offers only "No
    // division" and the commit binds lgC's target LeagueSeason on the fly.
    const wolvesLeagueSel = await page.$eval(`[data-rollover-league="${ids.wolves}"]`,
      (sel) => sel.value);
    if (wolvesLeagueSel !== ids.lgC) {
      throw new Error(`[${viewport.label}] Wolves' target League wasn't fixed to lgC: ${wolvesLeagueSel}`);
    }
    // Division select is untouched — stays at its "No division" default.
    await page.waitForFunction(() => {
      const commit = document.querySelector("[data-rollover-commit]");
      return commit && !commit.disabled;
    }, null, { timeout: 10000 });
    const resp2 = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/seasons/${ids.dst}/roll-forward`
      && r.request().method() === "POST");
    await page.click("[data-rollover-commit]");
    const resp2Req = (await resp2).request();
    if (resp2Req.postDataJSON) {
      const body2 = resp2Req.postDataJSON();
      const wolvesSel = (body2.selections || []).find((s) => s.team_id === ids.wolves);
      if (!wolvesSel || wolvesSel.division_id !== null) {
        throw new Error(`[${viewport.label}] wolves roll-forward selection division_id: ${JSON.stringify(wolvesSel)}`);
      }
    }
    const wolvesStored = await page.evaluate(async (i) => {
      const get = async (p) => (await fetch(p, { credentials: "same-origin" })).json();
      const regs = (await get(`/api/v2/setup/seasons/${i.dst}/team-registrations`)).registrations;
      return regs.find((r) => r.active && r.team_id === i.wolves);
    }, ids);
    if (!wolvesStored || wolvesStored.division_id !== null || !("division_id" in wolvesStored)) {
      throw new Error(`[${viewport.label}] wolves stored registration division_id wasn't null: ${
        JSON.stringify(wolvesStored)}`);
    }

    if (errors.length) {
      throw new Error(`[${viewport.label}] console/page errors:\n${errors.join("\n")}`);
    }
    console.log(`[${viewport.label}] OK — eligible team carried forward, already-registered team skipped, `
      + `league-only rollover stored a null division_id.`);
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
