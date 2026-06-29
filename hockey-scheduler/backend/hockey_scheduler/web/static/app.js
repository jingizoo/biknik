/* Hockey Scheduler — iPhone demo frontend.
   Talks to the real backend via the documented API endpoints. */

const GAME = "game_1"; // seed always creates this id
const BASE = `/api/games/${GAME}`;

let view = "coach";
let pickedPlayer = null;
let toast = "";

const POS_LABEL = { goalie: "G", defense: "D", forward: "F", skater: "S" };
const POS_CLASS = { goalie: "pos-G", defense: "pos-D", forward: "pos-F", skater: "pos-D" };

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

function bannerClass(status) {
  if (status === "open_slot") return "alert";
  if (status === "needs_substitute") return "warn";
  if (status === "roster_confirmed") return "ok";
  if (status === "locked" || status === "draft") return "neutral";
  return "neutral";
}

function statusBadge(p) {
  if (p.backed_out) return `<span class="badge red">Backed out</span>`;
  if (p.availability === "available" || p.roster_status === "confirmed" || p.roster_status === "accepted")
    return `<span class="badge green">Confirmed</span>`;
  if (p.availability === "unavailable") return `<span class="badge red">Unavailable</span>`;
  return `<span class="badge gray">Pending</span>`;
}

function posDot(p) {
  return `<div class="pos-dot ${POS_CLASS[p.position]}">${POS_LABEL[p.position]}</div>`;
}

/* ---------------- Coach view ---------------- */
function renderCoach(board) {
  const s = board.status;
  const selected = board.players.filter((p) => p.group === "selected");
  const subs = board.players.filter((p) => p.group === "substitute");
  const locked = s.status === "locked";

  const selectedRows = selected
    .map((p) => {
      let btn;
      if (p.backed_out) {
        btn = `<button class="act success" data-act="confirm" data-id="${p.id}">Re-confirm</button>`;
      } else if (p.availability === "available") {
        btn = `<button class="act danger" data-act="backout" data-id="${p.id}">Can't play</button>`;
      } else {
        btn = `<button class="act ghost" data-act="confirm" data-id="${p.id}">Confirm</button>`;
      }
      return `<div class="row">${posDot(p)}<span class="name">${p.name}</span>
        ${statusBadge(p)}${locked ? "" : btn}</div>`;
    })
    .join("");

  const subRows = subs.length
    ? subs
        .map((p) => {
          const offered = p.sub_status === "offered";
          return `<div class="row">${posDot(p)}<span class="name">${p.name}</span>
            ${offered ? '<span class="badge orange">Offered</span>' : '<span class="badge blue">Enrolled</span>'}
            ${locked ? "" : `<button class="act primary" data-act="add" data-id="${p.id}">Add</button>`}</div>`;
        })
        .join("")
    : `<div class="empty">No substitutes enrolled.</div>`;

  return `
    <div class="banner ${bannerClass(s.status)}">
      <h2>${prettyStatus(s.status)}</h2>
      <p>${s.message}</p>
    </div>
    <div class="game-head">
      <h2>${board.game.id === GAME ? "U16 Lions vs Falcons" : board.game.id}</h2>
      <div class="sub">Sat 18:30 · ${board.game.rink || "Rink 2"}</div>
    </div>
    <div class="card">
      <div class="row"><span class="name">Goalies</span>
        <span class="meta">${s.confirmed_goalies}/${s.target_goalies} confirmed${s.open_goalie_slots ? ` · ${s.open_goalie_slots} open` : ""}</span></div>
      <div class="row"><span class="name">Skaters</span>
        <span class="meta">${s.confirmed_skaters}/${s.target_skaters} confirmed${s.open_skater_slots ? ` · ${s.open_skater_slots} open` : ""}</span></div>
    </div>

    <div class="section-title">Selected Players</div>
    <div class="card">${selectedRows || '<div class="empty">No players selected.</div>'}</div>

    <div class="section-title">Substitute Pool</div>
    <div class="card">${subRows}</div>

    <div class="actions">
      ${locked
        ? `<button class="act ghost" data-act="unlock">Unlock Roster</button>`
        : `<button class="act ghost" data-act="lock">Lock Roster</button>`}
    </div>
    ${toast ? `<div class="toast">${toast}</div>` : ""}
  `;
}

/* ---------------- Player view ---------------- */
function renderPlayer(board) {
  const players = board.players;
  if (!pickedPlayer || !players.find((p) => p.id === pickedPlayer)) {
    pickedPlayer = players[0] ? players[0].id : null;
  }
  const options = players
    .map((p) => `<option value="${p.id}" ${p.id === pickedPlayer ? "selected" : ""}>
        ${p.name} · ${p.position}</option>`)
    .join("");

  const p = players.find((x) => x.id === pickedPlayer);
  let card = "";
  if (!p) {
    card = `<div class="empty">No players.</div>`;
  } else if (p.group === "selected" && !p.backed_out) {
    card = `
      <div class="banner ok"><h2>You are selected for this game</h2>
        <p>Status: ${statusText(p)}</p></div>
      <div class="actions">
        <button class="act success" data-act="confirm" data-id="${p.id}">I'm Available</button>
        <button class="act danger" data-act="backout" data-id="${p.id}">I Can't Play</button>
      </div>`;
  } else if (p.group === "selected" && p.backed_out) {
    card = `
      <div class="banner alert"><h2>You marked yourself unavailable</h2>
        <p>The coach has been notified.</p></div>
      <div class="actions">
        <button class="act success" data-act="confirm" data-id="${p.id}">I'm Available again</button>
      </div>`;
  } else if (p.group === "substitute") {
    if (p.sub_status === "offered") {
      card = `
        <div class="banner warn"><h2>A game slot is available</h2>
          <p>You've been offered a spot. Accept?</p></div>
        <div class="actions">
          <button class="act success" data-act="accept" data-id="${p.id}">Accept</button>
          <button class="act danger" data-act="decline" data-id="${p.id}">Decline</button>
        </div>`;
    } else {
      card = `
        <div class="banner neutral"><h2>You are enrolled as a substitute</h2>
          <p>Waiting for a slot to open.</p></div>
        <div class="actions">
          <button class="act ghost" data-act="withdraw" data-id="${p.id}">Withdraw</button>
        </div>`;
    }
  } else {
    card = `
      <div class="banner neutral"><h2>You are not selected for this game</h2>
        <p>Substitute status: Not enrolled.</p></div>
      <div class="actions">
        <button class="act primary" data-act="enroll" data-id="${p.id}">Enroll as Substitute</button>
      </div>`;
  }

  return `
    <div class="section-title">View as player</div>
    <select class="player-picker" id="player-picker">${options}</select>
    ${card}
    ${toast ? `<div class="toast">${toast}</div>` : ""}
  `;
}

function statusText(p) {
  if (p.roster_status === "accepted") return "Confirmed (substitute)";
  if (p.availability === "available" || p.roster_status === "confirmed") return "Confirmed";
  return "Awaiting your response";
}

function prettyStatus(s) {
  return {
    draft: "Draft",
    selected: "Selected",
    awaiting_responses: "Awaiting Responses",
    roster_confirmed: "Roster Confirmed",
    needs_substitute: "Needs Substitute Decision",
    open_slot: "Open Slot",
    locked: "Roster Locked",
    final: "Final",
  }[s] || s;
}

/* ---------------- actions ---------------- */
async function handleAction(act, id) {
  toast = "";
  if (act === "confirm")
    await post(`${BASE}/availability`, { player_id: id, availability_status: "available" });
  else if (act === "backout")
    await post(`${BASE}/availability`, { player_id: id, availability_status: "unavailable" });
  else if (act === "enroll")
    await post(`${BASE}/substitutes/enroll`, { player_id: id });
  else if (act === "withdraw")
    await post(`${BASE}/substitutes/withdraw`, { player_id: id });
  else if (act === "add")
    await post(`${BASE}/substitutes/${id}/add-to-roster`, {});
  else if (act === "accept")
    await post(`${BASE}/substitutes/${id}/accept`, {});
  else if (act === "decline")
    await post(`${BASE}/substitutes/${id}/decline`, {});
  else if (act === "lock")
    await post(`${BASE}/roster/lock`, {});
  else if (act === "unlock")
    await post(`${BASE}/roster/unlock`, {});
  await render();
}

/* ---------------- render & wiring ---------------- */
async function render() {
  const board = await getBoard();
  const content = document.getElementById("content");
  content.innerHTML = view === "coach" ? renderCoach(board) : renderPlayer(board);

  content.querySelectorAll("button[data-act]").forEach((b) => {
    b.addEventListener("click", () => handleAction(b.dataset.act, b.dataset.id));
  });
  const picker = document.getElementById("player-picker");
  if (picker) {
    picker.addEventListener("change", (e) => {
      pickedPlayer = e.target.value;
      toast = "";
      render();
    });
  }
}

document.querySelectorAll(".seg").forEach((b) => {
  b.addEventListener("click", () => {
    document.querySelectorAll(".seg").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    view = b.dataset.view;
    toast = "";
    render();
  });
});

document.getElementById("reset-btn").addEventListener("click", async () => {
  await post("/api/reset", {});
  toast = "";
  pickedPlayer = null;
  render();
});

render();
