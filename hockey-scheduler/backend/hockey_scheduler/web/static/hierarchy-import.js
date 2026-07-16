/* Complete client hierarchy import panel (#174 PR E2, #260 Slice F).

   This extends the existing Import screen without changing its current import
   types. Data lives only in this page's memory. Both validate and commit use the
   League-Admin-only import envelope with import_type="hierarchy"; the backend
   revalidates every sheet before the single-transaction commit.

   The nine-sheet CSV vocabulary here matches ADR 0001 / issue #260 exactly:
   organizations, programs, venues_rinks, competition, clubs, permanent_teams,
   players, registrations, season_venue_access. The "Setup profile" wizard below
   is pure UI-routing state (#260 review) — it only decides which sheet cards
   are shown and which hints appear; it has no backend representation, is never
   persisted, and every answer combination still submits through this one
   canonical import engine. */

const hierarchyImportTemplates = {
  organizations_csv:
    "organization_code,organization_name,short_name\n" +
    "CANLON,Canlon Ice Facilities,Canlon\n",
  programs_csv:
    "program_code,operator_organization_code,program_name,country,timezone\n" +
    "OVER55,CANLON,Over 55,US,America/Chicago\n",
  venues_rinks_csv:
    "venue_code,organization_code,venue_name,address,timezone,rink_code,rink_name\n" +
    "PLAINFIELD,CANLON,Plainfield Ice,123 Main St,America/Chicago,PF1,Rink 1\n",
  competition_csv:
    "program_code,season_code,season_name,league_code,league_name,league_sort_order,division_code,division_name,age_group\n" +
    "OVER55,FALL26,Fall 2026,L1,Adult League,1,DIVA,Division A,Adult\n",
  clubs_csv:
    "club_code,club_name,country\n" +
    "EAGLES,Eagles HC,US\n",
  permanent_teams_csv:
    "program_code,team_code,team_name,club_code\n" +
    "OVER55,LIONS,Lions,EAGLES\n",
  players_csv:
    "player_code,team_code,first_name,last_name,jersey_number,position,email\n" +
    "P001,LIONS,Jane,Smith,7,forward,jane@example.com\n",
  registrations_csv:
    "season_code,team_code,league_code,division_code\n" +
    "FALL26,LIONS,L1,DIVA\n",
  season_venue_access_csv:
    "season_code,venue_code,active\n" +
    "FALL26,PLAINFIELD,true\n",
};

let hierarchyImportState = {
  sheets: {
    organizations_csv: "", programs_csv: "", venues_rinks_csv: "",
    competition_csv: "", clubs_csv: "", permanent_teams_csv: "",
    players_csv: "", registrations_csv: "", season_venue_access_csv: "",
  },
  report: null,
  committed: null,
  validatedKey: null,
};

// Pure UI-routing state (#260 review decision — question 1 has no backend
// field, and neither does any other answer here): filters which sheet cards
// render and which contextual hints appear. Never sent to the server, never
// persisted across a reload.
let hierarchyWizardAnswers = {
  operatorType: "both",       // "competitions" | "venues" | "both"
  hasClubs: "yes",             // "yes" | "no"
  usesDivisions: "yes",        // "yes" | "no"
  importPlayers: "yes",        // "yes" | "no"
  venueCount: "one",           // "one" | "multiple"
  grantVenueAccess: "yes",     // "yes" | "no"
  importMode: "first-time",    // "first-time" | "repeat"
};

const hierarchySheetMeta = [
  ["organizations_csv", "organizations.csv", "Facility owners"],
  ["programs_csv", "programs.csv", "Programs and their operator"],
  ["venues_rinks_csv", "venues_rinks.csv", "Venues and rink surfaces"],
  ["competition_csv", "competition.csv", "Seasons, leagues, and divisions"],
  ["clubs_csv", "clubs.csv", "Clubs"],
  ["permanent_teams_csv", "permanent_teams.csv", "Permanent program teams"],
  ["players_csv", "players.csv", "Player rosters"],
  ["registrations_csv", "registrations.csv", "Season registrations"],
  ["season_venue_access_csv", "season_venue_access.csv", "Season venue access"],
];

function hierarchySheetVisible(key) {
  const a = hierarchyWizardAnswers;
  switch (key) {
    case "programs_csv":
    case "competition_csv":
    case "permanent_teams_csv":
    case "registrations_csv":
      return a.operatorType !== "venues";
    case "venues_rinks_csv":
      return a.operatorType !== "competitions";
    case "clubs_csv":
      return a.operatorType !== "venues" && a.hasClubs === "yes";
    case "players_csv":
      return a.operatorType !== "venues" && a.importPlayers === "yes";
    case "season_venue_access_csv":
      return a.grantVenueAccess === "yes";
    default:
      return true; // organizations_csv is always foundational
  }
}

function hierarchySheetHints(key) {
  const a = hierarchyWizardAnswers;
  const hints = [];
  if (key === "permanent_teams_csv" && a.hasClubs === "no") {
    hints.push("No clubs: leave club_code blank for every team.");
  }
  if (key === "competition_csv" && a.usesDivisions === "no") {
    hints.push("No divisions: leave division_code, division_name, and age_group blank on every row — teams register at the league level only.");
  }
  if (key === "registrations_csv" && a.usesDivisions === "no") {
    hints.push("No divisions: leave division_code blank on every row.");
  }
  if (key === "venues_rinks_csv" && a.venueCount === "multiple") {
    hints.push("Multiple venues: repeat a venue's venue_code once per rink row it owns, and use a distinct venue_code for each venue.");
  }
  if (key === "season_venue_access_csv" && a.venueCount === "multiple") {
    hints.push("Grant each season access to every venue it may use — one row per (season_code, venue_code) pair; a venue with no explicit row for a season stays unavailable to it.");
  }
  if (a.importMode === "repeat") {
    hints.push("Updating existing data: rows match by their stable code and update in place — a blank cell on a matched row clears that field (e.g. a blank club_code unassigns a team's club).");
  }
  return hints;
}

function hierarchyPayload(dryRun) {
  return { import_type: "hierarchy", dry_run: !!dryRun, ...hierarchyImportState.sheets };
}

function hierarchySnapshot() {
  return JSON.stringify(hierarchyImportState.sheets);
}

function hierarchyResultHtml() {
  const result = hierarchyImportState.committed || hierarchyImportState.report;
  if (!result) return "";
  if (result.error) {
    return `<div class="banner alert"><h2>Hierarchy import failed</h2><p>${esc(result.error.message)}</p></div>`;
  }
  const errors = result.errors || [];
  const warnings = result.warnings || [];
  const rows = result.committed && result.summary
    ? Object.entries(result.summary).map(([name, counts]) => `<div class="li">
        <div class="li-main"><div class="li-title">${esc(name)}</div>
        <div class="li-sub">Created ${counts.created} · Updated ${counts.updated} · Skipped ${counts.skipped}</div></div></div>`).join("")
    : Object.entries(result.entities || {}).map(([name, count]) => `<div class="li">
        <div class="li-main"><div class="li-title">${esc(name)}</div><div class="li-sub">${count} row(s) resolved</div></div></div>`).join("");
  const problems = errors.map((item) => `<li><code>${esc(item.sheet)}:${item.row}</code> ${esc(item.message)}</li>`).join("");
  const cautions = warnings.map((item) => `<li>${esc(item.message)}</li>`).join("");
  const kind = errors.length ? "alert" : result.committed ? "ok" : "neutral";
  const title = errors.length ? "Validation blocked the batch" : result.committed ? "Hierarchy committed" : "Hierarchy validation passed";
  return `<div class="banner ${kind}"><h2>${title}</h2>
      <p>${errors.length ? `${errors.length} error(s); nothing was written.` : "All cross-file references are valid."}</p></div>
    ${rows ? `<div class="card">${rows}</div>` : ""}
    ${problems ? `<div class="card"><div class="section-title">Errors</div><ul>${problems}</ul></div>` : ""}
    ${cautions ? `<div class="card"><div class="section-title">Warnings</div><ul>${cautions}</ul></div>` : ""}`;
}

function renderHierarchyWizard() {
  const a = hierarchyWizardAnswers;
  const question = (key, label, options) => `<div class="li" style="display:block" data-hierarchy-wizard-question="${key}">
      <div class="li-title">${esc(label)}</div>
      <div class="actions">${options.map(([value, text]) =>
        `<button type="button" class="act ${a[key] === value ? "primary" : "ghost"}" data-hierarchy-wizard="${key}" data-hierarchy-wizard-value="${value}">${esc(text)}</button>`).join("")}</div>
    </div>`;
  return `<section class="card" id="hierarchy-wizard">
      <div class="section-title" style="margin-top:0">Setup profile</div>
      <p class="muted">Answer these to show only the sheets you need below. They only control what's shown here — every combination still validates and commits through the same import.</p>
      ${question("operatorType", "What are you setting up today?", [
        ["both", "Competitions & venues"], ["competitions", "Competitions only"], ["venues", "Venues only"]])}
      ${question("hasClubs", "Do your teams belong to clubs?", [["yes", "Yes"], ["no", "No"]])}
      ${question("usesDivisions", "Do your leagues use divisions?", [["yes", "Yes"], ["no", "No"]])}
      ${question("importPlayers", "Import player rosters now?", [["yes", "Yes"], ["no", "Not yet"]])}
      ${question("venueCount", "How many venues host your ice?", [["one", "One"], ["multiple", "Multiple"]])}
      ${question("grantVenueAccess", "Grant seasons access to venue ice in this import?", [["yes", "Yes"], ["no", "Not yet"]])}
      ${question("importMode", "Is this a first-time import or an update?", [["first-time", "First-time"], ["repeat", "Updating existing data"]])}
    </section>`;
}

function renderHierarchyImportPanel() {
  if (!hasPerm("manage_setup")) return "";
  const visible = hierarchySheetMeta.filter(([key]) => hierarchySheetVisible(key));
  const fields = visible.map(([key, filename, label]) => {
    const hints = hierarchySheetHints(key);
    const hintHtml = hints.length
      ? `<p class="muted" style="margin:4px 0">${hints.map((h) => esc(h)).join(" ")}</p>` : "";
    return `<section class="card" style="margin:0">
      <div class="section-title" style="margin-top:0">${esc(label)}</div>
      <div class="actions"><button type="button" class="act ghost" data-hierarchy-template="${key}">Download ${esc(filename)}</button></div>
      ${hintHtml}
      <textarea data-hierarchy-sheet="${key}" rows="6" style="width:100%;box-sizing:border-box" placeholder="Paste ${esc(filename)} here">${esc(hierarchyImportState.sheets[key])}</textarea>
    </section>`;
  }).join("");
  const clean = hierarchyImportState.report && hierarchyImportState.report.ok
    && hierarchyImportState.validatedKey === hierarchySnapshot();
  return `<section class="card" id="hierarchy-import-panel">
      <div class="section-title" style="margin-top:0">Complete client hierarchy</div>
      <p class="muted">Import owners, programs, venues/rinks, seasons, leagues, divisions, clubs, permanent teams, player rosters, season registrations, and season venue access using stable codes. Validate checks every sheet and existing records before any write.</p>
      ${renderHierarchyWizard()}
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px">${fields}</div>
      <div class="actions">
        <button type="button" class="act primary" data-hierarchy-validate>Validate hierarchy</button>
        <button type="button" class="act success" data-hierarchy-commit ${clean ? "" : "disabled"}>Commit hierarchy</button>
      </div>
      ${hierarchyResultHtml()}
    </section>`;
}

const hierarchyBaseRenderImport = renderImport;
renderImport = function renderImportWithHierarchy(overview) {
  return renderHierarchyImportPanel() + hierarchyBaseRenderImport(overview);
};

function captureHierarchySheets(container) {
  container.querySelectorAll("[data-hierarchy-sheet]").forEach((field) => {
    hierarchyImportState.sheets[field.dataset.hierarchySheet] = field.value;
  });
}

function wireHierarchyImport(container) {
  container.querySelectorAll("[data-hierarchy-wizard]").forEach((button) => {
    button.onclick = async () => {
      captureHierarchySheets(container);
      hierarchyWizardAnswers[button.dataset.hierarchyWizard] = button.dataset.hierarchyWizardValue;
      await render();
    };
  });
  container.querySelectorAll("[data-hierarchy-sheet]").forEach((field) => {
    field.oninput = () => {
      hierarchyImportState.sheets[field.dataset.hierarchySheet] = field.value;
      hierarchyImportState.report = null;
      hierarchyImportState.committed = null;
      hierarchyImportState.validatedKey = null;
    };
  });
  container.querySelectorAll("[data-hierarchy-template]").forEach((button) => {
    button.onclick = () => {
      const key = button.dataset.hierarchyTemplate;
      const filename = hierarchySheetMeta.find((row) => row[0] === key)[1];
      downloadTextFile(filename, hierarchyImportTemplates[key]);
    };
  });
  const validate = container.querySelector("[data-hierarchy-validate]");
  if (validate) validate.onclick = async () => {
    captureHierarchySheets(container);
    hierarchyImportState.committed = null;
    hierarchyImportState.report = await post("/api/import/commit/teams-players", hierarchyPayload(true));
    hierarchyImportState.validatedKey = hierarchyImportState.report && hierarchyImportState.report.ok
      ? hierarchySnapshot() : null;
    await render();
  };
  const commit = container.querySelector("[data-hierarchy-commit]");
  if (commit) commit.onclick = async () => {
    captureHierarchySheets(container);
    if (!hierarchyImportState.report || !hierarchyImportState.report.ok
        || hierarchyImportState.validatedKey !== hierarchySnapshot()) return;
    hierarchyImportState.committed = await post("/api/import/commit/teams-players", hierarchyPayload(false));
    if (hierarchyImportState.committed && hierarchyImportState.committed.committed) {
      toast = "Hierarchy import committed.";
      onboardingStatusDirty = true;
    }
    await render();
  };
}

const hierarchyBaseRender = render;
render = async function renderWithHierarchyImport() {
  const result = await hierarchyBaseRender();
  if (view === "import" && hasPerm("manage_setup")) {
    const content = document.getElementById("content");
    if (content) wireHierarchyImport(content);
  }
  return result;
};
