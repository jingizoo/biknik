/* Hockey Scheduler — iPhone demo frontend.
   Talks to the real backend via the documented API endpoints. */

const GAME = "game_1"; // seed always creates this id
const BASE = `/api/games/${GAME}`;

let tab = "today";          // today | game | activity
let gameView = "coach";     // coach | player
let pickedPlayer = null;
let toast = "";

const POS_LABEL = { goalie: "G", defense: "D", forward: "F", skater: "S" };
const POS_CLASS = { goalie: "pos-G", defense: "pos-D", forward: "pos-F", skater: "pos-D" };
const NAV_TITLE = { today: "Today", game: "Game Detail", activity: "Activity" };

// Escape backend-provided text before interpolating into innerHTML.
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

async function getBoard() {
  const r = await fetch(`${BASE}/board`);
  return r.json();
}

async function post(path, body) {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const data = await r.json();
  if (data && data.error) toast = data.error.message;
  return data;
}

/* ---------------- shared helpers ---------------- */
function bannerClass(status) {
  if (status === "open_slot") return "alert";
  if (status === "needs_substitute") return "warn";
  if (status === "roster_confirmed") return "ok";
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

function gameTitle(board) {
  return board.game.id === GAME ? "U16 Lions vs Falcons" : esc(board.game.id);
}

function slotBar(label, confirmed, target, open) {
  const occupied = target - open;
  const pct = target ? Math.round((occupied / target) * 100) : 0;
  return `<div class="slot">
    <div class="slot-top"><span class="label">${label}</span>
      <span class="count">${confirmed}/${target} confirmed${open ? ` · ${open} open` : ""}</span></div>
    <div class="bar ${open ? "open" : ""}"><span style="width:${pct}%"></span></div>
  </div>`;
}

/* ---------------- Today tab ---------------- */
function renderToday(board) {
  const s = board.status;
  return `
    <div class="hero">
      <div class="when">Sat · 18:30</div>
      <h2>${gameTitle(board)}</h2>
      <div class="where">${esc(board.game.rink || "Rink 2")} · U16 Division</div>
      <span class="chip">${prettyStatus(s.status)}</span>
    </div>

    <div class="banner ${bannerClass(s.status)}">
      <h2>${s.action_required ? "Action needed" : "You're all set"}</h2>
      <p>${esc(s.message)}</p>
    </div>

    <div class="section-title">Roster fill</div>
    <div class="card">
      ${slotBar("Goalies", s.confirmed_goalies, s.target_goalies, s.open_goalie_slots)}
      ${slotBar("Skaters", s.confirmed_skaters, s.target_skaters, s.open_skater_slots)}
    </div>

    <div class="section-title">Substitute pool</div>
    <div class="card">
      <div class="row"><span class="name">Enrolled substitutes</span>
        <span class="badge ${s.substitutes_enrolled ? "blue" : "gray"}">${s.substitutes_enrolled}</span></div>
    </div>

    <div class="actions">
      <button class="act primary" data-goto="game">Open Game Detail</button>
    </div>
    ${toast ? `<div class="toast">${esc(toast)}</div>` : ""}
  `;
}

/* ---------------- Game tab ---------------- */
function renderGame(board) {
  return `
    <div class="segmented">
      <button class="seg ${gameView === "coach" ? "active" : ""}" data-view="coach">Coach</button>
      <button class="seg ${gameView === "player" ? "active" : ""}" data-view="player">Player</button>
    </div>
    <div style="padding-top:8px">${gameView === "coach" ? coachBody(board) : playerBody(board)}</div>
  `;
}

function coachBody(board) {
  const s = board.status;
  const selected = board.players.filter((p) => p.group === "selected");
  const subs = board.players.filter((p) => p.group === "substitute");
  const locked = s.status === "locked";

  const selectedRows = selected.map((p) => {
    let btn = "";
    if (!locked) {
      if (p.backed_out) {
        btn = `<button class="act success" data-act="confirm" data-id="${p.id}">Re-confirm</button>`;
      } else if (p.availability === "available") {
        btn = `<button class="act danger" data-act="backout" data-id="${p.id}">Can't play</button>`;
      } else {
        btn = `<button class="act ghost" data-act="confirm" data-id="${p.id}">Confirm</button>`;
      }
    }
    return `<div class="row">${avatar(p)}<span class="name">${esc(p.name)}</span>${statusBadge(p)}${btn}</div>`;
  }).join("");

  const subRows = subs.length
    ? subs.map((p) => {
        const offered = p.sub_status === "offered";
        const btn = locked ? "" : `<button class="act primary" data-act="add" data-id="${p.id}">Add</button>`;
        return `<div class="row">${avatar(p)}<span class="name">${esc(p.name)}</span>
          ${offered ? '<span class="badge orange">Offered</span>' : '<span class="badge blue">Enrolled</span>'}${btn}</div>`;
      }).join("")
    : `<div class="empty">No substitutes enrolled.</div>`;

  return `
    <div class="banner ${bannerClass(s.status)}">
      <h2>${prettyStatus(s.status)}</h2><p>${esc(s.message)}</p>
    </div>
    <div class="game-head"><h2>${gameTitle(board)}</h2>
      <div class="sub">Sat 18:30 · ${esc(board.game.rink || "Rink 2")}</div></div>

    <div class="card">
      ${slotBar("Goalies", s.confirmed_goalies, s.target_goalies, s.open_goalie_slots)}
      ${slotBar("Skaters", s.confirmed_skaters, s.target_skaters, s.open_skater_slots)}
    </div>

    <div class="section-title">Selected Players</div>
    <div class="card">${selectedRows || '<div class="empty">No players selected.</div>'}</div>

    <div class="section-title">Substitute Pool</div>
    <div class="card">${subRows}</div>

    ${locked
      ? `<div class="locked-note">🔒 Roster is locked. Player actions are disabled.
           <button class="act ghost" data-act="unlock" style="margin-left:auto">Unlock</button></div>`
      : `<div class="actions"><button class="act ghost" data-act="lock">Lock Roster</button></div>`}
    ${toast ? `<div class="toast">${esc(toast)}</div>` : ""}
  `;
}

function playerBody(board) {
  const players = board.players;
  const locked = board.status.status === "locked";
  if (!pickedPlayer || !players.find((p) => p.id === pickedPlayer)) {
    pickedPlayer = players[0] ? players[0].id : null;
  }
  const options = players.map((p) =>
    `<option value="${p.id}" ${p.id === pickedPlayer ? "selected" : ""}>${esc(p.name)} · ${esc(p.position)}</option>`
  ).join("");

  const p = players.find((x) => x.id === pickedPlayer);
  let card = `<div class="empty">No players.</div>`;
  if (p) {
    const acts = (html) => locked
      ? `<div class="locked-note">🔒 Roster is locked — actions are disabled until the coach unlocks.</div>`
      : `<div class="actions">${html}</div>`;
    if (p.group === "selected" && !p.backed_out) {
      card = `<div class="banner ok"><h2>You are selected for this game</h2><p>Status: ${statusText(p)}</p></div>
        ${acts(`<button class="act success" data-act="confirm" data-id="${p.id}">I'm Available</button>
                <button class="act danger" data-act="backout" data-id="${p.id}">I Can't Play</button>`)}`;
    } else if (p.group === "selected" && p.backed_out) {
      card = `<div class="banner alert"><h2>You marked yourself unavailable</h2><p>The coach has been notified.</p></div>
        ${acts(`<button class="act success" data-act="confirm" data-id="${p.id}">I'm Available again</button>`)}`;
    } else if (p.group === "substitute") {
      if (p.sub_status === "offered") {
        card = `<div class="banner warn"><h2>A game slot is available</h2><p>You've been offered a spot. Accept?</p></div>
          ${acts(`<button class="act success" data-act="accept" data-id="${p.id}">Accept</button>
                  <button class="act danger" data-act="decline" data-id="${p.id}">Decline</button>`)}`;
      } else {
        card = `<div class="banner neutral"><h2>You are enrolled as a substitute</h2><p>Waiting for a slot to open.</p></div>
          ${acts(`<button class="act ghost" data-act="withdraw" data-id="${p.id}">Withdraw</button>`)}`;
      }
    } else {
      card = `<div class="banner neutral"><h2>You are not selected for this game</h2><p>Substitute status: Not enrolled.</p></div>
        ${acts(`<button class="act primary" data-act="enroll" data-id="${p.id}">Enroll as Substitute</button>`)}`;
    }
  }

  return `<div class="section-title">View as player</div>
    <select class="player-picker" id="player-picker">${options}</select>
    ${card}
    ${toast ? `<div class="toast">${esc(toast)}</div>` : ""}`;
}

function statusText(p) {
  if (p.roster_status === "accepted") return "Confirmed (substitute)";
  if (p.availability === "available" || p.roster_status === "confirmed") return "Confirmed";
  return "Awaiting your response";
}

/* ---------------- Activity tab ---------------- */
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
    ? notifs.map((n) => `<div class="feed-item">
        <div class="feed-dot ${esc(n.audience)}"></div>
        <div class="feed-body"><div class="msg">${esc(n.message)}</div>
          <div class="who">to ${esc(n.audience)}${n.subject_player_id && names[n.subject_player_id] ? " · " + esc(names[n.subject_player_id]) : ""}</div>
        </div></div>`).join("")
    : `<div class="empty">No notifications yet.</div>`;

  const auditRows = audit.length
    ? audit.map((a) => {
        const who = a.subject_player_id && names[a.subject_player_id] ? " — " + esc(names[a.subject_player_id]) : "";
        return `<div class="audit-line"><span class="a-action">${esc(AUDIT_LABEL[a.action] || a.action)}</span>${who}</div>`;
      }).join("")
    : `<div class="empty">No audit entries.</div>`;

  return `
    <div class="section-title">Notifications</div>
    <div class="card">${feed}</div>
    <div class="section-title">Audit trail (${board.audit_count})</div>
    <div class="card">${auditRows}</div>
  `;
}

/* ---------------- actions ---------------- */
async function handleAction(act, id) {
  toast = "";
  if (act === "confirm")
    await post(`${BASE}/availability`, { player_id: id, availability_status: "available" });
  else if (act === "backout")
    await post(`${BASE}/availability`, { player_id: id, availability_status: "unavailable" });
  else if (act === "enroll") await post(`${BASE}/substitutes/enroll`, { player_id: id });
  else if (act === "withdraw") await post(`${BASE}/substitutes/withdraw`, { player_id: id });
  else if (act === "add") await post(`${BASE}/substitutes/${id}/add-to-roster`, {});
  else if (act === "accept") await post(`${BASE}/substitutes/${id}/accept`, {});
  else if (act === "decline") await post(`${BASE}/substitutes/${id}/decline`, {});
  else if (act === "lock") await post(`${BASE}/roster/lock`, {});
  else if (act === "unlock") await post(`${BASE}/roster/unlock`, {});
  await render();
}

/* ---------------- render & wiring ---------------- */
async function render() {
  const board = await getBoard();
  document.getElementById("nav-title").textContent = NAV_TITLE[tab];
  const content = document.getElementById("content");
  content.innerHTML =
    tab === "today" ? renderToday(board)
    : tab === "game" ? renderGame(board)
    : renderActivity(board);

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
  tab = next;
  toast = "";
  document.querySelectorAll(".tab").forEach((x) =>
    x.classList.toggle("active", x.dataset.tab === next));
  render();
}

document.querySelectorAll(".tab").forEach((b) =>
  b.addEventListener("click", () => switchTab(b.dataset.tab)));

document.getElementById("reset-btn").addEventListener("click", async () => {
  await post("/api/reset", {});
  toast = ""; pickedPlayer = null;
  render();
});

render();
