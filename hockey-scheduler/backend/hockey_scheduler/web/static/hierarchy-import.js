/* Complete client hierarchy import panel (#174 PR E2, #260 Slice F).

   This extends the existing Import screen without changing its current import
   types. Data lives only in this page's memory. Both validate and commit use the
   League-Admin-only import envelope with import_type="hierarchy"; the backend
   revalidates every sheet before the single-transaction commit.

   The nine-sheet CSV vocabulary here matches ADR 0001 / issue #260 exactly:
   organizations, programs, venues_rinks, competition, clubs, permanent_teams,
   players, registrations, season_venue_access.

   The "Setup profile" wizard (Q1-Q7, #260 review, locked) is pure UI-routing
   state: it only decides which sheet cards, sections, and rows are shown or
   required. It has no backend representation, is never persisted, creates no
   records, and never becomes a second source of truth — every answer
   combination still submits through this one canonical import engine, and the
   canonical import rows plus backend validation remain authoritative. Q2/Q3/Q6
   pickers offer codes already typed into the current draft AND (for an
   incremental import) already-persisted codes fetched read-only from
   /api/import/hierarchy-codes. A handful of narrower, non-locked toggles
   (importing players now, updating vs. first-time data) exist only as
   subordinate controls inside their relevant sheet section — never as
   additional top-level wizard questions. */

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

function freshHierarchyImportState() {
  return {
    sheets: {
    organizations_csv: "", programs_csv: "", venues_rinks_csv: "",
    competition_csv: "", clubs_csv: "", permanent_teams_csv: "",
    players_csv: "", registrations_csv: "", season_venue_access_csv: "",
    },
    report: null,
    committed: null,
    validatedKey: null,
  };
}

let hierarchyImportState = freshHierarchyImportState();

function freshHierarchyWizardAnswers() {
  return {
    operatorType: "both",   // Q1: "competitions" | "venues" | "both"
    programCodes: [],        // Q2: selected program_code values
    leagueCodes: [],         // Q3: selected league_code values (filtered by Q2)
    hasClubs: "yes",          // Q4: "yes" | "no"
    usesDivisions: "yes",     // Q5: "yes" | "no"
    venueCodes: [],           // Q6: selected venue_code values
    venueMode: "one",         // Q7: "one" | "multiple"
  };
}

// Q1-Q7 (#260 review, locked) — pure UI-routing state, never persisted, no
// backend field. programCodes/leagueCodes/venueCodes are transient
// multi-selects: an empty array means "no filter yet", never "none".
let hierarchyWizardAnswers = freshHierarchyWizardAnswers();

function freshHierarchySubordinate() {
  return {
    importPlayersNow: true,
    updatingExisting: false,
  };
}

// Subordinate, non-locked controls (#260 review) — not part of Q1-Q7; they
// live inside the specific sheet section they affect.
let hierarchySubordinate = freshHierarchySubordinate();

// Existing persisted Program/League/Venue codes for Q2/Q3/Q6 (#260 review),
// fetched once, read-only. null = not loaded yet.
let hierarchyExistingCodes = null;
let hierarchyExistingCodesLoading = false;

registerIdentityResetHook(() => {
  hierarchyImportState = freshHierarchyImportState();
  hierarchyWizardAnswers = freshHierarchyWizardAnswers();
  hierarchySubordinate = freshHierarchySubordinate();
  hierarchyExistingCodes = null;
  hierarchyExistingCodesLoading = false;
});

async function ensureHierarchyExistingCodes() {
  if (hierarchyExistingCodes !== null || hierarchyExistingCodesLoading) return;
  const myIdentityEpoch = uiIdentityEpoch;
  hierarchyExistingCodesLoading = true;
  const result = await getJSON("/api/import/hierarchy-codes");
  if (myIdentityEpoch !== uiIdentityEpoch) return;
  hierarchyExistingCodes = (result && !result.error)
    ? result : { programs: [], leagues: [], venues: [] };
  hierarchyExistingCodesLoading = false;
  await render();
}

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

// -- draft CSV parsing (for Q2/Q3/Q6 picker options) -----------------------

function hierarchyDraftRows(sheetKey) {
  const text = hierarchyImportState.sheets[sheetKey] || "";
  const lines = text.split("\n").map((l) => l.trim()).filter(Boolean);
  if (lines.length < 2) return [];
  const header = lines[0].split(",").map((h) => h.trim());
  return lines.slice(1).map((line) => {
    const cells = line.split(",");
    const row = {};
    header.forEach((h, i) => { row[h] = (cells[i] || "").trim(); });
    return row;
  });
}

function hierarchyDraftCodes(sheetKey, columnName) {
  const codes = [];
  const seen = new Set();
  for (const row of hierarchyDraftRows(sheetKey)) {
    const code = row[columnName];
    if (code && !seen.has(code)) { seen.add(code); codes.push(code); }
  }
  return codes;
}

function hierarchyProgramOptions() {
  const seen = new Set();
  const options = [];
  for (const code of hierarchyDraftCodes("programs_csv", "program_code")) {
    seen.add(code);
    options.push({ code, label: code });
  }
  for (const p of (hierarchyExistingCodes && hierarchyExistingCodes.programs) || []) {
    if (seen.has(p.code)) continue;
    seen.add(p.code);
    options.push({ code: p.code, label: `${p.code} (existing)` });
  }
  return options;
}

function hierarchyLeagueOptions() {
  const wantedPrograms = hierarchyWizardAnswers.programCodes;
  const seen = new Set();
  const options = [];
  for (const row of hierarchyDraftRows("competition_csv")) {
    const code = row.league_code;
    if (!code || seen.has(code)) continue;
    if (wantedPrograms.length && !wantedPrograms.includes(row.program_code)) continue;
    seen.add(code);
    options.push({ code, label: code });
  }
  for (const lg of (hierarchyExistingCodes && hierarchyExistingCodes.leagues) || []) {
    if (seen.has(lg.code)) continue;
    if (wantedPrograms.length && !wantedPrograms.includes(lg.program_code)) continue;
    seen.add(lg.code);
    options.push({ code: lg.code, label: `${lg.code} (existing)` });
  }
  return options;
}

function hierarchyVenueOptions() {
  const seen = new Set();
  const options = [];
  for (const code of hierarchyDraftCodes("venues_rinks_csv", "venue_code")) {
    seen.add(code);
    options.push({ code, label: code });
  }
  for (const v of (hierarchyExistingCodes && hierarchyExistingCodes.venues) || []) {
    if (seen.has(v.code)) continue;
    seen.add(v.code);
    options.push({ code: v.code, label: `${v.code} (existing)` });
  }
  return options;
}

// -- visibility + hints, driven by Q1-Q7 -----------------------------------

function hierarchySheetVisible(key) {
  const a = hierarchyWizardAnswers;
  switch (key) {
    case "programs_csv":
    case "competition_csv":
    case "permanent_teams_csv":
    case "registrations_csv":
      return a.operatorType !== "venues";
    case "venues_rinks_csv":
    case "season_venue_access_csv":
      return a.operatorType !== "competitions";
    case "clubs_csv":
      return a.operatorType !== "venues" && a.hasClubs === "yes";
    case "players_csv":
      return a.operatorType !== "venues" && hierarchySubordinate.importPlayersNow;
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
  if (key === "competition_csv") {
    if (a.usesDivisions === "no") {
      hints.push("No divisions: leave division_code, division_name, and age_group blank on every row — teams register at the league level only.");
    }
    if (a.programCodes.length) {
      hints.push(`Only add rows under these programs: ${a.programCodes.join(", ")}.`);
    }
  }
  if (key === "registrations_csv") {
    if (a.usesDivisions === "no") {
      hints.push("No divisions: leave division_code blank on every row.");
    }
    if (a.leagueCodes.length) {
      hints.push(`Expected league_code values: ${a.leagueCodes.join(", ")}.`);
    }
  }
  if (key === "venues_rinks_csv" && a.venueMode === "multiple") {
    hints.push("Multiple venues: repeat a venue's venue_code once per rink row it owns, and use a distinct venue_code for each venue.");
  }
  if (key === "season_venue_access_csv") {
    if (a.venueMode === "multiple") {
      hints.push("Grant each season access to every venue it may use — one row per (season_code, venue_code) pair; a venue with no explicit row for a season stays unavailable to it.");
    }
    if (a.venueCodes.length) {
      hints.push(`Selected venues: ${a.venueCodes.join(", ")}.`);
    }
  }
  if (hierarchySubordinate.updatingExisting) {
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
  // #331 review round 19: shares the app.js renderImportRows() renderer
  // instead of its own inline <li> mapping. This panel is a SEPARATE
  // renderer from renderImportReport/renderImportResult (also
  // renderImportRows callers) -- it's the one the real hierarchy commit
  // panel calls, and round 18's affected-id fix (idList: renders
  // affected_registration_ids/affected_game_ids) never touched it, so a
  // team_registration_conflict (or any other reason naming exact
  // conflicting rows) surfaced here with no way to identify which rows.
  // Sharing the renderer instead of re-fixing this file's own copy keeps
  // both panels' error/warning shape identical (sheet/row/field/message/
  // affected ids) by construction, never just by convention.
  // #331 review round 19: shares the app.js renderImportRows() renderer
  // instead of its own inline <li> mapping. This panel is a SEPARATE
  // renderer from renderImportReport/renderImportResult (also
  // renderImportRows callers) -- it's the one the real hierarchy commit
  // panel calls, and round 18's affected-id fix (idList: renders
  // affected_registration_ids/affected_game_ids) never touched it, so a
  // team_registration_conflict (or any other reason naming exact
  // conflicting rows) surfaced here with no way to identify which rows.
  // Sharing the renderer instead of re-fixing this file's own copy keeps
  // both panels' error/warning shape identical (sheet/row/field/message/
  // affected ids) by construction, never just by convention.
  const problems = renderImportRows(errors, "error");
  const cautions = renderImportRows(warnings, "warn");
  const kind = errors.length ? "alert" : result.committed ? "ok" : "neutral";
  const title = errors.length ? "Validation blocked the batch" : result.committed ? "Hierarchy committed" : "Hierarchy validation passed";
  return `<div class="banner ${kind}"><h2>${title}</h2>
      <p>${errors.length ? `${errors.length} error(s); nothing was written.` : "All cross-file references are valid."}</p></div>
    ${rows ? `<div class="card">${rows}</div>` : ""}
    ${problems ? `<div class="card"><div class="section-title">Errors</div>${problems}</div>` : ""}
    ${cautions ? `<div class="card"><div class="section-title">Warnings</div>${cautions}</div>` : ""}`;
}

// -- the seven locked questions (#260 review) -------------------------------

function hierarchySingleQuestion(number, key, label, options) {
  const a = hierarchyWizardAnswers;
  return `<div class="li" style="display:block" data-hierarchy-wizard-question="${key}">
      <div class="li-title">Q${number}. ${esc(label)}</div>
      <div class="actions">${options.map(([value, text]) =>
        `<button type="button" class="act ${a[key] === value ? "primary" : "ghost"}" data-hierarchy-wizard="${key}" data-hierarchy-wizard-value="${esc(value)}">${esc(text)}</button>`).join("")}</div>
    </div>`;
}

function hierarchyMultiQuestion(number, key, label, options, emptyHint) {
  const selected = hierarchyWizardAnswers[key];
  const chips = options.length
    ? options.map((opt) => `<button type="button"
        class="act ${selected.includes(opt.code) ? "primary" : "ghost"}"
        data-hierarchy-wizard-multi="${key}" data-hierarchy-wizard-code="${esc(opt.code)}">${esc(opt.label)}</button>`).join("")
    : `<span class="muted">${esc(emptyHint)}</span>`;
  return `<div class="li" style="display:block" data-hierarchy-wizard-question="${key}">
      <div class="li-title">Q${number}. ${esc(label)}</div>
      <div class="actions">${chips}</div>
    </div>`;
}

function renderHierarchyWizard() {
  return `<section class="card" id="hierarchy-wizard">
      <div class="section-title" style="margin-top:0">Setup profile</div>
      <p class="muted">Seven questions to show only the sheets, sections, and rows you need below. They only control what's shown here — every answer combination still validates and commits through the same import.</p>
      ${hierarchySingleQuestion(1, "operatorType", "Are you operating competitions, providing venues, or both?", [
        ["both", "Both"], ["competitions", "Competitions"], ["venues", "Venues"]])}
      ${hierarchyMultiQuestion(2, "programCodes", "Which Programs do you run?", hierarchyProgramOptions(),
        "No program codes yet — enter them in programs.csv below, or leave blank to include every program.")}
      ${hierarchyMultiQuestion(3, "leagueCodes", "Which Leagues exist in this Season?", hierarchyLeagueOptions(),
        "No league codes yet — enter them in competition.csv below, or leave blank to include every league.")}
      ${hierarchySingleQuestion(4, "usesDivisions", "Does a League use Divisions?", [["yes", "Yes"], ["no", "No"]])}
      ${hierarchySingleQuestion(5, "hasClubs", "Do Teams use Clubs?", [["yes", "Yes"], ["no", "No"]])}
      ${hierarchyMultiQuestion(6, "venueCodes", "Which Venues may this Season use?", hierarchyVenueOptions(),
        "No venue codes yet — enter them in venues_rinks.csv below, or leave blank to include every venue.")}
      ${hierarchySingleQuestion(7, "venueMode", "Can the Season use multiple Venues?", [
        ["multiple", "Yes"], ["one", "No"]])}
    </section>`;
}

function renderHierarchyImportPanel() {
  if (!hasPerm("manage_setup")) return "";
  const visible = hierarchySheetMeta.filter(([key]) => hierarchySheetVisible(key));
  const fields = visible.map(([key, filename, label]) => {
    const hints = hierarchySheetHints(key);
    const hintHtml = hints.length
      ? `<p class="muted" style="margin:4px 0">${hints.map((h) => esc(h)).join(" ")}</p>` : "";
    const playersToggle = key === "players_csv"
      ? `<div class="actions" style="margin-bottom:4px">
          <button type="button" class="act ${hierarchySubordinate.importPlayersNow ? "primary" : "ghost"}" data-hierarchy-players-toggle="true">Import players now</button>
          <button type="button" class="act ${!hierarchySubordinate.importPlayersNow ? "primary" : "ghost"}" data-hierarchy-players-toggle="false">Skip for now</button>
        </div>` : "";
    return `<section class="card" style="margin:0">
      <div class="section-title" style="margin-top:0">${esc(label)}</div>
      ${playersToggle}
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
      <div class="actions">
        <button type="button" class="act ${hierarchySubordinate.updatingExisting ? "primary" : "ghost"}" data-hierarchy-updating-toggle="true">Updating existing data</button>
        <button type="button" class="act ${!hierarchySubordinate.updatingExisting ? "primary" : "ghost"}" data-hierarchy-updating-toggle="false">First-time import</button>
      </div>
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

function toggleHierarchyMultiCode(key, code) {
  const list = hierarchyWizardAnswers[key];
  const idx = list.indexOf(code);
  if (idx === -1) list.push(code); else list.splice(idx, 1);
}

function wireHierarchyImport(container) {
  container.querySelectorAll("[data-hierarchy-wizard]").forEach((button) => {
    button.onclick = async () => {
      captureHierarchySheets(container);
      hierarchyWizardAnswers[button.dataset.hierarchyWizard] = button.dataset.hierarchyWizardValue;
      await render();
    };
  });
  container.querySelectorAll("[data-hierarchy-wizard-multi]").forEach((button) => {
    button.onclick = async () => {
      captureHierarchySheets(container);
      toggleHierarchyMultiCode(button.dataset.hierarchyWizardMulti, button.dataset.hierarchyWizardCode);
      await render();
    };
  });
  container.querySelectorAll("[data-hierarchy-players-toggle]").forEach((button) => {
    button.onclick = async () => {
      captureHierarchySheets(container);
      hierarchySubordinate.importPlayersNow = button.dataset.hierarchyPlayersToggle === "true";
      await render();
    };
  });
  container.querySelectorAll("[data-hierarchy-updating-toggle]").forEach((button) => {
    button.onclick = async () => {
      captureHierarchySheets(container);
      hierarchySubordinate.updatingExisting = button.dataset.hierarchyUpdatingToggle === "true";
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
    ensureHierarchyExistingCodes();
  }
  return result;
};
