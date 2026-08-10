// Permanent-teams + competition-terminology Setup journey (#180 structure, #233
// Slice B1 labels).
//
// Proves, at desktop and phone widths:
//  - #180: a team is a first-class member of its PROGRAM (a "Permanent program
//    teams" panel), created under the league — not a division — and the
//    Competition tree is structure only (no team children).
//  - #233 B1: the Setup surface shows the canonical hierarchy nouns
//    (Program / League) everywhere the operator reads them — Records-view card
//    titles, create-drawer field labels and titles, empty-select notes (which
//    must show the display noun, never the internal league/level key), the
//    Season-participation add control, the move/reassign dialog nouns, and the
//    delete-modal nouns. The Program operator is an "operating organization"
//    (never "facility owner"), and moving it carries no legacy-coupling
//    warning (#233 Slice E removed the Venue->Program bridge). The visible
//    trees never expose the internal "level" grouping word,
//    and the unrelated "League Admin" policy role is left untouched. The internal
//    entity keys and the v1 API (POST /api/setup/{league,level}) are unchanged.
// Fails on any browser console/page error.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");
const {
  installContextFixture, selectProgram,
} = require("./context-fixture.js");

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
  const tag = viewport.label;
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

  const fail = (msg) => { throw new Error(`[${tag}] ${msg}`); };
  // Open a create drawer by entity key from the Records grid and read it back.
  const openDrawer = async (key) => {
    await page.click(`.setup-card .sc-new[data-drawer="${key}"]`);
    await page.waitForSelector(".drawer[role=dialog]", { timeout: 5000 });
    return page.evaluate(() => {
      const d = document.querySelector(".drawer[role=dialog]");
      return {
        title: (d.querySelector(".drawer-title") || {}).textContent || "",
        labels: [...d.querySelectorAll("label")].map((l) => l.textContent.trim()),
        notes: [...d.querySelectorAll(".drawer-note")].map((n) => n.textContent.trim()),
        placeholders: [...d.querySelectorAll("input[placeholder]")].map((i) => i.placeholder),
      };
    });
  };
  const closeDrawer = async () => {
    await page.click(".drawer-foot [data-drawer-close]");
    await page.waitForFunction(() => !document.querySelector(".drawer[role=dialog]"), null, { timeout: 5000 });
  };
  // Open a delete-confirm modal for an entity kind from the Setup trees and read
  // its heading back, then dismiss it.
  const delModalTitle = async (kind) => {
    await page.click(`.setup-trees [data-del="${kind}"]`);
    await page.waitForSelector(".modal.danger[role=dialog]", { timeout: 5000 });
    const title = await page.$eval(".modal.danger h2", (h) => h.textContent.trim());
    await page.click(".modal.danger .modal-x");
    await page.waitForFunction(() => !document.querySelector(".modal[role=dialog]"), null, { timeout: 5000 });
    return title;
  };
  // Switch the PERSISTED active context through the real header switcher — the
  // same control an operator uses — so the scoped Setup reads actually see the
  // new selection. #ctx-select option values are "<programId>|<seasonId>", with
  // an EMPTY season part for a Program-only selection (e.g. "program_2|"). The
  // option list is seeded once per page load by loadContextOptions() and is not
  // re-polled per render, so a Program created after that point is only offered
  // once the page has been reloaded.
  const switchContext = async (value) => {
    await page.waitForSelector("#ctx-select:not([hidden])", { timeout: 10000 });
    await page.waitForFunction(
      (v) => [...document.querySelectorAll("#ctx-select option")].some((o) => o.value === v),
      value, { timeout: 10000 });
    const ctxPost = page.waitForResponse(
      (r) => r.url().endsWith("/api/context") && r.request().method() === "POST",
      { timeout: 10000 });
    await page.selectOption("#ctx-select", value);
    await ctxPost;
    // The switcher repaints from the CANONICAL post-switch options, so the
    // select settling on the requested value is proof the backend persisted it
    // — a rejected switch snaps back to the previously persisted selection.
    await page.waitForFunction(
      (v) => { const s = document.getElementById("ctx-select"); return s && s.value === v; },
      value, { timeout: 10000 });
  };
  // The switcher's current selection, so a step that borrows another Program's
  // context can hand the original back.
  const currentContext = () => page.$eval("#ctx-select", (s) => s.value);
  // Open a reassignment panel by "kind:parent" from the Setup trees, read its
  // title + any warning, then cancel out.
  const reassignPanel = async (key) => {
    await page.click(`.setup-trees [data-reassign="${key}"]`);
    await page.waitForSelector(".rz-panel", { timeout: 5000 });
    const info = await page.evaluate(() => {
      const p = document.querySelector(".rz-panel");
      return {
        title: (p.querySelector(".ca-title") || {}).textContent || "",
        warn: (p.querySelector(".ca-warn") || {}).textContent || "",
      };
    });
    await page.click(".rz-panel [data-reassign-cancel]");
    await page.waitForFunction(() => !document.querySelector(".rz-panel"), null, { timeout: 5000 });
    return info;
  };

  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });
    await installContextFixture(page);
    await page.click('.tab[data-tab="setup"]');
    // Setup now LANDS on the six-workflow hub (#345 batch 2), so a journey
    // that works against the Hierarchy tree must select that sub-view
    // explicitly -- the same deliberate navigation the Records-based
    // journeys already do for their own sub-view.
    await page.click('[data-setup-view="hierarchy"]');
    await page.click('[data-setup-view="records"]');
    await page.waitForSelector(".setup-card", { timeout: 10000 });

    // 1) Records-view card titles use the canonical nouns: the umbrella entity
    //    reads "Programs" and the tier entity reads "Leagues"; the old "Levels"
    //    label is gone.
    const cardTitles = await page.$$eval(".setup-card .sc-title", (els) => els.map((e) => e.textContent.trim()));
    if (!cardTitles.includes("Programs")) fail(`Records grid missing a "Programs" card (got ${JSON.stringify(cardTitles)})`);
    if (!cardTitles.includes("Leagues")) fail(`Records grid missing a "Leagues" card (got ${JSON.stringify(cardTitles)})`);
    if (cardTitles.includes("Levels")) fail(`Records grid still shows the old "Levels" card`);

    // 2) Empty-state drawers: with nothing created yet, a required parent select
    //    is empty and its note must name the parent by its DISPLAY noun, never
    //    the internal key. The Season drawer's Program parent → "program"
    //    (internal key "league"); the Program drawer's operator parent →
    //    "facility owner" (internal key "organization").
    const seasonDrawer = await openDrawer("season");
    const seasonNote = seasonDrawer.notes.join(" | ");
    if (!/Create a program first/i.test(seasonNote))
      fail(`Season drawer empty-select note is not "Create a program first" (got ${JSON.stringify(seasonDrawer.notes)})`);
    if (/\bleague\b/i.test(seasonNote))
      fail(`Season drawer empty-select note leaks the internal "league" key (got ${JSON.stringify(seasonDrawer.notes)})`);
    await closeDrawer();

    const programDrawer = await openDrawer("league");
    if (programDrawer.title.trim() !== "New program")
      fail(`Program drawer title is not "New program" (got "${programDrawer.title}")`);
    const programLabels = programDrawer.labels.join(" | ");
    if (!/Program name/.test(programLabels)) fail(`Program drawer missing "Program name" field`);
    // The operating organization is OPTIONAL in the canonical model (#233 B2a
    // review r1) — operator_organization_id is nullable server-side, so with
    // zero organizations the field must still offer an explicit "— none —"
    // choice and never block the drawer with a "create one first" note (that
    // pattern is reserved for genuinely required parents).
    if (!/Operating organization \(optional\)/.test(programLabels))
      fail(`Program drawer's organization field is not marked optional (got ${JSON.stringify(programDrawer.labels)})`);
    if (programDrawer.notes.length)
      fail(`Program drawer showed a blocking note for an optional field (got ${JSON.stringify(programDrawer.notes)})`);
    if (/facility owner/i.test(programLabels))
      fail(`Program drawer operator field still says "facility owner" (got ${JSON.stringify(programDrawer.labels)})`);
    await closeDrawer();

    // 2b) League/Division example copy (issue #245): the client confirmed
    //     Gold/Silver/Diamond are DIVISIONS within a League, never Leagues
    //     themselves — the League name field must never suggest a division
    //     example, and the Division name field must use one.
    const levelDrawer = await openDrawer("level");
    if (levelDrawer.placeholders.some((p) => /diamond|platinum|gold|silver/i.test(p)))
      fail(`League name field placeholder suggests a Division example (got ${JSON.stringify(levelDrawer.placeholders)})`);
    await closeDrawer();

    const divisionDrawer = await openDrawer("division");
    if (!divisionDrawer.placeholders.some((p) => /gold|silver|diamond/i.test(p)))
      fail(`Division name field placeholder is not a Gold/Silver/Diamond example (got ${JSON.stringify(divisionDrawer.placeholders)})`);
    await closeDrawer();

    // 3) Now build a program → season → league(grouping) → division, plus a
    //    club → permanent team, through the v1 API (still POST
    //    /api/setup/{league,season,level,division,club,team}). The team is
    //    created under the umbrella LEAGUE (= Program), not a division (#180).
    //    The grouping + division give the hierarchy real League and Division
    //    nodes so their move/delete dialog nouns can be asserted below.
    const ids = await page.evaluate(async () => {
      const F = window.hsFixture;
      // #409 EXPLICIT SELECTION on the V1 SURFACE. `POST /api/setup/league`
      // mints the PROGRAM (v1 calls it "league") and `POST /api/setup/season`
      // is PROGRAM-AXIS on the body's `league_id` (server.py:3686), behind the
      // same `setup_create_context_error` preflight v2 uses (server.py:1160).
      const league = await F.create("v1 league (the Program)", "/api/setup/league", { name: "Permanent League" });
      await F.selectProgram("Program-only bootstrap", league.id);
      const season = await F.create("season", "/api/setup/season", { league_id: league.id, name: "2026-27" });
      // Both "levels" (v2 Leagues) and the Division are SEASON-OWNED here.
      await F.selectProgramSeason("Program+Season", league.id, season.id);
      const level = await F.create("level Adult League", "/api/setup/level", { season_id: season.id, name: "Adult League" });
      const division = await F.create("division", "/api/setup/division",
        { season_id: season.id, level_id: level.id, name: "U14" });
      const club = await F.create("club", "/api/setup/club", { name: "Perma Club" });
      // #283 Slice E: a v1 team create keyed only on the Program (no division)
      // resolves its permanent League only when that Program has exactly ONE —
      // so create the Team while "Adult League" is still the sole League, and
      // the v1 program_id→league_id response still equals league.id.
      const team = await F.create("team", "/api/setup/team",
        { club_id: club.id, league_id: league.id, name: "Perma Bruins" });
      // A second grouping League in the same season, so the Division-move
      // dialog (below) has a real target to move to (#233 B2a review r1).
      // Created AFTER the Team so it doesn't make the sole-League resolution
      // above ambiguous.
      const level2 = await F.create("level Junior League", "/api/setup/level", { season_id: season.id, name: "Junior League" });
      return { league: league.id, team: team.id, division: division.id, level2: level2.id,
               teamOk: !team.error && team.league_id === league.id && !team.division_id,
               structureOk: !level.error && !level2.error && !division.error };
    });
    if (!ids.teamOk) fail(`team not created under league`);
    if (!ids.structureOk) fail(`league grouping / division not created`);

    // 4) #233 B2a review r1: canonical Venue create is org-owned only — the
    //    drawer must NOT offer a Program field at all (a create-time field
    //    couldn't apply the temporary bridge anyway, since a venue has no id
    //    yet, and one that silently discarded a selection was misleading).
    //    The bridge's exact wording lives on the Facility tree's control
    //    instead (asserted later, once a venue exists to attach it to).
    await page.click('[data-setup-view="records"]');
    await page.waitForSelector(".setup-card", { timeout: 10000 });
    const venueDrawer = await openDrawer("venue");
    const venueLabels = venueDrawer.labels.join(" | ");
    if (/[Pp]rogram/.test(venueLabels))
      fail(`Venue create drawer still has a Program field (got ${JSON.stringify(venueDrawer.labels)})`);
    if (/sets the owner/i.test(venueLabels))
      fail(`Venue drawer still says "sets the owner"`);
    await closeDrawer();

    // 5) Hierarchy view: the #180 permanent-team model and the #233 competition
    //    nouns, including the Season-participation add control.
    await page.click('[data-setup-view="hierarchy"]');
    await page.waitForFunction(
      () => [...document.querySelectorAll(".tree-title")]
        .some((x) => x.textContent.includes("Permanent teams")),
      null, { timeout: 15000 });

    const checks = await page.evaluate(() => {
      const titles = [...document.querySelectorAll(".tree-title")].map((x) => x.textContent);
      const subs = [...document.querySelectorAll(".tree-sub")].map((x) => x.textContent);
      const compSub = subs.find((s) => s.includes("Program → Season"));
      const body = document.body.textContent;
      return {
        hasPermanentPanel: titles.some((t) => t.includes("Permanent teams")),
        hasCompetition: titles.some((t) => t.includes("Competition structure")),
        hasParticipation: titles.some((t) => t.includes("Season participation")),
        competitionSaysTeam: !!compSub && /Team\s*$/.test(compSub.trim()),
        competitionUsesNewNouns: !!compSub && compSub.includes("Program → Season → League → Division"),
        bodyHasTeam: body.includes("Perma Bruins"),
        addProgramTeam: body.includes("Add a program team"),
        leaksLeagueTeam: /league team/i.test(body),
        leaksNoLeagues: /No leagues yet/i.test(body),
      };
    });
    if (!checks.hasPermanentPanel) fail(`no "Permanent program teams" panel`);
    if (!checks.hasCompetition || !checks.hasParticipation) fail(`missing Competition/Participation sections`);
    if (checks.competitionSaysTeam) fail(`Competition subtitle still ends in "Team"`);
    if (!checks.competitionUsesNewNouns) fail(`Competition subtitle not "Program → Season → League → Division" (#233)`);
    if (!checks.bodyHasTeam) fail(`permanent team not shown on Setup`);
    if (!checks.addProgramTeam) fail(`Season participation add control is not "Add a program team…"`);
    if (checks.leaksLeagueTeam) fail(`Setup still says "league team" somewhere`);
    if (checks.leaksNoLeagues) fail(`Setup still says "No leagues yet"`);

    // 6) The visible Setup trees must not expose the internal "level" grouping
    //    noun anywhere — it renders as "League" now (the old "Level"/"Add
    //    level"/"No level" strings are gone). data-* attributes still carry the
    //    frozen key; only user-visible text is checked.
    const treeText = await page.$eval(".setup-trees", (el) => el.textContent);
    if (/\blevel\b/i.test(treeText)) fail(`Setup trees still show the internal "level" grouping noun`);

    // 7) Move/reassign dialog nouns. The Program's operator move says
    //    "operating organization" (not "facility owner") and, since #233
    //    Slice E removed the Venue->Program bridge, carries no legacy-coupling
    //    warning anymore — the move is unconstrained by any Venue. Moving a
    //    Division targets a "league" (the grouping), never a "level".
    const progMove = await reassignPanel("league:organization");
    if (!/operating organization/i.test(progMove.title))
      fail(`Program move dialog title is not "…operating organization" (got "${progMove.title}")`);
    if (/facility owner/i.test(progMove.title))
      fail(`Program move dialog still says "facility owner"`);
    if (progMove.warn)
      fail(`Program move dialog unexpectedly shows a warning (got "${progMove.warn}")`);
    const divMove = await reassignPanel("division:level");
    if (!/\bleague\b/i.test(divMove.title) || /\blevel\b/i.test(divMove.title))
      fail(`Division move dialog does not target a "league" (got "${divMove.title}")`);

    // 8) Delete-modal nouns: the umbrella deletes a "program"; the grouping
    //    deletes a "league" — never "level".
    const progDel = await delModalTitle("league");
    if (!/Delete this program\?/i.test(progDel)) fail(`umbrella delete modal is not "Delete this program?" (got "${progDel}")`);
    const grpDel = await delModalTitle("level");
    if (!/Delete this league\?/i.test(grpDel) || /Delete this level\?/i.test(grpDel))
      fail(`grouping delete modal is not "Delete this league?" (got "${grpDel}")`);

    // 9) The League Admin *role* is unrelated to the competition-model rename and
    //    must be untouched — its policy label stays exactly "League Admin".
    const roles = await page.evaluate(() =>
      fetch("/api/auth/roles", { credentials: "same-origin" }).then((r) => r.json()));
    const laRole = (roles.roles || []).find((r) => r.id === "league_admin");
    if (!laRole || laRole.label !== "League Admin")
      fail(`League Admin role label changed (got ${JSON.stringify(laRole && laRole.label)})`);

    // 10) #233 B2a review r1: a Program's operating organization is OPTIONAL —
    //     the drawer must let it be created with no organization at all, post
    //     operator_organization_id: null (never omit/coerce it), and keep
    //     displaying "No operating org" after a fresh reload (never crash on
    //     the null field).
    await page.click('[data-setup-view="records"]');
    await page.waitForSelector(".setup-card", { timeout: 10000 });
    const noOrgReq = page.waitForRequest((r) =>
      r.url().includes("/api/v2/setup/program") && r.method() === "POST");
    await page.click('.setup-card .sc-new[data-drawer="league"]');
    await page.waitForSelector("#f-league", { timeout: 5000 });
    await page.fill("#f-league", "Orgless Program");
    await page.click('[data-drawer-submit="league"]');
    const noOrgRequest = await noOrgReq;
    const noOrgBody = noOrgRequest.postDataJSON();
    if (noOrgBody.operator_organization_id !== null)
      fail(`Program-create body did not carry a null operator_organization_id (got ${JSON.stringify(noOrgBody)})`);
    const orglessId = await noOrgRequest.response()
      .then((res) => (res ? res.json() : {}))
      .then((body) => body && body.id);
    if (!orglessId) fail(`Program create returned no id to switch context to`);
    await page.waitForFunction(() => !document.querySelector(".drawer[role=dialog]"), null, { timeout: 5000 });
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.click('.tab[data-tab="setup"]');
    await page.click('[data-setup-view="hierarchy"]');
    await page.click('[data-setup-view="records"]');
    await page.waitForSelector(".setup-card", { timeout: 10000 });
    //     #369 review: Setup Records now ceilings on the ACTIVE Program and its
    //     `programs` list collapses to exactly that one Program, so this second
    //     Program is only readable from its OWN context — the active context is
    //     still "Permanent League" (the deterministic fallback picks the Program
    //     that has an active Season). Switch to the new Program through the real
    //     header switcher before asserting on its card; the reload above is what
    //     makes the switcher offer it at all. The selection is Program-only
    //     ("<id>|") because an orgless, season-less Program has no Season to
    //     select. Afterwards hand the original context back: step 11's
    //     reassign-target options come from this same Program-scoped read, so it
    //     must run against the Program that owns the Division.
    const primaryContext = await currentContext();
    await switchContext(`${orglessId}|`);
    // Wait for the REPAINTED Programs card, not merely for ".setup-card" to
    // exist: the previous context's cards are still in the DOM the moment the
    // switch resolves, so a bare waitForSelector(".setup-card") returns
    // immediately and the read below can run against the OLD paint -- where
    // the Programs card has no "Orgless Program" row, `.find()` yields
    // undefined, and `card.querySelectorAll` throws
    // "Cannot read properties of undefined". That is a pure race: it never
    // reproduced locally (the repaint beats the next statement every time)
    // and failed on the slower CI runner.
    await page.waitForFunction(() => {
      const card = [...document.querySelectorAll(".setup-card")]
        .find((c) => (c.querySelector(".sc-title") || {}).textContent
          && c.querySelector(".sc-title").textContent.includes("Programs"));
      return !!card && [...card.querySelectorAll(".li-title")]
        .some((t) => t.textContent.trim() === "Orgless Program");
    }, null, { timeout: 15000 }).catch(() => {
      fail("the Programs card never repainted with the Orgless Program row "
        + "after switching to its context");
    });
    const orglessRow = await page.evaluate(() => {
      const card = [...document.querySelectorAll(".setup-card")]
        .find((c) => (c.querySelector(".sc-title") || {}).textContent
          && c.querySelector(".sc-title").textContent.includes("Programs"));
      if (!card) return null;
      const row = [...card.querySelectorAll(".li-title")]
        .find((t) => t.textContent.trim() === "Orgless Program");
      const sub = row && row.closest(".li")
        && row.closest(".li").querySelector(".li-sub");
      return sub ? sub.textContent.trim() : null;
    });
    if (orglessRow !== "No operating org")
      fail(`Orgless program not shown as "No operating org" after reload (got ${JSON.stringify(orglessRow)})`);
    await switchContext(primaryContext);
    // Same race in reverse, and this one matters more than a display check:
    // step 11's reassign panel builds its options from `ov.levels`, which is
    // the primary Program's read, so acting before the repaint would offer the
    // orgless Program's (empty) Leagues. Wait for the Programs card to stop
    // showing the orgless Program rather than for any card to exist.
    await page.waitForFunction(() => {
      const card = [...document.querySelectorAll(".setup-card")]
        .find((c) => (c.querySelector(".sc-title") || {}).textContent
          && c.querySelector(".sc-title").textContent.includes("Programs"));
      return !!card && ![...card.querySelectorAll(".li-title")]
        .some((t) => t.textContent.trim() === "Orgless Program");
    }, null, { timeout: 15000 }).catch(() => {
      fail("the Programs card never repainted back to the primary context");
    });

    // 11) #233 B2a review r1: structural Setup writes go to v2 — a Division
    //     move (division:level) and a Division delete both POST to
    //     /api/v2/setup/..., never the legacy /api/setup/... route.
    await page.click('[data-setup-view="hierarchy"]');
    await page.waitForFunction(
      () => document.querySelector('[data-reassign="division:level"]'),
      null, { timeout: 15000 });
    const divMoveReq = page.waitForRequest((r) =>
      /\/api\/(v2\/)?setup\/division\/[^/]+\/assign-/.test(r.url()) && r.method() === "POST");
    await page.click('[data-reassign="division:level"]');
    await page.waitForSelector(".rz-panel select#reassign-target", { timeout: 5000 });
    await page.selectOption("#reassign-target", ids.level2);
    await page.click(".rz-panel [data-reassign-confirm]");
    const divMoveUrl = (await divMoveReq).url();
    if (!divMoveUrl.includes("/api/v2/setup/division/"))
      fail(`Division move did not POST to v2 (got ${divMoveUrl})`);
    await page.waitForFunction(() => !document.querySelector(".rz-panel"), null, { timeout: 5000 });

    const divDelReq = page.waitForRequest((r) =>
      /\/api\/(v2\/)?setup\/division\/[^/]+\/delete$/.test(r.url()) && r.method() === "POST");
    await page.click('[data-del="division"]');
    await page.waitForSelector(".modal.danger [data-del-confirm]", { timeout: 5000 });
    await page.click(".modal.danger [data-del-confirm]");
    const divDelUrl = (await divDelReq).url();
    if (!divDelUrl.includes("/api/v2/setup/division/"))
      fail(`Division delete did not POST to v2 (got ${divDelUrl})`);
    await page.waitForFunction(() => !document.querySelector(".modal[role=dialog]"), null, { timeout: 5000 });

    if (errors.length) fail(`console/page errors:\n${errors.join("\n")}`);
    console.log(`[${tag}] OK — permanent team under league; Setup uses Program/League nouns end to end.`);
  } catch (error) {
    throw new Error(`${error.message}\n--- demo server output ---\n${serverOutput}`);
  } finally {
    await context.close();
    await stopServer(server);
  }
}

// #233 B2a review r1: an Arena Manager (MANAGE_ARENA, not MANAGE_SETUP) must
// be able to open both Setup views and manage Organization/Venue/Rink
// without the page crashing — before this fix, the canonical overview was
// fetched only under manage_setup, so an Arena Manager's Setup screen
// dereferenced an undefined `sv`.
async function checkArenaManager(browser, viewport) {
  const base = `http://${HOST}:${viewport.port}`;
  const tag = viewport.label;
  const server = spawn(
    process.env.PYTHON || "python3",
    ["-u", "-m", "hockey_scheduler.web.server", "--host", HOST, "--port", String(viewport.port)],
    { cwd: BACKEND_DIR, stdio: ["ignore", "pipe", "pipe"] });
  let serverOutput = "";
  server.stdout.on("data", (d) => { serverOutput += d.toString(); });
  server.stderr.on("data", (d) => { serverOutput += d.toString(); });

  const context = await browser.newContext({ viewport: { width: viewport.width, height: viewport.height } });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(`[pageerror] ${e.message}`));
  page.on("console", (m) => { if (m.type() === "error") errors.push(`[console] ${m.text()}`); });
  const fail = (msg) => { throw new Error(`[arena-manager/${tag}] ${msg}`); };

  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });
    await installContextFixture(page);
    // A fresh boot only seeds the "admin" account — the other demo personas
    // (including "arena") are UserAccount rows built by /api/demo/load, so
    // load the sample dataset first (as the auto-logged-in League Admin)
    // before switching sessions onto Arena Manager and reloading.
    const loadStatus = await page.evaluate(() => fetch("/api/demo/load", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" }, body: "{}",
    }).then((r) => r.status));
    if (loadStatus !== 200) fail(`demo load (as admin) failed (status ${loadStatus})`);
    const loginStatus = await page.evaluate(() => fetch("/api/auth/login", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: "arena", password: "demo" }),
    }).then((r) => r.status));
    if (loginStatus !== 200) fail(`arena manager login failed (status ${loginStatus})`);
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });
    await installContextFixture(page);

    // #409 EXPLICIT SELECTION for the ARENA MANAGER. The Venue and Rink this
    // leg creates through the real drawers are PROGRAM-AXIS, and an Arena
    // Manager who has just signed in has no saved selection of their own —
    // the demo load ran under the League Admin. Without a persisted Program
    // the Venue create is refused, and this leg's only symptom was the
    // "Arena One" text never appearing: a 10s waitForFunction timeout that
    // named neither context nor the refusal.
    //
    // It is a REAL selection an Arena Manager can make: `arena_manager` is a
    // global role (context_scope._GLOBAL_ROLES), so every Program is offered
    // in the header switcher this drives. The Program is taken from the
    // switcher's own options rather than hard-coded, so the fixture selects
    // something the operator is genuinely offered.
    const arenaProgram = await page.evaluate(() => {
      const sel = document.getElementById("ctx-select");
      const opt = sel && [...sel.options].find((o) => o.value);
      return opt ? opt.value.split("|")[0] : null;
    });
    if (!arenaProgram) fail("the context switcher offered the Arena Manager no Program to select");
    await selectProgram(page, `[arena-manager/${tag}] Program-only selection`, arenaProgram);

    await page.click('.tab[data-tab="setup"]');
    await page.click('[data-setup-view="hierarchy"]');
    await page.click('[data-setup-view="hierarchy"]');
    await page.waitForSelector(".setup-trees", { timeout: 10000 });
    if (!(await page.$$eval(".setup-trees .tree-panel", (els) => els.length)))
      fail(`Hierarchy view rendered no tree panels for Arena Manager`);

    await page.click('[data-setup-view="records"]');
    await page.waitForSelector(".setup-card", { timeout: 10000 });
    const cardTitles = await page.$$eval(".setup-card .sc-title", (els) => els.map((e) => e.textContent.trim()));
    if (!cardTitles.includes("Venues") || !cardTitles.includes("Rinks"))
      fail(`Records grid missing Venue/Rink cards for Arena Manager (got ${JSON.stringify(cardTitles)})`);

    // Create an Organization, then a Venue owned by it, then a Rink — the
    // full arena-side create path an Arena Manager actually uses. Each step
    // waits for its own record to actually appear (not just for the drawer
    // to close) before moving on, since the next drawer's "+New" click can
    // otherwise race the previous submit's full-page re-render under load.
    await page.click('.setup-card .sc-new[data-drawer="organization"]');
    await page.waitForSelector("#f-org", { timeout: 5000 });
    await page.fill("#f-org", "Arena Co");
    await page.click('[data-drawer-submit="organization"]');
    await page.waitForFunction(() => !document.querySelector(".drawer[role=dialog]")
      && document.body.textContent.includes("Arena Co"), null, { timeout: 10000 });

    await page.click('.setup-card .sc-new[data-drawer="venue"]');
    await page.waitForSelector("#f-venue", { timeout: 5000 });
    await page.fill("#f-venue", "Arena One");
    await page.click('[data-drawer-submit="venue"]');
    await page.waitForFunction(() => !document.querySelector(".drawer[role=dialog]")
      && document.body.textContent.includes("Arena One"), null, { timeout: 10000 });

    await page.click('.setup-card .sc-new[data-drawer="rink"]');
    await page.waitForSelector("#f-rink", { timeout: 5000 });
    await page.fill("#f-rink", "Rink A");
    await page.click('[data-drawer-submit="rink"]');
    await page.waitForFunction(() => !document.querySelector(".drawer[role=dialog]")
      && document.body.textContent.includes("Rink A"), null, { timeout: 10000 });

    if (errors.length) fail(`console/page errors:\n${errors.join("\n")}`);
    console.log(`[arena-manager/${tag}] OK — Setup Hierarchy + Records usable, Venue/Rink created, no crash.`);
  } catch (error) {
    throw new Error(`${error.message}\n--- demo server output ---\n${serverOutput}`);
  } finally {
    await context.close();
    await stopServer(server);
  }
}

// #233 B2a review r2: a role with neither manage_setup nor manage_arena (a
// Coach, here) must never be able to see or open the Setup tab — not from a
// fresh sign-in, and not by a no-reload persona switch off an operator
// identity that was already sitting on the Setup screen. Neither path may
// request /api/v2/setup/overview or /api/v2/setup/hierarchy.
async function checkNoSetupAccess(browser, viewport) {
  const base = `http://${HOST}:${viewport.port}`;
  const tag = viewport.label;
  const server = spawn(
    process.env.PYTHON || "python3",
    ["-u", "-m", "hockey_scheduler.web.server", "--host", HOST, "--port", String(viewport.port)],
    { cwd: BACKEND_DIR, stdio: ["ignore", "pipe", "pipe"] });
  let serverOutput = "";
  server.stdout.on("data", (d) => { serverOutput += d.toString(); });
  server.stderr.on("data", (d) => { serverOutput += d.toString(); });

  const context = await browser.newContext({ viewport: { width: viewport.width, height: viewport.height } });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(`[pageerror] ${e.message}`));
  page.on("console", (m) => { if (m.type() === "error") errors.push(`[console] ${m.text()}`); });
  const setupRequests = [];
  page.on("request", (r) => {
    if (/\/api\/v2\/setup\/(overview|hierarchy)(\?|$)/.test(r.url())) setupRequests.push(r.url());
  });
  const fail = (msg) => { throw new Error(`[no-setup-access/${tag}] ${msg}`); };

  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });
    await installContextFixture(page);
    // Seed the "coach" persona (a fresh boot only has "admin" — see
    // checkArenaManager above) while still the auto-logged-in League Admin.
    const loadStatus = await page.evaluate(() => fetch("/api/demo/load", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" }, body: "{}",
    }).then((r) => r.status));
    if (loadStatus !== 200) fail(`demo load (as admin) failed (status ${loadStatus})`);

    // 1) A fresh sign-in as Coach never sees the Setup tab at all.
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });
    await installContextFixture(page);
    const loginStatus = await page.evaluate(() => fetch("/api/auth/login", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: "coach", password: "demo" }),
    }).then((r) => r.status));
    if (loginStatus !== 200) fail(`coach login failed (status ${loginStatus})`);
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });
    await installContextFixture(page);
    if (await page.isVisible('.tab[data-tab="setup"]'))
      fail(`Setup tab is visible for a fresh Coach sign-in`);

    // 2) An operator switches (no reload) to Coach while already on Setup —
    // the view must bounce off, the tab must hide, and no further v2 setup
    // read may fire.
    const adminLoginStatus = await page.evaluate(() => fetch("/api/auth/login", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: "admin", password: "demo" }),
    }).then((r) => r.status));
    if (adminLoginStatus !== 200) fail(`admin re-login failed (status ${adminLoginStatus})`);
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });
    await installContextFixture(page);
    await page.click('.tab[data-tab="setup"]');
    await page.click('[data-setup-view="hierarchy"]');
    await page.waitForSelector(".setup-trees, .setup-card", { timeout: 10000 });
    if ((await page.evaluate(() => document.body.dataset.view)) !== "setup")
      fail(`did not actually land on the Setup view as League Admin`);

    setupRequests.length = 0;  // only count requests fired AFTER the switch below
    const sel = page.locator("#role-switch");
    await sel.selectOption({ label: "Coach" });
    await page.waitForFunction(() => document.body.dataset.view !== "setup", null, { timeout: 10000 });
    // Give any in-flight fetch a moment to land before checking the log.
    await page.waitForTimeout(500);
    if (await page.isVisible('.tab[data-tab="setup"]'))
      fail(`Setup tab still visible after switching to Coach`);
    if (setupRequests.length)
      fail(`v2 setup read(s) fired after switching to a non-operator role: ${JSON.stringify(setupRequests)}`);

    if (errors.length) fail(`console/page errors:\n${errors.join("\n")}`);
    console.log(`[no-setup-access/${tag}] OK — Coach never sees/keeps the Setup tab, no v2 setup reads.`);
  } catch (error) {
    throw new Error(`${error.message}\n--- demo server output ---\n${serverOutput}`);
  } finally {
    await context.close();
    await stopServer(server);
  }
}

// #283 Slice B: the permanent Program → League → Team tree is surfaced in the
// Setup hierarchy and is operable end to end — a Team can be created directly
// under its permanent League from the drawer (league_id in the POST), it nests
// under that League, and a league-to-league transfer (⇄ Move, promotion/
// relegation) moves it via POST /api/v2/setup/team/{id}/assign-league. A Team
// with no permanent League yet is surfaced under a "No league" bucket.
async function checkSliceB(browser, viewport) {
  const base = `http://${HOST}:${viewport.port}`;
  const tag = viewport.label;
  const server = spawn(
    process.env.PYTHON || "python3",
    ["-u", "-m", "hockey_scheduler.web.server", "--host", HOST, "--port", String(viewport.port)],
    { cwd: BACKEND_DIR, stdio: ["ignore", "pipe", "pipe"] });
  let serverOutput = "";
  server.stdout.on("data", (d) => { serverOutput += d.toString(); });
  server.stderr.on("data", (d) => { serverOutput += d.toString(); });

  const context = await browser.newContext({ viewport: { width: viewport.width, height: viewport.height } });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(`[pageerror] ${e.message}`));
  page.on("console", (m) => { if (m.type() === "error") errors.push(`[console] ${m.text()}`); });
  const fail = (msg) => { throw new Error(`[slice-b/${tag}] ${msg}`); };

  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });
    await installContextFixture(page);

    // Build a program → season → two permanent Leagues via the canonical v2
    // surface, plus an extra Program team under Rec to prove multi-team
    // league-nesting in the tree. Elite gets sort_order 1 so it sorts before Rec.
    const ids = await page.evaluate(async () => {
      const F = window.hsFixture;
      const program = await F.create("Slice B program", "/api/v2/setup/program", { name: "Slice B Program" });
      // #409: Season is PROGRAM-AXIS; both Leagues are SEASON-OWNED. This is
      // a SECOND Program in the same run, so nothing ambient could be right.
      await F.selectProgram("Slice B Program-only bootstrap", program.id);
      const season = await F.create("Slice B season", "/api/v2/setup/season", { program_id: program.id, name: "2027-28" });
      await F.selectProgramSeason("Slice B Program+Season", program.id, season.id);
      const elite = await F.create("league Elite", "/api/v2/setup/league", { season_id: season.id, name: "Elite", sort_order: 1 });
      const rec = await F.create("league Rec", "/api/v2/setup/league", { season_id: season.id, name: "Rec", sort_order: 2 });
      // #283 Slice E: a Team must always resolve a permanent League — NEITHER
      // the v1 nor the v2 create path can mint a league-less Team anymore (a
      // "teams_without_league" row is now only reachable as a legacy migration
      // remediation state, never through the API). So this extra Program team is
      // created under a real League (Rec) via the canonical v2 route; the tree
      // assertion below verifies it nests under that League rather than a
      // now-unreachable "No league" bucket.
      const loose = await F.create("team Undrafted", "/api/v2/setup/team", { league_id: rec.id, name: "Undrafted" });
      return { program: program.id, season: season.id, elite: elite.id, rec: rec.id,
               loose: loose.id, looseOk: !loose.error && !!loose.id };
    });
    if (!ids.looseOk) fail(`program-only team not created (got ${JSON.stringify(ids)})`);

    // Create a Team from the drawer, choosing the permanent League. The POST
    // must carry league_id (the Slice B assignment).
    await page.click('.tab[data-tab="setup"]');
    await page.click('[data-setup-view="hierarchy"]');
    await page.click('[data-setup-view="records"]');
    await page.waitForSelector(".setup-card", { timeout: 10000 });
    const teamReq = page.waitForRequest((r) =>
      r.url().endsWith("/api/v2/setup/team") && r.method() === "POST");
    await page.click('.setup-card .sc-new[data-drawer="team"]');
    await page.waitForSelector("#f-team", { timeout: 5000 });
    await page.fill("#f-team", "Falcons");
    await page.selectOption("#f-team-perm-league", ids.elite);
    await page.click('[data-drawer-submit="team"]');
    const teamBody = (await teamReq).postDataJSON();
    if (teamBody.league_id !== ids.elite)
      fail(`team-create POST did not carry the permanent league_id (got ${JSON.stringify(teamBody)})`);
    await page.waitForFunction(() => !document.querySelector(".drawer[role=dialog]"), null, { timeout: 5000 });

    // The permanent-teams tree nests each Team under its permanent League —
    // Falcons under Elite, and the extra Program team (Undrafted) under Rec.
    // (#283 Slice E removed the ability to create a league-less Team, so the
    // legacy "No league" remediation bucket is no longer reachable from a fresh
    // create; the tree instead proves league-nesting for every Program team.)
    await page.click('[data-setup-view="hierarchy"]');
    await page.waitForFunction(
      () => [...document.querySelectorAll(".tree-title")]
        .some((x) => x.textContent.includes("Permanent teams")),
      null, { timeout: 15000 });
    const nesting = await page.evaluate(() => {
      const panel = [...document.querySelectorAll(".tree-panel")]
        .find((p) => (p.querySelector(".tree-title") || {}).textContent.includes("Permanent teams"));
      if (!panel) return { ok: false };
      // Teams nested under the League block whose summary label matches `name`.
      const blockTeams = (name) => {
        const block = [...panel.querySelectorAll("details.tn")]
          .find((d) => {
            const lbl = d.querySelector(":scope > summary .tn-label");
            return lbl && lbl.textContent.includes(name);
          });
        return block ? [...block.querySelectorAll(".tn-leaf .tn-label")].map((l) => l.textContent) : [];
      };
      return {
        ok: true,
        eliteHasFalcons: blockTeams("Elite").some((t) => t.includes("Falcons")),
        recHasUndrafted: blockTeams("Rec").some((t) => t.includes("Undrafted")),
        looseSurfaced: panel.textContent.includes("Undrafted"),
      };
    });
    if (!nesting.ok) fail(`no "Permanent teams" panel in hierarchy`);
    if (!nesting.eliteHasFalcons) fail(`Falcons not nested under its Elite league`);
    if (!nesting.recHasUndrafted || !nesting.looseSurfaced)
      fail(`extra Program team not nested under its permanent League (Rec)`);

    // Transfer Falcons from Elite to Rec via the ⇄ Move (team:league) panel.
    const moveReq = page.waitForRequest((r) =>
      /\/api\/v2\/setup\/team\/[^/]+\/assign-league$/.test(r.url()) && r.method() === "POST");
    await page.click('.setup-trees [data-reassign="team:league"]');
    await page.waitForSelector(".rz-panel select#reassign-target", { timeout: 5000 });
    await page.selectOption("#reassign-target", ids.rec);
    await page.click(".rz-panel [data-reassign-confirm]");
    const moveBody = (await moveReq).postDataJSON();
    if (moveBody.league_id !== ids.rec)
      fail(`team transfer POST did not carry the target league_id (got ${JSON.stringify(moveBody)})`);
    await page.waitForFunction(() => !document.querySelector(".rz-panel"), null, { timeout: 5000 });

    // After the transfer, Falcons hangs under Rec, not Elite.
    await page.waitForFunction(() => [...document.querySelectorAll(".tree-title")]
      .some((x) => x.textContent.includes("Permanent teams")), null, { timeout: 15000 });
    const afterMove = await page.evaluate(() => {
      const panel = [...document.querySelectorAll(".tree-panel")]
        .find((p) => (p.querySelector(".tree-title") || {}).textContent.includes("Permanent teams"));
      const blockTeams = (name) => {
        const block = [...panel.querySelectorAll("details.tn")]
          .find((d) => {
            const lbl = d.querySelector(":scope > summary .tn-label");
            return lbl && lbl.textContent.includes(name);
          });
        return block ? [...block.querySelectorAll(".tn-leaf .tn-label")].map((l) => l.textContent) : [];
      };
      return { recTeams: blockTeams("Rec"), eliteTeams: blockTeams("Elite") };
    });
    if (!afterMove.recTeams.some((t) => t.includes("Falcons")))
      fail(`Falcons did not move under Rec after transfer (got ${JSON.stringify(afterMove)})`);
    if (afterMove.eliteTeams.some((t) => t.includes("Falcons")))
      fail(`Falcons still shown under Elite after transfer (got ${JSON.stringify(afterMove)})`);

    if (errors.length) fail(`console/page errors:\n${errors.join("\n")}`);
    console.log(`[slice-b/${tag}] OK — permanent League tree, team-under-league create, league transfer.`);
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
    for (const viewport of VIEWPORTS) await checkArenaManager(browser, viewport);
    for (const viewport of VIEWPORTS) await checkNoSetupAccess(browser, viewport);
    // #283 Slice B: run the permanent-League tree + transfer journey once
    // (desktop) — it exercises canonical routes independent of viewport width.
    await checkSliceB(browser, VIEWPORTS[0]);
    console.log("Permanent-teams + competition-terminology browser journey passed.");
  } catch (error) {
    console.error("Permanent-teams + competition-terminology browser journey FAILED.");
    console.error(error && error.message ? error.message : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
