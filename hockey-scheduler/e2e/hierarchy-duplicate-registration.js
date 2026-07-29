// Hierarchy participation same-target duplicate browser journey (#331
// review round 19 finding 4 bullet 1; strengthened by round 20 finding 2).
//
// A Team can end up with TWO ACTIVE registrations at the exact same
// (team, league) target: the Memory-only corruption #331 review round 19
// finding 1 closes off at every live write path. SQL's ux_team_league_season
// unique index (migration 035) makes it structurally unconstructible on
// SQLite/PostgreSQL, and finding 1's own fix means no live write sequence
// can build it on Memory either any more — it is reachable only as
// legacy/pre-existing data via direct store injection, exactly the
// convention this review's own backend suite uses
// (test_exact_registration_identity.py).
//
// That also means it CANNOT be represented in a durable SQLite file (the
// separate-injection-process technique this suite's other files use for
// legacy-data reproductions — see season-participation.js/season-rollover.js
// — relies on a real SQL database, which enforces the very index that rules
// this state out). This file instead runs the demo server IN-MEMORY (no
// DATABASE_URL, same clean-slate admin-only boot every other journey here
// gets from DemoState.__init__) via a small Python launcher that starts
// hockey_scheduler.web.server's own public serve() in a background thread,
// then injects a second active SeasonTeamRegistration directly into that
// SAME live store on demand — the identical store.add_season_team_registration
// call the rest of this review's injection helpers use, just reachable
// in-process instead of via a shared file, since Memory has no file to share.
//
// Proves the Setup hierarchy Season-participation tree renders both rows as
// independently addressable controls with DISTINCT accessible names (never
// regByTeamLeague's pre-fix last-wins collapse, where two rows appeared but
// BOTH resolved to the same winning registration id and the same aria-label)
// and that EITHER one is genuinely removable via REAL keyboard navigation —
// in both directions (Tab forward AND Shift+Tab back reach the same control)
// and both native activation keys (Enter in one order, Space in the other) —
// never a JS-forced `.focus()` on the Remove button itself, which could
// "activate" a control that isn't actually reachable in natural tab order —
// while the other row remains untouched, in both selection orders. Fails on
// any browser console/page error.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8321 },
  { label: "phone", width: 390, height: 844, port: 8322 },
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

// Starts the real hockey_scheduler.web.server HTTP server (its own public
// serve(), completely unmodified) in a background thread against the
// DemoState default in-memory store, then waits for "team_id,league_id,
// season_id" lines on stdin and injects one active SeasonTeamRegistration
// per line directly into that SAME live store -- store is InMemoryStore, so
// this only ever runs between distinct page actions this file itself
// sequences (never concurrently with a live request), matching the no-write-
// contention guarantee the SQL-file injection helpers document elsewhere.
const LAUNCHER_SCRIPT = `
import sys
import threading
from hockey_scheduler.web import server as srv
from hockey_scheduler.domain import SeasonTeamRegistration

threading.Thread(target=srv.serve, args=("${HOST}", PORT_PLACEHOLDER), daemon=True).start()

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    team_id, league_id, season_id = line.split(",")
    store = srv.STATE.api.store
    ls = store.league_season_for(league_id, season_id)
    reg_id = store.next_id("streg")
    store.add_season_team_registration(SeasonTeamRegistration(
        id=reg_id, league_season_id=ls.id, team_id=team_id,
        division_id=None, active=True))
    print("INJECTED:" + reg_id, flush=True)
`;

function startInjectableServer(port) {
  const script = LAUNCHER_SCRIPT.replace("PORT_PLACEHOLDER", String(port));
  const env = { ...process.env };
  delete env.DATABASE_URL;
  const proc = spawn(process.env.PYTHON || "python3", ["-u", "-c", script],
    { cwd: BACKEND_DIR, env, stdio: ["pipe", "pipe", "pipe"] });
  let serverOutput = "";
  let buffer = "";
  const pending = [];
  proc.stdout.on("data", (d) => {
    serverOutput += d.toString();
    buffer += d.toString();
    let idx;
    while ((idx = buffer.indexOf("\n")) !== -1) {
      const line = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 1);
      if (line.startsWith("INJECTED:") && pending.length) {
        pending.shift().resolve(line.slice("INJECTED:".length));
      }
    }
  });
  proc.stderr.on("data", (d) => { serverOutput += d.toString(); });
  proc.on("exit", () => {
    while (pending.length) {
      pending.shift().reject(new Error(`launcher process exited unexpectedly; output:\n${serverOutput}`));
    }
  });
  const inject = (teamId, leagueId, seasonId) => new Promise((resolve, reject) => {
    pending.push({ resolve, reject });
    proc.stdin.write(`${teamId},${leagueId},${seasonId}\n`);
  });
  return { proc, inject, getOutput: () => serverOutput };
}

// Selects the Hierarchy sub-view via the user-visible toggle and confirms it
// took effect (#345 batch 2: Setup now lands on the six-workflow hub, so a
// journey asserting against Hierarchy controls has to navigate there). Never
// sets setupView directly, and asserts the segment is active BEFORE any
// domain control is awaited, so a future default change fails with "wrong
// sub-view" rather than an opaque selector timeout.
async function enterSetupHierarchy(page) {
  await page.click('[data-setup-view="hierarchy"]');
  await page.waitForFunction(() => {
    const seg = document.querySelector(".setup-viewtoggle .seg.active");
    return !!(seg && seg.dataset.setupView === "hierarchy");
  }, null, { timeout: 10000 });
}

async function checkViewport(browser, viewport) {
  const base = `http://${HOST}:${viewport.port}`;
  const server = startInjectableServer(viewport.port);
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

    // Re-clicks the Setup tab (its onclick calls render() unconditionally)
    // so a raw fetch or store injection that mutated server state outside
    // this page's own writes is reflected in its in-memory hv/seasonRegs.
    const refreshSetup = async (marker) => {
      await page.click('.tab[data-tab="setup"]');
      await enterSetupHierarchy(page);
      await page.waitForFunction((sel) => !!document.querySelector(sel), marker, { timeout: 15000 });
    };

    // One independent Program/Season/League/Club/Team/registration fixture
    // per order, built entirely through genuine HTTP writes -- only the
    // SECOND registration at each fixture's exact target is ever injected
    // directly (the corrupted half no live write path can produce).
    const buildFixture = (label) => page.evaluate(async (i) => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      const program = await post("/api/v2/setup/program", { name: `Twin Program ${i.label}` });
      const season = await post("/api/v2/setup/season", { program_id: program.id, name: "2026-27" });
      const lg = await post("/api/v2/setup/league", { season_id: season.id, name: `Twin League ${i.label}` });
      const club = await post("/api/v2/setup/club", { name: `Twin Club ${i.label}` });
      // #367 prerequisite: a Team's League must belong to the ACTIVE Program.
      // This journey builds several Programs in one session, so the context
      // can still point at an earlier fixture's Program -- move to the one
      // being populated before creating into it.
      await post("/api/context",
        { program_id: program.id, season_id: season.id });
      const team = await post("/api/v2/setup/team",
        { league_id: lg.id, club_id: club.id, name: `Twin Team ${i.label}` });
      const reg1 = await post(`/api/v2/setup/seasons/${season.id}/team-registrations`,
        { team_id: team.id, league_id: lg.id, division_id: null });
      return { season: season.id, lg: lg.id, team: team.id, reg1: reg1.id };
    }, { label });

    const domSnapshot = (reg1, reg2) => page.evaluate((j) => {
      const el1 = document.querySelector(`[data-reg-remove="${j.reg1}"]`);
      const el2 = document.querySelector(`[data-reg-remove="${j.reg2}"]`);
      return {
        count1: document.querySelectorAll(`[data-reg-remove="${j.reg1}"]`).length,
        count2: document.querySelectorAll(`[data-reg-remove="${j.reg2}"]`).length,
        text1: el1 ? el1.closest(".reg-row").textContent : null,
        text2: el2 ? el2.closest(".reg-row").textContent : null,
        ariaLabel1: el1 ? el1.getAttribute("aria-label") : null,
        ariaLabel2: el2 ? el2.getAttribute("aria-label") : null,
        sameElement: !!el1 && el1 === el2,
      };
    }, { reg1, reg2 });

    const finalState = (season, reg1, reg2) => page.evaluate(async (i) => {
      const get = async (p) => (await fetch(p, { credentials: "same-origin" })).json();
      const regs = (await get(`/api/v2/setup/seasons/${i.season}/team-registrations`)).registrations;
      return { reg1: regs.find((r) => r.id === i.reg1), reg2: regs.find((r) => r.id === i.reg2) };
    }, { season, reg1, reg2 });

    // Real Tab, not .focus(): click the target row's own League select
    // (focuses it without changing its value, so no save/re-render fires),
    // then walk the native tab order forward -- League select -> Division
    // select -> Save -> Remove, per app.js's regRow template -- to that
    // row's OWN Remove control specifically. A bounded loop (rather than a
    // hardcoded Tab count) presses real Tab keys and checks after each one,
    // since Chromium's first Tab after a raw click on a <select> can be
    // consumed closing its native picker without moving focus -- still every
    // step is a genuine page.keyboard.press("Tab"), never a JS .focus().
    // Once on Remove, one more real Tab moves off it, then a real Shift+Tab
    // proves the SAME control is reachable going backward too -- both
    // directions of native tab order, not just forward. `activationKey` is
    // "Enter" or "Space" -- both are how a browser natively activates a
    // focused <button>, and the two call sites below use one each so both
    // are exercised somewhere in this file, never just one.
    const keyboardRemove = async (targetRegId, activationKey) => {
      await page.click(`#reg-league-${targetRegId}`);
      let focused = null;
      for (let i = 0; i < 6 && focused !== targetRegId; i++) {
        await page.keyboard.press("Tab");
        focused = await page.evaluate(() => {
          const el = document.activeElement;
          return el ? el.getAttribute("data-reg-remove") : null;
        });
      }
      if (focused !== targetRegId) {
        throw new Error(`[${viewport.label}] Tab navigation from the League select never reached this row's own Remove control: got ${focused}, expected ${targetRegId}`);
      }
      await page.keyboard.press("Tab");
      await page.keyboard.press("Shift+Tab");
      const refocused = await page.evaluate(() => {
        const el = document.activeElement;
        return el ? el.getAttribute("data-reg-remove") : null;
      });
      if (refocused !== targetRegId) {
        throw new Error(`[${viewport.label}] Shift+Tab back from the control after Remove didn't land on this row's own Remove control again: got ${refocused}, expected ${targetRegId}`);
      }
      const removeResp = page.waitForResponse((r) =>
        r.url().includes(`/season-team-registration/${targetRegId}/remove`) && r.request().method() === "POST");
      await page.keyboard.press(activationKey);
      if ((await removeResp).status() !== 200) {
        throw new Error(`[${viewport.label}] keyboard-activated (${activationKey}) Remove failed for ${targetRegId}`);
      }
    };

    // Order A: keyboard-remove the FIRST-created (real, API-registered) row,
    // proving the SECOND-created (injected) row survives untouched.
    const a = await buildFixture("A");
    const aReg2 = await server.inject(a.team, a.lg, a.season);
    await refreshSetup(`[data-reg-remove="${aReg2}"]`);
    const aDom = await domSnapshot(a.reg1, aReg2);
    if (aDom.count1 !== 1 || aDom.count2 !== 1 || aDom.sameElement) {
      throw new Error(`[${viewport.label}] (order A) same-target duplicate rows not both independently reachable -- exactly one distinct control each expected: ${JSON.stringify(aDom)}`);
    }
    if (!aDom.text1 || !/Twin Team A/.test(aDom.text1) || !aDom.text2 || !/Twin Team A/.test(aDom.text2)) {
      throw new Error(`[${viewport.label}] (order A) same-target duplicate rows missing/wrong team name: ${JSON.stringify(aDom)}`);
    }
    // Both rows show the identical team under the identical League, so
    // without a distinguishing accessible name a screen-reader user hears
    // "Remove Twin Team A from 2026-27" twice with no way to tell them
    // apart, even though the underlying controls are independently
    // addressable (#331 review round 20 finding 2).
    if (!aDom.ariaLabel1 || !aDom.ariaLabel2 || aDom.ariaLabel1 === aDom.ariaLabel2
        || !aDom.ariaLabel1.includes(a.reg1) || !aDom.ariaLabel2.includes(aReg2)) {
      throw new Error(`[${viewport.label}] (order A) duplicate rows' Remove controls don't have distinct, self-identifying accessible names: ${JSON.stringify(aDom)}`);
    }
    await keyboardRemove(a.reg1, "Enter");
    await page.waitForFunction((i) =>
      !document.querySelector(`[data-reg-remove="${i.reg1}"]`)
      && !!document.querySelector(`[data-reg-remove="${i.reg2}"]`),
      { reg1: a.reg1, reg2: aReg2 }, { timeout: 15000 });
    const aFinal = await finalState(a.season, a.reg1, aReg2);
    if (!aFinal.reg1 || aFinal.reg1.active) {
      throw new Error(`[${viewport.label}] (order A) first-created row wasn't deactivated: ${JSON.stringify(aFinal.reg1)}`);
    }
    if (!aFinal.reg2 || !aFinal.reg2.active) {
      throw new Error(`[${viewport.label}] (order A) second-created (injected) row was deactivated by mistake: ${JSON.stringify(aFinal.reg2)}`);
    }

    // Order B: a fresh, independent team/league pair, this time keyboard-
    // removing the SECOND-created (injected) row instead -- the opposite
    // order -- proving either specific row is independently reachable,
    // never just "whichever happens to be first/last".
    const b = await buildFixture("B");
    const bReg2 = await server.inject(b.team, b.lg, b.season);
    await refreshSetup(`[data-reg-remove="${bReg2}"]`);
    const bDom = await domSnapshot(b.reg1, bReg2);
    if (bDom.count1 !== 1 || bDom.count2 !== 1 || bDom.sameElement) {
      throw new Error(`[${viewport.label}] (order B) same-target duplicate rows not both independently reachable: ${JSON.stringify(bDom)}`);
    }
    if (!bDom.ariaLabel1 || !bDom.ariaLabel2 || bDom.ariaLabel1 === bDom.ariaLabel2
        || !bDom.ariaLabel1.includes(b.reg1) || !bDom.ariaLabel2.includes(bReg2)) {
      throw new Error(`[${viewport.label}] (order B) duplicate rows' Remove controls don't have distinct, self-identifying accessible names: ${JSON.stringify(bDom)}`);
    }
    await keyboardRemove(bReg2, "Space");
    await page.waitForFunction((i) =>
      !document.querySelector(`[data-reg-remove="${i.reg2}"]`)
      && !!document.querySelector(`[data-reg-remove="${i.reg1}"]`),
      { reg1: b.reg1, reg2: bReg2 }, { timeout: 15000 });
    const bFinal = await finalState(b.season, b.reg1, bReg2);
    if (!bFinal.reg2 || bFinal.reg2.active) {
      throw new Error(`[${viewport.label}] (order B) second-created (injected) row wasn't deactivated: ${JSON.stringify(bFinal.reg2)}`);
    }
    if (!bFinal.reg1 || !bFinal.reg1.active) {
      throw new Error(`[${viewport.label}] (order B) first-created row was deactivated by mistake: ${JSON.stringify(bFinal.reg1)}`);
    }

    if (errors.length) {
      throw new Error(`[${viewport.label}] console/page errors:\n${errors.join("\n")}`);
    }
    console.log(`[${viewport.label}] OK — same-target duplicate registrations render as independently addressable rows with distinct accessible names, each keyboard-removable via real Tab/Shift+Tab and Enter/Space while the other remains, in both orders.`);
  } catch (error) {
    throw new Error(`${error.message}\n--- demo server output ---\n${server.getOutput()}`);
  } finally {
    await context.close();
    await stopServer(server.proc);
  }
}

async function main() {
  let browser;
  try {
    browser = await chromium.launch(
      process.env.SMOKE_CHROMIUM_PATH ? { executablePath: process.env.SMOKE_CHROMIUM_PATH } : {});
    for (const viewport of VIEWPORTS) await checkViewport(browser, viewport);
    console.log("Hierarchy duplicate-registration browser journey passed.");
  } catch (error) {
    console.error("Hierarchy duplicate-registration browser journey FAILED.");
    console.error(error && error.message ? error.message : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
