// Season participation browser journey (#180 PR D, cut to v2 canonical #233
// Slice B2b).
//
// At desktop and phone widths, a League Admin builds a permanent program team,
// registers it for two different seasons — each under its own grouping League
// and division — through the Setup "Season participation" panel, confirms
// exactly one permanent Team backs two registrations, then removes it from one
// season and confirms the other season's registration is untouched. Fails on
// any browser console/page error.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8141 },
  { label: "phone", width: 390, height: 844, port: 8142 },
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
  // Named (not anonymous) so the partial-failure scenario below can detach it
  // for the one deliberately-failing request — Chromium logs a benign
  // "Failed to load resource: 500" console entry for any non-2xx response,
  // which isn't a real page bug and shouldn't fail the journey.
  const consoleErrorHandler = (m) => { if (m.type() === "error") errors.push(`[console] ${m.text()}`); };
  page.on("console", consoleErrorHandler);

  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });

    // Build a permanent program team + two seasons, each with its own grouping
    // League and division, through the canonical v2 API (demo default is
    // League Admin) — the same records an operator would type in. A v2
    // registration's League is REQUIRED, so each season needs one before the
    // panel can register anything into it (#233 Slice C2).
    const ids = await page.evaluate(async () => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      const program = await post("/api/v2/setup/program", { name: "Participation Program" });
      const s1 = await post("/api/v2/setup/season", { program_id: program.id, name: "2026-27" });
      const s2 = await post("/api/v2/setup/season", { program_id: program.id, name: "2027-28" });
      const lg1 = await post("/api/v2/setup/league", { season_id: s1.id, name: "Diamond" });
      const lg2 = await post("/api/v2/setup/league", { season_id: s2.id, name: "Diamond" });
      const dA = await post("/api/v2/setup/division", { league_id: lg1.id, name: "Division A" });
      const dB = await post("/api/v2/setup/division", { league_id: lg2.id, name: "Division B" });
      const club = await post("/api/v2/setup/club", { name: "Participation Club" });
      const team = await post("/api/v2/setup/team",
        { program_id: program.id, club_id: club.id, name: "Perma Lions" });
      return { program: program.id, s1: s1.id, s2: s2.id, lg1: lg1.id, lg2: lg2.id,
        dA: dA.id, dB: dB.id, team: team.id };
    }, );

    // Open Setup → Season participation reflects the fresh, empty seasons.
    // (Navigating to the tab re-renders and re-fetches the registration data;
    // no full reload, which would drop the signed-in session.) The register
    // control lives per League, keyed by the League's id.
    await page.click('.tab[data-tab="setup"]');
    await page.waitForFunction(
      (sel) => !!document.querySelector(sel), `#reg-team-${ids.lg1}`, { timeout: 15000 });

    // Register the permanent team for season 1 / League 1 / Division A. The
    // League select already defaults to lg1 (the section it's under).
    await page.selectOption(`#reg-team-${ids.lg1}`, ids.team);
    await page.selectOption(`#reg-div-add-${ids.lg1}`, ids.dA);
    let resp = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/seasons/${ids.s1}/team-registrations`
      && r.request().method() === "POST");
    await page.click(`[data-reg-add="${ids.lg1}"]`);
    if ((await resp).status() !== 200) throw new Error(`[${viewport.label}] register s1 failed`);

    // Register the SAME team for season 2 / League 2 / Division B. The team is
    // still available for s2 (registering it for s1 doesn't touch s2), so its
    // s2 register control is present.
    await page.waitForFunction(
      (sel) => !!document.querySelector(sel), `#reg-team-${ids.lg2}`, { timeout: 15000 });
    await page.selectOption(`#reg-team-${ids.lg2}`, ids.team);
    await page.selectOption(`#reg-div-add-${ids.lg2}`, ids.dB);
    resp = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/seasons/${ids.s2}/team-registrations`
      && r.request().method() === "POST");
    await page.click(`[data-reg-add="${ids.lg2}"]`);
    if ((await resp).status() !== 200) throw new Error(`[${viewport.label}] register s2 failed`);
    // Now the team is registered for BOTH seasons, so each league shows a
    // Remove control (and its register select is gone — nothing left to add).
    await page.waitForFunction(
      () => document.querySelectorAll("[data-reg-remove]").length >= 2, null, { timeout: 15000 });

    // Exactly one permanent Team backs two season registrations.
    const state = await page.evaluate(async (i) => {
      const get = async (p) => (await fetch(p, { credentials: "same-origin" })).json();
      const teams = await get(`/api/v2/setup/programs/${i.program}/teams`);
      const r1 = await get(`/api/v2/setup/seasons/${i.s1}/team-registrations`);
      const r2 = await get(`/api/v2/setup/seasons/${i.s2}/team-registrations`);
      return { teamCount: teams.teams.length,
               r1: r1.registrations.filter((r) => r.active).length,
               r2: r2.registrations.filter((r) => r.active).length };
    }, ids);
    if (state.teamCount !== 1 || state.r1 !== 1 || state.r2 !== 1) {
      throw new Error(`[${viewport.label}] expected 1 team + 2 registrations, got ${JSON.stringify(state)}`);
    }

    // Remove the team from season 2; season 1 must be untouched. Season/league
    // blocks render in season order (s1 then s2), so the last Remove button is
    // s2's.
    const removeBtns = await page.$$("[data-reg-remove]");
    resp = page.waitForResponse((r) => r.url().includes("/remove") && r.request().method() === "POST");
    await removeBtns[removeBtns.length - 1].click();
    await resp;
    // Removed from s2 → the team is available for s2 again, so its register
    // control reappears; wait for the panel to settle on that.
    await page.waitForFunction(
      (sel) => !!document.querySelector(sel), `#reg-team-${ids.lg2}`, { timeout: 15000 });

    const after = await page.evaluate(async (i) => {
      const get = async (p) => (await fetch(p, { credentials: "same-origin" })).json();
      const r1 = await get(`/api/v2/setup/seasons/${i.s1}/team-registrations`);
      const r2 = await get(`/api/v2/setup/seasons/${i.s2}/team-registrations`);
      const teams = await get(`/api/v2/setup/programs/${i.program}/teams`);
      return { r1: r1.registrations.filter((r) => r.active).length,
               r2: r2.registrations.filter((r) => r.active).length,
               teamCount: teams.teams.length };
    }, ids);
    if (after.teamCount !== 1) throw new Error(`[${viewport.label}] team was deleted on removal`);
    if (after.r1 !== 1) throw new Error(`[${viewport.label}] season 1 registration was lost`);

    // --- Edit-path + repair-surface coverage (#233 B2b review) -----------
    // The journey above only exercised Register/Remove. These steps drive
    // the Save controls' full League→Division cascade (shared by both Season
    // participation and the Needs-assignment repair row via the
    // saveRegistrationPlacement() helper in app.js), plus the repair row
    // itself. New fixtures are created via raw v2 fetches (like the setup
    // above) since a raw fetch doesn't refresh the page's own in-memory `hv`/
    // `leagueDivisions` state — refreshSetup() below forces that refetch by
    // re-clicking the Setup tab (its onclick calls render() unconditionally,
    // even when already on that tab).
    const refreshSetup = async (marker) => {
      await page.click('.tab[data-tab="setup"]');
      await page.waitForFunction((m) => {
        const sel = document.querySelector(m.selector);
        return !!sel && (!m.optionValue || Array.from(sel.options).some((o) => o.value === m.optionValue));
      }, marker, { timeout: 15000 });
    };
    const toastText = () => page.$eval(
      "#toast-root .toast-msg", (el) => el.textContent).catch(() => "");
    const waitForToast = (expected) => page.waitForFunction(
      (t) => (document.querySelector("#toast-root .toast-msg") || {}).textContent === t,
      expected, { timeout: 15000 });

    // Lions (still active in season 1 under League 1 / Division A) is reused
    // for the edit-path steps. A second League ("Sapphire") with its own
    // Division ("Division C") in the SAME season gives Save somewhere real
    // to move it to.
    const edit = await page.evaluate(async (i) => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      const get = async (p) => (await fetch(p, { credentials: "same-origin" })).json();
      const lg1b = await post("/api/v2/setup/league", { season_id: i.s1, name: "Sapphire" });
      const divC = await post("/api/v2/setup/division", { league_id: lg1b.id, name: "Division C" });
      const club = await post("/api/v2/setup/club", { name: "Edit Coverage Club" });
      const bears = await post("/api/v2/setup/team",
        { program_id: i.program, club_id: club.id, name: "Perma Bears" });
      const r1 = (await get(`/api/v2/setup/seasons/${i.s1}/team-registrations`)).registrations;
      const lionsReg = r1.find((r) => r.active && r.team_id === i.team);
      return { lg1b: lg1b.id, divC: divC.id, club: club.id, bears: bears.id, lionsReg: lionsReg.id };
    }, ids);
    await refreshSetup({ selector: `#reg-league-${edit.lionsReg}`, optionValue: edit.lg1b });

    // (1) Full edit path L1/D1 → L2/D2: Save fires clear-division, then
    // assign-league, then assign-division, in that order, and the stored
    // registration lands on the new League/Division.
    let seq = [];
    const track = (req) => {
      const url = req.url();
      if (req.method() === "POST" && url.includes(`/season-team-registration/${edit.lionsReg}/`)) {
        seq.push({ url, body: req.postDataJSON() });
      }
    };
    page.on("request", track);
    await page.selectOption(`#reg-league-${edit.lionsReg}`, edit.lg1b);
    await page.selectOption(`#reg-div-${edit.lionsReg}`, edit.divC);
    await page.click(`[data-reg-save="${edit.lionsReg}"]`);
    await waitForToast("Season registration updated.");
    page.off("request", track);
    if (seq.length !== 3
        || !seq[0].url.endsWith("/assign-division") || seq[0].body.division_id !== null
        || !seq[1].url.endsWith("/assign-league") || seq[1].body.league_id !== edit.lg1b
        || !seq[2].url.endsWith("/assign-division") || seq[2].body.division_id !== edit.divC) {
      throw new Error(`[${viewport.label}] unexpected edit-path request sequence: ${JSON.stringify(seq)}`);
    }
    let stored = await page.evaluate(async (i) => {
      const get = async (p) => (await fetch(p, { credentials: "same-origin" })).json();
      const regs = (await get(`/api/v2/setup/seasons/${i.s1}/team-registrations`)).registrations;
      return regs.find((r) => r.id === i.reg);
    }, { s1: ids.s1, reg: edit.lionsReg });
    if (!stored || stored.league_id !== edit.lg1b || stored.division_id !== edit.divC) {
      throw new Error(`[${viewport.label}] edit path didn't land on League 2 / Division C: ${JSON.stringify(stored)}`);
    }

    // (2) Clear Division, keep League: Save with the League unchanged and
    // Division reset to "No division" fires exactly ONE request.
    seq = [];
    page.on("request", track);
    await page.selectOption(`#reg-div-${edit.lionsReg}`, "");
    await page.click(`[data-reg-save="${edit.lionsReg}"]`);
    await waitForToast("Season registration updated.");
    page.off("request", track);
    if (seq.length !== 1 || !seq[0].url.endsWith("/assign-division") || seq[0].body.division_id !== null) {
      throw new Error(`[${viewport.label}] clear-division Save fired unexpected requests: ${JSON.stringify(seq)}`);
    }
    stored = await page.evaluate(async (i) => {
      const get = async (p) => (await fetch(p, { credentials: "same-origin" })).json();
      const regs = (await get(`/api/v2/setup/seasons/${i.s1}/team-registrations`)).registrations;
      return regs.find((r) => r.id === i.reg);
    }, { s1: ids.s1, reg: edit.lionsReg });
    if (!stored || stored.league_id !== edit.lg1b || stored.division_id) {
      throw new Error(`[${viewport.label}] clear-division didn't keep League 2 with a null Division: ${JSON.stringify(stored)}`);
    }

    // (3) League-only registration: register a NEW team choosing a League
    // but leaving Division at "No division" — the create POST body must
    // carry division_id: null (not omitted, not "").
    await page.waitForFunction(
      (sel) => !!document.querySelector(sel), `#reg-team-${ids.lg1}`, { timeout: 15000 });
    await page.selectOption(`#reg-team-${ids.lg1}`, edit.bears);
    const addResp = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/seasons/${ids.s1}/team-registrations`
      && r.request().method() === "POST");
    await page.click(`[data-reg-add="${ids.lg1}"]`);
    const addBody = (await addResp).request().postDataJSON();
    if (addBody.division_id !== null) {
      throw new Error(`[${viewport.label}] league-only register body had division_id ${
        JSON.stringify(addBody.division_id)}, expected null`);
    }
    stored = await page.evaluate(async (i) => {
      const get = async (p) => (await fetch(p, { credentials: "same-origin" })).json();
      const regs = (await get(`/api/v2/setup/seasons/${i.s1}/team-registrations`)).registrations;
      return regs.find((r) => r.active && r.team_id === i.bears);
    }, { s1: ids.s1, bears: edit.bears });
    if (!stored || stored.division_id !== null) {
      throw new Error(`[${viewport.label}] league-only registration didn't store a null division_id: ${
        JSON.stringify(stored)}`);
    }

    // (5) Partial-failure toast for Save: intercept the FINAL assign-division
    // request (the one that sets a real Division, not the null-clearing one)
    // and force it to fail. Lions is currently League 2 ("Sapphire") /
    // no Division; move it to League 1 ("Diamond") / Division A — the
    // League change succeeds, the Division change is intercepted and fails,
    // so the toast must clearly say the update was only partially applied
    // (matches app.js's placementSaveToast() partial-branch text exactly).
    await page.route("**/api/v2/setup/season-team-registration/*/assign-division", async (route) => {
      const body = route.request().postDataJSON();
      if (body && body.division_id) {
        await route.fulfill({
          status: 500, contentType: "application/json",
          body: JSON.stringify({ error: { code: "simulated_failure",
                                          message: "Simulated division-assign failure." } }),
        });
      } else {
        await route.continue();
      }
    });
    await page.waitForFunction(
      (sel) => !!document.querySelector(sel), `#reg-league-${edit.lionsReg}`, { timeout: 15000 });
    await page.selectOption(`#reg-league-${edit.lionsReg}`, ids.lg1);
    await page.selectOption(`#reg-div-${edit.lionsReg}`, ids.dA);
    // Detach the console-error listener only for this deliberately-failing
    // request: Chromium logs a benign "Failed to load resource: 500" entry
    // for the simulated failure above, which isn't a real page bug.
    page.off("console", consoleErrorHandler);
    await page.click(`[data-reg-save="${edit.lionsReg}"]`);
    const expectedPartialToast = "Partially saved — an earlier change was applied, but a later "
      + "step failed (Simulated division-assign failure.). Please retry to finish.";
    await waitForToast(expectedPartialToast);
    page.on("console", consoleErrorHandler);
    const actualToast = await toastText();
    if (actualToast !== expectedPartialToast) {
      throw new Error(`[${viewport.label}] partial-failure toast mismatch: ${actualToast}`);
    }
    await page.unroute("**/api/v2/setup/season-team-registration/*/assign-division");
    stored = await page.evaluate(async (i) => {
      const get = async (p) => (await fetch(p, { credentials: "same-origin" })).json();
      const regs = (await get(`/api/v2/setup/seasons/${i.s1}/team-registrations`)).registrations;
      return regs.find((r) => r.id === i.reg);
    }, { s1: ids.s1, reg: edit.lionsReg });
    if (!stored || stored.league_id !== ids.lg1 || stored.division_id) {
      throw new Error(`[${viewport.label}] partial-failure left unexpected state: ${JSON.stringify(stored)}`);
    }

    // (6) Repair surface: manufacture an invalid registration and use the
    // Needs-assignment repair row to fix it in place.
    //
    // registration_league_division_mismatch is NOT reachable through the
    // documented v2 write surface — every mutation path
    // (register_team_for_season, assign_season_team_league,
    // assign_season_team_division, assign_division_league,
    // roll_forward_registrations_v2) explicitly cross-validates League vs.
    // Division and rejects a mismatch before any write. So this fixture
    // manufactures registration_league_not_in_season instead: a League can
    // be deleted while it still owns a division-less registration —
    // delete_league's dependent check only blocks on live Divisions, never
    // on a registration parked directly under the League with no Division —
    // leaving that registration's league_id pointing at nothing. Verified
    // directly against the running service before writing this assertion.
    const orphan = await page.evaluate(async (i) => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      const lgOrphan = await post("/api/v2/setup/league", { season_id: i.s1, name: "Bronze" });
      const foxes = await post("/api/v2/setup/team",
        { program_id: i.program, club_id: i.club, name: "Perma Foxes" });
      const reg = await post(`/api/v2/setup/seasons/${i.s1}/team-registrations`,
        { team_id: foxes.id, league_id: lgOrphan.id, division_id: null });
      const del = await fetch(`/api/v2/setup/league/${lgOrphan.id}/delete`, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: "{}",
      });
      const statusBefore = await (await fetch("/api/v2/onboarding/status",
        { credentials: "same-origin" })).json();
      return { foxes: foxes.id, reg: reg.id, deleteStatus: del.status,
        hadInvalidRegBlocker: (statusBefore.blocking || []).some((b) => b.code === "invalid_registrations") };
    }, { s1: ids.s1, program: ids.program, club: edit.club });
    if (orphan.deleteStatus !== 200) {
      throw new Error(`[${viewport.label}] could not manufacture registration_league_not_in_season — `
        + `deleting a league with a division-less registration under it returned ${orphan.deleteStatus} `
        + `(expected 200); the repair-surface fixture needs a different reachable invalid state`);
    }
    if (!orphan.hadInvalidRegBlocker) {
      throw new Error(`[${viewport.label}] manufactured invalid registration didn't trip the `
        + `invalid_registrations onboarding blocker`);
    }
    await refreshSetup({ selector: `[data-repair-league-for="${orphan.reg}"]` });
    const repairRowText = await page.evaluate((rid) => {
      const btn = document.querySelector(`[data-repair-save="${rid}"]`);
      const row = btn && btn.closest(".repair-row");
      return row ? row.textContent : "";
    }, orphan.reg);
    if (!/isn't in this season/i.test(repairRowText)) {
      throw new Error(`[${viewport.label}] repair row missing its diagnostic reason: ${repairRowText}`);
    }
    await page.selectOption(`#repair-league-${orphan.reg}`, ids.lg1);
    await page.selectOption(`#repair-div-${orphan.reg}`, ids.dA);
    await page.click(`[data-repair-save="${orphan.reg}"]`);
    await waitForToast("Registration repaired — moved into the selected league/division.");
    if (await page.$(`[data-repair-save="${orphan.reg}"]`)) {
      throw new Error(`[${viewport.label}] repaired registration still shows a Needs-assignment row`);
    }
    const repaired = await page.evaluate(async (i) => {
      const get = async (p) => (await fetch(p, { credentials: "same-origin" })).json();
      const regs = (await get(`/api/v2/setup/seasons/${i.s1}/team-registrations`)).registrations;
      const reg = regs.find((r) => r.id === i.reg);
      const status = await (await fetch("/api/v2/onboarding/status", { credentials: "same-origin" })).json();
      return { league_id: reg && reg.league_id, division_id: reg && reg.division_id,
        hasInvalidRegBlocker: (status.blocking || []).some((b) => b.code === "invalid_registrations") };
    }, { s1: ids.s1, reg: orphan.reg });
    if (repaired.league_id !== ids.lg1 || repaired.division_id !== ids.dA) {
      throw new Error(`[${viewport.label}] repair didn't land in League 1 / Division A: ${JSON.stringify(repaired)}`);
    }
    if (repaired.hasInvalidRegBlocker) {
      throw new Error(`[${viewport.label}] invalid_registrations onboarding blocker didn't clear after repair`);
    }

    if (errors.length) {
      throw new Error(`[${viewport.label}] console/page errors:\n${errors.join("\n")}`);
    }
    console.log(`[${viewport.label}] OK — one permanent team, two seasons, safe removal.`);
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
    console.log("Season participation browser journey passed.");
  } catch (error) {
    console.error("Season participation browser journey FAILED.");
    console.error(error && error.message ? error.message : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
