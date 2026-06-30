/* Hockey Scheduler — Full E2E browser demo.
   Drives the real backend (setup + roster/substitute) via the documented API. */

let tab = "today";          // today|league|arena|schedule|game|activity|public
let gameView = "coach";     // coach | player
let pickedPlayer = null;
let toast = "";
let GAME = "game_1";        // resolved from the overview on first load

const NAV_TITLE = {
  today: "Today", league: "League", arena: "Arena", schedule: "Schedule",
  game: "Game Detail", activity: "Activity", public: "Public",
};
const POS_CLASS = { goalie: "pos-G", defense: "pos-D", forward: "pos-F", skater: "pos-D" };
const REPO = "https://github.com/jingizoo/biknik/issues";

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}
function fmtTime(iso) {
  // "2026-09-05T18:30:00+00:00" → "18:30"
  const m = /T(\d{2}:\d{2})/.exec(iso || "");
  return m ? m[1] : "";
}

async function getJSON(path) { return (await fetch(path)).json(); }
async function post(path, body) {
  const r = await fetch(path, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const data = await r.json();
  if (data && data.error) toast = data.error.message;
  return data;
}

/* ---------------- shared ---------------- */
function bannerClass(s) {
  if (s === "open_slot") return "alert";
  if (s === "needs_substitute") return "warn";
  if (s === "roster_confirmed") return "ok";
  return "neutral";
}
function prettyStatus(s) {
  return {
    draft: "Draft", selected: "Selected", awaiting_responses: "Awaiting Responses",
    roster_confirmed: "Roster Confirmed", needs_substitute: "Needs Substitute Decision",
    open_slot: "Open Slot", locked: "Roster Locked", final: "Final",
  }[s] || s;
}
function statusBadge(p) {
  if (p.backed_out) return `<span class="badge red">Backed out</span>`;
  if (p.availability === "available" || p.roster_status === "confirmed" || p.roster_status === "accepted")
    return `<span class="badge green">Confirmed</span>`;
  if (p.availability === "unavailable") return `<span class="badge red">Unavailable</span>`;
  return `<span class="badge gray">Pending</span>`;
}
function avatar(p) {
  const initials = esc(p.name).split(" ").map((x) => x[0]).slice(0, 2).join("");
  return `<div class="avatar ${POS_CLASS[p.position]}">${initials}</div>`;
}
function slotBar(label, confirmed, target, open) {
  const pct = target ? Math.round(((target - open) / target) * 100) : 0;
  return `<div class="slot"><div class="slot-top"><span class="label">${label}</span>
    <span class="count">${confirmed}/${target} confirmed${open ? ` · ${open} open` : ""}</span></div>
    <div class="bar ${open ? "open" : ""}"><span style="width:${pct}%"></span></div></div>`;
}
function stub(icon, title, sub, issue) {
  return `<div class="stub"><span class="stub-ico">${icon}</span>
    <div class="stub-main"><div class="stub-title">${title}</div><div class="stub-sub">${sub}</div></div>
    <a class="badge" href="${REPO}/${issue}" target="_blank">#${issue}</a></div>`;
}
function toastHtml() { return toast ? `<div class="toast">${esc(toast)}</div>` : ""; }

/* ---------------- Today ---------------- */
function renderToday(ov, board) {
  const s = board.status;
  const g = ov.schedule[0] || {};
  const actions = [];
  if (s.open_goalie_slots) actions.push(`${s.open_goalie_slots} open goalie slot`);
  if (s.open_skater_slots) actions.push(`${s.open_skater_slots} open skater slot`);
  if (s.substitutes_enrolled) actions.push(`${s.substitutes_enrolled} substitute available`);
  if (s.status !== "locked") actions.push("Roster lock pending");

  return `
    <div class="hero">
      <div class="when">Next game · Sat · ${fmtTime(g.start_time)}</div>
      <h2>${esc(g.home_team_name)} vs ${esc(g.away_team_name)}</h2>
      <div class="where">${esc(g.venue_name || "")} · ${esc(g.rink_name || "")}</div>
      <span class="chip">${prettyStatus(s.status)}</span>
    </div>
    <div class="banner ${bannerClass(s.status)}"><h2>${s.action_required ? "Action needed" : "You're all set"}</h2>
      <p>${esc(s.message)}</p></div>
    <div class="section-title">Action items</div>
    <div class="card">${actions.map((a) => `<div class="row"><span class="name">${esc(a)}</span></div>`).join("")
      || '<div class="empty">Nothing needs attention.</div>'}</div>
    <div class="actions"><button class="act primary" data-goto="game">Open Game Detail</button></div>
    <div class="section-title">Follow-up modules</div>
    <div class="card">
      ${stub("🦓", "Officials assignment", "Assign referees & linespersons", 30)}
      ${stub("📊", "Results & standings", "Scores, standings, playoffs", 31)}
      ${stub("📨", "Notification delivery", "Push / email delivery worker", 32)}
      ${stub("📆", "Calendar feeds", "ICS per team / player", 33)}
    </div>
    ${toastHtml()}`;
}

/* ---------------- League ---------------- */
function renderLeague(ov) {
  const lg = ov.league || {};
  const season = ov.seasons[0] || {};
  const teamsByDiv = {};
  ov.teams.forEach((t) => { (teamsByDiv[t.division_name] ||= []).push(t); });
  return `
    <div class="section-title">League</div>
    <div class="card">
      <div class="kv"><span class="k">League</span><span class="v">${esc(lg.name)}</span></div>
      <div class="kv"><span class="k">Country</span><span class="v">${esc(lg.country || "—")}</span></div>
      <div class="kv"><span class="k">Season</span><span class="v">${esc(season.name || "—")}</span></div>
    </div>
    <div class="section-title">Divisions</div>
    <div class="card">${ov.divisions.map((d) => `<div class="li"><div class="li-main">
      <div class="li-title">${esc(d.name)}</div></div>
      ${d.is_junior ? '<span class="pill junior">Junior</span>' : ""}</div>`).join("")}</div>
    <div class="section-title">Clubs</div>
    <div class="card">${ov.clubs.map((c) => `<div class="li"><div class="li-main">
      <div class="li-title">${esc(c.name)}</div></div></div>`).join("")}</div>
    <div class="section-title">Teams</div>
    <div class="card">${ov.teams.map((t) => `<div class="li"><div class="li-main">
      <div class="li-title">${esc(t.name)}</div>
      <div class="li-sub">${esc(t.club_name || "")} · ${esc(t.division_name || "")}</div></div></div>`).join("")}</div>
    <div class="section-title">Setup actions (via API · ${ov.setup_audit_count} audited)</div>
    <div class="card">
      <div class="stub"><span class="stub-ico">➕</span><div class="stub-main">
        <div class="stub-title">Create league / season / division / club / team</div>
        <div class="stub-sub">Seeded here; live via the setup API &amp; setup_demo</div></div>
        <span class="badge">#22</span></div>
    </div>
    ${toastHtml()}`;
}

/* ---------------- Arena ---------------- */
function renderArena(ov) {
  const venue = ov.venues[0] || {};
  return `
    <div class="section-title">Venue</div>
    <div class="card">
      <div class="kv"><span class="k">Venue</span><span class="v">${esc(venue.name)}</span></div>
      <div class="kv"><span class="k">Address</span><span class="v">${esc(venue.address || "—")}</span></div>
    </div>
    <div class="section-title">Rinks</div>
    <div class="card">${ov.rinks.map((r) => `<div class="li"><div class="li-main">
      <div class="li-title">${esc(r.name)}</div><div class="li-sub">${esc(r.venue_name || "")}</div></div></div>`).join("")}</div>
    <div class="section-title">Ice slots</div>
    <div class="card">${ov.ice_slots.map((s) => `<div class="li">
      <span class="li-time">${fmtTime(s.start_time)}–${fmtTime(s.end_time)}</span>
      <div class="li-main"><div class="li-title">${esc(s.rink_name)}</div>
        ${s.game_label ? `<div class="li-sub">${esc(s.game_label)}</div>` : ""}</div>
      <span class="pill ${s.status}">${s.status}</span></div>`).join("")}</div>
    <div class="actions"><button class="act ghost" data-act="add-slot">+ Add ice slot (Main Rink)</button></div>
    ${toastHtml()}`;
}

/* ---------------- Schedule ---------------- */
function renderSchedule(ov) {
  const rows = ov.schedule.map((g) => `<div class="li">
    <span class="li-time">${fmtTime(g.start_time)}</span>
    <div class="li-main"><div class="li-title">${esc(g.home_team_name)} vs ${esc(g.away_team_name)}</div>
      <div class="li-sub">${esc(g.division_name || "")} · ${esc(g.rink_name || "")} · ${g.published ? "Published" : "Draft"}</div></div>
    <button class="act ghost" data-goto="game">Open</button></div>`).join("");
  return `
    <div class="section-title">Saturday</div>
    <div class="card">${rows || '<div class="empty">No games scheduled.</div>'}</div>
    <div class="section-title">Automation</div>
    <div class="card">${stub("⚙️", "Fixture generator", "Auto round-robin + constraints", 28)}
      ${stub("🔁", "Reschedule workflow", "Request → approve → republish", 29)}</div>
    ${toastHtml()}`;
}

/* ---------------- Game (Coach/Player) ---------------- */
function renderGame(board) {
  return `<div class="segmented">
      <button class="seg ${gameView === "coach" ? "active" : ""}" data-view="coach">Coach</button>
      <button class="seg ${gameView === "player" ? "active" : ""}" data-view="player">Player</button>
    </div>
    <div style="padding-top:8px">${gameView === "coach" ? coachBody(board) : playerBody(board)}</div>`;
}
function coachBody(board) {
  const s = board.status;
  const selected = board.players.filter((p) => p.group === "selected");
  const subs = board.players.filter((p) => p.group === "substitute");
  const locked = s.status === "locked";
  const selectedRows = selected.map((p) => {
    let btn = "";
    if (!locked) {
      if (p.backed_out) btn = `<button class="act success" data-act="confirm" data-id="${p.id}">Re-confirm</button>`;
      else if (p.availability === "available") btn = `<button class="act danger" data-act="backout" data-id="${p.id}">Can't play</button>`;
      else btn = `<button class="act ghost" data-act="confirm" data-id="${p.id}">Confirm</button>`;
    }
    return `<div class="row">${avatar(p)}<span class="name">${esc(p.name)}</span>${statusBadge(p)}${btn}</div>`;
  }).join("");
  const openFor = (slotType) =>
    slotType === "goalie" ? s.open_goalie_slots > 0 : s.open_skater_slots > 0;
  const subRows = subs.length
    ? subs.map((p) => {
        const canAdd = !locked && openFor(p.slot_type);
        const ctrl = p.sub_status === "offered"
          ? '<span class="badge orange">Offered</span>'
          : '<span class="badge blue">Enrolled</span>';
        const btn = locked ? ""
          : canAdd ? `<button class="act primary" data-act="add" data-id="${p.id}">Add</button>`
          : `<button class="act ghost" disabled title="No matching open ${esc(p.slot_type)} slot">No slot</button>`;
        return `<div class="row">${avatar(p)}<span class="name">${esc(p.name)}</span>${ctrl}${btn}</div>`;
      }).join("")
    : `<div class="empty">No substitutes enrolled.</div>`;
  return `
    <div class="banner ${bannerClass(s.status)}"><h2>${prettyStatus(s.status)}</h2><p>${esc(s.message)}</p></div>
    <div class="game-head"><h2>U16 Lions vs U16 Falcons</h2><div class="sub">Sat 18:30 · Nord Arena · Main Rink</div></div>
    <div class="card">${slotBar("Goalies", s.confirmed_goalies, s.target_goalies, s.open_goalie_slots)}
      ${slotBar("Skaters", s.confirmed_skaters, s.target_skaters, s.open_skater_slots)}</div>
    <div class="section-title">Selected Players</div>
    <div class="card">${selectedRows || '<div class="empty">No players selected.</div>'}</div>
    <div class="section-title">Substitute Pool</div>
    <div class="card">${subRows}</div>
    ${locked
      ? `<div class="locked-note">🔒 Roster is locked. Player actions are disabled.
           <button class="act ghost" data-act="unlock" style="margin-left:auto">Unlock</button></div>`
      : `<div class="actions"><button class="act ghost" data-act="lock">Lock Roster</button></div>`}
    <div class="section-title">Officials</div>
    <div class="card">${stub("🦓", "Referee & linespersons", "Assignment + conflict checks", 30)}</div>
    ${toastHtml()}`;
}
function playerBody(board) {
  const players = board.players;
  const locked = board.status.status === "locked";
  if (!pickedPlayer || !players.find((p) => p.id === pickedPlayer)) pickedPlayer = players[0] ? players[0].id : null;
  const options = players.map((p) =>
    `<option value="${p.id}" ${p.id === pickedPlayer ? "selected" : ""}>${esc(p.name)} · ${esc(p.position)}</option>`).join("");
  const p = players.find((x) => x.id === pickedPlayer);
  const acts = (html) => locked
    ? `<div class="locked-note">🔒 Roster is locked — actions disabled until the coach unlocks.</div>`
    : `<div class="actions">${html}</div>`;
  let card = `<div class="empty">No players.</div>`;
  if (p) {
    if (p.group === "selected" && !p.backed_out)
      card = `<div class="banner ok"><h2>You are selected for this game</h2><p>Status: ${statusText(p)}</p></div>
        ${acts(`<button class="act success" data-act="confirm" data-id="${p.id}">I'm Available</button>
                <button class="act danger" data-act="backout" data-id="${p.id}">I Can't Play</button>`)}`;
    else if (p.group === "selected" && p.backed_out)
      card = `<div class="banner alert"><h2>You marked yourself unavailable</h2><p>The coach has been notified.</p></div>
        ${acts(`<button class="act success" data-act="confirm" data-id="${p.id}">I'm Available again</button>`)}`;
    else if (p.group === "substitute" && p.sub_status === "offered")
      card = `<div class="banner warn"><h2>A game slot is available</h2><p>You've been offered a spot. Accept?</p></div>
        ${acts(`<button class="act success" data-act="accept" data-id="${p.id}">Accept</button>
                <button class="act danger" data-act="decline" data-id="${p.id}">Decline</button>`)}`;
    else if (p.group === "substitute")
      card = `<div class="banner neutral"><h2>You are enrolled as a substitute</h2><p>Waiting for a slot to open.</p></div>
        ${acts(`<button class="act ghost" data-act="withdraw" data-id="${p.id}">Withdraw</button>`)}`;
    else
      card = `<div class="banner neutral"><h2>You are not selected for this game</h2><p>Substitute status: Not enrolled.</p></div>
        ${acts(`<button class="act primary" data-act="enroll" data-id="${p.id}">Enroll as Substitute</button>`)}`;
  }
  return `<div class="section-title">View as player</div>
    <select class="player-picker" id="player-picker">${options}</select>${card}
    <div class="privacy-note">👪 Guardians can respond for junior players — workflow tracked in
      <a href="${REPO}/26" target="_blank">#26</a>.</div>${toastHtml()}`;
}
function statusText(p) {
  if (p.roster_status === "accepted") return "Confirmed (substitute)";
  if (p.availability === "available" || p.roster_status === "confirmed") return "Confirmed";
  return "Awaiting your response";
}

/* ---------------- Activity ---------------- */
const AUDIT_LABEL = {
  roster_selected: "Roster selected", availability_set: "Availability updated",
  player_backed_out: "Player backed out", substitute_enrolled: "Substitute enrolled",
  substitute_withdrawn: "Substitute withdrawn", substitute_offered: "Substitute offered",
  substitute_accepted: "Substitute accepted", substitute_declined: "Substitute declined",
  substitute_added_to_roster: "Substitute added to roster", player_removed: "Player removed",
  roster_locked: "Roster locked", roster_unlocked: "Roster unlocked", game_cancelled: "Game cancelled",
};
function renderActivity(board) {
  const names = {};
  board.players.forEach((p) => (names[p.id] = p.name));
  const notifs = [...(board.notifications || [])].reverse();
  const audit = [...(board.audit || [])].reverse();
  const feed = notifs.length
    ? notifs.map((n) => `<div class="feed-item"><div class="feed-dot ${esc(n.audience)}"></div>
        <div class="feed-body"><div class="msg">${esc(n.message)}</div>
          <div class="who">to ${esc(n.audience)}${n.subject_player_id && names[n.subject_player_id] ? " · " + esc(names[n.subject_player_id]) : ""}</div></div></div>`).join("")
    : `<div class="empty">No notifications yet.</div>`;
  const auditRows = audit.length
    ? audit.map((a) => `<div class="audit-line"><span class="a-action">${esc(AUDIT_LABEL[a.action] || a.action)}</span>${a.subject_player_id && names[a.subject_player_id] ? " — " + esc(names[a.subject_player_id]) : ""}</div>`).join("")
    : `<div class="empty">No audit entries.</div>`;
  return `<div class="section-title">Notifications</div><div class="card">${feed}</div>
    <div class="section-title">Audit trail (${board.audit_count})</div><div class="card">${auditRows}</div>
    <div class="section-title">Delivery</div><div class="card">${stub("📨", "Push / email delivery", "Worker + device tokens", 32)}</div>`;
}

/* ---------------- Public ---------------- */
function renderPublic(ov) {
  const lg = ov.league || {};
  const rows = ov.public_fixtures.map((f) => `<div class="li">
    <span class="li-time">${fmtTime(f.start_time)}</span>
    <div class="li-main"><div class="li-title">${esc(f.home_team_name)} vs ${esc(f.away_team_name)}</div>
      <div class="li-sub">${esc(f.division_name || "")} · ${esc(f.venue_name || "")} · ${esc(f.rink_name || "")}</div></div>
    <span class="pill scheduled">${esc(f.status)}</span></div>`).join("");
  return `
    <div class="hero"><div class="when">Public fixtures</div><h2>${esc(lg.name)}</h2>
      <div class="where">${esc((ov.seasons[0] || {}).name || "")}</div></div>
    <div class="section-title">Fixtures</div>
    <div class="card">${rows || '<div class="empty">No fixtures.</div>'}</div>
    <div class="privacy-note">🔒 Public view shows fixtures only. Junior player names and all
      personal/guardian/medical data are never exposed (policy: #35).</div>
    <div class="section-title">Coming to the portal</div>
    <div class="card">${stub("📊", "Standings & results", "Public table + game centre", 34)}
      ${stub("📆", "Calendar subscription", "ICS feeds", 33)}</div>`;
}

/* ---------------- actions ---------------- */
async function handleAction(act, id) {
  toast = "";
  const B = `/api/games/${GAME}`;
  if (act === "confirm") await post(`${B}/availability`, { player_id: id, availability_status: "available" });
  else if (act === "backout") await post(`${B}/availability`, { player_id: id, availability_status: "unavailable" });
  else if (act === "enroll") await post(`${B}/substitutes/enroll`, { player_id: id });
  else if (act === "withdraw") await post(`${B}/substitutes/withdraw`, { player_id: id });
  else if (act === "add") await post(`${B}/substitutes/${id}/add-to-roster`, {});
  else if (act === "accept") await post(`${B}/substitutes/${id}/accept`, {});
  else if (act === "decline") await post(`${B}/substitutes/${id}/decline`, {});
  else if (act === "lock") await post(`${B}/roster/lock`, {});
  else if (act === "unlock") await post(`${B}/roster/unlock`, {});
  else if (act === "add-slot") await post(`/api/demo/add-ice-slot`, {});
  await render();
}

/* ---------------- render & wiring ---------------- */
async function render() {
  const ov = await getJSON("/api/demo/overview");
  if (ov && ov.schedule && ov.schedule[0]) GAME = ov.schedule[0].game_id;
  const needsBoard = ["today", "game", "activity"].includes(tab);
  const board = needsBoard ? await getJSON(`/api/games/${GAME}/board`) : null;

  document.getElementById("nav-title").textContent = NAV_TITLE[tab];
  const content = document.getElementById("content");
  content.innerHTML =
    tab === "today" ? renderToday(ov, board)
    : tab === "league" ? renderLeague(ov)
    : tab === "arena" ? renderArena(ov)
    : tab === "schedule" ? renderSchedule(ov)
    : tab === "game" ? renderGame(board)
    : tab === "activity" ? renderActivity(board)
    : renderPublic(ov);

  content.querySelectorAll("button[data-act]").forEach((b) =>
    b.addEventListener("click", () => handleAction(b.dataset.act, b.dataset.id)));
  content.querySelectorAll("button[data-goto]").forEach((b) =>
    b.addEventListener("click", () => switchTab(b.dataset.goto)));
  content.querySelectorAll(".seg").forEach((b) =>
    b.addEventListener("click", () => { gameView = b.dataset.view; toast = ""; render(); }));
  const picker = document.getElementById("player-picker");
  if (picker) picker.addEventListener("change", (e) => { pickedPlayer = e.target.value; toast = ""; render(); });
}

function switchTab(next) {
  tab = next; toast = "";
  document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x.dataset.tab === next));
  render();
}

document.querySelectorAll(".tab").forEach((b) =>
  b.addEventListener("click", () => switchTab(b.dataset.tab)));
document.getElementById("reset-btn").addEventListener("click", async () => {
  await post("/api/reset", {}); toast = ""; pickedPlayer = null; render();
});

render();
