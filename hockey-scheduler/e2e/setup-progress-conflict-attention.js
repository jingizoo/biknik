// Setup-progress card must never claim completion over an ambiguous
// registration (#331 review round 21 finding 2).
//
// A Team can hold a genuinely valid registration at its own permanent
// League AND, separately, an active stray registration at a different
// League in the same Season -- the identical season-wide conflict shape
// round 20 already made create_game/move_game/publish_game reject. Unlike
// round 20's exact-key duplicate (SQL's ux_team_league_season unique index
// makes that unconstructible on a real database), this shape spans TWO
// DIFFERENT LeagueSeasons, so no single-key unique index rules it out --
// it is real, durable-store-constructible data. This file still uses the
// in-memory-server-with-live-injection technique hierarchy-duplicate-
// registration.js established (identical LAUNCHER_SCRIPT, unchanged) purely
// for convenience: it lets one long-running page session build the fixture,
// inject, and re-observe the SAME live server without juggling a separate
// process per step.
//
// Proves: with every OTHER Setup workflow complete, the setup-progress card
// reads "All setup steps complete" BEFORE the stray exists (the fixture's
// own sanity check) and switches to "Continue setup" / "Season
// participation and divisions" the moment it does -- never the false
// success card the OLD per-row count reported -- with the conflict visibly
// rendered both in the primary card and in the workflow list below it, on
// both desktop and 390px. Fails on any browser console/page error.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8323 },
  { label: "phone", width: 390, height: 844, port: 8324 },
];
const KNOWN_CARD_HEADINGS = [
  "Continue setup", "✓ All setup steps complete",
  "Setup progress unavailable", "Setup progress",
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

// Identical to hierarchy-duplicate-registration.js's own launcher: starts the
// real hockey_scheduler.web.server serve() in a background thread against
// the DemoState default in-memory store, then injects one active
// SeasonTeamRegistration per "team_id,league_id,season_id" stdin line
// directly into that SAME live store.
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

function cardState(page) {
  return page.evaluate((headings) => {
    const h3 = Array.from(document.querySelectorAll(".dash-card h3")).find(
      (el) => headings.some((h) => el.textContent.trim().startsWith(h)));
    if (!h3) return null;
    const card = h3.closest(".dash-card");
    const rows = Array.from(card.querySelectorAll(".li")).map((li) => ({
      title: (li.querySelector(".li-title") || {}).textContent || "",
      statusText: (li.querySelector(".badge") || {}).textContent || "",
      attentionText: (li.querySelector(".li-sub.conflict") || {}).textContent || "",
    }));
    const attentionRows = Array.from(card.querySelectorAll(".na-ico.amber")).map((ico) => {
      const row = ico.closest(".na-row");
      return {
        title: (row.querySelector(".na-title") || {}).textContent || "",
        detail: (row.querySelector(".na-sub") || {}).textContent || "",
      };
    });
    return { heading: h3.textContent, rows, attentionRows };
  }, KNOWN_CARD_HEADINGS);
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
    await page.waitForFunction(
      () => document.body.dataset.view === "onboarding"
        || document.body.dataset.view === "dashboard", null, { timeout: 10000 });
    if (await page.evaluate(() => document.body.dataset.view) === "onboarding") {
      await page.click('[data-onboarding-goto="dashboard"]');
    }
    await page.waitForFunction(
      () => document.body.dataset.view === "dashboard", null, { timeout: 10000 });

    // Re-clicks the Dashboard tab (its onclick calls render() unconditionally,
    // which re-triggers loadSetupProgressCard()'s own fetch) so store
    // injection outside this page's own writes is reflected.
    const refreshDashboard = async () => {
      await page.click('.tab[data-tab="setup"]');
      await page.click('.tab[data-tab="dashboard"]');
      // The card's own fetch loads independently of the rest of the
      // Dashboard (#331 review round 2 finding 3): its slot only exists once
      // the Dashboard's OWN overview fetch has resolved and painted, so
      // waiting on the slot alone (absent -> vacuously "not loading") races
      // that outer paint. Fall back to .dash-stats -- the Dashboard's own
      // settled-content marker -- exactly as home-tasks-hub.js's own
      // waitForCardSettled() does.
      await page.waitForFunction(() => {
        const slot = document.getElementById("sp-card-slot");
        if (slot) return !slot.querySelector(".skeleton");
        return !!document.querySelector(".dash-stats");
      }, null, { timeout: 10000 });
    };

    // Every workflow complete via genuine HTTP writes: league profile,
    // permanent teams, a valid Season registration at League A, a player,
    // and facilities (venue + rink + ice slot + season venue access) --
    // isolates the reproduction to participation alone.
    const fixture = await page.evaluate(async () => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      const program = await post("/api/v2/setup/program", { name: "Conflict Program" });
      const season = await post("/api/v2/setup/season",
        { program_id: program.id, name: "2026-27" });
      const leagueA = await post("/api/v2/setup/league",
        { season_id: season.id, name: "League A" });
      const leagueB = await post("/api/v2/setup/league",
        { season_id: season.id, name: "League B" });
      const club = await post("/api/v2/setup/club", { name: "Club" });
      const team = await post("/api/v2/setup/team",
        { league_id: leagueA.id, club_id: club.id, name: "Conflict Team" });
      const reg = await post(`/api/v2/setup/seasons/${season.id}/team-registrations`,
        { team_id: team.id, league_id: leagueA.id, division_id: null });
      await post("/api/v2/setup/player",
        { team_id: team.id, name: "Player One", position: "forward" });
      const venue = await post("/api/v2/setup/venue", { name: "Venue", league_id: program.id });
      const rink = await post("/api/v2/setup/rink", { venue_id: venue.id, name: "Rink" });
      await post(`/api/v2/setup/seasons/${season.id}/venue-access`, { venue_id: venue.id });
      await post("/api/v2/setup/ice-slot", {
        rink_id: rink.id, start_time: "2026-09-01T18:00:00+00:00",
        end_time: "2026-09-01T19:00:00+00:00", slot_type: "game",
      });
      return {
        program: program.id, season: season.id, leagueA: leagueA.id,
        leagueB: leagueB.id, team: team.id, reg: reg.id,
      };
    });

    if (errors.length) {
      throw new Error(`[${viewport.label}] console/page errors during fixture setup:\n${errors.join("\n")}`);
    }

    // Sanity check the fixture: with no stray yet, the card genuinely reads
    // complete -- the reproduction below is a real regression, not an
    // artifact of a broken fixture.
    await refreshDashboard();
    const cleanState = await cardState(page);
    if (!cleanState || !cleanState.heading.includes("All setup steps complete")) {
      throw new Error(`[${viewport.label}] fixture sanity check failed -- expected the complete card before injecting the stray: ${JSON.stringify(cleanState)}`);
    }

    // The season-wide conflict: an active stray for the SAME Team at League
    // B, injected directly into the live store (no legitimate write path can
    // create it any more -- register_team_for_season itself rejects this
    // shape as of #331 review round 20).
    const strayId = await server.inject(fixture.team, fixture.leagueB, fixture.season);
    await refreshDashboard();
    const conflict = await cardState(page);
    if (!conflict || !conflict.heading.includes("Continue setup")) {
      throw new Error(`[${viewport.label}] expected "Continue setup" (never the false success card) once the stray exists: ${JSON.stringify(conflict)}`);
    }
    if (!conflict.attentionRows.length
        || conflict.attentionRows[0].title !== "Season participation and divisions"
        || !/don't match their team's permanent league or division/.test(conflict.attentionRows[0].detail)) {
      throw new Error(`[${viewport.label}] primary card doesn't render the participation workflow's attention: ${JSON.stringify(conflict.attentionRows)}`);
    }
    const participationRow = conflict.rows.find(
      (r) => r.title === "Season participation and divisions");
    if (!participationRow || participationRow.statusText !== "To do"
        || !/don't match their team's permanent league or division/.test(participationRow.attentionText)) {
      throw new Error(`[${viewport.label}] workflow list doesn't render participation as To do with its attention line: ${JSON.stringify(participationRow)}`);
    }

    if (errors.length) {
      throw new Error(`[${viewport.label}] console/page errors:\n${errors.join("\n")}`);
    }
    console.log(`[${viewport.label}] OK — the false-success card never appears once a Team's registration is season-wide ambiguous (stray ${strayId}); "Continue setup" and the workflow list both render the real reason.`);
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
    console.log("Setup-progress conflict-attention browser journey passed.");
  } catch (error) {
    console.error("Setup-progress conflict-attention browser journey FAILED.");
    console.error(error && error.message ? error.message : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
