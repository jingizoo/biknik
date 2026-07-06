/* Hockey Scheduler — calendar-first operator demo.
   Drives the real backend (setup + roster/substitute) via the documented API. */

let view = "dashboard";     // dashboard|setup|import|calendar|games|roster|activity|public
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
let officialsPool = [];            // [{id,name}] officials for the assign UI (#30)
let standingsDivision = null;      // selected division for the Standings tab (#31)
let notifState = { notifications: [], unread: 0 };  // feed for the bell (#32)
let deliveryState = { contacts: [], overview: null, deviceTokens: [] };  // ops delivery admin (#61/#65)
let usersState = { accounts: [], sessions: [] };  // account/session admin (#78)
let usersSelected = null;          // account id whose sessions are shown (#78)
let notifPrefs = null;             // signed-in user's own channel prefs (#81)
let feedTokens = [];               // signed-in user's calendar feed tokens (#82)
let newFeedUrl = null;             // freshly-minted feed URL, shown once (#82)
let publicState = { schedule: null, standings: null, division: null, game: null };
let publicTab = "schedule";        // "schedule" | "standings" (#83)
let schedulerState = { division: null, preview: null, drafts: [] };  // (#86)
let officialAvailability = [];      // signed-in official's windows (#88)
let availSummary = null;            // roster availability rollup (#89)
let availFilter = "all";            // all|available|unavailable|maybe|no_response
let contactForm = { recipient_ref: "", channel: "email", destination: "", label: "" };
let tokenForm = { recipient_ref: "", provider: "fcm", token: "", label: "" };
let movingGameId = null;    // click-to-move fallback: game awaiting a destination slot
let conflict = null;        // {ok, title, lines[], game, slot} — calendar side panel (#43)
let drawer = null;          // {kind} when a Setup create drawer is open (#44)
let drawerError = "";       // validation/API error shown inside the open drawer
let drawerValues = {};      // {fieldId: value} preserved across re-render on error
let activityExpandedBatches = new Set();  // import_batch_ids expanded in Activity (#102)
let importState = {         // Pilot onboarding import wizard (#96)
  type: "teams_players",    // which IMPORT_TYPES entry is selected
  seasonId: null,            // only used by the teams_players type
  sheetsText: {},            // {csv field name: pasted text}, reset on type switch
  report: null,              // last /api/import/dry-run result (or {error})
  validatedKey: null,        // snapshot of sheetsText at the last successful validate;
                             // Commit refuses to run if the current text has drifted
  committed: null,           // last commit result (or {error})
};

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
  inbox: "My Assignments", standings: "Standings",
  notifications: "Notifications", delivery: "Delivery", activity: "Activity",
  public: "Public", users: "Users", scheduler: "Scheduler", import: "Import",
};
const POS_CLASS = { goalie: "pos-G", defense: "pos-D", forward: "pos-F", skater: "pos-D" };
const REPO = "https://github.com/jingizoo/biknik/issues";
const DEMO_PASSWORD = "demo";  // shared password for the demo personas (#67)
let envStatus = null;          // deployment posture for the topbar chips (#72)

const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (iso) => { const m = /T(\d{2}:\d{2})/.exec(iso || ""); return m ? m[1] : ""; };
const val = (id) => { const e = document.getElementById(id); return e ? e.value.trim() : ""; };
const hasPerm = (p) => rolePerms.has(p);

// Client-side file download (#99) — no backend route needed, since the CSV
// template/sample content already lives in IMPORT_TYPES. A throwaway <a>
// with a Blob URL is the standard way to trigger a save-as without a server
// round trip.
function downloadTextFile(filename, text) {
  const blob = new Blob([text], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  try {
    a.click();
  } finally {
    document.body.removeChild(a);
    // Revoking the object URL synchronously right after click() is
    // unreliable on Safari/iOS (this app is iPhone-first, per CLAUDE.md) —
    // the browser may still be reading the blob when the URL is
    // invalidated, producing an empty/truncated download. Defer the revoke
    // to a later macrotask so the download has already started; wrapped in
    // try/finally so cleanup still runs even if click() itself throws.
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
}

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

/* ---------- Dashboard (operator triage) ---------- */
const fmtClock = (iso) => {
  const m = /T(\d{2}):(\d{2})/.exec(iso || "");
  if (!m) return "";
  let h = +m[1]; const min = m[2]; const ap = h >= 12 ? "p" : "a";
  h = h % 12 || 12;
  return `${h}:${min}${ap}`;
};
const dayOf = (iso) => (iso || "").slice(0, 10);

// Per-game triage: derive a status badge + a staffing note from the schedule
// row's real fields (officials assigned/accepted, roster status, result).
function gameTriage(g) {
  if (g.result_status === "final")
    return { badge: "final", label: "Final", note: "Result final", noteCls: "ok" };
  const assigned = g.officials_assigned || 0;
  const accepted = g.officials_accepted || 0;
  const rosterOk = ["roster_confirmed", "locked"].includes(g.roster_status);
  if (assigned === 0)
    return { badge: "needs", label: "Needs staff", note: "No officials assigned", noteCls: "bad" };
  if (accepted < assigned)
    return { badge: "needs", label: "Needs staff", note: "Officials pending acceptance", noteCls: "warn" };
  if (!rosterOk)
    return { badge: "needs", label: "Roster open", note: "Roster not confirmed", noteCls: "warn" };
  return { badge: "ready", label: "Ready", note: "Officials & roster set", noteCls: "ok" };
}

function renderDashboard(ov, standings) {
  const games = ov.schedule || [];
  const today = games.filter((g) => dayOf(g.start_time) === calendarDate);
  const todayList = today.length ? today : games;   // fall back to all if none "today"
  const upcoming = games.length - today.length;

  // Ice utilisation.
  const slots = ov.ice_slots || [];
  const booked = slots.filter((s) => s.status !== "available").length;
  const utilPct = slots.length ? Math.round((booked / slots.length) * 100) : 0;

  // Staffing gaps across all scheduled games.
  const needStaff = games.filter((g) => g.result_status !== "final" && gameTriage(g).badge === "needs");
  const toConfirm = games.filter((g) =>
    g.result_status !== "final" && !["roster_confirmed", "locked"].includes(g.roster_status));

  const pill = (cls, txt) => `<span class="ds-pill ${cls}">${esc(txt)}</span>`;
  const stat = (label, n, sub, pillHtml) => `
    <div class="dash-stat">${pillHtml || ""}
      <div class="ds-label">${esc(label)}</div>
      <div class="ds-n">${n}</div>
      <div class="ds-sub">${esc(sub)}</div></div>`;

  const stats = `<div class="dash-stats">
    ${stat("Games this week", games.length, `${today.length} today · ${upcoming} upcoming`,
           today.length ? pill("green", `+${today.length}`) : "")}
    ${stat("Ice slots booked", booked, `${utilPct}% of ${slots.length} slots`,
           pill("gray", `${utilPct}%`))}
    ${stat("Games needing staff", needStaff.length,
           needStaff.length ? `officials still open` : "all games staffed",
           needStaff.length ? pill("amber", "Fill") : pill("green", "Set"))}
    ${stat("Rosters to confirm", toConfirm.length,
           toConfirm.length ? `of ${games.length} games` : "all rosters set",
           toConfirm.length ? pill("blue", "Review") : pill("green", "Ready"))}
  </div>`;

  // Today's games list.
  const gameRows = todayList.map((g) => {
    const t = gameTriage(g);
    return `<div class="tg-row" data-open-sheet="${esc(g.game_id)}">
      <div class="tg-time"><div class="tgt-h">${fmtClock(g.start_time)}</div>
        <div class="tgt-r">${esc(g.rink_name || "")}</div></div>
      <div class="tg-match">
        <div class="tg-teams">${esc(g.home_team_name)}<span class="tg-vs">vs</span>${esc(g.away_team_name)}</div>
        <div class="tg-meta"><span class="tg-div">${esc(g.division_name || "")}</span>
          <span class="tg-note ${t.noteCls}">${esc(t.note)}</span></div>
      </div>
      <span class="tg-status ${t.badge}">${esc(t.label)}</span>
    </div>`;
  }).join("");
  const gamesCard = `<div class="dash-card">
    <div class="dash-card-head"><h3>${today.length ? "Today's Games" : "Scheduled Games"}</h3>
      <span class="dch-sub">${todayList.length} game${todayList.length === 1 ? "" : "s"}</span>
      <a class="dch-link" data-goto="games">Games →</a></div>
    ${gameRows || '<div class="na-empty">No games scheduled yet.</div>'}</div>`;

  // Needs Attention — real alerts only.
  const alerts = [];
  const noOfficials = games.filter((g) => g.result_status !== "final" && (g.officials_assigned || 0) === 0);
  if (noOfficials.length) alerts.push({ ico: "red", glyph: "⚠", title:
    `${noOfficials.length} game${noOfficials.length === 1 ? "" : "s"} missing officials`,
    sub: "No referee or timekeeper assigned yet" });
  if (toConfirm.length) alerts.push({ ico: "amber", glyph: "👥", title:
    `${toConfirm.length} roster${toConfirm.length === 1 ? "" : "s"} not confirmed`,
    sub: "Line-ups still open before game day" });
  if ((notifState.unread || 0) > 0) alerts.push({ ico: "blue", glyph: "🔔", title:
    `${notifState.unread} unread notification${notifState.unread === 1 ? "" : "s"}`,
    sub: "New assignment, roster, or result activity" });
  const alertRows = alerts.map((a) => `<div class="na-row">
    <div class="na-ico ${a.ico}">${a.glyph}</div>
    <div class="na-body"><div class="na-title">${esc(a.title)}</div>
      <div class="na-sub">${esc(a.sub)}</div></div></div>`).join("");
  const attentionCard = `<div class="dash-card">
    <div class="dash-card-head"><span class="dch-dot"></span><h3>Needs Attention</h3></div>
    ${alertRows || '<div class="na-empty">✓ You\'re all caught up.</div>'}</div>`;

  // Standings snapshot (top 4).
  const div0 = ov.divisions[0];
  const rows = ((standings && standings.standings) || []).slice(0, 4);
  const ssRows = rows.map((r, i) => `<div class="ss-row">
    <span class="ss-rank">${i + 1}</span>
    <span class="ss-team">${esc(r.team_name)}</span>
    <span class="ss-rec">${r.w}-${r.l}${r.t ? "-" + r.t : ""}</span>
    <span class="ss-pts">${r.pts}</span></div>`).join("");
  const standingsCard = div0 ? `<div class="dash-card">
    <div class="dash-card-head"><h3>${esc(div0.name)} · Standings</h3>
      <a class="dch-link" data-goto="standings">All →</a></div>
    ${ssRows || '<div class="na-empty">No games played yet.</div>'}</div>` : "";

  return `${stats}
    <div class="dash-grid">
      <div>${gamesCard}</div>
      <div style="display:flex;flex-direction:column;gap:16px">${attentionCard}${standingsCard}</div>
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
  { key: "official", title: "Officials", icon: "🧑‍⚖️", noun: "official", perm: "manage_schedule",
    list: (ov) => (ov.officials || []).map((o) => ({ title: o.name, sub: o.home_club_name || "" })),
    fields: [
      { id: "f-official", label: "Official name", required: true, placeholder: "e.g. Riley Whistle" },
      { id: "f-official-club", label: "Home club (optional — for conflict checks)", type: "select",
        options: (ov) => [["", "— none —"]].concat(ov.clubs.map((c) => [c.id, c.name])) }] },
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
  official: () => post("/api/setup/official", {
    name: val("f-official"), home_club_id: val("f-official-club") || null,
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
        ${ck((g.officials_assigned || 0) > 0 && (g.officials_accepted || 0) === g.officials_assigned, "Officials",
             g.officials_assigned ? `${g.officials_accepted}/${g.officials_assigned} accepted` : "None assigned")}
        ${ck(false, "Locker rooms", "Follow-up")}
        ${ck(g.result_status === "final", "Result",
             g.result_status === "final" ? "Final" : g.result_status === "draft" ? "Draft — approve to finalize" : "Not entered")}
        ${ck(g.published, "Public fixture", g.published ? "Published" : "Draft — not public")}
      </div>
      <div class="actions">
        <button class="act primary" data-openroster="${g.game_id}">Open Roster</button>
        ${g.published ? "" : `<button class="act success" data-publish="${g.game_id}">Publish</button>`}
      </div>`;
  }).join("") + toastHtml();
}

/* ---------- Roster (Coach/Player) ---------- */
const AVAIL_PILL = { available: "available", unavailable: "blocked",
  maybe: "junior", no_response: "gray" };
const AVAIL_LABEL = { all: "All", available: "Available", unavailable: "Unavailable",
  maybe: "Maybe", no_response: "No response" };

function renderAvailSummary() {
  if (!availSummary) return "";
  const c = availSummary.counts;
  const chip = (key) => {
    const n = key === "all"
      ? availSummary.players.length
      : (c[key] || 0);
    return `<button class="seg ${availFilter === key ? "active" : ""}" data-avail-filter="${key}">${AVAIL_LABEL[key]} ${n}</button>`;
  };
  const shown = availSummary.players.filter(
    (p) => availFilter === "all" || p.status === availFilter);
  const rows = shown.map((p) => `<div class="session-row">
    <span class="row-main">${esc(p.name)}</span>
    <span class="pill ${AVAIL_PILL[p.status] || "gray"}">${esc(p.status.replace("_", " "))}</span>
  </div>`).join("");
  const canRemind = hasPerm("manage_roster");
  return `<div class="card">
    <div class="section-title" style="margin-top:0">Availability
      ${canRemind && c.no_response ? `<button class="act ghost" data-avail-remind>Remind ${c.no_response} unresponded</button>` : ""}</div>
    <div class="seg-group">${["all", "available", "unavailable", "maybe", "no_response"].map(chip).join("")}</div>
    <div class="row-list">${rows || '<p class="muted">No players in this filter.</p>'}</div>
  </div>`;
}

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
    </div><div style="padding-top:8px">${gameView === "coach" ? coachBody(side) : playerBody(side)}</div>
    ${renderAvailSummary()}`;
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
  return `<div class="game-sheet">
    <div class="gs-toolbar no-print">
      <span class="gs-hint">Read-only official game sheet — combines both lineups.</span>
      <button class="act ghost" data-print>🖨 Print / Export</button></div>
    <header class="gs-header">
      <div class="gs-match">${esc(lineups.home.team_name)} <span class="gs-vs">vs</span> ${esc(lineups.away.team_name)}</div>
      <div class="gs-meta">${esc(fmtDateTime(g.start_time))}${g.rink ? " · " + esc(g.rink) : ""}</div>
      <div class="gs-badges">${pub} ${lock} ${cancelled}</div>
    </header>
    ${resultSection(lineups)}
    <div class="gs-grid">
      ${sheetSide(lineups.home, "Home")}
      ${sheetSide(lineups.away, "Away")}
    </div>
    ${officialsPanel(lineups)}
    <div class="privacy-note">📋 Roster names are visible to authorized operators only. Penalties
      and signatures are follow-ups. Standings update from Final results (<a href="${REPO}/31" target="_blank">#31</a>).</div>
  </div>`;
}
function resultSection(lineups) {
  const canEdit = hasPerm("manage_schedule");
  const r = lineups.result;
  const final = r && r.status === "final";
  const score = r ? `${r.home_score} – ${r.away_score}` : "– : –";
  const badge = !r ? `<span class="badge gray">No result</span>`
    : final ? `<span class="badge green">Final</span>`
    : `<span class="badge orange">Draft</span>`;
  const form = (canEdit && !final) ? `<div class="gs-result-form no-print">
      <input id="res-home" type="number" min="0" value="${r ? r.home_score : ""}" placeholder="H" />
      <span class="gs-dash">–</span>
      <input id="res-away" type="number" min="0" value="${r ? r.away_score : ""}" placeholder="A" />
      <button class="act primary" data-record-result>Save score</button>
      ${r ? `<button class="act success" data-approve-result>Approve → Final</button>` : ""}
    </div>` : "";
  return `<section class="gs-result">
    <div class="gs-result-head">🏒 Result ${badge}</div>
    <div class="gs-score"><span>${esc(lineups.home.team_name)}</span>
      <strong>${score}</strong><span>${esc(lineups.away.team_name)}</span></div>
    ${form}
    ${!final ? `<div class="gs-note no-print">Only an approved <strong>Final</strong> result affects standings.</div>` : ""}
  </section>`;
}
/* ---------- Official "My Assignments" inbox (#55) ---------- */
function renderAvailability() {
  // The signed-in official's declared availability windows (#88).
  const rows = officialAvailability.map((a) => `<div class="session-row">
    <span class="pill ${a.status === "unavailable" ? "blocked" : "available"}">${esc(a.status)}</span>
    <span class="row-main">${esc(fmtDateTime(a.start_time))} → ${esc(fmtDateTime(a.end_time))}</span>
    ${a.note ? `<span class="row-sub">${esc(a.note)}</span>` : ""}
    <button class="act ghost danger" data-avail-del="${esc(a.id)}">Remove</button>
  </div>`).join("");
  return `<div class="card">
    <div class="section-title" style="margin-top:0">Availability</div>
    <p class="muted">Mark windows you can't officiate; schedulers are warned before assigning you.</p>
    <div class="row-list">${rows || '<p class="muted">No availability windows set.</p>'}</div>
    <div class="avail-form">
      <input type="datetime-local" id="avail-start">
      <input type="datetime-local" id="avail-end">
      <select id="avail-status"><option value="unavailable">Unavailable</option><option value="available">Available</option></select>
      <input type="text" id="avail-note" placeholder="Note (optional)">
      <button class="act primary" data-avail-add>Add window</button>
    </div></div>`;
}

function renderInbox(inbox) {
  if (!inbox || !inbox.official_id) {
    return `<div class="empty">Sign in as an <strong>Official</strong> to see your assignments.</div>`;
  }
  const rows = inbox.assignments || [];
  if (!rows.length) {
    return `<div class="banner neutral"><h2>No assignments yet</h2>
      <p>When a scheduler assigns you to a game, it will appear here to accept or decline.</p></div>
      ${renderAvailability()}`;
  }
  const roleLabel = { referee: "👨‍⚖️ Referee", linesperson: "🚩 Linesperson", scorekeeper: "📝 Scorekeeper" };
  const badge = (st) => st === "accepted" ? `<span class="badge green">Accepted</span>`
    : st === "declined" ? `<span class="badge red">Declined</span>`
    : `<span class="badge orange">Proposed</span>`;
  const cards = rows.map((a) => {
    const matchup = `${esc(a.home_team_name)} vs ${esc(a.away_team_name || "TBD")}`;
    const where = `${esc(fmtDateTime(a.start_time))}${a.rink ? " · " + esc(a.rink) : ""}${a.venue_name ? " · " + esc(a.venue_name) : ""}`;
    const actions = a.status === "proposed" && !a.cancelled
      ? `<button class="act success" data-accept="${a.assignment_id}">Accept</button>
         <button class="act danger" data-decline="${a.assignment_id}">Decline</button>` : "";
    const cancelled = a.cancelled ? `<span class="badge red">Game cancelled</span>` : "";
    return `<div class="inbox-card">
      <div class="inbox-top"><span class="inbox-role">${roleLabel[a.role] || a.role}</span>${badge(a.status)}${cancelled}</div>
      <div class="inbox-match">${matchup}</div>
      <div class="inbox-where">${where}</div>
      <div class="inbox-actions">${actions}
        <button class="act ghost" data-open-sheet="${a.game_id}">Open game sheet</button></div>
    </div>`;
  }).join("");
  return `<div class="section-title">Your upcoming assignments (${rows.length})</div>
    <div class="inbox-list">${cards}</div>${renderAvailability()}${toastHtml()}`;
}

/* ---------- Notifications feed (#32) ---------- */
const NOTIF_ICON = {
  assignment_offered: "👨‍⚖️", assignment_accepted: "✅", assignment_declined: "❌",
  roster_open_slot: "⚠️", result_approved: "🏒",
};
function updateNotifBadge() {
  const badge = document.getElementById("notif-badge");
  if (!badge) return;
  const n = notifState.unread || 0;
  badge.textContent = n > 0 ? (n > 9 ? "9+" : String(n)) : "";
  badge.style.display = n > 0 ? "" : "none";
}
// The delivery recipient_ref the signed-in user speaks for (#81), used for the
// self-service notification-preference toggles. Mirrors the server's mapping.
function ownRecipientRef() {
  const u = currentUser;
  if (!u || !u.scope) return null;
  if (u.role === "official" && u.scope.official_id) return "official:" + u.scope.official_id;
  // Only a coach speaks for the shared team channel; a player has no own
  // delivery target in this slice, so they get no self-service prefs panel
  // (and cannot mute the whole team's notifications). Mirrors the server.
  if (u.role === "coach" && u.scope.team_id) return "team:" + u.scope.team_id;
  return null;
}

// The calendar-feed actor the signed-in user can subscribe to (#82).
function ownFeedActor() {
  const u = currentUser;
  if (!u || !u.scope) return null;
  if (u.role === "official" && u.scope.official_id)
    return { actor_type: "official", actor_ref: u.scope.official_id };
  if (u.role === "player" && u.scope.player_id)
    return { actor_type: "player", actor_ref: u.scope.player_id };
  if ((u.role === "coach" || u.role === "player") && u.scope.team_id)
    return { actor_type: "team", actor_ref: u.scope.team_id };
  return null;
}

function renderCalendarFeed() {
  if (!ownFeedActor()) return "";
  const minted = newFeedUrl
    ? `<div class="feed-url"><code>${esc(location.origin + newFeedUrl)}</code>
        <p class="muted">Copy this URL into your calendar app. It is shown once —
        it won't be displayed again.</p></div>`
    : "";
  const active = feedTokens.length
    ? `<div class="row-list">${feedTokens.map((t) => `
        <div class="session-row">
          <span class="row-main">Feed created ${fmtDateTime(t.created_at)}</span>
          <button class="act ghost" data-feed-revoke="${esc(t.id)}">Revoke</button>
        </div>`).join("")}</div>`
    : `<p class="muted">No active calendar feed.</p>`;
  return `<div class="card">
    <div class="section-title" style="margin-top:0">Calendar subscription</div>
    <p class="muted">Subscribe your calendar app to your games (fixtures only).</p>
    ${minted}${active}
    <div class="dq-actions"><button class="act primary" data-feed-create>Create feed URL</button></div>
  </div>`;
}

const PREF_CHAN_LABEL = { email: "Email", push: "Push" };

function renderNotifPrefs() {
  if (!notifPrefs) return "";
  const toggles = (notifPrefs.preferences || []).map((p) => `
    <label class="pref-toggle">
      <input type="checkbox" data-pref-channel="${esc(p.channel)}" ${p.enabled ? "checked" : ""}>
      <span>${CHAN_ICON[p.channel] || ""} ${esc(PREF_CHAN_LABEL[p.channel] || p.channel)}</span>
    </label>`).join("");
  return `<div class="card pref-card">
    <div class="section-title" style="margin-top:0">Delivery preferences</div>
    <p class="muted">In-app notifications always arrive here. Turn off a channel to stop those deliveries.</p>
    <div class="pref-toggles">${toggles}</div></div>`;
}

function renderNotifications() {
  const rows = notifState.notifications || [];
  const unread = notifState.unread || 0;
  const head = `<div class="notif-head"><div class="section-title" style="margin:0">Notifications${unread ? ` · ${unread} unread` : ""}</div>
    ${unread ? `<button class="act ghost" data-notif-readall>Mark all read</button>` : ""}</div>`;
  if (!rows.length) {
    return `${head}${renderNotifPrefs()}${renderCalendarFeed()}<div class="banner neutral"><h2>You're all caught up</h2>
      <p>Assignment offers, roster alerts, and final results will show up here.</p></div>`;
  }
  const cards = rows.map((n) => {
    const link = n.game_id
      ? `<button class="act ghost" data-notif-open="${n.game_id}">Open game</button>` : "";
    return `<div class="notif-card ${n.read ? "read" : "unread"}" data-notif-read="${n.id}">
      <span class="notif-ico">${NOTIF_ICON[n.kind] || "🔔"}</span>
      <div class="notif-body"><div class="notif-title">${esc(n.title)}${n.read ? "" : ` <span class="notif-dot"></span>`}</div>
        <div class="notif-msg">${esc(n.message)}</div>
        <div class="notif-meta">${esc(fmtDateTime(n.at))}</div></div>
      ${link}</div>`;
  }).join("");
  return `${head}${renderNotifPrefs()}${renderCalendarFeed()}<div class="notif-list">${cards}</div>${toastHtml()}`;
}

/* ---------- Delivery admin: contacts + queue monitor (#61) ---------- */
const CHAN_ICON = { email: "✉️", push: "📲" };
const DELIV_BADGE = { pending: "gray", sent: "green", failed: "red",
  dead_lettered: "red", ignored: "gray" };

function recipientOptions(ov) {
  // Quick-pick suggestions for the recipient_ref field; manual entry still wins.
  const opts = [
    { ref: "scheduler", label: "Scheduler group" },
    { ref: "public", label: "Public broadcast" },
  ];
  (officialsPool || []).forEach((o) =>
    opts.push({ ref: "official:" + o.id, label: "Official — " + o.name }));
  ((ov && ov.teams) || []).forEach((t) =>
    opts.push({ ref: "team:" + t.id, label: "Team — " + t.name }));
  return opts;
}

function renderContactsPanel(ov) {
  const f = contactForm;
  const picks = recipientOptions(ov);
  const pickOpts = [`<option value="">Quick pick…</option>`]
    .concat(picks.map((p) => opt(p.ref, p.label, false))).join("");
  const chan = (v) => opt(v, v === "email" ? "Email" : "Push", f.channel === v);
  const form = `
    <div class="card cd-form">
      <div class="cd-grid">
        <label class="cd-field"><span>Recipient</span>
          <select id="contact-pick" class="cd-input">${pickOpts}</select></label>
        <label class="cd-field"><span>recipient_ref</span>
          <input id="contact-ref" class="cd-input" placeholder="official:… / team:… / scheduler / public"
            value="${esc(f.recipient_ref)}" /></label>
        <label class="cd-field cd-narrow"><span>Channel</span>
          <select id="contact-channel" class="cd-input">${chan("email")}${chan("push")}</select></label>
        <label class="cd-field"><span>Destination</span>
          <input id="contact-dest" class="cd-input" placeholder="name@club.invalid or push-token"
            value="${esc(f.destination)}" /></label>
        <label class="cd-field"><span>Label <em>(optional)</em></span>
          <input id="contact-label" class="cd-input" placeholder="e.g. Ops Desk"
            value="${esc(f.label)}" /></label>
        <div class="cd-submit"><button class="act primary" data-contact-save>Save contact</button></div>
      </div>
    </div>`;
  const rows = (deliveryState.contacts || []).map((c) => `
    <tr>
      <td class="cd-ref">${esc(c.recipient_ref)}</td>
      <td>${CHAN_ICON[c.channel] || ""} ${esc(c.channel)}</td>
      <td class="cd-dest">${esc(c.destination)}</td>
      <td>${esc(c.label || "")}</td>
      <td><button class="act ghost cd-edit" data-contact-edit="${esc(c.id)}">Edit</button></td>
    </tr>`).join("");
  const table = rows
    ? `<div class="card"><table class="cd-table">
        <thead><tr><th>Recipient</th><th>Channel</th><th>Destination</th><th>Label</th><th></th></tr></thead>
        <tbody>${rows}</tbody></table></div>`
    : `<div class="banner neutral"><h2>No contacts registered</h2>
        <p>Deliveries fall back to a safe <code>.invalid</code> placeholder until you add one.</p></div>`;
  return `<div class="section-title">Contact destinations</div>${form}${table}`;
}

function renderDeliveryMonitor() {
  const ov = deliveryState.overview;
  if (!ov) return `<div class="section-title">Delivery queue</div>
    <div class="banner neutral"><h2>No deliveries yet</h2>
      <p>Notifications fan out to the queue as they are emitted.</p></div>`;
  const st = ov.by_status || {}, ch = ov.by_channel || {};
  const emailMode = ov.email_mode || "dry_run";
  const emailSender = ov.email_sender;
  const emailChip = emailMode === "smtp"
    ? `<span class="badge red">Email: SMTP (live)</span>${emailSender
        ? ` <span class="badge gray">from ${esc(emailSender)}</span>` : ""}`
    : `<span class="badge gray">Email: dry-run</span>`;
  const pushMode = ov.push_mode || "dry_run";
  const pushProvider = ov.push_provider;
  const pushChip = pushMode === "live"
    ? `<span class="badge red">Push: live</span>${pushProvider
        ? ` <span class="badge gray">via ${esc(pushProvider)}</span>` : ""}`
    : `<span class="badge gray">Push: dry-run</span>`;
  // Worker loop posture (#79): enabled + running, or a manual-drain hint.
  const w = ov.worker || {};
  const workerChip = w.running
    ? `<span class="badge red">Worker: on (every ${esc(w.interval_seconds)}s · batch ${esc(w.batch_size)})</span>`
    : w.enabled
      ? `<span class="badge gray">Worker: enabled, not running</span>`
      : `<span class="badge gray">Worker: manual drain</span>`;
  const modeChip = `${emailChip} ${pushChip} ${workerChip}`;
  const stat = (label, n, cls) =>
    `<div class="dq-stat ${cls}"><div class="dq-n">${n || 0}</div><div class="dq-l">${label}</div></div>`;
  const stats = `<div class="dq-stats">
    ${stat("Pending", st.pending, "pend")}
    ${stat("Sent", st.sent, "sent")}
    ${stat("Failed", st.failed, "fail")}
    ${stat("Dead-letter", st.dead_lettered, "dead")}
    ${stat("Ignored", st.ignored, "ign")}
    ${stat("Email", ch.email, "chan")}
    ${stat("Push", ch.push, "chan")}</div>`;
  const pending = st.pending || 0;
  const action = `<div class="dq-actions">
    <button class="act primary" data-process-deliveries ${pending ? "" : "disabled"}>
      Process pending${pending ? ` (${pending})` : ""}</button>
    ${modeChip}
    <span class="gs-hint">${(emailMode === "smtp" || pushMode === "live")
      ? "Live transports send to the configured provider; dry-run channels are recorded only."
      : "Dry-run — email and push are recorded, not sent (#62/#64)."}</span></div>`;
  const recent = (ov.deliveries || []).slice().reverse().slice(0, 12);
  const rows = recent.map((d) => `
    <tr>
      <td>${CHAN_ICON[d.channel] || ""} ${esc(d.channel)}</td>
      <td class="cd-ref">${esc(d.recipient_ref || "")}</td>
      <td class="cd-dest">${esc(d.destination || "")}${d.placeholder
        ? ` <span class="badge gray">placeholder</span>` : ""}</td>
      <td><span class="badge ${DELIV_BADGE[d.status] || "gray"}">${esc(d.status)}</span></td>
      <td class="dq-att">${d.attempts}</td>
      <td class="dq-err">${esc(d.last_error || "")}</td>
    </tr>`).join("");
  const table = recent.length
    ? `<div class="card"><table class="cd-table dq-table">
        <thead><tr><th>Channel</th><th>Recipient</th><th>Destination</th>
          <th>Status</th><th>Att</th><th>Last error</th></tr></thead>
        <tbody>${rows}</tbody></table></div>`
    : "";
  return `<div class="section-title">Delivery queue</div>${stats}${action}${table}`
    + renderDeadLetterPanel(ov);
}

/* Dead-letter operations (#80): parked/failed rows with retry / ignore. */
function renderDeadLetterPanel(ov) {
  const stuck = (ov.deliveries || []).filter(
    (d) => d.status === "dead_lettered" || d.status === "failed" || d.status === "ignored");
  if (!stuck.length) return "";
  const rows = stuck.map((d) => `
    <tr>
      <td>${CHAN_ICON[d.channel] || ""} ${esc(d.channel)}</td>
      <td class="cd-ref">${esc(d.recipient_ref || "")}</td>
      <td><span class="badge ${DELIV_BADGE[d.status] || "gray"}">${esc(d.status)}</span></td>
      <td class="dq-att">${d.attempts}</td>
      <td class="dq-err">${esc(d.last_error || "")}</td>
      <td class="dq-ops">
        ${d.status === "ignored" ? "" :
          `<button class="act ghost" data-delivery-retry="${esc(d.id)}">Retry</button>`}
        ${d.status === "ignored" ? "" :
          `<button class="act ghost" data-delivery-ignore="${esc(d.id)}">Ignore</button>`}
      </td>
    </tr>`).join("");
  return `<div class="section-title">Failed &amp; dead-letter</div>
    <div class="card"><table class="cd-table dq-table">
      <thead><tr><th>Channel</th><th>Recipient</th><th>Status</th>
        <th>Att</th><th>Last error</th><th>Actions</th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;
}

function renderDeviceTokensPanel(ov) {
  const f = tokenForm;
  const picks = recipientOptions(ov);
  const pickOpts = [`<option value="">Quick pick…</option>`]
    .concat(picks.map((p) => opt(p.ref, p.label, false))).join("");
  const prov = (v, label) => opt(v, label, f.provider === v);
  const form = `
    <div class="card cd-form">
      <div class="cd-grid">
        <label class="cd-field"><span>Recipient</span>
          <select id="token-pick" class="cd-input">${pickOpts}</select></label>
        <label class="cd-field"><span>recipient_ref</span>
          <input id="token-ref" class="cd-input" placeholder="official:… / team:… / scheduler"
            value="${esc(f.recipient_ref)}" /></label>
        <label class="cd-field cd-narrow"><span>Provider</span>
          <select id="token-provider" class="cd-input">${prov("fcm", "FCM")}${prov("apns", "APNs")}${prov("web", "Web push")}</select></label>
        <label class="cd-field"><span>Device token</span>
          <input id="token-value" class="cd-input" placeholder="real provider token"
            value="${esc(f.token)}" /></label>
        <label class="cd-field"><span>Device name <em>(optional)</em></span>
          <input id="token-label" class="cd-input" placeholder="e.g. Ref's iPhone"
            value="${esc(f.label)}" /></label>
        <div class="cd-submit"><button class="act primary" data-token-save>Register token</button></div>
      </div>
    </div>`;
  const rows = (deliveryState.deviceTokens || []).map((t) => `
    <tr class="${t.active ? "" : "dt-off"}">
      <td class="cd-ref">${esc(t.recipient_ref)}</td>
      <td>${esc(t.provider)}</td>
      <td class="cd-dest">${esc(t.token)}</td>
      <td>${esc(t.label || "")}</td>
      <td><span class="badge ${t.active ? "green" : "gray"}">${t.active ? "active" : "inactive"}</span></td>
      <td><button class="act ghost cd-edit" data-token-active="${esc(t.id)}"
        data-token-next="${t.active ? "0" : "1"}">${t.active ? "Deactivate" : "Reactivate"}</button></td>
    </tr>`).join("");
  const table = rows
    ? `<div class="card"><table class="cd-table">
        <thead><tr><th>Recipient</th><th>Provider</th><th>Token</th><th>Device</th>
          <th>Status</th><th></th></tr></thead>
        <tbody>${rows}</tbody></table></div>`
    : `<div class="banner neutral"><h2>No device tokens registered</h2>
        <p>Push deliveries use a <code>push-token:</code> placeholder until a real
        token is registered — live push refuses placeholders.</p></div>`;
  return `<div class="section-title">Push device tokens</div>${form}${table}`;
}

function renderDelivery(ov) {
  if (!hasPerm("manage_schedule")) {
    return `<div class="banner neutral"><h2>Operators only</h2>
      <p>Delivery contacts and the queue monitor are available to league admins
      and arena managers.</p></div>`;
  }
  return `${renderDeliveryMonitor()}${renderContactsPanel(ov)}` +
    `${renderDeviceTokensPanel(ov)}${toastHtml()}`;
}

/* ---------- Users / sessions admin (#78) ---------- */
function renderUsers() {
  if (!hasPerm("manage_users")) {
    return `<div class="banner neutral"><h2>League admins only</h2>
      <p>Account and session administration is limited to league admins.</p></div>`;
  }
  const accts = usersState.accounts;
  const accountList = accts.length
    ? accts.map((a) => `<button class="row-btn ${a.id === usersSelected ? "active" : ""}"
        data-user-sessions="${esc(a.id)}">
        <span class="row-main">${esc(a.username)}</span>
        <span class="row-sub">${esc(a.role)}${a.active ? "" : " · inactive"}</span>
      </button>`).join("")
    : `<div class="empty">No accounts yet.</div>`;
  const sessionPanel = usersSelected
    ? renderUserSessions()
    : `<p class="muted">Select an account to view its login sessions.</p>`;
  return `
    <div class="card">
      <div class="section-title">Accounts</div>
      <div class="row-list">${accountList}</div>
    </div>
    <div class="card">
      <div class="section-title">Sessions</div>
      ${sessionPanel}
    </div>${toastHtml()}`;
}

function renderUserSessions() {
  const rows = usersState.sessions;
  if (!rows.length) return `<p class="muted">No sessions on record for this account.</p>`;
  return `<div class="row-list">` + rows.map((s) => `
    <div class="session-row">
      <span class="row-main">${esc(s.user_agent || "Unknown device")}</span>
      <span class="pill ${esc(s.status)}">${esc(s.status)}</span>
      <span class="row-sub">issued ${fmtDateTime(s.issued_at)}</span>
      ${s.status === "active"
        ? `<button class="act danger" data-revoke-session="${esc(s.id)}">Revoke</button>`
        : ""}
    </div>`).join("") + `</div>`;
}

/* ---------- Draft scheduler review + publish (#86) ---------- */
function renderScheduler(ov) {
  if (!hasPerm("manage_schedule")) {
    return `<div class="banner neutral"><h2>Operators only</h2>
      <p>The draft scheduler is available to league admins and arena managers.</p></div>`;
  }
  const divs = ov.divisions || [];
  const opts = divs.map((d) =>
    `<option value="${esc(d.id)}" ${d.id === schedulerState.division ? "selected" : ""}>${esc(d.name)}</option>`).join("");
  const pv = schedulerState.preview;
  let previewBlock = "";
  if (pv) {
    const games = (pv.draft_games || pv.created || []);
    const gRows = games.map((g) => `<div class="li">
      <span class="li-time">${fmt(g.start_time)}</span>
      <div class="li-main"><div class="li-title">${esc(g.home_team_name)} vs ${esc(g.away_team_name)}</div>
        <div class="li-sub">${esc(g.rink_name || "")}</div></div></div>`).join("");
    const uRows = (pv.unscheduled || []).map((u) => `<div class="li">
      <div class="li-main"><div class="li-title">${esc(u.home_team_name)} vs ${esc(u.away_team_name)}</div>
        <div class="li-sub conflict">⚠ ${esc(u.reason)}</div></div></div>`).join("");
    previewBlock = `<div class="section-title">Preview — ${games.length} game(s), ${(pv.unscheduled || []).length} conflict(s)</div>
      <div class="card">${gRows || '<div class="empty">No games generated.</div>'}${uRows}</div>
      <div class="dq-actions"><button class="act primary" data-sched-commit>Commit as draft</button></div>`;
  }
  const drafts = schedulerState.drafts || [];
  const dRows = drafts.map((g) => `<div class="li">
    <span class="li-time">${fmt(g.start_time)}</span>
    <div class="li-main"><div class="li-title">${esc(g.home_team_name)} vs ${esc(g.away_team_name)}</div>
      <div class="li-sub">${esc(g.division_name || "")} · ${esc(g.rink_name || "")}</div></div>
    <span class="pill scheduled">draft</span></div>`).join("");
  const draftBlock = `<div class="section-title">Draft games (${drafts.length})</div>
    <div class="card">${dRows || '<div class="empty">No draft games. Generate and commit a schedule above.</div>'}</div>
    ${drafts.length ? `<div class="dq-actions">
      <button class="act primary" data-sched-publish>Publish all</button>
      <button class="act ghost danger" data-sched-discard>Discard all</button></div>` : ""}`;
  return `<div class="card">
      <div class="section-title" style="margin-top:0">Generate draft schedule</div>
      <div class="dq-actions">
        <select id="sched-div">${opts}</select>
        <button class="act" data-sched-generate>Generate</button>
      </div></div>
    ${previewBlock}${draftBlock}${toastHtml()}`;
}

/* ---------- Pilot onboarding import wizard (#96/#99) ----------
   Wires the existing #92-#95 endpoints only — no new backend logic. Each
   type below names the exact commit endpoint/permission #93/#94/#95 shipped;
   /api/import/dry-run is shared by all three (gated by manage_arena, same
   as the wizard tab itself), since #92's validator already covers every
   sheet name except official_availability (see the officials_availability
   type's `note` — that sheet is only checked at commit time, by #94's own
   sibling validator, which /api/import/dry-run never calls).

   `sample` (#99) is a ready-to-use multi-row CSV — the same column shapes
   #92's own module docstring documents — used both for the per-sheet
   "Download template" file and the per-type "Load sample data" action, so
   an operator never has to guess the exact header row from scratch. */
const IMPORT_TYPES = [
  { key: "teams_players", label: "Teams & Players", commitPerm: "manage_setup",
    commitPath: "/api/import/commit/teams-players", needsSeason: true,
    howTo: "Upload teams.csv first, then players.csv — player rows reference "
      + "their team by team_code. Existing teams/players are matched by code "
      + "and updated in place on repeat import, so re-uploading a corrected "
      + "file is always safe.",
    sheets: [
      { field: "teams_csv", label: "teams.csv",
        placeholder: "team_code,team_name,club_name,division_name",
        sample: "team_code,team_name,club_name,division_name\n"
          + "U16-LIONS,U16 Lions,North Club,U16\n"
          + "U16-FALCONS,U16 Falcons,South Club,U16\n" },
      { field: "players_csv", label: "players.csv",
        placeholder: "player_code,first_name,last_name,team_code,jersey_number,position,email",
        sample: "player_code,first_name,last_name,team_code,jersey_number,position,email\n"
          + "P001,Aarav,Mehta,U16-LIONS,12,forward,aarav@example.com\n"
          + "P002,Kabir,Shah,U16-LIONS,9,defense,\n"
          + "P003,Sam,Green,U16-FALCONS,1,goalie,sam@example.com\n" },
    ] },
  { key: "officials_availability", label: "Officials & Availability", commitPerm: "manage_schedule",
    commitPath: "/api/import/commit/officials-availability", needsSeason: false,
    note: "Validate only checks officials.csv — official_availability.csv rows "
      + "are checked when you commit (#94).",
    howTo: "official_code links the two sheets — an official already imported "
      + "by code (even in an earlier commit) can get new availability windows "
      + "without resending officials.csv every time.",
    sheets: [
      { field: "officials_csv", label: "officials.csv",
        placeholder: "official_code,name,email,home_club_name",
        sample: "official_code,name,email,home_club_name\n"
          + "O001,Riley Whistle,riley@example.com,North Club\n"
          + "O002,Lee Blueline,,South Club\n" },
      { field: "official_availability_csv", label: "official_availability.csv",
        placeholder: "official_code,start_time,end_time,status,note",
        sample: "official_code,start_time,end_time,status,note\n"
          + "O001,2026-09-01T18:00:00+00:00,2026-09-01T22:00:00+00:00,unavailable,Work shift\n" },
    ] },
  { key: "rinks_ice_slots", label: "Rinks & Ice Slots", commitPerm: "manage_arena",
    commitPath: "/api/import/commit/rinks-ice-slots", needsSeason: false,
    howTo: "ice_slots.csv rows reference their rink by rink_code from the "
      + "SAME upload's rinks.csv — send both sheets together in one commit.",
    sheets: [
      { field: "rinks_csv", label: "rinks.csv",
        placeholder: "venue_name,rink_code,rink_name,address",
        sample: "venue_name,rink_code,rink_name,address\n"
          + "Main Arena,RINK1,Rink 1,123 Ice Road\n" },
      { field: "ice_slots_csv", label: "ice_slots.csv",
        placeholder: "rink_code,start_time,end_time,slot_type",
        sample: "rink_code,start_time,end_time,slot_type\n"
          + "RINK1,2026-09-01T18:00:00+00:00,2026-09-01T19:30:00+00:00,game\n"
          + "RINK1,2026-09-01T20:00:00+00:00,2026-09-01T21:00:00+00:00,practice\n" },
    ] },
];

function importType() {
  return IMPORT_TYPES.find((t) => t.key === importState.type) || IMPORT_TYPES[0];
}

// Only the pasted sheet text counts toward "is this still what I validated?"
// — season_id isn't part of validate_import's contract at all, so changing
// the season between Validate and Commit doesn't need a re-validate.
//
// Reads the LIVE textarea DOM, not importState.sheetsText: sheetsText is
// just a cache kept for redisplay across renders (updated on every input
// event below), and comparing against a cache instead of the actual DOM
// would let an edit made after Validate go undetected if that cache were
// ever out of sync — this must always reflect what the user is looking at
// right now, at the moment Commit is clicked (review fix).
function importSnapshotKey(type) {
  const parts = {};
  type.sheets.forEach((s) => {
    const el = document.getElementById(`import-${s.field}`);
    parts[s.field] = el ? el.value : (importState.sheetsText[s.field] || "");
  });
  return JSON.stringify(parts);
}

function importCommitState(type) {
  const seasonOk = !type.needsSeason || !!importState.seasonId;
  const validated = !!importState.report && importState.report.ok
    && importState.validatedKey === importSnapshotKey(type);
  const canCommit = seasonOk && hasPerm(type.commitPerm) && validated;
  const commitTitle = !hasPerm(type.commitPerm)
    ? "Your role can't commit this import type."
    : !seasonOk ? "Choose a season first."
    : !validated ? "Validate successfully first."
    : "";
  return { canCommit, commitTitle };
}

function renderImportRows(items, cls) {
  return items.map((it) => `<div class="li"><div class="li-main">
    <div class="li-title">${esc(it.sheet || "")}${it.row != null ? ` — row ${it.row}` : ""}${it.field ? ` (${esc(it.field)})` : ""}</div>
    <div class="li-sub ${cls}">${cls === "error" ? "⚠" : "ℹ"} ${esc(it.message)}</div></div></div>`).join("");
}

function renderImportReport(report, type) {
  if (report.error) {
    return `<div class="banner alert"><h2>Could not validate</h2><p>${esc(report.error.message)}</p></div>`;
  }
  const errs = report.errors || [];
  const warns = report.warnings || [];
  // validate_import() always returns a count for all 5 canonical sheet
  // names, even ones this type never sends — filter to the type's own
  // sheets so e.g. officials_availability doesn't show a padded
  // "teams: 0 · players: 0 · rinks: 0 · ice_slots: 0" that reads as if
  // those sheets were checked too.
  const relevant = new Set(type.sheets.map((s) => s.field.replace(/_csv$/, "")));
  const summary = Object.entries(report.summary || {})
    .filter(([sheet]) => relevant.has(sheet))
    .map(([sheet, n]) => `${esc(sheet)}: ${n}`).join(" · ");
  // A type with an unvalidated sheet (see its `note`) never gets to claim
  // "ready to commit" — dry-run only checked part of what Commit will write.
  const readyCopy = type.note ? "No errors in the checked sheet(s) above."
    : "No errors — ready to commit.";
  const status = report.ok
    ? `<div class="banner ok"><h2>Looks good</h2><p>${readyCopy}</p></div>`
    : `<div class="banner alert"><h2>${errs.length} error(s) found</h2><p>Fix the rows below, then Validate again.</p></div>`;
  const rows = renderImportRows(errs, "error") + renderImportRows(warns, "warn");
  return `<div class="import-report">
      <div class="section-title">Validation report — ${summary}</div>
      ${status}
      ${rows ? `<div class="card">${rows}</div>` : ""}
    </div>`;
}

function importSummaryLine(summary) {
  return Object.entries(summary || {}).map(([key, v]) => (v && typeof v === "object")
    ? `${esc(key)}: ${v.created || 0} created, ${v.updated || 0} updated`
    : `${esc(key)}: ${v}`).join(" · ");
}

function renderImportResult(result) {
  if (result.error) {
    return `<div class="banner alert"><h2>Commit failed</h2><p>${esc(result.error.message)}</p></div>`;
  }
  if (!result.committed) {
    const errs = result.errors || [];
    const warns = result.warnings || [];
    return `<div class="import-report">
        <div class="banner alert"><h2>Not committed</h2><p>${errs.length} error(s) blocked the commit.</p></div>
        <div class="card">${renderImportRows(errs, "error")}${renderImportRows(warns, "warn")}</div>
      </div>`;
  }
  const warns = result.warnings || [];
  return `<div class="import-report">
      <div class="banner ok"><h2>Committed</h2><p>${importSummaryLine(result.summary)}</p></div>
      ${warns.length ? `<div class="card">${renderImportRows(warns, "warn")}</div>` : ""}
    </div>`;
}

function renderImport(ov) {
  if (!hasPerm("manage_arena")) {
    return `<div class="banner neutral"><h2>Operators only</h2>
      <p>The import wizard is available to league admins and arena managers.</p></div>`;
  }
  const type = importType();
  const typeButtons = IMPORT_TYPES.map((t) => `<button class="seg${t.key === type.key ? " active" : ""}"
    data-import-type="${t.key}">${esc(t.label)}</button>`).join("");

  const seasons = ov.seasons || [];
  const seasonField = !type.needsSeason ? "" : !seasons.length
    ? `<label>Season <span class="req">*</span></label>
       <div class="drawer-note">Create a season first.</div>`
    : `<label>Season <span class="req">*</span></label>
       <select id="import-season">${seasons.map((s) => `<option value="${esc(s.id)}"`
          + `${s.id === importState.seasonId ? " selected" : ""}>${esc(s.name)}</option>`).join("")}</select>`;

  const sheetFields = type.sheets.map((s) => `<div class="import-field-head">
      <label>${esc(s.label)}</label>
      <button type="button" class="linklike" data-import-template="${s.field}">Download template</button>
    </div>
    <textarea id="import-${s.field}" rows="6" placeholder="${esc(s.placeholder)}"
      >${esc(importState.sheetsText[s.field] || "")}</textarea>`).join("");
  const drawerNote = (text) => text ? `<div class="drawer-note">${esc(text)}</div>` : "";
  const noteHtml = drawerNote(type.note);
  const howToHtml = drawerNote(type.howTo);

  const { canCommit, commitTitle } = importCommitState(type);

  const reportHtml = importState.report ? renderImportReport(importState.report, type) : "";
  const resultHtml = importState.committed ? renderImportResult(importState.committed) : "";

  return `<div class="card">
      <div class="section-title" style="margin-top:0">Pilot onboarding import</div>
      <div class="segmented">${typeButtons}</div>
      <div class="import-form">${howToHtml}${seasonField}${sheetFields}${noteHtml}</div>
      <div class="dq-actions">
        <button class="act ghost" data-import-sample>Load sample data</button>
        <button class="act" data-import-validate>Validate</button>
        <button class="act primary" data-import-commit${canCommit ? "" : " disabled"}
          title="${esc(commitTitle)}">Commit</button>
      </div>
    </div>
    ${reportHtml}${resultHtml}${toastHtml()}`;
}

/* ---------- Standings (#31) ---------- */
function renderStandings(ov, standings) {
  if (!ov.divisions.length) return `<div class="empty">No divisions yet. Create one in Setup.</div>`;
  const opts = ov.divisions.map((d) =>
    `<option value="${esc(d.id)}" ${d.id === standingsDivision ? "selected" : ""}>${esc(d.name)}</option>`).join("");
  const rows = (standings && standings.standings) || [];
  const body = rows.length
    ? rows.map((r, i) => `<tr>
        <td class="st-rank">${i + 1}</td>
        <td class="st-team">${esc(r.team_name)}</td>
        <td>${r.gp}</td><td>${r.w}</td><td>${r.l}</td><td>${r.t}</td>
        <td>${r.gf}</td><td>${r.ga}</td>
        <td>${r.gd > 0 ? "+" + r.gd : r.gd}</td>
        <td class="st-pts">${r.pts}</td></tr>`).join("")
    : `<tr><td colspan="10" class="st-empty">No teams in this division yet.</td></tr>`;
  const anyPlayed = rows.some((r) => r.gp > 0);
  return `
    <div class="st-toolbar">
      <select id="standings-div" class="st-div">${opts}</select>
      <span class="gs-hint">Points: win 2 · tie 1 · loss 0 — from <strong>Final</strong> results only.</span>
    </div>
    <div class="card st-card"><table class="st-table">
      <thead><tr><th>#</th><th class="st-team">Team</th><th>GP</th><th>W</th><th>L</th><th>T</th>
        <th>GF</th><th>GA</th><th>GD</th><th>Pts</th></tr></thead>
      <tbody>${body}</tbody></table></div>
    ${anyPlayed ? "" : `<div class="privacy-note">No games have a Final result yet. Enter and approve a score on a game's Game Sheet.</div>`}
    ${toastHtml()}`;
}
function officialsPanel(lineups) {
  const canManage = hasPerm("manage_schedule");        // assign / unassign (operator)
  const canRespondAny = hasPerm("respond_assignment"); // admin, or a signed-in official
  const myOfficialId = (currentUser && currentUser.scope) ? currentUser.scope.official_id : null;
  // An official may respond only to their own proposed assignment (#54); an
  // admin (respond permission, no official scope) may respond to any.
  const canRespondTo = (a) => canRespondAny && a.status === "proposed"
    && (!myOfficialId || a.official_id === myOfficialId);
  const assigned = lineups.officials || [];
  const ROLES = [["referee", "👨‍⚖️ Referee"], ["linesperson", "🚩 Linesperson"],
                 ["scorekeeper", "📝 Scorekeeper"]];
  const badge = (st) => st === "accepted" ? `<span class="badge green">Accepted</span>`
    : st === "declined" ? `<span class="badge red">Declined</span>`
    : `<span class="badge orange">Proposed</span>`;
  const roleBlocks = ROLES.map(([role, label]) => {
    const list = assigned.filter((a) => a.role === role);
    const body = list.length
      ? list.map((a) => {
        const mine = myOfficialId && a.official_id === myOfficialId;
        return `<div class="gs-off-row${mine ? " mine" : ""}"><span class="gs-off-name">${esc(a.official_name)}${mine ? ` <span class="you-tag">you</span>` : ""}</span>${badge(a.status)}
          ${canRespondTo(a)
            ? `<button class="act success" data-accept="${a.assignment_id}">Accept</button>
               <button class="act danger" data-decline="${a.assignment_id}">Decline</button>` : ""}
          ${canManage ? `<button class="act danger ghost xbtn" data-unassign="${a.assignment_id}" title="Unassign">✕</button>` : ""}</div>`;
      }).join("")
      : `<div class="gs-off-slot">Unassigned</div>`;
    return `<div class="gs-off-role"><div class="gs-off-title">${label}</div>${body}</div>`;
  }).join("");
  const assignForm = (canManage && officialsPool.length) ? `<div class="gs-assign no-print">
      <select id="off-pick">${officialsPool.map((o) => `<option value="${esc(o.id)}">${esc(o.name)}</option>`).join("")}</select>
      <select id="off-role"><option value="referee">Referee</option><option value="linesperson">Linesperson</option><option value="scorekeeper">Scorekeeper</option></select>
      <button class="act primary" data-assign-official>Assign</button></div>` : "";
  const officialHint = (myOfficialId && !canManage)
    ? `<div class="gs-note">You're signed in as an official — accept or decline your own assignments below.</div>` : "";
  return `<section class="gs-officials">
    <div class="gs-off-head">👥 Officials <a class="badge" href="${REPO}/30" target="_blank">#30</a></div>
    ${officialHint}<div class="gs-off-grid">${roleBlocks}</div>${assignForm}</section>`;
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
  // Resource scoping (#51): a bound coach may edit only their own team's side.
  const boundTeam = (currentUser && currentUser.scope) ? currentUser.scope.team_id : null;
  const inScope = !boundTeam || board.team_id === boundTeam;
  const canRoster = hasPerm("manage_roster") && inScope;   // #24 + #51
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

  // Footer: lock control for roster managers, else a read-only note by role/scope.
  let footer;
  if (!inScope && hasPerm("manage_roster")) {
    const team = currentUser.scope.team_name || "your team";
    footer = `<div class="locked-note">🔒 Read-only — you manage <strong>${esc(team)}</strong>, not this team.</div>`;
  } else if (!canRoster) {
    footer = `<div class="locked-note">🔒 Read-only — your role can't manage rosters.</div>`;
  } else if (boundTeam) {
    // A scoped coach can select their team but not lock/unlock the whole game
    // (that flips shared game state) — #51. Show a note instead of the control.
    footer = locked
      ? `<div class="locked-note">🔒 Roster locked by a league admin.</div>`
      : `<div class="locked-note">Per-team roster locking is coming; a league admin locks the full game.</div>`;
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
  // Resource scoping (#51): a bound player is locked to their own record and
  // can only respond for themselves.
  const ownPlayer = (currentUser && currentUser.scope) ? currentUser.scope.player_id : null;
  const boundHere = ownPlayer && players.some((p) => p.id === ownPlayer);
  if (boundHere) pickedPlayer = ownPlayer;
  else if (!pickedPlayer || !players.find((p) => p.id === pickedPlayer)) pickedPlayer = players[0] ? players[0].id : null;
  const options = players.map((p) => opt(p.id, `${p.name} · ${p.position}`, p.id === pickedPlayer)).join("");
  const p = players.find((x) => x.id === pickedPlayer);
  const canRespond = hasPerm("respond_availability") && (!ownPlayer || pickedPlayer === ownPlayer);
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
  const picker = boundHere
    ? `<div class="scope-note">Signed in as <strong>${esc((p && p.name) || currentUser.scope.player_name || "you")}</strong> — you can only respond for yourself.</div>`
    : `<div class="section-title">View as player</div>
       <select class="player-picker" id="player-picker">${options}</select>`;
  return `${picker}${card}
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
  team_created: "Team created", team_updated: "Team updated",
  venue_created: "Venue created",
  rink_created: "Rink created", rink_updated: "Rink updated",
  ice_slot_created: "Ice slot created", ice_slot_updated: "Ice slot updated",
  game_created: "Game scheduled", player_added: "Player added",
  player_updated: "Player updated",
  official_created: "Official created", official_updated: "Official updated",
  official_availability_set: "Official availability set",
  official_availability_updated: "Official availability updated",
  import_committed: "Import committed",
};
function tlRow(time, msg, dotColor) {
  return `<div class="tl-item"><div class="tl-dot" style="background:${dotColor || "var(--blue)"}"></div>
    <div class="tl-body"><div class="tl-msg">${msg}</div></div>
    <div class="tl-time">${esc(time || "")}</div></div>`;
}
function humanizeAuditKey(k) {
  return k.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
}
// The flat counts an import commit records (teams_created, skipped, errors,
// …) minus the linking/grouping keys, humanized for display (#102).
function importBatchSummaryLine(detail) {
  const skip = new Set(["import_batch_id", "import_type", "season_id"]);
  return Object.entries(detail || {})
    .filter(([k]) => !skip.has(k))
    .map(([k, v]) => `${humanizeAuditKey(k)}: ${v}`)
    .join(" · ");
}
// Setup-audit entries for an import commit come in two shapes: one
// entity_type="import_batch" summary row (counts in `detail`) plus N
// per-row entries tagged `detail.import_batch_id` back to that summary.
// Group them so the feed shows one line per import, expandable to the rows
// underneath, instead of drowning the feed in every row it touched (#102).
function renderSetupAuditFeed(ov) {
  const all = [...((ov && ov.setup_audit) || [])].reverse();  // newest first
  const isBatch = (a) => a.entity_type === "import_batch";
  const batchIdOf = (a) => (a.detail && a.detail.import_batch_id) || null;
  const topLevel = all.filter((a) => isBatch(a) || !batchIdOf(a)).slice(0, 40);
  if (!topLevel.length) return `<div class="empty">No setup activity.</div>`;
  return topLevel.map((a) => {
    if (!isBatch(a)) {
      return tlRow(fmt(a.at),
        `<strong>${esc(SETUP_LABEL[a.action] || a.action)}</strong> · ${esc(a.entity_id)}`,
        "#06b6d4");
    }
    const importType = (a.detail || {}).import_type;
    const typeLabel = (IMPORT_TYPES.find((t) => t.key === importType) || {}).label || importType;
    const children = all.filter((c) => !isBatch(c) && batchIdOf(c) === a.entity_id);
    const expanded = activityExpandedBatches.has(a.entity_id);
    const toggle = children.length
      ? `<button class="linklike" data-audit-toggle="${esc(a.entity_id)}">${expanded ? "Hide" : "Show"} ${children.length} row${children.length === 1 ? "" : "s"}</button>`
      : "";
    const childrenHtml = expanded && children.length
      ? `<div class="tl-children">${children.map((c) =>
          tlRow(fmt(c.at), `${esc(SETUP_LABEL[c.action] || c.action)} · ${esc(c.entity_id)}`, "#94a3b8")).join("")}</div>`
      : "";
    return `<div class="tl-item"><div class="tl-dot" style="background:#06b6d4"></div>
      <div class="tl-body">
        <div class="tl-msg"><strong>${esc(SETUP_LABEL.import_committed)}${typeLabel ? " — " + esc(typeLabel) : ""}</strong>${a.actor_id ? " · " + esc(a.actor_id) : ""} ${toggle}</div>
        <div class="tl-sub">${esc(importBatchSummaryLine(a.detail))}</div>
        ${childrenHtml}
      </div>
      <div class="tl-time">${esc(fmt(a.at) || "")}</div></div>`;
  }).join("");
}
function renderActivity(board, ov) {
  const setupHtml = renderSetupAuditFeed(ov);
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
  const ps = publicState.schedule || { fixtures: [], divisions: [] };
  const lgName = ps.league_name || (ov.league || {}).name || "League";
  // A selected game's public result detail (#83).
  if (publicState.game) {
    const g = publicState.game;
    const score = (g.home_score != null)
      ? `<div class="pub-score">${g.home_score} – ${g.away_score}</div>` : "";
    return `<div class="hero"><div class="when">${esc(g.status)}</div>
        <h2>${esc(g.home_team_name)} vs ${esc(g.away_team_name || "TBD")}</h2>
        <div class="where">${esc(g.division_name || "")} · ${esc(g.venue_name || "")} · ${esc(g.rink_name || "")}</div></div>
      <div class="card pub-detail">${score}
        <div class="li-sub">${esc(fmtDateTime(g.start_time))}</div></div>
      <div class="actions"><button class="act ghost" data-public-back>← Back to schedule</button></div>`;
  }
  const tabBtn = (key, label) =>
    `<button class="seg ${publicTab === key ? "active" : ""}" data-public-tab="${key}">${label}</button>`;
  const tabs = `<div class="seg-group">${tabBtn("schedule", "Schedule")}${tabBtn("standings", "Standings")}</div>`;
  let body;
  if (publicTab === "standings") {
    const divs = ps.divisions || [];
    const opts = divs.map((d) =>
      `<option value="${esc(d.id)}" ${d.id === publicState.division ? "selected" : ""}>${esc(d.name)}</option>`).join("");
    const rows = ((publicState.standings && publicState.standings.standings) || []);
    const trs = rows.length ? rows.map((r, i) => `<tr>
        <td class="st-rank">${i + 1}</td><td class="st-team">${esc(r.team_name)}</td>
        <td>${r.gp}</td><td>${r.w}</td><td>${r.l}</td><td>${r.t}</td>
        <td>${r.gf}</td><td>${r.ga}</td><td>${r.gd > 0 ? "+" + r.gd : r.gd}</td>
        <td class="st-pts">${r.pts}</td></tr>`).join("")
      : `<tr><td colspan="10" class="empty">No results yet.</td></tr>`;
    body = `${divs.length ? `<div class="actions"><select id="public-div">${opts}</select></div>` : ""}
      <div class="card st-card"><table class="st-table">
        <thead><tr><th>#</th><th>Team</th><th>GP</th><th>W</th><th>L</th><th>T</th>
          <th>GF</th><th>GA</th><th>GD</th><th>Pts</th></tr></thead>
        <tbody>${trs}</tbody></table></div>`;
  } else {
    const rows = (ps.fixtures || []).map((f) => {
      const score = (f.home_score != null) ? ` <strong>${f.home_score}–${f.away_score}</strong>` : "";
      const cls = f.status === "Final" ? "gray" : f.status === "Cancelled" ? "blocked" : "scheduled";
      return `<button class="li li-btn" data-public-game="${esc(f.game_id)}">
        <span class="li-time">${fmt(f.start_time)}</span>
        <div class="li-main"><div class="li-title">${esc(f.home_team_name)} vs ${esc(f.away_team_name || "TBD")}${score}</div>
          <div class="li-sub">${esc(f.division_name || "")} · ${esc(f.venue_name || "")} · ${esc(f.rink_name || "")}</div></div>
        <span class="pill ${cls}">${esc(f.status)}</span></button>`;
    }).join("");
    body = `<div class="card">${rows || '<div class="empty">No fixtures.</div>'}</div>`;
  }
  return `<div class="hero"><div class="when">Public</div><h2>${esc(lgName)}</h2></div>
    ${tabs}${body}
    <div class="privacy-note">🔒 Public view shows fixtures, scores, and standings only.
      Player names and all personal data are never exposed (policy: #35).</div>`;
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
  if (!bc) return;
  if (view === "dashboard" && ov) {
    const games = (ov.schedule || []).length;
    const rinks = (ov.rinks || []).length;
    const when = new Date(calendarDate + "T00:00:00Z").toLocaleDateString("en-GB",
      { weekday: "long", day: "numeric", month: "long", timeZone: "UTC" });
    bc.textContent = `${when} · ${games} game${games === 1 ? "" : "s"} this week`
      + (rinks ? ` across ${rinks} rink${rinks === 1 ? "" : "s"}` : "");
  } else {
    const league = (ov && ov.league && ov.league.name) || "No league yet";
    const season = (ov && ov.seasons && ov.seasons[0] && ov.seasons[0].name) || "No season yet";
    bc.textContent = `${league} · ${season}`;
  }
}

async function render() {
  const c = document.getElementById("content");
  document.body.dataset.view = view;
  let ov, board, lineups, standings, inbox;
  try {
    c.innerHTML = `<div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div>`;
    ov = await getJSON("/api/demo/overview");
    if (ov && ov.error) throw new Error(ov.error.message);
    if (!currentGame && ov.schedule[0]) currentGame = ov.schedule[0].game_id;
    board = (view === "activity" && currentGame) ? await getJSON(`/api/games/${currentGame}/board`) : null;
    // The roster tab and the game sheet both use both sides' lineups (#25/#48).
    lineups = (["roster", "sheet"].includes(view) && currentGame)
      ? await getJSON(`/api/games/${currentGame}/lineups`) : null;
    // Availability rollup for the roster screen's selected side (#89).
    availSummary = null;
    if (view === "roster" && lineups && lineups[rosterSide] && !lineups.error) {
      const tid = lineups[rosterSide].team_id;
      const s = await getJSON(
        `/api/games/${currentGame}/availability-summary?team_id=${tid}`);
      if (s && !s.error) availSummary = s;
    }
    // The game sheet also needs the officials pool for its assign control (#30).
    if (view === "sheet") {
      const op = await getJSON("/api/officials");
      officialsPool = (op && op.officials) || [];
    }
    // The signed-in official's inbox (#55).
    if (view === "inbox") inbox = await getJSON("/api/me/assignments");
    // The official's own availability windows (#88).
    officialAvailability = [];
    if (view === "inbox" && inbox && inbox.official_id) {
      const av = await getJSON(`/api/officials/${inbox.official_id}/availability`);
      if (av && !av.error) officialAvailability = av.availability || [];
    }
    // Notifications feed drives the bell badge on every view (#32).
    const nf = await getJSON("/api/notifications");
    if (nf && !nf.error) notifState = nf;
    // Self-service channel preferences for the signed-in user (#81).
    notifPrefs = null;
    feedTokens = [];
    if (view === "notifications") {
      const ref = ownRecipientRef();
      if (ref) {
        const pr = await getJSON(
          `/api/notifications/preferences?recipient_ref=${encodeURIComponent(ref)}`);
        if (pr && !pr.error) notifPrefs = pr;
      }
      const actor = ownFeedActor();
      if (actor) {
        const ft = await getJSON(`/api/calendar-feeds?actor_type=${actor.actor_type}`
          + `&actor_ref=${encodeURIComponent(actor.actor_ref)}`);
        if (ft && !ft.error) feedTokens = (ft.feed_tokens || []).filter((t) => !t.revoked);
      }
    }
    // Delivery admin: contacts + queue overview, operator-only (#61).
    if (view === "delivery" && hasPerm("manage_schedule")) {
      const [op, contacts, overview, tokens] = await Promise.all([
        getJSON("/api/officials"),
        getJSON("/api/notifications/contacts"),
        getJSON("/api/notifications/deliveries"),
        getJSON("/api/notifications/device-tokens"),
      ]);
      officialsPool = (op && op.officials) || [];
      deliveryState = {
        contacts: (contacts && contacts.contacts) || [],
        overview: (overview && !overview.error) ? overview : null,
        deviceTokens: (tokens && tokens.device_tokens) || [],
      };
    }
    // Draft scheduler review (#86), operator-only.
    if (view === "scheduler" && hasPerm("manage_schedule")) {
      if (!schedulerState.division && ov.divisions[0]) {
        schedulerState.division = ov.divisions[0].id;
      }
      const dr = await getJSON("/api/scheduler/drafts");
      schedulerState.drafts = (dr && dr.draft_games) || [];
    }
    // Import wizard (#96): default the season picker once seasons exist,
    // same pattern as schedulerState.division/standingsDivision above —
    // the state default belongs here in the impure orchestrator, not
    // inside renderImport() itself, which stays a pure string-builder.
    if (view === "import" && !importState.seasonId && ov.seasons[0]) {
      importState.seasonId = ov.seasons[0].id;
    }
    // Account/session admin, League-Admin only (#78).
    if (view === "users" && hasPerm("manage_users")) {
      const acc = await getJSON("/api/accounts");
      usersState.accounts = (acc && acc.user_accounts) || [];
      if (usersSelected && !usersState.accounts.some((a) => a.id === usersSelected)) {
        usersSelected = null;
      }
      if (usersSelected) {
        const s = await getJSON(`/api/accounts/${usersSelected}/sessions`);
        usersState.sessions = (s && s.sessions) || [];
      } else {
        usersState.sessions = [];
      }
    }
    // Public surface (#83): schedule + standings from public-safe endpoints.
    if (view === "public") {
      const sch = await getJSON("/api/public/schedule");
      publicState.schedule = (sch && !sch.error) ? sch : { fixtures: [], divisions: [] };
      if (!publicState.division && publicState.schedule.divisions[0]) {
        publicState.division = publicState.schedule.divisions[0].id;
      }
      if (publicTab === "standings" && publicState.division) {
        publicState.standings = await getJSON(
          `/api/public/standings/${publicState.division}`);
      }
    }
    // Standings for the selected division (#31).
    if (view === "standings") {
      if (!standingsDivision || !ov.divisions.some((d) => d.id === standingsDivision)) {
        standingsDivision = ov.divisions[0] ? ov.divisions[0].id : null;
      }
      standings = standingsDivision
        ? await getJSON(`/api/standings/${standingsDivision}`) : null;
    }
    // The Dashboard shows a standings snapshot for the first division.
    if (view === "dashboard" && ov.divisions[0]) {
      standings = await getJSON(`/api/standings/${ov.divisions[0].id}`);
    }
  } catch (e) {
    setChrome(ov);
    c.innerHTML = `<div class="banner alert"><h2>Could not load data</h2>
      <p>The backend may not be running. ${esc(e.message || e)}</p></div>
      <div class="actions"><button class="act primary" id="retry-btn">Retry</button></div>`;
    const retry = document.getElementById("retry-btn");
    if (retry) retry.onclick = () => render();  // no inline handler (CSP)
    return;
  }

  setChrome(ov);
  updateNotifBadge();
  // Roster/Sheet expose private player data — a signed-in user outside the
  // game's scope gets a 403 (#73). Show a clear "restricted" state instead of
  // the generic backend-error banner.
  if (["roster", "sheet"].includes(view) && lineups && lineups.error) {
    c.innerHTML = `<div class="banner neutral"><h2>Restricted</h2>
      <p>${esc(lineups.error.message
        || "You don't have access to this game's roster.")}</p></div>`;
    return;
  }
  c.innerHTML =
    view === "dashboard" ? renderDashboard(ov, standings)
    : view === "setup" ? renderSetup(ov)
    : view === "import" ? renderImport(ov)
    : view === "calendar" ? renderCalendar(ov)
    : view === "games" ? renderGames(ov)
    : view === "roster" ? renderRoster(lineups)
    : view === "sheet" ? renderGameSheet(lineups)
    : view === "inbox" ? renderInbox(inbox)
    : view === "notifications" ? renderNotifications()
    : view === "delivery" ? renderDelivery(ov)
    : view === "users" ? renderUsers()
    : view === "scheduler" ? renderScheduler(ov)
    : view === "standings" ? renderStandings(ov, standings)
    : view === "activity" ? renderActivity(board, ov)
    : renderPublic(ov);

  c.querySelectorAll("[data-goto]").forEach((b) => b.onclick = () => switchTab(b.dataset.goto));
  // Public surface (#83): tab switch, division select, game detail, back.
  c.querySelectorAll("[data-public-tab]").forEach((b) => b.onclick = () => {
    publicTab = b.dataset.publicTab; publicState.game = null; render();
  });
  const pubDiv = c.querySelector("#public-div");
  if (pubDiv) pubDiv.onchange = () => { publicState.division = pubDiv.value; render(); };
  c.querySelectorAll("[data-public-game]").forEach((b) => b.onclick = async () => {
    const g = await getJSON(`/api/public/games/${b.dataset.publicGame}`);
    publicState.game = (g && !g.error) ? g : null; render();
  });
  const pubBack = c.querySelector("[data-public-back]");
  if (pubBack) pubBack.onclick = () => { publicState.game = null; render(); };
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
  c.querySelectorAll(".seg[data-view]").forEach((b) => b.onclick = () => { gameView = b.dataset.view; toast = ""; render(); });
  c.querySelectorAll("[data-side]").forEach((b) => b.onclick = () => { rosterSide = b.dataset.side; toast = ""; render(); });
  // Availability summary filter + remind (#89).
  c.querySelectorAll("[data-avail-filter]").forEach((b) => b.onclick = () => {
    availFilter = b.dataset.availFilter; render();
  });
  const remindBtn = c.querySelector("[data-avail-remind]");
  if (remindBtn) remindBtn.onclick = async () => {
    toast = "";
    const tid = availSummary && availSummary.team_id;
    const res = await post(`/api/games/${currentGame}/availability/remind`, { team_id: tid });
    if (res && !res.error) toast = `Reminder sent for ${res.reminded} player(s).`;
    await render();
  };
  const printBtn = c.querySelector("[data-print]");
  if (printBtn) printBtn.onclick = () => window.print();
  // Officials assignment (#30): assign from the pool, official accepts/declines.
  const assignBtn = c.querySelector("[data-assign-official]");
  if (assignBtn) assignBtn.onclick = async () => {
    const official_id = val("off-pick");
    const role = val("off-role");
    if (!official_id) return;
    const r = await post(`/api/games/${currentGame}/officials/assign`, { official_id, role });
    if (r && !r.error) toast = "Official assigned.";
    await render();
  };
  c.querySelectorAll("[data-accept]").forEach((b) => b.onclick = async () => {
    await post(`/api/officials/assignments/${b.dataset.accept}/accept`, {}); await render();
  });
  c.querySelectorAll("[data-decline]").forEach((b) => b.onclick = async () => {
    await post(`/api/officials/assignments/${b.dataset.decline}/decline`, {}); await render();
  });
  // Official availability (#88): add / remove windows.
  const availAdd = c.querySelector("[data-avail-add]");
  if (availAdd) availAdd.onclick = async () => {
    const oid = inbox && inbox.official_id;
    const start = val("avail-start"), end = val("avail-end");
    if (!oid || !start || !end) { toast = "Pick a start and end time."; return render(); }
    toast = "";
    // datetime-local has no zone; treat as UTC for the demo.
    await post(`/api/officials/${oid}/availability`, {
      start_time: start + ":00Z", end_time: end + ":00Z",
      status: val("avail-status") || "unavailable", note: val("avail-note") });
    await render();
  };
  c.querySelectorAll("[data-avail-del]").forEach((b) => b.onclick = async () => {
    toast = "";
    await post(`/api/officials/availability/${b.dataset.availDel}/delete`, {});
    await render();
  });
  c.querySelectorAll("[data-unassign]").forEach((b) => b.onclick = async () => {
    await post(`/api/officials/assignments/${b.dataset.unassign}/unassign`, {});
    toast = "Official unassigned."; await render();
  });
  // Result entry + approval (#31).
  const recBtn = c.querySelector("[data-record-result]");
  if (recBtn) recBtn.onclick = async () => {
    const r = await post(`/api/games/${currentGame}/result`,
      { home_score: val("res-home"), away_score: val("res-away") });
    if (r && !r.error) toast = "Score saved (draft).";
    await render();
  };
  const apprBtn = c.querySelector("[data-approve-result]");
  if (apprBtn) apprBtn.onclick = async () => {
    const r = await post(`/api/games/${currentGame}/result/approve`, {});
    if (r && !r.error) toast = "Result approved — standings updated.";
    await render();
  };
  const stDiv = c.querySelector("#standings-div");
  if (stDiv) stDiv.onchange = (e) => { standingsDivision = e.target.value; render(); };
  // Inbox: jump to a game's sheet (#55).
  c.querySelectorAll("[data-open-sheet]").forEach((b) => b.onclick = () => {
    currentGame = b.dataset.openSheet; switchTab("sheet");
  });
  // Notifications (#32): mark read on tap, open the related game, mark all.
  c.querySelectorAll("[data-notif-open]").forEach((b) => b.onclick = async (e) => {
    e.stopPropagation();
    const card = b.closest("[data-notif-read]");
    if (card) await post(`/api/notifications/${card.dataset.notifRead}/read`, {});
    currentGame = b.dataset.notifOpen; switchTab("sheet");
  });
  c.querySelectorAll("[data-notif-read]").forEach((el) => el.onclick = async () => {
    if (el.classList.contains("read")) return;
    await post(`/api/notifications/${el.dataset.notifRead}/read`, {}); await render();
  });
  const readAll = c.querySelector("[data-notif-readall]");
  if (readAll) readAll.onclick = async () => { await post("/api/notifications/read-all", {}); await render(); };
  // Self-service delivery-channel toggles (#81).
  c.querySelectorAll("[data-pref-channel]").forEach((el) => el.onchange = async () => {
    const ref = ownRecipientRef();
    if (!ref) return;
    toast = "";
    await post("/api/notifications/preferences", {
      recipient_ref: ref, channel: el.dataset.prefChannel, enabled: el.checked });
    await render();
  });
  // Calendar feed create / revoke (#82).
  const feedCreate = c.querySelector("[data-feed-create]");
  if (feedCreate) feedCreate.onclick = async () => {
    const actor = ownFeedActor();
    if (!actor) return;
    toast = "";
    const res = await post("/api/calendar-feeds", actor);
    newFeedUrl = (res && res.url) || null;
    await render();
  };
  c.querySelectorAll("[data-feed-revoke]").forEach((b) => b.onclick = async () => {
    toast = ""; newFeedUrl = null;
    await post(`/api/calendar-feeds/${b.dataset.feedRevoke}/revoke`, {});
    await render();
  });
  // Delivery admin (#61): quick-pick, save contact, edit, process queue.
  const pick = c.querySelector("#contact-pick");
  if (pick) pick.onchange = (e) => {
    const ref = c.querySelector("#contact-ref");
    if (ref && e.target.value) ref.value = e.target.value;
  };
  const saveContact = c.querySelector("[data-contact-save]");
  if (saveContact) saveContact.onclick = async () => {
    contactForm = {
      recipient_ref: val("contact-ref"), channel: val("contact-channel"),
      destination: val("contact-dest"), label: val("contact-label"),
    };
    toast = "";
    const res = await post("/api/notifications/contacts", {
      recipient_ref: contactForm.recipient_ref, channel: contactForm.channel,
      destination: contactForm.destination, label: contactForm.label || null,
    });
    if (res && !res.error) {
      toast = "Contact saved.";
      contactForm = { recipient_ref: "", channel: "email", destination: "", label: "" };
    }
    await render();
  };
  c.querySelectorAll("[data-contact-edit]").forEach((b) => b.onclick = () => {
    const cd = (deliveryState.contacts || []).find((x) => x.id === b.dataset.contactEdit);
    if (cd) contactForm = { recipient_ref: cd.recipient_ref, channel: cd.channel,
      destination: cd.destination, label: cd.label || "" };
    toast = ""; render();
  });
  const procBtn = c.querySelector("[data-process-deliveries]");
  if (procBtn) procBtn.onclick = async () => {
    const res = await post("/api/notifications/deliveries/process", {});
    if (res && !res.error) toast = `Processed ${res.processed} · sent ${res.sent}` +
      (res.failed ? ` · failed ${res.failed}` : "") +
      (res.dead_lettered ? ` · dead-lettered ${res.dead_lettered}` : "");
    await render();
  };
  // Dead-letter operations (#80): requeue or ignore a stuck delivery.
  c.querySelectorAll("[data-delivery-retry]").forEach((b) => b.onclick = async () => {
    toast = "";
    await post(`/api/notifications/deliveries/${b.dataset.deliveryRetry}/retry`, {});
    await render();
  });
  c.querySelectorAll("[data-delivery-ignore]").forEach((b) => b.onclick = async () => {
    toast = "";
    await post(`/api/notifications/deliveries/${b.dataset.deliveryIgnore}/ignore`, {});
    await render();
  });
  // Device tokens (#65): quick-pick, register, activate/deactivate.
  const tokenPick = c.querySelector("#token-pick");
  if (tokenPick) tokenPick.onchange = (e) => {
    const ref = c.querySelector("#token-ref");
    if (ref && e.target.value) ref.value = e.target.value;
  };
  const saveToken = c.querySelector("[data-token-save]");
  if (saveToken) saveToken.onclick = async () => {
    tokenForm = {
      recipient_ref: val("token-ref"), provider: val("token-provider"),
      token: val("token-value"), label: val("token-label"),
    };
    toast = "";
    const res = await post("/api/notifications/device-tokens", {
      recipient_ref: tokenForm.recipient_ref, provider: tokenForm.provider,
      token: tokenForm.token, label: tokenForm.label || null,
    });
    if (res && !res.error) {
      toast = "Device token registered.";
      tokenForm = { recipient_ref: "", provider: "fcm", token: "", label: "" };
    }
    await render();
  };
  c.querySelectorAll("[data-token-active]").forEach((b) => b.onclick = async () => {
    toast = "";
    await post(`/api/notifications/device-tokens/${b.dataset.tokenActive}/active`,
      { active: b.dataset.tokenNext === "1" });
    await render();
  });
  // Draft scheduler (#86): generate preview, commit, publish, discard.
  const schedDiv = c.querySelector("#sched-div");
  if (schedDiv) schedDiv.onchange = () => { schedulerState.division = schedDiv.value; };
  const schedGen = c.querySelector("[data-sched-generate]");
  if (schedGen) schedGen.onclick = async () => {
    toast = "";
    const res = await post("/api/scheduler/draft", { division_id: schedulerState.division });
    schedulerState.preview = (res && !res.error) ? res : null;
    await render();
  };
  const schedCommit = c.querySelector("[data-sched-commit]");
  if (schedCommit) schedCommit.onclick = async () => {
    toast = "";
    const res = await post("/api/scheduler/commit", { division_id: schedulerState.division });
    if (res && !res.error) { schedulerState.preview = null; toast = `Committed ${res.created.length} draft game(s).`; }
    await render();
  };
  const schedPub = c.querySelector("[data-sched-publish]");
  if (schedPub) schedPub.onclick = async () => {
    toast = "";
    const res = await post("/api/scheduler/drafts/publish", { all: true });
    if (res && !res.error) toast = `Published ${res.published} game(s).`;
    await render();
  };
  const schedDis = c.querySelector("[data-sched-discard]");
  if (schedDis) schedDis.onclick = async () => {
    toast = "";
    const res = await post("/api/scheduler/drafts/discard", { all: true });
    if (res && !res.error) toast = `Discarded ${res.discarded} draft(s).`;
    await render();
  };
  // Pilot onboarding import wizard (#96): switch type, validate, commit.
  c.querySelectorAll("[data-import-type]").forEach((b) => b.onclick = () => {
    importState.type = b.dataset.importType;
    importState.sheetsText = {};
    importState.report = null;
    importState.validatedKey = null;
    importState.committed = null;
    toast = "";
    render();
  });
  // "Load sample data" (#99): fills every sheet for the current type at
  // once, so an operator can see the whole validate → commit flow work
  // without typing anything. A full render() is fine here (unlike the
  // per-keystroke sync below) since this is a single discrete click, not
  // something that would fight the user for cursor focus.
  const importSample = c.querySelector("[data-import-sample]");
  if (importSample) importSample.onclick = () => {
    const type = importType();
    type.sheets.forEach((s) => { importState.sheetsText[s.field] = s.sample; });
    importState.report = null;
    importState.validatedKey = null;
    importState.committed = null;
    toast = "Sample data loaded — click Validate to preview it.";
    render();
  };
  const importSeason = c.querySelector("#import-season");
  if (importSeason) importSeason.onchange = () => { importState.seasonId = importSeason.value; };
  // Builds the POST body straight from the LIVE textarea DOM — always, for
  // both Validate and Commit. importState.sheetsText is a display cache
  // only (kept in sync below by each textarea's own `input` handler so a
  // re-render, e.g. after Commit, shows what's actually in the box); it is
  // never the source of truth for what gets sent, so Commit can't send
  // stale content out of sync with what's on screen (review fix).
  const buildImportBody = (type) => {
    const body = {};
    type.sheets.forEach((s) => {
      const el = document.getElementById(`import-${s.field}`);
      const text = el ? el.value : (importState.sheetsText[s.field] || "");
      if (text.trim()) body[s.field] = text;
    });
    return body;
  };
  const importCommitBtn = c.querySelector("[data-import-commit]");
  const currentImportType = importType();
  // Every keystroke re-syncs the display cache and the Commit button's
  // enabled/disabled state — WITHOUT a full render() (which would replace
  // the textarea DOM node mid-edit and drop focus/cursor position). This is
  // what actually makes "editing after Validate disables Commit" true: the
  // staleness check above already re-reads the DOM at click time regardless,
  // but without this the button's own visual state would lag until the next
  // unrelated render.
  currentImportType.sheets.forEach((s) => {
    const el = c.querySelector(`#import-${s.field}`);
    if (el) el.oninput = () => {
      importState.sheetsText[s.field] = el.value;
      if (importCommitBtn) {
        const { canCommit, commitTitle } = importCommitState(currentImportType);
        importCommitBtn.disabled = !canCommit;
        importCommitBtn.title = commitTitle;
      }
    };
    // Per-sheet CSV template download (#99): `s` is already this exact
    // sheet's config (sample text included), so no click-time lookup is
    // needed — reuse it directly instead of re-deriving it from a data
    // attribute via importType().sheets.find(...) on every click.
    const templateBtn = c.querySelector(`[data-import-template="${s.field}"]`);
    if (templateBtn) templateBtn.onclick = () =>
      downloadTextFile(`${s.field.replace(/_csv$/, "")}.csv`, s.sample);
  });
  const importValidate = c.querySelector("[data-import-validate]");
  if (importValidate) importValidate.onclick = async () => {
    const type = importType();
    const body = buildImportBody(type);
    // Snapshot what's actually being validated so a response that arrives
    // after the sheets changed underneath it (switched type, hit "Load
    // sample data", or a live edit — not just a type switch) can be told
    // apart from one that still matches what's on screen (review fix: this
    // used to only guard against a type switch, so loading sample data
    // while a Validate request was in flight could attach the OLD
    // response's report to the NEW sample text and misreport it as
    // already-validated).
    const requestKey = importSnapshotKey(type);
    importState.committed = null;
    const res = await post("/api/import/dry-run", body);
    if (importState.type !== type.key || importSnapshotKey(type) !== requestKey) return;
    toast = "";
    importState.report = res;
    importState.validatedKey = (res && !res.error) ? requestKey : null;
    await render();
  };
  if (importCommitBtn) importCommitBtn.onclick = async () => {
    const type = importType();
    // Belt-and-suspenders: the button is already `disabled` unless this
    // holds, but re-check at click time too, against the LIVE DOM (not a
    // cache) — a disabled button can still be reached via assistive tech,
    // and this is what actually stops a post-Validate edit from being
    // committed silently (review fix).
    const requestKey = importSnapshotKey(type);
    if (requestKey !== importState.validatedKey) {
      toast = "Sheets changed since Validate — please Validate again before committing.";
      return render();
    }
    const body = buildImportBody(type);
    if (type.needsSeason) body.season_id = importState.seasonId;
    const res = await post(type.commitPath, body);
    // Same stale-response guard as Validate above — discard this response
    // if the sheets or the selected type changed while the request was in
    // flight, rather than showing a commit result for content that's no
    // longer what's on screen.
    if (importState.type !== type.key || importSnapshotKey(type) !== requestKey) return;
    toast = "";
    importState.committed = res;
    if (res && res.committed) { importState.report = null; importState.validatedKey = null; }
    await render();
  };
  // Account/session admin (#78): pick an account, revoke one of its sessions.
  c.querySelectorAll("[data-user-sessions]").forEach((b) => b.onclick = () => {
    usersSelected = b.dataset.userSessions; toast = ""; render();
  });
  c.querySelectorAll("[data-revoke-session]").forEach((b) => b.onclick = async () => {
    toast = "";
    await post(`/api/accounts/${usersSelected}/sessions/${b.dataset.revokeSession}/revoke`, {});
    await render();
  });
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
  // Expand/collapse an import batch's row-level detail in the Activity feed (#102).
  c.querySelectorAll("[data-audit-toggle]").forEach((b) => b.onclick = () => {
    const id = b.dataset.auditToggle;
    if (activityExpandedBatches.has(id)) activityExpandedBatches.delete(id);
    else activityExpandedBatches.add(id);
    render();
  });
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
// Sign out ends the server session and returns to the sign-in screen (#71).
const signoutBtn = document.getElementById("signout-btn");
if (signoutBtn) signoutBtn.onclick = async () => {
  await post("/api/auth/logout", {});
  // Remember the explicit sign-out so a refresh does NOT silently re-run the
  // zero-friction demo auto-login — logout must stick until the user signs in.
  try { localStorage.setItem("hs_signed_out", "1"); } catch (_) {}
  setUser(null); toast = "";
  renderRoleSwitch();
  showLogin("You've been signed out.");
};
// Manual sign-in form (#71) — the only way in when the picker is empty.
const loginForm = document.getElementById("login-form");
if (loginForm) loginForm.onsubmit = (e) => {
  e.preventDefault();
  signIn(val("login-user"), document.getElementById("login-pass").value);
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
  // The "My Assignments" tab is only for a signed-in official (#55).
  const isOfficial = !!(currentUser && currentUser.scope && currentUser.scope.official_id);
  toggle('.tab[data-tab="inbox"]', isOfficial);
  // The Delivery admin tab is operator-only (#61).
  toggle('.tab[data-tab="delivery"]', hasPerm("manage_schedule"));
  toggle('.tab[data-tab="users"]', hasPerm("manage_users"));
  toggle('.tab[data-tab="scheduler"]', hasPerm("manage_schedule"));
  // The Import wizard tab mirrors /api/import/dry-run's own gate (#96):
  // manage_arena is the one permission both League Admin and Arena Manager
  // hold, and it's the entry point for all three import types.
  toggle('.tab[data-tab="import"]', hasPerm("manage_arena"));
  // Reset wipes all demo data — operator-only, like the API (hardening).
  toggle("#reset-btn", hasPerm("manage_schedule"));
  // Sign out only makes sense with a live session.
  toggle("#signout-btn", !!currentUser);
}
function setUser(user) {
  currentUser = user;
  currentRole = user ? user.role : "viewer";
  applyRolePerms();
}

// Show/hide the full-screen sign-in overlay (#71). ``body.signed-out`` hides
// the console shell so a signed-out visitor only ever sees the login card.
function showLogin(message) {
  const screen = document.getElementById("login-screen");
  document.body.classList.add("signed-out");
  if (screen) screen.hidden = false;
  const err = document.getElementById("login-error");
  if (err) { err.hidden = !message; err.textContent = message || ""; }
  renderLoginPersonas();
  const u = document.getElementById("login-user");
  if (u) u.focus();
}
function hideLogin() {
  document.body.classList.remove("signed-out");
  const screen = document.getElementById("login-screen");
  if (screen) screen.hidden = true;
}
// Demo mode exposes the seeded personas as one-click sign-ins; production
// returns an empty list, leaving only the manual username/password form.
function renderLoginPersonas() {
  const box = document.getElementById("login-personas");
  if (!box) return;
  if (!accounts.length) { box.innerHTML = ""; return; }
  box.innerHTML = `<div class="login-personas-label">Demo accounts · password "demo"</div>`
    + accounts.map((a) =>
      `<button type="button" class="login-persona" data-persona="${esc(a.username)}">
         <span>${esc(a.username)}</span><span class="lp-role">${esc(a.label)}</span></button>`).join("");
  box.querySelectorAll("[data-persona]").forEach((b) =>
    b.onclick = () => signIn(b.dataset.persona, DEMO_PASSWORD));
}

async function signIn(username, password) {
  const r = await post("/api/auth/login",
    { username, password: password == null ? DEMO_PASSWORD : password });
  if (!r || r.error) {
    showLogin((r && r.error && r.error.message) || "Sign in failed.");
    return false;
  }
  setUser(r.user); toast = "";
  // An explicit sign-in clears the sticky sign-out so the demo auto-login can
  // resume on future fresh visits.
  try { localStorage.removeItem("hs_signed_out"); } catch (_) {}
  drawer = null; movingGameId = null; conflict = null;
  hideLogin();
  renderRoleSwitch(); render();
  return true;
}
function renderRoleSwitch() {
  const sel = document.getElementById("role-switch");
  if (sel && accounts.length) {
    const signedOut = currentUser ? "" :
      `<option value="" selected disabled>Signed out</option>`;
    sel.innerHTML = signedOut + accounts.map((a) =>
      `<option value="${esc(a.username)}" ${currentUser && a.username === currentUser.username ? "selected" : ""}>${esc(a.label)}</option>`).join("");
    // Switching the demo account performs a real server-side sign-in (#50).
    sel.onchange = (e) => signIn(e.target.value);
  }
  // Sidebar user block: avatar initials, display name, role, scope.
  const sc = (currentUser && currentUser.scope) || {};
  const scopeName = sc.player_name || sc.official_name || sc.team_name;
  const av = document.getElementById("user-avatar");
  const nm = document.getElementById("user-name");
  const rl = document.getElementById("user-role");
  const chip = document.getElementById("scope-chip");
  const uname = currentUser ? currentUser.username : "";
  if (av) av.textContent = uname
    ? uname.replace(/[^a-z0-9]/gi, "").slice(0, 2).toUpperCase() : "–";
  if (nm) nm.textContent = currentUser ? uname : "Signed out";
  if (rl) rl.textContent = currentUser ? (currentUser.label || currentRole) : "";
  if (chip) chip.textContent = scopeName ? `· ${scopeName}` : "";
}
// Real deployment posture chips (#72): app mode, store backend, delivery modes.
function renderEnvChips() {
  const box = document.getElementById("env-chips");
  if (!box || !envStatus) return;
  const s = envStatus;
  const storeLabel = { memory: "in-memory", sqlite: "sqlite", postgres: "postgres" }[s.store] || s.store;
  const deliv = (label, mode, liveVal) => {
    const live = mode === liveVal;
    return `<span class="env-chip ${live ? "live" : "subtle"}">${label} ${live ? "live" : "dry-run"}</span>`;
  };
  box.innerHTML =
    (s.app_mode === "production"
      ? `<span class="env-chip prod">Production</span>`
      : `<span class="env-chip">Demo</span>`)
    + `<span class="env-chip subtle">${esc(storeLabel)}</span>`
    + deliv("email", s.email_mode, "smtp")
    + deliv("push", s.push_mode, "live");
}

// Whether the user explicitly signed out on this device — suppresses the
// zero-friction demo auto-login until the next explicit sign-in.
function signedOutSticky() {
  try { return localStorage.getItem("hs_signed_out") === "1"; }
  catch (_) { return false; }
}

async function bootstrap() {
  try {
    const [rolesRes, acctRes, statusRes, meResp] = await Promise.all([
      fetch("/api/auth/roles").then((r) => r.json()),
      fetch("/api/auth/accounts").then((r) => r.json()),
      fetch("/api/status").then((r) => r.json()).catch(() => null),
      fetch("/api/auth/me", { credentials: "same-origin" }),
    ]);
    roleCatalog = rolesRes.roles || [];
    accounts = acctRes.accounts || [];
    envStatus = statusRes && !statusRes.error ? statusRes : null;
    const meRes = await meResp.json();
    if (meRes && meRes.user) {
      setUser(meRes.user);
    } else if (meResp.status !== 401 && accounts.length && !signedOutSticky()) {
      // Demo mode, fresh visit (no session, personas available, and the user
      // has not explicitly signed out): keep the zero-friction auto-login as
      // League Admin.
      const r = await post("/api/auth/login",
        { username: "admin", password: DEMO_PASSWORD });
      if (r && !r.error) setUser(r.user); else setUser(null);
    } else {
      // Production (empty picker), an expired/invalid session (401), or an
      // explicit prior sign-out: no silent login — show the sign-in screen
      // (#71) so logout is meaningful across a refresh.
      setUser(null);
    }
  } catch (_) { roleCatalog = []; accounts = []; setUser(null); }
  applyRolePerms();
  renderRoleSwitch();
  renderEnvChips();
  if (currentUser) { hideLogin(); render(); }
  else { showLogin(); }
}
bootstrap();
