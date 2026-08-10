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
//       zero mutation on both the access row and the Game;
//   (8) an ARCHIVED selected Season (#369 owner ruling, follow-up) — the
//       read-only half of this very surface. This journey owns all three
//       control families the ruling removes there (Allow picker, Revoke,
//       revoked permanent-cleanup), so the archived case is asserted against
//       the same real panel rather than in a journey of its own:
//         * a THIRD fixture whose Season holds two ACTIVE grants (one of them
//           an arena owned by a second Organization and shared with ANOTHER
//           Program, so its name can only come from the grant row's additive
//           `venue_name`) plus one REVOKED grant;
//         * FIRST, while the Season is still active, the precondition: the
//           Allow picker, the Revoke buttons and the revoked-cleanup trash
//           action are all PRESENT and a /venue-candidates request WAS
//           issued — without this the post-archive "absent" assertions would
//           pass against a surface that never rendered them at all;
//         * then the Season is archived and re-selected, /api/context is
//           confirmed to report read_only, and the SAME panel must render the
//           allowed-venues history WITH its venue names while the Allow
//           picker, every Revoke control and the whole "Revoked venue access"
//           cleanup section are gone, explicit read-only copy is shown, and
//           NO /venue-candidates request is issued.
// Fails on any browser console/page error.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");
const { installContextFixture } = require("./context-fixture.js");

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
  // Step (8) asserts that an ARCHIVED selection issues NO grant-candidate
  // request at all — not merely that it renders no picker — so every request
  // the page makes is recorded from the first navigation onward and the log is
  // cleared immediately before the archived reload.
  let candidateRequests = [];
  page.on("request", (r) => {
    if (/\/venue-candidates(\?|$)/.test(r.url())) candidateRequests.push(r.url());
  });

  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });
    await installContextFixture(page);

    // Fixture prerequisites — an otherwise-empty Program/Season and a
    // Venue, built via raw fetch so this journey's UI interactions stay
    // focused on the venue-access-cleanup surface itself.
    const fx = await page.evaluate(async () => {
      const F = window.hsFixture;
      const org = await F.create("organization", "/api/v2/setup/organization", { name: "VA Cleanup Org" });
      const program = await F.create("program", "/api/v2/setup/program",
        { name: "VA Cleanup Program", operator_organization_id: org.id });
      // #409 EXPLICIT SELECTION, boundary 1: the Season create is
      // PROGRAM-AXIS, and minting the Program above is not a selection.
      await F.selectProgram("Program-only bootstrap", program.id);
      const season = await F.create("season", "/api/v2/setup/season",
        { program_id: program.id, name: "2028-29" });
      const venue = await F.create("venue", "/api/v2/setup/venue",
        { name: "VA Cleanup Venue", organization_id: org.id });
      // BOUNDARY 2, and the reason it was already needed here before #409:
      // #369 OWNER RULING -- the Allowed-venues list and its Allow picker are
      // served only for the EXACT persisted selected Season, and the client
      // decides which Season that is from the context options it loaded at
      // page load. The grant the journey then makes through the real Allow
      // control is SEASON-OWNED besides. Select the Season explicitly, and
      // through `setActiveContext` rather than a raw POST -- see the note at
      // the reload below for what that fixes.
      await F.selectProgramSeason("Program+Season", program.id, season.id);
      return { program: program.id, season: season.id, venue: venue.id };
    });

    // Re-enter with the context this journey operates in: `contextOptions` is
    // seeded once per page load and never re-polled by render(). The URL's
    // "#ctx=" deep link no longer has to be dropped first: the selection
    // above went through `setActiveContext`, the app's own switch pipeline,
    // which keeps the hash in step with the server — so what bootstrap()
    // re-adopts on this reload IS this journey's selection. Dropping the hash
    // was only ever a patch over a raw `POST /api/context` that moved the
    // server and left the client's URL naming the previous selection (#409).
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });
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
      const F = window.hsFixture;
      const org = await F.create("organization", "/api/v2/setup/organization", { name: "VA Game Org" });
      const program = await F.create("program", "/api/v2/setup/program",
        { name: "VA Game Program", operator_organization_id: org.id });
      // #409, same two boundaries as the first fixture: Season is
      // PROGRAM-AXIS; the League, Division, the two registrations, the
      // venue-access grant and the Game are all SEASON-OWNED and all land in
      // THIS Season.
      await F.selectProgram("Program-only bootstrap (VA Game)", program.id);
      const season = await F.create("season", "/api/v2/setup/season",
        { program_id: program.id, name: "2029-30" });
      await F.selectProgramSeason("Program+Season (VA Game)", program.id, season.id);
      const league = await F.create("league", "/api/v2/setup/league",
        { season_id: season.id, name: "L" });
      const division = await F.create("division", "/api/v2/setup/division", { league_id: league.id, name: "D" });
      const club = await F.create("club", "/api/v2/setup/club", { name: "VA Game Club" });
      const home = await F.create("home", "/api/v2/setup/team",
        { league_id: league.id, club_id: club.id, name: "Home" });
      const away = await F.create("away", "/api/v2/setup/team",
        { league_id: league.id, club_id: club.id, name: "Away" });
      await F.call("team registration", `/api/v2/setup/seasons/${season.id}/team-registrations`,
        { team_id: home.id, league_id: league.id, division_id: division.id });
      await F.call("team registration", `/api/v2/setup/seasons/${season.id}/team-registrations`,
        { team_id: away.id, league_id: league.id, division_id: division.id });
      const venue = await F.create("venue", "/api/v2/setup/venue", { name: "VA Game Venue", organization_id: org.id });
      const rink = await F.create("rink", "/api/v2/setup/rink", { venue_id: venue.id, name: "R" });
      const slot = await F.create("slot", "/api/v2/setup/ice-slot", {
        rink_id: rink.id, start_time: "2029-09-01T18:00:00+00:00",
        end_time: "2029-09-01T19:30:00+00:00", slot_type: "game",
      });
      const access = await F.create("access", `/api/v2/setup/seasons/${season.id}/venue-access`,
        { venue_id: venue.id });
      // The Game itself is out of this journey's scope (already driven
      // through the wizard/UI in allowed-venues.js) — built via raw fetch
      // so this journey stays focused on the cleanup-block surface.
      const game = await F.create("game", "/api/v2/setup/game", {
        season_id: season.id, division_id: division.id, league_id: league.id,
        home_team_id: home.id, away_team_id: away.id, ice_slot_id: slot.id,
      });
      await F.call("venue-access remove", `/api/v2/setup/season-venue-access/${access.id}/remove`, {});
      return { season: season.id, venue: venue.id, access: access.id, game: game.id };
    });

    // No deep-link drop needed here either: the fixture's own
    // `selectProgramSeason` moved the hash off the Season deleted in step (6)
    // and onto this scenario's Season at the same moment it moved the server,
    // so bootstrap() re-adopts the right one (#409).
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

    // (8) The ARCHIVED selected Season (#369 owner ruling, follow-up).
    //
    // The reviewer's reproduction: an archived Season, explicitly selected,
    // with /api/context reporting read_only:true, still received grant
    // candidates and still rendered the Allow picker — controls that cannot
    // succeed (the grant fails `season_archived`), fed by the one facility
    // read that deliberately reaches ACROSS the Program ceiling.
    //
    // `shared` is owned by a SECOND Organization and is granted to ANOTHER
    // Program's Season too, so it is absent from this Program's scoped venue
    // list: its name on the archived history can only come from the grant
    // row's additive `venue_name`. If the read-only branch lost that
    // resolution the row would render a bare id, and the name assertion below
    // is what catches it.
    const fx3 = await page.evaluate(async () => {
      const F = window.hsFixture;
      const orgA = await F.create("orgA", "/api/v2/setup/organization", { name: "VA Archive Org A" });
      const programA = await F.create("programA", "/api/v2/setup/program",
        { name: "VA Archive Program A", operator_organization_id: orgA.id });
      // #409: each Season create is PROGRAM-AXIS and names a DIFFERENT
      // Program, so the saved Program moves to the one being written into
      // before each — there is no ambient default that could be right for
      // both, and minting a Program is not selecting it.
      await F.selectProgram("Program A only", programA.id);
      const seasonA = await F.create("seasonA", "/api/v2/setup/season",
        { program_id: programA.id, name: "2031-32" });
      const orgB = await F.create("orgB", "/api/v2/setup/organization", { name: "VA Archive Org B" });
      const programB = await F.create("programB", "/api/v2/setup/program",
        { name: "VA Archive Program B", operator_organization_id: orgB.id });
      await F.selectProgram("Program B only", programB.id);
      const seasonB = await F.create("seasonB", "/api/v2/setup/season",
        { program_id: programB.id, name: "2031-32 B" });
      const home = await F.create("home", "/api/v2/setup/venue",
        { name: "VA-ARCHIVE-HOME", organization_id: orgA.id });
      const shared = await F.create("shared", "/api/v2/setup/venue",
        { name: "VA-ARCHIVE-SHARED", organization_id: orgB.id });
      const retired = await F.create("retired", "/api/v2/setup/venue",
        { name: "VA-ARCHIVE-RETIRED", organization_id: orgA.id });
      // A grant binds to the SELECTED destination Season (#369 target
      // authorization), so each destination is selected before granting into
      // it — the production guard is honoured, never bypassed.
      await F.selectProgramSeason("Program B + Season B", programB.id, seasonB.id);
      await F.call("Season B venue-access grant", `/api/v2/setup/seasons/${seasonB.id}/venue-access`, { venue_id: shared.id });
      await F.selectProgramSeason("Program A + Season A", programA.id, seasonA.id);
      const gHome = await F.create("gHome", `/api/v2/setup/seasons/${seasonA.id}/venue-access`,
        { venue_id: home.id });
      const gShared = await F.create("gShared", `/api/v2/setup/seasons/${seasonA.id}/venue-access`,
        { venue_id: shared.id });
      const gRetired = await F.create("gRetired", `/api/v2/setup/seasons/${seasonA.id}/venue-access`,
        { venue_id: retired.id });
      await F.call("venue-access remove", `/api/v2/setup/season-venue-access/${gRetired.id}/remove`, {});
      return {
        program: programA.id, season: seasonA.id,
        home: home.id, shared: shared.id, retired: retired.id,
        gHome: gHome.id, gShared: gShared.id, gRetired: gRetired.id,
      };
    });
    if (!fx3.gHome || !fx3.gShared || !fx3.gRetired) {
      throw new Error(`[${viewport.label}] archived-season fixture grants did not land: `
        + JSON.stringify(fx3));
    }

    // Same deep-link drop as the earlier fixtures: the hash still names the
    // PREVIOUS selection, which bootstrap() would faithfully re-adopt.
    const enterSetup = async () => {
      await page.evaluate(() => history.replaceState(
        null, "", location.pathname + location.search));
      await page.reload({ waitUntil: "domcontentloaded" });
      await page.waitForSelector("#content > *", { timeout: 10000 });
      await page.click('.tab[data-tab="setup"]');
      await page.click('[data-setup-view="hierarchy"]');
    };
    await enterSetup();

    // PRECONDITION, while the Season is still ACTIVE: all three control
    // families really do render here, and the candidate request really is
    // issued. Without this the "absent" assertions after the archive would
    // pass just as happily against a panel that never showed them.
    await page.waitForSelector(`#va-add-${fx3.season}`, { timeout: 15000 });
    for (const [label, selector] of [
      ["Allow button", `[data-va-add="${fx3.season}"]`],
      ["Revoke (home)", `[data-va-revoke="${fx3.gHome}"]`],
      ["Revoke (shared)", `[data-va-revoke="${fx3.gShared}"]`],
      ["revoked cleanup", `#season-participation [data-del="season-venue-access"][data-del-id="${fx3.gRetired}"]`],
    ]) {
      if (await page.locator(selector).count() === 0) {
        throw new Error(`[${viewport.label}] precondition failed: the ACTIVE season's `
          + `${label} is already absent, so its absence after archiving would prove nothing`);
      }
    }
    const activePanelText = await page.textContent("#season-participation");
    for (const name of ["VA-ARCHIVE-HOME", "VA-ARCHIVE-SHARED"]) {
      if (!activePanelText.includes(name)) {
        throw new Error(`[${viewport.label}] precondition failed: "${name}" is not on the `
          + `ACTIVE season's Allowed venues list`);
      }
    }
    if (!/Revoked venue access/i.test(activePanelText)) {
      throw new Error(`[${viewport.label}] precondition failed: the ACTIVE season shows no `
        + `"Revoked venue access" section, so its absence after archiving proves nothing`);
    }
    if (candidateRequests.length === 0) {
      throw new Error(`[${viewport.label}] precondition failed: no /venue-candidates request `
        + `was issued for the ACTIVE selected season, so "no request" after archiving `
        + `would be vacuously true`);
    }

    // Archive it and re-select it explicitly, exactly as the reviewer did.
    // There is no archive control in the shipped UI (#159 — a Season is
    // archived through the API), so this is a raw POST; the SELECTION that
    // follows goes through the same /api/context the switcher posts.
    const archivedCtx = await page.evaluate(async (i) => {
      const F = window.hsFixture;
      const archived = await F.create("archived", `/api/v2/setup/seasons/${i.season}/archive`,
        { reason: "season complete" });
      await F.selectProgramSeason("re-select the now-archived Season",
        i.program, i.season);
      const ctx = await (await fetch("/api/context", { credentials: "same-origin" })).json();
      return { archived, ctx };
    }, { program: fx3.program, season: fx3.season });
    if (archivedCtx.archived.error || archivedCtx.archived.status !== "archived") {
      throw new Error(`[${viewport.label}] archiving the season failed: `
        + JSON.stringify(archivedCtx.archived));
    }
    if (archivedCtx.ctx.season_id !== fx3.season || archivedCtx.ctx.read_only !== true) {
      throw new Error(`[${viewport.label}] /api/context does not report the archived season `
        + `as the selected, read-only one: ${JSON.stringify(archivedCtx.ctx)}`);
    }

    // Re-enter with the archived selection, watching for any candidate request.
    candidateRequests = [];
    await enterSetup();
    // Wait on something the panel paints in BOTH the gated and the ungated
    // build — this Season's own allowed-venue name — so a regression is caught
    // by the specific assertions below rather than by a bare selector timeout.
    await page.waitForFunction((name) => {
      const el = document.querySelector("#season-participation");
      return !!el && el.textContent.includes(name);
    }, "VA-ARCHIVE-HOME", { timeout: 15000 }).catch(() => {
      throw new Error(`[${viewport.label}] the archived season's allowed-venues history `
        + `never rendered at all`);
    });

    // The context bar's own read-only signal is what the panel keys off, so
    // assert the two agree rather than trusting the panel alone.
    if (!(await page.locator("#ctx-ro").isVisible())) {
      throw new Error(`[${viewport.label}] the context bar's read-only badge is hidden for `
        + `the selected archived season`);
    }

    // Every control that cannot succeed is GONE.
    for (const [label, selector] of [
      ["Allow picker", `#va-add-${fx3.season}`],
      ["Allow button", `[data-va-add="${fx3.season}"]`],
      ["Revoke control", "[data-va-revoke]"],
      ["revoked permanent-cleanup control", '#season-participation [data-del="season-venue-access"]'],
    ]) {
      const count = await page.locator(selector).count();
      if (count !== 0) {
        throw new Error(`[${viewport.label}] the archived season still offers its ${label} `
          + `(${count} matching "${selector}") — a control whose write fails season_archived`);
      }
    }
    // ...and the candidate directory was never even asked for.
    if (candidateRequests.length) {
      throw new Error(`[${viewport.label}] the archived selection still requested grant `
        + `candidates: ${candidateRequests.join(", ")}`);
    }

    // The history itself still renders, NAMES included — including the
    // cross-Program arena whose name only the grant row can supply — and says
    // in so many words why nothing here can be changed.
    const archivedPanelText = await page.textContent("#season-participation");
    for (const name of ["VA-ARCHIVE-HOME", "VA-ARCHIVE-SHARED"]) {
      if (!archivedPanelText.includes(name)) {
        throw new Error(`[${viewport.label}] the archived season's allowed-venues history `
          + `lost "${name}" — read-only history is exactly what this surface is for`);
      }
    }
    if (archivedPanelText.includes(fx3.shared)) {
      throw new Error(`[${viewport.label}] the archived season's shared arena rendered as a `
        + `bare venue id instead of its name`);
    }
    // Revoked grants are HISTORY and MUST still render on an archived Season --
    // the API preserves them deliberately. The earlier revision of this
    // assertion required the section to vanish, which encoded the defect: it
    // hid a Season's own past rather than the controls that mutate it. What
    // must be absent is only the permanent-cleanup button, asserted above.
    if (!/Revoked venue access/i.test(archivedPanelText)) {
      throw new Error(`[${viewport.label}] the archived season dropped its "Revoked venue `
        + `access" HISTORY; only the cleanup control may be withheld`);
    }
    if (!archivedPanelText.includes("VA-ARCHIVE-RETIRED")) {
      throw new Error(`[${viewport.label}] the archived season's revoked history does not `
        + `name VA-ARCHIVE-RETIRED: ${archivedPanelText.slice(0, 400)}`);
    }
    if (!/archived and read-only/i.test(archivedPanelText)) {
      throw new Error(`[${viewport.label}] the archived season's venue section carries no `
        + `explicit read-only copy: ${archivedPanelText.slice(0, 400)}`);
    }

    if (errors.length) {
      throw new Error(`[${viewport.label}] console/page errors:\n${errors.join("\n")}`);
    }
    console.log(`[${viewport.label}] OK — venue access cleanup UI verified, `
      + `including the archived season's read-only history.`);
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
