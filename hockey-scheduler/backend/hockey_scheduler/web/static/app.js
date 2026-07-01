/* Hockey Scheduler — calendar-first operator demo.
   Drives the real backend (setup + roster/substitute) via the documented API. */

let view = "dashboard";     // dashboard|setup|calendar|games|roster|activity|public
let gameView = "coach";     // coach | player (roster)
let rosterSide = "home";    // home | away — which lineup the roster tab shows (#25)
let rosterTeamId = null;    // team_id of the currently shown lineup (for copy)
let currentGame = null;     // game id whose roster we're viewing
let pickedPlayer = null;
let wizard = null;          // {slot_id, division_id, home_id, away_id} when scheduling
let calendarDate = "2026-09-05";  // YYYY-MM-DD shown on the arena calendar
let calendarMode = "day";   // day | week
let calFilters = { venueId: "all", rinkId: "all", divisionId: "all", teamId: "all" };
let toast = "";
let currentRole = "viewer";        // role of the signed-in user (from /api/auth/me)
let currentUser = null;            // {username, role, label} or null when signed out
let roleCatalog = [];              // [{id,label,permissions}] from /api/auth/roles
let accounts = [];                 // [{username,role,label}] demo sign-in options (#50)
let rolePerms = new Set();         // permissions of the current role
let movingGameId = null;    // click-to-move fallback: game awaiting a destination slot
let conflict = null;        // {ok, title, lines[], game, slot} — calendar side panel (#43)
let drawer = null;          // {kind} when a Setup create drawer is open (#44)
let drawerError = "";       // validation/API error shown inside the open drawer
let drawerValues = {};      // {fieldId: value} preserved across re-render on error

const DAY_MS = 86400000;
function addDays(dateStr, n) {
  const d = new Date(dateStr + "T00:00:00Z");
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}
function shiftDate(days) { calendarDate = addDays(calendarDate, days); }
function startOfWeek(dateStr) {
  const d = new Date(dateStr + "T00:00:00Z");
  const dow = (d.getUTCDay() + 6) % 7;  // Monday = 0
  return addDays(dateStr, -dow);
}
function weekDays(dateStr) {
  const mon = startOfWeek(dateStr);
  return Array.from({ length: 7 }, (_, i) => addDays(mon, i));
}
function fmtDate(d) {
  return new Date(d + "T00:00:00Z").toLocaleDateString("en-GB",
    { weekday: "short", day: "numeric", month: "short", year: "numeric", timeZone: "UTC" }).replace(",", "");
}
function fmtDayShort(d) {
  return new Date(d + "T00:00:00Z").toLocaleDateString("en-GB",
    { weekday: "short", day: "numeric", timeZone: "UTC" }).replace(",", "");
}

const NAV = {
  dashboard: "Dashboard", setup: "Setup", calendar: "Arena Calendar",
  games: "Games", roster: "Roster", sheet: "Game Sheet",
  activity: "Activity", public: "Public",
};
const POS_CLASS = { goalie: "pos-G", defense: "pos-D", forward: "pos-F", skater: "pos-D" };
const REPO = "https://github.com/jingizoo/biknik/issues";

const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (iso) => { const m = /T(\d{2}:\d{2})/.exec(iso || ""); return m ? m[1] : ""; };
const val = (id) => { const e = document.getElementById(id); return e ? e.value.trim() : ""; };
const hasPerm = (p) => rolePerms.has(p);

// The session cookie carries identity; the server resolves the role from it
// and authorizes each request (#50). No client-asserted role header.
async function getJSON(p) { return (await fetch(p, { credentials: "same-origin" })).json(); }
async function post(p, b) {
  const r = await fetch(p, { method: "POST", credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(b || {}) });
  const d = await r.json();
  if (d && d.error) toast = d.error.message;
  return d;
}

/* ---------- shared ---------- */
const bannerClass = (s) => s === "open_slot" ? "alert" : s === "needs_substitute" ? "warn" : s === "roster_confirmed" ? "ok" : "neutral";
const prettyStatus = (s) => ({
  draft: "Draft", selected: "Selected", awaiting_responses: "Awaiting Responses",
  roster_confirmed: "Roster Confirmed", needs_substitute: "Needs Substitute Decision",
  open_slot: "Open Slot", locked: "Roster Locked", final: "Final",
}[s] || s);
function statusBadge(p) {
  if (p.backed_out) return `<span class="badge red">Backed out</span>`;
  if (p.availability === "available" || p.roster_status === "confirmed" || p.roster_status === "accepted") return `<span class="badge green">Confirmed</span>`;
  if (p.availability === "unavailable") return `<span class="badge red">Unavailable</span>`;
  return `<span class="badge gray">Pending</span>`;
}
function avatar(p) {
  const i = esc(p.name).split(" ").map((x) => x[0]).slice(0, 2).join("");
  return `<div class="avatar ${POS_CLASS[p.position]}">${i}</div>`;
}
function slotBar(label, c, t, open) {
  const pct = t ? Math.round(((t - open) / t) * 100) : 0;
  return `<div class="slot"><div class="slot-top"><span class="label">${label}</span>
    <span class="count">${c}/${t} confirmed${open ? ` · ${open} open` : ""}</span></div>
    <div class="bar ${open ? "open" : ""}"><span style="width:${pct}%"></span></div></div>`;
}
const stub = (icon, title, sub, issue) => `<div class="stub"><span class="stub-ico">${icon}</span>
  <div class="stub-main"><div class="stub-title">${title}</div><div class="stub-sub">${sub}</div></div>
  <a class="badge" href="${REPO}/${issue}" target="_blank">#${issue}</a></div>`;
const toastHtml = () => toast ? `<div class="toast">${esc(toast)}</div>` : "";
const opt = (v, label, sel) => `<option value="${esc(v)}" ${sel ? "selected" : ""}>${esc(label)}</option>`;

/* ---------- Dashboard ---------- */
function renderDashboard(ov, board) {
  const avail = ov.ice_slots.filter((s) => s.status === "available").length;
  const confirmed = ov.schedule.filter((g) => g.roster_status === "roster_confirmed").length;
  const lg = ov.league || {};
  return `
    <div class="hero"><div class="when">${esc((ov.seasons[0] || {}).name || "")}</div>
      <h2>${esc(lg.name)}</h2><div class="where">${esc(lg.country || "")} · operator dashboard</div></div>
    <div class="stats">
      <div class="stat"><div class="n">${ov.schedule.length}</div><div class="l">Games scheduled</div></div>
      <div class="stat"><div class="n">${avail}</div><div class="l">Ice slots available</div></div>
      <div class="stat"><div class="n">${confirmed}</div><div class="l">Rosters confirmed</div></div>
      <div class="stat warn"><div class="n">0</div><div class="l">Officials assigned</div></div>
    </div>
    <div class="section-title">Primary actions</div>
    <div class="actions" style="flex-direction:column">
      <button class="act primary" data-goto="setup">Create league / teams</button>
      <button class="act primary" data-goto="calendar">Open arena calendar &amp; schedule</button>
      <button class="act ghost" data-goto="games">Game operations</button>
    </div>
    <div class="section-title">Follow-up modules</div>
    <div class="card">
      ${stub("🦓", "Officials assignment", "Referees & linespersons", 30)}
      ${stub("📊", "Results & standings", "Scores, standings, playoffs", 31)}
      ${stub("📨", "Notification delivery", "Push / email worker", 32)}
    </div>${toastHtml()}`;
}

/* ---------- Setup ---------- */
// Data-driven Setup (#44): each entity declares its card list projection and
// its create-form fields once, so the record cards and the create drawer stay
// in sync. The API endpoints are unchanged — the drawer just POSTs to them.
const SETUP_ENTITIES = [
  { key: "league", title: "Leagues", icon: "🏆", noun: "league", perm: "manage_setup",
    list: (ov) => ov.leagues.map((l) => ({ title: l.name })),
    fields: [{ id: "f-league", label: "League name", required: true, placeholder: "e.g. Coastal League" }] },
  { key: "season", title: "Seasons", icon: "🗓️", noun: "season", perm: "manage_setup",
    list: (ov) => ov.seasons.map((s) => ({ title: s.name, sub: nameById(ov.leagues, s.league_id) })),
    fields: [
      { id: "f-season-league", label: "League", type: "select", required: true, ofNoun: "league",
        options: (ov) => ov.leagues.map((l) => [l.id, l.name]) },
      { id: "f-season", label: "Season name", required: true, placeholder: "e.g. 2027–28" }] },
  { key: "division", title: "Divisions", icon: "🏅", noun: "division", perm: "manage_setup",
    list: (ov) => ov.divisions.map((d) => ({ title: d.name, sub: d.is_junior ? "Junior" : "" })),
    fields: [
      { id: "f-div-season", label: "Season", type: "select", required: true, ofNoun: "season",
        options: (ov) => ov.seasons.map((s) => [s.id, s.name]) },
      { id: "f-div", label: "Division name", required: true, placeholder: "e.g. U14" },
      { id: "f-div-age", label: "Age group", placeholder: "e.g. U14 (optional)" }] },
  { key: "club", title: "Clubs", icon: "🏒", noun: "club", perm: "manage_setup",
    list: (ov) => ov.clubs.map((c) => ({ title: c.name })),
    fields: [{ id: "f-club", label: "Club name", required: true, placeholder: "e.g. Eagles HC" }] },
  { key: "team", title: "Teams", icon: "👥", noun: "team", perm: "manage_setup",
    list: (ov) => ov.teams.map((t) => ({ title: t.name, sub: t.division_name || t.club_name || "" })),
    fields: [
      { id: "f-team-club", label: "Club", type: "select", required: true, ofNoun: "club",
        options: (ov) => ov.clubs.map((c) => [c.id, c.name]) },
      { id: "f-team-div", label: "Division", type: "select", required: true, ofNoun: "division",
        options: (ov) => ov.divisions.map((d) => [d.id, d.name]) },
      { id: "f-team", label: "Team name", required: true, placeholder: "e.g. U14 Eagles" }] },
  { key: "venue", title: "Venues", icon: "🏟️", noun: "venue", perm: "manage_arena",
    list: (ov) => ov.venues.map((v) => ({ title: v.name })),
    fields: [{ id: "f-venue", label: "Venue name", required: true, placeholder: "e.g. South Arena" }] },
  { key: "rink", title: "Rinks", icon: "⛸️", noun: "rink", perm: "manage_arena",
    list: (ov) => ov.rinks.map((r) => ({ title: r.name, sub: r.venue_name || "" })),
    fields: [
      { id: "f-rink-venue", label: "Venue", type: "select", required: true, ofNoun: "venue",
        options: (ov) => ov.venues.map((v) => [v.id, v.name]) },
      { id: "f-rink", label: "Rink name", required: true, placeholder: "e.g. Rink 3" }] },
  { key: "ice-slot", title: "Ice slots", icon: "🧊", noun: "ice slot", perm: "manage_arena",
    list: null,  // ice inventory is managed visually on the Arena Calendar
    fields: [
      { id: "f-slot-rink", label: "Rink", type: "select", required: true, ofNoun: "rink",
        options: (ov) => ov.rinks.map((r) => [r.id, `${r.venue_name ? r.venue_name + " · " : ""}${r.name}`]) },
      { id: "f-slot-date", label: "Date", type: "date", required: true, value: "2026-09-05" },
      { id: "f-slot-start", label: "Start", type: "time", required: true, value: "21:00" },
      { id: "f-slot-end", label: "End", type: "time", required: true, value: "22:30" },
      { id: "f-slot-type", label: "Type", type: "select", required: true,
        options: () => [["game", "Game"], ["practice", "Practice"], ["public_skate", "Public skate"],
                        ["maintenance", "Maintenance"], ["tournament", "Tournament"]] }] },
];

// Each entity's POST body, built from the drawer inputs (ids match the fields).
const SETUP_POST = {
  league: () => post("/api/setup/league", { name: val("f-league") }),
  season: () => post("/api/setup/season", { league_id: val("f-season-league"), name: val("f-season") }),
  division: () => post("/api/setup/division", { season_id: val("f-div-season"), name: val("f-div"), age_group: val("f-div-age") }),
  club: () => post("/api/setup/club", { name: val("f-club") }),
  team: () => post("/api/setup/team", { club_id: val("f-team-club"), division_id: val("f-team-div"), name: val("f-team") }),
  venue: () => post("/api/setup/venue", { name: val("f-venue") }),
  rink: () => post("/api/setup/rink", { venue_id: val("f-rink-venue"), name: val("f-rink") }),
  "ice-slot": () => post("/api/setup/ice-slot", {
    rink_id: val("f-slot-rink"),
    start_time: `${val("f-slot-date")}T${val("f-slot-start")}:00+00:00`,
    end_time: `${val("f-slot-date")}T${val("f-slot-end")}:00+00:00`,
    slot_type: val("f-slot-type"),
  }),
};

const nameById = (rows, id) => (rows.find((r) => r.id === id) || {}).name || "";

function renderSetup(ov) {
  const cards = SETUP_ENTITIES.map((ent) => setupCard(ent, ov)).join("");
  return `<div class="setup-intro">Create your league structure and arena. Tap
    <strong>＋ New</strong> on any card to open a form.</div>
    <div class="setup-grid">${cards}</div>${renderDrawer(ov)}${toastHtml()}`;
}

function setupCard(ent, ov) {
  const items = ent.list ? ent.list(ov) : null;
  let body;
  if (items === null) {
    body = `<div class="setup-hint">Ice inventory lives on the
      <button class="linklike" data-goto="calendar">Arena Calendar</button>.</div>`;
  } else if (!items.length) {
    body = `<div class="empty">None yet — create the first one.</div>`;
  } else {
    body = items.map((it) => `<div class="li"><div class="li-main">
      <div class="li-title">${esc(it.title)}</div>
      ${it.sub ? `<div class="li-sub">${esc(it.sub)}</div>` : ""}</div></div>`).join("");
  }
  const count = items ? `<span class="setup-count">${items.length}</span>` : "";
  const newBtn = hasPerm(ent.perm)
    ? `<button class="act primary sc-new" data-drawer="${ent.key}">＋ New</button>` : "";
  return `<section class="setup-card">
    <header class="setup-card-head"><span class="sc-ico">${ent.icon}</span>
      <span class="sc-title">${esc(ent.title)}</span>${count}${newBtn}</header>
    <div class="setup-card-body">${body}</div>
  </section>`;
}

function drawerField(f, ov) {
  const req = f.required ? ` <span class="req">*</span>` : "";
  // Preserve what the user already typed/selected across an error re-render;
  // fall back to the field's default only on first open.
  const current = f.id in drawerValues ? drawerValues[f.id] : (f.value || "");
  if (f.type === "select") {
    const rows = f.options(ov);
    if (!rows.length) {
      return `<label>${esc(f.label)}${req}</label>
        <div class="drawer-note">Create a ${esc(f.ofNoun || "record")} first.</div>`;
    }
    const sel = current || rows[0][0];
    const opts = rows.map(([v, label]) =>
      `<option value="${esc(v)}"${v === sel ? " selected" : ""}>${esc(label)}</option>`).join("");
    return `<label>${esc(f.label)}${req}</label><select id="${f.id}">${opts}</select>`;
  }
  const type = f.type || "text";
  const attrs = `${current ? ` value="${esc(current)}"` : ""}${f.placeholder ? ` placeholder="${esc(f.placeholder)}"` : ""}`;
  return `<label>${esc(f.label)}${req}</label><input id="${f.id}" type="${type}"${attrs} />`;
}

function renderDrawer(ov) {
  if (!drawer) return "";
  const ent = SETUP_ENTITIES.find((e) => e.key === drawer.kind);
  if (!ent) return "";
  const fields = ent.fields.map((f) => drawerField(f, ov)).join("");
  const err = drawerError ? `<div class="drawer-err">⚠ ${esc(drawerError)}</div>` : "";
  // A required select with no options can never be satisfied — block submit.
  const blocked = ent.fields.some((f) => f.type === "select" && f.required && !f.options(ov).length);
  return `<div class="drawer-scrim" data-drawer-close></div>
    <aside class="drawer" role="dialog" aria-modal="true" aria-label="New ${esc(ent.noun)}">
      <header class="drawer-head"><span class="drawer-ico">${ent.icon}</span>
        <span class="drawer-title">New ${esc(ent.noun)}</span>
        <button class="drawer-x" data-drawer-close aria-label="Close">×</button></header>
      <div class="drawer-body">${fields}${err}</div>
      <footer class="drawer-foot">
        <button class="act ghost" data-drawer-close>Cancel</button>
        <button class="act primary" data-drawer-submit="${ent.key}"${blocked ? " disabled" : ""}>Create ${esc(ent.noun)}</button>
      </footer>
    </aside>`;
}

/* ---------- Arena Calendar (day + week, filtered) ---------- */
function slotLabel(s) {
  if (s.game_label) return esc(s.game_label);
  if (s.status === "available") return "Available · Schedule";
  if (s.slot_type === "maintenance") return "Blocked · Maintenance";
  if (s.slot_type === "public_skate") return "Blocked · Public skate";
  if (s.slot_type === "practice") return "Blocked · Practice";
  if (s.slot_type === "tournament") return "Blocked · Tournament";
  return s.status;
}

function calContext(ov) {
  // rink_id → venue_id, and game lookups for division/team filtering.
  const rinkVenue = {};
  ov.rinks.forEach((r) => (rinkVenue[r.id] = r.venue_id));
  const gameById = {};
  ov.schedule.forEach((g) => (gameById[g.game_id] = g));
  return { rinkVenue, gameById };
}

function visibleRinks(ov) {
  return ov.rinks.filter((r) =>
    (calFilters.venueId === "all" || r.venue_id === calFilters.venueId) &&
    (calFilters.rinkId === "all" || r.id === calFilters.rinkId));
}

function slotPasses(s, ctx) {
  // Venue/rink restrict the ice inventory; division/team restrict allocated
  // games only — available ice stays visible.
  if (calFilters.venueId !== "all" && ctx.rinkVenue[s.rink_id] !== calFilters.venueId) return false;
  if (calFilters.rinkId !== "all" && s.rink_id !== calFilters.rinkId) return false;
  if (s.game_id) {
    const g = ctx.gameById[s.game_id];
    if (calFilters.divisionId !== "all" && (!g || g.division_id !== calFilters.divisionId)) return false;
    if (calFilters.teamId !== "all" &&
        (!g || (g.home_team_id !== calFilters.teamId && g.away_team_id !== calFilters.teamId))) return false;
  }
  return true;
}

function slotCard(s, draggable, ctx) {
  const cls = s.status === "available" ? "available" : s.slot_type === "maintenance" ? "maintenance" : s.status;
  const moving = movingGameId != null;
  // While a move is in progress, available game slots become click targets and
  // dragging is suspended so the two interaction modes don't fight.
  const canMove = hasPerm("manage_schedule");   // #24: arena manager / admin
  const isTarget = moving && draggable && s.status === "available";
  const dropClick = (draggable && s.status === "available" && canMove) ? `data-slot="${s.id}" data-drop="${s.id}"` : "";
  const drag = (draggable && s.game_id && !moving && canMove) ? `draggable="true" data-game="${s.game_id}"` : "";
  // Draft/Published state comes from the schedule game, not the slot row.
  const g = (s.game_id && ctx) ? ctx.gameById[s.game_id] : null;
  const state = g ? (g.published ? " · Published" : " · Draft") : "";
  const isMovingThis = s.game_id && s.game_id === movingGameId;
  // A Move button gives touch/mobile/keyboard users a drag-free path (#move-mode).
  const moveBtn = (draggable && s.game_id && !moving && canMove)
    ? `<button class="slot-move" data-move-game="${s.game_id}">Move</button>` : "";
  const extra = `${isTarget ? " move-target" : ""}${isMovingThis ? " moving" : ""}`;
  const cta = isTarget ? " · tap to move here" : (draggable && s.game_id && !moving && canMove ? " · drag or Move" : "");
  return `<div class="slot-card ${cls}${extra}" ${dropClick} ${drag}><div class="t">${fmt(s.start_time)}–${fmt(s.end_time)}</div><div class="s">${slotLabel(s)}${state}${cta}</div>${moveBtn}</div>`;
}

function calToolbar(ov) {
  const opt2 = (v, label, sel) => `<option value="${esc(v)}" ${sel ? "selected" : ""}>${esc(label)}</option>`;
  const venueOpts = `<option value="all">All venues</option>` +
    ov.venues.map((v) => opt2(v.id, v.name, v.id === calFilters.venueId)).join("");
  const rinkSrc = ov.rinks.filter((r) => calFilters.venueId === "all" || r.venue_id === calFilters.venueId);
  const rinkOpts = `<option value="all">All rinks</option>` +
    rinkSrc.map((r) => opt2(r.id, r.name, r.id === calFilters.rinkId)).join("");
  const divOpts = `<option value="all">All divisions</option>` +
    ov.divisions.map((d) => opt2(d.id, d.name, d.id === calFilters.divisionId)).join("");
  const teamOpts = `<option value="all">All teams</option>` +
    ov.teams.map((t) => opt2(t.id, t.name, t.id === calFilters.teamId)).join("");
  const head = calendarMode === "week"
    ? `Week of ${esc(fmtDate(startOfWeek(calendarDate)))}`
    : esc(fmtDate(calendarDate));
  return `
    <div class="cal-toolbar">
      <div class="cal-toprow">
        <div><div class="cal-date">${head}</div>
          <div class="cal-venue">${esc(calFilters.venueId === "all" ? "All venues" : (ov.venues.find((v) => v.id === calFilters.venueId) || {}).name || "Arena")}</div></div>
        <div class="cal-controls">
          <div class="seg-mini"><button class="segm ${calendarMode === "day" ? "active" : ""}" data-mode="day">Day</button>
            <button class="segm ${calendarMode === "week" ? "active" : ""}" data-mode="week">Week</button></div>
          <div class="cal-nav"><button class="act ghost" data-cal="-1">‹</button>
            <button class="act ghost" data-cal="0">Today</button>
            <button class="act ghost" data-cal="1">›</button></div>
        </div>
      </div>
      <div class="cal-filters">
        <select data-filter="venueId">${venueOpts}</select>
        <select data-filter="rinkId">${rinkOpts}</select>
        <select data-filter="divisionId">${divOpts}</select>
        <select data-filter="teamId">${teamOpts}</select>
      </div>
    </div>
    <div class="legend">
      <span><i class="dot lg-game"></i>Available</span>
      <span><i class="dot lg-alloc"></i>Allocated</span>
      <span><i class="dot lg-maint"></i>Maintenance</span>
      <span><i class="dot lg-skate"></i>Public skate</span>
    </div>`;
}

// Turn a /move API result into a conflict-panel model (#43). Explains *why* a
// drop was rejected, or — on success — what side effects the move triggered.
// The backend is authoritative: failures carry error.details.reason, successes
// carry a `moved` summary. We only translate those into operator-facing copy.
function buildConflict(res, ov, gameId, slotId) {
  const slotName = (id) => {
    const s = ov.ice_slots.find((x) => x.id === id);
    if (!s) return "that slot";
    const r = ov.rinks.find((rr) => rr.id === s.rink_id);
    return `${r ? r.name : "Rink"} ${fmt(s.start_time)}–${fmt(s.end_time)}`;
  };
  const gameName = (id) => {
    const g = ov.schedule.find((x) => x.game_id === id);
    return g ? `${g.home_team_name} vs ${g.away_team_name}` : "this game";
  };
  if (res && res.error) {
    const d = res.error.details || {};
    const reason = d.reason || res.error.code;
    const target = slotName(slotId);
    const MAP = {
      team_overlap: ["Team already booked",
        [`${gameName(gameId)} can't move to ${target}.`,
         "One of its teams already has a game overlapping that time. A team can't be in two places at once."]],
      slot_unavailable: ["Slot already taken",
        [`${target} is ${d.slot_status || "not available"}.`,
         "Drop the game onto an open (Available) slot, or free this one first."]],
      not_game_slot: ["Not a game slot",
        [`${target} is a ${(d.slot_type || "non-game").replace("_", " ")} slot.`,
         "Only slots reserved for games can host a fixture — maintenance and public-skate ice is off limits."]],
      same_slot: ["Already here", [`${gameName(gameId)} is already in ${target}.`]],
      game_cancelled: ["Game cancelled", ["A cancelled game can't be moved."]],
      game_missing: ["Game not found", ["That game no longer exists — refresh the calendar."]],
      slot_missing: ["Slot not found", ["That ice slot no longer exists — refresh the calendar."]],
    };
    const [title, lines] = MAP[reason] || ["Move blocked", [res.error.message]];
    return { ok: false, title, lines };
  }
  // Success — surface consequences worth a heads-up.
  const m = (res && res.moved) || {};
  const lines = [`${gameName(gameId)} now plays ${slotName(m.new_slot_id || slotId)}.`];
  if (m.unpublished) lines.push("It was published, so the fixture reverted to Draft — re-publish when you're ready.");
  if (m.roster_unlocked) lines.push("The roster was locked, so it reopened — players must reconfirm the new time.");
  return { ok: true, title: "Game moved", lines };
}

function conflictPanelHtml() {
  if (!conflict) return "";
  const cls = conflict.ok ? "ok" : "bad";
  const icon = conflict.ok ? "✅" : "⛔";
  return `<aside class="cal-aside ${cls}">
    <div class="ca-head"><span class="ca-ico">${icon}</span><span class="ca-title">${esc(conflict.title)}</span>
      <button class="ca-x" data-conflict-dismiss aria-label="Dismiss">×</button></div>
    <div class="ca-body">${conflict.lines.map((l) => `<p>${esc(l)}</p>`).join("")}</div>
  </aside>`;
}

function renderCalendar(ov) {
  if (wizard) return renderWizard(ov) + toastHtml();
  const ctx = calContext(ov);
  const rinks = visibleRinks(ov);
  const board = calendarMode === "week"
    ? renderWeek(ov, ctx, rinks)
    : renderDay(ov, ctx, rinks);
  return calToolbar(ov) +
    `<div class="cal-layout"><div class="cal-main">${board}</div>${conflictPanelHtml()}</div>` +
    toastHtml();
}

function moveBanner(ov) {
  if (movingGameId == null) return "";
  const g = ov.schedule.find((x) => x.game_id === movingGameId);
  const name = g ? `${g.home_team_name} vs ${g.away_team_name}` : "this game";
  return `<div class="move-banner"><span>🔀 Select an <strong>Available</strong> slot to move
    <strong>${esc(name)}</strong>.</span>
    <button class="act ghost" data-move-cancel>Cancel</button></div>`;
}

function renderDay(ov, ctx, rinks) {
  const moving = movingGameId != null;
  const canArena = hasPerm("manage_arena");   // #24: arena manager / admin adds ice
  const onDay = (s) => (s.start_time || "").startsWith(calendarDate) && slotPasses(s, ctx);
  const rows = rinks.map((r) => {
    const slots = ov.ice_slots.filter((s) => s.rink_id === r.id && onDay(s));
    const cards = slots.map((s) => slotCard(s, true, ctx)).join("")
      || `<div class="slot-card"><div class="s">No ice</div></div>`;
    const addIce = canArena
      ? `<div class="slot-card available" data-addslot="${r.id}"><div class="t">＋</div><div class="s">Add ice</div></div>` : "";
    return `<div class="cal-row"><div class="cal-rink">${esc(r.name)}</div>
      <div class="cal-slots">${cards}${addIce}</div></div>`;
  }).join("");
  // Draft tray (respects venue/rink/division/team filters by routing the draft
  // game's own ice slot through slotPasses, just like the allocated cards above).
  const slotById = {};
  ov.ice_slots.forEach((s) => (slotById[s.id] = s));
  const drafts = ov.schedule.filter((g) => {
    if (g.published || !(g.start_time || "").startsWith(calendarDate)) return false;
    const slot = slotById[g.ice_slot_id];
    return slot ? slotPasses(slot, ctx) : true;
  });
  const tray = drafts.length ? `<div class="tray"><span class="tray-label">Draft games</span>
    ${drafts.map((g) => `<span class="chip-drag${g.game_id === movingGameId ? " moving" : ""}" ${moving ? "" : `draggable="true" data-game="${g.game_id}"`}>⠿ ${esc(g.home_team_name)} vs ${esc(g.away_team_name)} · ${fmt(g.start_time)}${moving ? "" : ` <button class="chip-move" data-move-game="${g.game_id}">Move</button>`}</span>`).join("")}</div>` : "";
  const body = rinks.length
    ? moveBanner(ov) + tray + rows
    : `<div class="empty">No rinks match the selected filters.</div>`;
  return body + `<div class="privacy-note">📅 Tap an <strong>Available</strong> slot to schedule, or move a
    game onto available ice — <strong>drag</strong> it, or tap <strong>Move</strong> then tap a slot
    (works on touch). Moving changes the time/rink, so a published fixture is unpublished and a locked
    roster is unlocked. Validated server-side; this board is the source of truth (#33).</div>`;
}

function renderWeek(ov, ctx, rinks) {
  const days = weekDays(calendarDate);
  if (!rinks.length) return `<div class="empty">No rinks for the selected filters.</div>`;
  const grid = rinks.map((r) => {
    const cells = days.map((day) => {
      const slots = ov.ice_slots.filter((s) => s.rink_id === r.id
        && (s.start_time || "").startsWith(day) && slotPasses(s, ctx));
      const today = day === calendarDate ? " today" : "";
      const items = slots.length
        ? slots.map((s) => slotCard(s, false, ctx)).join("")
        : `<div class="wk-none">—</div>`;
      return `<div class="wk-cell${today}"><div class="wk-day">${esc(fmtDayShort(day))}</div>${items}</div>`;
    }).join("");
    return `<div class="wk-row"><div class="cal-rink">${esc(r.name)}</div><div class="wk-days">${cells}</div></div>`;
  }).join("");
  return grid + `<div class="privacy-note">📅 Week view is read-only.
    <strong>Switch to Day view to move games</strong> (drag, or tap Move then a slot).</div>`;
}

function renderWizard(ov) {
  const slot = ov.ice_slots.find((s) => s.id === wizard.slot_id);
  if (!slot) { wizard = null; return renderCalendar(ov); }
  const divs = ov.divisions;
  if (!wizard.division_id && divs[0]) wizard.division_id = divs[0].id;
  const teams = ov.teams.filter((t) => t.division_id === wizard.division_id);
  if (!teams.find((t) => t.id === wizard.home_id)) wizard.home_id = teams[0] ? teams[0].id : "";
  const awayTeams = teams.filter((t) => t.id !== wizard.home_id);
  if (!awayTeams.find((t) => t.id === wizard.away_id)) wizard.away_id = awayTeams[0] ? awayTeams[0].id : "";

  const sameDiv = wizard.home_id && wizard.away_id;
  const distinct = wizard.home_id && wizard.away_id && wizard.home_id !== wizard.away_id;
  const ok = sameDiv && distinct && slot.status === "available";
  const v = (good, t) => `<div class="valid ${good ? "ok" : "bad"}">${good ? "✓" : "✕"} ${t}</div>`;
  return `
    <div class="wizard">
      <h3>Schedule Game</h3>
      <div class="step">1 · Competition</div>
      <select id="w-div">${divs.map((d) => opt(d.id, d.name, d.id === wizard.division_id)).join("")}</select>
      <div class="step">2 · Teams</div>
      <select id="w-home">${teams.map((t) => opt(t.id, t.name, t.id === wizard.home_id)).join("")}</select>
      <div style="height:8px"></div>
      <select id="w-away">${awayTeams.map((t) => opt(t.id, t.name, t.id === wizard.away_id)).join("") || opt("", "—")}</select>
      <div class="step">3 · Ice</div>
      <div class="li"><span class="li-time">${fmt(slot.start_time)}–${fmt(slot.end_time)}</span>
        <div class="li-main"><div class="li-title">${esc(slot.rink_name)}</div>
          <div class="li-sub">${esc((ov.venues[0] || {}).name || "")}</div></div></div>
      <div class="step">4 · Validation</div>
      ${v(!!teams.length, "Same division")}
      ${v(distinct, "Home and away are different teams")}
      ${v(slot.status === "available", "Ice slot is available")}
      ${v(true, "Public-safe junior fixture (no PII)")}
      <div class="step">5 · Review</div>
      <div class="review">
        <div class="kv"><span class="k">Division</span><span class="v">${esc((divs.find((d) => d.id === wizard.division_id) || {}).name || "")}</span></div>
        <div class="kv"><span class="k">Home</span><span class="v">${esc((teams.find((t) => t.id === wizard.home_id) || {}).name || "—")}</span></div>
        <div class="kv"><span class="k">Away</span><span class="v">${esc((awayTeams.find((t) => t.id === wizard.away_id) || {}).name || "—")}</span></div>
        <div class="kv"><span class="k">Venue · Rink</span><span class="v">${esc((ov.venues[0] || {}).name || "")} · ${esc(slot.rink_name)}</span></div>
        <div class="kv"><span class="k">Time</span><span class="v">Sat ${fmt(slot.start_time)}–${fmt(slot.end_time)}</span></div>
      </div>
      <div class="actions">
        <button class="act ghost" data-wizcancel="1">Cancel</button>
        <button class="act primary" data-wizcreate="1" ${ok ? "" : "disabled"}>Create Draft Game</button>
      </div>
    </div>`;
}

/* ---------- Games + operations checklist ---------- */
function renderGames(ov) {
  if (!ov.schedule.length) return `<div class="empty">No games scheduled yet. Use the Calendar to schedule one.</div>`;
  return ov.schedule.map((g) => {
    const confirmed = g.roster_status === "roster_confirmed" || g.roster_status === "locked";
    const ck = (ok, lbl, meta) => `<div class="check ${ok ? "ok" : "todo"}"><span class="ic">${ok ? "✓" : "○"}</span>
      <span class="lbl">${lbl}</span>${meta ? `<span class="meta">${meta}</span>` : ""}</div>`;
    return `
      <div class="section-title">${esc(g.home_team_name)} vs ${esc(g.away_team_name)}</div>
      <div class="card">
        <div class="li"><span class="li-time">${fmt(g.start_time)}</span>
          <div class="li-main"><div class="li-title">${esc(g.division_name || "")}</div>
            <div class="li-sub">${esc(g.venue_name || "")} · ${esc(g.rink_name || "")}</div></div>
          <span class="pill ${g.published ? "scheduled" : "gray"}">${g.published ? "Published" : "Draft"}</span></div>
      </div>
      <div class="section-title">Game operations</div>
      <div class="card">
        ${ck(true, "Ice slot allocated")}
        ${ck(confirmed, "Roster", prettyStatus(g.roster_status))}
        ${ck(false, "Officials", "Coming #30")}
        ${ck(false, "Locker rooms", "Follow-up")}
        ${ck(false, "Scorekeeper", "Coming #31")}
        ${ck(g.published, "Public fixture", g.published ? "Published" : "Draft — not public")}
      </div>
      <div class="actions">
        <button class="act primary" data-openroster="${g.game_id}">Open Roster</button>
        ${g.published ? "" : `<button class="act success" data-publish="${g.game_id}">Publish</button>`}
      </div>`;
  }).join("") + toastHtml();
}

/* ---------- Roster (Coach/Player) ---------- */
function renderRoster(lineups) {
  if (!lineups) return `<div class="empty">Select a game from the Games tab.</div>`;
  if (!(rosterSide in lineups)) rosterSide = "home";
  const side = lineups[rosterSide];
  rosterTeamId = side.team_id;
  const tab = (key, icon, label) => {
    const l = lineups[key];
    return `<button class="ls ${rosterSide === key ? "active" : ""}" data-side="${key}">
      <span class="ls-team">${icon} ${esc(l.team_name)}</span>
      <span class="ls-sub">${label} · ${prettyStatus(l.status.status)}</span></button>`;
  };
  return `<div class="lineup-switch">${tab("home", "🏠", "Home")}${tab("away", "✈️", "Away")}</div>
    <div class="segmented">
      <button class="seg ${gameView === "coach" ? "active" : ""}" data-view="coach">Coach</button>
      <button class="seg ${gameView === "player" ? "active" : ""}" data-view="player">Player</button>
    </div><div style="padding-top:8px">${gameView === "coach" ? coachBody(side) : playerBody(side)}</div>`;
}

/* ---------- Game Sheet (read-only, both lineups) (#48) ---------- */
function fmtDateTime(iso) {
  if (!iso) return "Date TBD";
  const d = new Date(iso);
  const date = d.toLocaleDateString("en-GB",
    { weekday: "short", day: "numeric", month: "short", year: "numeric", timeZone: "UTC" }).replace(",", "");
  return `${date} · ${fmt(iso)}`;
}
function sheetSide(side, label) {
  const s = side.status;
  const occupying = side.players
    .filter((p) => p.group === "selected" && !p.backed_out)
    .sort((a, b) => (a.slot_type === b.slot_type
      ? (a.jersey_number || 99) - (b.jersey_number || 99)
      : (a.slot_type === "goalie" ? -1 : 1)));
  const warn = [];
  const plural = (n) => (n > 1 ? "s" : "");
  if (!occupying.length) warn.push("No players selected yet.");
  if (s.open_goalie_slots > 0) warn.push(`Need ${s.open_goalie_slots} more goalie${plural(s.open_goalie_slots)}.`);
  if (s.open_skater_slots > 0) warn.push(`Need ${s.open_skater_slots} more skater${plural(s.open_skater_slots)}.`);
  const warnHtml = warn.length ? `<div class="ros-warn">⚠ ${warn.map(esc).join(" ")}</div>` : "";
  const rows = occupying.length
    ? occupying.map((p) => `<div class="gs-row">
        <span class="gs-num">${p.jersey_number != null ? esc(p.jersey_number) : "—"}</span>
        ${posTag(p)}<span class="gs-name">${esc(p.name)}</span>${statusBadge(p)}</div>`).join("")
    : `<div class="empty">No lineup submitted.</div>`;
  const gF = s.target_goalies - s.open_goalie_slots;
  const sF = s.target_skaters - s.open_skater_slots;
  return `<section class="gs-side">
    <header class="gs-side-head">
      <div><div class="gs-side-team">${esc(side.team_name)}</div>
        <div class="gs-side-label">${label}</div></div>
      <span class="badge ${bannerClass(s.status) === "ok" ? "green" : bannerClass(s.status) === "alert" ? "red" : "gray"}">${prettyStatus(s.status)}</span>
    </header>
    <div class="gs-counts"><span>Goalies <strong>${gF}/${s.target_goalies}</strong></span>
      <span>Skaters <strong>${sF}/${s.target_skaters}</strong></span>
      <span>Confirmed <strong>${s.confirmed_goalies + s.confirmed_skaters}</strong></span></div>
    ${warnHtml}
    <div class="gs-roster">${rows}</div>
  </section>`;
}
function renderGameSheet(lineups) {
  if (!lineups) return `<div class="empty">Select a game from the Games tab.</div>`;
  const g = lineups.game;
  const pub = g.published
    ? `<span class="badge green">● Published</span>`
    : `<span class="badge gray">○ Draft</span>`;
  const lock = g.locked
    ? `<span class="badge orange">🔒 Locked</span>`
    : `<span class="badge blue">🔓 Open</span>`;
  const cancelled = g.cancelled ? `<span class="badge red">Cancelled</span>` : "";
  const placeholder = (icon, title, issue) => `<div class="gs-officials">
    <div class="gs-off-title">${icon} ${title}</div>
    <div class="gs-off-slots"><span class="gs-off-slot">To be assigned</span>
      <a class="badge" href="${REPO}/${issue}" target="_blank">#${issue}</a></div></div>`;
  return `<div class="game-sheet">
    <div class="gs-toolbar no-print">
      <span class="gs-hint">Read-only official game sheet — combines both lineups.</span>
      <button class="act ghost" data-print>🖨 Print / Export</button></div>
    <header class="gs-header">
      <div class="gs-match">${esc(lineups.home.team_name)} <span class="gs-vs">vs</span> ${esc(lineups.away.team_name)}</div>
      <div class="gs-meta">${esc(fmtDateTime(g.start_time))}${g.rink ? " · " + esc(g.rink) : ""}</div>
      <div class="gs-badges">${pub} ${lock} ${cancelled}</div>
    </header>
    <div class="gs-grid">
      ${sheetSide(lineups.home, "Home")}
      ${sheetSide(lineups.away, "Away")}
    </div>
    <div class="gs-grid">
      ${placeholder("👨‍⚖️", "Officials", 30)}
      ${placeholder("📝", "Scorekeeper / Results", 31)}
    </div>
    <div class="privacy-note">📋 Roster names are visible to authorized operators only. Score,
      penalties, official assignment, and signatures are follow-ups (auth in #24).</div>
  </div>`;
}

// Small building blocks shared by the roster-selection surface (#46).
function playerRow(p, right) {
  const jersey = p.jersey_number != null ? ` <span class="jersey">#${p.jersey_number}</span>` : "";
  return `<div class="row">${avatar(p)}<span class="name">${esc(p.name)}${jersey}</span>${right}</div>`;
}
function posTag(p) {
  const g = p.slot_type === "goalie";
  return `<span class="pos-tag ${g ? "g" : "s"}" title="${esc(p.position)}">${g ? "G" : "SK"}</span>`;
}
function posStat(label, filled, target, confirmed, open) {
  const pct = target ? Math.min(100, Math.round((filled / target) * 100)) : 0;
  return `<div class="pos-stat"><div class="ps-top"><span class="ps-label">${label}</span>
    <span class="ps-num">${filled}/${target} filled · ${confirmed} confirmed${open ? ` · ${open} open` : ""}</span></div>
    <div class="ps-bar ${open ? "open" : "full"}"><span style="width:${pct}%"></span></div></div>`;
}
function availableGroups(available, s, locked) {
  const mk = (label, st, open) => {
    const list = available.filter((p) => p.slot_type === st);
    if (!list.length) return "";
    const need = open > 0 ? `<span class="need">need ${open}</span>` : `<span class="need ok">full</span>`;
    const rows = list.map((p) => playerRow(p,
      `${posTag(p)}${locked ? "" : `<button class="act primary" data-act="select" data-id="${p.id}">Add</button>`}`)).join("");
    return `<div class="avail-group"><div class="avail-head">${label} ${need}</div>${rows}</div>`;
  };
  const body = available.length
    ? mk("Goalies", "goalie", s.open_goalie_slots) + mk("Skaters", "skater", s.open_skater_slots)
    : `<div class="empty">All eligible players are on the roster or in the sub pool.</div>`;
  return `<div class="section-title">Available players (${available.length})</div>
    <div class="card">${body}</div>`;
}

function coachBody(board) {
  const s = board.status;
  const locked = s.status === "locked";
  const canRoster = hasPerm("manage_roster");   // #24: coach/admin only
  const canEdit = !locked && canRoster;         // editable right now?
  const onRoster = board.players.filter((p) => p.group === "selected" && !p.backed_out);
  const backedOut = board.players.filter((p) => p.group === "selected" && p.backed_out);
  const available = board.players.filter((p) => p.group === "available");
  const subs = board.players.filter((p) => p.group === "substitute");

  if (!board.players.length) {
    return `<div class="banner neutral"><h2>No roster yet</h2>
      <p>The home team has no players. Add players in Setup first (team player
      management is a follow-up: #25).</p></div>${toastHtml()}`;
  }

  const gFilled = s.target_goalies - s.open_goalie_slots;
  const sFilled = s.target_skaters - s.open_skater_slots;
  const summary = `<div class="card ros-summary">
    ${posStat("Goalies", gFilled, s.target_goalies, s.confirmed_goalies, s.open_goalie_slots)}
    ${posStat("Skaters", sFilled, s.target_skaters, s.confirmed_skaters, s.open_skater_slots)}</div>`;

  // Eligibility / position warnings.
  const warn = [];
  const plural = (n) => (n > 1 ? "s" : "");
  if (s.open_goalie_slots > 0) warn.push(`Need ${s.open_goalie_slots} more goalie${plural(s.open_goalie_slots)}.`);
  if (s.open_skater_slots > 0) warn.push(`Need ${s.open_skater_slots} more skater${plural(s.open_skater_slots)}.`);
  if (backedOut.length) warn.push(`${backedOut.length} player${plural(backedOut.length)} backed out or removed — re-confirm or refill.`);
  const warnHtml = warn.length ? `<div class="ros-warn">⚠ ${warn.map(esc).join(" ")}</div>` : "";

  const toolbar = canEdit ? `<div class="ros-toolbar">
    <button class="act ghost" data-act="copy">⧉ Copy previous roster</button>
    <button class="act ghost" data-act="build">Auto-fill remaining</button></div>` : "";

  // Roster section: occupying players first, then anyone backed out / removed.
  const rosterRows = [...onRoster, ...backedOut].map((p) => {
    let btns = "";
    if (canEdit) {
      if (p.backed_out) {
        btns = p.roster_status === "removed"
          ? `<button class="act success" data-act="select" data-id="${p.id}">Add back</button>`
          : `<button class="act success" data-act="confirm" data-id="${p.id}">Re-confirm</button>`;
      } else if (p.availability === "available") {
        btns = `<button class="act danger" data-act="backout" data-id="${p.id}">Can't play</button>`;
      } else {
        btns = `<button class="act ghost" data-act="confirm" data-id="${p.id}">Confirm</button>`;
      }
      btns += `<button class="act danger ghost xbtn" data-act="remove" data-id="${p.id}" title="Remove from roster">✕</button>`;
    }
    return playerRow(p, `${posTag(p)}${statusBadge(p)}${btns}`);
  }).join("") || `<div class="empty">No players on the roster yet — add from Available below.</div>`;

  const subRows = subs.length ? subs.map((p) => {
    const canAdd = canEdit && (p.slot_type === "goalie" ? s.open_goalie_slots > 0 : s.open_skater_slots > 0);
    const ctrl = p.sub_status === "offered" ? '<span class="badge orange">Offered</span>' : '<span class="badge blue">Enrolled</span>';
    const btn = !canEdit ? "" : canAdd ? `<button class="act primary" data-act="add" data-id="${p.id}">Add</button>`
      : `<button class="act ghost" disabled>No slot</button>`;
    return playerRow(p, `${posTag(p)}${ctrl}${btn}`);
  }).join("") : `<div class="empty">No substitutes enrolled.</div>`;

  // Footer: lock control for roster managers, else a read-only note by role.
  let footer;
  if (!canRoster) {
    footer = `<div class="locked-note">🔒 Read-only — your role can't manage rosters.</div>`;
  } else if (locked) {
    footer = `<div class="locked-note">🔒 Roster locked. Selection disabled.
        <button class="act ghost" data-act="unlock" style="margin-left:auto">Unlock</button></div>`;
  } else {
    footer = `<div class="actions"><button class="act ghost" data-act="lock">Lock Roster</button></div>`;
  }

  const total = s.target_goalies + s.target_skaters;
  return `
    <div class="banner ${bannerClass(s.status)}"><h2>${prettyStatus(s.status)}</h2><p>${esc(s.message)}</p></div>
    ${summary}${warnHtml}${toolbar}
    <div class="section-title">Roster (${onRoster.length}/${total})</div>
    <div class="card">${rosterRows}</div>
    ${availableGroups(available, s, !canEdit)}
    <div class="section-title">Substitute pool</div>
    <div class="card">${subRows}</div>
    ${footer}
    ${toastHtml()}`;
}
function playerBody(board) {
  const players = board.players;
  const locked = board.status.status === "locked";
  if (!pickedPlayer || !players.find((p) => p.id === pickedPlayer)) pickedPlayer = players[0] ? players[0].id : null;
  const options = players.map((p) => opt(p.id, `${p.name} · ${p.position}`, p.id === pickedPlayer)).join("");
  const p = players.find((x) => x.id === pickedPlayer);
  const canRespond = hasPerm("respond_availability");   // #24: player/coach/admin
  const acts = (html) => !canRespond
    ? `<div class="locked-note">🔒 Read-only — your role can't respond for players.</div>`
    : locked ? `<div class="locked-note">🔒 Roster locked — actions disabled.</div>`
    : `<div class="actions">${html}</div>`;
  let card = `<div class="empty">No players.</div>`;
  if (p) {
    if (p.group === "selected" && !p.backed_out)
      card = `<div class="banner ok"><h2>You are selected</h2><p>Status: confirmed</p></div>
        ${acts(`<button class="act success" data-act="confirm" data-id="${p.id}">I'm Available</button>
                <button class="act danger" data-act="backout" data-id="${p.id}">I Can't Play</button>`)}`;
    else if (p.group === "selected" && p.backed_out)
      card = `<div class="banner alert"><h2>You marked yourself unavailable</h2><p>Coach notified.</p></div>
        ${acts(`<button class="act success" data-act="confirm" data-id="${p.id}">I'm Available again</button>`)}`;
    else if (p.group === "substitute" && p.sub_status === "offered")
      card = `<div class="banner warn"><h2>A slot is available</h2><p>Accept?</p></div>
        ${acts(`<button class="act success" data-act="accept" data-id="${p.id}">Accept</button>
                <button class="act danger" data-act="decline" data-id="${p.id}">Decline</button>`)}`;
    else if (p.group === "substitute")
      card = `<div class="banner neutral"><h2>Enrolled as substitute</h2><p>Waiting for a slot.</p></div>
        ${acts(`<button class="act ghost" data-act="withdraw" data-id="${p.id}">Withdraw</button>`)}`;
    else
      card = `<div class="banner neutral"><h2>Not selected</h2><p>Not enrolled.</p></div>
        ${acts(`<button class="act primary" data-act="enroll" data-id="${p.id}">Enroll as Substitute</button>`)}`;
  }
  return `<div class="section-title">View as player</div>
    <select class="player-picker" id="player-picker">${options}</select>${card}
    <div class="privacy-note">👪 Guardians respond for juniors — workflow in <a href="${REPO}/26" target="_blank">#26</a>.</div>${toastHtml()}`;
}

/* ---------- Activity ---------- */
const AUDIT_LABEL = {
  roster_selected: "Roster selected", availability_set: "Availability updated",
  player_backed_out: "Player backed out", substitute_enrolled: "Substitute enrolled",
  substitute_withdrawn: "Substitute withdrawn", substitute_offered: "Substitute offered",
  substitute_accepted: "Substitute accepted", substitute_declined: "Substitute declined",
  substitute_added_to_roster: "Substitute added to roster", player_removed: "Player removed",
  roster_locked: "Roster locked", roster_unlocked: "Roster unlocked", game_cancelled: "Game cancelled",
};
const SETUP_LABEL = {
  league_created: "League created", season_created: "Season created",
  division_created: "Division created", club_created: "Club created",
  team_created: "Team created", venue_created: "Venue created",
  rink_created: "Rink created", ice_slot_created: "Ice slot created",
  game_created: "Game scheduled", player_added: "Player added",
};
function tlRow(time, msg, dotColor) {
  return `<div class="tl-item"><div class="tl-dot" style="background:${dotColor || "var(--blue)"}"></div>
    <div class="tl-body"><div class="tl-msg">${msg}</div></div>
    <div class="tl-time">${esc(time || "")}</div></div>`;
}
function renderActivity(board, ov) {
  const setup = [...((ov && ov.setup_audit) || [])].reverse().slice(0, 40);
  const setupHtml = setup.length
    ? setup.map((a) => tlRow(fmt(a.at), `<strong>${esc(SETUP_LABEL[a.action] || a.action)}</strong> · ${esc(a.entity_id)}`, "#06b6d4")).join("")
    : `<div class="empty">No setup activity.</div>`;
  const operatorSection = `<div class="section-title">Operator setup (${(ov && ov.setup_audit_count) || 0})</div><div class="card">${setupHtml}</div>`;
  if (!board) return operatorSection + `<div class="section-title">Game</div><div class="card"><div class="empty">Open a game roster to see its game activity.</div></div>`;
  const names = {}; board.players.forEach((p) => (names[p.id] = p.name));
  const feed = [...(board.notifications || [])].reverse();
  const audit = [...(board.audit || [])].reverse();
  const dotFor = { coach: "var(--blue)", player: "var(--green)", team: "var(--purple)", guardian: "#b07bd6" };
  const fHtml = feed.length ? feed.map((n) => tlRow(fmt(n.at),
    `${esc(n.message)} <span style="color:var(--muted)">· to ${esc(n.audience)}${n.subject_player_id && names[n.subject_player_id] ? " · " + esc(names[n.subject_player_id]) : ""}</span>`,
    dotFor[n.audience])).join("") : `<div class="empty">No notifications yet.</div>`;
  const aHtml = audit.length ? audit.map((a) => tlRow(fmt(a.at),
    `<strong>${esc(AUDIT_LABEL[a.action] || a.action)}</strong>${a.subject_player_id && names[a.subject_player_id] ? " · " + esc(names[a.subject_player_id]) : ""}`,
    "#94a3b8")).join("") : `<div class="empty">No audit entries.</div>`;
  return `${operatorSection}
    <div class="section-title">Game notifications</div><div class="card">${fHtml}</div>
    <div class="section-title">Game audit (${board.audit_count})</div><div class="card">${aHtml}</div>
    <div class="section-title">Delivery</div><div class="card">${stub("📨", "Push / email delivery", "Worker + device tokens", 32)}</div>`;
}

/* ---------- Public Preview ---------- */
function renderPublic(ov) {
  const lg = ov.league || {};
  const rows = ov.public_fixtures.map((f) => `<div class="li"><span class="li-time">${fmt(f.start_time)}</span>
    <div class="li-main"><div class="li-title">${esc(f.home_team_name)} vs ${esc(f.away_team_name)}</div>
      <div class="li-sub">${esc(f.division_name || "")} · ${esc(f.venue_name || "")} · ${esc(f.rink_name || "")}</div></div>
    <span class="pill scheduled">${esc(f.status)}</span></div>`).join("");
  return `<div class="hero"><div class="when">Public fixtures</div><h2>${esc(lg.name)}</h2>
      <div class="where">${esc((ov.seasons[0] || {}).name || "")}</div></div>
    <div class="section-title">Fixtures</div>
    <div class="card">${rows || '<div class="empty">No fixtures.</div>'}</div>
    <div class="actions"><button class="act ghost" disabled>Add to calendar — Coming #33</button></div>
    <div class="privacy-note">🔒 Public view shows fixtures only. Junior player names and all
      personal/guardian/medical data are never exposed (policy: #35).</div>
    <div class="card">${stub("📊", "Standings & results", "Public table + game centre", 34)}</div>`;
}

/* ---------- actions ---------- */
function captureDrawerValues(ent) {
  // Snapshot the live inputs so an error re-render keeps the user's work.
  ent.fields.forEach((f) => {
    const el = document.getElementById(f.id);
    if (el) drawerValues[f.id] = el.value;
  });
}

async function submitSetup(kind) {
  const ent = SETUP_ENTITIES.find((e) => e.key === kind);
  toast = "";
  captureDrawerValues(ent);
  // Validate required fields client-side; the backend stays authoritative.
  const missing = ent.fields.filter((f) => f.required && !val(f.id));
  if (missing.length) {
    drawerError = `Please fill in: ${missing.map((f) => f.label).join(", ")}.`;
    return render();
  }
  drawerError = "";
  const res = await SETUP_POST[kind]();
  if (res && res.error) {
    // Keep the drawer open, preserve input, surface the server's message.
    drawerError = res.error.message;
    toast = "";
    return render();
  }
  drawer = null; drawerError = ""; drawerValues = {};
  toast = `${ent.noun[0].toUpperCase() + ent.noun.slice(1)} created.`;
  await render();
}

async function rosterAction(act, id) {
  toast = "";
  const B = `/api/games/${currentGame}`;
  if (act === "build") await post(`${B}/build-roster`, { team_id: rosterTeamId });
  else if (act === "select") await post(`${B}/roster/select`, { player_ids: [id] });
  else if (act === "remove") await post(`${B}/roster/remove`, { player_id: id });
  else if (act === "copy") { const r = await post(`${B}/roster/copy-previous`, { team_id: rosterTeamId }); if (r && !r.error) toast = `Copied ${r.copied} players from the previous game.`; }
  else if (act === "confirm") await post(`${B}/availability`, { player_id: id, availability_status: "available" });
  else if (act === "backout") await post(`${B}/availability`, { player_id: id, availability_status: "unavailable" });
  else if (act === "enroll") await post(`${B}/substitutes/enroll`, { player_id: id });
  else if (act === "withdraw") await post(`${B}/substitutes/withdraw`, { player_id: id });
  else if (act === "add") await post(`${B}/substitutes/${id}/add-to-roster`, {});
  else if (act === "accept") await post(`${B}/substitutes/${id}/accept`, {});
  else if (act === "decline") await post(`${B}/substitutes/${id}/decline`, {});
  else if (act === "lock") await post(`${B}/roster/lock`, {});
  else if (act === "unlock") await post(`${B}/roster/unlock`, {});
  await render();
}

/* ---------- render & wiring ---------- */
function setChrome(ov) {
  document.getElementById("nav-title").textContent = NAV[view];
  document.body.dataset.view = view;  // drives per-view max-width in web.css
  const bc = document.getElementById("breadcrumb");
  if (bc) {
    const league = (ov && ov.league && ov.league.name) || "No league yet";
    const season = (ov && ov.seasons && ov.seasons[0] && ov.seasons[0].name) || "No season yet";
    bc.textContent = `${league} · ${season}`;
  }
}

async function render() {
  const c = document.getElementById("content");
  document.body.dataset.view = view;
  let ov, board, lineups;
  try {
    c.innerHTML = `<div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div>`;
    ov = await getJSON("/api/demo/overview");
    if (ov && ov.error) throw new Error(ov.error.message);
    if (!currentGame && ov.schedule[0]) currentGame = ov.schedule[0].game_id;
    const needsBoard = ["activity", "dashboard"].includes(view);
    board = (needsBoard && currentGame) ? await getJSON(`/api/games/${currentGame}/board`) : null;
    // The roster tab and the game sheet both use both sides' lineups (#25/#48).
    lineups = (["roster", "sheet"].includes(view) && currentGame)
      ? await getJSON(`/api/games/${currentGame}/lineups`) : null;
  } catch (e) {
    setChrome(ov);
    c.innerHTML = `<div class="banner alert"><h2>Could not load data</h2>
      <p>The backend may not be running. ${esc(e.message || e)}</p></div>
      <div class="actions"><button class="act primary" onclick="render()">Retry</button></div>`;
    return;
  }

  setChrome(ov);
  c.innerHTML =
    view === "dashboard" ? renderDashboard(ov, board)
    : view === "setup" ? renderSetup(ov)
    : view === "calendar" ? renderCalendar(ov)
    : view === "games" ? renderGames(ov)
    : view === "roster" ? renderRoster(lineups)
    : view === "sheet" ? renderGameSheet(lineups)
    : view === "activity" ? renderActivity(board, ov)
    : renderPublic(ov);

  c.querySelectorAll("button[data-goto]").forEach((b) => b.onclick = () => switchTab(b.dataset.goto));
  // Setup drawers (#44): open, close, submit.
  c.querySelectorAll("[data-drawer]").forEach((b) => b.onclick = () => {
    drawer = { kind: b.dataset.drawer }; drawerError = ""; drawerValues = {}; toast = ""; render();
  });
  c.querySelectorAll("[data-drawer-close]").forEach((b) => b.onclick = () => {
    drawer = null; drawerError = ""; drawerValues = {}; render();
  });
  c.querySelectorAll("[data-drawer-submit]").forEach((b) => b.onclick = () => submitSetup(b.dataset.drawerSubmit));
  if (drawer) {
    const first = c.querySelector(".drawer-body input, .drawer-body select");
    if (first) first.focus();
  }
  c.querySelectorAll("button[data-act]").forEach((b) => b.onclick = () => rosterAction(b.dataset.act, b.dataset.id));
  c.querySelectorAll(".seg").forEach((b) => b.onclick = () => { gameView = b.dataset.view; toast = ""; render(); });
  c.querySelectorAll("[data-side]").forEach((b) => b.onclick = () => { rosterSide = b.dataset.side; toast = ""; render(); });
  const printBtn = c.querySelector("[data-print]");
  if (printBtn) printBtn.onclick = () => window.print();
  // Shared move: used by both drag/drop and the click-based Move fallback.
  const applyMove = async (gid, slotId) => {
    toast = "";
    const res = await post(`/api/games/${gid}/move`, { ice_slot_id: slotId, reason: "Moved on arena calendar" });
    conflict = buildConflict(res, ov, gid, slotId);   // #43 side panel explains outcome
    if (res && res.error) toast = "";
    movingGameId = null;
    await render();
  };
  // Tapping an Available slot: complete a pending move, else open the wizard.
  c.querySelectorAll("[data-slot]").forEach((b) => b.onclick = () => {
    if (movingGameId != null) return applyMove(movingGameId, b.dataset.slot);
    wizard = { slot_id: b.dataset.slot }; toast = ""; render();
  });
  // Enter move mode (drag-free path for touch/mobile/keyboard).
  c.querySelectorAll("[data-move-game]").forEach((b) => b.onclick = (e) => {
    e.stopPropagation();
    movingGameId = b.dataset.moveGame; conflict = null; toast = ""; render();
  });
  c.querySelectorAll("[data-move-cancel]").forEach((b) => b.onclick = () => { movingGameId = null; render(); });
  c.querySelectorAll("[data-addslot]").forEach((b) => b.onclick = async () => { await post("/api/demo/add-ice-slot", { rink_id: b.dataset.addslot, date: calendarDate }); await render(); });
  c.querySelectorAll("[data-cal]").forEach((b) => b.onclick = () => {
    const v = +b.dataset.cal;
    if (v === 0) calendarDate = "2026-09-05";
    else shiftDate(v * (calendarMode === "week" ? 7 : 1));
    toast = ""; conflict = null; movingGameId = null; render();
  });
  c.querySelectorAll("[data-mode]").forEach((b) => b.onclick = () => { calendarMode = b.dataset.mode; toast = ""; conflict = null; movingGameId = null; render(); });
  c.querySelectorAll("[data-filter]").forEach((sel) => sel.onchange = (e) => {
    const key = sel.dataset.filter;
    calFilters[key] = e.target.value;
    if (key === "venueId") calFilters.rinkId = "all";  // rink list depends on venue
    toast = ""; conflict = null; movingGameId = null; render();
  });
  // Drag a game (allocated card or draft chip) onto an available slot to move it.
  c.querySelectorAll("[data-game]").forEach((el) => {
    el.addEventListener("dragstart", (e) => {
      e.dataTransfer.setData("text/plain", el.dataset.game);
      e.dataTransfer.effectAllowed = "move";
      el.classList.add("dragging");
    });
    el.addEventListener("dragend", () => el.classList.remove("dragging"));
  });
  c.querySelectorAll("[data-drop]").forEach((el) => {
    el.addEventListener("dragover", (e) => { e.preventDefault(); el.classList.add("drop-hover"); });
    el.addEventListener("dragleave", () => el.classList.remove("drop-hover"));
    el.addEventListener("drop", async (e) => {
      e.preventDefault();
      el.classList.remove("drop-hover");
      const gid = e.dataTransfer.getData("text/plain");
      if (!gid) return;
      // Same server-validated move + #43 conflict panel as the click fallback.
      await applyMove(gid, el.dataset.drop);
    });
  });
  const dismiss = c.querySelector("[data-conflict-dismiss]");
  if (dismiss) dismiss.onclick = () => { conflict = null; render(); };
  c.querySelectorAll("[data-publish]").forEach((b) => b.onclick = async () => { await post(`/api/games/${b.dataset.publish}/publish`, {}); toast = "Game published."; await render(); });
  c.querySelectorAll("[data-openroster]").forEach((b) => b.onclick = () => { currentGame = b.dataset.openroster; switchTab("roster"); });
  const picker = document.getElementById("player-picker");
  if (picker) picker.onchange = (e) => { pickedPlayer = e.target.value; toast = ""; render(); };
  // wizard wiring
  const wd = document.getElementById("w-div");
  if (wd) wd.onchange = (e) => { wizard.division_id = e.target.value; wizard.home_id = null; wizard.away_id = null; render(); };
  const wh = document.getElementById("w-home");
  if (wh) wh.onchange = (e) => { wizard.home_id = e.target.value; wizard.away_id = null; render(); };
  const wa = document.getElementById("w-away");
  if (wa) wa.onchange = (e) => { wizard.away_id = e.target.value; render(); };
  const wc = c.querySelector("[data-wizcancel]"); if (wc) wc.onclick = () => { wizard = null; render(); };
  const wcr = c.querySelector("[data-wizcreate]");
  if (wcr) wcr.onclick = async () => {
    // Use the SELECTED division's season, not the first seeded season.
    const div = ov.divisions.find((d) => d.id === wizard.division_id);
    const res = await post("/api/setup/game", {
      season_id: div ? div.season_id : (ov.seasons[0] || {}).id,
      division_id: wizard.division_id,
      home_team_id: wizard.home_id, away_team_id: wizard.away_id, ice_slot_id: wizard.slot_id,
    });
    if (res && !res.error) { toast = "Game scheduled."; currentGame = res.id; wizard = null; view = "games"; }
    render();
  };
}

function switchTab(next) {
  view = next; toast = ""; if (next !== "calendar") { wizard = null; conflict = null; movingGameId = null; }
  if (next !== "setup") { drawer = null; drawerError = ""; drawerValues = {}; }
  document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x.dataset.tab === next));
  render();
}
document.querySelectorAll(".tab").forEach((b) => b.onclick = () => switchTab(b.dataset.tab));
// Topbar command actions (web shell) — outside #content, wired once.
document.querySelectorAll(".topbar [data-goto]").forEach((b) => b.onclick = () => switchTab(b.dataset.goto));
// Topbar shortcut: jump to Setup and open a create drawer directly (#44).
document.querySelectorAll(".topbar [data-open-drawer]").forEach((b) => b.onclick = () => {
  drawer = { kind: b.dataset.openDrawer }; drawerError = ""; drawerValues = {};
  switchTab("setup");
});
document.getElementById("reset-btn").onclick = async () => {
  await post("/api/reset", {});
  toast = ""; currentGame = null; pickedPlayer = null; wizard = null;
  movingGameId = null; conflict = null; drawer = null; drawerError = ""; drawerValues = {};
  render();
};
// Escape closes an open Setup drawer (#44).
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (drawer) { drawer = null; drawerError = ""; drawerValues = {}; render(); }
  else if (movingGameId != null) { movingGameId = null; render(); }
});

// -- sign-in / session (demo auth, #50) ----------------------------------
function applyRolePerms() {
  const r = roleCatalog.find((x) => x.id === currentRole);
  rolePerms = new Set(r ? r.permissions : []);
  gateChrome();
}
function gateChrome() {
  const toggle = (sel, ok) => document.querySelectorAll(sel).forEach((el) => {
    el.style.display = ok ? "" : "none";
  });
  toggle('.topbar [data-goto="calendar"]', hasPerm("manage_schedule"));
  toggle('.topbar [data-open-drawer="ice-slot"]', hasPerm("manage_arena"));
}
function setUser(user) {
  currentUser = user;
  currentRole = user ? user.role : "viewer";
  applyRolePerms();
}
async function signIn(username) {
  const r = await post("/api/auth/login", { username, password: "demo" });
  if (r && !r.error) { setUser(r.user); toast = ""; }
  drawer = null; movingGameId = null; conflict = null;
  renderRoleSwitch(); render();
}
function renderRoleSwitch() {
  const sel = document.getElementById("role-switch");
  if (!sel || !accounts.length) return;
  sel.innerHTML = accounts.map((a) =>
    `<option value="${esc(a.username)}" ${a.role === currentRole ? "selected" : ""}>${esc(a.label)}</option>`).join("");
  // Switching the demo account performs a real server-side sign-in (#50).
  sel.onchange = (e) => signIn(e.target.value);
}
async function bootstrap() {
  try {
    const [rolesRes, acctRes, meResp] = await Promise.all([
      fetch("/api/auth/roles").then((r) => r.json()),
      fetch("/api/auth/accounts").then((r) => r.json()),
      fetch("/api/auth/me", { credentials: "same-origin" }),
    ]);
    roleCatalog = rolesRes.roles || [];
    accounts = acctRes.accounts || [];
    const meRes = await meResp.json();
    if (meResp.status === 401) {
      // A stale/invalid session must NOT silently become a new admin session.
      setUser(null);
      toast = "Session expired — pick an account to sign in.";
    } else if (meRes.user) {
      setUser(meRes.user);
    } else {
      // No session at all: start the demo signed in as League Admin.
      await post("/api/auth/login", { username: "admin", password: "demo" })
        .then((r) => { if (r && !r.error) setUser(r.user); });
    }
  } catch (_) { roleCatalog = []; accounts = []; }
  applyRolePerms();
  renderRoleSwitch();
  render();
}
bootstrap();
