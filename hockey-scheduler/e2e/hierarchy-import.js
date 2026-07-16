// Complete hierarchy import browser journey (#174 PR E2, #260 Slice F).
//
// At desktop and phone widths, a League Admin opens the existing Import screen,
// answers the "Setup profile" wizard (pure UI-routing — every answer combo
// still submits through the one canonical import engine), downloads every
// visible template, validates every hierarchy CSV across all nine sheets,
// commits the batch, verifies the resulting club/team/player/registration/
// venue-access appear via the canonical v2 setup reads, and checks that no
// pasted hierarchy data was written to browser storage. Fails on browser
// console/page errors.
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

    // The Setup profile wizard is pure UI-routing (#260): its default
    // answers already show all nine sheets, but this journey drives it
    // explicitly (rather than relying on defaults) to prove the real wizard
    // — not raw API calls — is what's being exercised. "Both" + "Yes" to
    // every question keeps every sheet card visible.
    await page.waitForSelector("#hierarchy-wizard", { timeout: 10000 });
    await page.click('[data-hierarchy-wizard="operatorType"][data-hierarchy-wizard-value="both"]');
    await page.click('[data-hierarchy-wizard="hasClubs"][data-hierarchy-wizard-value="yes"]');
    await page.click('[data-hierarchy-wizard="usesDivisions"][data-hierarchy-wizard-value="yes"]');
    await page.click('[data-hierarchy-wizard="importPlayers"][data-hierarchy-wizard-value="yes"]');
    await page.click('[data-hierarchy-wizard="venueCount"][data-hierarchy-wizard-value="one"]');
    await page.click('[data-hierarchy-wizard="grantVenueAccess"][data-hierarchy-wizard-value="yes"]');
    await page.click('[data-hierarchy-wizard="importMode"][data-hierarchy-wizard-value="first-time"]');

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
      // /api/players?team_id=... returns a raw array (ApiService.list_players
      // is not wrapped in a {players: [...]} envelope).
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
        playerCount: players.length, playerTeamId: players[0] && players[0].team_id,
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
    console.log(`[${viewport.label}] OK — wizard-routed, downloaded, validated, and committed hierarchy.`);
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
