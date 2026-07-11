/* Complete client hierarchy import panel (#174 PR E2).

   This extends the existing Import screen without changing its current import
   types. Data lives only in this page's memory. Both validate and commit use the
   League-Admin-only import envelope with import_type="hierarchy"; the backend
   revalidates the same four files before the single-transaction commit. */

const hierarchyImportTemplates = {
  organizations_csv:
    "organization_code,organization_name,short_name\n" +
    "CANLON,Canlon Ice Facilities,Canlon\n",
  leagues_csv:
    "league_code,organization_code,league_name,country,timezone\n" +
    "OVER55,CANLON,Over 55,US,America/Chicago\n",
  venues_rinks_csv:
    "venue_code,organization_code,league_code,venue_name,address,timezone,rink_code,rink_name\n" +
    "PLAINFIELD,CANLON,OVER55,Plainfield Ice,123 Main St,America/Chicago,PF1,Rink 1\n",
  competition_csv:
    "league_code,season_code,season_name,level_code,level_name,level_sort_order,division_code,division_name,age_group\n" +
    "OVER55,FALL26,Fall 2026,L1,Level 1,1,DIVA,Division A,Adult\n",
};

let hierarchyImportState = {
  sheets: { organizations_csv: "", leagues_csv: "", venues_rinks_csv: "", competition_csv: "" },
  report: null,
  committed: null,
  validatedKey: null,
};

const hierarchySheetMeta = [
  ["organizations_csv", "organizations.csv", "Facility owners"],
  ["leagues_csv", "leagues.csv", "Leagues and their owner"],
  ["venues_rinks_csv", "venues_rinks.csv", "Venues and rink surfaces"],
  ["competition_csv", "competition.csv", "Seasons, levels, and divisions"],
];

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

function renderHierarchyImportPanel() {
  if (!hasPerm("manage_setup")) return "";
  const fields = hierarchySheetMeta.map(([key, filename, label]) => `<section class="card" style="margin:0">
      <div class="section-title" style="margin-top:0">${esc(label)}</div>
      <div class="actions"><button type="button" class="act ghost" data-hierarchy-template="${key}">Download ${esc(filename)}</button></div>
      <textarea data-hierarchy-sheet="${key}" rows="6" style="width:100%;box-sizing:border-box" placeholder="Paste ${esc(filename)} here">${esc(hierarchyImportState.sheets[key])}</textarea>
    </section>`).join("");
  const clean = hierarchyImportState.report && hierarchyImportState.report.ok
    && hierarchyImportState.validatedKey === hierarchySnapshot();
  return `<section class="card" id="hierarchy-import-panel">
      <div class="section-title" style="margin-top:0">Complete client hierarchy</div>
      <p class="muted">Import owners, leagues, venues/rinks, seasons, levels, and divisions using stable codes. Validate checks all four files and existing records before any write.</p>
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
