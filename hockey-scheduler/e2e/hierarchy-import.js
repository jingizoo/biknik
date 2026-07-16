// Complete hierarchy import browser journey (#174 PR E2, #260 Slice F).
//
// At desktop and phone widths, a League Admin opens the existing Import
// screen and drives the real "Setup profile" wizard — the locked Q1-Q7 from
// issue #260, pure UI-routing state with no backend field of its own — to
// prove: (1) the exact question order/content; (2) answer-driven sheet
// visibility; (3) a fresh commit; (4) an idempotent repeat commit with no
// duplicates; and (5) an invalid row blocking the whole batch with zero
// persisted changes. Every step goes through the real DOM (typing into
// textareas, clicking wizard/validate/commit buttons) and the canonical v2
// setup reads for verification — never a raw API call for the behavior being
// accepted. Fails on browser console/page errors.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(__dirname, "..", "backend");
const READY_TIMEOUT_MS = 15000;
const VIEWPORTS = [
  { label: "desktop", width: 1440, height: 900, port: 8135 },
  { label: "phone", width: 390, height: 844, port: 8136 },
];

// The locked Q1-Q7 order/content (#260 review) — asserted verbatim below.
const WIZARD_QUESTIONS = [
  "Q1. What are you setting up today?",
  "Q2. Which Programs do you run?",
  "Q3. Which Leagues exist in this Season?",
  "Q4. Do your teams belong to clubs?",
  "Q5. Do your leagues use divisions?",
  "Q6. Which Venues may this Season use?",
  "Q7. Does this Season use one Venue or multiple?",
];

const ALWAYS_VISIBLE_SHEETS = ["organizations_csv"];
const COMPETITION_TRACK_SHEETS = [
  "programs_csv", "competition_csv", "permanent_teams_csv",
  "registrations_csv", "clubs_csv",
];
const VENUE_TRACK_SHEETS = ["venues_rinks_csv", "season_venue_access_csv"];

const SHEETS = {
  organizations_csv:
    "organization_code,organization_name,short_name\n" +
    "BROWSERORG,Browser Ice Facilities,Browser Ice\n",
  programs_csv:
    "program_code,operator_organization_code,program_name,country,timezone\n" +
    "BROWSERPROGRAM,BROWSERORG,Browser Program,US,America/Chicago\n",
  venues_rinks_csv:
    "venue_code,organization_code,venue_name,address,timezone,rink_code,rink_name\n" +
    "BROWSERVENUE,BROWSERORG,Browser Arena,1 Test Way,America/Chicago,BROWSERRINK,Browser Rink\n",
  competition_csv:
    "program_code,season_code,season_name,league_code,league_name,league_sort_order,division_code,division_name,age_group\n" +
    "BROWSERPROGRAM,BROWSERSEASON,Browser Season,BROWSERLEAGUE,Browser League,1,BROWSERDIV,Browser Division,Adult\n",
  clubs_csv:
    "club_code,club_name,country\n" +
    "BROWSERCLUB,Browser Club,US\n",
  permanent_teams_csv:
    "program_code,team_code,team_name,club_code\n" +
    "BROWSERPROGRAM,BROWSERTEAM,Browser Team,BROWSERCLUB\n",
  players_csv:
    "player_code,team_code,first_name,last_name,jersey_number,position,email\n" +
    "BROWSERPLAYER,BROWSERTEAM,Browser,Player,9,forward,browser.player@example.com\n",
  registrations_csv:
    "season_code,team_code,league_code,division_code\n" +
    "BROWSERSEASON,BROWSERTEAM,BROWSERLEAGUE,BROWSERDIV\n",
  season_venue_access_csv:
    "season_code,venue_code,active\n" +
    "BROWSERSEASON,BROWSERVENUE,true\n",
};

const INVALID_REGISTRATIONS_CSV =
  "season_code,team_code,league_code,division_code\n" +
  "BROWSERSEASON,BROWSERTEAM,BROWSERLEAGUE,BROWSERDIV\n" +
  "BROWSERSEASON,BROWSERTEAM,BOGUSLEAGUE,BROWSERDIV\n";

const FILENAMES = {
  organizations_csv: "organizations.csv",
  programs_csv: "programs.csv",
  venues_rinks_csv: "venues_rinks.csv",
  competition_csv: "competition.csv",
  clubs_csv: "clubs.csv",
  permanent_teams_csv: "permanent_teams.csv",
  players_csv: "players.csv",
  registrations_csv: "registrations.csv",
  season_venue_access_csv: "season_venue_access.csv",
};

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
    server.once("exit", () => {
      clearTimeout(escalate);
      resolve();
    });
    server.kill("SIGTERM");
  });
}

async function waitForSheetVisibility(page, key, visible) {
  await page.waitForFunction(
    ({ key, visible }) =>
      !!document.querySelector(`[data-hierarchy-sheet="${key}"]`) === visible,
    { key, visible },
    { timeout: 5000 },
  );
}

async function assertSheetsVisible(page, label, visibleKeys, hiddenKeys) {
  for (const key of visibleKeys) await waitForSheetVisibility(page, key, true);
  for (const key of hiddenKeys) await waitForSheetVisibility(page, key, false);
}

async function readOverview(page) {
  return page.evaluate(async () => {
    const r = await fetch("/api/v2/setup/overview", { credentials: "same-origin" });
    return r.json();
  });
}

async function readRecordCounts(page) {
  const overview = await readOverview(page);
  const teams = (overview.teams || []).filter((t) => t.name === "Browser Team");
  const team = teams[0];
  const playersResp = team
    ? await page.evaluate(async (teamId) => {
        const r = await fetch(`/api/players?team_id=${teamId}`, { credentials: "same-origin" });
        return r.json();
      }, team.id)
    : [];
  return {
    organizations: (overview.organizations || []).filter((o) => o.name === "Browser Ice Facilities").length,
    programs: (overview.programs || []).filter((p) => p.name === "Browser Program").length,
    clubs: (overview.clubs || []).filter((c) => c.name === "Browser Club").length,
    teams: teams.length,
    players: (Array.isArray(playersResp) ? playersResp : []).filter((p) => p.name === "Browser Player").length,
  };
}

async function checkViewport(browser, viewport) {
  const base = `http://${HOST}:${viewport.port}`;
  const server = spawn(
    process.env.PYTHON || "python3",
    ["-u", "-m", "hockey_scheduler.web.server", "--host", HOST,
      "--port", String(viewport.port)],
    { cwd: BACKEND_DIR, stdio: ["ignore", "pipe", "pipe"] },
  );
  let serverOutput = "";
  server.stdout.on("data", (data) => { serverOutput += data.toString(); });
  server.stderr.on("data", (data) => { serverOutput += data.toString(); });

  const context = await browser.newContext({
    viewport: { width: viewport.width, height: viewport.height },
    acceptDownloads: true,
  });
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(`[pageerror] ${error.message}`));
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(`[console] ${message.text()}`);
  });

  try {
    await waitForServer(`${base}/api/health`, READY_TIMEOUT_MS);
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.waitForSelector("#content > *", { timeout: 10000 });
    await page.click('.tab[data-tab="import"]');
    await page.waitForSelector("#hierarchy-import-panel", { timeout: 10000 });
    await page.waitForSelector("#hierarchy-wizard", { timeout: 10000 });

    // (1) Exact Q1-Q7 order/content (#260 review, locked).
    const titles = await page.locator("#hierarchy-wizard .li-title").allTextContents();
    if (JSON.stringify(titles) !== JSON.stringify(WIZARD_QUESTIONS)) {
      throw new Error(`[${viewport.label}] wizard question order/content mismatch.\n`
        + `expected: ${JSON.stringify(WIZARD_QUESTIONS)}\n`
        + `actual:   ${JSON.stringify(titles)}`);
    }

    // (2) Answer-driven sheet visibility (Q1 operatorType).
    await page.click('[data-hierarchy-wizard="operatorType"][data-hierarchy-wizard-value="venues"]');
    await assertSheetsVisible(page, "venues-only",
      [...ALWAYS_VISIBLE_SHEETS, ...VENUE_TRACK_SHEETS], COMPETITION_TRACK_SHEETS);

    await page.click('[data-hierarchy-wizard="operatorType"][data-hierarchy-wizard-value="competitions"]');
    await assertSheetsVisible(page, "competitions-only",
      [...ALWAYS_VISIBLE_SHEETS, ...COMPETITION_TRACK_SHEETS], VENUE_TRACK_SHEETS);

    await page.click('[data-hierarchy-wizard="operatorType"][data-hierarchy-wizard-value="both"]');
    await assertSheetsVisible(page, "both",
      [...ALWAYS_VISIBLE_SHEETS, ...COMPETITION_TRACK_SHEETS, ...VENUE_TRACK_SHEETS], []);

    // Q4 hasClubs="no" hides clubs.csv and hints permanent_teams.csv.
    await page.click('[data-hierarchy-wizard="hasClubs"][data-hierarchy-wizard-value="no"]');
    await waitForSheetVisibility(page, "clubs_csv", false);
    await page.locator('[data-hierarchy-sheet="permanent_teams_csv"]')
      .locator("xpath=../p").getByText("leave club_code blank", { exact: false }).waitFor();
    // Restore Q1/Q4 defaults for the real import below.
    await page.click('[data-hierarchy-wizard="hasClubs"][data-hierarchy-wizard-value="yes"]');
    await waitForSheetVisibility(page, "clubs_csv", true);

    for (const [key, filename] of Object.entries(FILENAMES)) {
      await page.waitForSelector(`[data-hierarchy-template="${key}"]`, { timeout: 5000 });
      const downloadPromise = page.waitForEvent("download");
      await page.click(`[data-hierarchy-template="${key}"]`);
      const download = await downloadPromise;
      if (download.suggestedFilename() !== filename) {
        throw new Error(`[${viewport.label}] expected ${filename}, got ${download.suggestedFilename()}`);
      }
    }

    for (const [key, csv] of Object.entries(SHEETS)) {
      await page.fill(`[data-hierarchy-sheet="${key}"]`, csv);
    }
    // Typing into a sheet doesn't itself re-render the wizard's Q2/Q3/Q6
    // pickers (only their answer state does) — re-select Q1's current
    // answer to force one, the same way any other wizard click would, so
    // the pickers pick up the codes just typed above.
    await page.click('[data-hierarchy-wizard="operatorType"][data-hierarchy-wizard-value="both"]');

    // Q2/Q3/Q6 pickers pick up codes typed into the draft above.
    await page.click('[data-hierarchy-wizard-multi="programCodes"][data-hierarchy-wizard-code="BROWSERPROGRAM"]');
    await page.click('[data-hierarchy-wizard-multi="leagueCodes"][data-hierarchy-wizard-code="BROWSERLEAGUE"]');
    await page.click('[data-hierarchy-wizard-multi="venueCodes"][data-hierarchy-wizard-code="BROWSERVENUE"]');
    // Selecting a league surfaces its code as a hint on registrations.csv.
    await page.locator('[data-hierarchy-sheet="registrations_csv"]')
      .locator("xpath=../p").getByText("BROWSERLEAGUE", { exact: false }).waitFor();

    // (3) Fresh commit.
    const validateResponse = page.waitForResponse((response) =>
      response.url() === `${base}/api/import/commit/teams-players`
      && response.request().method() === "POST"
      && response.request().postData().includes('"dry_run":true'));
    await page.click("[data-hierarchy-validate]");
    const preview = await validateResponse;
    if (preview.status() !== 200) {
      throw new Error(`[${viewport.label}] hierarchy validation returned HTTP ${preview.status()}`);
    }
    await page.getByRole("heading", { name: "Hierarchy validation passed" }).waitFor();
    await page.waitForSelector("[data-hierarchy-commit]:not([disabled])");

    const commitResponse = page.waitForResponse((response) =>
      response.url() === `${base}/api/import/commit/teams-players`
      && response.request().method() === "POST"
      && response.request().postData().includes('"dry_run":false'));
    await page.click("[data-hierarchy-commit]");
    const committed = await commitResponse;
    if (committed.status() !== 200) {
      throw new Error(`[${viewport.label}] hierarchy commit returned HTTP ${committed.status()}`);
    }
    await page.getByRole("heading", { name: "Hierarchy committed" }).waitFor();
    await page.getByText("organizations", { exact: true }).waitFor();
    await page.getByText("clubs", { exact: true }).waitFor();
    await page.getByText("permanent_teams", { exact: true }).waitFor();
    await page.getByText("players", { exact: true }).waitFor();
    await page.getByText("registrations", { exact: true }).waitFor();
    await page.getByText("season_venue_access", { exact: true }).waitFor();

    // Verify the ACTUAL resulting records via the canonical v2 setup reads
    // (#214/#260 review), not just the summary labels: the imported Club,
    // permanent Team, Player, and active Season/Division registration all
    // exist exactly once, the Team resolves its Club, and Venue.league_id
    // is never written (#233 Slice E / #260).
    const verify = await page.evaluate(async () => {
      const getJson = async (url) =>
        (await fetch(url, { credentials: "same-origin" })).json();
      const overview = await getJson("/api/v2/setup/overview");
      const program = (overview.programs || []).find((p) => p.name === "Browser Program");
      const season = (overview.seasons || []).find((s) => s.name === "Browser Season"
        && program && s.program_id === program.id);
      const division = (overview.divisions || []).find((d) => d.name === "Browser Division"
        && season && d.season_id === season.id);
      const club = (overview.clubs || []).find((c) => c.name === "Browser Club");
      const venue = (overview.venues || []).find((v) => v.name === "Browser Arena");
      const teams = (overview.teams || []).filter((t) => t.name === "Browser Team"
        && program && t.program_id === program.id);
      const team = teams[0];
      const playersResp = team ? await getJson(`/api/players?team_id=${team.id}`) : [];
      const players = (Array.isArray(playersResp) ? playersResp : [])
        .filter((p) => p.name === "Browser Player");
      const regsResp = season
        ? await getJson(`/api/v2/setup/seasons/${season.id}/team-registrations`)
        : { registrations: [] };
      const regs = team ? (regsResp.registrations || []).filter((r) => r.team_id === team.id) : [];
      const vaResp = season
        ? await getJson(`/api/v2/setup/seasons/${season.id}/venue-access`)
        : { venue_access: [] };
      const grants = venue
        ? (vaResp.venue_access || vaResp.grants || []).filter((g) => g.venue_id === venue.id)
        : [];
      return {
        programId: program && program.id, seasonId: season && season.id,
        divisionId: division && division.id, clubId: club && club.id,
        venueId: venue && venue.id, venueLeagueId: venue ? venue.league_id : "no-venue",
        teamCount: teams.length, teamClubId: team && team.club_id,
        playerCount: players.length,
        regCount: regs.length, regDivisionId: regs[0] && regs[0].division_id,
        regActive: regs[0] && regs[0].active,
        grantCount: grants.length, grantActive: grants[0] && grants[0].active,
      };
    });
    if (verify.teamCount !== 1) {
      throw new Error(`[${viewport.label}] expected exactly one imported Browser Team, got ${verify.teamCount}`);
    }
    if (verify.teamClubId !== verify.clubId) {
      throw new Error(`[${viewport.label}] imported team did not resolve Browser Club`);
    }
    // The canonical v2 overview omits Venue.league_id entirely (#233 Slice E)
    // rather than exposing a field that's always null — its absence from the
    // v2 API surface is itself the proof this importer never writes it.
    if (verify.venueLeagueId !== undefined && verify.venueLeagueId !== null) {
      throw new Error(`[${viewport.label}] imported venue must carry no league_id, got ${verify.venueLeagueId}`);
    }
    if (verify.playerCount !== 1) {
      throw new Error(`[${viewport.label}] expected exactly one imported Browser Player, got ${verify.playerCount}`);
    }
    if (verify.regCount !== 1 || verify.regDivisionId !== verify.divisionId || !verify.regActive) {
      throw new Error(`[${viewport.label}] expected one active registration in Browser Division: ${JSON.stringify(verify)}`);
    }
    if (verify.grantCount !== 1 || !verify.grantActive) {
      throw new Error(`[${viewport.label}] expected one active season venue access grant: ${JSON.stringify(verify)}`);
    }

    // (4) Idempotent repeat commit: same draft, same wizard answers — must
    // update/skip in place, never duplicate.
    const revalidateResponse = page.waitForResponse((response) =>
      response.url() === `${base}/api/import/commit/teams-players`
      && response.request().method() === "POST"
      && response.request().postData().includes('"dry_run":true'));
    await page.click("[data-hierarchy-validate]");
    await revalidateResponse;
    await page.getByRole("heading", { name: "Hierarchy validation passed" }).waitFor();
    await page.waitForSelector("[data-hierarchy-commit]:not([disabled])");
    const recommitResponse = page.waitForResponse((response) =>
      response.url() === `${base}/api/import/commit/teams-players`
      && response.request().method() === "POST"
      && response.request().postData().includes('"dry_run":false'));
    await page.click("[data-hierarchy-commit]");
    const recommitBody = await (await recommitResponse).json();
    if (!recommitBody.committed) {
      throw new Error(`[${viewport.label}] repeat commit failed: ${JSON.stringify(recommitBody)}`);
    }
    for (const [name, counts] of Object.entries(recommitBody.summary || {})) {
      if (counts.created !== 0) {
        throw new Error(`[${viewport.label}] repeat commit created new ${name} (${counts.created}) — not idempotent`);
      }
    }
    const afterRepeat = await readRecordCounts(page);
    if (afterRepeat.teams !== 1 || afterRepeat.players !== 1 || afterRepeat.clubs !== 1
        || afterRepeat.programs !== 1 || afterRepeat.organizations !== 1) {
      throw new Error(`[${viewport.label}] repeat commit duplicated records: ${JSON.stringify(afterRepeat)}`);
    }

    // (5) Invalid row blocks the whole batch, zero persisted changes.
    const beforeInvalid = await readRecordCounts(page);
    await page.fill('[data-hierarchy-sheet="registrations_csv"]', INVALID_REGISTRATIONS_CSV);
    const invalidValidateResponse = page.waitForResponse((response) =>
      response.url() === `${base}/api/import/commit/teams-players`
      && response.request().method() === "POST"
      && response.request().postData().includes('"dry_run":true'));
    await page.click("[data-hierarchy-validate]");
    await invalidValidateResponse;
    await page.getByRole("heading", { name: "Validation blocked the batch" }).waitFor();
    const commitDisabled = await page.getAttribute("[data-hierarchy-commit]", "disabled");
    if (commitDisabled === null) {
      throw new Error(`[${viewport.label}] commit must stay disabled after a failed validation`);
    }
    const afterInvalid = await readRecordCounts(page);
    if (JSON.stringify(afterInvalid) !== JSON.stringify(beforeInvalid)) {
      throw new Error(`[${viewport.label}] invalid row changed persisted records: `
        + `before=${JSON.stringify(beforeInvalid)} after=${JSON.stringify(afterInvalid)}`);
    }

    const browserStorage = await page.evaluate(() => JSON.stringify({
      local: { ...localStorage },
      session: { ...sessionStorage },
    }));
    for (const marker of ["BROWSERORG", "BROWSERPROGRAM", "BROWSERVENUE",
      "BROWSERDIV", "BROWSERTEAM", "BROWSERCLUB", "BROWSERPLAYER"]) {
      if (browserStorage.includes(marker)) {
        throw new Error(`[${viewport.label}] hierarchy data appeared in browser storage`);
      }
    }
    if (errors.length) {
      throw new Error(`[${viewport.label}] console/page errors:\n${errors.join("\n")}`);
    }
    console.log(`[${viewport.label}] OK — wizard order/visibility, fresh commit, `
      + "idempotent repeat, and invalid-row rollback all verified.");
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
      process.env.SMOKE_CHROMIUM_PATH
        ? { executablePath: process.env.SMOKE_CHROMIUM_PATH }
        : {},
    );
    for (const viewport of VIEWPORTS) await checkViewport(browser, viewport);
    console.log("Hierarchy import browser journey passed.");
  } catch (error) {
    console.error("Hierarchy import browser journey FAILED.");
    console.error(error && error.message ? error.message : error);
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}

main();
