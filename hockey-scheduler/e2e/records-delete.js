// Setup Records delete-action coverage (#232): before this issue only the
// Clubs card in Setup > Records showed a trash button. Every other record
// card — Facility owners, Programs, Seasons, Leagues, Divisions, Teams,
// Venues, Rinks, Officials, Players — now wires the same delBtn()/confirm/
// blocked-modal machinery Clubs already had, and Officials/Players gained a
// brand-new backend delete contract (previously they had none at all).
//
// At desktop and 390px, a League Admin:
//   1. Builds one full chain (Organization -> Venue -> Rink, Program ->
//      Season -> League -> Division -> Club -> Team, plus an Official and
//      two Players) entirely through the real Setup > Records drawers, then
//      confirms every one of those 10 record kinds shows a delete control on
//      its row — and that Ice slots (intentionally managed only from the
//      Arena Calendar) shows none.
//   2. Cancels one confirmation (the Program) and confirms nothing changed.
//   3. Triggers one dependency-blocked delete (the Club, blocked by its
//      Team) and confirms zero mutation.
//   4. Proves the Official's new delete contract end to end: blocked by a
//      live availability window, then succeeds once that dependency is
//      cleared through the official's own supported cleanup route.
//   5. Proves the Player's new delete contract: an emailed Player with a
//      scoped account, device token, AND a disabled notification preference
//      is blocked by all four; each is cleared through its own supported
//      route — account/device-token deactivation, contact/preference
//      RETIREMENT (#232 review 4: never a delete, so nothing is erased);
//      the delete then succeeds, and the retired contact/preference are
//      proven to remain queryable history, not gone. A bare Player also
//      deletes successfully.
//
// Fails on any browser console/page error.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8195 },
  { label: "phone", width: 390, height: 844, port: 8196 },
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
  const consoleErrorHandler = (m) => { if (m.type() === "error") errors.push(`[console] ${m.text()}`); };
  page.on("pageerror", (e) => errors.push(`[pageerror] ${e.message}`));
  page.on("console", consoleErrorHandler);

  const createViaDrawer = async (key, fields, expectedUrl) => {
    await page.click(`.setup-card .sc-new[data-drawer="${key}"]`);
    await page.waitForSelector(".drawer[role=dialog]", { timeout: 10000 });
    for (const [id, value] of Object.entries(fields)) {
      const tag = await page.$eval(`#${id}`, (el) => el.tagName);
      if (tag === "SELECT") await page.selectOption(`#${id}`, value);
      else await page.fill(`#${id}`, value);
    }
    const resp = page.waitForResponse((r) =>
      r.url() === `${base}${expectedUrl}` && r.request().method() === "POST");
    await page.click(`[data-drawer-submit="${key}"]`);
    const body = await (await resp).json();
    if (body.error) {
      throw new Error(`[${viewport.label}] ${key} create failed: ${JSON.stringify(body.error)}`);
    }
    await page.waitForFunction(
      () => !document.querySelector(".drawer[role=dialog]"), null, { timeout: 10000 });
    return body;
  };

  // Clicks a row's delete control and, if `expectBlocked`, treats the
  // resulting 4xx as expected (deliberate) rather than a page bug.
  const clickDelete = async (kind, id) => {
    await page.click(`[data-del="${kind}"][data-del-id="${id}"]`);
    await page.waitForSelector(".modal.danger", { timeout: 10000 });
  };

  const confirmDelete = async (kind, id, { expectBlocked } = {}) => {
    const highRisk = await page.$("#del-confirm");
    if (highRisk) await page.fill("#del-confirm", "DELETE");
    const resp = page.waitForResponse((r) =>
      r.url() === `${base}/api/v2/setup/${kind}/${id}/delete` && r.request().method() === "POST");
    if (expectBlocked) page.off("console", consoleErrorHandler);
    await page.click("[data-del-confirm]");
    const body = await (await resp).json();
    if (expectBlocked) page.on("console", consoleErrorHandler);
    return body;
  };

  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });
    await page.click('.tab[data-tab="setup"]');
    await page.click('[data-setup-view="records"]');
    await page.waitForSelector(".setup-card", { timeout: 10000 });

    // (1) One full chain, entirely through the real Records drawers.
    const org = await createViaDrawer("organization",
      { "f-org": "Records Delete Org" }, "/api/v2/setup/organization");
    const program = await createViaDrawer("league",
      { "f-league": "Records Delete Program", "f-league-org": org.id }, "/api/v2/setup/program");
    const season = await createViaDrawer("season",
      { "f-season-league": program.id, "f-season": "2026-27" }, "/api/v2/setup/season");
    const league = await createViaDrawer("level",
      { "f-level-season": season.id, "f-level": "Adult League" }, "/api/v2/setup/league");
    const division = await createViaDrawer("division",
      { "f-div-league": league.id, "f-div": "Gold" }, "/api/v2/setup/division");
    const club = await createViaDrawer("club",
      { "f-club": "Records Delete Club" }, "/api/v2/setup/club");
    const team = await createViaDrawer("team",
      { "f-team-club": club.id, "f-team-league": program.id, "f-team": "Records Delete Team" },
      "/api/v2/setup/team");
    const venue = await createViaDrawer("venue",
      { "f-venue": "Records Delete Venue", "f-venue-org": org.id }, "/api/v2/setup/venue");
    const rink = await createViaDrawer("rink",
      { "f-rink-venue": venue.id, "f-rink": "Records Delete Rink" }, "/api/v2/setup/rink");
    const slot = await createViaDrawer("ice-slot", {
      "f-slot-rink": rink.id, "f-slot-date": "2026-09-05",
      "f-slot-start": "18:00", "f-slot-end": "19:00",
    }, "/api/v2/setup/ice-slot");
    const official = await createViaDrawer("official",
      { "f-official": "Records Delete Official", "f-official-club": club.id },
      "/api/v2/setup/official");
    const playerBlocked = await createViaDrawer("player", {
      "f-player-team": team.id, "f-player-name": "Blocked Player",
      "f-player-position": "forward", "f-player-email": "blocked-player@example.com",
    }, "/api/v2/setup/player");
    const playerBare = await createViaDrawer("player", {
      "f-player-team": team.id, "f-player-name": "Bare Player",
      "f-player-position": "forward",
    }, "/api/v2/setup/player");

    // The common real-world shape (#232 review): an emailed Player also
    // picks up a login account and a push device token — an account/token
    // scoped to it, prerequisites for the full blocked->cleared->succeeds
    // lifecycle proof below, so built via raw fetch like every other
    // journey's non-target prerequisites.
    const playerAccount = await page.evaluate(async (i) => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      return post("/api/accounts", {
        username: "records_delete_player", password: "a-real-password",
        role: "player", scope: { team_id: i.team, player_id: i.player },
      });
    }, { team: team.id, player: playerBlocked.id });
    if (playerAccount.error) {
      throw new Error(`[${viewport.label}] player account create failed: ` +
        `${JSON.stringify(playerAccount.error)}`);
    }
    const playerDevice = await page.evaluate(async (i) => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      return post("/api/notifications/device-tokens", {
        recipient_ref: `player:${i.player}`, provider: "fcm", token: "records-delete-tok",
      });
    }, { player: playerBlocked.id });
    if (playerDevice.error) {
      throw new Error(`[${viewport.label}] player device token create failed: ` +
        `${JSON.stringify(playerDevice.error)}`);
    }
    // An explicit opt-out (#232 review 4): a disabled notification
    // preference on the emailed Player, alongside its automatically-created
    // contact destination — both block the delete until explicitly retired.
    const playerPref = await page.evaluate(async (i) => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      return post("/api/notifications/preferences", {
        recipient_ref: `player:${i.player}`, channel: "email", enabled: false,
      });
    }, { player: playerBlocked.id });
    if (playerPref.error) {
      throw new Error(`[${viewport.label}] player notification preference create failed: ` +
        `${JSON.stringify(playerPref.error)}`);
    }

    // Every one of the 10 delete-capable Records kinds shows a delete
    // control on the row this journey just created for it.
    const expectPresent = [
      ["organization", org.id], ["league", program.id], ["season", season.id],
      ["level", league.id], ["division", division.id], ["club", club.id],
      ["team", team.id], ["venue", venue.id], ["rink", rink.id],
      ["official", official.id], ["player", playerBare.id],
    ];
    for (const [kind, id] of expectPresent) {
      await page.waitForSelector(`[data-del="${kind}"][data-del-id="${id}"]`, { timeout: 10000 });
    }
    // Ice slots are intentionally NOT duplicated in Records (managed only
    // from the Arena Calendar) — the freshly-created slot must not appear
    // as a deletable row here.
    if (await page.$('[data-del="ice-slot"]')) {
      throw new Error(`[${viewport.label}] Ice slots unexpectedly offers a Records delete control ` +
        `(slot ${slot.id})`);
    }

    // (2) Cancel one confirmation — nothing changes.
    await clickDelete("league", program.id);
    await page.click("button.act[data-modal-close]");
    await page.waitForSelector(".modal", { state: "detached", timeout: 10000 });
    await page.waitForSelector(`[data-del="league"][data-del-id="${program.id}"]`, { timeout: 10000 });

    // (3) One dependency-blocked delete — the Club is blocked by its Team.
    await clickDelete("club", club.id);
    let result = await confirmDelete("club", club.id, { expectBlocked: true });
    if (!result.error || result.error.code !== "has_dependencies") {
      throw new Error(`[${viewport.label}] expected the Club delete to be blocked, got ${JSON.stringify(result)}`);
    }
    await page.waitForSelector(".modal.blocked", { timeout: 10000 });
    const blockedText = (await page.textContent(".modal.blocked")) || "";
    if (!blockedText.toLowerCase().includes("team")) {
      throw new Error(`[${viewport.label}] blocked-Club modal doesn't mention the blocking team: ${blockedText}`);
    }
    await page.click("button.act[data-modal-close]");
    await page.waitForSelector(".modal", { state: "detached", timeout: 10000 });
    await page.waitForSelector(`[data-del="club"][data-del-id="${club.id}"]`, { timeout: 10000 });

    // (4) Official's new delete contract: blocked by a live availability
    // window (an official's own self-service action, out of THIS
    // regression's scope, so set up via a direct call to its own supported
    // route), then succeeds once that window is cleared through the
    // official's own supported cleanup route.
    const availId = await page.evaluate(async (officialId) => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      const av = await post(`/api/officials/${officialId}/availability`, {
        start_time: "2027-01-01T00:00:00+00:00", end_time: "2027-01-02T00:00:00+00:00",
        status: "unavailable",
      });
      return av.id;
    }, official.id);

    await clickDelete("official", official.id);
    result = await confirmDelete("official", official.id, { expectBlocked: true });
    if (!result.error || result.error.code !== "has_dependencies") {
      throw new Error(`[${viewport.label}] expected the Official delete to be blocked, got ${JSON.stringify(result)}`);
    }
    await page.waitForSelector(".modal.blocked", { timeout: 10000 });
    const officialBlockedText = (await page.textContent(".modal.blocked")) || "";
    if (!officialBlockedText.toLowerCase().includes("availability")) {
      throw new Error(`[${viewport.label}] blocked-Official modal doesn't mention the ` +
        `blocking availability window: ${officialBlockedText}`);
    }
    await page.click("button.act[data-modal-close]");
    await page.waitForSelector(".modal", { state: "detached", timeout: 10000 });

    await page.evaluate(async (id) => {
      await fetch(`/api/officials/availability/${id}/delete`, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: "{}",
      });
    }, availId);

    await clickDelete("official", official.id);
    result = await confirmDelete("official", official.id);
    if (result.error) {
      throw new Error(`[${viewport.label}] Official delete still blocked after clearing its ` +
        `availability window: ${JSON.stringify(result)}`);
    }
    await page.waitForSelector(".modal", { state: "detached", timeout: 10000 });
    await page.waitForFunction(
      (id) => !document.querySelector(`[data-del="official"][data-del-id="${id}"]`),
      official.id, { timeout: 10000 });

    // (5) Player's new delete contract, proven for the COMMON real-world
    // shape (#232 review) — not sidestepped by deleting a separate bare
    // Player instead: the emailed Player, with a scoped account, device
    // token, AND a disabled notification preference, is blocked by all
    // four; each is cleared through its own supported route (account/
    // device-token deactivation, contact/preference RETIREMENT — #232
    // review 4: never a delete, so nothing is erased and the delete itself
    // never silently cascades). The contact and preference are retired
    // through the real UI (#232 review 6) — a "Retire" control right in the
    // blocked-delete modal, not a raw fetch.
    await clickDelete("player", playerBlocked.id);
    result = await confirmDelete("player", playerBlocked.id, { expectBlocked: true });
    if (!result.error || result.error.code !== "has_dependencies") {
      throw new Error(`[${viewport.label}] expected the emailed Player delete to be blocked, ` +
        `got ${JSON.stringify(result)}`);
    }
    await page.waitForSelector(".modal.blocked", { timeout: 10000 });
    const playerBlockedText = (await page.textContent(".modal.blocked")) || "";
    for (const term of ["contact", "account", "device", "preference"]) {
      if (!playerBlockedText.toLowerCase().includes(term)) {
        throw new Error(`[${viewport.label}] blocked-Player modal doesn't mention the blocking ` +
          `${term} dependency: ${playerBlockedText}`);
      }
    }

    // Retire the contact destination through the modal's own "Retire"
    // button; capture its backend id from the button itself (the only
    // place the UI exposes it) for the later no-erase proof. Retiring one
    // dependency auto-retries the delete internally (still blocked by the
    // rest — an EXPECTED 409, so console-error capture is suspended around
    // the click exactly like confirmDelete's own expectBlocked path).
    // waitForSelector (not a single-shot page.$) for the button itself:
    // the retry's own re-render can still be in flight the instant the
    // ".modal.blocked" wrapper reappears, so polling for the actual
    // control avoids a race against that re-render.
    const contactRetireBtn = await page.waitForSelector(
      '.modal.blocked [data-retire-route="contacts"]', { timeout: 10000 })
      .catch(() => { throw new Error(`[${viewport.label}] blocked-Player modal has no Retire ` +
        `control for its contact destination`); });
    const contactId = await contactRetireBtn.getAttribute("data-retire-id");
    const retireContactResp = page.waitForResponse((r) =>
      r.url() === `${base}/api/notifications/contacts/${contactId}/active` &&
      r.request().method() === "POST");
    page.off("console", consoleErrorHandler);
    await contactRetireBtn.click();
    if ((await (await retireContactResp).json()).error) {
      page.on("console", consoleErrorHandler);
      throw new Error(`[${viewport.label}] retiring the Player's contact destination via the ` +
        `blocked modal failed`);
    }
    // The modal auto-retries the delete and refreshes with the remaining
    // blockers (account, device, preference) — still blocked.
    await page.waitForSelector(".modal.blocked", { timeout: 10000 });
    page.on("console", consoleErrorHandler);

    // Same for the notification preference.
    const prefRetireBtn = await page.waitForSelector(
      '.modal.blocked [data-retire-route="preferences"]', { timeout: 10000 })
      .catch(() => { throw new Error(`[${viewport.label}] blocked-Player modal has no Retire ` +
        `control for its notification preference`); });
    const prefId = await prefRetireBtn.getAttribute("data-retire-id");
    const retirePrefResp = page.waitForResponse((r) =>
      r.url() === `${base}/api/notifications/preferences/${prefId}/active` &&
      r.request().method() === "POST");
    page.off("console", consoleErrorHandler);
    await prefRetireBtn.click();
    if ((await (await retirePrefResp).json()).error) {
      page.on("console", consoleErrorHandler);
      throw new Error(`[${viewport.label}] retiring the Player's notification preference via the ` +
        `blocked modal failed`);
    }
    await page.waitForSelector(".modal.blocked", { timeout: 10000 });
    page.on("console", consoleErrorHandler);
    const stillBlockedText = (await page.textContent(".modal.blocked")) || "";
    if (stillBlockedText.toLowerCase().includes("contact") ||
        stillBlockedText.toLowerCase().includes("preference")) {
      throw new Error(`[${viewport.label}] blocked-Player modal still lists a retired dependency: ` +
        stillBlockedText);
    }
    await page.click("button.act[data-modal-close]");
    await page.waitForSelector(".modal", { state: "detached", timeout: 10000 });

    // Clear the two remaining dependencies (account/device-token
    // deactivation already has its own established UI elsewhere in the app;
    // this journey exercises it via its supported route directly, matching
    // every earlier round's convention).
    const cleared = await page.evaluate(async (i) => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      const acc = await post(`/api/accounts/${i.account}/active`, { active: false });
      const dev = await post(`/api/notifications/device-tokens/${i.device}/active`, { active: false });
      return { acc, dev };
    }, { account: playerAccount.id, device: playerDevice.id });
    if (cleared.acc.error || cleared.dev.error) {
      throw new Error(`[${viewport.label}] clearing the Player's remaining dependencies failed: ` +
        JSON.stringify(cleared));
    }

    await clickDelete("player", playerBlocked.id);
    result = await confirmDelete("player", playerBlocked.id);
    if (result.error) {
      throw new Error(`[${viewport.label}] the emailed Player delete still failed after clearing ` +
        `every dependency: ${JSON.stringify(result)}`);
    }
    await page.waitForSelector(".modal", { state: "detached", timeout: 10000 });
    await page.waitForFunction(
      (id) => !document.querySelector(`[data-del="player"][data-del-id="${id}"]`),
      playerBlocked.id, { timeout: 10000 });

    // No-erase proof (#232 review 4): the retired contact destination and
    // notification preference remain queryable history after the Player is
    // gone — never erased, never left as a dangling live row. Re-pointing
    // either at the now-dead ref is rejected.
    page.off("console", consoleErrorHandler);
    const historyProof = await page.evaluate(async (i) => {
      const post = async (p, b) => (await fetch(p, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b),
      })).json();
      const contactsResp = await fetch("/api/notifications/contacts", { credentials: "same-origin" });
      const contactsBody = await contactsResp.json();
      const contact = (contactsBody.contacts || [])
        .find((c) => c.id === i.contactId);
      const prefsResp = await fetch(
        `/api/notifications/preferences?recipient_ref=player:${i.player}`,
        { credentials: "same-origin" });
      const prefsBody = await prefsResp.json();
      const emailPref = (prefsBody.preferences || []).find((p) => p.channel === "email");
      const retry = await post("/api/notifications/preferences",
        { recipient_ref: `player:${i.player}`, channel: "email", enabled: false });
      return { contact, emailPref, retry };
    }, { player: playerBlocked.id, contactId });
    page.on("console", consoleErrorHandler);
    if (!historyProof.contact || historyProof.contact.active) {
      throw new Error(`[${viewport.label}] the deleted Player's contact destination is not ` +
        `queryable retired history: ${JSON.stringify(historyProof)}`);
    }
    if (!historyProof.emailPref || historyProof.emailPref.active) {
      throw new Error(`[${viewport.label}] the deleted Player's notification preference is not ` +
        `queryable retired history: ${JSON.stringify(historyProof)}`);
    }
    if (!historyProof.retry.error ||
        (historyProof.retry.error.details || {}).reason !== "scope_subject_missing") {
      throw new Error(`[${viewport.label}] re-setting a notification preference for the deleted ` +
        `Player was not rejected as expected: ${JSON.stringify(historyProof)}`);
    }

    // Post-delete reactivation rejection (#232 review 2): the device token
    // deactivated above to clear the way for the delete must not be
    // reactivatable now that its Player is gone — a dangling-identity hole
    // scoping the delete blocker to active-only tokens would otherwise
    // reopen.
    page.off("console", consoleErrorHandler);
    const reactivation = await page.evaluate(async (deviceId) => {
      const resp = await fetch(`/api/notifications/device-tokens/${deviceId}/active`, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ active: true }),
      });
      const body = await resp.json();
      return { status: resp.status, body };
    }, playerDevice.id);
    page.on("console", consoleErrorHandler);
    if (reactivation.status === 200 || !reactivation.body.error) {
      throw new Error(`[${viewport.label}] reactivating the deleted Player's device token ` +
        `unexpectedly succeeded: ${JSON.stringify(reactivation)}`);
    }
    if ((reactivation.body.error.details || {}).reason !== "scope_subject_missing") {
      throw new Error(`[${viewport.label}] reactivation was rejected for the wrong reason: ` +
        `${JSON.stringify(reactivation)}`);
    }

    await clickDelete("player", playerBare.id);
    result = await confirmDelete("player", playerBare.id);
    if (result.error) {
      throw new Error(`[${viewport.label}] bare Player delete failed: ${JSON.stringify(result)}`);
    }
    await page.waitForSelector(".modal", { state: "detached", timeout: 10000 });
    await page.waitForFunction(
      (id) => !document.querySelector(`[data-del="player"][data-del-id="${id}"]`),
      playerBare.id, { timeout: 10000 });

    if (errors.length) {
      throw new Error(`[${viewport.label}] console/page errors:\n${errors.join("\n")}`);
    }
    console.log(`[${viewport.label}] OK — every Records card offers delete, cancel/blocked/success ` +
      `flows verified, Official and Player's new delete contract proven end to end for the common ` +
      `real-world (account + device token + contact + preference) case, and the retired rows ` +
      `confirmed as queryable, unerased history.`);
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
    console.log("Records-delete browser journey passed.");
  } catch (error) {
    console.error("Records-delete browser journey FAILED.");
    console.error(error && error.message ? error.message : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
