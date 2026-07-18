// Season-venue-access cleanup browser journey (#233 Slice E, PR #257 review) —
// the same hidden-inactive-row bug #251 fixed for team registrations,
// reintroduced for SeasonVenueAccess and now fixed the same way.
//
// revoke_season_venue_access only deactivates a row (history preserved), but
// delete_season()/delete_venue() block on a matching access row REGARDLESS of
// active status — so a revoked-but-uncleaned row silently blocks the Season
// or Venue forever with no UI path to resolve it, unless the "Revoked venue
// access" section's permanent-cleanup action exists.
//
// At desktop and 390px, through the real Setup UI (Season participation
// panel, id="season-participation", and the Facility tree):
//   (1) grant a Venue access to an otherwise-empty Season via the "Allowed
//       venues" Allow control;
//   (2) deleting the Venue is blocked ("venue access" dependency) — zero
//       mutation, cancel closes the blocked modal;
//   (3) revoke access via the Revoke control — the row moves to "Revoked
//       venue access" with a permanent-cleanup trash action;
//   (4) deleting the Venue is STILL blocked (revoke alone doesn't clear it);
//   (5) permanently clean the revoked row via its trash action;
//   (6) the Venue delete now succeeds, and separately the otherwise-empty
//       Season also becomes deletable;
//   (7) a SEPARATE game-backed scenario (#257 review): a revoked access row
//       that is the only explicit record of why a Game's ice was allowed
//       must never be purgeable — clicking its cleanup trash action shows
//       the blocked modal (mentioning the Game), not a silent success, with
//       zero mutation on both the access row and the Game.
// Fails on any browser console/page error.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8211 },
  { label: "phone", width: 390, height: 844, port: 8212 },
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
  const consoleErrorHandler = (m) => { if (m.type() === "error") errors.push(`[console] ${m.text()}`); };
  page.on("console", consoleErrorHandler);

  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });

    // Fixture prerequisites — an otherwise-empty Program/Season and a
    // Venue, built via raw fetch so this journey's UI interactions stay
    // focused on the venue-access-cleanup surface itself.
    const fx = await page.evaluate(async () => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      const org = await post("/api/v2/setup/organization", { name: "VA Cleanup Org" });
      const program = await post("/api/v2/setup/program",
        { name: "VA Cleanup Program", operator_organization_id: org.id });
      const season = await post("/api/v2/setup/season", { program_id: program.id, name: "2028-29" });
      const venue = await post("/api/v2/setup/venue", { name: "VA Cleanup Venue", organization_id: org.id });
      return { program: program.id, season: season.id, venue: venue.id };
    });

    await page.click('.tab[data-tab="setup"]');
    await page.click('[data-setup-view="hierarchy"]');
    await page.waitForFunction(
      (sel) => !!document.querySelector(sel), `#va-add-${fx.season}`, { timeout: 15000 });

    // (1) Grant the Venue access to the Season through the real Allow control.
    await page.selectOption(`#va-add-${fx.season}`, fx.venue);
    const grantResp = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/seasons/${fx.season}/venue-access`
      && r.request().method() === "POST");
    await page.click(`[data-va-add="${fx.season}"]`);
    const grantBody = await (await grantResp).json();
    if (grantBody.error) throw new Error(`[${viewport.label}] grant failed: ${JSON.stringify(grantBody.error)}`);
    const accessId = grantBody.id;
    await page.waitForSelector(`[data-va-revoke="${accessId}"]`, { timeout: 10000 });

    // (2) Deleting the Venue is blocked by the active access row — zero
    // mutation, cancel closes the blocked modal.
    const attemptVenueDelete = async () => {
      await page.waitForSelector(
        `#facility-tree [data-del="venue"][data-del-id="${fx.venue}"]`, { timeout: 10000 });
      await page.click(`#facility-tree [data-del="venue"][data-del-id="${fx.venue}"]`);
      await page.waitForSelector(".modal.danger [data-del-confirm]", { timeout: 10000 });
      // Venue is high-risk (#215): the confirm button stays disabled until
      // the typed-confirmation input carries the name or "DELETE".
      await page.fill("#del-confirm", "DELETE");
      const resp = page.waitForResponse((r) =>
        r.url() === `${base}/api/v2/setup/venue/${fx.venue}/delete` && r.request().method() === "POST");
      // A deliberately-blocked delete's 409 response logs a benign
      // "Failed to load resource" Chromium console entry, not a page bug
      // (same pattern as registration-cleanup.js / division-delete-cleanup.js).
      page.off("console", consoleErrorHandler);
      await page.click("[data-del-confirm]");
      await resp;
      await page.waitForSelector(".modal.blocked", { timeout: 10000 });
      page.on("console", consoleErrorHandler);
      const blockedText = await page.textContent(".modal.blocked");
      return blockedText || "";
    };
    const blocked1 = await attemptVenueDelete();
    if (!/venue access/i.test(blocked1)) {
      throw new Error(`[${viewport.label}] blocked modal did not mention venue access: ${blocked1}`);
    }
    await page.click(".modal.blocked [data-modal-close]");
    await page.waitForSelector(".modal", { state: "detached", timeout: 10000 });

    // Zero mutation: the Venue is still there.
    const venueStillThere1 = await page.evaluate(async (i) => {
      const get = async (p) => (await fetch(p, { credentials: "same-origin" })).json();
      const ov = await get("/api/v2/setup/overview");
      return (ov.venues || []).some((v) => v.id === i.venue);
    }, { venue: fx.venue });
    if (!venueStillThere1) throw new Error(`[${viewport.label}] Venue vanished despite the block`);

    // (3) Revoke access — the row moves to "Revoked venue access" with a
    // permanent-cleanup trash action, and the venue is grantable again.
    const revokeResp = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/season-venue-access/${accessId}/remove`
      && r.request().method() === "POST");
    await page.click(`[data-va-revoke="${accessId}"]`);
    if ((await revokeResp).status() !== 200) throw new Error(`[${viewport.label}] revoke failed`);
    await page.waitForSelector(
      `#season-participation [data-del="season-venue-access"][data-del-id="${accessId}"]`,
      { timeout: 10000 });
    const revokedText = await page.textContent("#season-participation");
    if (!/Revoked venue access/i.test(revokedText) || !/VA Cleanup Venue/.test(revokedText)) {
      throw new Error(`[${viewport.label}] revoked venue access section missing expected detail`);
    }

    // (4) Deleting the Venue is STILL blocked — revoke alone never resolves
    // the dependency, only the explicit cleanup below does.
    const blocked2 = await attemptVenueDelete();
    if (!/venue access/i.test(blocked2)) {
      throw new Error(`[${viewport.label}] revoked-row block did not mention venue access: ${blocked2}`);
    }
    await page.click(".modal.blocked [data-modal-close]");
    await page.waitForSelector(".modal", { state: "detached", timeout: 10000 });

    // (5) Permanently clean the revoked row via its trash action.
    await page.click(`#season-participation [data-del="season-venue-access"][data-del-id="${accessId}"]`);
    await page.waitForSelector(".modal.danger [data-del-confirm]", { timeout: 10000 });
    const cleanupResp = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/season-venue-access/${accessId}/delete`
      && r.request().method() === "POST");
    await page.click("[data-del-confirm]");
    const cleanupBody = await (await cleanupResp).json();
    if (cleanupBody.error) throw new Error(`[${viewport.label}] cleanup failed: ${JSON.stringify(cleanupBody.error)}`);
    await page.waitForSelector(".modal", { state: "detached", timeout: 10000 });
    const afterCleanupText = await page.textContent("#season-participation");
    if (/Revoked venue access/i.test(afterCleanupText)) {
      throw new Error(`[${viewport.label}] revoked venue access section still present after cleanup`);
    }

    // (6) The Venue delete now succeeds.
    await page.waitForSelector(
      `#facility-tree [data-del="venue"][data-del-id="${fx.venue}"]`, { timeout: 10000 });
    await page.click(`#facility-tree [data-del="venue"][data-del-id="${fx.venue}"]`);
    await page.waitForSelector(".modal.danger [data-del-confirm]", { timeout: 10000 });
    await page.fill("#del-confirm", "DELETE");
    const venueDelResp = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/venue/${fx.venue}/delete` && r.request().method() === "POST");
    await page.click("[data-del-confirm]");
    const venueDelBody = await (await venueDelResp).json();
    if (venueDelBody.error) throw new Error(`[${viewport.label}] Venue delete failed: ${JSON.stringify(venueDelBody.error)}`);
    await page.waitForSelector(".modal", { state: "detached", timeout: 10000 });
    const venueGone = await page.evaluate(async (i) => {
      const get = async (p) => (await fetch(p, { credentials: "same-origin" })).json();
      const ov = await get("/api/v2/setup/overview");
      return !(ov.venues || []).some((v) => v.id === i.venue);
    }, { venue: fx.venue });
    if (!venueGone) throw new Error(`[${viewport.label}] Venue still exists after delete`);

    // Separately, the otherwise-empty Season also becomes deletable — the
    // same cleanup unblocks both sides of the SeasonVenueAccess join.
    await page.waitForSelector(
      `#season-participation [data-del="season"][data-del-id="${fx.season}"]`, { timeout: 10000 });
    await page.click(`#season-participation [data-del="season"][data-del-id="${fx.season}"]`);
    await page.waitForSelector(".modal.danger [data-del-confirm]", { timeout: 10000 });
    await page.fill("#del-confirm", "DELETE");
    const seasonDelResp = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/season/${fx.season}/delete` && r.request().method() === "POST");
    await page.click("[data-del-confirm]");
    const seasonDelBody = await (await seasonDelResp).json();
    if (seasonDelBody.error) throw new Error(`[${viewport.label}] Season delete failed: ${JSON.stringify(seasonDelBody.error)}`);
    await page.waitForSelector(".modal", { state: "detached", timeout: 10000 });

    // (7) A SEPARATE game-backed scenario (#257 review): a revoked access
    // row that is the only explicit record of why a Game's ice was allowed
    // must never be purgeable — the cleanup trash action itself must show
    // the blocked modal, not silently succeed.
    const fx2 = await page.evaluate(async () => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      const org = await post("/api/v2/setup/organization", { name: "VA Game Org" });
      const program = await post("/api/v2/setup/program",
        { name: "VA Game Program", operator_organization_id: org.id });
      const season = await post("/api/v2/setup/season", { program_id: program.id, name: "2029-30" });
      const league = await post("/api/v2/setup/league", { season_id: season.id, name: "L" });
      const division = await post("/api/v2/setup/division", { league_id: league.id, name: "D" });
      const club = await post("/api/v2/setup/club", { name: "VA Game Club" });
      const home = await post("/api/v2/setup/team",
        { league_id: league.id, club_id: club.id, name: "Home" });
      const away = await post("/api/v2/setup/team",
        { league_id: league.id, club_id: club.id, name: "Away" });
      await post(`/api/v2/setup/seasons/${season.id}/team-registrations`,
        { team_id: home.id, league_id: league.id, division_id: division.id });
      await post(`/api/v2/setup/seasons/${season.id}/team-registrations`,
        { team_id: away.id, league_id: league.id, division_id: division.id });
      const venue = await post("/api/v2/setup/venue", { name: "VA Game Venue", organization_id: org.id });
      const rink = await post("/api/v2/setup/rink", { venue_id: venue.id, name: "R" });
      const slot = await post("/api/v2/setup/ice-slot", {
        rink_id: rink.id, start_time: "2029-09-01T18:00:00+00:00",
        end_time: "2029-09-01T19:30:00+00:00", slot_type: "game",
      });
      const access = await post(`/api/v2/setup/seasons/${season.id}/venue-access`,
        { venue_id: venue.id });
      // The Game itself is out of this journey's scope (already driven
      // through the wizard/UI in allowed-venues.js) — built via raw fetch
      // so this journey stays focused on the cleanup-block surface.
      const game = await post("/api/v2/setup/game", {
        season_id: season.id, division_id: division.id, league_id: league.id,
        home_team_id: home.id, away_team_id: away.id, ice_slot_id: slot.id,
      });
      await post(`/api/v2/setup/season-venue-access/${access.id}/remove`, {});
      return { season: season.id, venue: venue.id, access: access.id, game: game.id };
    });

    await page.reload({ waitUntil: "domcontentloaded" });
    await page.click('.tab[data-tab="setup"]');
    await page.click('[data-setup-view="hierarchy"]');
    await page.waitForSelector(
      `#season-participation [data-del="season-venue-access"][data-del-id="${fx2.access}"]`,
      { timeout: 15000 });
    await page.click(`#season-participation [data-del="season-venue-access"][data-del-id="${fx2.access}"]`);
    await page.waitForSelector(".modal.danger [data-del-confirm]", { timeout: 10000 });
    const gameBlockResp = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/season-venue-access/${fx2.access}/delete`
      && r.request().method() === "POST");
    page.off("console", consoleErrorHandler);
    await page.click("[data-del-confirm]");
    await gameBlockResp;
    await page.waitForSelector(".modal.blocked", { timeout: 10000 });
    page.on("console", consoleErrorHandler);
    const gameBlockedText = await page.textContent(".modal.blocked");
    if (!/game/i.test(gameBlockedText || "")) {
      throw new Error(`[${viewport.label}] game-backed cleanup block did not mention the Game: ${gameBlockedText}`);
    }
    await page.click(".modal.blocked [data-modal-close]");
    await page.waitForSelector(".modal", { state: "detached", timeout: 10000 });

    // Zero mutation: the revoked row (and the Game) are both still there.
    const stillGameBacked = await page.evaluate(async (i) => {
      const get = async (p) => (await fetch(p, { credentials: "same-origin" })).json();
      const listing = await get(`/api/v2/setup/seasons/${i.season}/venue-access`);
      const demo = await get("/api/demo/overview");
      return {
        accessGone: !(listing.venue_access || []).some((a) => a.id === i.access),
        gameGone: !(demo.schedule || []).some((g) => g.game_id === i.game),
      };
    }, { season: fx2.season, access: fx2.access, game: fx2.game });
    if (stillGameBacked.accessGone) throw new Error(`[${viewport.label}] revoked access vanished despite the block`);
    if (stillGameBacked.gameGone) throw new Error(`[${viewport.label}] Game vanished despite the block`);

    if (errors.length) {
      throw new Error(`[${viewport.label}] console/page errors:\n${errors.join("\n")}`);
    }
    console.log(`[${viewport.label}] OK — venue access cleanup UI verified.`);
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
    console.log("Venue-access cleanup browser journey passed.");
  } catch (error) {
    console.error("Venue-access cleanup browser journey FAILED.");
    console.error(error && error.message ? error.message : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
