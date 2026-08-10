// Scheduler preview surfaces already-scheduled pairings, not just games and
// conflicts (#206 slice 1 / #326, #328 review).
//
// renderScheduler() previously read only draft_games/created and unscheduled;
// a pairing the backend now reports in already_scheduled[] (#206 slice 1 —
// a real Game already exists for it, so it is neither proposed nor a
// conflict) was silently invisible, and an all-already-scheduled Division
// rendered the misleading generic "No games generated." At desktop and
// 390px, this journey proves two acceptance states on the real UI:
//   * MIXED   — a 4-team Division with 2 of its 6 round-robin pairings
//     already real Games: the preview shows 4 proposed games, 0 conflicts,
//     and the 2 already-scheduled pairings named with their existing Game
//     reference; commit stays ENABLED (there are 4 real games to commit).
//   * ALL-DONE — a 2-team Division whose one possible pairing already has a
//     real Game: the preview shows 0 games, 0 conflicts, 1 already
//     scheduled, an explanatory "already scheduled" message instead of the
//     generic empty state, and commit stays DISABLED.
// It also proves the operator-facing reaction to two stubbed commit-time
// refusals: a concurrent-commit race (pairing_already_scheduled) and a
// stale preview invalidated by a Game created/cancelled after Generate
// (preview_stale, #328 review round 5) -- both show an actionable message,
// clear the stale preview, and require a fresh Generate before retrying.
// Both stubs also capture and assert the real request body's
// draft_fingerprint against the preceding Generate response (#328 review
// round 8 finding 3), and the stale-preview one is triggered via keyboard
// Enter and asserts focus lands on the newly rendered Generate control
// rather than silently dropping to the document body (#328 review round 8
// finding 4).
// Finally, an UNSTUBBED scenario proves the real backend, not just a canned
// response: a brand-new team registers in a division after Generate but
// before Commit, and the real commit_draft_schedule refusal (preview_stale)
// is surfaced the same way, with zero Games created (#328 review round 10
// finding 1).
//
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
  { label: "desktop", width: 1440, height: 900, port: 8301 },
  { label: "phone", width: 390, height: 844, port: 8302 },
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

async function generateFor(page, divId, waitSelector) {
  await page.selectOption("#sched-div", divId);
  await page.click("[data-sched-generate]");
  await page.waitForSelector(waitSelector, { timeout: 15000 });
}

function previewState(page) {
  return page.evaluate(() => {
    const pv = document.querySelector("#sched-preview");
    if (!pv) return null;
    const commit = document.querySelector("[data-sched-commit]");
    // #328 review: structured per-row text (not just aggregate counts/text),
    // so callers can assert the EXACT pairing name + Game id a specific row
    // shows, not merely that the right number of rows exist.
    const rows = Array.from(pv.querySelectorAll(".card .li")).map((li) => ({
      title: ((li.querySelector(".li-title") || {}).textContent || "").trim(),
      sub: ((li.querySelector(".li-sub") || {}).textContent || "").trim(),
    }));
    return {
      games: pv.getAttribute("data-games"),
      conflicts: pv.getAttribute("data-conflicts"),
      alreadyScheduled: pv.getAttribute("data-already-scheduled"),
      commitPresent: !!commit,
      commitDisabled: commit ? commit.disabled : null,
      rows,
      text: pv.textContent.replace(/\s+/g, " ").trim(),
    };
  });
}

// Exact-match row lookup (#328 review): a pairing's rendered title must be
// EXACTLY "Home vs Away" and its sub-line must contain the given substring
// (e.g. naming its specific existing Game id) -- proves the right row, not
// merely that some row somewhere mentions "already scheduled".
function hasRow(rows, title, subIncludes) {
  return rows.some((r) => r.title === title && r.sub.includes(subIncludes));
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
    // The stubbed 409 in scenario (3) below deliberately triggers the
    // browser's own benign resource-load log for that response; it is not
    // an application error, so it must not fail the strict zero-error bar
    // the other two scenarios still enforce (mirrors api-error-resilience.js's
    // pageerror-only approach for its own intentional error responses).
    if (m.type() === "error" && !/Failed to load resource.*409/.test(m.text())) {
      errors.push(`[console] ${m.text()}`);
    }
  });

  const fail = (msg) => { throw new Error(`[${viewport.label}] ${msg}`); };

  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });
    await installContextFixture(page);
    // This journey never navigates the calendar, so its day only has to be
    // strictly in the FUTURE — step (5) deletes the leftover open slots, and
    // `delete_ice_slot` refuses anything at or before its clock ("past slots
    // are history"). The literal 2026-09-12 met that only because the date had
    // not arrived yet; a week out from the app's own current day always does,
    // at every hour, and comes from the app's clock rather than a second one
    // (#387/#389).
    const ICE_DAY = await page.evaluate(() => addDays(calendarDate, 7));

    // Build one League with two Divisions: a 4-team "Mixed" (six round-robin
    // pairings) and a 2-team "AllDone" (exactly one pairing).
    const ids = await page.evaluate(async (day) => {
      const F = window.hsFixture;
      // #409 EXPLICIT SELECTION on the V1 SURFACE. `POST /api/setup/league`
      // mints the PROGRAM (v1 calls it "league") and `POST /api/setup/season`
      // is a PROGRAM-AXIS create comparing the body's `league_id`
      // (server.py:3686), behind the same `setup_create_context_error`
      // preflight v2 uses (server.py:1160). Minting is not selecting.
      const league = await F.create("v1 league (the Program)", "/api/setup/league", { name: "Already-Scheduled Program" });
      await F.selectProgram("Program-only bootstrap", league.id);
      const season = await F.create("season", "/api/setup/season", { league_id: league.id, name: "Fall 2026" });
      // The v1 "level" IS the v2 League; it, the Divisions, the registrations
      // and the venue-access grant are SEASON-OWNED and land in THIS Season.
      await F.selectProgramSeason("Program+Season", league.id, season.id);
      const level = await F.create("level (the v2 League)", "/api/setup/level", { season_id: season.id, name: "Silver League" });
      const dMixed = await F.create("dMixed", "/api/setup/division", { season_id: season.id, level_id: level.id, name: "SilverMixed" });
      const dAllDone = await F.create("dAllDone", "/api/setup/division", { season_id: season.id, level_id: level.id, name: "SilverAllDone" });
      const club = await F.create("club", "/api/setup/club", { name: "Club" });
      const team = async (n) =>
        (await F.create("team", "/api/v2/setup/team", { club_id: club.id, league_id: level.id, name: n })).id;
      const m0 = await team("Mixed 0"), m1 = await team("Mixed 1");
      const m2 = await team("Mixed 2"), m3 = await team("Mixed 3");
      const a0 = await team("AllDone 0"), a1 = await team("AllDone 1");
      const register = (teamId, divisionId) => post(
        `/api/setup/seasons/${season.id}/team-registrations`,
        { team_id: teamId, division_id: divisionId });
      await register(m0, dMixed.id); await register(m1, dMixed.id);
      await register(m2, dMixed.id); await register(m3, dMixed.id);
      await register(a0, dAllDone.id); await register(a1, dAllDone.id);

      const venue = await F.create("venue", "/api/setup/venue", { name: "Arena", league_id: league.id });
      await F.call("season venue-access grant", `/api/v2/setup/seasons/${season.id}/venue-access`, { venue_id: venue.id });
      const rink = await F.create("rink", "/api/setup/rink", { venue_id: venue.id, name: "Rink 1" });
      const pad = (n) => String(n).padStart(2, "0");
      const slot = async (h) => (await F.create("ice-slot", "/api/setup/ice-slot", {
        rink_id: rink.id, start_time: `${day}T${pad(h)}:00:00+00:00`,
        end_time: `${day}T${pad(h + 1)}:00:00+00:00`, slot_type: "game",
      })).id;

      // Ask the scheduler which pairings the Mixed round robin actually
      // produces (no ice yet, so all six land in unscheduled) instead of
      // re-implementing the circle method here; pre-seed real Games for
      // the first two so the NEXT preview is genuinely mixed. Capture each
      // seeded pairing's names + Game id (#328 review) so the caller can
      // assert the EXACT already-scheduled rows the preview renders, not
      // just their count.
      // A draft PREVIEW returns no id — it is a computed response, not a
      // created record — so it is asserted with `F.call` (success, no id)
      // rather than `F.create` (#409).
      const bare = await F.call("scheduler draft preview", "/api/scheduler/draft",
        { division_id: dMixed.id });
      const toSeed = bare.unscheduled.slice(0, 2);
      const seededMixed = [];
      for (const pairing of toSeed) {
        const seedSlot = await slot(6 + toSeed.indexOf(pairing));
        const g = await F.create("g", "/api/setup/game", {
          season_id: season.id, division_id: dMixed.id,
          home_team_id: pairing.home_team_id, away_team_id: pairing.away_team_id,
          ice_slot_id: seedSlot,
        });
        seededMixed.push({
          home_team_name: pairing.home_team_name,
          away_team_name: pairing.away_team_name,
          game_id: g.id,
        });
      }
      // Ice for the four still-missing Mixed pairings -- these stay
      // AVAILABLE for the rest of the run (scenarios (3)/(4) only ever
      // stub the commit response, so nothing here ever really commits).
      const mixedOpenSlotIds = [];
      for (let h = 8; h < 8 + 4; h++) mixedOpenSlotIds.push(await slot(h));

      // AllDone's one pairing already has a real Game — no ice slot is
      // even needed for it to be picked up as already-scheduled.
      const allDoneSlot = await slot(20);
      const allDoneGame = await F.create("allDoneGame", "/api/setup/game", {
        season_id: season.id, division_id: dAllDone.id,
        home_team_id: a0, away_team_id: a1, ice_slot_id: allDoneSlot,
      });

      return {
        dMixed: dMixed.id, dAllDone: dAllDone.id, seededMixed,
        allDone: { home_team_name: "AllDone 0", away_team_name: "AllDone 1",
                  game_id: allDoneGame.id },
        seasonId: season.id, levelId: level.id, leagueId: league.id,
        mixedOpenSlotIds,
      };
    }, ICE_DAY);

    await page.waitForSelector('.tab[data-tab="scheduler"]', { state: "visible", timeout: 10000 });
    await page.click('.tab[data-tab="scheduler"]');
    await page.waitForSelector("#sched-div", { timeout: 10000 });
    await page.waitForFunction(
      (id) => !!document.querySelector(`#sched-div option[value="${id}"]`),
      ids.dMixed, { timeout: 10000 });

    // (1) Mixed: 4 missing pairings proposed, 2 already scheduled, 0 conflicts.
    await generateFor(page, ids.dMixed,
      '#sched-preview[data-games="4"][data-already-scheduled="2"]');
    let s = await previewState(page);
    if (s.conflicts !== "0") {
      fail(`mixed: expected 0 conflicts, got ${JSON.stringify(s)}`);
    }
    if (!/4 game\(s\), 0 conflict\(s\), 2 already scheduled/.test(s.text)) {
      fail(`mixed: header should name the already-scheduled count: ${s.text}`);
    }
    // #328 review: assert the EXACT two seeded pairings, each naming its OWN
    // existing Game id -- not just that some row somewhere says "already
    // scheduled" and the counts add up (which a duplicated or misattributed
    // row could also satisfy).
    for (const seeded of ids.seededMixed) {
      const title = `${seeded.home_team_name} vs ${seeded.away_team_name}`;
      if (!hasRow(s.rows, title, `Already scheduled — Game ${seeded.game_id}`)) {
        fail(`mixed: missing exact already-scheduled row "${title}" naming Game ${seeded.game_id}: ${JSON.stringify(s.rows)}`);
      }
    }
    const alreadyRows = s.rows.filter((r) => r.sub.includes("Already scheduled"));
    if (alreadyRows.length !== 2) {
      fail(`mixed: expected exactly 2 already-scheduled rows, got ${JSON.stringify(alreadyRows)}`);
    }
    // The two seeded pairings must never ALSO appear as proposed games.
    const gameRows = s.rows.filter((r) => !r.sub.includes("Already scheduled"));
    for (const seeded of ids.seededMixed) {
      const title = `${seeded.home_team_name} vs ${seeded.away_team_name}`;
      if (gameRows.some((r) => r.title === title)) {
        fail(`mixed: seeded pairing "${title}" must not also appear as a proposed game: ${JSON.stringify(gameRows)}`);
      }
    }
    if (s.commitPresent !== true || s.commitDisabled !== false) {
      fail(`mixed: commit must stay enabled with 4 real missing games: ${JSON.stringify(s)}`);
    }

    // (2) All-done: nothing missing, nothing to commit, but NOT the
    // misleading generic "No games generated." — an explanatory message
    // naming that the round robin is already fully scheduled.
    await generateFor(page, ids.dAllDone,
      '#sched-preview[data-games="0"][data-already-scheduled="1"]');
    s = await previewState(page);
    if (s.conflicts !== "0") {
      fail(`all-done: expected 0 conflicts, got ${JSON.stringify(s)}`);
    }
    if (/No games generated\./.test(s.text)) {
      fail(`all-done: must not show the generic "No games generated." message: ${s.text}`);
    }
    if (!/already scheduled/i.test(s.text) || !/nothing missing/i.test(s.text)) {
      fail(`all-done: missing explanatory "already scheduled" message: ${s.text}`);
    }
    // #328 review: assert the exact row, not just the aggregate count/text.
    const allDoneTitle = `${ids.allDone.home_team_name} vs ${ids.allDone.away_team_name}`;
    if (!hasRow(s.rows, allDoneTitle, `Already scheduled — Game ${ids.allDone.game_id}`)) {
      fail(`all-done: missing exact already-scheduled row "${allDoneTitle}" naming Game ${ids.allDone.game_id}: ${JSON.stringify(s.rows)}`);
    }
    if (s.commitDisabled !== true) {
      fail(`all-done: commit must stay disabled with nothing missing: ${JSON.stringify(s)}`);
    }

    // (3) Commit-time race, stubbed (#328 review round 3): genuinely
    // reproducing the concurrent-commit race in a single browser page
    // isn't practical, so /api/scheduler/commit is stubbed with the exact
    // shape a real pairing_already_scheduled refusal returns
    // (DomainError.to_dict(), HTTP 409) -- proving the UI reaction, not
    // the backend race itself (that is the forced two-session PostgreSQL
    // test in test_placement_concurrency.py). The message must be
    // actionable on its own: post()'s generic toast surfaces
    // error.message alone, never error.details.
    //
    // #328 review round 8 finding 3: the stub must not just answer any
    // request that arrives -- it must prove app.js actually SENT the
    // previewed draft_fingerprint. Without capturing and asserting the real
    // request body, removing or renaming that field in app.js would leave
    // this journey green while production returns preview_required.
    await generateFor(page, ids.dMixed,
      '#sched-preview[data-games="4"][data-already-scheduled="2"]');
    s = await previewState(page);
    if (s.commitDisabled !== false) {
      fail(`race stub: needs an enabled Commit to click: ${JSON.stringify(s)}`);
    }
    let expectedFingerprint = await page.evaluate(
      () => schedulerState.preview && schedulerState.preview.draft_fingerprint);
    if (!expectedFingerprint) {
      fail(`race stub: no draft_fingerprint on the Generate response to compare against: ${expectedFingerprint}`);
    }
    let sentFingerprint;
    await page.route("**/api/scheduler/commit", (r) => {
      const body = r.request().postDataJSON();
      sentFingerprint = body && body.draft_fingerprint;
      return r.fulfill({
        status: 409,
        contentType: "application/json",
        body: JSON.stringify({
          error: {
            code: "concurrency_conflict",
            message: "Mixed 0 vs Mixed 1 is already scheduled as Game "
              + "game_stub_race — generate a fresh preview before "
              + "committing again.",
            details: {
              reason: "pairing_already_scheduled",
              home_team_id: "stub_home", away_team_id: "stub_away",
              existing_game_id: "game_stub_race",
            },
          },
        }),
      });
    });
    await page.click("[data-sched-commit]");
    await page.waitForFunction(
      () => /Mixed 0 vs Mixed 1 is already scheduled as Game game_stub_race/
        .test((document.querySelector(".toast-msg") || {}).textContent || ""),
      null, { timeout: 10000 });
    // render() wipes #content to a loading skeleton SYNCHRONOUSLY, then does
    // several awaited fetches before the real content replaces it -- so
    // "#sched-preview detached" alone fires the moment the skeleton
    // appears, not once the real re-render (with a fresh, uncommittable
    // Generate-only state) has actually landed. Wait for that settled state
    // directly instead.
    await page.waitForFunction(
      () => !document.querySelector("[data-sched-commit]")
        && !!document.querySelector("[data-sched-generate]"),
      null, { timeout: 10000 });
    if (sentFingerprint !== expectedFingerprint) {
      fail(`race stub: Commit must POST the exact previewed draft_fingerprint `
        + `(${JSON.stringify(expectedFingerprint)}), got ${JSON.stringify(sentFingerprint)}`);
    }
    await page.unroute("**/api/scheduler/commit");

    // (4) Stale-preview TOCTOU gate, stubbed (#328 review round 5): a Game
    // created or cancelled in the window between Generate and Commit
    // invalidates the reviewed preview's fingerprint. Genuinely reproducing
    // that gap in a single browser page isn't practical either (the
    // backend regressions in test_scheduler.py cover the real
    // create/cancel-between-preview-and-commit scenarios directly against
    // the store), so /api/scheduler/commit is stubbed with the exact shape
    // a real preview_stale refusal returns -- proving the UI reaction: the
    // message is actionable on its own (post()'s generic toast surfaces
    // error.message alone, never error.details), the stale preview is
    // cleared, and Commit cannot be retried without a fresh Generate.
    //
    // #328 review round 8 finding 3: same request-capture requirement as
    // scenario (3) above -- prove app.js actually sent the previewed
    // draft_fingerprint, not just that SOME request arrived.
    //
    // #328 review round 8 finding 4: triggered with a keyboard Enter (not a
    // pointer click) on the focused Commit button, then asserts focus lands
    // on Generate once the terminal error clears the stale preview --
    // render() replaces #content wholesale, so the focused Commit button
    // is simply removed from the DOM; nothing otherwise moves focus
    // anywhere, silently dropping a keyboard user back to the document
    // body even though the live-region toast (outside #content, so it
    // survives) announced what to do next.
    await generateFor(page, ids.dMixed,
      '#sched-preview[data-games="4"][data-already-scheduled="2"]');
    s = await previewState(page);
    if (s.commitDisabled !== false) {
      fail(`stale-preview stub: needs an enabled Commit to click: ${JSON.stringify(s)}`);
    }
    expectedFingerprint = await page.evaluate(
      () => schedulerState.preview && schedulerState.preview.draft_fingerprint);
    if (!expectedFingerprint) {
      fail(`stale-preview stub: no draft_fingerprint on the Generate response to compare against: ${expectedFingerprint}`);
    }
    await page.route("**/api/scheduler/commit", (r) => {
      const body = r.request().postDataJSON();
      sentFingerprint = body && body.draft_fingerprint;
      return r.fulfill({
        status: 409,
        contentType: "application/json",
        body: JSON.stringify({
          error: {
            code: "concurrency_conflict",
            message: "This preview is out of date — a game may have been "
              + "added, cancelled, or otherwise changed since you "
              + "generated it. Generate a fresh preview and review it "
              + "before committing.",
            details: { reason: "preview_stale" },
          },
        }),
      });
    });
    await page.focus("[data-sched-commit]");
    if (!(await page.evaluate(
        () => document.activeElement.hasAttribute("data-sched-commit")))) {
      fail("stale-preview stub: Commit must be focusable to trigger it via keyboard");
    }
    await page.keyboard.press("Enter");
    await page.waitForFunction(
      () => /This preview is out of date/
        .test((document.querySelector(".toast-msg") || {}).textContent || ""),
      null, { timeout: 10000 });
    // render() wipes #content to a loading skeleton SYNCHRONOUSLY, then does
    // several awaited fetches before the real content (and the focus-restore
    // this scenario checks for) replaces it -- so "#sched-preview detached"
    // fires the moment the skeleton appears, long before the real re-render
    // completes. Waiting for the fresh Generate-only state directly (no
    // Commit button, since the preview was cleared) is true in neither the
    // pre-click nor the skeleton DOM, only once the real re-render lands.
    await page.waitForFunction(
      () => !document.querySelector("[data-sched-commit]")
        && !!document.querySelector("[data-sched-generate]"),
      null, { timeout: 10000 });
    if (sentFingerprint !== expectedFingerprint) {
      fail(`stale-preview stub: Commit must POST the exact previewed draft_fingerprint `
        + `(${JSON.stringify(expectedFingerprint)}), got ${JSON.stringify(sentFingerprint)}`);
    }
    const focusAfter = await page.evaluate(() => ({
      hasGenerateAttr: document.activeElement
        ? document.activeElement.hasAttribute("data-sched-generate") : false,
      tag: document.activeElement ? document.activeElement.tagName : null,
    }));
    if (!focusAfter.hasGenerateAttr) {
      fail(`stale-preview stub: focus must land on Generate after terminal `
        + `recovery, not silently drop to <${focusAfter.tag}>: ${JSON.stringify(focusAfter)}`);
    }
    await page.unroute("**/api/scheduler/commit");

    // (5) Real (UNSTUBBED) end-to-end proof that a team-eligibility change
    // between Generate and Commit is caught by the REAL backend and
    // correctly surfaced by the UI (#328 review round 10 finding 1).
    // Unlike scenarios (3)/(4) above, Commit here is NOT intercepted --
    // this is a genuine round trip through commit_draft_schedule's own
    // regeneration and fingerprint comparison, not a canned response.
    //
    // The fixture starts with an EVEN (2) team count -- round_robin_pairings'
    // circle method has no bye at all there, so with exactly one ice slot
    // the two teams' single pairing is what gets placed. Registering a
    // THIRD team that sorts alphabetically FIRST makes the round robin ODD
    // and gives THAT new team the round-0 bye, while the two original teams
    // shift up by exactly one array position each -- preserving their
    // mutual pairing byte-for-byte on Commit's own regeneration (the same
    // circle-method mechanic proven directly, with hand-picked ids, in
    // test_scheduler.py's `_stale_preview_refused_on_team_eligibility_change`
    // "register" case). This isolates the eligibility axis from the
    // (separately covered) placement axis: a fingerprint that only hashed
    // draft_games/already_scheduled would see no difference at all and let
    // Commit silently persist a batch the operator never reviewed the
    // three-team version of.
    //
    // Two setup quirks this depends on:
    // - /api/scheduler/draft draws game slots from the WHOLE store, not
    //   just this division's own rink -- the four still-AVAILABLE Mixed
    //   slots from setup above (scenarios (1)/(3)/(4) only ever preview or
    //   stub-fail Commit for that division, so they were never actually
    //   consumed) would otherwise leak into this fixture's round robin.
    // - Team ids are assigned sequentially as "team_N" and compared as
    //   STRINGS, not numbers: crossing from single- to double-digit (e.g.
    //   "team_9" -> "team_10") sorts the new double-digit id FIRST (its
    //   first differing character, '1', is less than '9'). One
    //   unregistered decoy team is created between the two original teams
    //   and the late third team specifically to land that crossing exactly
    //   where the new team needs to sort first.
    const elig = await page.evaluate(async ({ seasonId, levelId, leagueId, day, openSlotIds }) => {
      const F = window.hsFixture;
      for (const id of openSlotIds) {
        await F.call("delete", `/api/setup/ice-slot/${id}/delete`, {});
      }
      // #409: the Division, the two registrations and the venue-access grant
      // are SEASON-OWNED in this Season, and real UI work has intervened
      // since the bootstrap — state the tuple rather than assume it stands.
      await F.selectProgramSeason("Program+Season for the eligibility fixture",
        leagueId, seasonId);
      const division = await F.create("division", "/api/setup/division",
        { season_id: seasonId, level_id: levelId, name: "TeamEligibility" });
      const club = await F.create("club", "/api/setup/club", { name: "Eligibility Club" });
      const team = async (n) => (await post("/api/v2/setup/team",
        { club_id: club.id, league_id: levelId, name: n })).id;
      const e0 = await team("Eligibility 0");
      const e1 = await team("Eligibility 1");
      const register = (teamId) => post(
        `/api/setup/seasons/${seasonId}/team-registrations`,
        { team_id: teamId, division_id: division.id });
      await register(e0);
      await register(e1);
      const venue = await F.create("venue", "/api/setup/venue",
        { name: "Eligibility Arena", league_id: leagueId });
      await F.call("season venue-access grant", `/api/v2/setup/seasons/${seasonId}/venue-access`,
        { venue_id: venue.id });
      const rink = await F.create("rink", "/api/setup/rink",
        { venue_id: venue.id, name: "Eligibility Rink" });
      await F.call("ice-slot", "/api/setup/ice-slot", {
        rink_id: rink.id, start_time: `${day}T23:00:00+00:00`,
        end_time: `${day}T23:59:00+00:00`, slot_type: "game",
      });
      return { divisionId: division.id, clubId: club.id };
    }, {
      seasonId: ids.seasonId, levelId: ids.levelId, leagueId: ids.leagueId,
      day: ICE_DAY, openSlotIds: ids.mixedOpenSlotIds,
    });

    // The Scheduler tab's division list is captured at render time, not
    // re-fetched on every action -- switch away and back to force a fresh
    // render that picks up the just-created division.
    await page.click('.tab[data-tab="dashboard"]');
    await page.waitForSelector('.tab[data-tab="scheduler"]', { state: "visible", timeout: 10000 });
    await page.click('.tab[data-tab="scheduler"]');
    await page.waitForSelector("#sched-div", { timeout: 10000 });
    await page.waitForFunction(
      (id) => !!document.querySelector(`#sched-div option[value="${id}"]`),
      elig.divisionId, { timeout: 10000 });
    await generateFor(page, elig.divisionId,
      '#sched-preview[data-games="1"][data-already-scheduled="0"]');
    s = await previewState(page);
    if (s.commitDisabled !== false) {
      fail(`team-eligibility staleness: needs an enabled Commit to click: ${JSON.stringify(s)}`);
    }
    const placedRowBefore = s.rows.find((r) => !r.sub.includes("Already scheduled"));
    if (!placedRowBefore) {
      fail(`team-eligibility staleness: expected exactly one placed row: ${JSON.stringify(s)}`);
    }

    // One unregistered decoy team (see the digit-boundary note above), then
    // a third team that registers in the SAME division after Generate --
    // no stub, no interception, just a real backend write in the gap
    // between the operator's preview and clicking Commit.
    await page.evaluate(async ({ seasonId, divisionId, clubId, levelId, leagueId }) => {
      const F = window.hsFixture;
      // #409: the late registration below is SEASON-OWNED in this Season.
      await F.selectProgramSeason("Program+Season for the late registration",
        leagueId, seasonId);
      await F.call("decoy team", "/api/v2/setup/team",
        { club_id: clubId, league_id: levelId, name: "(decoy)" });
      const e2 = await F.create("late team Eligibility 2", "/api/v2/setup/team",
        { club_id: clubId, league_id: levelId, name: "Eligibility 2 (late)" });
      await F.call("team registration", `/api/setup/seasons/${seasonId}/team-registrations`,
        { team_id: e2.id, division_id: divisionId });
    }, {
      seasonId: ids.seasonId, divisionId: elig.divisionId,
      clubId: elig.clubId, levelId: ids.levelId, leagueId: ids.leagueId,
    });

    await page.click("[data-sched-commit]");
    await page.waitForFunction(
      () => /out of date/i.test((document.querySelector(".toast-msg") || {}).textContent || ""),
      null, { timeout: 10000 });
    await page.waitForFunction(
      () => !document.querySelector("[data-sched-commit]")
        && !!document.querySelector("[data-sched-generate]"),
      null, { timeout: 10000 });

    // Zero side effects: a fresh Generate for the SAME (now 3-team)
    // division must still show nothing already-scheduled -- if the
    // refused commit had silently persisted anyway, the previously
    // previewed pairing would show up as already-scheduled here. It must
    // also still place the EXACT SAME pairing on the exact same slot
    // (confirming the fixture really did isolate the eligibility axis --
    // Commit was refused despite the placed row being unchanged, not
    // because the round robin happened to reshuffle it too).
    await generateFor(page, elig.divisionId,
      '#sched-preview[data-already-scheduled="0"]');
    s = await previewState(page);
    if (s.alreadyScheduled !== "0") {
      fail(`team-eligibility staleness: refused commit must not have created `
        + `any Game (would show as already-scheduled on the next preview): ${JSON.stringify(s)}`);
    }
    const placedRowAfter = s.rows.find((r) => !r.sub.includes("Already scheduled"));
    if (!placedRowAfter || placedRowAfter.title !== placedRowBefore.title
        || placedRowAfter.sub !== placedRowBefore.sub) {
      fail(`team-eligibility staleness: expected the late third team's `
        + `registration to leave the originally placed row unchanged `
        + `(proving this fixture isolates the eligibility axis): `
        + `before=${JSON.stringify(placedRowBefore)} after=${JSON.stringify(placedRowAfter)}`);
    }

    if (errors.length) {
      fail(`console/page errors:\n${errors.join("\n")}`);
    }
    console.log(`[${viewport.label}] OK — mixed and all-already-scheduled previews both name already-scheduled pairings distinctly from proposed games and conflicts, both a commit-time race refusal (naming the winning Game) and a stale-preview refusal show an actionable message then require a fresh Generate, and a real (unstubbed) team-registration change between Generate and Commit is refused by the real backend with zero Games created.`);
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
    console.log("Scheduler already-scheduled browser journey passed.");
  } catch (error) {
    console.error("Scheduler already-scheduled browser journey FAILED.");
    console.error(error && error.message ? error.message : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
