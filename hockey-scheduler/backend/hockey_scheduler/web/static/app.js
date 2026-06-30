/* Hockey Scheduler — calendar-first operator demo.
   Drives the real backend (setup + roster/substitute) via the documented API. */

let view = "dashboard";     // dashboard|setup|calendar|games|roster|activity|public
let gameView = "coach";     // coach | player (roster)
let currentGame = null;     // game id whose roster we're viewing
let pickedPlayer = null;
let wizard = null;          // {slot_id, division_id, home_id, away_id} when scheduling
let calendarDate = "2026-09-05";  // YYYY-MM-DD shown on the arena calendar
let toast = "";

function shiftDate(days) {
  const d = new Date(calendarDate + "T00:00:00Z");
  d.setUTCDate(d.getUTCDate() + days);
  calendarDate = d.toISOString().slice(0, 10);
}
function fmtDate(d) {
  return new Date(d + "T00:00:00Z").toLocaleDateString("en-GB",
    { weekday: "short", day: "numeric", month: "short", year: "numeric", timeZone: "UTC" }).replace(",", "");
}

const NAV = {
  dashboard: "Dashboard", setup: "Setup", calendar: "Arena Calendar",
  games: "Games", roster: "Roster", activity: "Activity", public: "Public",
};
const POS_CLASS = { goalie: "pos-G", defense: "pos-D", forward: "pos-F", skater: "pos-D" };
const REPO = "https://github.com/jingizoo/biknik/issues";

const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (iso) => { const m = /T(\d{2}:\d{2})/.exec(iso || ""); return m ? m[1] : ""; };
const val = (id) => { const e = document.getElementById(id); return e ? e.value.trim() : ""; };

async function getJSON(p) { return (await fetch(p)).json(); }
async function post(p, b) {
  const r = await fetch(p, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b || {}) });
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
function renderSetup(ov) {
  const leagueOpts = ov.leagues.map((l) => opt(l.id, l.name)).join("");
  const seasonOpts = ov.seasons.map((s) => opt(s.id, s.name)).join("");
  const clubOpts = ov.clubs.map((c) => opt(c.id, c.name)).join("");
  const divOpts = ov.divisions.map((d) => opt(d.id, d.name)).join("");
  const venueOpts = ov.venues.map((v) => opt(v.id, v.name)).join("");
  const rinkOpts = ov.rinks.map((r) => opt(r.id, `${r.venue_name || ""} · ${r.name}`)).join("");
  return `
    <div class="section-title">League &amp; season</div>
    <div class="form">
      <label>New league name</label><input id="f-league" placeholder="e.g. Coastal League" />
      <button class="act primary" data-create="league">Create league</button>
    </div>
    <div class="form">
      <label>Season — league</label><select id="f-season-league">${leagueOpts}</select>
      <label>Season name</label><input id="f-season" placeholder="e.g. 2027–28" />
      <button class="act primary" data-create="season">Create season</button>
    </div>
    <div class="form">
      <label>Division — season</label><select id="f-div-season">${seasonOpts}</select>
      <div class="row2">
        <div><label>Division name</label><input id="f-div" placeholder="e.g. U14" /></div>
        <div><label>Age group</label><input id="f-div-age" placeholder="U14" /></div>
      </div>
      <button class="act primary" data-create="division">Create division</button>
    </div>
    <div class="section-title">Clubs &amp; teams</div>
    <div class="form">
      <label>New club name</label><input id="f-club" placeholder="e.g. Eagles HC" />
      <button class="act primary" data-create="club">Create club</button>
    </div>
    <div class="form">
      <div class="row2">
        <div><label>Team — club</label><select id="f-team-club">${clubOpts}</select></div>
        <div><label>Division</label><select id="f-team-div">${divOpts}</select></div>
      </div>
      <label>Team name</label><input id="f-team" placeholder="e.g. U14 Eagles" />
      <button class="act primary" data-create="team">Create team</button>
    </div>
    <div class="section-title">Arena</div>
    <div class="form">
      <label>New venue name</label><input id="f-venue" placeholder="e.g. South Arena" />
      <button class="act primary" data-create="venue">Create venue</button>
    </div>
    <div class="form">
      <label>Rink — venue</label><select id="f-rink-venue">${venueOpts}</select>
      <label>Rink name</label><input id="f-rink" placeholder="e.g. Rink 3" />
      <button class="act primary" data-create="rink">Create rink</button>
    </div>
    <div class="form">
      <label>Ice slot — rink</label><select id="f-slot-rink">${rinkOpts}</select>
      <label>Date</label><input id="f-slot-date" type="date" value="2026-09-05" />
      <div class="row2">
        <div><label>Start</label><input id="f-slot-start" type="time" value="21:00" /></div>
        <div><label>End</label><input id="f-slot-end" type="time" value="22:30" /></div>
      </div>
      <label>Type</label>
      <select id="f-slot-type">
        <option value="game">Game</option>
        <option value="practice">Practice</option>
        <option value="public_skate">Public skate</option>
        <option value="maintenance">Maintenance</option>
        <option value="tournament">Tournament</option>
      </select>
      <button class="act primary" data-create="ice-slot">Add ice slot</button>
    </div>
    ${setupList("Leagues", ov.leagues.map((l) => l.name))}
    ${setupList("Seasons", ov.seasons.map((s) => s.name))}
    ${setupList("Divisions", ov.divisions.map((d) => d.name + (d.is_junior ? " · Junior" : "")))}
    ${setupList("Clubs", ov.clubs.map((c) => c.name))}
    ${setupList("Teams", ov.teams.map((t) => `${t.name} — ${t.division_name || ""}`))}
    ${setupList("Venues", ov.venues.map((v) => v.name))}
    ${setupList("Rinks", ov.rinks.map((r) => `${r.name} — ${r.venue_name || ""}`))}
    ${toastHtml()}`;
}

function setupList(title, items) {
  const rows = items.length
    ? items.map((x) => `<div class="li"><div class="li-main"><div class="li-title">${esc(x)}</div></div>
        <button class="act ghost" disabled title="Edit/delete is a follow-up">⋯</button></div>`).join("")
    : `<div class="empty">None yet.</div>`;
  return `<div class="section-title">${title} (${items.length})</div><div class="card">${rows}</div>`;
}

/* ---------- Arena Calendar ---------- */
function renderCalendar(ov) {
  if (wizard) return renderWizard(ov) + toastHtml();
  const slotsByRink = {};
  ov.rinks.forEach((r) => (slotsByRink[r.id] = []));
  // Only show ice for the selected calendar date.
  ov.ice_slots
    .filter((s) => (s.start_time || "").startsWith(calendarDate))
    .forEach((s) => { (slotsByRink[s.rink_id] ||= []).push(s); });
  const label = (s) => {
    if (s.game_label) return esc(s.game_label);
    if (s.status === "available") return "Available · Schedule";
    if (s.slot_type === "maintenance") return "Blocked · Maintenance";
    if (s.slot_type === "public_skate") return "Blocked · Public skate";
    if (s.slot_type === "practice") return "Blocked · Practice";
    if (s.slot_type === "tournament") return "Blocked · Tournament";
    return s.status;
  };
  const rows = ov.rinks.map((r) => {
    const cards = (slotsByRink[r.id] || []).map((s) => {
      const cls = s.status === "available" ? "available" : s.slot_type === "maintenance" ? "maintenance" : s.status;
      // Available game ice is a click-target (schedule) and a drop-target (move).
      const attr = s.status === "available" ? `data-slot="${s.id}" data-drop="${s.id}"` : "";
      // Allocated game cards are draggable to move the game to another slot.
      const drag = s.game_id ? `draggable="true" data-game="${s.game_id}"` : "";
      const cta = s.game_label ? " · drag to move" : "";
      return `<div class="slot-card ${cls}" ${attr} ${drag}><div class="t">${fmt(s.start_time)}–${fmt(s.end_time)}</div><div class="s">${label(s)}${cta}</div></div>`;
    }).join("") || `<div class="slot-card"><div class="s">No ice</div></div>`;
    return `<div class="cal-row"><div class="cal-rink">${esc(r.name)}</div>
      <div class="cal-slots">${cards}
        <div class="slot-card available" data-addslot="${r.id}"><div class="t">＋</div><div class="s">Add ice</div></div></div></div>`;
  }).join("");
  const drafts = ov.schedule.filter((g) => !g.published);
  const tray = drafts.length ? `<div class="tray"><span class="tray-label">Draft games</span>
    ${drafts.map((g) => `<span class="chip-drag" draggable="true" data-game="${g.game_id}">⠿ ${esc(g.home_team_name)} vs ${esc(g.away_team_name)} · ${fmt(g.start_time)}</span>`).join("")}</div>` : "";
  return `
    <div class="cal-head">
      <div><div class="cal-date">${esc(fmtDate(calendarDate))}</div>
        <div class="cal-venue">${esc((ov.venues[0] || {}).name || "Arena")}</div></div>
      <div class="cal-nav"><button class="act ghost" data-cal="-1">‹</button>
        <button class="act ghost" data-cal="0">Today</button>
        <button class="act ghost" data-cal="1">›</button></div>
    </div>
    <div class="legend">
      <span><i class="dot lg-game"></i>Available</span>
      <span><i class="dot lg-alloc"></i>Allocated</span>
      <span><i class="dot lg-maint"></i>Maintenance</span>
      <span><i class="dot lg-skate"></i>Public skate</span>
    </div>
    ${tray}
    ${rows}
    <div class="privacy-note">📅 Tap an <strong>Available</strong> slot to schedule, or
      <strong>drag</strong> a game onto available ice to move it (validated server-side).
      Moving changes the time/rink, so a published fixture is unpublished and a locked
      roster is unlocked for reconfirmation. This board is the source of truth (#33).</div>
    ${toastHtml()}`;
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
function renderRoster(board) {
  if (!board) return `<div class="empty">Select a game from the Games tab.</div>`;
  return `<div class="segmented">
      <button class="seg ${gameView === "coach" ? "active" : ""}" data-view="coach">Coach</button>
      <button class="seg ${gameView === "player" ? "active" : ""}" data-view="player">Player</button>
    </div><div style="padding-top:8px">${gameView === "coach" ? coachBody(board) : playerBody(board)}</div>`;
}
function coachBody(board) {
  const s = board.status;
  const selected = board.players.filter((p) => p.group === "selected");
  const subs = board.players.filter((p) => p.group === "substitute");
  const locked = s.status === "locked";

  // Newly-scheduled game: no roster selected yet.
  if (!selected.length && !subs.length) {
    if (!board.players.length) {
      return `<div class="banner neutral"><h2>No roster yet</h2>
        <p>The home team has no players. Add or import players first (player
        management is a follow-up: #25).</p></div>${toastHtml()}`;
    }
    return `<div class="banner neutral"><h2>No roster selected yet</h2>
      <p>${board.players.length} eligible players on the home team. For the demo
      you can auto-fill a draft roster; production will offer Select / Import /
      Copy-from-previous (#25).</p></div>
      <div class="actions"><button class="act primary" data-act="build">Auto-fill demo roster</button></div>
      ${toastHtml()}`;
  }
  const openFor = (st) => st === "goalie" ? s.open_goalie_slots > 0 : s.open_skater_slots > 0;
  const selRows = selected.map((p) => {
    let btn = "";
    if (!locked) {
      if (p.backed_out) btn = `<button class="act success" data-act="confirm" data-id="${p.id}">Re-confirm</button>`;
      else if (p.availability === "available") btn = `<button class="act danger" data-act="backout" data-id="${p.id}">Can't play</button>`;
      else btn = `<button class="act ghost" data-act="confirm" data-id="${p.id}">Confirm</button>`;
    }
    return `<div class="row">${avatar(p)}<span class="name">${esc(p.name)}</span>${statusBadge(p)}${btn}</div>`;
  }).join("");
  const subRows = subs.length ? subs.map((p) => {
    const canAdd = !locked && openFor(p.slot_type);
    const ctrl = p.sub_status === "offered" ? '<span class="badge orange">Offered</span>' : '<span class="badge blue">Enrolled</span>';
    const btn = locked ? "" : canAdd ? `<button class="act primary" data-act="add" data-id="${p.id}">Add</button>`
      : `<button class="act ghost" disabled>No slot</button>`;
    return `<div class="row">${avatar(p)}<span class="name">${esc(p.name)}</span>${ctrl}${btn}</div>`;
  }).join("") : `<div class="empty">No substitutes enrolled.</div>`;
  return `
    <div class="banner ${bannerClass(s.status)}"><h2>${prettyStatus(s.status)}</h2><p>${esc(s.message)}</p></div>
    <div class="card">${slotBar("Goalies", s.confirmed_goalies, s.target_goalies, s.open_goalie_slots)}
      ${slotBar("Skaters", s.confirmed_skaters, s.target_skaters, s.open_skater_slots)}</div>
    <div class="roster-cols">
      <div><div class="section-title">Selected Players</div>
        <div class="card">${selRows || '<div class="empty">No players selected.</div>'}</div></div>
      <div><div class="section-title">Substitute Pool</div>
        <div class="card">${subRows}</div></div>
    </div>
    ${locked ? `<div class="locked-note">🔒 Roster locked. Player actions disabled.
        <button class="act ghost" data-act="unlock" style="margin-left:auto">Unlock</button></div>`
      : `<div class="actions"><button class="act ghost" data-act="lock">Lock Roster</button></div>`}
    ${toastHtml()}`;
}
function playerBody(board) {
  const players = board.players;
  const locked = board.status.status === "locked";
  if (!pickedPlayer || !players.find((p) => p.id === pickedPlayer)) pickedPlayer = players[0] ? players[0].id : null;
  const options = players.map((p) => opt(p.id, `${p.name} · ${p.position}`, p.id === pickedPlayer)).join("");
  const p = players.find((x) => x.id === pickedPlayer);
  const acts = (html) => locked ? `<div class="locked-note">🔒 Roster locked — actions disabled.</div>` : `<div class="actions">${html}</div>`;
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
async function createEntity(kind) {
  toast = "";
  const map = {
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
  const res = await map[kind]();
  if (res && !res.error) toast = `${kind === "ice-slot" ? "Ice slot" : kind[0].toUpperCase() + kind.slice(1)} created.`;
  await render();
}

async function rosterAction(act, id) {
  toast = "";
  const B = `/api/games/${currentGame}`;
  if (act === "build") await post(`${B}/build-roster`, {});
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
  let ov, board;
  try {
    c.innerHTML = `<div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div>`;
    ov = await getJSON("/api/demo/overview");
    if (ov && ov.error) throw new Error(ov.error.message);
    if (!currentGame && ov.schedule[0]) currentGame = ov.schedule[0].game_id;
    const needsBoard = ["roster", "activity"].includes(view) || view === "dashboard";
    board = (needsBoard && currentGame) ? await getJSON(`/api/games/${currentGame}/board`) : null;
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
    : view === "roster" ? renderRoster(board)
    : view === "activity" ? renderActivity(board, ov)
    : renderPublic(ov);

  c.querySelectorAll("button[data-goto]").forEach((b) => b.onclick = () => switchTab(b.dataset.goto));
  c.querySelectorAll("button[data-create]").forEach((b) => b.onclick = () => createEntity(b.dataset.create));
  c.querySelectorAll("button[data-act]").forEach((b) => b.onclick = () => rosterAction(b.dataset.act, b.dataset.id));
  c.querySelectorAll(".seg").forEach((b) => b.onclick = () => { gameView = b.dataset.view; toast = ""; render(); });
  c.querySelectorAll("[data-slot]").forEach((b) => b.onclick = () => { wizard = { slot_id: b.dataset.slot }; toast = ""; render(); });
  c.querySelectorAll("[data-addslot]").forEach((b) => b.onclick = async () => { await post("/api/demo/add-ice-slot", { rink_id: b.dataset.addslot, date: calendarDate }); await render(); });
  c.querySelectorAll("[data-cal]").forEach((b) => b.onclick = () => { const v = +b.dataset.cal; if (v === 0) calendarDate = "2026-09-05"; else shiftDate(v); toast = ""; render(); });
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
      toast = "";
      const res = await post(`/api/games/${gid}/move`, { ice_slot_id: el.dataset.drop, reason: "Moved on arena calendar" });
      if (res && !res.error) toast = "Game moved.";
      await render();
    });
  });
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
  view = next; toast = ""; if (next !== "calendar") wizard = null;
  document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x.dataset.tab === next));
  render();
}
document.querySelectorAll(".tab").forEach((b) => b.onclick = () => switchTab(b.dataset.tab));
// Topbar command actions (web shell) — outside #content, wired once.
document.querySelectorAll(".topbar [data-goto]").forEach((b) => b.onclick = () => switchTab(b.dataset.goto));
document.getElementById("reset-btn").onclick = async () => { await post("/api/reset", {}); toast = ""; currentGame = null; pickedPlayer = null; wizard = null; render(); };
render();
