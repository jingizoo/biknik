// Player Home cross-team substitute opt-in (#287).
//
// Runs the shipped index.html/app.js/styles at desktop and the canonical
// 390x844 phone viewport while replacing only API responses with a focused,
// deterministic fixture. This makes the assertions about the REAL browser
// rendering and event handlers without coupling the UI contract to database
// setup. It proves:
//   * every cross-team row is rendered (including rows after the old 3-row
//     cutoff), with native, accessibly named auto-save checkboxes;
//   * every cross-team action sends the compound (game_id, target_team_id)
//     identity while the legacy same-team action keeps its empty body;
//   * a held write disables both same-game target choices and both Details
//     controls across a navigation-driven rerender, with no second request;
//   * pending Saving/Removing state is announced, canonical errors recover,
//     per-game outcomes cannot overwrite one another, and stale responses do
//     not cross a no-reload player identity switch;
//   * focus is restored only while the initiating intent is still current,
//     with a section-heading fallback when withdrawal removes the row;
//   * detail GETs retain target_team_id, and going Back clears it before a
//     legacy same-team detail is opened;
//   * a privacy-minimal cross-team detail (no team/roster/slot fields) renders;
//   * the existing same-team "View Opportunity" interaction remains; and
//   * scoped text meets 4.5:1 contrast, the row has a >=44px target, and the
//     complete list has no horizontal overflow at 390px.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8997 },
  { label: "phone", width: 390, height: 844, port: 8998 },
];

function waitForServer(url, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const attempt = () => {
      const request = http.get(url, (response) => {
        response.resume();
        resolve();
      });
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

function json(route, body, status = 200) {
  return route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitFor(predicate, message, timeoutMs = 10000) {
  const deadline = Date.now() + timeoutMs;
  while (!predicate()) {
    if (Date.now() >= deadline) throw new Error(message);
    await delay(10);
  }
}

function rgbChannels(cssColor) {
  const match = String(cssColor).match(
    /^rgba?\(\s*([\d.]+)[, ]+\s*([\d.]+)[, ]+\s*([\d.]+)/i);
  if (!match) throw new Error(`Cannot parse computed color ${cssColor}`);
  return match.slice(1, 4).map(Number);
}

function relativeLuminance(cssColor) {
  const channels = rgbChannels(cssColor).map((channel) => {
    const value = channel / 255;
    return value <= 0.04045 ? value / 12.92
      : Math.pow((value + 0.055) / 1.055, 2.4);
  });
  return 0.2126 * channels[0] + 0.7152 * channels[1]
    + 0.0722 * channels[2];
}

function contrastRatio(foreground, background) {
  const lighter = Math.max(
    relativeLuminance(foreground), relativeLuminance(background));
  const darker = Math.min(
    relativeLuminance(foreground), relativeLuminance(background));
  return (lighter + 0.05) / (darker + 0.05);
}

function crossChoice({
  gameId, targetId, teamName, opponentName, enrolled = false,
  canWithdraw = enrolled, needsCleanup = false, blockedReason = null,
  startTime = "2026-09-11T19:30:00+00:00",
}) {
  return {
    game_id: gameId,
    target_team_id: targetId,
    cross_team: true,
    team_name: teamName,
    opponent_name: opponentName,
    start_time: startTime,
    venue_name: "Twin Rinks",
    rink_name: "Blue Rink",
    position_needed: "skater",
    enrollment_status: enrolled ? "enrolled" : null,
    can_enroll: !enrolled,
    can_withdraw: canWithdraw,
    needs_cleanup: needsCleanup,
    blocked_reason: blockedReason,
  };
}

function crossOpportunity(enrolled) {
  return crossChoice({
    gameId: "game-cross", targetId: "bronze-team-4",
    teamName: "Bronze Team 4", opponentName: "Bronze Team 5", enrolled,
  });
}

const raceChoice4 = () => crossChoice({
  gameId: "game-race", targetId: "bronze-team-4",
  teamName: "Bronze Team 4", opponentName: "Bronze Team 5",
});
const raceChoice5 = () => crossChoice({
  gameId: "game-race", targetId: "bronze-team-5",
  teamName: "Bronze Team 5", opponentName: "Bronze Team 4",
});
const LONG_UNBROKEN_TEAM_NAME = "BronzeTeam6" + "UnbrokenName".repeat(20);
const errorChoice = () => crossChoice({
  gameId: "game-error", targetId: "bronze-team-6",
  teamName: LONG_UNBROKEN_TEAM_NAME, opponentName: "Bronze Team 7",
});
const identityChoice = () => crossChoice({
  gameId: "game-identity", targetId: "bronze-team-12",
  teamName: "Bronze Team 12", opponentName: "Bronze Team 13",
});
const repeatedMatchupChoice = () => crossChoice({
  gameId: "game-repeat", targetId: "bronze-team-4",
  teamName: "Bronze Team 4", opponentName: "Bronze Team 5",
  startTime: "2026-09-18T19:30:00+00:00",
});
const lockedCleanupChoice = () => crossChoice({
  gameId: "game-locked", targetId: "bronze-team-8",
  teamName: "Bronze Team 8", opponentName: "Bronze Team 9",
  enrolled: true, canWithdraw: false, needsCleanup: true,
  blockedReason: "The roster for this game is locked.",
});
const crossTeamOffer = () => ({
  ...crossChoice({
    gameId: "game-offer", targetId: "bronze-team-10",
    teamName: "Bronze Team 10", opponentName: "Bronze Team 11",
  }),
  enrollment_status: "offered",
  can_enroll: false,
  can_withdraw: false,
  can_accept_offer: true,
  can_decline_offer: true,
});

function focusedCrossTeamOffer({
  gameId, targetId, teamName, opponentName, startTime,
  offerExpired = false,
}) {
  return {
    ...crossChoice({
      gameId, targetId, teamName, opponentName, startTime,
    }),
    enrollment_status: "offered",
    can_enroll: false,
    can_withdraw: false,
    can_accept_offer: !offerExpired,
    can_decline_offer: true,
    offer_expired: offerExpired,
    blocked_reason: offerExpired ? "This offer has expired." : null,
  };
}

const sameViewOffer = () => focusedCrossTeamOffer({
  gameId: "game-offer-success", targetId: "bronze-team-14",
  teamName: "Bronze Team 14", opponentName: "Bronze Team 15",
  startTime: "2026-09-20T19:30:00+00:00",
});
const refusedOffer = () => focusedCrossTeamOffer({
  gameId: "game-offer-error", targetId: "bronze-team-16",
  teamName: "Bronze Team 16", opponentName: "Bronze Team 17",
  startTime: "2026-09-21T19:30:00+00:00",
});
const vanishedOffer = () => focusedCrossTeamOffer({
  gameId: "game-offer-gone", targetId: "bronze-team-18",
  teamName: "Bronze Team 18", opponentName: "Bronze Team 19",
  startTime: "2026-09-22T19:30:00+00:00",
});
const orphanFocusOffer = () => focusedCrossTeamOffer({
  gameId: "game-offer-orphan", targetId: "bronze-team-20",
  teamName: "Bronze Team 20", opponentName: "Bronze Team 21",
  startTime: "2026-09-23T19:30:00+00:00",
});
const expiredOffer = () => focusedCrossTeamOffer({
  gameId: "game-offer-expired", targetId: "bronze-team-22",
  teamName: "Bronze Team 22", opponentName: "Bronze Team 23",
  startTime: "2026-09-24T19:30:00+00:00", offerExpired: true,
});
const focusChoice = (enrolled = false) => crossChoice({
  gameId: "game-focus", targetId: "bronze-team-24",
  teamName: "Bronze Team 24", opponentName: "Bronze Team 25", enrolled,
  startTime: "2026-09-25T19:30:00+00:00",
});

function enrolledChoice(choice) {
  return {
    ...choice,
    enrollment_status: "enrolled",
    can_enroll: false,
    can_withdraw: true,
  };
}

const sameTeamOpportunity = {
  game_id: "game-same",
  target_team_id: "bronze-team-1",
  cross_team: false,
  team_name: "Bronze Team 1",
  opponent_name: "Bronze Team 2",
  start_time: "2026-09-12T20:00:00+00:00",
  venue_name: "Twin Rinks",
  rink_name: "Red Rink",
  position_needed: "skater",
};

const guardianInlineOffer = () => focusedCrossTeamOffer({
  gameId: "guardian-inline-offer", targetId: "bronze-team-30",
  teamName: "Bronze Team 30", opponentName: "Bronze Team 31",
  startTime: "2026-09-26T19:30:00+00:00",
});
const guardianSameTeamOffer = () => ({
  ...sameTeamOpportunity,
  game_id: "guardian-same-offer",
  enrollment_status: "offered",
  can_accept: false,
  can_withdraw: false,
  can_accept_offer: false,
  can_decline_offer: true,
});
const guardianDetailChoice = () => crossChoice({
  gameId: "guardian-detail-offer", targetId: "bronze-team-32",
  teamName: "Bronze Team 32", opponentName: "Bronze Team 33",
  startTime: "2026-09-27T19:30:00+00:00",
});
const guardianDetailOffer = () => focusedCrossTeamOffer({
  gameId: "guardian-detail-offer", targetId: "bronze-team-32",
  teamName: "Bronze Team 32", opponentName: "Bronze Team 33",
  startTime: "2026-09-27T19:30:00+00:00",
});

async function checkViewport(browser, viewport) {
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
  const enrollBodies = [];
  const withdrawBodies = [];
  const sameTeamEnrollBodies = [];
  const detailReads = [];
  const raceEnrollBodies = [];
  const errorEnrollBodies = [];
  const raceDetailReads = [];
  const identityEnrollBodies = [];
  const offerDetailReads = [];
  const offerAcceptBodies = [];
  const offerDeclineBodies = [];
  const focusedOfferDetailReads = [];
  const sameViewAcceptBodies = [];
  const refusedAcceptBodies = [];
  const vanishedAcceptBodies = [];
  const orphanAcceptBodies = [];
  const expiredDeclineBodies = [];
  const focusEnrollBodies = [];
  const guardianInlineAcceptBodies = [];
  const guardianSameDeclineBodies = [];
  const guardianDetailDeclineBodies = [];
  const guardianDetailReads = [];
  let enrolled = false;
  let crossVisible = true;
  let raceEnrolledTarget = null;
  let offerResolved = false;
  let sameViewOfferResolved = false;
  let vanishedOfferRemoved = false;
  let orphanOfferResolved = false;
  let expiredOfferResolved = false;
  let focusCanonicalEnrolled = false;
  let guardianInlineResolved = false;
  let guardianSameResolved = false;
  let guardianDetailResolved = false;
  let notificationReads = 0;
  let expectedConflictInFlight = false;
  let holdNextPlayerHomeRead = false;
  let heldPlayerHomeReads = 0;
  let holdBackPlayerHomeRead = false;
  let heldBackPlayerHomeReads = 0;
  let releaseRaceResponse;
  let releaseErrorResponse;
  let releaseWithdrawResponse;
  let releaseOfferResponse;
  let releaseIdentityResponse;
  let releasePlayerHomeRead;
  let releaseFocusResponse;
  let releaseOrphanOfferResponse;
  let releaseBackPlayerHomeRead;
  const raceResponseGate = new Promise((resolve) => {
    releaseRaceResponse = resolve;
  });
  const errorResponseGate = new Promise((resolve) => {
    releaseErrorResponse = resolve;
  });
  const withdrawResponseGate = new Promise((resolve) => {
    releaseWithdrawResponse = resolve;
  });
  const offerResponseGate = new Promise((resolve) => {
    releaseOfferResponse = resolve;
  });
  const identityResponseGate = new Promise((resolve) => {
    releaseIdentityResponse = resolve;
  });
  const playerHomeReadGate = new Promise((resolve) => {
    releasePlayerHomeRead = resolve;
  });
  const focusResponseGate = new Promise((resolve) => {
    releaseFocusResponse = resolve;
  });
  const orphanOfferResponseGate = new Promise((resolve) => {
    releaseOrphanOfferResponse = resolve;
  });
  const backPlayerHomeReadGate = new Promise((resolve) => {
    releaseBackPlayerHomeRead = resolve;
  });
  page.on("pageerror", (e) => errors.push(`[pageerror] ${e.message}`));
  page.on("console", (m) => {
    if (m.type() !== "error") return;
    // Chromium reports the deliberately mocked 409 below as a console-level
    // resource error. The response body, canonical rollback, toast and focus
    // are asserted directly; do not misclassify that expected refusal as an
    // unrelated page error.
    if (expectedConflictInFlight
        && /status of (409 \(Conflict\)|404 \(Not Found\))/.test(m.text())) return;
    errors.push(`[console] ${m.text()}`);
  });
  const fail = (msg) => { throw new Error(`[${viewport.label}] ${msg}`); };

  const optIn = (gameId, targetId) => page.locator(
    `[data-ph-sub-optin][data-ph-sub-game="${gameId}"][data-ph-sub-target="${targetId}"]`);
  const details = (gameId, targetId) => page.locator(
    `[data-ph-view-opp="${gameId}"][data-ph-opp-target="${targetId}"]`);
  const optInRow = (box) => box.locator(
    "xpath=ancestor::div[contains(concat(' ', normalize-space(@class), ' '), ' ph-sub-row ')][1]");
  const switchTo = async (tab, expectedView = tab) => {
    await page.evaluate((name) => {
      const button = document.querySelector(`.tab[data-tab="${name}"]`);
      if (!button) throw new Error(`Missing ${name} navigation control`);
      button.click();
    }, tab);
    await page.waitForFunction(
      (name) => document.body.dataset.view === name,
      expectedView, { timeout: 10000 });
  };

  const opportunities = () => {
    const race4 = raceChoice4();
    const race5 = raceChoice5();
    const raceRows = !raceEnrolledTarget
      ? [race4, race5]
      : [enrolledChoice(
          raceEnrolledTarget === race4.target_team_id ? race4 : race5)];
    return [
      ...(crossVisible ? [crossOpportunity(enrolled)] : []),
      ...raceRows,
      errorChoice(),
      identityChoice(),
      focusCanonicalEnrolled
        ? enrolledChoice(focusChoice(true)) : focusChoice(false),
      repeatedMatchupChoice(),
      lockedCleanupChoice(),
      sameTeamOpportunity,
    ];
  };

  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.route("**/api/**", async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      const p = url.pathname;
      if (p === "/api/auth/roles") {
        return json(route, { roles: [{ id: "player", label: "Player", permissions: [] }] });
      }
      if (p === "/api/auth/accounts") return json(route, { accounts: [] });
      if (p === "/api/status") {
        return json(route, { app_mode: "production", store: "memory",
          email_mode: "dry_run", push_mode: "dry_run" });
      }
      if (p === "/api/auth/me") {
        return json(route, { user: {
          username: "bronze_player", role: "player", label: "Player",
          scope: { player_id: "player-1", player_name: "Bronze One Player",
            team_id: "bronze-team-1", team_name: "Bronze Team 1" },
        } });
      }
      if (p === "/api/context/options") {
        return json(route, { programs: [], selected: {}, context_epoch: "ui-fixture" });
      }
      if (p === "/api/demo/overview") return json(route, { schedule: [] });
      if (p === "/api/notifications") {
        notificationReads += 1;
        return json(route, { notifications: [], unread: 0 });
      }
      if (p === "/api/me/guardian/home" && request.method() === "GET") {
        return json(route, { juniors: [{
          player_id: "junior-1",
          player_name: "Junior One",
          next_game: null,
          substitute_offers: [
            ...(guardianInlineResolved ? [] : [guardianInlineOffer()]),
            ...(guardianSameResolved ? [] : [guardianSameTeamOffer()]),
          ],
          substitute_opportunities: guardianDetailResolved
            ? [] : [guardianDetailChoice()],
        }] });
      }
      if (p === "/api/me/guardian/junior-1/substitute-opportunities/guardian-detail-offer"
          && request.method() === "GET") {
        guardianDetailReads.push(url.search);
        return json(route, guardianDetailOffer());
      }
      if (p === "/api/me/guardian/junior-1/substitute-opportunities/guardian-inline-offer/accept-offer"
          && request.method() === "POST") {
        guardianInlineAcceptBodies.push(request.postDataJSON());
        guardianInlineResolved = true;
        return json(route, { game_id: "guardian-inline-offer",
          target_team_id: "bronze-team-30", status: "accepted", cross_team: true });
      }
      if (p === "/api/me/guardian/junior-1/substitute-opportunities/guardian-same-offer/decline-offer"
          && request.method() === "POST") {
        guardianSameDeclineBodies.push(request.postDataJSON());
        guardianSameResolved = true;
        return json(route, { game_id: "guardian-same-offer",
          status: "declined", cross_team: false });
      }
      if (p === "/api/me/guardian/junior-1/substitute-opportunities/guardian-detail-offer/decline-offer"
          && request.method() === "POST") {
        guardianDetailDeclineBodies.push(request.postDataJSON());
        guardianDetailResolved = true;
        return json(route, { game_id: "guardian-detail-offer",
          target_team_id: "bronze-team-32", status: "declined", cross_team: true });
      }
      if (p === "/api/me/player-home") {
        if (holdBackPlayerHomeRead) {
          holdBackPlayerHomeRead = false;
          heldBackPlayerHomeReads += 1;
          await backPlayerHomeReadGate;
        }
        if (holdNextPlayerHomeRead) {
          holdNextPlayerHomeRead = false;
          heldPlayerHomeReads += 1;
          await playerHomeReadGate;
        }
        return json(route, {
          player_id: "player-1", player_name: "Bronze One Player",
          next_game: null, today_count: 0,
          substitute_offers: [
            ...(offerResolved ? [] : [crossTeamOffer()]),
            ...(sameViewOfferResolved ? [] : [sameViewOffer()]),
            refusedOffer(),
            ...(vanishedOfferRemoved ? [] : [vanishedOffer()]),
            ...(orphanOfferResolved ? [] : [orphanFocusOffer()]),
            ...(expiredOfferResolved ? [] : [expiredOffer()]),
          ],
          substitute_opportunities: opportunities(),
          unread_notifications: 0,
        });
      }
      if (p === "/api/me/substitute-opportunities/game-cross"
          && request.method() === "GET") {
        detailReads.push(url.search);
        return json(route, {
          ...crossOpportunity(enrolled),
          can_accept: !enrolled,
          can_accept_offer: false,
          can_decline_offer: false,
          blocked_reason: null,
          // Deliberately NO roster_status, team_status or open-slot counts.
        });
      }
      if (p === "/api/me/substitute-opportunities/game-same"
          && request.method() === "GET") {
        detailReads.push(url.search);
        return json(route, {
          ...sameTeamOpportunity,
          enrollment_status: null, can_accept: true, can_withdraw: false,
          can_accept_offer: false, can_decline_offer: false,
          blocked_reason: null, roster_status: "full", team_status: "full",
          open_goalie_slots: 0, open_skater_slots: 0,
        });
      }
      if (p === "/api/me/substitute-opportunities/game-same/enroll"
          && request.method() === "POST") {
        sameTeamEnrollBodies.push(request.postDataJSON());
        return json(route, { error: { code: "conflict",
          message: "Same-team compatibility probe." } }, 409);
      }
      if (p === "/api/me/substitute-opportunities/game-race"
          && request.method() === "GET") {
        raceDetailReads.push(url.search);
        const target = url.searchParams.get("target_team_id");
        const choice = target === "bronze-team-5" ? raceChoice5() : raceChoice4();
        return json(route, {
          ...(raceEnrolledTarget === choice.target_team_id
            ? enrolledChoice(choice) : choice),
          can_accept: raceEnrolledTarget !== choice.target_team_id,
          can_accept_offer: false, can_decline_offer: false,
        });
      }
      if (p === "/api/me/substitute-opportunities/game-offer"
          && request.method() === "GET") {
        offerDetailReads.push(url.search);
        return json(route, crossTeamOffer());
      }
      const focusedOfferDetails = {
        "/api/me/substitute-opportunities/game-offer-success": sameViewOffer,
        "/api/me/substitute-opportunities/game-offer-error": refusedOffer,
        "/api/me/substitute-opportunities/game-offer-gone": vanishedOffer,
        "/api/me/substitute-opportunities/game-offer-orphan": orphanFocusOffer,
        "/api/me/substitute-opportunities/game-offer-expired": expiredOffer,
      };
      if (request.method() === "GET" && focusedOfferDetails[p]) {
        const gameId = p.split("/").pop();
        focusedOfferDetailReads.push([gameId, url.search]);
        if (gameId === "game-offer-gone" && vanishedOfferRemoved) {
          return json(route, { error: { code: "not_found",
            message: "This offer no longer exists." } }, 404);
        }
        return json(route, focusedOfferDetails[p]());
      }
      if (p === "/api/me/substitute-opportunities/game-cross/enroll"
          && request.method() === "POST") {
        const body = request.postDataJSON();
        enrollBodies.push(body);
        enrolled = true;
        return json(route, { game_id: "game-cross", target_team_id: "bronze-team-4",
          status: "enrolled", cross_team: true });
      }
      if (p === "/api/me/substitute-opportunities/game-cross/withdraw"
          && request.method() === "POST") {
        const body = request.postDataJSON();
        withdrawBodies.push(body);
        await withdrawResponseGate;
        enrolled = false;
        if (withdrawBodies.length >= 2) crossVisible = false;
        return json(route, { game_id: "game-cross", target_team_id: "bronze-team-4",
          status: "withdrawn", cross_team: true });
      }
      if (p === "/api/me/substitute-opportunities/game-race/enroll"
          && request.method() === "POST") {
        const body = request.postDataJSON();
        raceEnrollBodies.push(body);
        await raceResponseGate;
        raceEnrolledTarget = body.target_team_id;
        return json(route, { game_id: "game-race",
          target_team_id: body.target_team_id, status: "enrolled", cross_team: true });
      }
      if (p === "/api/me/substitute-opportunities/game-error/enroll"
          && request.method() === "POST") {
        const body = request.postDataJSON();
        errorEnrollBodies.push(body);
        await errorResponseGate;
        return json(route, { error: { code: "conflict",
          message: "Roster changed; try again." } }, 409);
      }
      if (p === "/api/me/substitute-opportunities/game-identity/enroll"
          && request.method() === "POST") {
        identityEnrollBodies.push(request.postDataJSON());
        await identityResponseGate;
        return json(route, { error: { code: "conflict",
          message: "Old player refusal must stay private." } }, 409);
      }
      if (p === "/api/me/substitute-opportunities/game-focus/enroll"
          && request.method() === "POST") {
        focusEnrollBodies.push(request.postDataJSON());
        await focusResponseGate;
        return json(route, { error: { code: "conflict",
          message: "Focus test refusal." } }, 409);
      }
      if (p === "/api/me/substitute-opportunities/game-offer/accept-offer"
          && request.method() === "POST") {
        offerAcceptBodies.push(request.postDataJSON());
        await offerResponseGate;
        offerResolved = true;
        return json(route, { game_id: "game-offer",
          target_team_id: "bronze-team-10", status: "accepted", cross_team: true });
      }
      if (p === "/api/me/substitute-opportunities/game-offer/decline-offer"
          && request.method() === "POST") {
        offerDeclineBodies.push(request.postDataJSON());
        return json(route, { game_id: "game-offer",
          target_team_id: "bronze-team-10", status: "declined", cross_team: true });
      }
      if (p === "/api/me/substitute-opportunities/game-offer-success/accept-offer"
          && request.method() === "POST") {
        sameViewAcceptBodies.push(request.postDataJSON());
        sameViewOfferResolved = true;
        return json(route, { game_id: "game-offer-success",
          target_team_id: "bronze-team-14", status: "accepted", cross_team: true });
      }
      if (p === "/api/me/substitute-opportunities/game-offer-error/accept-offer"
          && request.method() === "POST") {
        refusedAcceptBodies.push(request.postDataJSON());
        return json(route, { error: { code: "conflict",
          message: "Offer changed; try again." } }, 409);
      }
      if (p === "/api/me/substitute-opportunities/game-offer-gone/accept-offer"
          && request.method() === "POST") {
        vanishedAcceptBodies.push(request.postDataJSON());
        vanishedOfferRemoved = true;
        return json(route, { error: { code: "conflict",
          message: "Offer changed; try again." } }, 409);
      }
      if (p === "/api/me/substitute-opportunities/game-offer-orphan/accept-offer"
          && request.method() === "POST") {
        orphanAcceptBodies.push(request.postDataJSON());
        await orphanOfferResponseGate;
        orphanOfferResolved = true;
        return json(route, { game_id: "game-offer-orphan",
          target_team_id: "bronze-team-20", status: "accepted", cross_team: true });
      }
      if (p === "/api/me/substitute-opportunities/game-offer-expired/decline-offer"
          && request.method() === "POST") {
        expiredDeclineBodies.push(request.postDataJSON());
        expiredOfferResolved = true;
        return json(route, { game_id: "game-offer-expired",
          target_team_id: "bronze-team-22", status: "expired", cross_team: true });
      }
      return json(route, {});
    });

    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForFunction(
      () => document.body.dataset.view === "player_home"
        && !!document.querySelector("[data-ph-sub-optin]"),
      null, { timeout: 10000 });

    const heading = await page.textContent("#content");
    if (!/Games you can sub in/.test(heading || "")) {
      fail("Player Home did not expose the requested cross-team section");
    }
    if (!/team will notify you if you are selected/i.test(heading || "")) {
      fail("opt-in helper text does not explain selection/notification");
    }
    if (!/Games you can sub in \(7\)/.test(heading || "")) {
      fail("cleanup-only game was counted as a current substitute choice");
    }
    const headingTags = await page.evaluate(() => ({
      offers: document.getElementById("ph-sub-offers-title")?.tagName,
      opportunities: document.getElementById("ph-sub-opportunities-title")?.tagName,
    }));
    if (headingTags.offers !== "H2" || headingTags.opportunities !== "H2") {
      fail(`substitute sections are missing semantic headings: ${JSON.stringify(headingTags)}`);
    }

    // The old UI sliced opportunities to three rows without a View-all path.
    // Pin every compound identity so adding rows cannot silently hide the
    // fourth or fifth actionable target.
    const initialKeys = await page.locator("[data-ph-sub-optin]").evaluateAll(
      (nodes) => nodes.map((node) =>
        `${node.dataset.phSubGame}|${node.dataset.phSubTarget}`).sort());
    const expectedInitialKeys = [
      "game-cross|bronze-team-4",
      "game-error|bronze-team-6",
      "game-focus|bronze-team-24",
      "game-identity|bronze-team-12",
      "game-locked|bronze-team-8",
      "game-repeat|bronze-team-4",
      "game-race|bronze-team-4",
      "game-race|bronze-team-5",
    ].sort();
    if (JSON.stringify(initialKeys) !== JSON.stringify(expectedInitialKeys)
        || initialKeys.length <= 3) {
      fail(`not every cross-team row rendered: ${JSON.stringify(initialKeys)}`);
    }
    for (const [targetId, targetName, opponentName] of [
      ["bronze-team-4", "Bronze Team 4", "Bronze Team 5"],
      ["bronze-team-5", "Bronze Team 5", "Bronze Team 4"],
    ]) {
      const namedDetails = optInRow(optIn("game-race", targetId)).getByRole(
        "button", { name: new RegExp(
          `Details.*${targetName}.*${opponentName}.*2026`, "i") });
      if (await namedDetails.count() !== 1) {
        fail(`same-game Details control does not identify ${targetName} and ${opponentName}`);
      }
    }
    const repeatedLabels = await Promise.all([
      details("game-race", "bronze-team-4").getAttribute("aria-label"),
      details("game-repeat", "bronze-team-4").getAttribute("aria-label"),
    ]);
    if (!repeatedLabels.every((label) => label && /2026/.test(label))
        || repeatedLabels[0] === repeatedLabels[1]) {
      fail(`repeated matchup Details names do not identify dates: ${JSON.stringify(repeatedLabels)}`);
    }

    let crossBox = optIn("game-cross", "bronze-team-4");
    if (await crossBox.count() !== 1) fail("cross-team checkbox is missing");
    const accessibleCrossBox = optInRow(crossBox).getByRole("checkbox", {
      name: /Sub for Bronze Team 4.*Bronze Team 5.*I can sub for Bronze Team 4/i,
    });
    if (await accessibleCrossBox.count() !== 1) {
      fail("cross-team checkbox accessible name omits the target or opponent");
    }
    if (await crossBox.isChecked()) fail("fresh cross-team choice rendered checked");
    const targetHeight = await crossBox.evaluate((el) =>
      el.closest("label").getBoundingClientRect().height);
    if (targetHeight < 44) fail(`checkbox label target is only ${targetHeight}px high`);

    // An already-enrolled row whose game is no longer mutable must not keep
    // advertising the positive "I can sub" action.  It remains checked as
    // history, disabled because cleanup is unavailable, and explains why.
    const lockedBox = optIn("game-locked", "bronze-team-8");
    const lockedCopy = await optInRow(lockedBox)
      .locator(".ph-sub-optin-text").textContent();
    if (!await lockedBox.isChecked() || !await lockedBox.isDisabled()
        || !/No longer available.*roster.*locked/i.test(lockedCopy || "")
        || /I can sub/i.test(lockedCopy || "")) {
      fail(`locked enrollment rendered an untruthful action: ${lockedCopy}`);
    }

    // WCAG AA normal-text contrast is evaluated from the browser's computed
    // styles, including the nearest opaque background, at both viewports.
    const contrastSamples = await page.evaluate(() => {
      const selectors = [
        "#ph-sub-offers-title", ".ph-sub-offers .li-sub",
        ".ph-sub-offers button.primary",
        "#ph-sub-opportunities-title", ".ph-sub-help",
        ".ph-sub-row .li-sub", ".ph-sub-optin-text",
      ];
      const opaqueBackground = (element) => {
        let node = element;
        while (node) {
          const color = getComputedStyle(node).backgroundColor;
          if (color !== "transparent"
              && !/^rgba\([^)]*,\s*0(?:\.0+)?\)$/.test(color)) return color;
          node = node.parentElement;
        }
        return getComputedStyle(document.body).backgroundColor;
      };
      return selectors.flatMap((selector) =>
        Array.from(document.querySelectorAll(selector), (element, index) => ({
          name: `${selector}[${index}]`,
          foreground: getComputedStyle(element).color,
          background: opaqueBackground(element),
        })));
    });
    for (const selector of [
      "#ph-sub-offers-title", ".ph-sub-offers .li-sub",
      ".ph-sub-offers button.primary",
      "#ph-sub-opportunities-title", ".ph-sub-help",
      ".ph-sub-row .li-sub", ".ph-sub-optin-text",
    ]) {
      if (!contrastSamples.some((sample) => sample.name.startsWith(`${selector}[`))) {
        fail(`contrast selector ${selector} matched no rendered text`);
      }
    }
    for (const sample of contrastSamples) {
      const ratio = contrastRatio(sample.foreground, sample.background);
      if (ratio < 4.5) {
        fail(`${sample.name} contrast is ${ratio.toFixed(2)}:1 `
          + `(${sample.foreground} on ${sample.background})`);
      }
    }

    // Existing same-team UI remains detail-first, not silently changed to the
    // proactive cross-team checkbox contract.
    const legacyButton = page.getByRole("button", { name: "View Opportunity" });
    if (await legacyButton.count() !== 1) {
      fail("legacy same-team View Opportunity control changed or disappeared");
    }
    if (await page.locator('[data-ph-sub-optin][data-ph-sub-target="bronze-team-1"]').count()) {
      fail("same-team opportunity was incorrectly converted to cross-team opt-in");
    }

    // Keyboard activation is the accessibility path and fires the real
    // onchange handler. Its request must carry target_team_id, not game alone,
    // and a same-view completion restores focus to the replacement control.
    const originalCrossBox = await crossBox.elementHandle();
    await crossBox.focus();
    await crossBox.press("Space");
    await page.waitForFunction(
      (original) => !original.isConnected, originalCrossBox, { timeout: 10000 });
    await page.waitForFunction(() => {
      const replacement = document.querySelector(
        '[data-ph-sub-optin][data-ph-sub-game="game-cross"][data-ph-sub-target="bronze-team-4"]');
      return !!replacement && replacement.checked
        && !replacement.closest(".ph-sub-row")?.hasAttribute("aria-busy");
    }, null, { timeout: 10000 });
    if (enrollBodies.length !== 1
        || JSON.stringify(enrollBodies[0]) !== JSON.stringify({ target_team_id: "bronze-team-4" })) {
      fail(`enroll did not send exact target identity: ${JSON.stringify(enrollBodies)}`);
    }
    crossBox = optIn("game-cross", "bronze-team-4");
    if (!await crossBox.evaluate((element) => document.activeElement === element)) {
      fail("same-view enroll did not restore focus to its replacement checkbox");
    }

    const crossDetailsButton = details("game-cross", "bronze-team-4");
    await crossDetailsButton.focus();
    await crossDetailsButton.press("Enter");
    await page.waitForFunction(
      () => /Substitute Opportunity/.test(document.getElementById("content")?.textContent || "")
        && !document.querySelector("#content .skeleton"),
      null, { timeout: 10000 });
    if (!await page.locator("#opp-detail-title").evaluate(
      (element) => element.tagName === "H2" && document.activeElement === element)) {
      fail("keyboard opening detail did not focus its semantic heading");
    }
    const crossDetail = (await page.textContent("#content")) || "";
    if (!/Bronze Team 4/.test(crossDetail) || !/Bronze Team 5/.test(crossDetail)) {
      fail("cross-team detail does not clearly name target and opponent");
    }
    if (/Team status|undefined/.test(crossDetail)) {
      fail(`privacy-minimal cross-team detail depended on omitted private fields: ${crossDetail}`);
    }
    if (detailReads.at(-1) !== "?target_team_id=bronze-team-4") {
      fail(`cross-team detail lost target query: ${JSON.stringify(detailReads)}`);
    }
    const detailContrast = await page.evaluate(() => {
      const selectors = [
        "#opp-detail-title", "#opp-detail-title ~ .card .li-sub",
      ];
      const opaqueBackground = (element) => {
        let node = element;
        while (node) {
          const color = getComputedStyle(node).backgroundColor;
          if (color !== "transparent"
              && !/^rgba\([^)]*,\s*0(?:\.0+)?\)$/.test(color)) return color;
          node = node.parentElement;
        }
        return getComputedStyle(document.body).backgroundColor;
      };
      return selectors.flatMap((selector) =>
        Array.from(document.querySelectorAll(selector), (element) => ({
          selector, foreground: getComputedStyle(element).color,
          background: opaqueBackground(element),
        })));
    });
    for (const selector of [
      "#opp-detail-title", "#opp-detail-title ~ .card .li-sub",
    ]) {
      if (!detailContrast.some((sample) => sample.selector === selector)) {
        fail(`detail contrast selector ${selector} matched no rendered text`);
      }
    }
    for (const sample of detailContrast) {
      const ratio = contrastRatio(sample.foreground, sample.background);
      if (ratio < 4.5) {
        fail(`${sample.selector} contrast is ${ratio.toFixed(2)}:1`);
      }
    }

    // Back clears both halves of the detail identity. Opening the legacy row
    // immediately afterward must not inherit Team 4's query parameter.
    await page.getByRole("button", { name: "Back to Home" }).click();
    await page.waitForFunction(
      () => !!document.querySelector(
        '[data-ph-sub-game="game-cross"][data-ph-sub-target="bronze-team-4"]'),
      null, { timeout: 10000 });
    const backTarget = await page.evaluate(() => ({
      game: document.activeElement?.dataset?.phSubGame
        || document.activeElement?.dataset?.phViewOpp,
      target: document.activeElement?.dataset?.phSubTarget
        || document.activeElement?.dataset?.phOppTarget,
    }));
    if (backTarget.game !== "game-cross" || backTarget.target !== "bronze-team-4") {
      fail(`Back did not restore the exact compound Home control: ${JSON.stringify(backTarget)}`);
    }
    await page.getByRole("button", { name: "View Opportunity" }).click();
    await page.waitForFunction(
      () => /Team status/.test(document.getElementById("content")?.textContent || ""),
      null, { timeout: 10000 });
    if (detailReads.at(-1) !== "") {
      fail(`legacy detail inherited stale target identity: ${JSON.stringify(detailReads)}`);
    }
    // Cross-team hardening must not change the established same-team route
    // contract: without a target side, the body remains exactly {}.
    expectedConflictInFlight = true;
    const sameTeamAccept = page.locator('[data-opp-accept="game-same"]');
    await sameTeamAccept.click();
    await page.waitForFunction(
      () => !!document.querySelector('[data-opp-accept="game-same"]:not(:disabled)')
        && /Same-team compatibility probe/.test(
          document.getElementById("toast-root")?.textContent || ""),
      null, { timeout: 10000 });
    expectedConflictInFlight = false;
    if (JSON.stringify(sameTeamEnrollBodies) !== JSON.stringify([{}])) {
      fail(`same-team action no longer sends an empty body: ${JSON.stringify(sameTeamEnrollBodies)}`);
    }
    await page.getByRole("button", { name: "Back to Home" }).click();
    await page.waitForFunction(
      () => !!document.querySelector('[data-ph-sub-game="game-error"]'),
      null, { timeout: 10000 });

    // A held server rejection exposes the intermediate live-region state,
    // returns to canonical unchecked state, and restores focus because the
    // initiating keyboard intent has not been superseded.
    let errorBox = optIn("game-error", "bronze-team-6");
    expectedConflictInFlight = true;
    await errorBox.focus();
    await errorBox.press("Space");
    await waitFor(() => errorEnrollBodies.length === 1,
      `[${viewport.label}] error fixture never received its enrollment request`);
    let pendingRow = optInRow(errorBox);
    let pendingStatus = pendingRow.locator(".ph-sub-optin-text");
    if (!await pendingRow.getAttribute("aria-busy")
        || await pendingStatus.getAttribute("aria-live") !== "polite"
        || !/Saving availability/.test((await pendingStatus.textContent()) || "")) {
      fail("held enrollment did not announce its Saving state");
    }
    // Start an independent game while this refusal is held. Per-game outcome
    // state must preserve the refusal after the other game later succeeds.
    let raceBox4 = optIn("game-race", "bronze-team-4");
    await raceBox4.evaluate((box) => {
      box.checked = true;
      box.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await waitFor(() => raceEnrollBodies.length === 1,
      `[${viewport.label}] concurrent race fixture never received its enrollment request`);
    const errorResponse = page.waitForResponse((response) =>
      new URL(response.url()).pathname
        === "/api/me/substitute-opportunities/game-error/enroll"
      && response.request().method() === "POST");
    releaseErrorResponse();
    await errorResponse;
    await page.waitForFunction(() => {
      const box = document.querySelector(
        '[data-ph-sub-game="game-error"][data-ph-sub-target="bronze-team-6"]');
      return !!box && !box.checked && !box.closest(".ph-sub-row")?.hasAttribute("aria-busy");
    }, null, { timeout: 10000 });
    expectedConflictInFlight = false;
    if (JSON.stringify(errorEnrollBodies) !== JSON.stringify([
      { target_team_id: "bronze-team-6" },
    ])) {
      fail(`error path sent the wrong request(s): ${JSON.stringify(errorEnrollBodies)}`);
    }
    const errorResult = optInRow(optIn("game-error", "bronze-team-6"))
      .locator(".ph-sub-optin-text");
    if (await errorResult.getAttribute("role") !== "alert"
        || !/Roster changed; try again/.test((await errorResult.textContent()) || "")) {
      fail("server refusal was not announced on its compound choice");
    }
    errorBox = optIn("game-error", "bronze-team-6");
    if (!await errorBox.evaluate((element) => document.activeElement === element)) {
      fail("same-view refusal did not restore focus to its replacement checkbox");
    }

    // Hold target 4. Every control for the SAME game (target 4, target 5,
    // and both Details buttons) must lock. The ledger is module-scoped, so a
    // Notifications -> Home navigation must not recreate enabled controls.
    raceBox4 = optIn("game-race", "bronze-team-4");
    pendingRow = optInRow(raceBox4);
    pendingStatus = pendingRow.locator(".ph-sub-optin-text");
    if (await pendingRow.getAttribute("aria-busy") !== "true"
        || await pendingStatus.getAttribute("aria-live") !== "polite"
        || !/Saving availability/.test((await pendingStatus.textContent()) || "")) {
      fail("held same-game enrollment did not announce its Saving state");
    }
    for (const control of [
      optIn("game-race", "bronze-team-4"),
      optIn("game-race", "bronze-team-5"),
      details("game-race", "bronze-team-4"),
      details("game-race", "bronze-team-5"),
    ]) {
      if (!await control.isDisabled()) fail("a same-game sibling stayed enabled during save");
    }
    const immediateSiblingRow = optInRow(
      optIn("game-race", "bronze-team-5"));
    const immediateSiblingCopy = (await immediateSiblingRow
      .locator(".ph-sub-optin-text").textContent()) || "";
    if (await immediateSiblingRow.getAttribute("aria-busy") !== "true"
        || !/another team choice.*(?:sav|pending|progress)/i.test(
          immediateSiblingCopy)
        || /Saving availability|Removing availability/i.test(
          immediateSiblingCopy)) {
      fail(`immediate sibling lock was not announced truthfully: ${immediateSiblingCopy}`);
    }

    await switchTo("notifications");
    await switchTo("player_home");
    await page.waitForFunction(
      () => !!document.querySelector('[data-ph-sub-game="game-race"]'),
      null, { timeout: 10000 });
    for (const control of [
      optIn("game-race", "bronze-team-4"),
      optIn("game-race", "bronze-team-5"),
      details("game-race", "bronze-team-4"),
      details("game-race", "bronze-team-5"),
    ]) {
      if (!await control.isDisabled()) {
        fail("navigation-driven rerender reopened a same-game race control");
      }
    }
    if (!await optIn("game-race", "bronze-team-4").isChecked()
        || await optIn("game-race", "bronze-team-5").isChecked()) {
      fail("navigation-driven rerender replaced the pending target intent with stale canonical state");
    }
    const siblingPendingCopy = (await optInRow(
      optIn("game-race", "bronze-team-5"))
      .locator(".ph-sub-optin-text").textContent()) || "";
    if (/Saving availability|Removing availability/i.test(siblingPendingCopy)
        || !/another team choice.*(?:sav|pending|progress)/i.test(siblingPendingCopy)) {
      fail(`same-game sibling described the chosen target's write as its own: ${siblingPendingCopy}`);
    }
    if (!/Saving availability/.test((await optInRow(
      optIn("game-race", "bronze-team-4"))
      .locator(".ph-sub-optin-text").textContent()) || "")) {
      fail("navigation-driven rerender lost the pending Saving announcement");
    }

    // Browser-disabled controls cannot normally fire. Force both handlers to
    // prove the operation ledger is the authority rather than the DOM flag.
    await optIn("game-race", "bronze-team-5").evaluate((box) => {
      box.disabled = false;
      box.checked = true;
      box.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await details("game-race", "bronze-team-5").evaluate((button) => {
      button.disabled = false;
      button.click();
    });
    await delay(150);
    if (raceEnrollBodies.length !== 1) {
      fail(`same-game sibling issued a second request: ${JSON.stringify(raceEnrollBodies)}`);
    }
    if (raceDetailReads.length !== 0) {
      fail(`same-game Details bypassed the write lock: ${JSON.stringify(raceDetailReads)}`);
    }

    // A second navigation restores the genuine disabled DOM. Its focus is a
    // newer user intent than the original checkbox. Completing the old save
    // may rerender Home, but must neither steal focus nor revive target 5.
    await switchTo("notifications");
    await switchTo("player_home");
    const homeNav = page.locator('.tab[data-tab="player_home"]');
    await homeNav.focus();
    const raceResponse = page.waitForResponse((response) =>
      new URL(response.url()).pathname
        === "/api/me/substitute-opportunities/game-race/enroll"
      && response.request().method() === "POST");
    releaseRaceResponse();
    await raceResponse;
    await page.waitForFunction(() => {
      const chosen = document.querySelector(
        '[data-ph-sub-game="game-race"][data-ph-sub-target="bronze-team-4"]');
      return !!chosen && chosen.checked
        && !chosen.closest(".ph-sub-row")?.hasAttribute("aria-busy");
    }, null, { timeout: 10000 });
    if (JSON.stringify(raceEnrollBodies) !== JSON.stringify([
      { target_team_id: "bronze-team-4" },
    ])) {
      fail(`same-game target identity drifted: ${JSON.stringify(raceEnrollBodies)}`);
    }
    if (!await homeNav.evaluate((element) => document.activeElement === element)) {
      fail("an older save stole focus after navigation superseded its intent");
    }
    if (await optIn("game-race", "bronze-team-5").count()) {
      fail("canonical successful enrollment retained the sibling target action");
    }
    const preservedError = (await optInRow(
      optIn("game-error", "bronze-team-6"))
      .locator(".ph-sub-optin-text").textContent()) || "";
    if (!/Roster changed; try again/.test(preservedError)) {
      fail(`another game's success erased the earlier refusal: ${preservedError}`);
    }

    // A response belongs to the signed-in player who issued it. Hold a
    // refusal, switch to a different player without reloading the document,
    // then release it: neither the new player's page nor the original
    // player's later page may inherit the old private error.
    let identityBox = optIn("game-identity", "bronze-team-12");
    expectedConflictInFlight = true;
    await identityBox.focus();
    await identityBox.press("Space");
    await waitFor(() => identityEnrollBodies.length === 1,
      `[${viewport.label}] identity fixture never received its enrollment request`);
    await page.evaluate(async () => {
      setUser({
        username: "second_player", role: "player", label: "Second Player",
        scope: { player_id: "player-2", player_name: "Second Player",
          team_id: "bronze-team-2", team_name: "Bronze Team 2" },
      });
      await render();
    });
    await page.waitForFunction(
      () => document.body.dataset.view === "player_home"
        && !!document.querySelector('[data-ph-sub-game="game-identity"]'),
      null, { timeout: 10000 });
    const identityResponse = page.waitForResponse((response) =>
      new URL(response.url()).pathname
        === "/api/me/substitute-opportunities/game-identity/enroll"
      && response.request().method() === "POST");
    releaseIdentityResponse();
    await identityResponse;
    // The stale handler correctly returns without rendering. Force an
    // unrelated render owned by player 2 so a mutant that used global post()
    // cannot hide its leaked module-level toast behind the already-painted
    // DOM and then have the later identity reset erase the evidence.
    await page.evaluate(() => render());
    await page.waitForFunction(
      () => document.body.dataset.view === "player_home"
        && !!document.querySelector('[data-ph-sub-game="game-identity"]'),
      null, { timeout: 10000 });
    const secondPlayerPage = `${await page.textContent("#content") || ""} `
      + `${await page.textContent("#toast-root") || ""}`;
    if (/Old player refusal must stay private/.test(secondPlayerPage)) {
      fail("a departed player's held refusal leaked to the next identity");
    }
    expectedConflictInFlight = false;
    if (JSON.stringify(identityEnrollBodies) !== JSON.stringify([
      { target_team_id: "bronze-team-12" },
    ])) {
      fail(`identity path sent the wrong request(s): ${JSON.stringify(identityEnrollBodies)}`);
    }
    await page.evaluate(async () => {
      setUser({
        username: "bronze_player", role: "player", label: "Player",
        scope: { player_id: "player-1", player_name: "Bronze One Player",
          team_id: "bronze-team-1", team_name: "Bronze Team 1" },
      });
      await render();
    });
    await page.waitForFunction(
      () => document.body.dataset.view === "player_home"
        && !!document.querySelector('[data-ph-sub-game="game-identity"]'),
      null, { timeout: 10000 });
    const restoredPlayerPage = `${await page.textContent("#content") || ""} `
      + `${await page.textContent("#toast-root") || ""}`;
    if (/Old player refusal must stay private/.test(restoredPlayerPage)) {
      fail("a superseded refusal reappeared after returning to its old identity");
    }

    // A newly focused Home control may be destroyed by the canonical render
    // which follows a held checkbox POST. Preserve it when possible and use
    // the stable section heading when the exact control no longer exists.
    // The same fixture also proves a notice invalidated by contradictory
    // canonical state can never resurrect on a later matching state.
    let focusBox = optIn("game-focus", "bronze-team-24");
    expectedConflictInFlight = true;
    await focusBox.focus();
    await focusBox.press("Space");
    await waitFor(() => focusEnrollBodies.length === 1,
      `[${viewport.label}] focus fixture never received its enrollment request`);
    const newerNotificationsRow = page.locator(
      '#content [data-goto="notifications"]').first();
    await newerNotificationsRow.focus();
    if (!await newerNotificationsRow.evaluate(
      (element) => document.activeElement === element)) {
      fail("could not establish newer in-content focus during held checkbox POST");
    }
    const focusResponse = page.waitForResponse((response) =>
      new URL(response.url()).pathname
        === "/api/me/substitute-opportunities/game-focus/enroll"
      && response.request().method() === "POST");
    releaseFocusResponse();
    await focusResponse;
    await page.waitForFunction(() => {
      const box = document.querySelector(
        '[data-ph-sub-game="game-focus"][data-ph-sub-target="bronze-team-24"]');
      return !!box && !box.checked
        && !box.closest(".ph-sub-row")?.hasAttribute("aria-busy");
    }, null, { timeout: 10000 });
    expectedConflictInFlight = false;
    if (!await page.locator("#ph-sub-opportunities-title").evaluate(
      (element) => document.activeElement === element)) {
      fail("checkbox completion left focus orphaned after replacing a newer Home control");
    }
    let focusCopy = (await optInRow(optIn("game-focus", "bronze-team-24"))
      .locator(".ph-sub-optin-text").textContent()) || "";
    if (!/Focus test refusal/.test(focusCopy)) {
      fail(`focused checkbox refusal was not announced: ${focusCopy}`);
    }
    if (JSON.stringify(focusEnrollBodies) !== JSON.stringify([
      { target_team_id: "bronze-team-24" },
    ])) {
      fail(`focus fixture lost target identity: ${JSON.stringify(focusEnrollBodies)}`);
    }
    focusCanonicalEnrolled = true;
    await switchTo("notifications");
    await switchTo("player_home");
    focusBox = optIn("game-focus", "bronze-team-24");
    focusCopy = (await optInRow(focusBox)
      .locator(".ph-sub-optin-text").textContent()) || "";
    if (!await focusBox.isChecked() || /Focus test refusal/.test(focusCopy)) {
      fail(`contradictory canonical enrollment kept a stale refusal: ${focusCopy}`);
    }
    focusCanonicalEnrolled = false;
    await switchTo("notifications");
    await switchTo("player_home");
    focusBox = optIn("game-focus", "bronze-team-24");
    focusCopy = (await optInRow(focusBox)
      .locator(".ph-sub-optin-text").textContent()) || "";
    if (await focusBox.isChecked() || /Focus test refusal/.test(focusCopy)) {
      fail(`invalidated refusal resurrected after a later canonical change: ${focusCopy}`);
    }

    const openFocusedOffer = async (gameId, targetId) => {
      const before = focusedOfferDetailReads.length;
      const control = details(gameId, targetId);
      await control.focus();
      await control.press("Enter");
      await page.waitForFunction(
        (id) => document.getElementById("opp-detail-title")?.tagName === "H2"
          && !!document.querySelector(
            `[data-opp-accept-offer="${id}"],`
            + `[data-opp-decline-offer="${id}"]`),
        gameId, { timeout: 10000 });
      if (!await page.locator("#opp-detail-title").evaluate(
        (element) => document.activeElement === element)) {
        fail(`keyboard opening ${gameId} did not focus its detail heading`);
      }
      const reads = focusedOfferDetailReads.slice(before);
      if (JSON.stringify(reads) !== JSON.stringify([
        [gameId, `?target_team_id=${targetId}`],
      ])) {
        fail(`${gameId} detail lost compound query identity: ${JSON.stringify(reads)}`);
      }
    };

    // Same-view success removes the offer, announces the result and focuses
    // the stable Home heading because the initiating action has no replacement.
    await openFocusedOffer("game-offer-success", "bronze-team-14");
    let focusedAction = page.locator(
      '[data-opp-accept-offer="game-offer-success"]');
    const focusedActionColors = await focusedAction.evaluate((element) => ({
      foreground: getComputedStyle(element).color,
      background: getComputedStyle(element).backgroundColor,
    }));
    if (contrastRatio(focusedActionColors.foreground,
      focusedActionColors.background) < 4.5) {
      fail(`detail Accept contrast is below 4.5:1: ${JSON.stringify(focusedActionColors)}`);
    }
    await focusedAction.focus();
    await focusedAction.press("Enter");
    await page.waitForFunction(
      () => !document.querySelector('[data-ph-view-opp="game-offer-success"]')
        && !!document.getElementById("ph-sub-opportunities-title"),
      null, { timeout: 10000 });
    if (!await page.locator("#ph-sub-opportunities-title").evaluate(
      (element) => document.activeElement === element)
        || !/Offer accepted/.test((await page.textContent("#toast-root")) || "")) {
      fail("same-view Accept did not publish success and restore stable focus");
    }

    // A 409 which leaves the detail actionable returns focus to the exact
    // action. Back then restores the exact compound Home control.
    await openFocusedOffer("game-offer-error", "bronze-team-16");
    expectedConflictInFlight = true;
    focusedAction = page.locator(
      '[data-opp-accept-offer="game-offer-error"]');
    await focusedAction.focus();
    await focusedAction.press("Enter");
    await page.waitForFunction(
      () => !!document.querySelector(
        '[data-opp-accept-offer="game-offer-error"]:not(:disabled)')
        && /Offer changed; try again/.test(
          document.getElementById("toast-root")?.textContent || ""),
      null, { timeout: 10000 });
    expectedConflictInFlight = false;
    focusedAction = page.locator(
      '[data-opp-accept-offer="game-offer-error"]');
    if (!await focusedAction.evaluate(
      (element) => document.activeElement === element)) {
      fail("same-view 409 did not return focus to the actionable Accept control");
    }
    const errorBack = page.getByRole("button", { name: "Back to Home" });
    await errorBack.focus();
    await errorBack.press("Enter");
    await page.waitForFunction(
      () => !!document.querySelector(
        '[data-ph-view-opp="game-offer-error"][data-ph-opp-target="bronze-team-16"]'),
      null, { timeout: 10000 });
    const errorBackFocus = await page.evaluate(() => ({
      game: document.activeElement?.dataset?.phViewOpp,
      target: document.activeElement?.dataset?.phOppTarget,
    }));
    if (errorBackFocus.game !== "game-offer-error"
        || errorBackFocus.target !== "bronze-team-16") {
      fail(`plain Back lost compound focus: ${JSON.stringify(errorBackFocus)}`);
    }

    // A 409 followed by a canonical 404 has no action replacement. The error
    // detail heading is the stable focus target, not BODY.
    await openFocusedOffer("game-offer-gone", "bronze-team-18");
    expectedConflictInFlight = true;
    focusedAction = page.locator(
      '[data-opp-accept-offer="game-offer-gone"]');
    await focusedAction.focus();
    await focusedAction.press("Enter");
    await page.waitForFunction(
      () => /Opportunity unavailable/.test(
        document.getElementById("content")?.textContent || "")
        && /This offer no longer exists/.test(
          document.getElementById("content")?.textContent || ""),
      null, { timeout: 10000 });
    expectedConflictInFlight = false;
    if (!await page.locator("#opp-detail-title").evaluate(
      (element) => element.tagName === "H2"
        && document.activeElement === element)) {
      fail("409 followed by canonical 404 did not focus the error heading");
    }
    await page.getByRole("button", { name: "Back to Home" }).click();
    await page.waitForFunction(
      () => !document.querySelector('[data-ph-view-opp="game-offer-gone"]')
        && !!document.getElementById("ph-sub-opportunities-title"),
      null, { timeout: 10000 });

    // If a held action returns after Back, a newly focused Home row may be
    // replaced by its canonical refresh. Re-anchor the orphan without stealing
    // any still-connected newer focus.
    await openFocusedOffer("game-offer-orphan", "bronze-team-20");
    focusedAction = page.locator(
      '[data-opp-accept-offer="game-offer-orphan"]');
    await focusedAction.focus();
    await focusedAction.press("Enter");
    await waitFor(() => orphanAcceptBodies.length === 1,
      `[${viewport.label}] orphan-focus offer never reached its held POST`);
    await page.getByRole("button", { name: "Back to Home" }).click();
    await page.waitForFunction(
      () => !!document.querySelector('[data-ph-view-opp="game-offer-orphan"]'),
      null, { timeout: 10000 });
    const newerHomeRow = page.locator(
      '#content [data-goto="notifications"]').first();
    await newerHomeRow.focus();
    const orphanResponse = page.waitForResponse((response) =>
      new URL(response.url()).pathname
        === "/api/me/substitute-opportunities/game-offer-orphan/accept-offer"
      && response.request().method() === "POST");
    releaseOrphanOfferResponse();
    await orphanResponse;
    await page.waitForFunction(
      () => !document.querySelector('[data-ph-view-opp="game-offer-orphan"]')
        && !!document.getElementById("ph-sub-opportunities-title"),
      null, { timeout: 10000 });
    if (!await page.locator("#ph-sub-opportunities-title").evaluate(
      (element) => document.activeElement === element)) {
      fail("held offer completion left newer replaced Home focus on BODY");
    }

    // An expired cross-team offer is cleanup, not a normal decline: only its
    // dismissal is exposed, its response remains target-bound, and the returned
    // EXPIRED status drives precise copy.
    await openFocusedOffer("game-offer-expired", "bronze-team-22");
    if (await page.locator(
      '[data-opp-accept-offer="game-offer-expired"]').count()) {
      fail("expired offer still exposed Accept");
    }
    const dismissExpired = page.getByRole("button", {
      name: "Dismiss Expired Offer",
    });
    if (await dismissExpired.count() !== 1) {
      fail("expired offer did not expose explicit dismissal copy");
    }
    await dismissExpired.focus();
    await dismissExpired.press("Enter");
    await page.waitForFunction(
      () => !document.querySelector('[data-ph-view-opp="game-offer-expired"]')
        && !!document.getElementById("ph-sub-opportunities-title"),
      null, { timeout: 10000 });
    if (!/Expired offer removed/.test(
      (await page.textContent("#toast-root")) || "")
        || !await page.locator("#ph-sub-opportunities-title").evaluate(
          (element) => document.activeElement === element)) {
      fail("expired dismissal did not announce cleanup and restore focus");
    }
    if (JSON.stringify(sameViewAcceptBodies) !== JSON.stringify([
      { target_team_id: "bronze-team-14" },
    ]) || JSON.stringify(refusedAcceptBodies) !== JSON.stringify([
      { target_team_id: "bronze-team-16" },
    ]) || JSON.stringify(vanishedAcceptBodies) !== JSON.stringify([
      { target_team_id: "bronze-team-18" },
    ]) || JSON.stringify(orphanAcceptBodies) !== JSON.stringify([
      { target_team_id: "bronze-team-20" },
    ]) || JSON.stringify(expiredDeclineBodies) !== JSON.stringify([
      { target_team_id: "bronze-team-22" },
    ])) {
      fail(`offer response bodies drifted: ${JSON.stringify({
        sameViewAcceptBodies, refusedAcceptBodies, vanishedAcceptBodies,
        orphanAcceptBodies, expiredDeclineBodies,
      })}`);
    }

    // A cross-team offer has two mutually exclusive response buttons. Hold
    // Accept, then prove Decline is locked by the same game-level ledger and
    // cannot be forced into a second request. Back and a full navigation cycle
    // must expose a disabled, truthfully labelled offer row. Finally, open a
    // different opportunity before the late response lands: the old action
    // must not close or repaint that newer detail.
    const offerView = details("game-offer", "bronze-team-10");
    if (await offerView.count() !== 1) fail("cross-team View Offer is missing");
    await offerView.click();
    await page.waitForFunction(
      () => /Substitute Offer/.test(document.getElementById("content")?.textContent || "")
        && !!document.querySelector("[data-opp-accept-offer]")
        && !!document.querySelector("[data-opp-decline-offer]"),
      null, { timeout: 10000 });
    if (JSON.stringify(offerDetailReads) !== JSON.stringify([
      "?target_team_id=bronze-team-10",
    ])) {
      fail(`cross-team offer detail lost target identity: ${JSON.stringify(offerDetailReads)}`);
    }
    let acceptOffer = page.locator('[data-opp-accept-offer="game-offer"]');
    await acceptOffer.click();
    await waitFor(() => offerAcceptBodies.length === 1,
      `[${viewport.label}] held offer never received its Accept request`);
    acceptOffer = page.locator('[data-opp-accept-offer="game-offer"]');
    let declineOffer = page.locator('[data-opp-decline-offer="game-offer"]');
    if (!await acceptOffer.isDisabled() || !await declineOffer.isDisabled()) {
      fail("held Accept Offer did not lock both mutually exclusive response buttons");
    }
    const offerPendingStatus = page.locator(
      '#content [role="status"][aria-live="polite"]');
    if (await offerPendingStatus.count() !== 1
        || !/Accepting your substitute offer/.test(
          (await offerPendingStatus.textContent()) || "")) {
      fail("held Accept Offer did not publish an accessible pending status");
    }
    await declineOffer.evaluate((button) => {
      button.disabled = false;
      button.click();
    });
    await delay(150);
    if (offerAcceptBodies.length !== 1 || offerDeclineBodies.length !== 0) {
      fail(`offer siblings issued competing requests: accept=${JSON.stringify(offerAcceptBodies)} `
        + `decline=${JSON.stringify(offerDeclineBodies)}`);
    }

    holdBackPlayerHomeRead = true;
    await page.getByRole("button", { name: "Back to Home" }).click();
    await waitFor(() => heldBackPlayerHomeReads === 1,
      `[${viewport.label}] Back never reached its held canonical Home read`);
    const newerHeaderFocus = page.locator('.tab[data-tab="notifications"]');
    await newerHeaderFocus.focus();
    releaseBackPlayerHomeRead();
    await page.waitForFunction(
      () => !!document.querySelector(
        '[data-ph-view-opp="game-offer"][data-ph-opp-target="bronze-team-10"]'),
      null, { timeout: 10000 });
    if (!await newerHeaderFocus.evaluate(
      (element) => document.activeElement === element)) {
      fail("held Back completion stole newer header focus");
    }
    let pendingOfferView = details("game-offer", "bronze-team-10");
    if (!await pendingOfferView.isDisabled()
        || !/Accepting/.test((await pendingOfferView.textContent()) || "")
        || await pendingOfferView.locator("xpath=ancestor::div[contains(concat(' ', normalize-space(@class), ' '), ' li ')][1]")
          .getAttribute("aria-busy") !== "true") {
      fail("Back to Home exposed an enabled or unlabelled in-flight offer");
    }
    await switchTo("notifications");
    await switchTo("player_home");
    pendingOfferView = details("game-offer", "bronze-team-10");
    if (!await pendingOfferView.isDisabled()
        || !/Accepting/.test((await pendingOfferView.textContent()) || "")) {
      fail("navigation-driven rerender reopened the held offer action");
    }
    await pendingOfferView.evaluate((button) => {
      button.disabled = false;
      button.click();
    });
    await delay(150);
    if (offerDetailReads.length !== 1) {
      fail(`held offer reopened its detail: ${JSON.stringify(offerDetailReads)}`);
    }

    await details("game-cross", "bronze-team-4").click();
    await page.waitForFunction(
      () => /Substitute Opportunity/.test(document.getElementById("content")?.textContent || "")
        && /Bronze Team 4/.test(document.getElementById("content")?.textContent || "")
        && !!document.querySelector("[data-opp-withdraw]"),
      null, { timeout: 10000 });
    const offerResponse = page.waitForResponse((response) =>
      new URL(response.url()).pathname
        === "/api/me/substitute-opportunities/game-offer/accept-offer"
      && response.request().method() === "POST");
    releaseOfferResponse();
    await offerResponse;
    await delay(150);
    const newerDetail = (await page.textContent("#content")) || "";
    if (!/Substitute Opportunity/.test(newerDetail)
        || !/Bronze Team 4/.test(newerDetail)
        || !await page.locator('[data-opp-withdraw="game-cross"]').count()) {
      fail("late offer completion closed or repainted a newer opportunity detail");
    }
    if (JSON.stringify(offerAcceptBodies) !== JSON.stringify([
      { target_team_id: "bronze-team-10" },
    ])
        || offerDeclineBodies.length !== 0) {
      fail(`offer action request contract drifted: accept=${JSON.stringify(offerAcceptBodies)} `
        + `decline=${JSON.stringify(offerDeclineBodies)}`);
    }
    await page.getByRole("button", { name: "Back to Home" }).click();
    await page.waitForFunction(
      () => !!document.querySelector('[data-ph-sub-game="game-cross"]')
        && !document.querySelector('[data-ph-view-opp="game-offer"]'),
      null, { timeout: 10000 });

    // Withdrawal is also held so its distinct Removing announcement is pinned.
    // Hold the canonical Home refresh after the POST, then focus Notifications
    // during that render. The newer intent must survive completion; a focus
    // listener/token removed before render would steal it back to the checkbox.
    let checkedBox = optIn("game-cross", "bronze-team-4");
    await checkedBox.focus();
    await checkedBox.press("Space");
    await waitFor(() => withdrawBodies.length === 1,
      `[${viewport.label}] withdraw fixture never received its request`);
    pendingRow = optInRow(checkedBox);
    pendingStatus = pendingRow.locator(".ph-sub-optin-text");
    if (await pendingRow.getAttribute("aria-busy") !== "true"
        || await pendingStatus.getAttribute("aria-live") !== "polite"
        || !/Removing availability/.test((await pendingStatus.textContent()) || "")) {
      fail("held withdrawal did not announce its Removing state");
    }
    const withdrawResponse = page.waitForResponse((response) =>
      new URL(response.url()).pathname
        === "/api/me/substitute-opportunities/game-cross/withdraw"
      && response.request().method() === "POST");
    holdNextPlayerHomeRead = true;
    releaseWithdrawResponse();
    await withdrawResponse;
    await waitFor(() => heldPlayerHomeReads === 1,
      `[${viewport.label}] completion did not reach the held canonical Home refresh`);
    const notificationsNav = page.locator('.tab[data-tab="notifications"]');
    await notificationsNav.focus();
    if (!await notificationsNav.evaluate((element) => document.activeElement === element)) {
      fail("test could not establish newer focus intent during the canonical refresh");
    }
    releasePlayerHomeRead();
    await page.waitForFunction(() => {
      const box = document.querySelector(
        '[data-ph-sub-game="game-cross"][data-ph-sub-target="bronze-team-4"]');
      return !!box && !box.checked
        && !box.closest(".ph-sub-row")?.hasAttribute("aria-busy");
    }, null, { timeout: 10000 });
    if (JSON.stringify(withdrawBodies) !== JSON.stringify([
      { target_team_id: "bronze-team-4" },
    ])) {
      fail(`withdraw lost target identity: ${JSON.stringify(withdrawBodies)}`);
    }
    if (!await notificationsNav.evaluate((element) => document.activeElement === element)) {
      fail("withdraw completion stole newer focus established during its Home refresh");
    }

    // When canonical refresh removes the focused row entirely, focus lands
    // on the section heading rather than disappearing to <body>. Re-enrol,
    // then make the fixture withdraw the opportunity on the second removal.
    checkedBox = optIn("game-cross", "bronze-team-4");
    await checkedBox.focus();
    await checkedBox.press("Space");
    await page.waitForFunction(() => {
      const box = document.querySelector(
        '[data-ph-sub-game="game-cross"][data-ph-sub-target="bronze-team-4"]');
      return !!box && box.checked
        && !box.closest(".ph-sub-row")?.hasAttribute("aria-busy");
    }, null, { timeout: 10000 });
    if (enrollBodies.length !== 2
        || JSON.stringify(enrollBodies[1])
          !== JSON.stringify({ target_team_id: "bronze-team-4" })) {
      fail(`re-enrol changed the target contract: ${JSON.stringify(enrollBodies)}`);
    }
    checkedBox = optIn("game-cross", "bronze-team-4");
    await checkedBox.focus();
    await checkedBox.press("Space");
    await page.waitForFunction(
      () => !document.querySelector('[data-ph-sub-game="game-cross"]'),
      null, { timeout: 10000 });
    if (JSON.stringify(withdrawBodies) !== JSON.stringify([
      { target_team_id: "bronze-team-4" },
      { target_team_id: "bronze-team-4" },
    ])) {
      fail(`final withdraw lost target identity: ${JSON.stringify(withdrawBodies)}`);
    }
    const opportunityHeading = page.locator("#ph-sub-opportunities-title");
    if (!await opportunityHeading.evaluate(
      (element) => document.activeElement === element)) {
      fail("removing the focused opportunity did not restore focus to its section");
    }

    const dimensions = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      clientWidth: document.documentElement.clientWidth,
    }));
    if (dimensions.scrollWidth > dimensions.clientWidth) {
      fail(`horizontal overflow ${dimensions.scrollWidth}px > ${dimensions.clientWidth}px`);
    }
    if (viewport.width === 390) {
      const clippedCopy = await page.locator(".ph-sub-choice-copy").evaluateAll(
        (nodes) => nodes.map((node) => ({
          text: (node.textContent || "").trim().slice(0, 40),
          scrollWidth: node.scrollWidth,
          clientWidth: node.clientWidth,
        })).filter((sample) => sample.scrollWidth > sample.clientWidth + 1));
      if (clippedCopy.length) {
        fail(`390px choice copy is clipped inside overflow-hidden card: ${JSON.stringify(clippedCopy)}`);
      }
    }

    // Guardian responses are the same compound operation performed on behalf
    // of a linked junior. Pin both the inline-offer path and the detail path;
    // the latter must carry target_team_id through the View control, live GET
    // query, and response button. A same-team guardian offer remains {}.
    await page.evaluate(async () => {
      setUser({
        id: "guardian-user", username: "guardian_user",
        role: "guardian", label: "Guardian",
      });
      await render();
    });
    await page.waitForFunction(
      () => document.body.dataset.view === "guardian_home"
        && !!document.querySelector(
          '[data-g-accept-offer="junior-1|guardian-inline-offer"]'),
      null, { timeout: 10000 });
    const guardianInlineAccept = page.locator(
      '[data-g-accept-offer="junior-1|guardian-inline-offer"]');
    const guardianInlineDecline = page.locator(
      '[data-g-decline-offer="junior-1|guardian-inline-offer"]');
    if (await guardianInlineAccept.getAttribute("data-g-opp-target")
          !== "bronze-team-30"
        || await guardianInlineDecline.getAttribute("data-g-opp-target")
          !== "bronze-team-30") {
      fail("guardian inline offer buttons lost target identity");
    }
    await guardianInlineAccept.click();
    await page.waitForFunction(
      () => !document.querySelector(
        '[data-g-accept-offer="junior-1|guardian-inline-offer"]'),
      null, { timeout: 10000 });
    if (JSON.stringify(guardianInlineAcceptBodies) !== JSON.stringify([
      { target_team_id: "bronze-team-30" },
    ])) {
      fail(`guardian inline accept lost target body: ${JSON.stringify(guardianInlineAcceptBodies)}`);
    }

    const guardianSameDecline = page.locator(
      '[data-g-decline-offer="junior-1|guardian-same-offer"]');
    if (await guardianSameDecline.getAttribute("data-g-opp-target") !== null) {
      fail("same-team guardian offer gained a cross-team target attribute");
    }
    await guardianSameDecline.click();
    await page.waitForFunction(
      () => !document.querySelector(
        '[data-g-decline-offer="junior-1|guardian-same-offer"]'),
      null, { timeout: 10000 });
    if (JSON.stringify(guardianSameDeclineBodies) !== JSON.stringify([{}])) {
      fail(`same-team guardian response changed its empty body: ${JSON.stringify(guardianSameDeclineBodies)}`);
    }

    const guardianDetailView = page.locator(
      '[data-g-view-opp="junior-1|guardian-detail-offer"]');
    if (await guardianDetailView.getAttribute("data-g-opp-target")
        !== "bronze-team-32") {
      fail("guardian detail control lost target identity");
    }
    await guardianDetailView.click();
    await page.waitForFunction(
      () => !!document.querySelector(
        '[data-g-decline-offer="junior-1|guardian-detail-offer"]'),
      null, { timeout: 10000 });
    if (JSON.stringify(guardianDetailReads) !== JSON.stringify([
      "?target_team_id=bronze-team-32",
    ])) {
      fail(`guardian detail GET lost target query: ${JSON.stringify(guardianDetailReads)}`);
    }
    const guardianDetailAccept = page.locator(
      '[data-g-accept-offer="junior-1|guardian-detail-offer"]');
    const guardianDetailDecline = page.locator(
      '[data-g-decline-offer="junior-1|guardian-detail-offer"]');
    if (await guardianDetailAccept.getAttribute("data-g-opp-target")
          !== "bronze-team-32"
        || await guardianDetailDecline.getAttribute("data-g-opp-target")
          !== "bronze-team-32") {
      fail("guardian detail response buttons lost target identity");
    }
    await guardianDetailDecline.click();
    await page.waitForFunction(
      () => !document.querySelector(
        '[data-g-view-opp="junior-1|guardian-detail-offer"]'),
      null, { timeout: 10000 });
    if (JSON.stringify(guardianDetailDeclineBodies) !== JSON.stringify([
      { target_team_id: "bronze-team-32" },
    ])) {
      fail(`guardian detail decline lost target body: ${JSON.stringify(guardianDetailDeclineBodies)}`);
    }

    if (errors.length) fail(`console/page errors:\n${errors.join("\n")}`);
    if (notificationReads < 5) {
      fail(`navigation/rerender coverage was not exercised (${notificationReads} notification reads)`);
    }
    console.log(`[${viewport.label}] OK — all cross-team rows render; held writes lock `
      + "same-game controls, announce progress, preserve current focus intent, and layout fits.");
  } catch (error) {
    throw new Error(`${error.message}\n--- server output ---\n${serverOutput}`);
  } finally {
    releaseRaceResponse();
    releaseErrorResponse();
    releaseWithdrawResponse();
    releaseOfferResponse();
    releaseIdentityResponse();
    releasePlayerHomeRead();
    releaseFocusResponse();
    releaseOrphanOfferResponse();
    releaseBackPlayerHomeRead();
    await context.close();
    await stopServer(server);
  }
}

async function main() {
  let browser;
  try {
    browser = await chromium.launch(
      process.env.SMOKE_CHROMIUM_PATH
        ? { executablePath: process.env.SMOKE_CHROMIUM_PATH } : {});
    for (const viewport of VIEWPORTS) await checkViewport(browser, viewport);
    console.log("Cross-team substitution Player Home journey passed.");
  } catch (error) {
    console.error("Cross-team substitution Player Home journey FAILED.");
    console.error(error && error.message ? error.message : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
