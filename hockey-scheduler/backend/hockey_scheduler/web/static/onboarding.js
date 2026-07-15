/* Guided Initial Setup UI (#174 PR D).

   This is a thin extension over the existing operator console. Progress is
   always fetched from /api/v2/onboarding/status (#233 Slice B2b — the
   canonical Program→Season→League→optional Division readiness check); the
   browser stores no completed step number and creates no parallel onboarding
   records. Actions deep-link to the existing Setup drawers, Users screen,
   Import wizard, and Calendar. */

NAV.onboarding = "Initial Setup";

let onboardingStatus = null;
let onboardingStatusDirty = true;
let onboardingRoutePending = true;
let onboardingDismissedForSession = false;

const onboardingBaseSetUser = setUser;
setUser = function setUserWithOnboarding(user) {
  const previous = currentUser ? currentUser.username : null;
  onboardingBaseSetUser(user);
  const next = currentUser ? currentUser.username : null;
  if (previous !== next) {
    onboardingStatus = null;
    onboardingStatusDirty = true;
    onboardingRoutePending = !!(currentUser && currentUser.role === "league_admin");
    onboardingDismissedForSession = false;
  }
  if (!hasPerm("manage_setup") && view === "onboarding") view = "dashboard";
};

const onboardingBaseGateChrome = gateChrome;
gateChrome = function gateChromeWithOnboarding() {
  onboardingBaseGateChrome();
  const tab = document.querySelector('.tab[data-tab="onboarding"]');
  if (tab) tab.style.display = hasPerm("manage_setup") ? "" : "none";
};

const onboardingBasePost = post;
post = async function postWithOnboardingRefresh(path, body) {
  const result = await onboardingBasePost(path, body);
  const changedSetup = path.startsWith("/api/setup/")
    || path.startsWith("/api/v2/setup/")
    || path === "/api/accounts"
    || path.startsWith("/api/import/commit/")
    || path.startsWith("/api/guardians/links");
  if (changedSetup && result && !result.error) onboardingStatusDirty = true;
  return result;
};

function onboardingActivateTab() {
  document.querySelectorAll(".tab").forEach((tab) =>
    tab.classList.toggle("active", tab.dataset.tab === view));
}

function onboardingStepMap(status) {
  return Object.fromEntries((status.steps || []).map((step) => [step.key, step]));
}

function onboardingWarningCodes(status) {
  return new Set((status.warnings || []).map((warning) => warning.code));
}

function updateOnboardingBadge(status) {
  const badge = document.getElementById("onboarding-badge");
  if (!badge || !hasPerm("manage_setup")) return;
  const steps = (status && status.steps) || [];
  const done = steps.filter((step) => step.status === "done").length;
  badge.hidden = false;
  badge.textContent = status && status.ready_to_schedule ? "Ready" : `${done}/${steps.length}`;
  badge.classList.toggle("ready", !!(status && status.ready_to_schedule));
}

async function loadOnboardingStatus(force) {
  if (!force && onboardingStatus && !onboardingStatusDirty) return onboardingStatus;
  // Canonical v2 readiness (#233 Slice B2b): same shape as v1
  // (complete/ready_to_schedule/steps/blocking/warnings) but v2 vocabulary —
  // see buildOnboardingGroups/nextOnboardingFix below for the key/code map.
  const status = await getJSON("/api/v2/onboarding/status");
  if (!status || status.error) {
    throw new Error((status && status.error && status.error.message)
      || "Could not load initial setup status.");
  }
  onboardingStatus = status;
  onboardingStatusDirty = false;
  updateOnboardingBadge(status);
  return status;
}

function onboardingAction(label, attrs, primary) {
  const data = Object.entries(attrs).map(([key, value]) =>
    `data-${key}="${esc(value)}"`).join(" ");
  return `<button type="button" class="act ${primary ? "primary" : "ghost"}" ${data}>${esc(label)}</button>`;
}

function onboardingCheck(label, done, detail) {
  return `<div class="onboarding-check ${done ? "done" : "todo"}">
    <span class="onboarding-check-icon" aria-hidden="true">${done ? "✓" : "○"}</span>
    <span><strong>${esc(label)}</strong>${detail ? `<small>${esc(detail)}</small>` : ""}</span>
  </div>`;
}

function onboardingGroupState(group, firstIncomplete) {
  if (group.done) return "done";
  if (group.optional) return "optional";
  return group.number === firstIncomplete ? "current" : "todo";
}

function renderOnboardingGroup(group, firstIncomplete) {
  const state = onboardingGroupState(group, firstIncomplete);
  const actions = (group.actions || []).map((action, index) =>
    onboardingAction(action.label, action.attrs, state === "current" && index === 0)).join("");
  return `<li class="onboarding-step ${state}" data-onboarding-step="${esc(group.key)}">
    <div class="onboarding-step-marker" aria-hidden="true">${group.done ? "✓" : group.number}</div>
    <div class="onboarding-step-body">
      <div class="onboarding-step-heading">
        <div><span class="onboarding-step-kicker">Step ${group.number}</span>
          <h3>${esc(group.title)}</h3></div>
        <span class="onboarding-state ${state}">${state === "done" ? "Complete" : state === "current" ? "Next" : state === "optional" ? "Optional" : "Not started"}</span>
      </div>
      <p>${esc(group.description)}</p>
      ${group.checks && group.checks.length ? `<div class="onboarding-checks">${group.checks.join("")}</div>` : ""}
      ${actions ? `<div class="onboarding-step-actions">${actions}</div>` : ""}
    </div>
  </li>`;
}

function buildOnboardingGroups(status) {
  const step = onboardingStepMap(status);
  const warnings = onboardingWarningCodes(status);
  const done = (...keys) => keys.every((key) => step[key] && step[key].status === "done");
  const detail = (key) => (step[key] && step[key].detail) || "";

  return [
    {
      number: 1, key: "organization", title: "Facility owner",
      description: "Create the organization that will own your venues as the facility owner. The same organization can separately operate your program — you link it as the program's operating organization in the next step. The two roles are distinct.",
      done: done("organization"),
      checks: [onboardingCheck("Organization", done("organization"), detail("organization"))],
      actions: [{ label: "Add facility owner", attrs: { "onboarding-drawer": "organization" } }],
    },
    {
      // Group key renamed league→program (#233 Slice B2b) to match the v2
      // backend step keys this group now reads (program/program_ownership) —
      // v1's umbrella step key was "league"; v2's is "program". The internal
      // onboarding-drawer token stays the frozen SETUP_ENTITIES kind
      // ("league" = the Program entity), unrelated to this group key.
      number: 2, key: "program", title: "Program",
      // #233 B2b review r2: the operator is optional and, when set, is simply
      // the organization that operates the Program — it may be the SAME
      // organization as a facility owner or a DIFFERENT one entirely (an
      // externally-operated program using another owner's venue is a valid
      // v2 configuration). Never imply the operator must match a facility
      // owner — that's exactly the coupling ADR 0001/#233 breaks.
      description: "Create the program. An operating organization is optional — when set, it's the organization that operates the program, which may be a different organization than any facility owner.",
      // #233 B2b review: operator_organization_id is nullable (B2a/ADR 0001)
      // — a Program with no operator is already complete, so group
      // completion depends only on "program", never "program_ownership".
      done: done("program"),
      checks: [
        onboardingCheck("Program created", done("program"), detail("program")),
        onboardingCheck("Operating organization assigned (optional)", done("program_ownership"), detail("program_ownership")),
      ],
      actions: [
        { label: "Add program", attrs: { "onboarding-drawer": "league" } },
        { label: "Repair assignments", attrs: { "onboarding-goto": "setup" } },
      ],
    },
    {
      number: 3, key: "venues", title: "Venues and rinks",
      description: "Assign a venue to the program — a temporary v1 compatibility link that Season-to-Venue access replaces in Slice E; venues remain owned by the organization — then add the rink surfaces used for scheduling.",
      done: done("venue", "rink"),
      checks: [
        onboardingCheck("Program-linked venue (temporary v1)", done("venue"), detail("venue")),
        onboardingCheck("Rink", done("rink"), detail("rink")),
      ],
      actions: [
        { label: done("venue") ? "Add rink" : "Add venue", attrs: { "onboarding-drawer": done("venue") ? "rink" : "venue" } },
        { label: "View hierarchy", attrs: { "onboarding-goto": "setup" } },
      ],
    },
    {
      number: 4, key: "season", title: "Season structure",
      description: "Create the playing season, then give it at least one league. Every season requires a grouping league; a division is an optional split of a league.",
      done: done("season", "league"),
      checks: [
        onboardingCheck("Season", done("season"), detail("season")),
        onboardingCheck("League", done("league"), detail("league")),
        onboardingCheck("Division (optional)", done("division"), detail("division")),
      ],
      // Action order follows the frozen v1 drawer dependencies: the league and
      // division drawers both require a Season, so before one exists the only
      // offered action is "Add season". Once a Season exists, lead with the
      // required league, then the optional division.
      actions: done("season")
        ? [
            { label: "Add league", attrs: { "onboarding-drawer": "level" } },
            { label: "Add division", attrs: { "onboarding-drawer": "division" } },
          ]
        : [{ label: "Add season", attrs: { "onboarding-drawer": "season" } }],
    },
    {
      number: 5, key: "teams", title: "Clubs and teams",
      description: "Create clubs and teams. Each team belongs permanently to its program; its per-season placement in a league (and an optional division) is set later under Season participation, not fixed here.",
      done: done("team"),
      checks: [onboardingCheck("At least one program team", done("team"), detail("team"))],
      actions: [
        { label: "Add club", attrs: { "onboarding-drawer": "club" } },
        { label: "Add team", attrs: { "onboarding-drawer": "team" } },
      ],
    },
    {
      number: 6, key: "people", title: "Players and officials",
      description: "Enter a small number manually or use the existing import templates for a larger client data set.",
      done: !warnings.has("no_players") && !warnings.has("no_officials"),
      optional: true,
      checks: [
        onboardingCheck("Players", !warnings.has("no_players"), warnings.has("no_players") ? "None added yet" : "Added"),
        onboardingCheck("Officials", !warnings.has("no_officials"), warnings.has("no_officials") ? "None added yet" : "Added"),
      ],
      actions: [
        { label: "Add player", attrs: { "onboarding-drawer": "player" } },
        { label: "Add official", attrs: { "onboarding-drawer": "official" } },
        { label: "Open Import", attrs: { "onboarding-goto": "import" } },
      ],
    },
    {
      number: 7, key: "ice", title: "Game ice inventory",
      description: "Add at least one available Game ice slot under a rink at a configured venue.",
      done: done("ice"),
      checks: [onboardingCheck("Available Game ice", done("ice"), detail("ice"))],
      actions: [
        { label: "Add game ice", attrs: { "onboarding-drawer": "ice-slot" } },
        { label: "Import rink and ice data", attrs: { "onboarding-goto": "import" } },
      ],
    },
    {
      number: 8, key: "staff", title: "Staff accounts",
      description: "Create arena managers, coaches, players, officials, guardians, or viewers in the existing Users screen.",
      done: false, optional: true,
      checks: [onboardingCheck("Additional accounts", false, "Optional before scheduling")],
      actions: [{ label: "Open Users", attrs: { "onboarding-goto": "users" } }],
    },
    {
      number: 9, key: "review", title: "Review and finish",
      description: status.ready_to_schedule
        ? "All structural blockers are resolved. Scheduling can begin."
        : "Review the remaining blockers and use the exact Fix action for the next item.",
      done: !!status.ready_to_schedule,
      checks: [onboardingCheck("Ready to schedule", !!status.ready_to_schedule,
        status.ready_to_schedule ? "No structural blockers" : `${(status.blocking || []).length} blocker(s) remaining`)],
      actions: status.ready_to_schedule
        ? [{ label: "Start scheduling", attrs: { "onboarding-goto": "calendar" } }]
        : [{ label: "Fix next blocker", attrs: { "onboarding-fix-next": "1" } }],
    },
  ];
}

// Blocking-code → drawer map, v2 vocabulary (#233 Slice B2b). v1's umbrella
// code was no_league (→ the "league" drawer token, the frozen Program
// entity); v2's umbrella code is no_program, same drawer. v2 adds no_league
// for the season-scoped grouping League (→ the "level" drawer token, the
// frozen League entity) — a concept v1 has no requirement for. v2 drops
// no_division entirely (Division is never blocking in v2).
//
// no_venue_assigned_to_program deliberately has NO drawer entry (#233 B2b
// review r1): canonical Venue creation is org-owned only (B2a) — the
// venue→program game-ice bridge can only be assigned post-creation, from the
// Setup hierarchy's allowlisted "⇄ Move" control, never from the create
// drawer. Opening the Venue drawer here would let an operator create another
// venue without ever clearing this blocker, so it falls through to the
// setup-view fallback below instead.
function nextOnboardingFix(status) {
  const code = status.blocking && status.blocking[0] && status.blocking[0].code;
  const drawers = {
    no_organization: "organization",
    no_program: "league",
    no_rink: "rink",
    no_available_ice: "ice-slot",
    no_season: "season",
    no_league: "level",
  };
  if (drawers[code]) return { type: "drawer", value: drawers[code] };
  // Club is optional on a Team (#233 Slice D): the fix routes straight to the
  // Team drawer rather than requiring a Club to exist first.
  if (code === "no_team") return { type: "drawer", value: "team" };
  if (["non_durable_store", "migrations_stale", "no_active_admin"].includes(code)) {
    return { type: "view", value: "readiness" };
  }
  // Fallback for codes with no dedicated drawer — no_venue_assigned_to_program
  // (see above), venue_owner_mismatch, seasons_without_program,
  // leagues_without_season, teams_without_program, no_participation,
  // invalid_registrations — the Setup hierarchy surfaces every one of these
  // under "Needs assignment" or Season participation (with the Venue→Program
  // bridge control specifically living on the Facility tree there), so
  // routing to setup/hierarchy is always a safe, capable landing spot.
  return { type: "view", value: "setup" };
}

function openOnboardingDrawer(kind) {
  onboardingDismissedForSession = true;
  onboardingRoutePending = false;
  setupView = "records";
  drawer = { kind };
  drawerError = "";
  drawerValues = {};
  switchTab("setup");
}

function leaveOnboarding(target) {
  onboardingDismissedForSession = true;
  onboardingRoutePending = false;
  if (target === "setup") setupView = "hierarchy";
  switchTab(target);
}

function wireOnboarding(container, status) {
  container.querySelectorAll("[data-onboarding-drawer]").forEach((button) => {
    button.onclick = () => openOnboardingDrawer(button.dataset.onboardingDrawer);
  });
  container.querySelectorAll("[data-onboarding-goto]").forEach((button) => {
    button.onclick = () => leaveOnboarding(button.dataset.onboardingGoto);
  });
  const next = container.querySelector("[data-onboarding-fix-next]");
  if (next) next.onclick = () => {
    const fix = nextOnboardingFix(status);
    if (fix.type === "drawer") openOnboardingDrawer(fix.value);
    else leaveOnboarding(fix.value);
  };
  const refresh = container.querySelector("[data-onboarding-refresh]");
  if (refresh) refresh.onclick = async () => {
    onboardingStatusDirty = true;
    await render();
  };
}

async function renderInitialSetup() {
  const content = document.getElementById("content");
  if (!content) return;
  onboardingActivateTab();
  document.getElementById("nav-title").textContent = NAV.onboarding;
  document.body.dataset.view = "onboarding";
  const breadcrumb = document.getElementById("breadcrumb");
  if (breadcrumb) breadcrumb.textContent = "Admin Setup · Initial Setup";
  updateToast();

  if (!hasPerm("manage_setup")) {
    content.innerHTML = `<div class="banner neutral"><h2>League Admin only</h2>
      <p>Initial Setup contains the competition and facility configuration and account readiness.</p></div>`;
    return;
  }

  content.innerHTML = `<div class="onboarding-loading"><div class="skeleton"></div><div class="skeleton"></div></div>`;
  let status;
  try {
    status = await loadOnboardingStatus(true);
  } catch (error) {
    content.innerHTML = `<div class="banner alert"><h2>Could not load Initial Setup</h2>
      <p>${esc(error.message || error)}</p></div>
      <div class="actions"><button type="button" class="act primary" data-onboarding-refresh="1">Retry</button></div>`;
    wireOnboarding(content, { blocking: [] });
    return;
  }

  const groups = buildOnboardingGroups(status);
  const firstIncompleteGroup = (groups.find((group) => !group.done && !group.optional) || {}).number;
  const required = groups.filter((group) => !group.optional);
  const completed = required.filter((group) => group.done).length;
  const percentage = required.length ? Math.round((completed / required.length) * 100) : 0;
  const foundations = onboardingStepMap(status);
  const foundationKeys = ["league_admin", "durable_storage", "migrations"];
  const foundationChecks = foundationKeys.map((key) => {
    const row = foundations[key] || { label: key, status: "todo", detail: "" };
    return onboardingCheck(row.label, row.status === "done", row.detail);
  }).join("");
  const blockerRows = (status.blocking || []).map((item) =>
    `<li><code>${esc(item.code)}</code><span>${esc(item.message)}</span></li>`).join("");
  const warningRows = (status.warnings || []).map((item) =>
    `<li><span aria-hidden="true">△</span><span>${esc(item.message)}</span></li>`).join("");

  content.innerHTML = `<div class="onboarding-shell">
    <section class="onboarding-hero">
      <div><span class="onboarding-eyebrow">Resumable client onboarding</span>
        <h2>${status.ready_to_schedule ? "Ready to schedule" : "Complete the program foundation"}</h2>
        <p>Progress is recalculated from saved records every time this page opens. You can leave and continue later without losing work.</p></div>
      <div class="onboarding-progress-card" aria-label="Required setup progress">
        <strong>${completed}/${required.length}</strong><span>required stages complete</span>
        <div class="onboarding-progress"><span style="width:${percentage}%"></span></div>
      </div>
    </section>

    <section class="onboarding-foundation-card">
      <div class="onboarding-section-head"><div><span class="onboarding-eyebrow">Deployment foundation</span>
        <h3>Before program configuration</h3></div>
        <button type="button" class="act ghost" data-onboarding-goto="readiness">View deployment readiness</button></div>
      <div class="onboarding-foundation-grid">${foundationChecks}</div>
    </section>

    ${status.ready_to_schedule
      ? `<div class="banner ok"><h2>Structural setup is ready</h2><p>Warnings below are recommended follow-up work, not scheduling blockers.</p></div>`
      : `<div class="banner warn"><h2>${(status.blocking || []).length} blocker(s) remain</h2>
          <p>Resolve the next highlighted stage or use Fix next blocker.</p></div>`}

    <ol class="onboarding-step-list">${groups.map((group) => renderOnboardingGroup(group, firstIncompleteGroup)).join("")}</ol>

    <div class="onboarding-review-grid">
      <section class="card onboarding-issues"><div class="section-title" style="margin-top:0">Blocking</div>
        ${blockerRows ? `<ul>${blockerRows}</ul>` : `<div class="empty">No structural blockers.</div>`}</section>
      <section class="card onboarding-issues warning"><div class="section-title" style="margin-top:0">Warnings</div>
        ${warningRows ? `<ul>${warningRows}</ul>` : `<div class="empty">No onboarding warnings.</div>`}</section>
    </div>

    <div class="onboarding-footer-actions">
      <button type="button" class="act ghost" data-onboarding-goto="dashboard">Continue setup later</button>
      <button type="button" class="act ghost" data-onboarding-refresh="1">Refresh progress</button>
      ${status.ready_to_schedule
        ? `<button type="button" class="act primary" data-onboarding-goto="calendar">Start scheduling</button>`
        : `<button type="button" class="act primary" data-onboarding-fix-next="1">Fix next blocker</button>`}
    </div>
  </div>`;
  wireOnboarding(content, status);
}

const onboardingBaseRender = render;
render = async function renderWithInitialSetup() {
  if (currentUser && hasPerm("manage_setup") && onboardingRoutePending
      && !onboardingDismissedForSession && view === "dashboard") {
    onboardingRoutePending = false;
    try {
      const status = await loadOnboardingStatus(false);
      if (!status.ready_to_schedule) {
        view = "onboarding";
        onboardingActivateTab();
      }
    } catch (_) {
      // A status failure must not block the rest of the application shell.
    }
  }

  if (view === "onboarding") return renderInitialSetup();
  const result = await onboardingBaseRender();
  if (currentUser && hasPerm("manage_setup")) {
    if (onboardingStatusDirty || !onboardingStatus) {
      loadOnboardingStatus(false).catch(() => {});
    } else {
      updateOnboardingBadge(onboardingStatus);
    }
  }
  return result;
};

// app.js begins its async bootstrap before this extension file is requested. On
// a fast local/production connection that bootstrap can finish and render the
// dashboard before the wrappers above are installed. Reconcile that already-
// authenticated state once after load; if bootstrap is still pending, the
// wrapped setUser/render path handles it instead. This closes the timing race
// without storing navigation or setup progress in the browser.
if (currentUser && hasPerm("manage_setup") && view === "dashboard") {
  onboardingRoutePending = true;
  Promise.resolve().then(() => render()).catch(() => {});
}
