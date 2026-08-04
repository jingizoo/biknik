/* Hockey Scheduler — calendar-first operator demo.
   Drives the real backend (setup + roster/substitute) via the documented API. */

let view = "dashboard";     // dashboard|setup|import|calendar|games|roster|activity|public|readiness|player_home
let gameView = "coach";     // coach | player (roster)
let rosterSide = "home";    // home | away — which lineup the roster tab shows (#25)
let rosterTeamId = null;    // team_id of the currently shown lineup (for copy)
let currentGame = null;     // game id whose roster we're viewing
let pickedPlayer = null;
let wizard = null;          // {slot_id, league_id, division_id, home_id, away_id} when scheduling
let iceBuilder = null;      // {form, preview} when the Ice Availability Builder is open (#158)
let calendarDate = todayISO();    // YYYY-MM-DD shown on the arena calendar
let calendarMode = "day";   // day | week | month (#158)
let calFilters = { venueId: "all", rinkId: "all", divisionId: "all", teamId: "all" };
let toast = "";
// Errors persist until the next interaction/close; success messages
// auto-clear (#118 Phase 5). post() resets this at the start of every
// mutating call and sets it back to true only when that call errors, so a
// handful of purely client-side validation messages set it explicitly too.
let toastIsError = false;
let toastTimer = null;      // pending auto-clear timeout for a success toast
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
let guardianLinksState = [];       // guardian↔junior links, Users tab (#35)
let guardianLinkForm = { guardian_user_id: "", player_id: "" };
// New-account form, Users tab (#135). Deliberately holds no password field —
// the password input is read live via val() only at submit time and never
// assigned here, so it can never be re-rendered back onto the page.
let newAccountForm = { username: "", role: "", team_id: "", player_id: "", official_id: "" };
let newAccountError = "";
let notifPrefs = null;             // signed-in user's own channel prefs (#81)
let feedTokens = [];               // signed-in user's calendar feed tokens (#82)
let newFeedUrl = null;             // freshly-minted feed URL, shown once (#82)
// Active Program/Season context (#159): the AUTHORIZED options + the current
// selection, from GET /api/context/options (never the unfiltered overview, so a
// scoped user can neither select nor enumerate an unrelated context). Since
// #367/#369 this is NOT a display-only selection: the server resolves the same
// tuple on its own side and the operational reads (get_demo_overview,
// get_standings, get_setup_progress, get_setup_overview_v2) are scoped to it,
// so switching genuinely changes what the screens show. A handful of lists are
// still deliberately broader — the general Setup screen's "+ Add" parent
// selects, and get_setup_overview_v2's Program-wide shape — so neither "these
// screens are not filtered" nor "everything is filtered" is a safe thing to
// write down; see docs/architecture/active-context-scoping.md for what is
// actually scoped, and index.html's #ctx-scope-note for why the operator-facing
// copy describes the control instead of listing screens.
let contextOptions = null;         // {programs:[{id,name,seasons:[...]}], selected:{program_id,season_id,read_only}}
// Monotonic "context generation" (#331 review round 7), bumped by every
// successful setActiveContext() call. #159's context switcher can move the
// active Program/Season out from under a view that cached something scoped
// to the PREVIOUS selection (the Import wizard's chosen Season, an open Ice
// Builder's whole form+preview) -- a plain boolean/timestamp can't tell
// "still the same selection" apart from "changed and changed back", but an
// always-incrementing counter can: a view stamps the revision it was last
// bound under, and a mismatch against the current one is unambiguous proof
// something needs rebinding, however many switches happened in between.
let contextRevision = 0;
// Monotonic count of context-SWITCH ATTEMPTS (#331 review round 8), distinct
// from contextRevision above: this bumps on every CALL to setActiveContext(),
// whether or not its POST ultimately succeeds, so a rapid A->B->C (each
// started before the previous one's round trip resolved) can tell which is
// the LATEST attempt -- only it may apply its eventual POST/refresh/render
// result; an earlier one that resolves later must recognize it's been
// superseded and do nothing further, on success OR failure.
let contextSwitchSeq = 0;
// Monotonic op tokens for Import/Ice Builder's own async operations (#331
// review round 10), distinct from contextRevision/contextSwitchSeq above:
// those only ever detect the ACTIVE CONTEXT changing underneath a request.
// A stale response can just as easily be wrong within the SAME context --
// canceling and reopening the Ice Builder, editing its form (or an
// exclusion) while a Preview/Commit is in flight, or simply firing two
// Validates/Previews back to back and having them resolve out of order --
// none of which touch contextRevision at all. Each bumps on EVERY event
// that makes a not-yet-resolved Preview/Validate/Commit response
// obsolete -- issuing a newer one of the same kind, opening/canceling the
// builder, or editing the live form/exclusions/sheets -- and each handler
// snapshots the current value immediately before its own vulnerable
// `await` and rechecks it after, the exact same idiom contextRevision
// itself already uses. A single global counter per surface is enough:
// there is only ever one live `iceBuilder`/`importState` at a time, so
// nothing here needs a per-instance identity separate from "the latest
// token issued."
let iceOperationSeq = 0;
let importOperationSeq = 0;
let publicState = { schedule: null, standings: null, division: null, game: null,
  feedUrl: null, feedLabel: null };  // feedUrl/feedLabel: freshly-minted public calendar subscription (#33)
let publicTab = "schedule";        // "schedule" | "standings" (#83)
let schedulerState = {
  division: null, preview: null, drafts: [], summary: null,
  // #375 — the configurable regular-season format: how many times each team
  // plays every other. 1 is the historical single round-robin, so an
  // operator who never touches the control gets exactly the old behaviour.
  meetings: 1,
  // #390 — the configurable turnaround, in MINUTES, measured from the
  // previous game's end. 0 is exactly the pre-#390 behaviour, so an operator
  // who never touches the control gets the historical proposal; the field
  // itself is now always SENT, which is the half of the defect that lived
  // here: the screen never sent a rest value at all, so the backend's own
  // field defaulted to zero and the engine's other two defects were masked.
  turnaround: 0,
  filters: { division: "all", rink: "all", issue: "all" },  // (#106)
  selected: new Set(),  // game_ids picked for publish/discard (#106)
};  // (#86)
// #328 review round 8 finding 4 -- a terminal commit refusal
// (pairing_already_scheduled/preview_stale) clears the preview and forces
// a fresh Generate, but render() replaces #content wholesale, so the
// focused Commit button is simply gone -- nothing moves focus anywhere,
// silently dropping a keyboard user back to the document body. Set by
// schedCommit's error branch, consumed once by the scheduler wiring below
// right after the fresh content (with a Generate button again) is in the
// DOM.
let schedFocusGenerateAfterRender = false;
let officialAvailability = [];      // signed-in official's windows (#88)
let availSummary = null;            // roster availability rollup (#89)
let subCandidates = null;           // coach substitute outreach queue (#112)
let addableSubs = null;             // eligible-but-not-enrolled team players a coach can add (#114)
let dashAvailability = null;        // availability-summary for the coach dashboard's next game (#146)
let dashSubQueue = null;            // substitute-candidates for the coach dashboard's next game (#146)
let playersList = [];               // [{id,name,team_id,position,jersey_number,...}] for Setup (#114)
let leagueTeams = {};               // program_id -> [{id,name,program_id}] permanent members (#180/#233 v2)
// #283 Slice B: the permanent Program → League membership tree, derived from
// the canonical hierarchy so the Setup UI can render a team's permanent League
// and offer a league-to-league transfer (promotion/relegation). Keyed by
// program for the transfer picker's program-scoped options; the flat list backs
// the team-create drawer's optional permanent-League select.
let permLeaguesByProgram = {};      // program_id -> [{id,name,programName}]
let allPermLeagues = [];            // [{id,name,programName}] across all programs
let teamPermLeague = {};            // team_id -> {id,name} permanent League (#283 Slice E)
let seasonRegs = {};                // season_id -> [{id,team_id,league_id,division_id,active}] registrations (#180/#233 v2)
let seasonVenueAccess = {};         // season_id -> [{id,season_id,venue_id,active}] (#233 Slice E)
let seasonVenueCandidates = {};     // season_id -> [{id,name}] grantable Venues (#369; MANAGE_SETUP-gated route)
let leagueDivisions = {};           // league_id -> [{id,name,...}] — cascade data for the League→Division
                                     // selects in Season participation / Rollover (#233 Slice B2b), populated
                                     // each render from hv and read by the onchange handlers that rescope a
                                     // Division select after its paired League select changes.
let rollover = { programId: "", fromSeasonId: "", toSeasonId: "", result: null };  // season rollover picker (#180/#233 v2)
let modal = null;                   // themed confirm/blocked modal (#215): {type, ...}
let demoMenuOpen = false;           // header demo (database) dropdown open? (#215)
let rescheduleRequests = null;      // reschedule request(s) for the current game (#29)
let availFilter = "all";            // all|available|unavailable|maybe|no_response
let gamesFilter = { division: "all", team: "all", rink: "all", status: "all", from: "", to: "" };  // Games list filters (#152)
let gamesExpanded = new Set();      // game_ids whose full checklist is expanded in the Games list (#152)
let contactForm = { recipient_ref: "", channel: "email", destination: "", label: "" };
let tokenForm = { recipient_ref: "", provider: "fcm", token: "", label: "" };
let movingGameId = null;    // click-to-move fallback: game awaiting a destination slot
let conflict = null;        // {ok, title, lines[], undo?} — calendar side panel (#43/#153)
let pendingMove = null;     // {gid, slotId, willUnpublish, willUnlock} — move awaiting confirmation (#153)
let pendingReassign = null; // {kind, parent, id, name, curId, seasonId, programId} — setup reassignment awaiting confirm (#166, programId #283)
let drawer = null;          // {kind} when a Setup create drawer is open (#44)
let drawerError = "";       // validation/API error shown inside the open drawer
let drawerValues = {};      // {fieldId: value} preserved across re-render on error
let checkoutConfirm = null;  // {game_id} while the Player Home "Can't Play" confirmation is open (#107)
let oppDetailGame = null;    // game_id of the substitute opportunity whose detail is open (#110)
let oppDetail = null;        // fetched detail payload for oppDetailGame (#110)
// Guardian linked-junior surface (#26). A guardian acts FOR a junior, so every
// piece of open UI state is keyed by the junior's player_id, never a global
// "current player" — a guardian may have several linked juniors on screen.
let guardianHome = null;     // fetched /api/me/guardian/home payload
let gCheckout = null;        // {jid, game_id} while a junior's "Can't Play" confirm is open
let gOpp = null;             // {jid, game_id} of the junior's opportunity detail open
let gOppDetail = null;       // fetched detail payload for gOpp
let activityExpandedBatches = new Set();  // import_batch_ids expanded in Activity (#102)
// Home/Tasks and Setup card state lives in the per-card store (#365 —
// cardStates/cardGenerations, further down). It replaced the three
// page-wide values that used to sit here: `setupProgress`,
// `setupProgressError` and the single `setupProgressFetchSeq` counter that
// guarded the whole setup-progress fetch. One counter for a whole surface
// cannot express "this ONE card's retry superseded this ONE card's earlier
// request", and one payload + one error boolean cannot express "this card
// failed while its neighbours are fine" — both of which #365 requires.
let setupView = "hub";  // "hub" | "hierarchy" | "records" — Setup sub-view
// (#165; "hub" added by #345 batch 2). The workflow hub is the DEFAULT route
// into Setup: the undifferentiated mega-page must not be the only way in.
// Both older sub-views stay reachable from the segmented toggle, so no
// destination that was reachable before became unreachable.
let readinessCheck = null;  // /api/readiness snapshot for the Pilot Readiness card (#104)
let importState = {         // Pilot onboarding import wizard (#96)
  type: "teams_players",    // which IMPORT_TYPES entry is selected
  seasonId: null,            // only used by the teams_players type
  sheetsText: {},            // {csv field name: pasted text}, reset on type switch
  report: null,              // last /api/import/dry-run result (or {error})
  validatedKey: null,        // snapshot of sheetsText at the last successful validate;
                             // Commit refuses to run if the current text has drifted
  committed: null,           // last commit result (or {error})
  contextRevision: null,     // #331 review round 7 -- the contextRevision this
                             // seasonId/report/validatedKey/committed were last
                             // bound under; a mismatch means they're stale.
};

const DAY_MS = 86400000;
// "Today", as the rest of this file already means it: the current UTC calendar
// day. Every other date here is UTC — addDays/addMonths use getUTC*, dayOf
// slices a UTC ISO string, and the calendar's own labels pass
// timeZone: "UTC" — and the backend lays its ice down at UTC midnight, so
// reading the LOCAL day would put the calendar a day off its own data for
// anyone whose offset has rolled over.
//
// This used to be the string "2026-09-05" in three places (#387/#389): the
// initial calendarDate, the calendar's "Today" button, and the ice-slot
// drawer's Date default. That literal agreed with a seed that booked
// 2026-09-05..19, so the pair looked coherent while both were wrong; "Today"
// navigated to a fixed day in 2026 on main, whatever the date really was.
// e2e/calendar-today.js fails if any of them is named again.
function todayISO() { return new Date().toISOString().slice(0, 10); }
function addDays(dateStr, n) {
  const d = new Date(dateStr + "T00:00:00Z");
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}
function shiftDate(days) { calendarDate = addDays(calendarDate, days); }
// Shift by whole months, clamping the day to the target month's length so
// e.g. Jan 31 + 1 month lands on the last day of February, never overflows (#158).
function addMonths(dateStr, n) {
  const d = new Date(dateStr + "T00:00:00Z");
  const day = d.getUTCDate();
  d.setUTCDate(1);
  d.setUTCMonth(d.getUTCMonth() + n);
  const lastDay = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 0)).getUTCDate();
  d.setUTCDate(Math.min(day, lastDay));
  return d.toISOString().slice(0, 10);
}
function fmtMonth(dateStr) {
  return new Date(dateStr + "T00:00:00Z").toLocaleDateString("en-GB",
    { month: "long", year: "numeric", timeZone: "UTC" });
}
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
  readiness: "Pilot Readiness", player_home: "Home", guardian_home: "My Players",
  // #345: "onboarding" is a real destination (the #174 Initial Setup wizard,
  // and the view a fresh Program actually LANDS on) but had no NAV entry, so
  // it had neither a topbar heading nor -- once titles existed -- a page
  // title. Label matches its sidebar button.
  onboarding: "Initial Setup",
};
const POS_CLASS = { goalie: "pos-G", defense: "pos-D", forward: "pos-F", skater: "pos-D" };
const REPO = "https://github.com/jingizoo/biknik/issues";
const DEMO_PASSWORD = "demo";  // shared password for the demo personas (#67)
let envStatus = null;          // deployment posture for the topbar chips (#72)

const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (iso) => { const m = /T(\d{2}:\d{2})/.exec(iso || ""); return m ? m[1] : ""; };
const val = (id) => { const e = document.getElementById(id); return e ? e.value.trim() : ""; };
// Format a stored UTC season boundary as its intended CALENDAR date, rendered in
// the Program's timezone (#272) — a date-only bound was anchored to local
// midnight there, so formatting in that zone shows the entered day, never an
// adjacent one from raw UTC conversion.
const fmtDateInTz = (iso, tz) => {
  if (!iso) return null;
  // Fall back to UTC when the Program's stored timezone can't be resolved by
  // Intl (a legacy/invalid zone) — matching create_season/parse_season_boundary
  // — so a validly-stored boundary is shown as its UTC calendar day rather than
  // silently disappearing (#272 review).
  for (const zone of [tz || "UTC", "UTC"]) {
    try {
      return new Intl.DateTimeFormat("en-CA", {
        timeZone: zone, year: "numeric", month: "short", day: "numeric",
      }).format(new Date(iso));
    } catch (e) { /* unresolved zone → retry in UTC */ }
  }
  return null;
};
const seasonDateRange = (s, tz) => {
  const a = fmtDateInTz(s.start_date, tz), b = fmtDateInTz(s.end_date, tz);
  if (a && b) return `${a} – ${b}`;
  if (a) return `from ${a}`;
  if (b) return `until ${b}`;
  return null;
};
// Read-only test hook (#272): the strict CSP (script-src 'self') forbids a
// browser regression from injecting these pure display helpers, so expose them
// for e2e assertion of the unresolvable-timezone UTC fallback. No app state.
if (typeof window !== "undefined") {
  window.__seasonFmt = { fmtDateInTz, seasonDateRange };
}
const hasPerm = (p) => rolePerms.has(p);
// Demo vs production posture (#215): the "Reset demo" action and demo-only
// affordances only make sense outside production. Defaults to demo until the
// status probe resolves, matching the server default (APP_MODE=demo).
const isDemo = () => !envStatus || envStatus.app_mode !== "production";
// True when the demo setup is a clean slate (#215) — drives Load vs Reset and
// the "Start your league" empty state. Server-computed; defaults to false so a
// missing status never hides Reset on a populated demo.
const isDemoEmpty = () => !!(envStatus && envStatus.demo_empty);
// Whether to surface the Administration → Danger zone factory-reset control
// (#256). The server already computes `factory_reset_enabled` as production-mode
// AND the deployment opt-in flag — the exact gate the /execute route enforces —
// so the UI never has to re-derive it; we add the client-side identity check to
// mirror the backend exactly: the exact `league_admin` role AND both
// manage_setup and manage_users. Checking the role explicitly (not just the two
// permissions) matches FactoryResetService, so a future permission-matrix change
// that granted both permissions to some other role could never surface this
// control there. The service re-verifies role and both permissions on every
// call, so this gate is presentational only, never the security boundary.
const canFactoryReset = () =>
  !!(envStatus && envStatus.factory_reset_enabled)
  && currentRole === "league_admin"
  && hasPerm("manage_setup") && hasPerm("manage_users");

// Theme-aligned inline SVG icons (#215): 20×20, stroke=currentColor so a button
// class controls colour (neutral at rest, red on destructive hover/focus). Kept
// tiny and dependency-free — no icon font, no external asset.
const _svg = (paths) => `<svg class="ico" viewBox="0 0 24 24" fill="none" `
  + `stroke="currentColor" stroke-width="2" stroke-linecap="round" `
  + `stroke-linejoin="round" aria-hidden="true" focusable="false">${paths}</svg>`;
const ICONS = {
  trash: _svg('<path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/>'),
  swap: _svg('<path d="M8 3L4 7l4 4"/><path d="M4 7h16"/><path d="M16 21l4-4-4-4"/><path d="M20 17H4"/>'),
  circleMinus: _svg('<circle cx="12" cy="12" r="9"/><path d="M8 12h8"/>'),
  circleCheck: _svg('<circle cx="12" cy="12" r="9"/><path d="M8.5 12.5l2.5 2.5 4.5-5"/>'),
  circleX: _svg('<circle cx="12" cy="12" r="9"/><path d="M15 9l-6 6"/><path d="M9 9l6 6"/>'),
  database: _svg('<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>'),
  pencil: _svg('<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5z"/>'),
};

// Shared page-intro block (#146): every view's *title* already comes from
// the topbar's nav-title (set by setChrome() from the NAV map, #24) — this
// is deliberately just the "what is this screen for, what do I do here"
// helper text a tester's first 5 seconds on a screen actually needs,
// optionally paired with the one primary action for that screen. Not a
// full header component (title/breadcrumb/status already live in the
// topbar shell) — adding a second, competing title inside content would
// duplicate rather than clarify.
function pageIntro(helperText, primaryActionHtml) {
  return `<div class="page-intro"><p class="muted">${esc(helperText)}</p>
    ${primaryActionHtml || ""}</div>`;
}

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

// Read an API Response defensively. A healthy endpoint returns JSON — either a
// payload or the structured `{error:{...}}` shape — but a proxy 5xx (e.g. Render
// returning a 502 "Bad Gateway" HTML page), an empty body, or a dropped
// connection would make a bare `.json()` throw an uncaught SyntaxError and leave
// the UI silently hung. Instead, surface those as the same `{error:{...}}` shape
// so every existing `if (d && d.error)` call site shows a message and recovers.
async function readApiResponse(r) {
  let text = "";
  try { text = await r.text(); } catch (_) { text = ""; }
  if (text) {
    try { return JSON.parse(text); } catch (_) { /* not JSON (e.g. a 502 page) */ }
  }
  if (r.ok) return {};  // a successful but empty body (e.g. 204) — not an error
  const msg = r.status >= 500 || r.status === 0
    ? `The server is temporarily unavailable (${r.status}). Please try again in a moment.`
    : `The request could not be completed (${r.status}).`;
  return { error: { code: "server_unavailable", message: msg } };
}
function networkErrorResult() {
  return { error: { code: "network_error",
    message: "Couldn't reach the server. Check your connection and try again." } };
}

// The session cookie carries identity; the server resolves the role from it
// and authorizes each request (#50). No client-asserted role header.
async function getJSON(p) {
  try {
    return await readApiResponse(await fetch(p, { credentials: "same-origin" }));
  } catch (_) {
    return networkErrorResult();
  }
}
async function post(p, b) {
  // Reset first: an explicit success message the caller sets after a clean
  // response (the common `if (r && !r.error) toast = "..."` pattern) always
  // runs after this point, so it inherits the correct "not an error" state
  // without every one of those call sites needing to say so itself.
  toastIsError = false;
  let d;
  try {
    const r = await fetch(p, { method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(b || {}) });
    d = await readApiResponse(r);
  } catch (_) {
    d = networkErrorResult();
  }
  if (d && d.error) { toast = d.error.message; toastIsError = true; }
  return d;
}
// The same transport as post(), minus its GLOBAL toast writes — for a write
// whose outcome belongs to ONE card rather than to the page (#365).
//
// post() publishes the server's error message into the sitewide toast the
// instant its own await resolves, which is a mutation of the CURRENT tuple
// performed by a response that may already be superseded: the operator can
// have switched Program/Season while the POST was in flight, and an older
// tuple's failure would then speak over the new one. A card-scoped write has
// to be able to decide, AFTER re-checking its identity, whether that message
// is allowed to be said at all — so this hands the outcome back and says
// nothing itself. Callers announce through announceCardStatus(), which is
// identity-gated like every other mutation point.
async function postScoped(p, b) {
  try {
    return await readApiResponse(await fetch(p, { method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(b || {}) }));
  } catch (_) {
    return networkErrorResult();
  }
}

// Shared write-order logic for moving a season registration to a new
// League/Division (#233 B2b review) — used by BOTH Season participation's
// own Save control (renderSeasonParticipation's regRow) and the Needs-
// assignment repair row's Save control (renderSetupHierarchy's
// regIssueRow), so the sequencing lives in exactly one place.
//
// A registration's League and Division are cross-validated server-side (a
// set Division must belong to the registration's current League) — see
// setup_service.assign_season_team_league / assign_season_team_division
// (v2). Changing League while an old, now-foreign Division is still
// attached would be rejected by assign-league itself. Deviation from a
// literal "assign-league then assign-division" order (#233 B2b): clear the
// Division FIRST whenever League is changing and one is currently set, so
// the League move always lands cleanly, then re-apply the (already
// league-rescoped) Division afterward if one was picked.
//
// Each individual POST is server-side transactional (a single write can't
// half-apply), so the only partial state possible is "an earlier step in
// this sequence succeeded, then a later one failed" — callers use the
// returned `partial` flag to tell an operator their Save actually mutated
// something rather than silently no-op'ing.
async function saveRegistrationPlacement(registrationId, newLeagueId, newDivisionId,
                                         origLeagueId, origDivisionId) {
  const leagueChanged = newLeagueId !== origLeagueId;
  const divChanged = newDivisionId !== origDivisionId;
  let appliedAny = false;
  const fail = (res) => ({
    ok: false,
    error: (res && res.error && res.error.message) || "The update could not be completed.",
    partial: appliedAny,
  });

  if (leagueChanged && origDivisionId) {
    const res = await post(
      `/api/v2/setup/season-team-registration/${registrationId}/assign-division`, { division_id: null });
    if (res && res.error) return fail(res);
    appliedAny = true;
  }
  if (leagueChanged) {
    const res = await post(
      `/api/v2/setup/season-team-registration/${registrationId}/assign-league`, { league_id: newLeagueId });
    if (res && res.error) return fail(res);
    appliedAny = true;
  }
  if (newDivisionId && (leagueChanged || divChanged)) {
    const res = await post(
      `/api/v2/setup/season-team-registration/${registrationId}/assign-division`, { division_id: newDivisionId });
    if (res && res.error) return fail(res);
    appliedAny = true;
  } else if (!leagueChanged && divChanged) {
    const res = await post(
      `/api/v2/setup/season-team-registration/${registrationId}/assign-division`, { division_id: null });
    if (res && res.error) return fail(res);
    appliedAny = true;
  }
  return { ok: true, error: null, partial: false };
}
// Sets `toast`/`toastIsError` from a saveRegistrationPlacement() result — the
// one place that turns its {ok, error, partial} contract into operator-facing
// text, shared by both Save call sites so "fully applied" / "failed before
// any write" / "partially applied" always read the same way everywhere.
function placementSaveToast(result, successMessage) {
  if (result.ok) { toast = successMessage; toastIsError = false; return; }
  if (result.partial) {
    toast = `Partially saved — an earlier change was applied, but a later step failed (${result.error}). Please retry to finish.`;
  } else {
    toast = result.error;
  }
  toastIsError = true;
}

/* ---------- shared ---------- */
const bannerClass = (s) => s === "open_slot" ? "alert" : s === "needs_substitute" ? "warn" : s === "roster_confirmed" ? "ok" : "neutral";
// Same ok/warn/alert/neutral → green/orange/red/gray mapping the .banner
// classes use (styles.css), so a roster status badge never disagrees with the
// banner shown for the same status elsewhere (#118 Phase 3.2).
const GS_STATUS_BADGE = { ok: "green", warn: "orange", alert: "red", neutral: "gray" };
const prettyStatus = (s) => ({
  draft: "Draft", selected: "Selected", awaiting_responses: "Awaiting Responses",
  roster_confirmed: "Roster Confirmed", needs_substitute: "Needs Substitute Decision",
  open_slot: "Open Slot", locked: "Roster Locked", final: "Final",
}[s] || s);
function statusBadge(p) {
  if (p.backed_out) return `<span class="badge red">Backed out</span>`;
  if (p.availability === "available" || p.roster_status === "confirmed" || p.roster_status === "accepted") return `<span class="badge green">Confirmed</span>`;
  if (p.availability === "unavailable") return `<span class="badge red">Unavailable</span>`;
  // Matches the roster availability summary's own maybe → purple/"Maybe"
  // convention (AVAIL_PILL/AVAIL_LABEL) — same status, same color everywhere.
  if (p.availability === "maybe") return `<span class="badge purple">Maybe</span>`;
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
// #390 — one proposed/draft row's own calendar DAY, for the surfaces that
// previously rendered a bare clock time. Two proposals on different days at
// the same hour were indistinguishable, so an operator could not see which
// day a proposed game fell on.
//
// Derived by SLICING the ISO string and handing the date-only value to
// fmtDate, exactly as `fmt` reads the clock straight out of the same string:
// both halves of one row therefore describe the same instant by construction.
// Converting through `new Date` here would reinterpret it in the viewer's
// zone and could print a different day than the time beside it.
const fmtRowDate = (iso) => { const d = dayOf(iso); return d ? fmtDate(d) : ""; };

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

// Shared between render()'s data-fetch step (needs the coach's next game's
// id to pull its availability/substitute-queue detail) and renderDashboard's
// display (which highlights that same game) — one definition of "next game"
// used in both places (#146).
function coachTeamGames(ov, teamId) {
  return (ov.schedule || []).filter((g) => !teamId
    || g.home_team_id === teamId || g.away_team_id === teamId);
}
function nextUpcomingGame(games) {
  return games.filter((g) => g.result_status !== "final")
    .slice().sort((a, b) => new Date(a.start_time) - new Date(b.start_time))[0] || null;
}

/* ---------- Per-card state model (#365) ---------- */
// Home/Tasks and each of the six Setup workflow cards/landings own their
// state SEPARATELY, per context tuple — not as the one page-wide pair
// (`setupProgress` + `setupProgressError`, behind the single page-wide
// `setupProgressFetchSeq`) that this replaces. #365, verbatim: "Model state
// per workflow/card and per context tuple, not as one page-wide boolean. A
// retry should replace only the failed card generation. A response for an
// older Program/Season/League tuple or older retry generation must be
// discarded before it mutates DOM, focus, announcements, completion, or
// next-task selection."
//
// TWO orthogonal, explicitly discriminated axes — deliberately not collapsed
// into one enum:
//
//   CARD_STATE   the card's own data lifecycle. Every value is a decision the
//                code MADE, never something inferred from a missing payload
//                field: EMPTY means "resolved, and there is nothing here yet"
//                (and carries its own `reason`), ERROR means "this card's own
//                fetch failed", STALE means "held data whose context tuple is
//                no longer the active one".
//   CARD_STATUS  the workflow's own done/todo/OPTIONAL axis, which the
//                BACKEND owns — get_setup_progress emits `status: "optional"`
//                for Workflow 6 (api/service.py). Folding it into CARD_STATE
//                would make "this card's fetch failed" and "this step is
//                optional" mutually exclusive, which they are not: Workflow 6
//                is optional whether its card is loading, ready, stale or
//                errored.
const CARD_STATE = Object.freeze({
  LOADING: "loading",   // a request for this card is in flight
  READY: "ready",       // resolved, with data to show
  EMPTY: "empty",       // resolved, and there is genuinely nothing here yet
  STALE: "stale",       // held data bound to a context tuple that is no longer active
  ERROR: "error",       // this card's own fetch failed; retry is scoped to it
  CONFIRM: "confirm",   // an in-card confirmation is awaiting the operator
  // A WRITE this card itself started is in flight. Distinct from LOADING,
  // which is a READ: a read can be superseded and forgotten, whereas an
  // unresolved write has already left the browser and its outcome is not yet
  // known. The card therefore keeps its counts, offers NO control (a second
  // press would fire a second write), and holds keyboard focus itself rather
  // than letting the replaced confirmation drop focus to <body>. Terminal in
  // one direction only: the write's own response — verified still current —
  // is what moves it, never a render that happens to run.
  PENDING: "pending",
  SUCCESS: "success",   // resolved, and this card's work is complete
});
const CARD_STATUS = Object.freeze({
  DONE: "done", TODO: "todo", OPTIONAL: "optional",
  UNKNOWN: "unknown",   // no progress payload for this card (yet, or by role)
});

// The outcome of a READ a card model depends on — the third axis, and the one
// #365 review round 2 finding 3 says was missing: "Carry an explicit progress
// outcome into the model." A transport that turns its own failure into `null`
// FAILS OPEN, because every later reader is then free to read that `null` as
// "nothing to report, carry on" — which is exactly how a failed
// /api/v2/setup/progress could still produce a READY card, an UNKNOWN status
// and a retry that announced "updated". `null` cannot be told apart from
// "this role may not read it" or "the backend had nothing to say"; an
// asserted outcome can. #365 verbatim: "Use explicit discriminated states
// rather than inferring state from missing payload fields."
const CARD_READ = Object.freeze({
  OK: "ok",                       // the read happened; its payload is authoritative
  FAILED: "failed",               // the read was attempted and it failed
  UNAUTHORIZED: "unauthorized",   // not attempted: this role may not read it
});

// Card ids — the first component of the identity record below. Setup's six
// reuse the SAME keys `SETUP_WORKFLOWS` declares and the backend's
// get_setup_progress reports, so a card can never be identified one way in
// the model and another way anywhere else.
const HOME_TASKS_CARD = "home/setup-progress";
function setupWorkflowCardId(key) { return `setup/${key}`; }

// card id -> newest generation issued for THAT card, and card id -> its
// committed model. One counter per card (not one per surface, which is
// exactly what `setupProgressFetchSeq` was) is what lets a retry supersede
// only its own card's earlier request. Same snapshot-before-the-await /
// re-check-after idiom as contextRevision, contextSwitchSeq, iceOperationSeq
// and importOperationSeq elsewhere in this file, narrowed from "the page" to
// "this card".
const cardGenerations = {};
const cardStates = {};

// ========== THE AUTHENTICATED-PRINCIPAL / SESSION EPOCH (#365 round 7) ======
// The three axes above -- card, context tuple, generation -- describe WHAT is
// on screen. None of them describes WHO IS LOOKING AT IT, and that omission is
// this round's defect, verbatim from the review:
//
//   "The ledger and card identity survive an authenticated-user change, so the
//   departing user's delayed operation response can mutate the next user's UI
//   and disclose the departing user's typed reason. resetTransientUiState()
//   invalidates other identity-scoped UI state but does not invalidate
//   cardWrites, cardStates, or cardGenerations; the card identity contains
//   only card + Program/Season/League + generation, not the authenticated
//   principal/session epoch. Because the ledger refuses the arriving user's
//   render for the same tuple, the departing identity's generation also
//   remains current."
//
// That last sentence is the sting. The round-6 serialization rule is what
// KEEPS the departing identity current: beginCardRequest() refuses every
// request for a card with an unresolved write and leaves cardGenerations
// untouched, so the arriving principal's own renders cannot advance the
// counter past the departing principal's operation. Generation equality --
// the one thing cardIdentityCurrent() had left to judge by -- therefore says
// "still current" precisely when the person in front of the browser has
// changed. Reproduced through the app's own no-reload signIn()/setUser() path:
// A holds a reopen with the reason "first write held", the operator switches
// in-app to Admin B on the SAME persisted Program/archived Season, and A's 503
// lands while currentUser.username === "second_admin" -- turning B's card into
// CONFIRM carrying A's exact reason, A's error text, A's focus move and A's
// toast. With a lower-privileged arriving role it also restores a control that
// role is not authorized to exercise.
//
// WHY THE FIX IS NOT "DELETE THE LEDGER ON AN IDENTITY CHANGE". The ledger
// entry conflates two facts that have DIFFERENT OWNERS AND DIFFERENT LIFETIMES,
// and they have to be split:
//
//   (1) SERIALIZATION BOOKKEEPING -- "a write is in flight against this card
//       and this target tuple". This is a fact about the SERVER, not about the
//       view or the viewer. It must survive a principal change untouched, or
//       the round-6 A -> B -> A duplicate-write hole reopens while A's request
//       is still live and its transaction may still commit. It is
//       epoch-INDEPENDENT: ANY principal, the arriving one included, is
//       blocked from starting a second write for that card+tuple.
//       currentCardWrite() and therefore beginCardRequest() deliberately ask
//       no question whatsoever about who is signed in.
//
//   (2) THE PRIVATE PAYLOAD -- the held PENDING model, the operator's typed
//       reason, the server's error text, the focus target. This belongs to THE
//       PRINCIPAL WHO TYPED IT. It is epoch-SCOPED and must never be visible
//       to, restorable by, or consumed as the UI outcome of another principal.
//
// So on a principal change the record KEEPS (1) and DROPS (2). The arriving
// principal sees a card that is non-actionable -- otherwise the duplicate
// write returns -- carrying NO text the departing principal entered: no
// reason, no error, no focus move, no toast, and no hint that anything was
// typed at all. foreignCardWriteModel() below is that neutral presentation,
// and resetTransientUiState() destroys the real payload rather than merely
// hiding it.
//
// THE EPOCH IS CLIENT-SIDE and needs no server field or new endpoint.
// setUser() already fires resetTransientUiState() on exactly the transition
// that matters (its `prevId !== nextId` guard), and that one transition covers
// both no-reload paths the regression requires: the demo role-switcher / login
// form calling signIn(), and a real sign-out followed by a sign-in. A page
// reload starts a fresh document with an empty ledger and empty card state, so
// it needs no epoch at all.
let uiIdentityEpoch = 1;

// WHO a card identity belongs to.
//
// Two halves, and the epoch is the authoritative one: it separates two
// consecutive SESSIONS of the same username -- a real sign-out followed by the
// same person signing back in -- which a username comparison alone cannot see.
// The principal is compared as well so that any future path which swapped
// `currentUser` WITHOUT going through resetTransientUiState() would still be
// caught, rather than silently inheriting the previous operator's live
// operations.
function cardPrincipalId() { return currentUser ? currentUser.username : null; }
function cardIdentitySamePrincipal(identity) {
  if (!identity) return false;
  return identity.epoch === uiIdentityEpoch
    && identity.principal === cardPrincipalId();
}

// What the ARRIVING principal is shown for a card whose unresolved write
// belongs to a DEPARTED one. Non-actionable (PENDING withdraws every control
// on both surfaces -- setupCardBodyHtml and setupLandingActions -- and sets
// aria-busy), `status: UNKNOWN` so the hub roll-up counts nothing it cannot
// see, and no `stats`, because even the counts were read under the departing
// principal's permissions.
//
// The copy describes only THIS CARD'S OWN AVAILABILITY. It deliberately does
// not name the operation, the operator or the reason: "this season is being
// reopened" would itself disclose what the previous operator did, and the
// requirement is that B not learn even that A typed anything.
const FOREIGN_CARD_WRITE_NOTE = "This card is waiting on the server. It can't"
  + " be changed until that finishes.";
function foreignCardWriteModel() {
  return { state: CARD_STATE.PENDING, status: CARD_STATUS.UNKNOWN,
           pendingNote: FOREIGN_CARD_WRITE_NOTE };
}

// card id -> target tuple -> an UNRESOLVED WRITE this card started, and the
// enforcement behind the PENDING declaration above. That declaration says a
// pending card is "terminal in one direction only: the write's own response —
// verified still current — is what moves it, never a render that happens to
// run." Nothing enforced it: commitSetupWorkflowCards() issued a fresh
// generation and committed a newly fetched model for EVERY workflow on an
// ordinary Setup render, so re-entering the Facilities destination under the
// unchanged Program/Season superseded a live write's identity, restored
// "Reopen this season", and let the operator fire a SECOND reopen POST for the
// same Season while the first was still unresolved. A declared invariant
// standing in for an enforced one is not an invariant.
//
// ============================ THE SERIALIZATION RULE ======================
// An unresolved card write is an OPERATION-level state. While the ledger holds
// an entry for this card AGAINST THE TUPLE THAT IS CURRENT NOW:
//
//  (1) REFUSED, not repainted. Every request for that card is refused
//      OUTRIGHT by beginCardRequest(), which returns null WITHOUT touching
//      cardGenerations. The generation never advances, so the pending
//      identity stays current, its committed model, aria-busy and focus
//      target stay exactly as the write left them, and its eventual response
//      still passes cardIdentityCurrent(). Repainting the pending body back
//      afterwards would NOT be equivalent: the generation would already have
//      moved, and the write's own response would then be discarded as stale
//      even though the server may well have committed it.
//
//  (2) ONLY ITS OWN SETTLEMENT ENDS IT. Nothing gets past the gate in (1) —
//      there is no exempting token — because the registration is released by
//      settleCardWrite() at the single point after the operation's own await,
//      and only then does its terminal handling run. Settlement is
//      unconditional and comes before every return on that path, so an
//      operation cannot end without its record ending with it.
//
//  (3) NO SECOND USER-INITIATED WRITE. Every other operation entry point —
//      the confirmation opener, its resolver, the per-card retry — is refused
//      by the same gate. That is defence in depth behind the surfaces
//      themselves: the CARD BODY paints no control in PENDING
//      (setupCardBodyHtml) and the LANDING withdraws all three action groups
//      (setupLandingActions), so there is nothing for a pointer to hit and
//      nothing for the keyboard to reach in the first place.
//
//  (4) A DIFFERENT TUPLE RENDERS NORMALLY. currentCardWrite() looks the
//      registration up UNDER THE TUPLE THAT IS CURRENT NOW, so a switched-to
//      Program/Season finds none of its own and paints its card with its own
//      generations exactly as it always did — while the registration made
//      against the tuple the operator left goes on existing.
//
// ================ WHAT LEAVING THE TUPLE DOES *NOT* DO ====================
// It does not end the operation, and the previous round of this comment said
// it did. It called such a write "ABANDONED", dropped its registration, and
// argued that discarding the eventual response "is the right outcome and not
// a gap". That conflated two different facts, and only the first of them was
// true:
//
//   * TRUE: the RESPONSE must be discarded when it arrives into a tuple the
//     operator has moved on from. Applying it would paint one Season's
//     outcome under another Season's heading — the race legs 2 and 3 of
//     setup-card-write-identity.js exist for.
//
//   * FALSE: that this makes leaving harmless. Navigation cancelled NOTHING.
//     The HTTP request is still in flight and the server transaction behind
//     it may still commit. Discarding the response protects the UI; it does
//     nothing whatever about the live write.
//
// So dropping the registration on tuple mismatch destroyed the only record
// that the operation existed, and a short round trip — A, to B, back to A,
// all before the first response settles — restored the mutation: the card
// re-rendered READY with "Reopen this season" actionable, and confirming
// again issued a SECOND concurrent lifecycle write against the same Season
// while the outcome of the first was still unknown. Duplicated lifecycle and
// audit effects, and a UI reasoning from neither write.
//
// ================== THE LEDGER: KEYED BY CARD *AND TARGET* =================
// `cardWrites` is therefore an UNRESOLVED-OPERATION LEDGER, two levels deep:
//
//     cardWrites[cardId][cardTupleKey(target tuple)] -> entry
//
// and the second level is the whole correction. An operation belongs to a
// CARD AND THE TUPLE/SEASON IT TARGETS, and ITS LIFETIME IS THE REQUEST'S,
// NOT THE VIEW'S. Nothing about which tuple happens to be on screen creates
// or destroys an entry:
//
//   * REGISTERED when the PENDING model commits (commitCardState), against
//     the identity's own tuple, carrying the exact PENDING model so the
//     presentation can be REBUILT after a round trip has destroyed the DOM
//     and overwritten cardStates with the other tuple's model.
//   * READ BY CURRENT TUPLE (currentCardWrite) — so B is free and A is
//     blocked, at the same time, from the same ledger. A refusal that were
//     global-by-card would freeze B, which is not the rule.
//   * RELEASED ONLY BY SETTLEMENT (settleCardWrite), at the one point after
//     the request's own await, on EVERY path — including the path where the
//     initiating UI identity has gone stale, which is precisely the path a
//     happy-path-only drain leaves blocked forever.
//
// A card with an entry for the CURRENT tuple reads as PENDING through
// readCardState() whatever cardStates holds, so returning to the target of an
// unresolved write repaints the non-actionable pending presentation rather
// than a settled card with a live control on it.
const cardWrites = {};

// The three #345 axes as one comparable key. The Season axis is IN it, so
// "card + target tuple" and "card + target Season" are the same lookup for
// every write this file performs — each names the SELECTED Season as its
// target. The entry carries `target` as well, mirroring the id the write
// actually put in its URL, so a future card that reopened some OTHER Season
// would have the explicit target to key on instead of inheriting it.
// NUL-joined because ids are opaque strings and any printable separator could
// in principle occur inside one.
function cardTupleKey(t) {
  return `${(t && t.program_id) || ""}\u0000${(t && t.season_id) || ""}`
    + `\u0000${(t && t.league_id) || ""}`;
}

// The unresolved write registered for `cardId` AGAINST THE TUPLE THAT IS
// CURRENT NOW, or null.
//
// It deliberately does NOT delete anything. The line that used to live here —
// `if (!cardTupleCurrent(held)) { delete cardWrites[cardId]; return null; }` —
// is the round-6 defect itself: it answered "no unresolved write" correctly
// for the switched-to tuple, but paid for that answer by forgetting the
// operation, so coming back found nothing to be blocked by. Answering by
// LOOKUP gives the same freedom to B without costing A its record.
//
// IT ALSO ASKS NOTHING ABOUT WHO IS SIGNED IN, and that is deliberate (#365
// round 7). This is half (1) of the split above -- SERIALIZATION BOOKKEEPING,
// a fact about the server. Adding an epoch condition here would answer "no
// unresolved write" to the arriving principal and hand them back an actionable
// control against a Season whose first write is still in flight: exactly the
// duplicate-write hole round 6 closed, re-opened through a different door. The
// epoch governs half (2), the PRIVATE PAYLOAD, and it is applied at the
// readers (readCardState) and at the identity gate (cardIdentityCurrent) --
// never here.
function currentCardWrite(cardId) {
  const ledger = cardWrites[cardId];
  if (!ledger) return null;
  return ledger[cardTupleKey(currentCardTuple())] || null;
}

// Open the ledger entry for `identity`'s operation, holding the PENDING model
// it committed. Called from commitCardState alone, so "the card is PENDING"
// and "an operation is registered" are the same event and cannot disagree.
function registerCardWrite(identity, model) {
  const ledger = cardWrites[identity.card] || (cardWrites[identity.card] = {});
  ledger[cardTupleKey(identity)] = {
    card: identity.card, identity: identity,
    // The tuple this operation TARGETS — the key, spelled out — and the
    // Season its URL names. Both are the identity's, captured before the
    // await, never re-read from live context afterwards.
    tuple: { program_id: identity.program_id, season_id: identity.season_id,
             league_id: identity.league_id },
    target: identity.season_id,
    // What the card must be rebuilt as while this stays unresolved. The round
    // trip repaints from readCardState(), and by then cardStates holds the
    // OTHER tuple's model — so the pending presentation has to come from here
    // or it does not come at all.
    //
    // THIS FIELD IS THE PRIVATE PAYLOAD — half (2) of the split (#365 round
    // 7). It carries the operator's typed reason (`confirmReason`), this
    // card's counts read under their permissions, and the confirmation they
    // would be handed back. It belongs to `identity.principal` in
    // `identity.epoch` and to nobody else: readCardState() substitutes
    // foreignCardWriteModel() for any other principal, and
    // resetTransientUiState() overwrites it outright so the text stops
    // existing rather than merely stops being rendered. Everything ELSE in
    // this entry is half (1) and survives a principal change untouched.
    model: model,
  };
}

// SETTLEMENT — the one and only place a registration is released, and the
// reason the ledger cannot leak. Keyed by the identity's OWN tuple, not by
// the current one, so it drains identically whether the operator is standing
// on the target or three switches away from it. Returns the entry (so the
// caller can tell a real settlement from a double one) or null.
function settleCardWrite(identity) {
  const ledger = identity && cardWrites[identity.card];
  if (!ledger) return null;
  const key = cardTupleKey(identity);
  const entry = ledger[key];
  // Identity equality, not just key equality: only THIS operation's own
  // response may retire THIS operation's registration.
  if (!entry || entry.identity !== identity) return null;
  delete ledger[key];
  if (!Object.keys(ledger).length) delete cardWrites[identity.card];
  return entry;
}

// The active context tuple (#345's three axes). Read from
// contextOptions.selected — the CONFIRMED selection, not a switch merely
// attempted: setActiveContext() bumps contextRevision the instant a switch
// starts but only updates `selected` once its POST succeeds, so a response
// that resolves during that window is still answering for the tuple that is
// genuinely on screen, and discarding it there would throw away good data.
// A failed switch leaves the tuple untouched and nothing goes stale; a
// successful one updates it before its own re-render, so the next commit
// binds to the new tuple and everything held under the old one reads stale.
function currentCardTuple() {
  const sel = (contextOptions && contextOptions.selected) || {};
  return { program_id: sel.program_id || null,
           season_id: sel.season_id || null,
           league_id: sel.league_id || null };
}

// Issue the identity a request must still match to be allowed to change
// anything. Owner-specified shape: workflow/card id + the exact context tuple
// (program_id, season_id, league_id) + the request generation. Captured
// BEFORE the vulnerable `await` and re-checked after it at EVERY mutation
// point — a stale response that skips the DOM write but still moves focus or
// re-announces is the same defect.
//
// Returns null when the serialization rule above REFUSES the request: the
// LEDGER holds an unresolved operation for THIS CARD AGAINST THE TUPLE THAT
// IS CURRENT RIGHT NOW. That pairing is the rule, and both halves of it
// matter: keyed on the card alone the refusal would freeze the switched-to
// Program/Season as well, which is not the guarantee; keyed on "the write
// whose tuple still happens to be active" — the old currentCardWrite() — the
// registration was destroyed by merely looking from somewhere else, so a
// round trip back to the target found nothing left to refuse.
//
// The refusal is taken BEFORE the counter moves, so a refused caller leaves
// no trace at all — that is what makes the pending identity survive an
// ordinary render rather than merely be painted back over a generation that
// has already advanced past the write. Every call site treats null as "do
// nothing"; commitCardState, repaintSetupWorkflowCard, announceCardStatus and
// focusCardTarget all refuse a null identity on their own account too.
//
// There is no "resolving" token any more, and there must not be one: the
// registration is released by SETTLEMENT (settleCardWrite, at the single
// point after the operation's own await) rather than handed forward to the
// one caller allowed past a still-standing gate. A token only works on the
// paths that remember to carry it, and the path this round is about — the
// response landing while the initiating identity is stale — is exactly the
// one that would not have carried it.
//
// The record carries the AUTHENTICATED PRINCIPAL AND SESSION EPOCH too (#365
// round 7). Card + tuple + generation describes what is on screen; it says
// nothing about who is looking at it, and under the serialization rule the
// generation cannot even move while a write is unresolved — so without the
// epoch a departing operator's response arrives looking perfectly current to
// the arriving one. The refusal above is still epoch-INDEPENDENT: any
// principal is blocked from starting a second write for a card+tuple that has
// one outstanding.
function beginCardRequest(cardId, opts) {
  const t = currentCardTuple();
  if (currentCardWrite(cardId)) return null;
  cardGenerations[cardId] = (cardGenerations[cardId] || 0) + 1;
  return { card: cardId, program_id: t.program_id, season_id: t.season_id,
           league_id: t.league_id, generation: cardGenerations[cardId],
           epoch: uiIdentityEpoch, principal: cardPrincipalId(),
           // Whether a person asked for this load (a Retry/Refresh press), as
           // opposed to a routine render-driven one. Only an operator-
           // initiated load is allowed to move focus or announce — a routine
           // refresh that did either would be a spurious repeat, not news.
           userInitiated: !!(opts && opts.userInitiated) };
}

// THE gate. True only when `identity` was issued to THE PRINCIPAL AND SESSION
// THAT IS SIGNED IN NOW, is still the newest request for its own card, AND the
// context tuple it was issued under is still the active one. Everything that a
// response could change — DOM, focus, announcement, completion, next-task
// selection — asks this first.
//
// The principal check is FIRST and is not a formality (#365 round 7). It is
// the only one of the three that can fail on an authenticated-user change: the
// tuple is per-user persisted and two operators can legitimately be on the
// same Program/Season, and the generation cannot advance at all while the
// serialization rule is refusing this card's requests. Drop it and a departing
// operator's delayed response passes every remaining test and mutates the
// arriving operator's card — model, DOM, focus, live region, completion and
// next task — with the departing operator's own typed text.
//
// WHICH PATHS ACTUALLY DEPEND ON IT — named, because a guard credited with a
// protection nothing exercises is the same defect as a missing one (#365 round
// 10). This line is LOAD-BEARING for the card's READS, which ask no principal
// question of their own anywhere:
//
//   retrySetupWorkflowCard()  — the per-card Retry/Refresh. Its four post-await
//     mutation points (the model commit, repaintSetupWorkflowCard,
//     announceCardStatus, focusCardTarget) are gated on this function alone.
//   loadSetupProgressCard()   — the Home/Tasks card. Same shape: the model
//     commit, the combined DOM+announcement gate and the focus move.
//   restorePendingCardWriteFocus() — passes the LEDGER entry's identity to
//     focusCardTarget, and that entry deliberately survives a principal change.
//
// It is NOT what protects the card's WRITE. reopenSelectedSeasonFromCard() asks
// cardIdentitySamePrincipal() DIRECTLY on its own post-await line, before this
// gate is consulted, and routes a foreign-principal response into the silent
// reconcile — so on that path this test is reached only for an identity that
// has already passed the same question. Say so plainly rather than let leg 7's
// coverage read as coverage of this line: deleting it left the whole write
// journey green, which is why the regression for it races a READ instead.
// e2e/setup-card-write-identity.js leg 9 holds the per-card refresh's own
// /api/v2/setup/overview across an in-app principal change and releases it
// inside the post-auth/pre-render window — where no render of the arriving
// principal's has run, so the generation counter has not moved and generation
// equality still says "current". Removing this one line makes that leg fail on
// all four points: the departing operator's "Venues, rinks and ice updated."
// is spoken into the arriving principal's live region, their model is
// committed, their card body is repainted, and focus is pulled onto the
// landing heading.
function cardIdentityCurrent(identity) {
  if (!identity) return false;
  if (!cardIdentitySamePrincipal(identity)) return false;  // #365 identity gate — principal/session epoch
  if (cardGenerations[identity.card] !== identity.generation) return false;
  const t = currentCardTuple();
  return identity.program_id === t.program_id
    && identity.season_id === t.season_id
    && identity.league_id === t.league_id;
}

// Staleness is a TUPLE question, not a generation one — deliberately a
// different predicate from cardIdentityCurrent() above. Held data goes stale
// when the operator moves to another Program/Season/League, NOT merely
// because a refresh for the same context happens to be in flight; answering
// the second question with the first would flash every card to "stale" on
// every ordinary reload.
//
// IT IS ALSO DELIBERATELY PRINCIPAL-FREE (#365 round 7), and this is the one
// place where folding the epoch in would be actively WRONG. The post-await
// branch in reopenSelectedSeasonFromCard() uses this predicate to ask "is the
// operator standing on the tuple this write targets" — and the correction for
// this round requires that when the ARRIVING principal is on that tuple, the
// card is reconciled from fresh server truth rather than left alone. If this
// predicate answered "no" merely because the principal changed, that branch
// would return early and the arriving principal would be left looking at the
// neutral pending presentation with nothing to replace it. The epoch decides
// WHOSE UI STATE MAY BE USED (cardIdentityCurrent, readCardState); the tuple
// decides WHICH SEASON IS ON SCREEN. They are different questions.
//
// Every caller that passes a HELD model's identity here is nonetheless safe,
// because it obtains that model through readCardState(), which substitutes a
// principal-neutral model (carrying no identity at all) for a foreign entry —
// so `cardTupleCurrent(held.identity)` is false for the arriving principal by
// way of the reader, not by way of this predicate.
function cardTupleCurrent(identity) {
  if (!identity) return false;
  const t = currentCardTuple();
  return identity.program_id === t.program_id
    && identity.season_id === t.season_id
    && identity.league_id === t.league_id;
}

// The ONE writer. Refuses outright when the response's identity is no longer
// current, so a late loser never becomes the card's stored model in the first
// place. Every downstream mutation point re-checks as well — this is only
// where the corruption would otherwise begin, not the whole guard.
function commitCardState(identity, next) {
  if (!cardIdentityCurrent(identity)) return false;   // #365 identity gate — model commit
  // `identity` last, so a `next` cloned from a previous entry can never carry
  // that entry's older identity through into the new commit.
  cardStates[identity.card] = Object.assign({}, next, { identity: identity });
  // Committing PENDING is what REGISTERS the unresolved operation, here rather
  // than at the writer, so the operation-level state and the card-level state
  // can never disagree about whether a write is outstanding: a card is
  // PENDING exactly when there is a registered operation for it, by
  // construction. The model just stored travels into the ledger, because it is
  // what the card has to be REBUILT as if the operator leaves this tuple and
  // comes back — by then cardStates holds the other tuple's model instead.
  if (next && next.state === CARD_STATE.PENDING) {
    registerCardWrite(identity, cardStates[identity.card]);
  }
  return true;
}

// What a renderer paints. Downgrades any held model whose tuple is no longer
// active to STALE — #365's "keep the last successful data visible only when
// the stale contract calls for it, clearly labelled stale with an actionable
// refresh path" — rather than silently painting one Program's numbers under
// another Program's heading. A LOADING entry is left alone: it has no data to
// mislabel, and a fresher commit is already on its way.
//
// AN UNRESOLVED OPERATION AGAINST THE CURRENT TUPLE OUTRANKS cardStates
// ENTIRELY. Returning to the target of a write that has not settled has to
// repaint the non-actionable pending presentation, and by then there is
// nothing left to repaint it FROM: the round trip destroyed the DOM, and the
// renders that ran under the other tuple overwrote cardStates with that
// tuple's model. So the pending model is reconstructed from the LEDGER, which
// is the only thing that survived — and every reader of a card's state goes
// through this one function, so the card body, the landing's action groups,
// aria-busy and the hub roll-up all agree with the operation rather than with
// whatever the last render left behind.
//
// AND IT IS WHERE THE PRIVATE PAYLOAD IS WITHHELD (#365 round 7). Every
// surface that paints a card — the body, the landing's action groups, the
// status chip, aria-busy, the hub roll-up, the confirmation opener and its
// resolver — reads through this one function, so a single substitution here
// covers all of them and no call site has to remember. The two epoch-scoped
// sources are both handled:
//
//   * THE LEDGER'S HELD MODEL. The entry itself survives an authenticated-user
//     change (it is serialization bookkeeping, half (1)), so the card stays
//     non-actionable for the arriving principal — but the model it carries is
//     half (2) and is replaced by the neutral one. The arriving principal
//     learns that this card is busy with the server; nothing else.
//   * A COMMITTED cardStates ENTRY. resetTransientUiState() already destroys
//     these on an identity change, so this branch should be unreachable — and
//     it is asserted anyway, because "a stale cardStates entry from the
//     departing principal read under the arriving one is the same leak by
//     another route" (the review names cardStates and cardGenerations
//     alongside cardWrites). LOADING is the honest answer: this principal has
//     not read this card yet, and the render already under way commits their
//     own model within the same pass.
function readCardState(cardId) {
  const outstanding = currentCardWrite(cardId);
  if (outstanding) {
    return cardIdentitySamePrincipal(outstanding.identity)
      ? outstanding.model : foreignCardWriteModel();
  }
  const entry = cardStates[cardId];
  if (!entry) return { state: CARD_STATE.LOADING, status: CARD_STATUS.UNKNOWN };
  if (!cardIdentitySamePrincipal(entry.identity)) {
    return { state: CARD_STATE.LOADING, status: CARD_STATUS.UNKNOWN };
  }
  if (entry.state !== CARD_STATE.LOADING && !cardTupleCurrent(entry.identity)) {
    return Object.assign({}, entry, { state: CARD_STATE.STALE, staleFrom: entry.state });
  }
  return entry;
}

// (3) live-region announcement, for the surfaces that have no live region of
// their own. Routes through the ONE existing sitewide region — #toast-root,
// `role="status" aria-live="polite"` in index.html, driven by updateToast() —
// rather than minting a second one per card, so a confirmation or a success
// is spoken exactly once. The Home/Tasks card deliberately does NOT use this:
// #sp-card-slot is already its own polite live region, so settling it is what
// speaks there, and adding a toast would be the same sentence twice.
function announceCardStatus(identity, message, isError) {
  if (!cardIdentityCurrent(identity)) return false;   // #365 identity gate — announcement
  toast = message;
  toastIsError = !!isError;
  updateToast();
  return true;
}

// (3b) THE SAME ANNOUNCEMENT, WHEN THE THING BEING ANNOUNCED NAVIGATES.
//
// #365 requires confirmation and success announcements to reach the live
// region "without duplicate speech". Workflow 6's confirmation completes by
// LEAVING this surface (it opens the guided Initial Setup wizard), and every
// navigation goes through switchTab(), which sets `toast = ""` and calls
// render() -> updateToast(). Announcing first and navigating second therefore
// wrote the completion sentence into the region and withdrew it again inside
// ONE synchronous task -- before the browser painted, and before any
// accessibility update could be delivered. The region was populated and empty
// again in the same frame: not duplicate speech, no speech at all.
//
// Announcing on BOTH sides of the transition would be the duplicate the very
// same clause forbids. So the sentence is written exactly ONCE, AFTER the
// destination has rendered: `navigate` runs first and the announcement lands
// when it settles -- past switchTab()'s own render() and past any awaited
// destination load -- so nothing left in that transition clears it again, and
// it is still standing a task and a paint later.
//
// THE IDENTITY IS CHECKED BEFORE THE NAVIGATION, deliberately. It answers "is
// this still the operator's own current, unsuperseded confirmation", which is
// a question about the moment they confirmed. The destination's own render
// legitimately issues fresh requests for these cards and supersedes this
// generation on the way -- reading that as "a stale response tried to
// announce" would silently drop the very sentence this exists to deliver.
function announceCardStatusAfter(identity, message, navigate) {
  if (!cardIdentityCurrent(identity)) return Promise.resolve(false);  // #365 identity gate
  return Promise.resolve(navigate()).then(() => {
    toast = message;
    toastIsError = false;
    updateToast();
    return true;
  });
}

// (2) focus. Moves keyboard focus onto `el`, using the same tabindex="-1"
// convention focusContentHeading() uses for a non-focusable destination
// heading — but never stamping tabindex on a control that is already
// focusable, which would pull it out of the sequential tab order. Refuses for
// a superseded identity: a late loser that skipped the DOM write but still
// yanked focus out of whatever the operator moved on to is the same defect
// the DOM guard exists to prevent.
function focusCardTarget(identity, el) {
  if (!cardIdentityCurrent(identity)) return false;   // #365 identity gate — focus
  if (!el) return false;
  if (!el.hasAttribute("tabindex")
      && !/^(BUTTON|A|INPUT|SELECT|TEXTAREA)$/.test(el.tagName)) {
    el.setAttribute("tabindex", "-1");
  }
  el.focus();
  return true;
}

// Structural optionality for Workflow 6 (Decision 9 / #365). `optional` is a
// PARTITION of the workflow list, not a flag each call site is trusted to
// remember: `required` is the ONLY list the completion and next-task
// derivations are ever handed, so generic logic cannot count the optional
// workflow even by accident. Derived from the BACKEND's own `status`
// ("optional" — api/service.py get_setup_progress), never from a title, key
// or list position.
function partitionSetupWorkflows(rows) {
  const workflows = (rows || []).map((w) => Object.assign({}, w, {
    status: w.status === CARD_STATUS.DONE ? CARD_STATUS.DONE
      : w.status === CARD_STATUS.OPTIONAL ? CARD_STATUS.OPTIONAL
      : CARD_STATUS.TODO,
    optional: w.status === CARD_STATUS.OPTIONAL,
  }));
  return {
    workflows: workflows,
    required: workflows.filter((w) => !w.optional),
    optional: workflows.filter((w) => w.optional),
  };
}

// The Setup progress read, as ONE asserted record instead of a payload that
// may or may not be there (#365 review round 2 finding 3). Every consumer is
// handed this — never a bare payload and never `null` — so "the status read
// failed" is a value the model can switch on rather than an absence a reader
// has to guess about. Frozen, and it carries its own partition, so no caller
// can re-partition the same rows a second, differently.
//
// `byKey` is deliberately EMPTY for anything but OK: a failed or unattempted
// read has no rows, and manufacturing one from a previous read is precisely
// the "retain old completion/next" the correction forbids.
function setupProgressRead(outcome, payload) {
  const part = partitionSetupWorkflows(
    outcome === CARD_READ.OK ? (payload && payload.workflows) : null);
  const byKey = {};
  part.workflows.forEach((row) => { byKey[row.key] = row; });
  return Object.freeze({ outcome: outcome, part: part, byKey: byKey });
}
// A COMPLETED /api/v2/setup/progress fetch, classified once. getJSON()
// resolves to `{error}` for a transport or HTTP failure (readApiResponse /
// networkErrorResult), so this is the single place that turns that into an
// asserted outcome — instead of the two call sites that each independently
// wrote `pr && !pr.error ? pr : null` and lost the distinction.
function setupProgressReadOf(payload) {
  return setupProgressRead(
    payload && !payload.error ? CARD_READ.OK : CARD_READ.FAILED, payload);
}

// (4) completion and (5) next-task, over a partition. Both read `required`
// only — the optional partition is not in that list at all, which is what
// makes "Workflow 6 never blocks completion and is never the next
// recommendation" structural rather than a special case someone could forget.
// `remaining` is deliberately a count of REQUIRED work, so the copy the card
// renders says something an operator can act on.
function setupWorkflowCompletion(part) {
  const done = part.required.filter((w) => w.status === CARD_STATUS.DONE);
  return { total: part.required.length, done: done.length,
           remaining: part.required.length - done.length,
           allDone: part.required.length > 0 && done.length === part.required.length };
}
// The next recommendation, in the fixed #204 workflow order. Only ever drawn
// from `required`; an optional workflow cannot be returned because it is not
// in the list being searched.
function nextRequiredWorkflow(part) {
  return part.required.find((w) => w.status !== CARD_STATUS.DONE) || null;
}

/* ---------- Home/Tasks hub setup-progress card (#204/#330) ---------- */
// The hub's single primary action (§4 of the operator-UX requirements): a
// dynamic "Continue setup" naming the actual next incomplete Setup workflow
// this caller's role can actually execute AND that is actually safe to run
// given the resolved Season (the backend already filters `next` to a
// role-actionable, prerequisite-clear workflow — #330 review round 1
// finding 1, #331 review round 3 finding 1), with the other workflows this
// caller's role can manage listed below as a non-competing secondary list
// (also role-filtered as of round 3 -- an Arena Manager never receives
// League-Admin-only completion detail here either). A workflow that's
// permitted but blocked on the Season (no Season resolved, or the resolved
// one archived) surfaces as actionable guidance instead — `next_blocked` —
// never a CTA that would just fail. Renders the required success state
// once the WHOLE Program's setup is done (`complete`); renders the named
// EMPTY state — its own heading, status sentence and explanation, and no
// control — when there's simply nothing left for THIS role to act on AND
// nothing blocked to explain either, while other, not-this-role's workflows
// remain (three different claims — see get_setup_progress's docstring).
//
// #365: the payload is turned into an explicit discriminated model FIRST
// (buildTasksCardModel below) and the renderer only ever switches on
// `model.state`. Nothing downstream re-reads the raw response or infers a
// state from an absent field, which is what let "no data" and "the fetch
// failed" look alike before.
function buildTasksCardModel(payload) {
  if (!payload || payload.error) {
    return { state: CARD_STATE.ERROR, status: CARD_STATUS.UNKNOWN,
             error: (payload && payload.error && payload.error.message) || null };
  }
  // Two DIFFERENT empty answers, discriminated by `reason` rather than left
  // to a `!progress.program_id` test at the render site. Naming them as
  // states is what lets renderSetupProgressCard give each its OWN heading,
  // status sentence and explanation (#365): "no_program" means no Program
  // exists or resolves for this operator at all, "nothing_actionable" means a
  // Program IS resolved and this role's visible slice of it has nothing left
  // to do — a claim that deliberately says nothing about workflows this role
  // cannot see (#331 review round 3 finding 1 / round 5 finding 3). An empty
  // answer is something the model asserts, not something the renderer infers
  // from a missing field, and neither reason is silent any more.
  if (!payload.program_id) {
    return { state: CARD_STATE.EMPTY, reason: "no_program", status: CARD_STATUS.UNKNOWN };
  }
  const part = partitionSetupWorkflows(payload.workflows);
  // (5) next-task selection. The backend's recommendation is authoritative
  // about which workflow is next (it alone knows role permissions and Season
  // prerequisites), but it is ADMITTED here only if it is not optional —
  // resolved through the same partition, so "Workflow 6 is never the next
  // recommendation" holds on the client too, structurally, without a key or
  // title check. An unrecognized key stays admitted: a workflow the payload
  // named as `next` but did not include in `workflows` is not evidence that
  // it is optional.
  const nextRow = payload.next
    ? (part.workflows.find((w) => w.key === payload.next.key) || null) : null;
  const next = payload.next && !(nextRow && nextRow.optional) ? payload.next : null;
  // (4) completion. `complete` stays the SERVER's claim — it is computed
  // there over the FULL, unfiltered workflow list and is deliberately `null`
  // for a role whose visible slice is narrower (get_setup_progress: a partial
  // view can never truthfully verify a whole-Program claim). Re-deriving it
  // from the visible rows here would manufacture exactly the overclaim that
  // `null` exists to prevent. The client's OWN completion arithmetic is
  // `progress` below, which is explicitly scoped to the workflows this role
  // can see and reads the `required` partition only.
  const model = { part: part, progress: setupWorkflowCompletion(part),
                  program: payload.program, next: next,
                  nextBlocked: payload.next_blocked || null,
                  status: CARD_STATUS.UNKNOWN };
  if (payload.complete === true) return Object.assign(model, { state: CARD_STATE.SUCCESS });
  if (next || model.nextBlocked) return Object.assign(model, { state: CARD_STATE.READY });
  return Object.assign(model, { state: CARD_STATE.EMPTY, reason: "nothing_actionable" });
}

// Client-side completion copy for this card (#365), derived from the
// `required` partition only. Rendered under the workflow list so an operator
// can see how much REQUIRED work is left without having to work out for
// themselves that the optional row does not count towards it.
function tasksProgressLine(model) {
  const p = model.progress;
  if (!p || !p.total || !model.part) return "";
  const optionalNote = model.part.optional.length
    ? ` ${esc(model.part.optional.map((w) => w.label).join(", "))} is optional and never blocks completion.`
    : "";
  return `<p class="muted sp-progress-line">${p.done} of ${p.total} required
    setup workflow${p.total === 1 ? "" : "s"} done.${optionalNote}</p>`;
}

// The workflow rows this card shows — extracted (#365 review round 2 finding
// 4) so the STALE state can render the SAME retained rows it says it is
// preserving, rather than a banner claiming data that isn't on screen. Rows
// are read-only markup by construction (no button, no data-* action hook), so
// reusing them in STALE cannot smuggle a CTA back into a withdrawn state.
function tasksWorkflowRowsHtml(model) {
  if (!model.part) return "";
  return model.part.workflows.map((w) => {
    // "optional" (Imports and onboarding, #331 review round 1 finding 5) is
    // a standing alternative entry point, not a required step -- its badge
    // must read as neither "Done" nor a to-do nag. #365: `w.optional` is the
    // partition flag partitionSetupWorkflows() derived from the backend's own
    // `status`, so the badge and the completion arithmetic can never disagree
    // about which workflow is the optional one.
    const cls = w.status === CARD_STATUS.DONE ? "green"
      : w.optional ? "blue" : "gray";
    const text = w.status === CARD_STATUS.DONE ? "Done"
      : w.optional ? "Optional" : "To do";
    // #331 review round 21 finding 2: surfaces `attention` here too --
    // reusing the existing `.li-sub.conflict` convention (draft scheduler)
    // -- so a workflow reading "To do" because its only registration(s)
    // are ambiguous shows THAT reason, not just the generic done/todo
    // detail text alone.
    const attentionLine = w.attention
      ? `<div class="li-sub conflict">⚠️ ${esc(w.attention.detail)}</div>` : "";
    return `<div class="li">
      <span class="badge ${cls}">${text}</span>
      <div class="li-main"><div class="li-title">${esc(w.label)}</div>
        <div class="li-sub">${esc(w.detail)}</div>${attentionLine}</div>
    </div>`;
  }).join("");
}

// The exact markup last written into #sp-card-slot (#365, no duplicate
// speech). Every write to that live region goes through paintHomeCard(), so a
// later render can tell whether painting again would be a byte-identical
// repaint of a polite region — which is the same sentence announced twice.
//
// Compared against the STRING that was written, never against
// `slot.innerHTML`: reading innerHTML back re-serializes the live DOM (a
// valueless attribute like `data-setup-progress-retry` comes back as
// `data-setup-progress-retry=""`), so a source-vs-serialized comparison never
// matches even when the paint is genuinely identical.
let homeCardPaintedHtml = null;
function paintHomeCard(slot, html) {
  slot.innerHTML = html;
  homeCardPaintedHtml = html;
}
function renderSetupProgressCard(model) {
  const state = (model && model.state) || CARD_STATE.LOADING;
  // The Home/Tasks half of the context-switch withdrawal (#365 owner
  // correction). The card's own CTA is a context-scoped mutation entry point
  // -- goToSetupWorkflow() opens a seeded create drawer or a Setup
  // destination -- so from the instant a switch is intended until it is
  // reconciled it must not be painted, whatever state the model is in. Same
  // flag, same window, same clearing points as the Setup landings'
  // (setupLandingActions). Applied by suppressing the button rather than by
  // an early return, so the retained rows/counts the operator was reading
  // stay on screen; only the action goes.
  const ctaWithdrawn = contextSwitchIntentPending;
  // Per-card loading boundary (#331 review round 2 finding 3): the caller
  // paints this skeleton immediately, before the real fetch even starts, so
  // a slow setup-progress request only delays this one card, never the rest
  // of the Dashboard (see loadSetupProgressCard()).
  //
  // #331 review round 5 finding 5: the loading state used to be a bare,
  // heading-less <div class="skeleton"> -- invisible to a screen-reader
  // user (no busy cue, no label) and unscannable by axe helpers that
  // locate this card by its own heading (KNOWN_CARD_HEADINGS in the e2e
  // suite). It now carries a real <h3> (so it's a first-class state, not
  // structurally excluded) plus a visually-hidden label; #sp-card-slot
  // itself (render()'s own wrapper, persisting across every re-render of
  // this function's output) carries the actual aria-busy/aria-live
  // semantics, so this is just the content that live region announces.
  if (state === CARD_STATE.LOADING) {
    return `<div class="dash-card sp-card" style="margin-bottom:16px">
      <div class="dash-card-head"><h3>Setup progress</h3></div>
      <div class="skeleton"><span class="sr-only">Loading setup progress…</span></div>
    </div>`;
  }
  // #365: held data whose Program/Season/League tuple is no longer the active
  // one. The numbers stay visible (they are the last thing that was true) but
  // are labelled as belonging to an earlier context and carry a refresh path;
  // the obsolete primary action is WITHDRAWN rather than left standing, so a
  // stale model can never hand the operator a CTA bound to a context they
  // have already left. Refresh is a ghost button, never .act.primary, so the
  // one-primary-action-per-screen rule holds in this state too.
  if (state === CARD_STATE.STALE) {
    // #365 review round 2 finding 4. Two defects, both of them the renderer
    // not consulting the model it was handed:
    //
    // (a) it dropped the retained rows and counts entirely, so a card whose
    //     whole justification is "the last successful read stays visible,
    //     clearly labelled" showed a label and NO retained read.
    // (b) it preferred `model.next.label` — a WORKFLOW name — for copy
    //     reading "setup progress for …", producing "setup progress for
    //     Permanent teams" instead of naming the Program the data is from.
    //     `next` is what to do next, never whose data this is; the two are
    //     different fields and only one of them can answer this sentence.
    //
    // What is retained is read-only by construction (rows and the completion
    // line carry no action hooks) and the CTA is withdrawn: Refresh is the
    // only control, a ghost button, so the one-primary-action-per-screen rule
    // holds here too.
    //
    // A held EMPTY carries no retained READ — no rows, no counts, nothing
    // this state exists to keep on screen. What EMPTY renders (see below) is
    // a CLAIM about the tuple the operator has just left: "no program yet",
    // or "nothing for your role to do HERE". Re-showing either under a
    // "showing earlier data" banner would be labelling a claim as data, and
    // the claim itself is about a context that is no longer the one selected.
    // So this state stands down and the fresh load for the new tuple paints
    // whatever is actually true there.
    if (model.staleFrom === CARD_STATE.EMPTY) return "";
    const rows = tasksWorkflowRowsHtml(model);
    // The PROGRAM the retained data belongs to, named exactly. Falls back to
    // naming the tuple generically rather than to any other field: a wrong
    // name is worse than no name.
    const from = model.program && model.program.name
      ? `<strong>${esc(model.program.name)}</strong>, the program you had
         selected earlier`
      : "the program, season and league you had selected earlier";
    return `<div class="dash-card sp-card" style="margin-bottom:16px">
      <div class="dash-card-head"><h3>Setup progress — showing earlier data</h3></div>
      <div class="banner neutral" role="status"><p>This is the setup progress for
        ${from} — not the program, season and league you have selected now.</p></div>
      ${rows ? `<div class="section-title">Setup workflows</div>${rows}` : ""}
      ${tasksProgressLine(model)}
      <div class="actions">
        <button class="act ghost" data-setup-progress-retry>Refresh setup progress</button>
      </div>
    </div>`;
  }
  if (state === CARD_STATE.ERROR) {
    // sp-card scopes the color overrides below (#331 review round 3
    // finding 4): .banner.alert's own white-on-red is a sitewide convention
    // used well below WCAG AA (~3.3:1) -- fixed here without touching the
    // shared class every other screen using .banner.alert still relies on.
    // role="alert" (#331 review round 5 finding 5): an assertive
    // announcement distinct from the outer #sp-card-slot's own polite live
    // region -- a failed fetch is more urgent/actionable than routine
    // content settling and should interrupt rather than wait its turn.
    return `<div class="dash-card sp-card" style="margin-bottom:16px">
      <div class="dash-card-head"><h3>Setup progress unavailable</h3></div>
      <div class="banner alert" role="alert"><p>Could not load your setup progress.</p></div>
      <div class="actions">
        <button class="act primary" data-setup-progress-retry>Retry</button>
      </div>
    </div>`;
  }
  // EMPTY, as a RENDERED state with its own heading and status text — one
  // body per named reason (#365 owner correction, superseding the earlier
  // #331 comments that documented silence here).
  //
  // Both reasons used to return the empty string. That left a no-Program
  // operator and a role with nothing actionable with no explanation at all,
  // and it left a keyboard operator whose Retry resolved into this state with
  // nowhere to land: the fallback focused #sp-card-slot, a zero-height
  // wrapper carrying no text and no accessible name, so focus was technically
  // off <body> and perceptually nowhere. #365 requires every applicable state
  // to render a stable semantic heading and status text, and requires an
  // empty state to explain what is missing.
  //
  // The two reasons are DIFFERENT claims and get different copy, which is the
  // whole point of having named them in the model:
  //
  //   "no_program"          no Program exists or resolves for this operator
  //                         at all, so there is no setup to have progress on.
  //   "nothing_actionable"  a Program is resolved and this ROLE's visible
  //                         slice of it has nothing left to do — which is
  //                         explicitly NOT a claim that the whole Program is
  //                         finished (get_setup_progress: a partial view can
  //                         never truthfully verify a whole-Program claim).
  //
  // ACTIONS FOLLOW THE SAME RULE THE REST OF THIS SLICE FOLLOWS: expose a
  // primary path only where one is genuinely authorized AND can actually
  // resolve the state; otherwise render guidance and NO control.
  //   * "no_program" is resolved by creating the first Program, which is the
  //     guided Initial Setup wizard's job — and onboarding.js gates that
  //     whole view on MANAGE_SETUP (a role without it is shown a "League
  //     Admin only" banner there). So the button is rendered only for
  //     MANAGE_SETUP; every other role gets the sentence that names who does
  //     it instead of a control that would dead-end.
  //   * "nothing_actionable" has, by construction, nothing for this role to
  //     act on — so it carries no control at all. "Go to Schedule" belongs to
  //     SUCCESS, where the server has actually verified completion; offering
  //     it here would imply a whole-Program claim this state cannot make.
  // Navigation-only, exactly like SUCCESS's own "Go to Schedule": not a
  // context-scoped mutation entry point, so it is not part of the
  // context-switch withdrawal (`ctaWithdrawn`) either.
  if (state === CARD_STATE.EMPTY) {
    const canBootstrap = hasPerm("manage_setup");
    if (model.reason === "nothing_actionable") {
      return `<div class="dash-card sp-card" style="margin-bottom:16px">
        <div class="dash-card-head"><h3>Setup progress — nothing for your role to do</h3></div>
        <p class="muted sp-empty-status">There is nothing left for your role to do
          in this program's setup right now.</p>
        <p class="muted">Setup workflows your role doesn't manage aren't shown on this
          card, so this isn't a claim that the whole program is finished.</p>
      </div>`;
    }
    return `<div class="dash-card sp-card" style="margin-bottom:16px">
      <div class="dash-card-head"><h3>Setup progress — no program yet</h3></div>
      <p class="muted sp-empty-status">No program has been set up yet, so there is
        no setup progress to show.</p>
      ${canBootstrap
        ? `<p class="muted">The guided Initial Setup wizard creates the first one —
             this card fills in as each setup workflow is done.</p>
           <div class="actions">
             <button class="act primary" data-goto="onboarding">Start Initial Setup</button>
           </div>`
        : `<p class="muted">A League Admin creates the first program. There is nothing
             here for your role to do until one exists.</p>`}
    </div>`;
  }
  if (state === CARD_STATE.SUCCESS) {
    // "Imports and onboarding" stays reachable even once every REQUIRED
    // workflow is done (#331 review round 2 finding 2) -- it's an
    // always-available alternative entry point (decision 9), not something
    // that should vanish once its own "optional" status is the only one
    // left. "Go to Schedule" stays the single primary action per #204's
    // one-primary-action-per-screen principle; Import data is secondary.
    // The backend now omits "import" from `workflows` entirely for a role
    // that cannot manage it (#331 review round 3 finding 5 -- MANAGE_SETUP,
    // League Admin only), so rendering the button only when found redacts
    // it for Arena Manager instead of routing them to a surface they cannot
    // use, rather than falling back to a generic always-shown label.
    // Read off the OPTIONAL partition, not by key: "the always-available
    // alternative entry point" IS the optional partition (#365), so this
    // button follows the backend's own `status: "optional"` rather than a
    // hardcoded "import" string that a renamed workflow would silently break.
    const importWf = model.part.optional[0] || null;
    // #331 review round 21 finding 2: `complete` and a workflow's own
    // `attention` are independent by design (round 19's docstring) -- a
    // Team can be genuinely, validly participating (this workflow reads
    // "done") while some OTHER row still needs cleanup. The success card
    // used to render unconditionally here whenever `complete` was true,
    // silently dropping every workflow's `attention` on the floor -- the
    // one place in this whole function that never read the field at all.
    // Reuses the exact na-row/amber markup `next_blocked` below already
    // renders in this same card, rather than introducing an unreviewed
    // color combination.
    const attentionRows = model.part.workflows.filter((w) => w.attention).map((w) => `
      <div class="na-row">
        <div class="na-ico amber">⚠️</div>
        <div class="na-body"><div class="na-title">${esc(w.label)}</div>
          <div class="na-sub">${esc(w.attention.detail)}</div></div>
      </div>`).join("");
    return `<div class="dash-card sp-card" style="margin-bottom:16px">
      <div class="dash-card-head"><h3>✓ All setup steps complete</h3></div>
      <p class="muted">Every Setup workflow is done for ${esc(model.program.name)}.</p>
      ${tasksProgressLine(model)}
      ${attentionRows}
      <div class="actions">
        <button class="act primary" data-goto="calendar">Go to Schedule</button>
        ${importWf && !ctaWithdrawn ? `<button class="act ghost" data-setup-progress-action="${esc(importWf.key)}"
          >${esc(importWf.primary_action)}</button>` : ""}
      </div>
    </div>`;
  }
  const rows = tasksWorkflowRowsHtml(model);
  const next = model.next;
  if (next) {
    // #331 review round 21 finding 2: `next` is the same workflow object
    // `rows` renders below, carrying its own `attention` when present (e.g.
    // "participation" reading "No team registered to play yet" while a
    // registration DOES exist, just ambiguously -- this workflow's own
    // attention names that instead of leaving the generic detail as the
    // only signal).
    const nextAttention = next.attention ? `
      <div class="na-row">
        <div class="na-ico amber">⚠️</div>
        <div class="na-body"><div class="na-title">${esc(next.label)}</div>
          <div class="na-sub">${esc(next.attention.detail)}</div></div>
      </div>` : "";
    return `<div class="dash-card sp-card" style="margin-bottom:16px">
      <div class="dash-card-head"><span class="dch-dot"></span><h3>Continue setup</h3></div>
      <div class="na-row">
        <div class="na-ico blue">📋</div>
        <div class="na-body"><div class="na-title">${esc(next.label)}</div>
          <div class="na-sub">${esc(next.detail)}</div></div>
      </div>
      ${nextAttention}
      ${ctaWithdrawn ? "" : `<div class="actions">
        <button class="act primary" data-setup-progress-action="${esc(next.key)}"
          >${esc(next.primary_action)}</button>
      </div>`}
      <div class="section-title">Setup workflows</div>
      ${rows}
      ${tasksProgressLine(model)}
    </div>`;
  }
  // Nothing is both permitted AND safe to execute right now (#331 review
  // round 3 finding 1). Two different reasons look the same to a naive
  // "next is null" check but must not be conflated: `next_blocked` names a
  // workflow this caller COULD act on except for an unmet Season
  // prerequisite (guidance to surface, not a CTA that would just fail), vs.
  // truly nothing left for this role while other, not-this-role's workflows
  // remain (see get_setup_progress's docstring). #365 moved that second case
  // into the model as EMPTY/"nothing_actionable", which is rendered above
  // with its own heading and explanation, so reaching here always means there
  // IS a blocked workflow to explain.
  const blocked = model.nextBlocked;
  if (!blocked) return "";
  return `<div class="dash-card sp-card" style="margin-bottom:16px">
    <div class="dash-card-head"><span class="dch-dot"></span><h3>Continue setup</h3></div>
    <div class="na-row">
      <div class="na-ico amber">⚠️</div>
      <div class="na-body"><div class="na-title">${esc(blocked.label)}</div>
        <div class="na-sub">${esc(blocked.detail)}</div></div>
    </div>
    <div class="section-title">Setup workflows</div>
    ${rows}
  </div>`;
}
// Fetches and paints the setup-progress card independently of the rest of
// the Dashboard (#331 review round 2 finding 3): render() paints an
// immediate loading skeleton into #sp-card-slot and calls this
// fire-and-forget rather than awaiting the fetch inline, so a slow request
// only delays this one card's own content, never the Dashboard's first
// paint. Also the card's own Retry action, so retrying only re-fetches
// this one thing instead of the whole Dashboard (overview/standings too).
//
// #365 replaced the single page-wide setupProgressFetchSeq with this card's
// OWN generation inside a full identity record (card id + context tuple +
// generation). The five mutation points a superseded response must not reach
// are each gated separately below, and labelled — the issue names all five,
// and a stale response that skips the DOM write but still moves focus or
// re-announces is the same defect.
async function loadSetupProgressCard(opts) {
  const identity = beginCardRequest(HOME_TASKS_CARD, opts);
  // Refused by the serialization rule (see cardWrites). Unreachable TODAY —
  // the Home/Tasks card is a pure READ and starts no write, so it never
  // registers one — and asserted rather than assumed, so this card cannot
  // become the one render-driven commit that still clobbers a PENDING write
  // if it ever grows an action of its own.
  if (!identity) return;
  // Hold the card in LOADING under the NEW identity right away, so any full
  // render() that happens while this request is in flight paints this card's
  // loading state rather than the previous context's settled numbers.
  commitCardState(identity, { state: CARD_STATE.LOADING, status: CARD_STATUS.UNKNOWN });
  // aria-busy (#331 review round 5 finding 5): re-asserted here, not just
  // left over from render()'s initial paint, so a RETRY (the slot's own
  // aria-busy already flipped back to "false" after the failed fetch that
  // preceded it) is exposed as busy again too, not just the very first load.
  const busySlot = document.getElementById("sp-card-slot");
  if (busySlot) busySlot.setAttribute("aria-busy", "true");
  const sp = await getJSON("/api/v2/setup/progress");
  // (4) completion update and (5) next-task selection. Both are computed by
  // buildTasksCardModel and stored by commitCardState, which refuses a
  // superseded identity -- so a late loser cannot revise which workflow is
  // "next" or whether the Program reads complete, even if every DOM write
  // below were somehow skipped. This is the FIRST of the five gates, not the
  // only one, deliberately: the model is where a stale answer would do the
  // most invisible damage.
  if (!commitCardState(identity, buildTasksCardModel(sp))) return;
  const slot = document.getElementById("sp-card-slot");
  if (!slot) return;  // navigated away from Dashboard before this resolved
  // Where focus is BEFORE the replacement below destroys it: an operator who
  // pressed Retry is standing on a button inside this slot, and innerHTML
  // would drop them on <body> for good.
  const hadFocusInCard = !!(document.activeElement && slot.contains(document.activeElement));
  // (1) DOM mutation and (3) live-region announcement, in one write: this
  // slot IS the card's live region (role="status" aria-live="polite" --
  // render()'s own wrapper, which persists across every replacement here), so
  // settling it is also what speaks. Deliberately NOT also toasted: that
  // would be the same sentence announced twice.
  if (!cardIdentityCurrent(identity)) return;   // #365 identity gate — DOM + announcement
  paintHomeCard(slot, renderSetupProgressCard(readCardState(HOME_TASKS_CARD)));
  slot.setAttribute("aria-busy", "false");
  const spAction = slot.querySelector("[data-setup-progress-action]");
  if (spAction) spAction.onclick = () =>
    goToSetupWorkflow(spAction.dataset.setupProgressAction);
  const spRetry = slot.querySelector("[data-setup-progress-retry]");
  // Retry/Refresh is scoped to THIS card and issues its own generation, so it
  // replaces only this card's failed generation -- nothing else on the
  // Dashboard is refetched or repainted. Flagged userInitiated so the settle
  // is allowed to move focus (below); a routine render-driven load is not.
  if (spRetry) spRetry.onclick = () => loadSetupProgressCard({ userInitiated: true });
  // (2) focus change. Only for a load a person actually asked for, and only
  // when they were standing inside this card when it was replaced -- and only
  // through focusCardTarget(), which re-checks the identity, so a superseded
  // response can never yank focus back into a card the operator has moved on
  // from.
  //
  // THE TARGET IS THE SETTLED STATE'S OWN HEADING, in all SIX states.
  //
  // This used to be true of five of them: EMPTY rendered the empty string, so
  // `.dash-card h3` found nothing, focusCardTarget() refused a null target and
  // keyboard focus was left on <body> -- Tab restarting from the top of the
  // document after an action the operator deliberately took. Reproduced as an
  // Arena Manager whose only visible workflow is already done: ERROR ->
  // keyboard Retry -> EMPTY, focus on BODY.
  //
  // The first fix for that landed focus on #sp-card-slot itself, which was
  // only half a fix and the #365 review said so: the wrapper carries no text,
  // no heading and no accessible name, and in the EMPTY state it had no box
  // either, so focus was off <body> and still nowhere a person could perceive.
  // EMPTY now renders a real heading and status text per reason (see
  // renderSetupProgressCard), so the SAME selector below lands on a visible,
  // named destination in every state this card can settle into.
  //
  // The slot is kept as the last-resort fallback rather than deleted: it is
  // render()'s own wrapper, it survives every replacement here, and a future
  // state that somehow painted no heading must still not drop focus on
  // <body>. focusCardTarget() stamps the same tabindex="-1" it uses for any
  // other non-focusable destination.
  if (opts && opts.userInitiated && hadFocusInCard) {
    focusCardTarget(identity, slot.querySelector(".dash-card h3") || slot);
  }
  // The complete state's "Go to Schedule" button uses the generic
  // data-goto convention (c.querySelectorAll("[data-goto]") in render()),
  // which only wires elements present at render()'s OWN paint -- this slot
  // didn't have this button yet then (it was still the loading skeleton),
  // so it needs the same wiring here too.
  slot.querySelectorAll("[data-goto]").forEach((b) =>
    b.onclick = () => switchTab(b.dataset.goto));
}
// Each workflow's real entry point (#330 review round 1 finding 3), not the
// generic Setup tab: season/team/player open the matching create drawer
// directly (mirrors the existing topbar "jump to Setup and open a drawer"
// shortcut at the bottom of this file — drawer state must be set BEFORE
// switchTab("setup"), since switchTab only clears `drawer` when leaving
// Setup, not when entering it); facilities opens the Ice Availability
// Builder, which renders from the Calendar view, not Setup; participation
// has no dedicated drawer (it's an inline action inside the Setup hierarchy
// tree) so it lands on that tree; import lands on the standalone Import tab.
// Drawer opens already move focus to the drawer's first field (existing
// render() behavior); the plain view switches below additionally focus the
// destination's own heading so keyboard/screen-reader users land somewhere
// meaningful, not silently at the top of the page.
async function goToSetupWorkflow(key) {
  if (key === "facilities") {
    // Bump here too (#331 review round 10), same reasoning as the manual
    // "Build ice" button's own onclick: this is a SECOND place a fresh
    // builder instance gets created, and a Preview/Commit held from a
    // PREVIOUS one (canceled, or left over from before this hub-driven
    // navigation) must not be mistaken for belonging to this new one.
    iceOperationSeq += 1;
    iceBuilder = { form: null, preview: null };
    switchTab("calendar");
    focusContentHeading();
    return;
  }
  if (key === "league_season" || key === "teams" || key === "roster") {
    const kind = key === "league_season" ? "season"
      : key === "teams" ? "team" : "player";
    // Seed the parent field from the ACTIVE Program (#331 review round 5
    // finding 4): left empty, drawerField()'s own fallback picks
    // rows[0] -- whichever Program/League/Team happens to sort first
    // GLOBALLY (these SHARED drawer fields' option lists still span every
    // Program by design, even though the operational reads behind the
    // screens themselves have been scoped to the active tuple since #367/
    // #369), not the one this hub is scoped to.
    // A valid submit against that silent wrong default would create data
    // under a DIFFERENT Program than the one the operator is acting from.
    // Awaited BEFORE opening the drawer (a single, cheap fetch) rather
    // than reading the module-level permLeaguesByProgram/leagueTeams
    // caches, which are only populated as a side effect of the Setup
    // view's OWN last render and can be stale or entirely unpopulated at
    // the moment this hub CTA is clicked straight from the Dashboard.
    //
    // Fail CLOSED, not open (#331 review round 6): a hierarchy-fetch
    // failure, or the active Program changing mid-flight (the operator
    // uses the unrelated context switcher while this await is still
    // resolving), must never open the drawer with drawerValues left at
    // {} -- that's exactly what re-triggers drawerField()'s own first-
    // GLOBAL-option fallback, recreating the wrong-Program write risk
    // this whole fix exists to close. mySeq guards against a NEWER
    // goToSetupWorkflow call (a rapid re-click) superseding this one;
    // contextSeededDrawerValues() itself separately guards against the
    // context SWITCHER changing contextOptions.selected while its own
    // fetch is in flight (a different trigger, so a different check).
    const mySeq = ++drawerSeedFetchSeq;
    const seeded = await contextSeededDrawerValues(kind);
    if (mySeq !== drawerSeedFetchSeq) return;  // a newer navigation already won
    if (!seeded.ok) {
      toast = "Couldn't load what's needed to open that — try again.";
      toastIsError = true;
      return render();
    }
    drawer = { kind }; drawerError = ""; drawerValues = seeded.values;
    switchTab("setup");
    return;
  }
  if (key === "participation") {
    setupView = "hierarchy";
    switchTab("setup");
    focusParticipationRegisterControl();
    return;
  }
  if (key === "import") {
    switchTab("import");
    focusContentHeading();
    return;
  }
  switchTab("setup");
  focusContentHeading();
}
// Monotonic guard for the two async drawer-seeding steps below (#331 review
// round 6), mirroring setupProgressFetchSeq's own pattern for the same
// class of problem: a rapid re-click of a hub CTA before its first
// in-flight seed fetch resolves must never let the OLDER call's result win
// and open a (by then stale) drawer after a newer navigation already did.
let drawerSeedFetchSeq = 0;
// The correct parent-field seed for a hub-driven create drawer (#331 review
// round 5 finding 4), scoped to the ACTIVE Program (#159's
// contextOptions.selected) via a FRESH, targeted fetch of the canonical
// Program->League->Team hierarchy -- NOT the module-level
// permLeaguesByProgram/leagueTeams caches (only populated as a side effect
// of the Setup view's OWN last render, so stale or entirely empty when this
// hub CTA is clicked straight from the Dashboard), and NOT a change to the
// shared drawer field definitions' own option lists, which stay
// global/unfiltered (the general Setup screen's own "+ Add" entry points
// are not Program-scoped by design -- a deliberate exception that survived
// #367/#369 scoping the operational reads behind the screens themselves).
// Seeding only the DEFAULT
// selected value is enough to close the actual bug -- a silent wrong-
// Program default on first open -- while an operator who deliberately wants
// a different Program can still change the select same as always.
//
// Returns {ok, values}, not a bare values object (#331 review round 6):
// {ok: true, values: {}} means "resolved cleanly, nothing to seed" (a
// legitimate empty state -- no active Program, or the active Program
// genuinely has no candidate of its own yet, e.g. a fresh Program with zero
// permanent Leagues/Teams so far; drawerField() falls back to its ordinary
// first-option behavior, or its own "create one first" empty state, same as
// any other entry point into the same drawer). {ok: false} means "do NOT
// open the drawer at all" -- either the hierarchy fetch itself failed
// (network/server error, surfaced via getJSON's own {error} shape), or the
// active Program changed while this fetch was in flight (the operator used
// the context switcher mid-request): re-checking programId AFTER the await,
// against the value captured BEFORE it, is the point -- that captured value
// is exactly what could have gone stale out from under this call. Silently
// falling through to {} in either failure case would open the drawer with
// nothing seeded, hitting drawerField()'s own first-GLOBAL-option fallback
// -- exactly the wrong-Program write risk this whole fix exists to close,
// just moved one layer deeper.
async function contextSeededDrawerValues(kind) {
  const programId = contextOptions && contextOptions.selected
    && contextOptions.selected.program_id;
  // Captured BEFORE the await below (#331 review round 8), compared after it
  // against contextRevision -- not contextOptions.selected.program_id, which
  // setActiveContext() doesn't update until ITS OWN POST succeeds. A switch
  // merely ATTEMPTED (not yet confirmed) while this fetch was in flight used
  // to still read as "unchanged" here and let this seed win; contextRevision
  // now bumps the instant a switch is attempted (setActiveContext()'s own
  // first bump, before its POST), so comparing against it closes that gap.
  const seededRevision = contextRevision;
  // Kinds whose parent select spans every Program/Season. For these there is
  // no safe "open it unseeded" outcome: drawerField()'s `current || rows[0][0]`
  // fallback would pick the first GLOBAL row, so a missing or unresolvable
  // active context must fail CLOSED rather than returning ok with {} and
  // letting the drawer open on a global guess.
  const CONTEXT_BOUND = ["level", "division"];
  const mustBind = CONTEXT_BOUND.indexOf(kind) !== -1;
  if (!programId) {
    return mustBind ? { ok: false, needsContext: true } : { ok: true, values: {} };
  }
  if (kind === "season") return { ok: true, values: { "f-season-league": programId } };
  const hvr = await getJSON("/api/v2/setup/hierarchy");
  const stillCurrent = contextRevision === seededRevision;
  if (!hvr || hvr.error || !stillCurrent) return { ok: false };
  const program = (hvr.programs || []).find((p) => p.id === programId);
  // Selected Program not present in the hierarchy the server just returned --
  // a stale or mismatched context. Same rule: fail closed for the
  // context-bound kinds instead of falling through to a global default.
  if (!program) {
    return mustBind ? { ok: false, needsContext: true } : { ok: true, values: {} };
  }
  if (kind === "team") {
    const lgs = program.leagues || [];
    // #364: prefer the operator's active League selection as the default
    // (still an ordinary, changeable <select> field, unlike Division's
    // fail-closed LeagueSeason pairing above) -- falls back to the first
    // League when none is active or the active one isn't under this
    // Program, same as before this change.
    const activeLeagueId = contextOptions.selected && contextOptions.selected.league_id;
    const preferred = activeLeagueId && lgs.find((lg) => lg.id === activeLeagueId);
    const league = preferred || lgs[0];
    return { ok: true, values: lgs.length ? { "f-team-perm-league": league.id } : {} };
  }
  if (kind === "player") {
    const teams = (program.leagues || []).flatMap((lg) => lg.teams || [])
      .concat(program.teams_without_league || []);
    return { ok: true, values: teams.length ? { "f-player-team": teams[0].id } : {} };
  }
  // #345 batch 2 review blocker: the Setup workflow landings expose secondary
  // create actions whose parent <select> spans EVERY Program (these shared
  // drawer option lists are deliberately left global -- #367/#369 scoped the
  // operational READS, not the shared create-drawer field definitions).
  // Opened with drawerValues = {}, drawerField()'s `current || rows[0][0]`
  // fallback selects the first GLOBAL row -- so a valid submit persists the
  // record under a different Program than the landing the operator is acting
  // from. Same wrong-Program write risk rounds 5/6 closed for the hub-driven
  // paths, reintroduced by copying Records' unseeded "+ New" onto a
  // context-scoped surface.
  if (kind === "level") {
    // A League hangs off a Season. Seed from the ACTIVE Season in the context
    // selection rather than from the hierarchy payload: it is the
    // authoritative value the operator actually chose, and it avoids
    // depending on a per-program `seasons` field this payload is not
    // confirmed to carry -- an absent field would fall through to `{}` and
    // silently restore the first-global fallback this fix exists to close.
    // No active Season means there is nothing correct to seed, so fail closed
    // rather than open the drawer on a global guess.
    const seasonId = contextOptions && contextOptions.selected
      && contextOptions.selected.season_id;
    if (!seasonId) return { ok: false, needsSeason: true };
    return { ok: true, values: { "f-level-season": seasonId } };
  }
  if (kind === "division") {
    // A Division hangs off a LeagueSeason -- a League paired with a SEASON --
    // so seeding the Program's first permanent League ignores which Season is
    // active and lets a Division be created under a League that is not in it.
    // Bind to the active Season instead, using the overview payload's
    // per-league `season_ids` (#345 -- NOT the older singular `season_id`,
    // which is only the FIRST binding `get_setup_overview_v2` happened to see
    // and would silently miss a League that also participates in the active
    // Season through a LATER binding).
    const seasonId = contextOptions && contextOptions.selected
      && contextOptions.selected.season_id;
    if (!seasonId) return { ok: false, needsSeason: true };
    const svr = await getJSON("/api/v2/setup/overview");
    if (!svr || svr.error || contextRevision !== seededRevision) return { ok: false };
    const inSeason = (svr.leagues || [])
      .filter((lg) => (lg.season_ids || []).indexOf(seasonId) !== -1);
    if (!inSeason.length) return { ok: false, noLeagueInSeason: true };
    // #364: prefer the operator's own ACTIVE League selection over an
    // arbitrary first match, the same "never invent an unrelated choice"
    // rule the context service itself applies (ContextService's own League
    // resolution drops to None rather than guessing). A League selected in
    // context that is genuinely not IN this season's own eligible set is a
    // stale/mismatched selection -- fail closed rather than silently
    // substituting a DIFFERENT League than the one the operator has active,
    // which would be exactly the "committed under a League they didn't
    // choose" defect this whole seeding path exists to prevent.
    const activeLeagueId = contextOptions.selected.league_id;
    let league = inSeason[0];
    if (activeLeagueId) {
      league = inSeason.find((lg) => lg.id === activeLeagueId) || null;
      if (!league) return { ok: false, leagueNotInSeason: true };
    }
    // f-div-season carries the exact active Season through to submit (#345),
    // so a League bound to several Seasons commits into this one instead of
    // the ambiguous legacy sole-binding path.
    return { ok: true, values: { "f-div-league": league.id, "f-div-season": seasonId } };
  }
  // Rink and ice-slot deliberately fall through to the unseeded return below.
  // Their parents are a Venue and a Rink -- shared facilities with no Program
  // axis -- so an unseeded default cannot produce the cross-PROGRAM write this
  // seeding exists to prevent, and there is no active-context value to bind
  // them to. An earlier revision failed these closed, which rendered two
  // controls that could never succeed; a dead control is a worse outcome than
  // an unseeded one. Season-scoped venue access (SeasonVenueAccess) would be
  // the real axis to bind to and is not wired into the context bar yet.
  return { ok: true, values: {} };
}
// WHICH focus request is the CURRENT one (#365 review round 12).
//
// focusContentHeading() below is a POLL -- a chain of setTimeout(..., 50) up
// to 40 attempts that ends in an unconditional #content landing -- and until
// this counter existed, that chain belonged to nobody. It kept running after
// the navigation that started it had been replaced, and then landed focus on
// behalf of an operator who had already asked to go somewhere else. Recorded
// as a real browser-journey failure at 390px ("the resolution path did not
// focus the Allow picker; focus is on {id: content}"), and traced there
// verbatim -- the nav click's poll firing 32ms AFTER the deep link it was
// racing had already kept its promise:
//
//     19ms  focusContentHeading {attempt: 0, exit: "poll"}   <- nav to the
//                                  Facilities landing; #content is skeletons
//     38ms  render:end (setup/hub)                           <- landing paints
//     60ms  focusin BUTTON                                   <- the operator
//                                  activates "Allow a venue for this season"
//     61ms  requestDestinationFocus setupHierarchy           <- NEWER request
//     70ms  focusContentHeading {attempt: 1, exit: "poll"}   <- still running
//     89ms  focusin va-add-season_2/SELECT                   <- promise KEPT
//    121ms  focusContentHeading {attempt: 2, exit: "content-early"}
//    121ms  focusin content/DIV                              <- and STOLEN
//
// The theft is silent and permanent: the intent is already spent, so nothing
// puts focus back, and a keyboard/screen-reader operator who asked to be
// taken to the grant control is left at the top of the page instead. Note the
// exit it takes -- "content-early", not the 2s floor. The Setup hierarchy
// tree has no heading of its own (its `headings` count inside #content is
// literally 0), which is exactly the case the fallback below was written for;
// so the older poll does not even have to run out of budget to land on
// #content, it only has to tick once after the newer destination has painted.
//
// SO EVERY FOCUS REQUEST TAKES A TICKET, and a poll that no longer holds the
// current one stops where it stands. Two kinds of request take one:
// focusContentHeading() itself (a second navigation supersedes the first --
// the operator asked for the newer destination) and requestDestinationFocus()
// (a deep link is a focus request too, and a more specific one). This is the
// same supersession discipline the rest of #365 applies to every other async
// mutation -- cardGenerations for card writes, contextRevision for renders,
// drawerSeedFetchSeq for drawer seeds -- and it is here for the same reason:
// an older async operation must never get to answer for a newer one.
//
// WHAT IT DELIBERATELY DOES NOT DO is weaken the fallback for the caller that
// is genuinely current. A poll holding the current ticket behaves EXACTLY as
// before, floor included; only a superseded one is dropped, and it is dropped
// silently because the newer request is what owns focus now.
//
// Nor does the ticket run the other way, cancelling a standing destination
// intent when a newer focusContentHeading() lands: an intent already carries
// a far stronger binding than a ticket (principal + session epoch + context
// tuple + view + sub-view + "no dialog is open", all re-checked at
// settlement), so it cannot fire onto a surface the operator has left. The
// generic poll has no binding of any kind -- it focuses whatever #content
// happens to hold whenever it happens to tick -- which is precisely why it,
// and only it, needs one.
//
// AND THE BOUNDARIES BUMP IT TOO (#365 round 13). Round 12 read the sentence
// above too narrowly: it gave the poll a binding to newer REQUESTS and
// stopped there, which closed the reported race and left the identical stale
// work alive across the two boundaries the rest of this slice already
// defends. A poll started under principal A / tuple X survived both a
// principal or session-epoch change (resetTransientUiState) and a confirmed
// context switch (sendContextSwitch's success path, where
// contextOptions.selected moves), and could still fire afterwards -- landing
// #content on a surface belonging to an identity or a tuple that never asked
// for it. The intent above is dropped at exactly those two points for exactly
// that reason; the ticket is now bumped at the same two points, and since a
// newer ticket is all the poll can be told, that bump IS its cancellation.
// Deliberately silent: the crossing focuses nothing on the arriving surface
// on the departing one's behalf. And deliberately cheap for the arriving
// identity -- a focusContentHeading() called after the bump holds the current
// ticket and behaves exactly as before, floor included.
let focusRequestSeq = 0;
function newFocusRequest() { return ++focusRequestSeq; }

// Best-effort focus landing for a plain view switch (no drawer of its own to
// auto-focus) — the first heading-ish element in the freshly rendered view,
// falling back to the #content region itself for a destination with no
// heading of its own (e.g. the Setup hierarchy tree), so focus always lands
// somewhere real rather than silently staying nowhere. switchTab() kicks off
// render() without awaiting it, and render() itself is async (it awaits its
// own overview fetch) — so neither target necessarily exists yet on the very
// next tick. Poll briefly instead of a single setTimeout(0), which raced
// that fetch and could fire before the real content painted (#331 review
// round 1 finding 4).
//
// Takes a fresh ticket per CALL, not per attempt: the whole chain of attempts
// is one request, and it is the request that gets superseded, never an
// individual tick.
function focusContentHeading() {
  focusContentHeadingAttempt(0, newFocusRequest());
}

function focusContentHeadingAttempt(attempt, request) {
  // SUPERSEDED (#365 round 12): a newer focus request exists, so this chain
  // is answering for a navigation the operator has already left behind.
  // Returns without focusing ANYTHING -- including without taking the floor
  // below, which is the entire point: the floor is a promise that focus will
  // not be left nowhere, and when a newer request is live focus is not
  // nowhere, it is wherever that request has put it or is about to.
  if (request !== focusRequestSeq) return;
  const content = document.getElementById("content");
  const heading = content && content.querySelector(
    "h1, h2, h3, .section-title");
  if (heading) { heading.setAttribute("tabindex", "-1"); heading.focus(); return; }
  const stillLoading = content && content.querySelector(".skeleton");
  if (content && !stillLoading && content.firstElementChild) {
    content.setAttribute("tabindex", "-1");
    content.setAttribute("aria-label", "Page content");
    content.focus();
    return;
  }
  if (attempt < 40) {
    setTimeout(() => focusContentHeadingAttempt(attempt + 1, request), 50);
    return;
  }
  // Poll exhausted (40 x 50ms = 2s). It used to simply return here, which
  // silently LOST focus: on a loaded machine the destination view can still
  // be fetching after two seconds, so the caller -- typically the dialog
  // close path restoring focus when the trigger itself was removed -- was
  // left with focus on <body>. Reproduced as a real CI failure on
  // browser-smoke shard 2 ("focus restore (removed trigger): focus was left
  // on <body> instead of the view fallback"), passing locally where the
  // render lands well inside the budget.
  //
  // #content is always present and carries tabindex="-1" (added with the
  // skip link), so it is a guaranteed floor. Taking it is strictly better
  // than <body>: focus is inside the main region, the next Tab continues
  // from there, and a screen reader announces the region rather than
  // nothing. Deliberately unconditional -- a slow render must never end with
  // focus nowhere.
  //
  // Still unconditional after the supersession check above (#365 round 12),
  // and reaching this line is exactly what proves the request is the current
  // one: nothing newer has been asked for, so "focus is nowhere" is a real
  // possibility and this is the answer to it. The only case that no longer
  // gets here is the one where focus is demonstrably NOT nowhere, because a
  // newer request owns it. e2e/facilities-venue-access.js asserts both halves
  // -- (F) that a superseded poll never fires, (F2) that a current one still
  // takes this floor when its destination is still loading.
  if (content) {
    content.setAttribute("tabindex", "-1");
    content.setAttribute("aria-label", "Page content");
    content.focus();
  }
}

// ===== DESTINATION FOCUS INTENTS (#365 review round 11) ==================
//
// A Setup DEEP LINK promises to land the operator ON one specific control --
// the selected Season's "Register" add row (participation), or its "Allow a
// venue" picker (the control that actually creates the missing
// SeasonVenueAccess) -- and that control does not exist at click time.
// switchTab() kicks off an ASYNC render() whose Setup reads (setup overview,
// players, the canonical hierarchy, per-Season registrations, the selected
// Season's venue-access and grant candidates, setup progress) all have to
// land before the tree paints once.
//
// WHAT THIS REPLACES, AND WHY A BIGGER BUDGET WAS NEVER THE ANSWER. Both deep
// links used to POLL: every 50ms, up to 200 attempts (10s) while a skeleton
// was still up, then hand off to focusContentHeading(), whose own 40x50ms
// (2s) poll ends in an UNCONDITIONAL #content landing -- a combined ~12s
// budget, after which the promise silently degraded to "somewhere in the page
// region". A budget is a GUESS about how long a render takes, and every guess
// is wrong under enough load: the recorded 390px failure ("the resolution
// path did not focus the Allow picker; focus is on {id: content}") is that
// guess losing, with a keyboard/screen-reader operator told they were being
// taken to the control that grants venue access and stranded at the top of
// the page instead -- silently, because taking the floor looks exactly like
// arriving. Widening the budget only moves the boundary; it cannot remove it.
//
// SO THE INTENT IS RESOLVED BY AN EVENT, NOT BY ELAPSED TIME. render() calls
// settleDestinationFocus() at each point where a pass has CONCLUDED what the
// destination shows, and the intent resolves there in exactly one of three
// ways:
//
//   (1) SUPERSEDED -- the authenticated principal/session epoch or the active
//       context tuple has moved since the intent was registered. CANCELLED,
//       focusing NOTHING. The intent was "take THIS operator to THIS Season's
//       grant control"; under another principal or another tuple there is no
//       such promise left to keep, and keeping it would yank the arriving
//       operator's focus onto a control they never asked for. Same identity
//       discipline as every other #365 gate -- see cardIdentitySamePrincipal()
//       and currentCardTuple() -- and deliberately the same two halves.
//   (2) THE CONTROL EXISTS -- focus it. The promise kept, however long the
//       destination took.
//   (3) THE SETTLED DESTINATION PROVES IT CANNOT EXIST -- fall back. This is
//       a CONCLUSION, not a guess: `proof` says the pass that just painted is
//       the one that performed this destination's own reads, so what it
//       painted is what this Season HAS. renderSeasonParticipation() renders
//       explanatory copy instead of a picker for a Season with no grantable
//       venue left ("Every venue is already allowed", "Create a venue on the
//       Facility tree first"), for an archived read-only selection, and
//       nothing at all for a role without MANAGE_SETUP -- in every one of
//       those the control genuinely cannot appear later, so the generic
//       content-region landing is the right answer rather than a surrender.
//
// ...and until one of those three holds, the intent simply STAYS ALIVE for
// the next settlement. There is no tick count, no deadline and no timer
// anywhere on this path: "not yet" can never convert itself into "give up".
// The one call into focusContentHeading() is made only at (3) -- at a
// destination that has already settled, where its first attempt finds either
// a heading or painted content and it therefore never reaches its own poll.
let destinationFocusIntent = null;

// WHOSE intent this is, and for WHICH context. Identical in shape and
// intention to cardIdentitySamePrincipal() + cardIdentityCurrent()'s tuple
// half; generation has no meaning here (a focus intent belongs to a
// navigation, not to a card request), so it is deliberately absent rather
// than faked.
function destinationFocusIntentCurrent(intent) {
  if (!intent) return false;
  if (intent.epoch !== uiIdentityEpoch) return false;
  if (intent.principal !== cardPrincipalId()) return false;
  const t = currentCardTuple();
  return intent.program_id === t.program_id
    && intent.season_id === t.season_id
    && intent.league_id === t.league_id;
}

// Registered by the deep link, stamped with the identity that asked for it.
// Deliberately NOT resolved here: every caller has just called switchTab(),
// so a render for the destination is already in flight and the control on
// screen right now (if any) belongs to the surface being left behind.
function requestDestinationFocus(spec) {
  // A deep link IS a focus request, and the newest one (#365 round 12): the
  // operator has just activated a control that promises to take them to ONE
  // named destination. Taking the ticket here is what stops the navigation
  // that got them to this screen from finishing its own generic landing on
  // top of that promise a fraction of a second later -- the recorded
  // "focus is on {id: content}" failure, traced at newFocusRequest() above.
  //
  // Taken BEFORE the intent is stored rather than after, so there is no
  // instant at which an intent exists while an older poll still holds the
  // current ticket.
  newFocusRequest();
  const t = currentCardTuple();
  destinationFocusIntent = {
    view: spec.view, setupView: spec.setupView || null,
    provenBy: spec.provenBy, find: spec.find,
    epoch: uiIdentityEpoch, principal: cardPrincipalId(),
    program_id: t.program_id, season_id: t.season_id, league_id: t.league_id,
  };
}

// The identity boundary's own half of (1). The settlement check below is the
// authoritative one -- nothing can be focused without passing it -- but an
// intent whose principal or tuple has already moved is dead the moment that
// happens, and leaving it in place until some later render happens to settle
// would be holding a promise on behalf of an operator who is gone. Called
// from resetTransientUiState() (principal/session epoch) and from the
// confirmed context switch (tuple).
function cancelSupersededDestinationFocus() {
  if (destinationFocusIntent
      && !destinationFocusIntentCurrent(destinationFocusIntent)) {
    destinationFocusIntent = null;
  }
}

// The ATTEMPTED-switch boundary, which is earlier than the confirmed one and
// is the one that matters (#365 owner correction, round 13).
//
// cancelSupersededDestinationFocus() is deliberately NOT what runs here, and
// could not be: it asks whether the intent still matches the canonical tuple,
// and at the moment a switch is ATTEMPTED that tuple has not moved yet --
// contextOptions.selected is only updated once /api/context answers. The old
// intent therefore still looks perfectly current, so a "cancel if superseded"
// test cancels nothing. Meanwhile the operator has already made the new
// selection in the native control and is looking at it.
//
// So this is unconditional. Anything focus-related that was asked for under
// the tuple being left is void the instant the operator asks for another one,
// whether or not the server has caught up, and whether or not this particular
// call goes on to be queued behind an in-flight switch. The generic poll gets
// the same treatment through the ticket -- its next attempt sees a newer
// request and returns without focusing anything.
//
// Cancelled means SILENT, never redirected: nothing here focuses anything on
// the arriving surface. The arriving tuple's own render decides that.
function abandonFocusWorkForContextSwitch() {
  newFocusRequest();
  destinationFocusIntent = null;
}

// THE settlement. `proof` names what the concluding render pass actually
// established -- see render()'s own call sites.
function settleDestinationFocus(proof) {
  const intent = destinationFocusIntent;
  if (!intent) return;
  // (1) identity. #365 identity gate — principal/session epoch + context tuple.
  if (!destinationFocusIntentCurrent(intent)) { destinationFocusIntent = null; return; }
  // The operator LEFT the destination (a nav click, a sub-view toggle) while
  // it was still loading. Nothing to keep: focus belongs to wherever they
  // went, and this intent must not follow them there.
  if (view !== intent.view
      || (intent.setupView && setupView !== intent.setupView)) {
    destinationFocusIntent = null;
    return;
  }
  // An open dialog OWNS focus (see syncOverlayFocus's lifecycle). A deep-link
  // promise cannot outrank a focus trap the operator is inside, and pulling
  // focus out of one would be a worse defect than the one this fixes.
  if (openOverlayElement()) { destinationFocusIntent = null; return; }
  // (2) the control exists.
  const el = intent.find();
  if (el) {
    destinationFocusIntent = null;
    // Same tabindex="-1" convention focusCardTarget()/focusContentHeading()
    // use, and the same refusal to stamp it on something already focusable.
    if (!el.hasAttribute("tabindex")
        && !/^(BUTTON|A|INPUT|SELECT|TEXTAREA)$/.test(el.tagName)) {
      el.setAttribute("tabindex", "-1");
    }
    el.focus();
    return;
  }
  // (3) settled, and it is not there -- so it cannot be.
  if (proof && proof[intent.provenBy]) {
    destinationFocusIntent = null;
    focusContentHeading();
    return;
  }
  // NOT YET: this pass settled something else (or nothing this intent's
  // destination depends on). Keep waiting. No counter is touched here,
  // because there is no counter.
}

// Destination focus for "participation" (#331 review round 2 finding 4):
// landing generically on the Setup hierarchy tree isn't enough -- focus
// must reach the ACTUAL registration control for the currently-selected
// Season (contextOptions.selected.season_id, #159), the same "Register" add
// row renderSetupHierarchy's league sections render per (season, league)
// (data-reg-add/data-reg-add-season).
//
// Settlement-bound for the same reason the venue-access deep link below is,
// and it had the identical time-based give-up shape (the review's audit of
// the other deep-link focus helpers found exactly this one, and nothing
// else): the same 200 x 50ms poll into the same focusContentHeading() floor,
// on the same slowest-loading Setup surface. It is the same defect, so it
// gets the same fix rather than a second one.
function focusParticipationRegisterControl() {
  requestDestinationFocus({
    view: "setup", setupView: "hierarchy", provenBy: "setupHierarchy",
    find: () => {
      const seasonId = contextOptions && contextOptions.selected
        && contextOptions.selected.season_id;
      return (seasonId && document.querySelector(
        `[data-reg-add][data-reg-add-season="${CSS.escape(seasonId)}"]`)) || null;
    },
  });
}

// The same deep-link, for the selected Season's "Allow a venue" picker (#365
// review, Facilities fail-open). This is the control that actually creates
// the missing SeasonVenueAccess, so an operator sent here from the Facilities
// card lands ON it rather than at the top of the hierarchy tree.
//
// Prefers the <select> (the field that must be filled before the Allow button
// can do anything) and falls back to the Allow button itself.
//
// THE SEASON IS READ LIVE, from contextOptions.selected, and that is safe for
// exactly one reason: the identity gate in settleDestinationFocus() has
// already refused every tuple except the one this intent was registered
// under, so "the live selected Season" and "this intent's own Season" are the
// same value by the time this runs. Delete that gate and they are not: the
// intent would reach across a context switch and focus a picker belonging to
// a Season the operator moved to but never asked to be sent to -- which is
// what e2e/facilities-venue-access.js's superseded-context leg proves.
function focusVenueAccessControl() {
  requestDestinationFocus({
    view: "setup", setupView: "hierarchy", provenBy: "setupHierarchy",
    find: () => {
      const seasonId = contextOptions && contextOptions.selected
        && contextOptions.selected.season_id;
      if (!seasonId) return null;
      // getElementById, not a selector: the id embeds a raw Season id.
      return document.getElementById(`va-add-${seasonId}`)
        || document.querySelector(`[data-va-add="${CSS.escape(seasonId)}"]`);
    },
  });
}

function renderDashboard(ov, standings) {
  // A coach's "what needs attention" is their own team, not the whole
  // league (#145 research: coaches landed on the same league-wide operator
  // dashboard as League Admin, with nothing scoped to them). Every stat/
  // alert below derives from `games`, so filtering it here is enough —
  // no separate coach-specific dashboard to build or keep in sync.
  const coachTeamId = (currentRole === "coach" && currentUser
    && currentUser.scope && currentUser.scope.team_id) || null;
  const games = coachTeamGames(ov, coachTeamId);
  const today = games.filter((g) => dayOf(g.start_time) === calendarDate);
  const todayList = today.length ? today : games;   // fall back to all if none "today"
  // "Games this week" (#118 Phase 7) means the 7-day window starting today —
  // ov.schedule carries every non-draft game the read returns, over the WHOLE
  // date range, so counting its full length mislabeled arbitrarily-far-past/
  // future games as "this week". (This comment used to say "every non-draft
  // game in the whole demo". That stopped being true at #367/#369:
  // get_demo_overview now excludes any game failing its active-tuple
  // `_in_scope_game` predicate. The date bug this guards against is unchanged
  // either way — a Season spans months, so its own games are still not all
  // "this week" — but the stated reason was a lie about the payload.)
  const weekEnd = addDays(calendarDate, 6);
  const weekGames = games.filter((g) => {
    const d = dayOf(g.start_time);
    return d >= calendarDate && d <= weekEnd;
  });
  const upcoming = weekGames.length - today.length;

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

  // Ice slots are shared arena infrastructure, not owned by one team — there
  // is no correct team-scoped version of this stat (review finding for
  // #145), so it's dropped entirely for a coach rather than shown
  // unscoped alongside three team-scoped tiles it would visually imply.
  const iceStat = coachTeamId ? "" : stat("Ice slots booked", booked,
    `${utilPct}% of ${slots.length} slots`, pill("gray", `${utilPct}%`));
  const stats = `<div class="dash-stats">
    ${stat("Games this week", weekGames.length, `${today.length} today · ${upcoming} upcoming`,
           today.length ? pill("green", `+${today.length}`) : "")}
    ${iceStat}
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
    return `<div class="tg-row" data-open-sheet="${esc(g.game_id)}" role="button" tabindex="0"
        aria-label="Open game sheet: ${esc(g.home_team_name)} vs ${esc(g.away_team_name)}">
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
  const gamesTitle = (today.length ? "Today's Games" : "Scheduled Games")
    + (coachTeamId ? " · Your Team" : "");
  const gamesCard = `<div class="dash-card">
    <div class="dash-card-head"><h3>${esc(gamesTitle)}</h3>
      <span class="dch-sub">${todayList.length} game${todayList.length === 1 ? "" : "s"}</span>
      <button class="linklike dch-link" data-goto="games">Games →</button></div>
    ${gameRows || (coachTeamId
      ? '<div class="na-empty">No games scheduled yet for your team.</div>'
      : '<div class="na-empty">No games scheduled yet.</div>')}</div>`;

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
      <button class="linklike dch-link" data-goto="standings">All →</button></div>
    ${ssRows || '<div class="na-empty">No games played yet.</div>'}</div>` : "";

  // Coach action card (#146): the coach dashboard research for #145 scoped
  // the existing operator stats/alerts to the coach's team but stopped
  // there — a coach still had to leave the dashboard to see whether their
  // next game's roster is actually fillable. This surfaces the same
  // availability-summary/substitute-candidates data the Roster tab already
  // shows, but for the coach's single next game, with a link to go act on it.
  const nextGame = coachTeamId ? nextUpcomingGame(games) : null;
  const actionCard = nextGame ? renderCoachActionCard(nextGame) : "";
  const linksCard = coachTeamId ? renderCoachQuickLinks() : "";

  return `${stats}
    <div class="dash-grid">
      <div>${gamesCard}</div>
      <div style="display:flex;flex-direction:column;gap:16px">${attentionCard}${actionCard}${standingsCard}${linksCard}</div>
    </div>`;
}

function renderCoachActionCard(nextGame) {
  const av = dashAvailability;
  const sq = dashSubQueue;
  const availLine = av
    ? `${av.counts.available} available · ${av.counts.unavailable} unavailable · ${av.counts.maybe} maybe · ${av.counts.no_response} no response`
    : "Loading…";
  const availWarn = av && (av.counts.unavailable + av.counts.no_response) > 0;
  const openSlots = sq ? sq.open_goalie_slots + sq.open_skater_slots : 0;
  const subLine = !sq ? "Loading…"
    : openSlots === 0 ? "No open roster slots for this game."
    : `${openSlots} open slot${openSlots === 1 ? "" : "s"} · ${sq.candidates.length} substitute${sq.candidates.length === 1 ? "" : "s"} available to offer`;
  return `<div class="dash-card">
    <div class="dash-card-head"><h3>Next Game · Roster &amp; Subs</h3>
      <button class="linklike dch-link" data-goto="roster">Roster →</button></div>
    <div class="na-row"><div class="na-ico blue">🗓️</div>
      <div class="na-body"><div class="na-title">${esc(nextGame.home_team_name)} vs ${esc(nextGame.away_team_name || "TBD")}</div>
        <div class="na-sub">${esc(fmtDateTime(nextGame.start_time))}${nextGame.rink_name ? " · " + esc(nextGame.rink_name) : ""}</div></div></div>
    <div class="na-row"><div class="na-ico ${availWarn ? "amber" : "green"}">👥</div>
      <div class="na-body"><div class="na-title">Availability</div><div class="na-sub">${esc(availLine)}</div></div></div>
    <div class="na-row"><div class="na-ico ${openSlots > 0 ? "red" : "green"}">🔁</div>
      <div class="na-body"><div class="na-title">Substitute queue</div><div class="na-sub">${esc(subLine)}</div></div></div>
  </div>`;
}

function renderCoachQuickLinks() {
  return `<div class="dash-card">
    <div class="dash-card-head"><h3>Quick Links</h3></div>
    <div class="dash-card-links">
      <button class="act ghost" data-goto="roster">Roster</button>
      <button class="act ghost" data-goto="games">Games</button>
      <button class="act ghost" data-goto="notifications">Notifications</button>
      <button class="act ghost" data-goto="notifications">📅 Subscribe to calendar</button>
    </div>
  </div>`;
}

/* ---------- Setup ---------- */
// get_setup_overview_v2's Club/Organization/Official/Venue/Rink/IceSlot
// lists are STRICTLY scoped to the active Program/Season/League (a record
// with no validated chain into it is omitted, never shown globally) -- but
// a freshly-created record of one of these kinds has NO chain to ANY
// Program yet (a just-created Club has no Team; a just-created Venue has no
// SeasonVenueAccess grant), so it would otherwise vanish from its own "just
// created this" card and from every picker that exists to link it to
// something.
//
// #367 owner ruling: an unlinked record is NOT thereby "nobody's data", so
// the old installation-wide `unassigned_<key>` buckets are gone -- they let
// any operator enumerate every never-linked record in the installation,
// Officials' personal names included. What the backend returns instead is
// `pending_link_<key>`: the unlinked records THIS caller created, per the
// audit trail. Every list/options function below that reads one of these
// six kinds unions the scoped list with its own pending-link counterpart
// through this one helper, so the operator running a create-then-link flow
// keeps seeing their fresh record right up until its first real link
// narrows it -- and nobody else ever sees it at all.
function withPendingLink(sv, key) {
  return (sv[key] || []).concat((sv["pending_link_" + key]) || []);
}

// The ACTIVE Program's authorized Seasons, from the context bar's own
// options payload (/api/context/options -- filtered through the same
// context_scope rules as every other read, and already rendered in
// #ctx-select on this page).
//
// #367 owner ruling: the Setup surface is ceilinged on the ACTIVE Season, so
// `sv.seasons` holds exactly one Season (or none, in a Program-only
// context). That is right for what the surface SHOWS, and wrong for the one
// control that has to point AT a Season the operator is not currently in:
// the League create drawer's Season picker. Reading it from here keeps the
// create-then-link flow alive without widening any scoped read, and without
// disclosing a Season the context bar would not already offer.
function contextSeasonOptions() {
  const sel = contextOptions && contextOptions.selected;
  const programId = sel && sel.program_id;
  if (!programId) return [];
  const program = (contextOptions.programs || [])
    .find((p) => p.id === programId);
  return (program && program.seasons) || [];
}
// A Season's display name for a row that is NOT itself Season-narrowed (a
// permanent League's subtitle): prefer the ceilinged payload, fall back to
// the authorized context options, so the label never renders blank just
// because that Season is not the active one.
function contextSeasonName(sv, seasonId) {
  if (!seasonId) return "";
  const own = (sv.seasons || []).find((s) => s.id === seasonId);
  if (own) return own.name;
  const opt = contextSeasonOptions().find((s) => s.id === seasonId);
  return opt ? opt.name : "";
}

// Data-driven Setup (#44): each entity declares its card list projection and
// its create-form fields once, so the record cards and the create drawer stay
// in sync. The API endpoints are unchanged — the drawer just POSTs to them.
const SETUP_ENTITIES = [
  // Program (#233) is the permanent competition umbrella. Internally still the
  // "league" entity/key/noun (v1 API frozen) — only the display noun changes.
  { key: "league", title: "Programs", icon: "🏆", noun: "league", displayNoun: "program",
    perm: "manage_setup", delKind: "league",
    list: (ov) => (ov.programs || []).map((l) => ({
      id: l.id, title: l.name,
      sub: nameById(ov.organizations, l.operator_organization_id) || "No operating org" })),
    fields: [
      { id: "f-league", label: "Program name", required: true, placeholder: "e.g. Adult Men" },
      // A Program's operating organization is OPTIONAL in the canonical model
      // (#233 B2a review r1) — operator_organization_id is nullable server-
      // side, so the field must offer an explicit "none" option rather than
      // forcing an organization to exist before a Program can be created.
      { id: "f-league-org", label: "Operating organization (optional)", type: "select",
        ofNoun: "organization", ofNounDisplay: "operating organization",
        options: (ov) => [["", "— none —"]].concat(withPendingLink(ov, "organizations").map((o) => [o.id, o.name])) },
      // The Program timezone anchors date-only Season boundaries (#272): an
      // IANA name, defaulting to UTC. A bad name is rejected server-side with
      // invalid_timezone and surfaced in the drawer.
      { id: "f-league-tz", label: "Timezone (IANA)", placeholder: "e.g. America/Chicago", value: "UTC" }] },
  { key: "season", title: "Seasons", icon: "🗓️", noun: "season", perm: "manage_setup",
    delKind: "season",
    list: (ov) => (ov.seasons || []).map((s) => {
      const prog = (ov.programs || []).find((p) => p.id === s.program_id);
      const range = seasonDateRange(s, prog && prog.timezone);
      return { id: s.id, title: s.name,
        sub: nameById(ov.programs, s.program_id) + (range ? ` · ${range}` : "") };
    }),
    fields: [
      { id: "f-season-league", label: "Program", type: "select", required: true, ofNoun: "league",
        options: (ov) => (ov.programs || []).map((l) => [l.id, l.name]) },
      { id: "f-season", label: "Season name", required: true, placeholder: "e.g. 2027–28" },
      // Optional calendar-date boundaries (#272). A native date input sends
      // YYYY-MM-DD, which the API anchors to local midnight in the Program's
      // timezone — no need to hand-craft a UTC instant.
      { id: "f-season-start", label: "Start date (optional)", type: "date" },
      { id: "f-season-end", label: "End date (optional)", type: "date" }] },
  // League (#233): the season-specific competitive grouping (e.g. Adult
  // League, Junior League). Internally still the "level" entity/key/noun (v1
  // API frozen) — only the display noun changes. Gold/Silver/Diamond etc. are
  // DIVISIONS *within* a League, never Leagues themselves (issue #245 — the
  // client confirmed this after B2b, correcting an earlier reversed example).
  { key: "level", title: "Leagues", icon: "🎚️", noun: "level", displayNoun: "league",
    perm: "manage_setup", delKind: "level",
    // A League is permanent Program structure, so the card itself is never
    // Season-narrowed -- but its Season SUBTITLE has to resolve names the
    // ceilinged `sv.seasons` no longer carries, hence contextSeasonName.
    list: (ov) => (ov.leagues || []).map((lv) => ({
      id: lv.id, title: lv.name, sub: contextSeasonName(ov, lv.season_id) })),
    fields: [
      // #367 owner ruling: `sv.seasons` is now the ACTIVE Season alone, and a
      // Program-only context makes it EMPTY -- so sourcing this picker from
      // it would dead-end the ordinary "create the Season, then its League"
      // flow with "Create a season first." while the Season the operator just
      // made sat right there in the Records card. The context bar's own
      // authorized Season list is the right source: it is authorization-
      // filtered by the same context_scope rules, and it is ALREADY rendered
      // on this very page in #ctx-select, so offering it here discloses
      // nothing new. What it must not do is widen the READ -- the card lists
      // above still show only the active Season's records.
      { id: "f-level-season", label: "Season", type: "select", required: true, ofNoun: "season",
        options: () => contextSeasonOptions().map((s) => [s.id, s.name]) },
      { id: "f-level", label: "League name", required: true, placeholder: "e.g. Adult League" },
      { id: "f-level-sort", label: "Sort order (optional)", type: "number", placeholder: "e.g. 1" }] },
  // Division (#245): the optional split WITHIN a League (e.g. Gold, Silver,
  // Diamond) — never a League example itself.
  { key: "division", title: "Divisions", icon: "🏅", noun: "division", perm: "manage_setup",
    delKind: "division",
    list: (ov) => (ov.divisions || []).map((d) => ({
      id: d.id, title: d.name,
      sub: [d.league_name, d.is_junior ? "Junior" : ""].filter(Boolean).join(" · ") })),
    fields: [
      // A v2 division is parented by a League (season is derived) — required.
      // The League options stay the active Program's own (permanent Program
      // structure); only their Season LABEL needs the context fallback, for
      // a League whose binding is not the active Season.
      { id: "f-div-league", label: "League", type: "select", required: true, ofNoun: "level",
        options: (ov) => (ov.leagues || []).map((lv) => [lv.id, `${contextSeasonName(ov, lv.season_id)} · ${lv.name}`]) },
      { id: "f-div", label: "Division name", required: true, placeholder: "e.g. Gold" },
      { id: "f-div-age", label: "Age group", placeholder: "e.g. U14 (optional)" },
      // Carries the active Season a #345 context-seeded open resolved the
      // League against, so a League bound to several Seasons commits into the
      // exact one the operator was acting from rather than the ambiguous
      // legacy sole-binding path. Empty/absent (the plain Records "+ New"
      // open, or any League with only one binding) preserves that legacy
      // behavior unchanged.
      { id: "f-div-season", type: "hidden" }] },
  { key: "club", title: "Clubs", icon: "🏒", noun: "club", perm: "manage_setup",
    delKind: "club",  // a club with no team can be deleted from here (#215)
    list: (ov) => withPendingLink(ov, "clubs").map((c) => ({ id: c.id, title: c.name })),
    fields: [{ id: "f-club", label: "Club name", required: true, placeholder: "e.g. Eagles HC" }] },
  { key: "team", title: "Teams", icon: "👥", noun: "team", perm: "manage_setup",
    delKind: "team",
    // A team is a permanent member of a LEAGUE (#180) — its season/division is
    // set separately via Season participation, so the subtitle shows the club
    // only, not a (now season-specific) division.
    // #283 Slice E: the subtitle shows a Team's PERMANENT League (its
    // competition membership) alongside its Club — the seasonal
    // Season→LeagueSeason→Division participation is shown separately in the
    // Hierarchy view, never conflated with the permanent identity here.
    list: (ov) => (ov.teams || []).map((t) => {
      const lg = teamPermLeague[t.id];
      return {
        id: t.id, title: t.name,
        sub: [lg ? `🎚️ ${lg.name}` : "No league",
              t.club_name || "No club"].join(" · "),
      };
    }),
    fields: [
      { id: "f-team-club", label: "Club (optional)", type: "select", ofNoun: "club",
        options: (ov) => [["", "— none —"]].concat(withPendingLink(ov, "clubs").map((c) => [c.id, c.name])) },
      // #283 Slice E: a Team is created under its PERMANENT League (required);
      // the backend derives its Program from that League, so no Program field is
      // needed. A league-less Team is only a legacy/migration remediation state,
      // never a fresh canonical create.
      { id: "f-team-perm-league", label: "Permanent league", type: "select", required: true,
        ofNoun: "level",
        options: () => allPermLeagues.map((lg) => [lg.id, `${lg.programName} · ${lg.name}`]) },
      { id: "f-team", label: "Team name", required: true, placeholder: "e.g. U14 Eagles" }] },
  // Organization (#166): the facility owner/operator that owns venues — a rink
  // company, distinct from a hockey Club. Arena-side, like venue/rink.
  { key: "organization", title: "Facility owners", icon: "🏢", noun: "facility owner", perm: "manage_arena",
    delKind: "organization",
    list: (ov) => withPendingLink(ov, "organizations").map((o) => ({
      id: o.id, title: o.name, sub: o.short_name || "" })),
    fields: [
      { id: "f-org", label: "Facility owner name", required: true, placeholder: "e.g. Summit Ice Facilities" },
      { id: "f-org-short", label: "Short name (optional)", placeholder: "e.g. Summit" }] },
  { key: "venue", title: "Venues", icon: "🏟️", noun: "venue", perm: "manage_arena",
    delKind: "venue",
    // A venue is owned by an organization (#233 canonical) — show its facility
    // owner. Which Seasons may use a venue's ice is a separate, independent
    // grant (SeasonVenueAccess, #233 Slice E) managed under each Season.
    list: (ov) => withPendingLink(ov, "venues").map((v) => ({
      id: v.id, title: v.name,
      sub: [v.organization_name].filter(Boolean).join(" · ") || "Unassigned" })),
    // No Program field on this form (#233 B2a review r1): canonical Venue
    // create is org-owned only.
    fields: [
      { id: "f-venue", label: "Venue name", required: true, placeholder: "e.g. South Arena" },
      { id: "f-venue-org", label: "Facility owner (organization)", type: "select", ofNoun: "organization", ofNounDisplay: "facility owner",
        options: (ov) => [["", "— none —"]].concat(withPendingLink(ov, "organizations").map((o) => [o.id, o.name])) }] },
  { key: "rink", title: "Rinks", icon: "⛸️", noun: "rink", perm: "manage_arena",
    delKind: "rink",
    list: (ov) => withPendingLink(ov, "rinks").map((r) => ({
      id: r.id, title: r.name, sub: r.venue_name || "" })),
    fields: [
      { id: "f-rink-venue", label: "Venue", type: "select", required: true, ofNoun: "venue",
        options: (ov) => withPendingLink(ov, "venues").map((v) => [v.id, v.name]) },
      { id: "f-rink", label: "Rink name", required: true, placeholder: "e.g. Rink 3" }] },
  { key: "ice-slot", title: "Ice slots", icon: "🧊", noun: "ice slot", perm: "manage_arena",
    list: null,  // ice inventory is managed visually on the Arena Calendar
    fields: [
      { id: "f-slot-rink", label: "Rink", type: "select", required: true, ofNoun: "rink",
        options: (ov) => withPendingLink(ov, "rinks").map((r) => [r.id, `${r.venue_name ? r.venue_name + " · " : ""}${r.name}`]) },
      // A function, not a constant: SETUP_ENTITIES is built once at load, so a
      // literal default here would freeze whatever day the app started on. The
      // operator opens this drawer from a calendar sitting on a particular
      // day, and that day is what they mean (#389 review).
      { id: "f-slot-date", label: "Date", type: "date", required: true,
        value: () => calendarDate },
      { id: "f-slot-start", label: "Start", type: "time", required: true, value: "21:00" },
      { id: "f-slot-end", label: "End", type: "time", required: true, value: "22:30" },
      { id: "f-slot-type", label: "Type", type: "select", required: true,
        options: () => [["game", "Game"], ["practice", "Practice"], ["public_skate", "Public skate"],
                        ["maintenance", "Maintenance"], ["tournament", "Tournament"]] }] },
  { key: "official", title: "Officials", icon: "🧑‍⚖️", noun: "official", perm: "manage_schedule",
    delKind: "official",
    // An Official reaches the active context through a home Club or an
    // assignment, so a just-created one has neither yet: union in the
    // caller's own pending-link officials, exactly like Clubs/Venues above
    // (and matching the Officials count on the Setup hub, which already
    // did). Without it, the operator could create an Official and then not
    // see the row they just made — nor delete it.
    list: (ov) => withPendingLink(ov, "officials").map((o) => ({
      id: o.id, title: o.name, sub: o.home_club_name || "" })),
    fields: [
      { id: "f-official", label: "Official name", required: true, placeholder: "e.g. Riley Whistle" },
      { id: "f-official-club", label: "Home club (optional — for conflict checks)", type: "select",
        options: (ov) => [["", "— none —"]].concat(withPendingLink(ov, "clubs").map((c) => [c.id, c.name])) }] },
  // Players (#114): a manual-create path so a late-arriving player doesn't
  // force an operator through the CSV Import wizard for one row. Sourced
  // from its own /api/players call (playersList, fetched in render() only
  // while this view is open) — never from /api/demo/overview, which is
  // unauthenticated and this app's own convention keeps player names out of.
  { key: "player", title: "Players", icon: "🧑", noun: "player", perm: "manage_setup",
    delKind: "player", editKind: "player", activeKind: "player",
    list: (ov) => playersList.map((p) => ({
      id: p.id, title: p.name, active: p.is_active !== false,
      sub: `${nameById(ov.teams, p.team_id) || ""}${p.jersey_number != null ? " · #" + p.jersey_number : ""}${p.is_active === false ? " · inactive" : ""}`,
    })),
    fields: [
      // On EDIT the Team is shown for context but locked — moving a player to
      // another Team is the separate "⇄ Move" reassignment (#268 keeps
      // reassignment and the active/inactive lifecycle as their own operations).
      { id: "f-player-team", label: "Team", type: "select", required: true, ofNoun: "team",
        lockOnEdit: true,
        options: (ov) => (ov.teams || []).map((t) => [t.id, t.name]) },
      { id: "f-player-name", label: "Player name", required: true, placeholder: "e.g. Jordan Lee" },
      { id: "f-player-position", label: "Position", type: "select", required: true,
        options: () => [["forward", "Forward"], ["defense", "Defense"], ["goalie", "Goalie"]] },
      { id: "f-player-shoots", label: "Shoots (optional)", type: "select",
        options: () => [["", "—"], ["L", "Left"], ["R", "Right"]] },
      { id: "f-player-jersey", label: "Jersey number (optional)", type: "number", placeholder: "e.g. 17" },
      { id: "f-player-email", label: "Email (optional)", type: "email", placeholder: "player@example.com" }] },
];

// Display noun for a setup entity (#233). Falls back to the internal `noun`
// linkage token, which stays frozen for the v1 API and ofNoun matching — only
// the user-visible word changes (e.g. league→Program, level→League).
const entNoun = (e) => e.displayNoun || e.noun;

// Display noun for a field's parent entity (`ofNoun` holds the parent's internal
// key). Maps to the parent's display noun so empty-select notes never expose the
// internal league/level tokens (#233). A field may set `ofNounDisplay` to override
// the shared entity's noun where the role differs: the `organization` entity is a
// "facility owner" when it owns venues, but a Program's parent org is its
// "operating organization" — the same table in a different role (ADR 0001).
const ofNounLabel = (f) => {
  if (f.ofNounDisplay) return f.ofNounDisplay;
  if (!f.ofNoun) return "record";
  const e = SETUP_ENTITIES.find((x) => x.key === f.ofNoun);
  return e ? entNoun(e) : f.ofNoun;
};

// Each entity's POST body, built from the drawer inputs (ids match the fields).
const SETUP_POST = {
  league: () => post("/api/v2/setup/program", { name: val("f-league"), operator_organization_id: val("f-league-org") || null, timezone: val("f-league-tz") || "UTC" }),
  season: () => post("/api/v2/setup/season", { program_id: val("f-season-league"), name: val("f-season"),
    start_date: val("f-season-start") || null, end_date: val("f-season-end") || null }),
  level: () => post("/api/v2/setup/league", { season_id: val("f-level-season"), name: val("f-level"), sort_order: val("f-level-sort") ? Number(val("f-level-sort")) : 0 }),
  division: () => post("/api/v2/setup/division", { league_id: val("f-div-league"), name: val("f-div"), age_group: val("f-div-age"), season_id: val("f-div-season") || null }),
  club: () => post("/api/v2/setup/club", { name: val("f-club") }),
  team: () => post("/api/v2/setup/team", { league_id: val("f-team-perm-league") || null, club_id: val("f-team-club") || null, name: val("f-team") }),
  organization: () => post("/api/v2/setup/organization", { name: val("f-org"), short_name: val("f-org-short") }),
  venue: () => post("/api/v2/setup/venue", { name: val("f-venue"), organization_id: val("f-venue-org") || null }),
  rink: () => post("/api/v2/setup/rink", { venue_id: val("f-rink-venue"), name: val("f-rink") }),
  "ice-slot": () => post("/api/v2/setup/ice-slot", { rink_id: val("f-slot-rink"), start_time: `${val("f-slot-date")}T${val("f-slot-start")}:00+00:00`, end_time: `${val("f-slot-date")}T${val("f-slot-end")}:00+00:00`, slot_type: val("f-slot-type") }),
  official: () => post("/api/v2/setup/official", {
    name: val("f-official"), home_club_id: val("f-official-club") || null,
  }),
  player: () => post("/api/v2/setup/player", {
    team_id: val("f-player-team"), name: val("f-player-name"),
    position: val("f-player-position"),
    shoots: val("f-player-shoots") || null,
    jersey_number: val("f-player-jersey") ? Number(val("f-player-jersey")) : null,
    email: val("f-player-email") || null,
  }),
};

// Each editable entity's UPDATE body (#268). Distinct from SETUP_POST: it
// targets /<id>/update and omits fields that are their own operation (Team
// reassignment, active/inactive) — only the correctable profile fields.
const SETUP_EDIT = {
  player: (id) => post(`/api/v2/setup/player/${id}/update`, {
    name: val("f-player-name"), position: val("f-player-position"),
    shoots: val("f-player-shoots") || null,
    jersey_number: val("f-player-jersey") ? Number(val("f-player-jersey")) : null,
    email: val("f-player-email") || null,
  }),
};

const nameById = (rows, id) => (rows.find((r) => r.id === id) || {}).name || "";

// Group a flat list of rows by a foreign-key field, for the setup trees (#165).
function groupBy(rows, key) {
  const m = {};
  (rows || []).forEach((r) => { (m[r[key]] = m[r[key]] || []).push(r); });
  return m;
}
// A quick-create button that opens the existing Setup drawer with the parent
// preselected (#165) — data-prefill-field/value seed drawerValues so the
// drawer's parent <select> lands on the node the operator clicked under.
function treeAdd(kind, label, prefillField, prefillValue, prefillField2, prefillValue2) {
  const ent = SETUP_ENTITIES.find((e) => e.key === kind);
  if (!ent || !hasPerm(ent.perm)) return "";
  const pf = prefillField
    ? ` data-prefill-field="${esc(prefillField)}" data-prefill-value="${esc(prefillValue || "")}"` : "";
  // A second parent can be preselected too (e.g. a division under a level
  // needs both its level and its season seeded) — #166.
  const pf2 = prefillField2
    ? ` data-prefill-field2="${esc(prefillField2)}" data-prefill-value2="${esc(prefillValue2 || "")}"` : "";
  return `<button class="act ghost tree-add" data-drawer="${esc(kind)}"${pf}${pf2}>＋ ${esc(label)}</button>`;
}
const capWord = (s) => (s ? String(s).charAt(0).toUpperCase() + String(s).slice(1) : "");

// Reassignment (#166 PR D UI): move a record under a new parent via the
// /api/setup/<kind>/<id>/assign-<parent> endpoints. Each entry describes one
// move: its permission, parent noun, whether it can be unassigned (nullable),
// whether it's a risky move that warrants a warning, and how to build the
// candidate-parent options from the overview.
const REASSIGN = {
  "venue:organization": {
    perm: "manage_arena", noun: "facility owner", nullable: true, risky: false,
    fromSetupRead: true,
    options: (sv) => withPendingLink(sv, "organizations").map((o) => [o.id, o.name]) },
  "rink:venue": {
    perm: "manage_arena", noun: "venue", nullable: false, risky: false,
    fromSetupRead: true,
    options: (sv) => withPendingLink(sv, "venues").map((v) => [v.id, v.name]) },
  "division:level": {
    // Not nullable (#233 B2a review r1): v2 division create/reassign REQUIRES
    // a League, so the panel must never offer "— none —" here — it would
    // just produce a validation_error the canonical model rejects.
    perm: "manage_setup", noun: "level", displayNoun: "league", nullable: false, risky: false,
    // Only levels in the division's own season — the backend rejects a
    // cross-season link, so never offer one.
    options: (ov, pr) => (ov.levels || [])
      .filter((lv) => lv.season_id === pr.seasonId).map((lv) => [lv.id, lv.name]) },
  "team:club": {
    // Club is optional on a Team (#233 Slice D): nullable lets the operator
    // unassign a Team's Club from the reassign panel.
    perm: "manage_setup", noun: "club", nullable: true, risky: false,
    fromSetupRead: true,
    options: (sv) => withPendingLink(sv, "clubs").map((c) => [c.id, c.name]) },
  // team:league (#283 Slice B): move a Team to a different PERMANENT League —
  // promotion/relegation/transfer (rule 10). Not nullable (a Team is always
  // league-permanent once assigned) and risky (it changes the Team's standing
  // competition membership). Candidates are the permanent Leagues in the Team's
  // OWN program (a Team can't cross programs), scoped via pr.programId.
  "team:league": {
    perm: "manage_setup", noun: "league", nullable: false, risky: true,
    warn: "Moving a team to a different league changes its permanent competition membership. Past registrations, games, and standings are kept as history.",
    options: (ov, pr) => (permLeaguesByProgram[pr.programId] || []).map((lg) => [lg.id, lg.name]) },
  // team:division removed (#233 B2c): a Team has no seasonal Division of its
  // own — participation is a SeasonTeamRegistration (League required,
  // Division optional), moved via the registration's own League→Division
  // cascade (Season participation / Needs-assignment repair row), never a
  // structural reassignment on the Team itself. assign_team_division was
  // already removed server-side (#180); this control was dead UI config with
  // no reachable backend route.
  "player:team": {
    perm: "manage_setup", noun: "team", nullable: false, risky: true,
    warn: "Moving a player changes which team's roster they belong to.",
    options: (ov) => (ov.teams || []).map((t) => [t.id, t.name]) },
  // Program↔facility owner move (#173) — MANAGE_SETUP. Independent of any
  // Venue (#233 Slice E): a program's operating organization is free to
  // change regardless of Venue state.
  "league:organization": {
    perm: "manage_setup", noun: "facility owner", displayNoun: "operating organization", nullable: true, risky: false,
    fromSetupRead: true,
    options: (sv) => withPendingLink(sv, "organizations").map((o) => [o.id, o.name]) },
};
// v2 route + canonical body-key mapping for the reassignments moved to v2
// (#233 B2a review r1): frontend kind/parent tokens stay frozen (league =
// Program, level = League), but the v2 request needs canonical route
// segments and body keys. The venue:league temporary game-ice compatibility
// bridge (the one deliberate v1 holdout) was removed in #233 Slice E along
// with its backend route; every remaining structural entity here moves to v2.
const REASSIGN_V2 = {
  "league:organization": { kind: "program", parent: "organization", bodyKey: "operator_organization_id" },
  "division:level": { kind: "division", parent: "league", bodyKey: "league_id" },
  "team:club": { kind: "team", parent: "club", bodyKey: "club_id" },
  "team:league": { kind: "team", parent: "league", bodyKey: "league_id" },
  "player:team": { kind: "player", parent: "team", bodyKey: "team_id" },
  "rink:venue": { kind: "rink", parent: "venue", bodyKey: "venue_id" },
  "venue:organization": { kind: "venue", parent: "organization", bodyKey: "organization_id" },
};
// A small "⇄ Move" button that opens the reassignment confirm panel, seeded
// with the record's current parent so the operator sees where it sits now.
function reassignBtn(kind, parent, rec, curId, seasonId, programId) {
  const cfg = REASSIGN[`${kind}:${parent}`];
  if (!cfg || !hasPerm(cfg.perm)) return "";
  // programId scopes the candidate list for a permanent-League transfer to the
  // Team's own program (#283 Slice B) — other moves ignore it.
  return `<button class="act ghost xs tn-reassign-btn" data-reassign="${kind}:${parent}"
    data-rz-id="${esc(rec.id)}" data-rz-name="${esc(rec.name || rec.id)}"
    data-rz-cur="${esc(curId || "")}" data-rz-season="${esc(seasonId || "")}"
    data-rz-program="${esc(programId || "")}"
    title="Move to a different ${esc(entNoun(cfg))}">⇄ Move</button>`;
}
// The confirm panel: pick a new parent, see a warning for risky moves, commit.
// #369: takes `sv` (the Setup v2 read) as well as `ov`, because the
// Club/Organization/Venue pickers must come from the Setup surface, not the
// Dashboard. `ov`'s reference collections are now scoped to what the active
// Program actually USES, which is right for a Dashboard but wrong for a
// reassign panel: the whole point of "move this Team to a Club" is to link a
// Club that is not linked yet, and a just-created Club has no Team, so it is
// absent from `ov.clubs` by construction. `sv` carries the additive
// `unassigned_*` buckets for exactly this create-then-link case.
function reassignPanelHtml(ov, sv) {
  if (!pendingReassign) return "";
  const pr = pendingReassign;
  const cfg = REASSIGN[`${pr.kind}:${pr.parent}`];
  if (!cfg) return "";
  const rows = (cfg.nullable ? [["", "— none —"]] : []).concat(
    cfg.options(cfg.fromSetupRead ? (sv || {}) : ov, pr));
  const optsHtml = rows.map(([v, label]) =>
    `<option value="${esc(v)}"${v === (pr.curId || "") ? " selected" : ""}>${esc(label)}</option>`).join("");
  const empty = !rows.length;
  return `<aside class="cal-aside confirm rz-panel">
    <div class="ca-head"><span class="ca-ico">⇄</span><span class="ca-title">Move to a different ${esc(entNoun(cfg))}</span>
      <button class="ca-x" data-reassign-cancel aria-label="Cancel">×</button></div>
    <div class="ca-body">
      <p><strong>${esc(pr.name)}</strong></p>
      ${empty
        ? `<p class="tn-empty">No ${esc(entNoun(cfg))} available yet — create one first.</p>`
        : `<label class="rz-label" for="reassign-target">New ${esc(entNoun(cfg))}</label>
           <select id="reassign-target">${optsHtml}</select>`}
      ${cfg.risky ? `<p class="ca-warn">⚠ ${esc(cfg.warn)}</p>` : ""}
      <div class="ca-actions">
        ${empty ? "" : `<button class="act primary" data-reassign-confirm>Move</button>`}
        <button class="act ghost" data-reassign-cancel>Cancel</button>
      </div>
    </div>
  </aside>`;
}
// POST the chosen parent to the reassignment endpoint, then refresh.
async function commitReassign(newId) {
  const pr = pendingReassign;
  if (!pr) return;
  const cfg = REASSIGN[`${pr.kind}:${pr.parent}`];
  toast = "";
  if ((newId || "") === (pr.curId || "")) { pendingReassign = null; return render(); }
  const v2 = REASSIGN_V2[`${pr.kind}:${pr.parent}`];
  const res = v2
    ? await post(`/api/v2/setup/${v2.kind}/${pr.id}/assign-${v2.parent}`, { [v2.bodyKey]: newId || null })
    : await post(`/api/setup/${pr.kind}/${pr.id}/assign-${pr.parent}`,
      { [`${pr.parent}_id`]: newId || null });
  if (res && res.error) { toast = res.error.message; pendingReassign = null; return render(); }
  pendingReassign = null;
  toast = newId ? `Moved to a new ${entNoun(cfg)}.` : `Unassigned from its ${entNoun(cfg)}.`;
  await render();
}

// -- Safe destructive deletion UI (#215) --------------------------------
// A compact, neutral outlined "Delete" control for a setup record. It reads as
// a quiet secondary action (red only on hover, see .del-btn in styles.css) and
// never deletes on click — it opens a themed confirmation modal. ``kind`` is
// the route entity (league|season|division|club|team|venue|rink|ice-slot).
function delBtn(kind, id, name, label) {
  if (!hasPerm("manage_setup")) return "";
  // Compact neutral icon button (#215): a trash glyph, red only on hover/focus
  // (see .icon-btn.danger). The accessible label carries the full intent; the
  // click opens a themed confirmation, never deletes outright.
  const aria = `${label || "Delete"}${name ? " " + name : ""}`;
  return `<button class="icon-btn danger" data-del="${esc(kind)}"
    data-del-id="${esc(id)}" data-del-name="${esc(name || id)}"
    title="${esc(aria)}" aria-label="${esc(aria)}">${ICONS.trash}</button>`;
}

function editBtn(kind, id, name) {
  // Compact neutral pencil button (#268): opens the edit drawer prefilled from
  // the record. MANAGE_SETUP-gated, same as create/delete.
  if (!hasPerm("manage_setup")) return "";
  const aria = `Edit${name ? " " + name : ""}`;
  return `<button class="icon-btn" data-edit="${esc(kind)}"
    data-edit-id="${esc(id)}" title="${esc(aria)}"
    aria-label="${esc(aria)}">${ICONS.pencil}</button>`;
}

function activeBtn(kind, id, name, isActive) {
  // Deactivate/reactivate control (#270): the supported roster exit that keeps
  // all history, distinct from delete. The click opens a confirmation; it never
  // toggles outright. MANAGE_SETUP-gated. A deactivate reads as the neutral
  // circle-minus; a reactivate as the check.
  if (!hasPerm("manage_setup")) return "";
  const verb = isActive ? "Deactivate" : "Reactivate";
  const aria = `${verb}${name ? " " + name : ""}`;
  return `<button class="icon-btn" data-player-active="${esc(id)}"
    data-player-active-next="${isActive ? "0" : "1"}"
    data-player-active-name="${esc(name || id)}"
    title="${esc(aria)}" aria-label="${esc(aria)}">${
      isActive ? ICONS.circleMinus : ICONS.circleCheck}</button>`;
}

// Human labels for the entity kinds shown in the confirm/blocked modals.
// Keys are internal delete-kinds (frozen); values are display nouns (#233:
// the "league" kind is the umbrella Program, the "level" kind is the League).
const DEL_NOUN = {
  organization: "facility owner", league: "program", season: "season",
  level: "league", division: "division", club: "club", team: "team",
  venue: "venue", rink: "rink", "ice-slot": "ice slot", game: "game",
  "season-team-registration": "registration",
  "season-venue-access": "venue access",
  official: "official", player: "player",
};
// Higher-level records (#215) demand a typed confirmation — the operator must
// type the record's name or DELETE before the destructive button enables.
// Lower-risk entities keep a single-click confirm. A registration cleanup
// (#251) is already scoped to an inactive, game-free row before it ever
// reaches this modal, so it stays single-click like division/club/game.
const HIGH_RISK_DELETE = new Set(["league", "season", "team", "venue", "rink"]);
// v2 delete route segment for each structural entity (#233 B2a review r1,
// extended to organization/game in B2c; official/player added in #232) —
// frozen frontend kind tokens map to canonical names, 1:1 except
// league→program/level→league.
const DEL_ROUTE_V2 = {
  league: "program", season: "season", level: "league", division: "division",
  club: "club", team: "team", venue: "venue", rink: "rink", "ice-slot": "ice-slot",
  organization: "organization", game: "game",
  "season-team-registration": "season-team-registration",
  "season-venue-access": "season-venue-access",
  official: "official", player: "player",
};

// Structural deletes use v2 (#233 B2a review r1, extended B2c); frozen
// frontend kind tokens map to canonical v2 route segments (league→program,
// level→league), others 1:1. Shared by the initial confirm and the blocked
// modal's retry-after-retiring-a-dependency flow (#232 review 6).
async function attemptDelete(kind, id) {
  const v2Kind = DEL_ROUTE_V2[kind];
  return v2Kind
    ? await post(`/api/v2/setup/${v2Kind}/${id}/delete`, {})
    : await post(`/api/setup/${kind}/${id}/delete`, {});
}

function renderModal() {
  if (!modal) return "";
  if (modal.type === "demo-confirm") return demoConfirmModalHtml(modal);
  if (modal.type === "confirm-delete") return confirmDeleteModalHtml(modal);
  if (modal.type === "player-active") return playerActiveModalHtml(modal);
  if (modal.type === "cancel-game") return cancelGameModalHtml(modal);
  if (modal.type === "blocked") return blockedModalHtml(modal);
  if (modal.type === "factory-reset") return factoryResetModalHtml(modal);
  return "";
}

// Cancel game (#215): a committed/published fixture is never hard-deleted — it
// is cancelled, preserving its fixture and result history.
function cancelGameModalHtml(m) {
  return modalShell("danger", "Cancel this game?",
    `<p>You're about to cancel <strong>${esc(m.name || "this game")}</strong>. The
       fixture and any result history are kept — this is not a delete.</p>
     <p class="muted">Rosters can no longer be changed for a cancelled game.</p>`,
    `<button class="act ghost" data-modal-close>Keep game</button>
     <button class="act danger" data-cancel-game-confirm>Cancel game</button>`);
}

// Deactivate/reactivate a Player (#270): the supported roster exit — never a
// delete. Deactivation keeps every historical row; it only removes the player
// from FUTURE roster selection and substitute candidacy. Reactivation re-runs
// jersey/team integrity server-side, so a collision is surfaced as an error.
function playerActiveModalHtml(m) {
  if (m.next) {
    return modalShell("neutral", "Reactivate this player?",
      `<p>You're about to reactivate <strong>${esc(m.name || "this player")}</strong>.
         They'll be eligible for new rosters again.</p>
       <p class="muted">Their jersey number must still be free on the team —
         if a teammate took it, reactivation is blocked.</p>
       <p class="muted">Reactivating the player does <strong>not</strong> restore
         their login. If they had a player account, it stays disabled — reactivate
         it separately from account management.</p>`,
      `<button class="act ghost" data-modal-close>Cancel</button>
       <button class="act primary" data-player-active-confirm>Reactivate player</button>`);
  }
  return modalShell("danger", "Deactivate this player?",
    `<p>You're about to deactivate <strong>${esc(m.name || "this player")}</strong>.
       Their history — past rosters, games, and stats — is kept; this is not a
       delete.</p>
     <p class="muted">They'll be removed from future roster selection and
       substitute lists, and their jersey number frees up for the team. You can
       reactivate the player anytime.</p>
     <p class="muted">Any player login for them will be disabled and signed out
       immediately. Reactivating the player later does not bring the login back —
       you'd reactivate the account separately.</p>`,
    `<button class="act ghost" data-modal-close>Keep active</button>
     <button class="act danger" data-player-active-confirm>Deactivate player</button>`);
}

function modalShell(kind, title, body, foot) {
  return `<div class="modal-scrim" data-modal-close></div>
    <div class="modal ${kind}" role="dialog" aria-modal="true" tabindex="-1" aria-label="${esc(title)}">
      <header class="modal-head"><h2>${esc(title)}</h2>
        <button class="modal-x" data-modal-close aria-label="Close">×</button></header>
      <div class="modal-body">${body}</div>
      <footer class="modal-foot">${foot}</footer>
    </div>`;
}

// Demo reset/clear confirmation (#215): both are destructive and typed-confirm
// gated. Reset rebuilds the sample dataset (RESET); Clear returns to an empty
// setup (CLEAR).
function demoConfirmModalHtml(m) {
  const clear = m.action === "clear";
  const word = clear ? "CLEAR" : "RESET";
  const title = clear ? "Clear demo data" : "Reset demo data";
  const lead = clear
    ? `<p>This clears all demo data and returns to an empty setup. Everyone
         using this demo will see it emptied. This can't be undone.</p>`
    : `<p>This clears all demo data and rebuilds the sample competition. Everyone
         using this demo will see it restart. This can't be undone.</p>`;
  return modalShell("danger", title,
    `${lead}
     <label class="modal-confirm-label" for="demo-confirm-input">Type <code>${word}</code> to confirm</label>
     <input id="demo-confirm-input" class="modal-confirm-input" autocomplete="off"
       spellcheck="false" placeholder="${word}">`,
    `<button class="act ghost" data-modal-close>Cancel</button>
     <button class="act danger" data-demo-confirm disabled>${esc(title)}</button>`);
}

// Production factory-reset confirmation (#256). One themed modal that walks the
// operator through every safety control the issue requires, in order:
//   loading  — the preview POST is in flight (read-only, zero writes);
//   confirm  — row-count preview + irreversible warning + backup acknowledgement
//              + password re-entry + the exact typed phrase, with the execute
//              button locked until all of them are satisfied;
//   success  — the wipe completed; the caller is being signed out;
//   error    — the preview or execute call failed (permission, stale challenge,
//              server error) — the message is shown with a way to start over.
// The CONFIRMATION_PHRASE mirrors the server constant exactly.
const FACTORY_RESET_PHRASE = "DELETE ALL PRODUCTION DATA";
function factoryResetCountRows(counts) {
  // Show only the domains that actually hold rows, largest first, plus a total,
  // so the operator sees the concrete blast radius rather than a wall of zeros.
  const entries = Object.entries(counts || {})
    .filter(([, n]) => n > 0)
    .sort((a, b) => b[1] - a[1]);
  const total = entries.reduce((sum, [, n]) => sum + n, 0);
  if (!total) {
    return `<p class="muted">This installation currently holds no business or
      operational data. A reset will only re-assert the clean baseline.</p>`;
  }
  const rows = entries.map(([table, n]) =>
    `<div class="fr-count-row"><span class="fr-count-name">${esc(table)}</span>
       <span class="fr-count-n">${esc(String(n))}</span></div>`).join("");
  return `<div class="fr-counts" data-fr-counts>
      <div class="fr-count-row fr-count-total"><span class="fr-count-name">Total rows</span>
        <span class="fr-count-n">${esc(String(total))}</span></div>
      ${rows}
    </div>`;
}
function factoryResetModalHtml(m) {
  const title = "Factory reset production data";
  if (m.step === "loading") {
    return modalShell("danger", title,
      `<p class="muted">Counting the data that would be erased…</p>`,
      `<button class="act ghost" data-modal-close>Cancel</button>`);
  }
  if (m.step === "success") {
    return modalShell("danger", title,
      `<p><strong>Production data has been reset.</strong></p>
       <p class="muted">Every session was signed out. You'll be returned to the
         sign-in screen to set the installation up again.</p>`,
      `<button class="act primary" data-fr-done>Return to sign-in</button>`);
  }
  if (m.step === "error") {
    return modalShell("danger", title,
      `<p>${esc(m.error || "The factory reset could not be started.")}</p>
       <p class="muted">Nothing was changed.</p>`,
      `<button class="act ghost" data-modal-close>Close</button>
       <button class="act danger" data-fr-restart>Start over</button>`);
  }
  // step === "confirm"
  const inlineError = m.error
    ? `<p class="fr-inline-error" role="alert">${esc(m.error)}</p>` : "";
  return modalShell("danger", title,
    `<p>You're about to <strong>permanently erase all production data</strong>
       for this installation. This cannot be undone. Make sure a database backup
       has been taken first.</p>
     <div class="section-title" style="margin-top:0">What will be erased</div>
     ${factoryResetCountRows(m.counts)}
     <label class="fr-check"><input type="checkbox" id="fr-backup"
        ${m.backup ? "checked" : ""}> I have taken a full database backup and
        understand this permanently erases all production data.</label>
     <label class="modal-confirm-label" for="fr-password">Re-enter your password</label>
     <input id="fr-password" class="modal-confirm-input fr-input" type="password"
       autocomplete="current-password" spellcheck="false" placeholder="Password">
     <label class="modal-confirm-label" for="fr-phrase">Type
       <code>${FACTORY_RESET_PHRASE}</code> to confirm (exactly, in capitals)</label>
     <input id="fr-phrase" class="modal-confirm-input fr-input" autocomplete="off"
       spellcheck="false" placeholder="${FACTORY_RESET_PHRASE}">
     ${inlineError}`,
    `<button class="act ghost" data-modal-close>Cancel</button>
     <button class="act danger" data-fr-confirm disabled>Reset production data</button>`);
}

function confirmDeleteModalHtml(m) {
  const noun = DEL_NOUN[m.kind] || "record";
  const highRisk = HIGH_RISK_DELETE.has(m.kind);
  // High-risk records require typing the name (or DELETE) before the button
  // enables (#215); lower-risk ones confirm in one click.
  const confirmField = highRisk
    ? `<label class="modal-confirm-label" for="del-confirm">Type the ${esc(noun)}'s name
         (<strong>${esc(m.name)}</strong>) or <code>DELETE</code> to confirm</label>
       <input id="del-confirm" class="modal-confirm-input" autocomplete="off"
         spellcheck="false" placeholder="DELETE">`
    : "";
  return modalShell("danger", `Delete this ${noun}?`,
    `<p>You're about to permanently delete the ${esc(noun)}
       <strong>${esc(m.name)}</strong>. This can't be undone.</p>
     <p class="muted">If anything depends on it, the delete is refused and nothing changes.</p>
     ${confirmField}`,
    `<button class="act ghost" data-modal-close>Cancel</button>
     <button class="act danger" data-del-confirm ${highRisk ? "disabled" : ""}>Delete ${esc(noun)}</button>`);
}

// Dependency group types (#232 review 6) an operator can resolve inline from
// the blocked-delete modal itself, rather than navigating elsewhere: the
// retire action (never a delete) that clears each. Maps the group's `type`
// to the recipient-scoped `/active` route segment.
const RETIRABLE_DEP_ROUTE = {
  "contact destination": "contacts",
  "notification preference": "preferences",
};

function blockedModalHtml(m) {
  const noun = DEL_NOUN[m.kind] || "record";
  const deps = (m.error && m.error.details && m.error.details.dependencies) || [];
  const rows = deps.map((g) => {
    // Prefer id+name pairs so a specific blocker is identifiable even when
    // names collide (#215 review 4); fall back to bare names for older shapes.
    const items = g.items || (g.names || []).map((n) => ({ name: n }));
    const retireRoute = hasPerm("manage_setup") ? RETIRABLE_DEP_ROUTE[g.type] : null;
    const shown = items.map((it) => {
      const label = it.id
        ? `${esc(it.name)} <code>${esc(it.id)}</code>`
        : esc(it.name);
      // Retirement (#232 review 6): a contact destination/notification
      // preference blocker is resolved right here — never a delete, and
      // never requiring the operator to find a separate admin screen.
      const retireBtn = retireRoute && it.id
        ? `<button class="act ghost retire-btn" data-retire-route="${esc(retireRoute)}"
             data-retire-id="${esc(it.id)}">Retire</button>` : "";
      return `<span class="dep-item">${label}${retireBtn}</span>`;
    }).join(", ");
    const more = g.count > items.length ? ", …" : "";
    // Prefer the canonical display noun (#233); the structured `type` stays the
    // frozen code (e.g. league/level) for programmatic consumers.
    const depNoun = g.display || g.type;
    return `<div class="li"><div class="li-main">
      <div class="li-title">${esc(g.count)} ${esc(depNoun)}${g.count === 1 ? "" : "s"}</div>
      ${shown ? `<div class="li-sub">${shown}${more}</div>` : ""}</div></div>`;
  }).join("");
  // A registration cleanup (#251) blocked by Game history has no "remove or
  // reassign" action available — a draft/scheduled/cancelled Game is kept as
  // permanent record on purpose, so the generic call-to-action below would be
  // pointing at a step the operator can't actually take.
  const footer = m.kind === "season-team-registration"
    ? `<p class="muted">This registration is tied to permanent Game history and
         can't be purged while any game — including cancelled ones — still
         references it in this season.</p>`
    : `<p class="muted">Remove or reassign these first, then delete the ${esc(noun)}.</p>`;
  return modalShell("blocked", `Can't delete this ${noun}`,
    `<p>${esc((m.error && m.error.message) || "This record still has dependents.")}</p>
     <div class="card">${rows || `<div class="li"><div class="li-sub">Dependent records exist.</div></div>`}</div>
     ${footer}`,
    `<button class="act primary" data-modal-close>Close</button>`);
}

// Clear per-view interaction state after a demo reset rebuilds the store, so no
// stale id (a game, a picked player, an open drawer) survives the reseed.
function clearTransientStateAfterReset() {
  toast = ""; currentGame = null; pickedPlayer = null; wizard = null;
  movingGameId = null; conflict = null; pendingMove = null;
  drawer = null; drawerError = ""; drawerValues = {};
  pendingReassign = null; modal = null;
}

// After any demo lifecycle change (Load / Reset / Clear): drop transient view
// state, refresh the demo-empty status (which flips the header Load↔Reset and
// the empty-state card), and re-render. envStatus is refetched because
// demo_empty is server-computed and not part of the overview (#215).
async function afterDemoLifecycleChange(message) {
  clearTransientStateAfterReset();
  onboardingStatusDirty = true;
  demoMenuOpen = false;
  const status = await getJSON("/api/status");
  if (status && !status.error) envStatus = status;
  toast = message;
  modal = null;
  await render();
}

function wireModal(c) {
  c.querySelectorAll("[data-modal-close]").forEach((b) =>
    b.onclick = () => { modal = null; render(); });
  // Demo reset/clear flow: enable the destructive button only once the exact
  // word is typed, then POST the matching route.
  const demoInput = c.querySelector("#demo-confirm-input");
  const demoConfirm = c.querySelector("[data-demo-confirm]");
  if (demoInput && demoConfirm && modal && modal.type === "demo-confirm") {
    const clear = modal.action === "clear";
    const word = clear ? "CLEAR" : "RESET";
    demoInput.oninput = () => {
      demoConfirm.disabled = demoInput.value.trim().toUpperCase() !== word;
    };
    demoInput.focus();
    demoConfirm.onclick = async () => {
      if (demoInput.value.trim().toUpperCase() !== word) return;
      demoConfirm.disabled = true;  // prevent a duplicate submit while running
      // Server re-checks demo mode, MANAGE_SETUP, and the confirm value (#215).
      const res = await post(clear ? "/api/demo/clear" : "/api/demo/reset",
                             { confirm: word });
      if (res && res.error) { modal = null; return render(); }  // post() set the toast
      await afterDemoLifecycleChange(clear ? "Demo data cleared."
                                           : "Demo data reset.");
    };
  }
  // Confirm-delete flow: POST the delete; a has_dependencies error swaps this
  // modal for the blocked view (which lists what's in the way) rather than
  // just flashing a toast.
  const delConfirm = c.querySelector("[data-del-confirm]");
  if (delConfirm && modal && modal.type === "confirm-delete") {
    const m = modal;
    // For a high-risk record the button stays disabled until the name or
    // DELETE is typed (#215); the input is absent for lower-risk kinds.
    const delInput = c.querySelector("#del-confirm");
    const confirmed = () => !delInput
      || delInput.value.trim() === m.name
      || delInput.value.trim().toUpperCase() === "DELETE";
    if (delInput) {
      delInput.oninput = () => { delConfirm.disabled = !confirmed(); };
      delInput.focus();
    }
    delConfirm.onclick = async () => {
      if (!confirmed()) return;  // defense in depth; the button is disabled too
      toast = "";
      const res = await attemptDelete(m.kind, m.id);
      if (res && res.error && res.error.code === "has_dependencies") {
        modal = { type: "blocked", kind: m.kind, id: m.id, name: m.name, error: res.error };
        return render();
      }
      if (res && res.error) { modal = null; return render(); }  // post() set the toast
      modal = null;
      // A Division delete may have cleared inactive, game-free registrations
      // pointing at it (#233 D1 bundled fix, #248) — surface that count so
      // the operator knows those rows survived (division_id cleared, not
      // deleted), not just that the Division itself is gone.
      const cleaned = res && res.inactive_registrations_cleaned;
      toast = `Deleted ${DEL_NOUN[m.kind] || "record"} “${m.name}”.` + (cleaned
        ? ` Cleared ${cleaned} inactive registration${cleaned === 1 ? "" : "s"}.`
        : "");
      await render();
    };
  }
  // Deactivate/reactivate confirm (#270): POST the state change. On a jersey
  // conflict the server error toast is surfaced (post() sets it) and the modal
  // closes; on success the setup view re-fetches so the row reflects the new
  // state (and the team's active-player count updates).
  const paConfirm = c.querySelector("[data-player-active-confirm]");
  if (paConfirm && modal && modal.type === "player-active") {
    const m = modal;
    paConfirm.onclick = async () => {
      paConfirm.disabled = true;  // prevent a duplicate submit while running
      toast = "";
      const res = await post(`/api/v2/setup/player/${m.id}/active`,
                             { active: m.next });
      if (res && res.error) { modal = null; return render(); }  // post() set the toast
      modal = null;
      toast = m.next ? `Reactivated ${m.name}.` : `Deactivated ${m.name}.`;
      await render();
    };
  }
  // Retire a contact destination / notification preference from the blocked
  // modal itself (#232 review 6): the row is never deleted (its stored value
  // and history survive), it just stops counting as a live dependency. On
  // success, retry the same delete so the modal reflects fresh state — either
  // it now succeeds, or the (shorter) remaining blocker list.
  if (modal && modal.type === "blocked") {
    const m = modal;
    c.querySelectorAll("[data-retire-route]").forEach((b) => b.onclick = async () => {
      toast = "";
      b.disabled = true;
      const res = await post(
        `/api/notifications/${b.dataset.retireRoute}/${b.dataset.retireId}/active`,
        { active: false });
      if (res && res.error) { b.disabled = false; return render(); }  // post() set the toast
      const retry = await attemptDelete(m.kind, m.id);
      if (retry && retry.error && retry.error.code === "has_dependencies") {
        modal = { type: "blocked", kind: m.kind, id: m.id, name: m.name, error: retry.error };
        return render();
      }
      if (retry && retry.error) { modal = null; return render(); }
      modal = null;
      toast = `Deleted ${DEL_NOUN[m.kind] || "record"} “${m.name}”.`;
      await render();
    });
  }
  // Cancel-game confirm: posts to the roster cancel route (history preserved).
  const cancelGameBtn = c.querySelector("[data-cancel-game-confirm]");
  if (cancelGameBtn && modal && modal.type === "cancel-game") {
    const m = modal;
    cancelGameBtn.onclick = async () => {
      toast = "";
      const res = await post(`/api/games/${m.game_id}/cancel`, {});
      modal = null;
      if (res && !res.error) toast = "Game cancelled; history preserved.";
      await render();
    };
  }
  // Production factory reset (#256). The confirm step gates the execute button
  // behind three live conditions — backup acknowledged, a non-empty password,
  // and the exact typed phrase — toggled by input handlers WITHOUT a re-render
  // (a re-render would drop focus mid-type, exactly as the demo-confirm flow
  // avoids). "Start over" re-runs a fresh preview; "Return to sign-in" completes
  // the post-reset sign-out.
  if (modal && modal.type === "factory-reset") {
    const restartBtn = c.querySelector("[data-fr-restart]");
    if (restartBtn) restartBtn.onclick = () => startFactoryReset();
    // After a successful reset the server session is already destroyed, so EVERY
    // exit from the success view — the footer button, the × and the backdrop
    // scrim — must complete the sign-out. The generic `[data-modal-close]`
    // handler wired at the top of wireModal only clears the modal, which would
    // leave a stale authenticated console over a dead session; override all of
    // them (plus the explicit "Return to sign-in" button) here.
    if (modal.step === "success") {
      c.querySelectorAll("[data-modal-close], [data-fr-done]").forEach((b) =>
        b.onclick = () => finishFactoryResetSignOut());
    }
    const backup = c.querySelector("#fr-backup");
    const password = c.querySelector("#fr-password");
    const phrase = c.querySelector("#fr-phrase");
    const confirm = c.querySelector("[data-fr-confirm]");
    if (confirm && backup && password && phrase && modal.step === "confirm") {
      const m = modal;
      const ready = () => backup.checked
        && password.value.length > 0
        && phrase.value === FACTORY_RESET_PHRASE;
      const sync = () => {
        // Mirror the checkbox into modal state so a re-render (e.g. after an
        // inline execute error) preserves the operator's acknowledgement.
        m.backup = backup.checked;
        confirm.disabled = !ready();
      };
      backup.onchange = sync;
      password.oninput = sync;
      phrase.oninput = sync;
      sync();
      confirm.onclick = async () => {
        if (!ready()) return;
        // Progress lock (#256): disable every control so a double-submit can't
        // fire a second wipe while the first is in flight.
        confirm.disabled = true; confirm.textContent = "Resetting…";
        backup.disabled = password.disabled = phrase.disabled = true;
        const res = await post("/api/admin/factory-reset/execute", {
          password: password.value,
          typed_phrase: phrase.value,
          challenge_token: m.token,
          backup_acknowledged: true,
        });
        if (res && res.error) {
          // The challenge is single-use: a stale/expired/consumed one can't be
          // retried in place, so send the operator back to a fresh preview.
          // Everything else (wrong password, etc.) is retryable on this screen.
          const code = res.error.code || "";
          if (code === "invalid_challenge" || res.error.message &&
              /preview|challenge/i.test(res.error.message)) {
            modal = { type: "factory-reset", step: "error", error: res.error.message };
          } else {
            modal.step = "confirm"; modal.error = res.error.message;
          }
          toast = "";  // the modal owns the error surface, not the toast
          return render();
        }
        modal.step = "success";
        await render();
      };
    }
  }
}

// Open the Danger-zone factory-reset modal and immediately fetch the preview
// (#256): a read-only, zero-write row-count snapshot plus a single-use challenge
// token bound to it. Kept as a standalone function so both the initial button
// and the modal's "Start over" reuse the same entry point.
async function startFactoryReset() {
  modal = { type: "factory-reset", step: "loading" };
  render();
  const res = await post("/api/admin/factory-reset/preview", {});
  if (!modal || modal.type !== "factory-reset") return;  // closed while loading
  if (res && res.error) {
    modal = { type: "factory-reset", step: "error", error: res.error.message };
  } else {
    modal = { type: "factory-reset", step: "confirm", counts: res.counts,
              token: res.challenge_token, backup: false, error: "" };
  }
  toast = "";
  render();
}

// Complete the post-reset sign-out (#256): the server already revoked every
// session and expired this caller's cookie, so this just drops client auth
// state and returns to the sign-in screen, mirroring the header Sign-out.
function finishFactoryResetSignOut() {
  modal = null;
  try { localStorage.setItem("hs_signed_out", "1"); } catch (_) {}
  setUser(null);
  toast = "";
  renderRoleSwitch();
  showLogin("Production data was reset. Sign in to set up the installation.");
}

// Setup → Hierarchy (#165): a functional tree of the league's structure built
// from the data already loaded (overview + playersList), with counts,
// missing-assignment warnings, and quick-create actions that reuse the Setup
// drawers. Facility (Venue→Rink) and Competition (League→Season→Division→Team)
// are structure-only and safe for anyone; the Roster tree exposes player names
// and so is gated to setup operators, matching where playersList is fetched.
// Clean-slate empty state (#215): when there's nothing set up yet, invite the
// operator to build by hand or load the sample dataset instead of showing empty
// trees. Shown only to an operator who can manage setup.
function startYourLeagueCard() {
  const load = (hasPerm("manage_setup") && isDemo())
    ? `<button class="act ghost" data-demo-load>Load demo data</button>` : "";
  return `<section class="card start-league">
    <div class="section-title" style="margin-top:0">Start your competition</div>
    <p class="muted">Create your competition structure from scratch, or load the
      complete sample dataset to explore the app.</p>
    <div class="actions">
      <button class="act primary" data-drawer="league">Create manually</button>
      ${load}
    </div>
  </section>`;
}

function renderSetupHierarchy(sv, hv, ov) {
  // A brand-new demo (or any empty setup) opens on the "Start your league"
  // card rather than empty trees (#215).
  if (hasPerm("manage_setup") && !(sv.programs || []).length
      && !(sv.teams || []).length
      && !withPendingLink(sv, "organizations").length) {
    return startYourLeagueCard();
  }
  const canSeePlayers = hasPerm("manage_setup");
  const pCount = {};
  const pByTeam = {};
  (canSeePlayers ? playersList : []).forEach((p) => {
    pCount[p.team_id] = (pCount[p.team_id] || 0) + 1;
    (pByTeam[p.team_id] = pByTeam[p.team_id] || []).push(p);
  });

  // -- Facility: Organization owns Venue → Rink (canonical #233 — venues are
  //    org-owned, independent of any competition Program). Which Seasons may
  //    use a Venue's ice is managed under each Season (SeasonVenueAccess,
  //    #233 Slice E), not here on the facility tree. --
  const rinksByVenue = groupBy(withPendingLink(sv, "rinks"), "venue_id");
  const venueNode = (v) => {
    const rinks = rinksByVenue[v.id] || [];
    const badge = rinks.length
      ? `<span class="tn-badge ok">Ready</span>` : `<span class="tn-badge warn">Needs rinks</span>`;
    const rinkRows = rinks.map((r) =>
      `<div class="tn-leaf"><span class="tn-label">⛸️ ${esc(r.name)}</span>${reassignBtn("rink", "venue", r, r.venue_id)}${delBtn("rink", r.id, r.name)}</div>`).join("")
      || `<div class="tn-empty">No rinks yet. Add a rink so this venue can host games.</div>`;
    return `<details class="tn" open><summary class="tn-sum">
        <span class="tn-label">🏟️ ${esc(v.name)}</span>
        <span class="tn-meta">${rinks.length} rink${rinks.length === 1 ? "" : "s"}</span>${badge}${reassignBtn("venue", "organization", v, v.organization_id)}${delBtn("venue", v.id, v.name)}</summary>
      <div class="tn-children">${rinkRows}${treeAdd("rink", "Add rink to " + v.name, "f-rink-venue", v.id)}</div>
    </details>`;
  };
  // A facility owner (organization) groups the venues it owns.
  const venuesByOrg = groupBy(withPendingLink(sv, "venues"), "organization_id");
  const orgSections = withPendingLink(sv, "organizations").map((o) => {
    const vs = venuesByOrg[o.id] || [];
    const venueRows = vs.map(venueNode).join("")
      || `<div class="tn-empty">No venues yet. Add a venue owned by ${esc(o.name)}.</div>`;
    return `<details class="tn" open><summary class="tn-sum">
        <span class="tn-label">🏢 ${esc(o.name)}</span>
        <span class="tn-meta">${vs.length} venue${vs.length === 1 ? "" : "s"}</span>${delBtn("organization", o.id, o.name)}</summary>
      <div class="tn-children">${venueRows}${treeAdd("venue", "Add venue to " + o.name, "f-venue-org", o.id)}</div>
    </details>`;
  }).join("");
  // Venues with no facility owner (canonical venue.organization_id is null).
  const orphanVenues = (venuesByOrg[null] || []).concat(venuesByOrg[undefined] || []);
  const orphanVenueSection = orphanVenues.length
    ? `<details class="tn" open><summary class="tn-sum">
        <span class="tn-label tn-warn-text">🏟️ No facility owner</span>
        <span class="tn-meta">${orphanVenues.length} venue${orphanVenues.length === 1 ? "" : "s"}</span></summary>
      <div class="tn-children">${orphanVenues.map(venueNode).join("")}</div>
    </details>` : "";
  const facility = `<section class="tree-panel" id="facility-tree">
    <div class="tree-head"><span class="tree-title">🏟️ Facility</span>
      <span class="tree-sub">Facility owner → Venue → Rink</span></div>
    ${orgSections}${orphanVenueSection}
    ${!orgSections && !orphanVenueSection ? `<div class="tn-empty">No owners yet. Add a facility owner to start your arena setup.</div>` : ""}
    <div class="tree-actions">${treeAdd("organization", "Add facility owner")}</div>
  </section>`;

  // -- Competition: Program → Season → League → Division (#233 display;
  //    internal entities remain league/season/level/division on the v1 API).
  //    Consumed directly from the canonical hv (/api/v2/setup/hierarchy)
  //    payload (#233 B2a review r1) rather than reconstructed from flat sv
  //    lists client-side, so nesting, teams_without_division, and dangling
  //    divisions match the server's canonical parentage rules exactly. --
  const REG_REASON_LABEL = {
    team_missing: "Registered team no longer exists",
    team_program_mismatch: "Registered team belongs to a different program",
    registration_league_division_mismatch: "Registration's league doesn't match its division's league",
    registration_league_not_in_season: "Registration's league isn't in this season",
  };
  const seasonRegIssues = [];  // flattened across all seasons, for Needs assignment below
  const teamLeaf = (t) => `<div class="tn-leaf"><span class="tn-label">👥 ${esc(t.name)}</span>${
    t.player_count != null ? `<span class="tn-meta">${t.player_count} player${t.player_count === 1 ? "" : "s"}</span>` : ""}</div>`;
  const divisionNode = (d, seasonId) => {
    const n = (d.teams || []).length;
    const teamRows = (d.teams || []).map(teamLeaf).join("")
      || `<div class="tn-empty">No teams registered. Register teams under
        <button class="linklike" data-goto="setup">Season participation</button>.</div>`;
    return `<details class="tn"><summary class="tn-sum">
        <span class="tn-label">🏅 ${esc(d.name)}</span>
        <span class="tn-meta">${n} team${n === 1 ? "" : "s"} registered</span>${reassignBtn("division", "level", d, d.league_id, seasonId)}${delBtn("division", d.id, d.name)}</summary>
      <div class="tn-children">${teamRows}</div>
    </details>`;
  };
  const leagueRows = (hv.programs || []).map((program) => {
    const seasons = program.seasons || [];
    const progTeams = seasons.reduce((n, s) => n + (s.leagues || []).reduce(
      (m, lv) => m + (lv.divisions || []).reduce((k, d) => k + (d.teams || []).length, 0)
        + (lv.teams_without_division || []).length, 0), 0);
    const seasonRows = seasons.map((s) => {
      const leagues = (s.leagues || []).slice()
        .sort((a, b) => (a.sort_order || 0) - (b.sort_order || 0) || a.name.localeCompare(b.name));
      const divCount = leagues.reduce((n, lv) => n + (lv.divisions || []).length, 0);
      const teamCount = leagues.reduce((n, lv) => n + (lv.divisions || []).reduce(
        (m, d) => m + (d.teams || []).length, 0) + (lv.teams_without_division || []).length, 0);
      const levelSections = leagues.map((lv) => {
        const divs = lv.divisions || [];
        const twd = lv.teams_without_division || [];
        const divRows = divs.map((d) => divisionNode(d, s.id)).join("");
        const twdSection = twd.length
          ? `<div class="tn-empty">Registered directly under this league (no division):</div>${twd.map(teamLeaf).join("")}`
          : "";
        const inner = (divRows || twdSection)
          ? `${divRows}${twdSection}`
          : `<div class="tn-empty">No divisions in this league yet.</div>`;
        return `<details class="tn" open><summary class="tn-sum">
            <span class="tn-label">🎚️ ${esc(lv.name)}</span>
            <span class="tn-meta">${divs.length} division${divs.length === 1 ? "" : "s"}</span>${delBtn("level", lv.id, lv.name)}</summary>
          <div class="tn-children">${inner}${treeAdd("division", "Add division to " + lv.name, "f-div-league", lv.id)}</div>
        </details>`;
      }).join("");
      // Dangling divisions (no league, or a league that doesn't resolve in
      // this season) — INVALID structure per the canonical hierarchy, never
      // shown as a valid branch.
      const dangling = (s.needs_assignment && s.needs_assignment.divisions_without_league) || [];
      const orphanDivSection = dangling.length
        ? `<details class="tn" open><summary class="tn-sum">
            <span class="tn-label tn-warn-text">🏅 No league</span>
            <span class="tn-meta">${dangling.length} division${dangling.length === 1 ? "" : "s"}</span></summary>
          <div class="tn-children">${dangling.map((d) => divisionNode(d, s.id)).join("")}</div>
        </details>` : "";
      // Each issue carries its OWN season's id/name/leagues (not just the raw
      // backend row) so the repair row below can build a League→Division
      // cascade scoped to the right season, never the whole program (#233
      // B2b review — repair surface for invalid season registrations).
      (s.needs_assignment && s.needs_assignment.registrations || []).forEach((r) =>
        seasonRegIssues.push({ ...r, season_id: s.id, season_name: s.name, season_leagues: s.leagues || [] }));
      const seasonBody = (levelSections || orphanDivSection)
        ? `${levelSections}${orphanDivSection}`
        : `<div class="tn-empty">No divisions in this season yet.</div>`;
      const dateRange = seasonDateRange(s, program.timezone);
      return `<details class="tn" open><summary class="tn-sum">
          <span class="tn-label">🗓️ ${esc(s.name)}</span>
          <span class="tn-meta">${divCount} division(s) · ${teamCount} team(s)${
            dateRange ? ` · ${esc(dateRange)}` : ""}</span>${delBtn("season", s.id, s.name)}</summary>
        <div class="tn-children">${seasonBody}
          <div class="tree-actions sub">${treeAdd("level", "Add league to " + s.name, "f-level-season", s.id)}</div></div>
      </details>`;
    }).join("") || `<div class="tn-empty">No seasons in this program yet.</div>`;
    return `<details class="tn" open><summary class="tn-sum">
        <span class="tn-label">🏆 ${esc(program.name)}</span>
        <span class="tn-meta">${seasons.length} season(s) · ${progTeams} team(s)</span>${
          reassignBtn("league", "organization", program, program.operator_organization_id)}${delBtn("league", program.id, program.name)}</summary>
      <div class="tn-children">${seasonRows}${treeAdd("season", "Add season to " + program.name, "f-season-league", program.id)}</div>
    </details>`;
  }).join("");
  let competition = `<section class="tree-panel" id="competition-structure">
    <div class="tree-head"><span class="tree-title">🏆 Competition structure</span>
      <span class="tree-sub">Program → Season → League → Division</span></div>
    ${leagueRows || `<div class="tn-empty">No programs yet. Add a program to begin.</div>`}
    <div class="tree-actions">${treeAdd("league", "Add program")}</div>
  </section>`;
  if (!hasPerm("manage_setup")) {
    competition = `<section class="tree-panel" id="competition-structure">
      <div class="tree-head"><span class="tree-title">🏆 Competition structure</span></div>
      <div class="tree-note">Programs, seasons, leagues, and divisions are visible to setup operators.</div></section>`;
  }

  // -- Permanent teams (#283 Slice B): a team belongs permanently to a LEAGUE
  // (Team.league_id), which belongs permanently to a Program — membership that
  // persists across Seasons. This is the first-class place a team "exists";
  // its Season/Division participation is shown separately below. Consumed from
  // the canonical hierarchy (hv.programs[].leagues[] + teams_without_league),
  // so nesting matches the server's parentage exactly. Each team offers a
  // league-to-league transfer (⇄ Move, promotion/relegation) and a Club move;
  // teams with no permanent League yet are surfaced for inline assignment. --
  const permTeamLeaf = (t, curLeagueId, programId) =>
    `<div class="tn-leaf"><span class="tn-label">👥 ${esc(t.name)}</span>${
      t.player_count != null ? `<span class="tn-meta">${t.player_count} player${t.player_count === 1 ? "" : "s"}</span>` : ""}${
      reassignBtn("team", "league", t, curLeagueId, "", programId)}${
      reassignBtn("team", "club", t, t.club_id)}${delBtn("team", t.id, t.name)}</div>`;
  const permanentTeams = `<section class="tree-panel">
    <div class="tree-head"><span class="tree-title">👥 Permanent teams</span>
      <span class="tree-sub">Program → League → its permanent member teams</span></div>
    ${(hv.programs || []).map((program) => {
      const leagues = program.leagues || [];
      const loose = program.teams_without_league || [];
      const totalTeams = leagues.reduce((n, lg) => n + (lg.teams || []).length, 0) + loose.length;
      const leagueBlocks = leagues.map((lg) => {
        const teams = lg.teams || [];
        const scount = lg.season_count || 0;
        const rows = teams.map((t) => permTeamLeaf(t, lg.id, program.id)).join("")
          || `<div class="tn-empty">No teams yet. Add a permanent team to ${esc(lg.name)}.</div>`;
        return `<details class="tn" open><summary class="tn-sum">
            <span class="tn-label">🎚️ ${esc(lg.name)}</span>
            <span class="tn-meta">${teams.length} team${teams.length === 1 ? "" : "s"} · ${scount} season${scount === 1 ? "" : "s"}</span></summary>
          <div class="tn-children">${rows}${treeAdd("team", "Add team to " + lg.name, "f-team-perm-league", lg.id)}</div>
        </details>`;
      }).join("");
      // Teams with a Program but no permanent League yet — surfaced (never
      // hidden) with a ⇄ Move control so an operator can assign one inline.
      const looseBlock = loose.length
        ? `<details class="tn" open><summary class="tn-sum">
            <span class="tn-label tn-warn-text">🏅 No league</span>
            <span class="tn-meta">${loose.length} team${loose.length === 1 ? "" : "s"}</span></summary>
          <div class="tn-children">${loose.map((t) => permTeamLeaf(t, "", program.id)).join("")}</div>
        </details>` : "";
      const inner = (leagueBlocks || looseBlock)
        ? `${leagueBlocks}${looseBlock}`
        : `<div class="tn-empty">No leagues in this program yet. Add a league under a season first.</div>`;
      return `<details class="tn" open><summary class="tn-sum">
          <span class="tn-label">🏆 ${esc(program.name)}</span>
          <span class="tn-meta">${totalTeams} team${totalTeams === 1 ? "" : "s"}</span></summary>
        <div class="tn-children">${inner}</div>
      </details>`;
    }).join("") || `<div class="tn-empty">No programs yet. Add a program, then its teams.</div>`}
  </section>`;

  // -- Roster: Team → Players (operator-only; player names) --
  let roster;
  if (!canSeePlayers) {
    roster = `<section class="tree-panel">
      <div class="tree-head"><span class="tree-title">🧑 Rosters</span></div>
      <div class="tree-note">Player rosters are visible to setup operators.</div></section>`;
  } else {
    const teamRows = (ov.teams || []).map((t) => {
      const ps = pByTeam[t.id] || [];
      const badge = ps.length ? "" : `<span class="tn-badge warn">No players</span>`;
      const pRows = ps.map((p) =>
        `<div class="tn-leaf"><span class="tn-label">🧑 ${esc(p.name)}</span><span class="tn-meta">${
          esc(capWord(p.position))}${p.jersey_number != null ? " · #" + esc(String(p.jersey_number)) : ""}</span>${
          reassignBtn("player", "team", p, p.team_id)}</div>`).join("")
        || `<div class="tn-empty">No players yet. Add players before building rosters.</div>`;
      return `<details class="tn"><summary class="tn-sum">
          <span class="tn-label">👥 ${esc(t.name)}</span>
          <span class="tn-meta">${ps.length} player${ps.length === 1 ? "" : "s"}</span>${badge}</summary>
        <div class="tn-children">${pRows}${treeAdd("player", "Add player to " + t.name, "f-player-team", t.id)}</div>
      </details>`;
    }).join("");
    roster = `<section class="tree-panel">
      <div class="tree-head"><span class="tree-title">🧑 Rosters</span>
        <span class="tree-sub">${(ov.teams || []).length} team(s)</span></div>
      ${teamRows || `<div class="tn-empty">No teams yet.</div>`}
    </section>`;
  }

  // -- Needs assignment: broken/orphaned records, never hidden --
  const venueIds = new Set((ov.venues || []).map((v) => v.id));
  const teamIds = new Set((ov.teams || []).map((t) => t.id));
  const orphanRinks = (ov.rinks || []).filter((r) => !r.venue_id || !venueIds.has(r.venue_id));
  const noClubTeams = (ov.teams || []).filter((t) => !t.club_id && !t.club_name);
  const orphanPlayers = canSeePlayers
    ? playersList.filter((p) => !p.team_id || !teamIds.has(p.team_id)) : [];
  // Each orphan row carries the same "⇄ Move" control (labelled by its fix)
  // so an operator can assign a parent inline — the primary fix surface (#166).
  const naRow = (label, rows, fix) => rows.length
    ? `<div class="na-group"><div class="na-group-label">${esc(label)} (${rows.length})</div>${
        rows.map((r) => `<div class="tn-leaf warn"><span class="tn-label">⚠ ${esc(r.name)}</span>${
          fix ? fix(r) : ""}</div>`).join("")}</div>` : "";
  // Season registration issues the canonical hierarchy detects (#233 B2a
  // review r1) — a team's League/Division no longer agrees with its
  // registration, or the team/league itself is gone. Fixed in Slice B2b's
  // registration UI. A row's diagnostic reason is always shown; a
  // registration whose Team still resolves gets a repair control — a
  // League(required)+Division(optional) cascade for a League/Division
  // mismatch (mirrors renderSeasonParticipation's own regRow cascade,
  // scoped to the ISSUE's own season via season_leagues), or a plain Remove
  // when the Team itself is gone/foreign (not safely repairable here — see
  // REPAIRABLE_REG_REASONS below). Never hidden manually; a repaired/removed
  // row simply stops appearing once the backend's needs_assignment no longer
  // reports it (#233 B2b review — repair surface for invalid registrations).
  const regTeamName = (tid) => (((ov.teams || []).find((t) => t.id === tid)) || {}).name || tid;
  // Only a League/Division inconsistency is safely repairable inline: the
  // registration and Team are real, just mis-parented. team_missing (no such
  // Team) and team_program_mismatch (a cross-Program Team) both need
  // out-of-scope reparenting decisions, so they only offer Remove.
  const REPAIRABLE_REG_REASONS = new Set([
    "registration_league_division_mismatch", "registration_league_not_in_season"]);
  const repairRemoveBtn = (issue) => `<button class="icon-btn danger" data-repair-remove="${esc(issue.registration_id)}"
      title="Remove from season" aria-label="Remove ${esc(regTeamName(issue.team_id))} from ${esc(issue.season_name)}">${ICONS.circleMinus}</button>`;
  const regIssueRow = (issue) => {
    const diag = `<span class="tn-label">⚠ ${esc(regTeamName(issue.team_id))}</span>
          <span class="tn-meta">${esc(REG_REASON_LABEL[issue.reason] || issue.reason)}</span>`;
    if (!REPAIRABLE_REG_REASONS.has(issue.reason)) {
      return `<div class="tn-leaf warn repair-row">${diag}${repairRemoveBtn(issue)}</div>`;
    }
    // The League select is scoped to the issue's OWN season (issue.season_leagues,
    // attached when seasonRegIssues was built above) — never the whole program.
    // A registration_league_not_in_season issue's current league_id won't match
    // any option here, so nothing starts selected; that's expected.
    const leagues = issue.season_leagues || [];
    const leagueOpts = leagues.map((lv) => opt(lv.id, lv.name, lv.id === issue.league_id)).join("");
    const curLeague = leagues.find((lv) => lv.id === issue.league_id);
    const divOpts = ((curLeague && curLeague.divisions) || [])
      .map((d) => opt(d.id, d.name, d.id === issue.division_id)).join("");
    return `<div class="tn-leaf warn repair-row">${diag}
      <select id="repair-league-${esc(issue.registration_id)}" data-repair-league-for="${esc(issue.registration_id)}">
        <option value="">Choose a league…</option>${leagueOpts}</select>
      <select id="repair-div-${esc(issue.registration_id)}" data-repair-div-for="${esc(issue.registration_id)}">
        <option value="">No division</option>${divOpts}</select>
      <button class="act" data-repair-save="${esc(issue.registration_id)}"
        data-repair-orig-league="${esc(issue.league_id || "")}"
        data-repair-orig-div="${esc(issue.division_id || "")}">Save</button>
      ${repairRemoveBtn(issue)}</div>`;
  };
  const regIssueRows = seasonRegIssues.length
    ? `<div class="na-group"><div class="na-group-label">Season registrations needing attention (${seasonRegIssues.length})</div>${
        seasonRegIssues.map(regIssueRow).join("")}</div>` : "";
  const naBody = naRow("Rinks without a venue", orphanRinks,
            (r) => reassignBtn("rink", "venue", r, r.venue_id))
    + naRow("Teams without a club", noClubTeams,
            (t) => reassignBtn("team", "club", t, t.club_id))
    + naRow("Players without a team", orphanPlayers,
            (p) => reassignBtn("player", "team", p, p.team_id))
    + regIssueRows;
  const needsAssignment = naBody
    ? `<section class="tree-panel na"><div class="tree-head"><span class="tree-title">⚠ Needs assignment</span></div>
        <div class="tree-note">These records can't be scheduled until they're assigned.</div>${naBody}</section>` : "";

  return `${reassignPanelHtml(ov, sv)}<div class="setup-trees">${facility}${permanentTeams}${competition}${renderSeasonParticipation(hv, ov, sv)}${renderRollover(hv, ov)}${roster}${needsAssignment}</div>`;
}

// Season participation (#180, cut to v2 canonical #233 Slice B2b): permanent
// program teams and which season/league/division each plays. Kept separate
// from the Competition tree above (which reads hv read-only) so the two ideas
// — a team's permanent program membership vs. its per-season participation —
// read distinctly. Grouped Program → Season → League (three levels, matching
// hv.programs[].seasons[].leagues[]), since a v2 registration's League is
// REQUIRED and its Division is an optional split of that League. leagueTeams
// (permanent roster) and seasonRegs (registration rows, each carrying a
// league_id) are the dedicated v2 reads loaded in render(); hv supplies the
// Program→Season→League→Division shape and each league's current teams.
function renderSeasonParticipation(hv, ov, sv) {
  if (!hasPerm("manage_setup")) return "";
  const programs = (hv.programs || []).filter((p) => (p.seasons || []).length);
  if (!programs.length) return "";
  // #369: unions the additive `unassigned_venues` bucket -- this is the
  // "Allow a venue" picker, i.e. the very control that CREATES a Venue's
  // first link to a Season. A just-created Venue has no SeasonVenueAccess
  // grant yet, so under the strict active-Program scoping it is by
  // definition not in `sv.venues`; without the union it could never be
  // granted to anything and the Setup flow would deadlock.
  const allVenues = sv ? withPendingLink(sv, "venues") : [];
  // The facility-tree exception (#369 owner ruling): a Season may be granted
  // access to ANY Venue, including one another Program already uses -- an
  // arena serves several leagues. `grantable_venues` carries id+name only, and
  // deliberately nothing about Rinks, IceSlots or the competition tree.
  // Falls back to the scoped union so an older payload still renders.
  // Established shared facilities, UNIONED with this caller's own scoped and
  // pending-link venues -- a Venue it just created is linked to nothing yet, so
  // it is deliberately absent from `grantable_venues` (that list must not carry
  // other operators' unlinked drafts) and comes in through the union instead.
  // Per-Season grant candidates, unioned with this caller's own scoped and
  // pending-link venues. Keyed by Season because the contract is per
  // destination Season -- see get_venue_grant_candidates.
  const grantableFor = (seasonId) => {
    const seen = new Set();
    const out = [];
    for (const v of (seasonVenueCandidates[seasonId] || []).concat(allVenues)) {
      if (v && v.id && !seen.has(v.id)) { seen.add(v.id); out.push(v); }
    }
    return out;
  };
  // #369 owner ruling: the venue-access list and the candidate route both
  // require the requested Season to BE the persisted selected Season, so this
  // tree is no longer an all-Seasons venue inventory. Only the selected Season
  // renders its allowed venues, its Revoke controls, its revoked-access
  // cleanup rows and its Allow picker; every other Season of the Program gets
  // a placeholder that names nothing (no venue id, no venue name, no count)
  // and for which render() issued no request at all. `allVenues` is the reason
  // this gate has to exist on the render side too and not only in the fetch
  // loop: `grantableFor` unions the scoped overview venues, so an ungated
  // Allow picker would still list real facilities under a Season the operator
  // has not selected.
  const selectedSeasonId = (contextOptions && contextOptions.selected
    && contextOptions.selected.season_id) || null;
  // ...and when that selection is ARCHIVED the surface is HISTORY, not a
  // workbench (#369 owner ruling, follow-up). An archived Season is read-only:
  // grant, revoke and permanent-cleanup all fail `season_archived`, and the
  // candidate route now refuses the read that feeds the Allow picker. So the
  // selected Season still shows its OWN allowed-venues history, names included
  // -- that is what a historical Season is for -- but offers no control that
  // cannot succeed. Read off `contextOptions.selected.read_only`, the same
  // signal behind the switcher's "archived (read-only)" label and #ctx-ro
  // badge, so this can never disagree with what the context bar shows.
  const selectionIsReadOnly = !!(contextOptions && contextOptions.selected
    && contextOptions.selected.read_only);

  const programBlocks = programs.map((program) => {
    const permanentTeams = leagueTeams[program.id] || [];
    const seasons = program.seasons || [];

    const seasonBlocks = seasons.map((s) => {
      const regs = (seasonRegs[s.id] || []).filter((r) => r.active);
      // #331 review round 19: keyed by registration id, never re-derived
      // from (team_id, league_id) — round 18's fix (see below), which still
      // collapses two ACTIVE rows sharing the exact same target (the Memory
      // multiplicity round 19 finding 1 now rejects at every write path,
      // still reachable as legacy/injected data). get_setup_hierarchy_v2
      // already emits one distinct tree entry per registration in that case
      // (teams_by_div/teams_direct_by_league append one (team, reg) pair
      // per registration, so a Team with two rows at one target appears
      // TWICE), so trusting each entry's own registration_id — rather than
      // re-deriving "the" registration from the team/league it happens to
      // sit under — is what makes both rows independently addressable
      // instead of two identical-looking rows that are secretly the same
      // control twice.
      const regsById = {};
      regs.forEach((r) => { regsById[r.id] = r; });
      // #331 review round 20: a duplicate-target pair (finding 1's Memory-only
      // corruption) renders as two rows with the SAME visible team name and
      // the SAME accessible name on their Save/Remove controls (identical
      // `t.name`/`s.name`, the only inputs the aria-label was built from) —
      // reachable and independently addressable via the reg-id keying above,
      // but indistinguishable to a screen reader. Counted per (team_id,
      // league_id) so the common single-row case stays exactly as before.
      const dupKeyCounts = {};
      regs.forEach((r) => {
        const k = `${r.team_id}::${r.league_id}`;
        dupKeyCounts[k] = (dupKeyCounts[k] || 0) + 1;
      });
      const registeredTeamIds = new Set(regs.map((r) => r.team_id));
      const leagues = s.leagues || [];
      // Cache each League's Division options for the cascade handlers wired
      // later in wireApp — a League select's onchange rescopes its paired
      // Division select using this map, never guessing a same-named division.
      leagues.forEach((lv) => { leagueDivisions[lv.id] = lv.divisions || []; });
      const leagueOptsFor = (selId) => leagues.map((lv) =>
        opt(lv.id, lv.name, lv.id === selId)).join("");

      // Inactive registrations (#251): unregister_team_from_season only
      // deactivates a row — Season/Team/League identity (and Division, if it
      // had one) is retained for history, which is correct, but that history
      // is otherwise invisible and silently blocks League/Season/Team
      // deletes. Surface every inactive row here with a permanent-cleanup
      // trash action; a row is only actually removable once no Game (draft,
      // scheduled, cancelled, or historical) still references it — the
      // has_dependencies path is handled generically by the confirm/blocked
      // modal flow (delBtn → confirmDeleteModalHtml/blockedModalHtml).
      const teamNameById = {};
      permanentTeams.forEach((t) => { teamNameById[t.id] = t.name; });
      const leagueNameById = {};
      const divisionNameById = {};
      leagues.forEach((lv) => {
        leagueNameById[lv.id] = lv.name;
        (lv.divisions || []).forEach((d) => { divisionNameById[d.id] = d.name; });
      });
      const inactiveRegs = (seasonRegs[s.id] || []).filter((r) => !r.active);
      const inactiveRows = inactiveRegs.map((r) => {
        const teamName = teamNameById[r.team_id] || r.team_id;
        const leagueName = r.league_id ? (leagueNameById[r.league_id] || r.league_id) : "";
        const divisionName = r.division_id ? (divisionNameById[r.division_id] || r.division_id) : "";
        const where = [leagueName, divisionName].filter(Boolean).join(" / ") || "no league";
        return `<div class="tn-leaf reg-row inactive-reg">
          <span class="tn-label">👥 ${esc(teamName)}</span>
          <span class="tn-meta">${esc(where)} · inactive · <code>${esc(r.id)}</code></span>
          ${delBtn("season-team-registration", r.id, `${teamName} registration`,
            "Permanently remove this inactive registration")}</div>`;
      }).join("");
      const inactiveSection = inactiveRegs.length
        ? `<details class="tn" open><summary class="tn-sum">
            <span class="tn-label">🗑️ Inactive registrations</span>
            <span class="tn-meta">${inactiveRegs.length} row${inactiveRegs.length === 1 ? "" : "s"}</span></summary>
          <div class="tn-children">${inactiveRows}</div></details>`
        : "";

      // Allowed venues (#233 Slice E): which Venues this Season may schedule
      // game ice on, via SeasonVenueAccess — independent of any Venue-Program
      // ownership. A Venue may be granted to several Seasons/Programs at
      // once, and a Season may use several Venues.
      // Resolve names from the grantable set too: a Venue this Season holds a
      // grant to may belong to another Program, so its name is not in the
      // Season-ceilinged `venues` -- and the "Allowed venues" row must still
      // name it rather than showing a bare id.
      // ...but only for the SELECTED Season (#369 owner ruling). Every other
      // Season here has no grant rows and no candidates because render()
      // deliberately asked for neither, so it renders a placeholder rather
      // than an empty-looking list that would read as "this season has no
      // venues" — a claim this surface is no longer entitled to make.
      // ...and when the SELECTED Season is archived it is read-only HISTORY:
      // its own grant rows still render, names and all, but every control that
      // would mutate them is gone (see `selectionIsReadOnly` above).
      const isSelectedSeason = s.id === selectedSeasonId;
      const isHistoricalSeason = isSelectedSeason && selectionIsReadOnly;
      const seasonAccessRows = isSelectedSeason
        ? (seasonVenueAccess[s.id] || []) : [];
      const venueNameById = {};
      // No candidates were fetched for a read-only selection, and `grantable`
      // also unions the scoped overview venues -- so this gate has to exist
      // here and not only in the fetch loop, or the picker would still list
      // real facilities under an archived Season that can grant none of them.
      const grantable = (isSelectedSeason && !isHistoricalSeason)
        ? grantableFor(s.id) : [];
      grantable.forEach((v) => { venueNameById[v.id] = v.name; });
      allVenues.forEach((v) => { venueNameById[v.id] = v.name; });
      // A Venue this Season ALREADY holds a grant to is deliberately not a
      // candidate, and a cross-Program one is not in the scoped `venues`
      // either -- so the grant row itself is the only place its name survives.
      // That is the `venue_name` resolution the selected Season still needs
      // for a shared arena owned by another Program, whose name is in neither
      // the scoped venue list nor the candidate list.
      seasonAccessRows.forEach((a) => {
        if (a.venue_name) venueNameById[a.venue_id] = a.venue_name;
      });
      const grantedAccess = seasonAccessRows.filter((a) => a.active);
      const grantedVenueIds = new Set(grantedAccess.map((a) => a.venue_id));
      // A historical row names its Venue and carries NO Revoke control — the
      // revoke would fail `season_archived`, so offering it would be a button
      // that cannot succeed.
      const venueAccessRows = grantedAccess.map((a) => {
        const venueName = venueNameById[a.venue_id] || a.venue_id;
        return isHistoricalSeason
          ? `<div class="tn-leaf reg-row">
          <span class="tn-label">🏟️ ${esc(venueName)}</span>
          <span class="tn-meta">read-only history</span></div>`
          : `<div class="tn-leaf reg-row">
          <span class="tn-label">🏟️ ${esc(venueName)}</span>
          <button class="icon-btn danger" data-va-revoke="${esc(a.id)}"
            title="Revoke venue access" aria-label="Revoke ${esc(venueName)} from ${esc(s.name)}">${ICONS.circleMinus}</button></div>`;
      }).join("")
        || (isHistoricalSeason
            ? `<div class="tn-empty">This archived season never had a venue allowed.</div>`
            : `<div class="tn-empty">No venues allowed for this season yet — games can't be scheduled until one is added.</div>`);
      const availableVenues = grantable.filter(
        (v) => !grantedVenueIds.has(v.id));
      // The archived branch replaces the Allow picker with explicit read-only
      // copy: no <select>, no Allow button, and nothing that reads as "you
      // could add one here".
      const venueAddCtl = isHistoricalSeason
        ? `<div class="tn-empty" data-va-readonly="${esc(s.id)}">This season is archived and read-only — venue access can't be allowed, revoked or removed. Reopen the season to change it.</div>`
        : (availableVenues.length
          ? `<div class="tn-leaf reg-add">
            <select id="va-add-${esc(s.id)}"><option value="">Add a venue…</option>${
              availableVenues.map((v) => opt(v.id, v.name)).join("")}</select>
            <button class="act primary" data-va-add="${esc(s.id)}">Allow</button></div>`
          : (grantable.length
              ? `<div class="tn-empty">Every venue is already allowed for this season.</div>`
              : `<div class="tn-empty">Create a venue on the Facility tree first, then allow it here.</div>`));
      // The placeholder for a non-selected Season carries NO venue id, NO
      // venue name and NO count — a count would still be an inventory, just a
      // coarser one — and offers no Allow picker, because granting is a write
      // against a Season this operator has not selected.
      const venueAccessSection = isSelectedSeason
        ? `<details class="tn" open><summary class="tn-sum">
          <span class="tn-label">🏟️ Allowed venues</span>
          <span class="tn-meta">${grantedAccess.length} venue${grantedAccess.length === 1 ? "" : "s"}${isHistoricalSeason ? " · archived (read-only)" : ""}</span></summary>
        <div class="tn-children">${venueAccessRows}${venueAddCtl}</div></details>`
        : `<details class="tn"><summary class="tn-sum">
          <span class="tn-label">🏟️ Allowed venues</span>
          <span class="tn-meta">not loaded</span></summary>
        <div class="tn-children"><div class="tn-empty">Select this season to manage its venues.</div></div></details>`;

      // Revoked venue access (#233 Slice E, mirrors #251's inactive
      // registrations exactly): revoke only deactivates a row, preserving
      // history — but delete_season()/delete_venue() block on a matching
      // access row REGARDLESS of active status, so a revoked row still needs
      // this explicit, audited permanent-cleanup path before the Season or
      // Venue it references can ever be deleted.
      // Selected-Season-only for the same reason: a revoked row names its
      // Venue and carries a permanent-delete control, so it is exactly the
      // kind of row a non-selected Season must not put on screen.
      // Suppressed WHOLESALE for an archived selection: this section exists
      // only to host the permanent-cleanup action, and that delete fails
      // `season_archived` like every other write the Season owns. The
      // read-only copy in the Allowed-venues section above says so explicitly,
      // so the surface still explains itself rather than silently dropping it.
      // Revoked grants are HISTORY, and history stays readable on an archived
      // Season exactly as the active grants above do -- the API preserves these
      // rows deliberately. An earlier revision of this read-only work emptied
      // the list, which hid a Season's own past instead of hiding the controls
      // that act on it. Only the permanent-cleanup button is a mutation, so
      // only that is withheld.
      const revokedAccess = seasonAccessRows.filter((a) => !a.active);
      const revokedAccessRows = revokedAccess.map((a) => {
        const venueName = venueNameById[a.venue_id] || a.venue_id;
        return `<div class="tn-leaf reg-row inactive-reg">
          <span class="tn-label">🏟️ ${esc(venueName)}</span>
          <span class="tn-meta">revoked · <code>${esc(a.id)}</code>${
            isHistoricalSeason ? " · read-only history" : ""}</span>
          ${isHistoricalSeason ? "" : delBtn("season-venue-access", a.id,
            `${venueName} access`,
            "Permanently remove this revoked venue access")}</div>`;
      }).join("");
      const revokedAccessSection = revokedAccess.length
        ? `<details class="tn" open><summary class="tn-sum">
            <span class="tn-label">🗑️ Revoked venue access</span>
            <span class="tn-meta">${revokedAccess.length} row${revokedAccess.length === 1 ? "" : "s"}</span></summary>
          <div class="tn-children">${revokedAccessRows}</div></details>`
        : "";

      const leagueSections = leagues.map((lv) => {
        const divs = lv.divisions || [];
        const divOptsFor = (selId) => divs.map((d) => opt(d.id, d.name, d.id === selId)).join("");
        // A registered team's row: its own League+Division cascade (both
        // scoped to this season) plus Save/Remove. `reg` is looked up by
        // THIS tree entry's own registration_id — #331 review round 19:
        // never re-derived from (team id, league id), which collapses to
        // one winning row when a Team has two ACTIVE registrations at the
        // exact same target (round 18's fix only separated DIFFERENT
        // League targets). Trusting the id `t` already carries is what lets
        // two such rows — same team, same league, genuinely different
        // registrations — render as independently addressable controls
        // instead of one control duplicated twice.
        const regRow = (t, divId) => {
          const reg = regsById[t.registration_id];
          if (!reg) return "";
          // #331 review round 20: when two rows share this exact (team,
          // league) target, their visible name and their controls' accessible
          // names would otherwise be byte-for-byte identical (both built from
          // the same t.name/s.name) -- indistinguishable to a screen reader
          // even though the controls underneath are independently addressable
          // by registration id. Only the duplicate case gets the suffix; the
          // ordinary single-row case is unchanged.
          const dup = dupKeyCounts[`${t.id}::${lv.id}`] > 1;
          const distinguisher = dup ? ` (registration ${esc(reg.id)})` : "";
          return `<div class="tn-leaf reg-row">
            <span class="tn-label">👥 ${esc(t.name)}${dup ? ` <code>${esc(reg.id)}</code>` : ""}</span>
            <select id="reg-league-${esc(reg.id)}" data-reg-league-for="${esc(reg.id)}">${leagueOptsFor(lv.id)}</select>
            <select id="reg-div-${esc(reg.id)}" data-reg-div-for="${esc(reg.id)}"><option value="">No division</option>${divOptsFor(divId)}</select>
            <button class="act" data-reg-save="${esc(reg.id)}" data-reg-orig-league="${esc(lv.id)}" data-reg-orig-div="${esc(divId || "")}"${
              dup ? ` aria-label="Save ${esc(t.name)}${distinguisher} in ${esc(s.name)}"` : ""}>Save</button>
            <button class="icon-btn danger" data-reg-remove="${esc(reg.id)}"
              title="Remove from season" aria-label="Remove ${esc(t.name)}${distinguisher} from ${esc(s.name)}">${ICONS.circleMinus}</button></div>`;
        };
        const divRows = divs.map((d) => (d.teams || []).map((t) => regRow(t, d.id)).join("")).join("");
        const twdRows = (lv.teams_without_division || []).map((t) => regRow(t, "")).join("");
        const rows = `${divRows}${twdRows}`
          || `<div class="tn-empty">No teams registered under this league yet.</div>`;
        const available = permanentTeams.filter((t) => !registeredTeamIds.has(t.id));
        const addCtl = available.length
          ? `<div class="tn-leaf reg-add">
              <select id="reg-team-${esc(s.id)}-${esc(lv.id)}"><option value="">Add a program team…</option>${
                available.map((t) => opt(t.id, t.name)).join("")}</select>
              <select id="reg-league-add-${esc(s.id)}-${esc(lv.id)}" data-reg-league-add="${esc(s.id)}-${esc(lv.id)}">${leagueOptsFor(lv.id)}</select>
              <select id="reg-div-add-${esc(s.id)}-${esc(lv.id)}"><option value="">No division</option>${divOptsFor("")}</select>
              <button class="act primary" data-reg-add="${esc(lv.id)}" data-reg-add-season="${esc(s.id)}">Register</button></div>`
          : (permanentTeams.length
              ? `<div class="tn-empty">Every program team is registered for this season.</div>`
              : `<div class="tn-empty">Create a program team first, then register it here.</div>`);
        const teamCount = divs.reduce((n, d) => n + (d.teams || []).length, 0)
          + (lv.teams_without_division || []).length;
        return `<details class="tn" open><summary class="tn-sum">
            <span class="tn-label">🎚️ ${esc(lv.name)}</span>
            <span class="tn-meta">${teamCount} team${teamCount === 1 ? "" : "s"}</span>
            ${delBtn("level", lv.id, lv.name)}</summary>
          <div class="tn-children">${rows}${addCtl}</div></details>`;
      }).join("");
      const leagueBlocks = leagueSections
        || `<div class="tn-empty">No leagues in this season yet. Add a league under
            Competition structure, then register teams here.</div>`;
      const teamCount = leagues.reduce((n, lv) =>
        n + (lv.divisions || []).reduce((m, d) => m + (d.teams || []).length, 0)
          + (lv.teams_without_division || []).length, 0);
      return `<details class="tn" open><summary class="tn-sum">
          <span class="tn-label">🗓️ ${esc(s.name)}</span>
          <span class="tn-meta">${leagues.length} league${leagues.length === 1 ? "" : "s"} · ${
            teamCount} team${teamCount === 1 ? "" : "s"}</span>
          ${delBtn("season", s.id, s.name)}</summary>
        <div class="tn-children">${leagueBlocks}${venueAccessSection}${revokedAccessSection}${inactiveSection}</div></details>`;
    }).join("");

    // Permanent members not registered for ANY season this program — surfaced
    // so an operator sees teams that exist but sit out every current season.
    const idle = permanentTeams.filter((t) => !seasons.some(
      (s) => (seasonRegs[s.id] || []).some((r) => r.active && r.team_id === t.id)));
    const idleRows = idle.length
      ? `<div class="tn-leaf reg-row"><span class="tn-meta">${idle.length} program team${
          idle.length === 1 ? "" : "s"} not in any current season: ${
          idle.map((t) => esc(t.name)).join(", ")}</span></div>` : "";
    return `<details class="tn" open><summary class="tn-sum">
        <span class="tn-label">🏆 ${esc(program.name)}</span>
        <span class="tn-meta">${permanentTeams.length} program team${permanentTeams.length === 1 ? "" : "s"}</span>
        ${delBtn("league", program.id, program.name)}</summary>
      <div class="tn-children">${seasonBlocks}${idleRows}</div></details>`;
  }).join("");

  return `<section class="tree-panel" id="season-participation">
    <div class="tree-head"><span class="tree-title">🗓️ Season participation</span>
      <span class="tree-sub">Permanent program teams → season, league &amp; division they play</span></div>
    ${programBlocks}</section>`;
}

// Season rollover (#180, cut to v2 canonical #233 Slice B2b): carry a prior
// season's participation forward into a new season of the SAME program,
// reusing the permanent teams. A bounded, functional picker — choose a source
// and a target season, pick which eligible teams to carry and each one's
// target-season League (required) and optional Division, then commit through
// the hardened v2 rollover service (services/setup_service.
// roll_forward_registrations_v2), which re-checks program membership and
// League/Division consistency before any write. Unlike the FROZEN v1 route,
// v2 has no division-only mode — every selection needs an explicit target
// League. Source rows that aren't permanent members of this program (orphaned
// or cross-program data) and teams already active in the target are shown but
// never sent. The full Setup and navigation redesign stays out of this slice
// (#204).
function renderRollover(hv, ov) {
  if (!hasPerm("manage_setup")) return "";
  // A rollover needs a source and a distinct target, so only programs with at
  // least two seasons can be rolled.
  const eligiblePrograms = (hv.programs || []).filter((p) => (p.seasons || []).length >= 2);
  if (!eligiblePrograms.length) return "";
  let programId = rollover.programId;
  if (!eligiblePrograms.some((p) => p.id === programId)) programId = eligiblePrograms[0].id;
  const program = eligiblePrograms.find((p) => p.id === programId);
  const seasons = program.seasons || [];
  const fromId = seasons.some((s) => s.id === rollover.fromSeasonId) ? rollover.fromSeasonId : "";
  const toId = seasons.some((s) => s.id === rollover.toSeasonId) && rollover.toSeasonId !== fromId
    ? rollover.toSeasonId : "";
  const permanentTeams = leagueTeams[programId] || [];
  const teamById = (tid) => permanentTeams.find((t) => t.id === tid);

  const programSel = eligiblePrograms.length > 1
    ? `<label class="ro-field">Program
        <select data-rollover-program>${eligiblePrograms.map((p) =>
          opt(p.id, p.name, p.id === programId)).join("")}</select></label>` : "";
  const fromSel = `<label class="ro-field">Copy from
      <select data-rollover-from><option value="">Choose a season…</option>${
        seasons.map((s) => opt(s.id, s.name, s.id === fromId)).join("")}</select></label>`;
  const toSel = `<label class="ro-field">Into
      <select data-rollover-to><option value="">Choose a season…</option>${
        seasons.filter((s) => s.id !== fromId).map((s) =>
          opt(s.id, s.name, s.id === toId)).join("")}</select></label>`;

  let body;
  if (!fromId || !toId) {
    body = `<div class="tn-empty">Choose a source and a target season to preview which teams can be carried forward.</div>`;
  } else {
    const toSeason = seasons.find((s) => s.id === toId);
    const toLeagues = (toSeason && toSeason.leagues) || [];
    // Cache each target League's Division options for the cascade handler
    // wired in wireApp — a League select's onchange rescopes its paired
    // Division select using this map.
    toLeagues.forEach((lv) => { leagueDivisions[lv.id] = lv.divisions || []; });
    // Split the source season's active registrations three ways using the data
    // already loaded for Season participation: teams eligible to carry, teams
    // already registered in the target (the service skips these), and source
    // rows whose team isn't a permanent member of this program (orphaned or
    // cross-program — the service rejects these, so they're never offered).
    const srcRegs = (seasonRegs[fromId] || []).filter((r) => r.active);
    const targetActive = new Set((seasonRegs[toId] || [])
      .filter((r) => r.active).map((r) => r.team_id));
    // #331 review round 19: a Team can hold more than one active SOURCE
    // registration in the same Season (a Rule 7 violation legacy data / a
    // write path predating Rule 7 can leave behind, or -- Memory only --
    // two rows at the exact same key, the corruption finding 1 this same
    // round closes off at every write path). `eligible` keeps each row's
    // OWN registration alongside its team, never just the team, so two
    // rows for one team render as genuinely distinct, independently
    // selectable entries instead of colliding on a team-id-only key.
    // `already` still dedupes by team (informational only, never a pick
    // target) so a team with two source rows doesn't list itself twice.
    const eligible = [], ineligible = [];
    const alreadyByTeam = new Map();
    srcRegs.forEach((r) => {
      const team = teamById(r.team_id);
      if (!team) { ineligible.push(r); return; }
      if (targetActive.has(r.team_id)) { alreadyByTeam.set(team.id, team); return; }
      eligible.push({ reg: r, team });
    });
    const already = [...alreadyByTeam.values()];
    // Every carried team must land under a concrete target-season League — the
    // v2 rollover has no division-only mode, so a null-league selection can
    // never be sent (#233 Slice C2). Rows start unchecked (no implicit bulk
    // assignment); the per-row League defaults to a "Choose a league…"
    // placeholder unless the target season has exactly one League, which is
    // preselected. Division stays optional and defaults to "No division",
    // scoped to whichever League is currently selected. The commit button
    // stays disabled until every checked team has a League.
    // #283 Slice E: a Team only ever rolls into its OWN permanent League — the
    // backend rejects any other. So each row's target League is FIXED to the
    // team's permanent League (never a free picker), and its Division options
    // are that League's divisions in the target season.
    // #331 review round 19: every per-row control keys off the SOURCE
    // registration's own id, never team.id -- two rows for the same team
    // (see the eligible/already split above) must never collide on a
    // shared data-rollover-* value. data-rollover-team stays on the
    // checkbox alongside it so the commit handler can still report which
    // team a picked row belongs to without re-deriving it from the DOM.
    const carryRows = eligible.map(({ reg, team }) => {
      const perm = teamPermLeague[team.id];
      const permInTo = perm && toLeagues.find((lv) => lv.id === perm.id);
      const leagueCell = perm
        ? `<select class="reg-league" data-rollover-league="${esc(reg.id)}"><option value="${esc(perm.id)}" selected>${esc(perm.name)}</option></select>`
        : `<select class="reg-league" data-rollover-league="${esc(reg.id)}"><option value="">No permanent league</option></select>`;
      const initialDivs = permInTo ? (permInTo.divisions || []) : [];
      return `<div class="tn-leaf reg-row">
        <label class="ro-pick"><input type="checkbox" data-rollover-pick="${esc(reg.id)}" data-rollover-team="${esc(team.id)}">
          <span class="tn-label">👥 ${esc(team.name)}</span></label>
        ${leagueCell}
        <select class="reg-div" data-rollover-div="${esc(reg.id)}"><option value="">No division</option>${
          initialDivs.map((d) => opt(d.id, d.name)).join("")}</select>
        <span class="ro-row-err" hidden>This team has no permanent league</span></div>`;
    }).join("");
    let carry;
    if (!toLeagues.length) {
      // The target season has no leagues — block the rollover outright rather
      // than offer a path that could only produce league-less selections,
      // which the v2 service always rejects.
      carry = `<div class="tn-empty">The target season has no leagues yet.
        Create a target league first, then roll teams into it.</div>`;
    } else if (!eligible.length) {
      carry = `<div class="tn-empty">No eligible teams left to carry into the target season.</div>`;
    } else {
      carry = `<div class="tn-leaf reg-add"><label class="ro-pick">
          <input type="checkbox" data-rollover-all> Select all (${eligible.length})</label></div>${carryRows}
        <div class="actions"><button class="act primary" data-rollover-commit disabled>Roll forward selected teams</button></div>`;
    }
    const alreadyRow = already.length
      ? `<div class="tn-leaf reg-row"><span class="tn-meta">Already registered in the target season (will be skipped): ${
          already.map((t) => esc(t.name)).join(", ")}</span></div>` : "";
    const ineligibleRow = ineligible.length
      ? `<div class="tn-leaf warn"><span class="tn-meta">⚠ ${ineligible.length} source registration${
          ineligible.length === 1 ? "" : "s"} can't be carried — the team isn't a permanent member of this program (orphaned or cross-program data).</span></div>` : "";
    body = `<div class="tn-children">${carry}${alreadyRow}${ineligibleRow}${rolloverResultHtml()}</div>`;
  }

  return `<section class="tree-panel">
    <div class="tree-head"><span class="tree-title">↪ Season rollover</span>
      <span class="tree-sub">Carry a prior season's teams into a new season of the same program</span></div>
    <div class="ro-controls">${programSel}${fromSel}${toSel}</div>
    ${body}</section>`;
}

// The rollover service returns {rolled_forward, skipped, registrations} or a
// structured {error}. Render whichever the last commit produced.
function rolloverResultHtml() {
  const res = rollover.result;
  if (!res) return "";
  if (res.error) {
    return `<div class="banner alert"><h2>Rollover failed</h2><p>${esc(res.error.message)}</p></div>`;
  }
  const parts = [`Carried ${res.rolled_forward} team${res.rolled_forward === 1 ? "" : "s"} forward`];
  if (res.skipped) parts.push(`skipped ${res.skipped} already registered`);
  return `<div class="banner ok"><h2>Rollover complete</h2><p>${esc(parts.join(" · "))}.</p></div>`;
}

// Rollover commit gate (review #216, re-scoped to League for v2 #233 Slice
// B2b): reflect the current selection without a full re-render (which would
// drop the checkbox/league/division state). A checked team with no target
// League shows an inline row error, and the commit button is enabled only
// when at least one team is checked and every checked team has a League —
// Division stays optional — matching the v2 backend's per-selection League
// requirement (the v2 rollover has no division-only mode).
function updateRolloverCommitState(c) {
  const commit = c.querySelector("[data-rollover-commit]");
  if (!commit) return;
  let anyChecked = false, allAssigned = true;
  c.querySelectorAll("[data-rollover-pick]").forEach((cb) => {
    // #331 review round 19: scoped to THIS row, not a global attribute-value
    // lookup -- two rows can share the same data-rollover-league VALUE
    // (both showing the same permanent-league option) even now that the
    // data-rollover-pick/-league/-div KEY is the row's own registration id,
    // so a value-based query is never safe here regardless of keying.
    const row = cb.closest(".reg-row");
    const league = row && row.querySelector("[data-rollover-league]");
    const assigned = !!(league && league.value);
    const err = row && row.querySelector(".ro-row-err");
    if (cb.checked) {
      anyChecked = true;
      if (!assigned) allAssigned = false;
      if (err) err.hidden = assigned;
    } else if (err) {
      err.hidden = true;
    }
  });
  commit.disabled = !(anyChecked && allAssigned);
}

// The six Setup workflows (#345 batch 2), matching the keys the backend's
// get_setup_progress already reports and goToSetupWorkflow already routes --
// one definition, so the Home/Tasks hub, the progress API and these screens
// can never name a different set of six.
//
// Each entry carries the PRIMARY action the requirements package's
// primary-action audit designates for that workflow, plus the actions that
// audit explicitly demotes. That table is a checklist, not a design decision
// taken here: one `.act.primary` per screen, everything else secondary or
// tertiary.
// #367: the selected League's own Teams, when one is active -- mirrors
// get_setup_progress's server-side narrowing of its "Permanent teams"/
// "Clubs, players and staff" workflows for the SAME two axes here.
// get_setup_overview_v2 (`sv`) is deliberately Program-wide for every
// authorized Program (a structural/management read, never League-narrowed
// server-side -- see that method's own docstring), so this is a client-side
// filter using the additive `sv.teams[].league_id` field (Team.league_id,
// the real competition-League id) -- the same client-side-filter idiom the
// division drawer already uses against `leagues[].season_ids`. "No League"
// selected (`league_id` falsy) keeps the full Program-wide set.
function leagueScopedTeams(sv) {
  const lid = contextOptions && contextOptions.selected
    && contextOptions.selected.league_id;
  const teams = sv.teams || [];
  return lid ? teams.filter((t) => t.league_id === lid) : teams;
}

// `prereq` (#365 owner ruling on EMPTY dead ends): the ordered chain of
// records this workflow's DECLARED primary action needs before it can create
// anything. "Primary" means the single action that resolves the state the
// card is actually in, not necessarily the static `primary` above -- an EMPTY
// Facilities landing that names the missing venues and rinks and then offers
// "Add Ice" (which needs a rink, which needs a venue) is a dead end: it says
// what is missing and hands over an action that cannot create it.
//
// A step is one of two kinds. `{ assert: "<key>" }` is a SERVER-asserted hard
// prerequisite, resolved against get_setup_progress's own ordered
// `workflows[].prerequisites` by setupResolvedPrereqChain (fail-closed: an
// unasserted or unreadable claim is never "met"). Everything else is a
// locally-derived step over visible row counts, declared inline as
// `{ met, action, why }`:
//   met(facts)  whether this prerequisite is satisfied, read from the LIVE
//               payload the card's own summary came from (setupPrereqFacts)
//               -- never from a declared default. Usually a COUNT of visible
//               rows. A step about a CAPABILITY rather than an inventory
//               declares `assert` instead and never writes its own `met`,
//               because no count can answer it.
//   action      the ONE control offered while it is not. `act`/`go` are the
//               ordinary landing action kinds; `open` is the third kind this
//               ruling needs -- the missing record belongs to ANOTHER
//               workflow, so the single action opens THAT workflow's landing
//               (which derives its own effective action the same way, so the
//               chain resolves recursively) rather than smuggling a foreign
//               create drawer onto this card. A create committed here would
//               be committed under THIS card's identity while changing a
//               DIFFERENT card's data, which is precisely the per-card
//               binding #365 exists to hold.
//               An action may also declare `perm`: the permission its
//               destination genuinely requires. A role that lacks it gets NO
//               action rather than a control it cannot execute -- see
//               setupEffectiveAction.
//   why         the sentence the card body uses to name the blocker, so the
//               copy and the action can never describe different problems.
//               A function of the facts where the sentence is itself an
//               asserted, scoped claim (naming the exact Season), so the copy
//               is the backend's own statement rather than a paraphrase.
//   advice      what to add to `why` when the action is WITHDRAWN (this role
//               cannot resolve this step). Defaults to "Ask a league admin to
//               set that up." -- true for a grant or a reopen, and wrong for
//               a step nobody can resolve with a control at all (picking a
//               Season is a context-bar action), which is why it is per-step
//               rather than baked into the renderer.
//
// The chain is walked in order and the FIRST unmet step wins; all met means
// the declared primary is genuinely the resolving action. Only the EMPTY
// state renders a single action, so this is what that one action is; every
// other state renders its declared groups exactly as before.
const SETUP_WORKFLOWS = [
  { key: "league_season", title: "League profile and seasons", icon: "🗓️",
    purpose: "The program's identity, and the seasons that give it schedulable time.",
    perm: "manage_setup",
    primary: { label: "Add Season", go: "league_season" },
    secondary: [{ label: "Programs", act: "league" }, { label: "Leagues", act: "level" }],
    // A Season hangs off a Program, so with no Program at all "Add Season"
    // opens a drawer whose only required parent select has nothing in it.
    prereq: [{ met: (f) => f.programs > 0,
      action: { label: "Add program", act: "league" },
      why: "A season belongs to a program, and there is no program yet." }],
    summary: (sv) => [
      { label: "Programs", n: (sv.programs || []).length },
      { label: "Seasons", n: (sv.seasons || []).length },
      { label: "Leagues", n: (sv.leagues || []).length }] },
  { key: "teams", title: "Permanent teams", icon: "👥",
    purpose: "Teams as lasting members of a league, independent of any one season.",
    perm: "manage_setup",
    primary: { label: "Add Team", go: "teams" },
    secondary: [{ label: "Clubs", act: "club" }],
    // A permanent Team hangs off a League (its `league_id`); a Club does NOT
    // (team-club-optional.js / test_optional_club.py), so the Club is not a
    // prerequisite for anything and never appears in this chain.
    prereq: [{ met: (f) => f.leagues > 0,
      action: { label: "Add a league first", open: "league_season" },
      why: "A permanent team belongs to a league, and this program's active"
        + " season has none yet." }],
    summary: (sv) => [
      { label: "Teams", n: leagueScopedTeams(sv).length },
      { label: "Clubs", n: withPendingLink(sv, "clubs").length }] },
  { key: "participation", title: "Season participation and divisions", icon: "🏅",
    purpose: "Which teams play in which league-season, and the divisions that split them.",
    perm: "manage_setup",
    primary: { label: "Register Team", go: "participation" },
    secondary: [{ label: "Divisions", act: "division" }],
    // Owner ruling, verbatim: "Participation: Add Division before Register
    // Team." A Division hangs off a League paired with the active Season, and
    // a registration needs a permanent Team to register -- so the full chain
    // is league -> division -> team -> register, and the EMPTY state (zero
    // divisions) always stops at one of the first two.
    //
    // ...interleaved with the SERVER's own hard floors (#365 review round 3).
    // `register_team_for_season` takes `_require_active_season` and enforces
    // rule 7, so `get_setup_progress` publishes THREE ordered assertions for
    // this workflow -- season_selected, season_active, team_league_eligible --
    // and none of them can be recovered from a row count. Their positions
    // here are not cosmetic:
    //
    //   * the Season floors sit BEFORE `divisions`, not merely before the
    //     declared "Register Team". That step's own action is "Add division",
    //     and `create_division` takes the SAME `_require_active_season`
    //     guard -- so under an archived Season the round-2 chain answered a
    //     dead "Register Team" with an equally dead "Add division".
    //   * `leagues` stays first because its action is pure NAVIGATION to the
    //     league_season landing (which derives its own effective action), and
    //     navigation is never refused by a Season guard. Demoting a safe,
    //     genuinely useful action behind a floor it does not violate would
    //     withdraw help for no reason.
    //   * team_league_eligible sits last, after `teams`: "there is no team at
    //     all" and "no team's league matches this season" are different
    //     states with different resolutions, and the count answers the first.
    //
    // Relative order among the three ASSERTED steps is exactly the server's
    // own (season_selected -> season_active -> team_league_eligible), which
    // is the order `_workflow_prerequisite_gap` tests them in.
    prereq: [
      { met: (f) => f.leagues > 0,
        action: { label: "Add a league first", open: "league_season" },
        why: "Divisions live inside a league, and this program's active season"
          + " has none yet." },
      { assert: "season_selected" },
      { assert: "season_active" },
      { met: (f) => f.divisions > 0,
        action: { label: "Add division", act: "division" },
        why: "Teams are registered into a division, and there is none yet." },
      { met: (f) => f.teams > 0,
        action: { label: "Add a team first", open: "teams" },
        why: "There is no permanent team to register yet." },
      { assert: "team_league_eligible" }],
    summary: (sv) => [
      { label: "Divisions", n: (sv.divisions || []).length }] },
  { key: "roster", title: "Clubs, players and staff", icon: "🧑",
    purpose: "The people: players on teams, and the officials who work the games.",
    perm: "manage_setup",
    primary: { label: "Add Player", go: "roster" },
    secondary: [{ label: "Officials", act: "official" }],
    // The case the owner called out by name: "especially Roster when no Team
    // exists." A Player hangs off a Team, so with no Team the declared
    // primary opens a drawer whose required Team select is empty, and
    // goToSetupWorkflow("roster") seeds it from a team list that is itself
    // empty. The permanent Team belongs to the "teams" workflow, so the one
    // action goes there.
    prereq: [{ met: (f) => f.teams > 0,
      action: { label: "Add a team first", open: "teams" },
      why: "Players are added to a team, and this program, season and league"
        + " has none yet." }],
    // Takes the player list as an ARGUMENT rather than reading the
    // module-level `playersList` (#365): a per-card retry re-fetches
    // /api/players for THIS card alone and must be able to compute its own
    // summary from that response without writing the shared list other Setup
    // surfaces (Records, the hierarchy tree) render from.
    summary: (sv, ov, players) => {
      const lid = contextOptions && contextOptions.selected
        && contextOptions.selected.league_id;
      if (lid) {
        const teamIds = new Set(leagueScopedTeams(sv).map((t) => t.id));
        players = players.filter((p) => teamIds.has(p.team_id));
      }
      return [
        { label: "Players", n: players.length },
        { label: "Officials", n: withPendingLink(sv, "officials").length }];
    } },
  { key: "facilities", title: "Venues, rinks and ice", icon: "🏟️",
    purpose: "Where games can be played, and the recurring ice that makes them schedulable.",
    perm: "manage_arena",
    // "Add Ice" opens the recurring Ice Availability Builder rather than a
    // single-slot form: it is the highest-leverage action here, because it
    // bulk-generates the inventory everything downstream schedules against.
    primary: { label: "Add Ice", go: "facilities" },
    secondary: [{ label: "Venues", act: "venue" }, { label: "Rinks", act: "rink" }],
    tertiary: [{ label: "Add one ice slot", act: "ice-slot" }],
    // Owner ruling, verbatim: "Facilities: Add Venue -> Add Rink -> Add Ice."
    // Ice is generated onto a Rink, and a Rink hangs off a Venue, so the
    // highest-leverage action is also the LAST one that becomes possible.
    //
    // ...and a Rink is not enough, which is the #365 review's Facilities
    // fail-open. The first two steps ask whether a Venue/Rink is VISIBLE, and
    // the scoped overview's lists deliberately include revoked-grant history
    // and creator-owned pending rows -- both correct reads, neither of which
    // makes a Rink schedulable. So the chain continues with steps that are
    // not counts at all: the SERVER's own ordered hard floors for this
    // workflow, asserted on get_setup_progress's `prerequisites` and consumed
    // fail-closed here (setupResolvedPrereqChain).
    //
    // Round 2 declared only the LAST of those floors, venue_access. Round 3's
    // review reproduced the same fail-open one layer up: an ARCHIVED selected
    // Season with a LIVE grant asserts `venue_access: met` quite correctly --
    // a rink really is reachable -- while `commit_ice_availability` refuses
    // the whole workflow with `season_archived`. One capability fact was
    // standing in for the whole capability. So the Season floors are declared
    // too, and BEFORE venue_access, which is the order the real writes fail
    // in: season_guard.require_active_season runs before anything looks at a
    // rink.
    //
    // They sit AFTER venues/rinks rather than at the head of the chain, and
    // that placement is asserted rather than incidental: `create_venue` and
    // `create_rink` take NO Season guard (a Venue is organization-owned; a
    // Rink hangs off a Venue), so "Add venue"/"Add rink" are never refused by
    // an archived or unselected Season. A chain that demoted them would
    // withdraw a working action and offer nothing in its place.
    //
    // Both resolutions need MANAGE_SETUP -- granting venue access and
    // reopening a Season are League Admin actions -- so each step's action
    // declares it and setupEffectiveAction withdraws the control entirely for
    // an Arena Manager, who would otherwise be handed a second dead end. The
    // `why` sentences are the BACKEND's own, so they name the exact Season
    // the claim is scoped to instead of a client-side paraphrase.
    prereq: [
      { met: (f) => f.venues > 0,
        action: { label: "Add venue", act: "venue" },
        why: "Ice is booked on a rink inside a venue, and there is no venue yet." },
      { met: (f) => f.rinks > 0,
        action: { label: "Add rink", act: "rink" },
        why: "Ice is booked on a rink, and this venue has none yet." },
      { assert: "season_selected" },
      { assert: "season_active" },
      { assert: "venue_access" }],
    summary: (sv) => [
      { label: "Venues", n: withPendingLink(sv, "venues").length },
      { label: "Rinks", n: withPendingLink(sv, "rinks").length }] },
  // Workflow 6 is OPTIONAL (Decision 9): always visible and always reachable,
  // never the hub's `next` recommendation, and never blocking the complete
  // state. It carries its own status wording rather than done/todo.
  //
  // #365: `optional: true` here is the DECLARED contract, used only as the
  // fallback for a caller that has no progress payload to read (no Program
  // resolved yet, or a role the progress route refuses). The LIVE model takes
  // optionality from the backend's own `status: "optional"` instead
  // (partitionSetupWorkflows), so client and server can never disagree about
  // which workflow is the optional one. Nothing anywhere derives it from a
  // title or a list position.
  //
  // No `summary`: this workflow has no Program-scoped inventory of its own to
  // count (the same absence of a completion signal that makes it optional
  // server-side), so its card has no data dependency and therefore no
  // loading/empty/error of its own to reach.
  //
  // Consequently no `prereq` either, and that is an ASSERTION rather than an
  // omission: with no EMPTY state to reach there is no emptiness for an
  // effective action to resolve, and "Import data" needs no record to exist
  // first -- importing is precisely how an operator with nothing gets
  // started. buildSetupWorkflowCardModel derives an effective action for
  // every workflow regardless, so an empty chain here resolves to the
  // declared primary rather than to nothing.
  { key: "import", title: "Imports and onboarding", icon: "📥", optional: true,
    purpose: "Bring an existing league in from spreadsheets instead of typing it in.",
    perm: "manage_arena",
    primary: { label: "Import data", go: "import" },
    // Re-entering the guided Initial Setup wizard replaces the whole working
    // surface with a linear, start-from-the-top flow for the active Program.
    // It is the only action on any of the six landings that is neither a
    // create drawer nor a jump to an inventory screen, so it is the one that
    // carries a card-scoped confirmation (#365's `confirmation` state). The
    // MODEL supports `confirm` on any action; only this one declares it.
    secondary: [{ label: "Initial Setup wizard", go: "onboarding",
      confirm: {
        prompt: "Restart the guided Initial Setup wizard? Your existing data "
          + "isn't changed — you'll be taken through setup from the top.",
        yes: "Start the wizard", no: "Stay here",
        done: "Opening the Initial Setup wizard.",
        cancelled: "Stayed on Imports and onboarding." } }] },
];

// Which workflow landing is open, or null for the hub index.
let setupWorkflow = null;

function setupWorkflowsFor() {
  return SETUP_WORKFLOWS.filter((w) => !w.perm || hasPerm(w.perm));
}

// The ONE permission-aware transition into a Setup workflow LANDING (#345),
// shared by the Setup hub's own cards and by the Facilities nav destination.
//
// It deliberately does NOT route through switchTab(): that helper clears
// `setupWorkflow` unconditionally (so the top-level Setup destination always
// returns to the workflow index), which would wipe the very half of this
// composite destination the caller is trying to establish. Both halves —
// `view` and `setupWorkflow` — are set together here, before the single
// render, so the landing can never be reached in a half-applied state.
//
// Fails CLOSED on a key this caller may not open: `setupWorkflowsFor()`
// already filters by each workflow's own `perm` (Facilities is `manage_arena`,
// which is exactly the permission the Arena Manager whose primary journey this
// is holds), so an unpermitted or unknown key is refused rather than rendering
// a landing whose actions would all be denied. Returns whether it navigated,
// so a caller can tell "not permitted" from "done".
function openSetupWorkflowLanding(key) {
  const workflow = key
    ? setupWorkflowsFor().find((w) => w.key === key) : null;
  if (key && !workflow) return false;
  view = "setup";
  // The landing (and the hub index it belongs to) renders ONLY inside the
  // "hub" sub-view -- renderSetup branches on `setupView` before it ever looks
  // at `setupWorkflow`. Establishing only `view` + `setupWorkflow` therefore
  // left this composite destination half-applied whenever the operator was on
  // Hierarchy or Records: the Facilities nav entry, a hub card's "Open …" and
  // the landing's own "← All setup workflows" all silently rendered the tree
  // instead. Latent before (only a manual sub-view toggle could set it up),
  // but #365's venue-access resolution path deep-links THROUGH the Hierarchy
  // tree on purpose, so returning to the card was the very next step of the
  // flow this fix creates. Set here, with the other halves, so no transition
  // can establish one without the others.
  setupView = "hub";
  setupWorkflow = key || null;
  toast = "";
  // The SAME per-destination reset switchTab() applies. Bypassing switchTab is
  // deliberate (it would clear the `setupWorkflow` half being established), but
  // that must not mean bypassing the transient-state discipline too: without
  // this, arriving from the Calendar carried a live Ice Builder over, and from
  // Player/Guardian Home a pending checkout confirmation.
  resetTransientViewState("setup");
  syncActiveNav();
  setPageTitle("setup");
  render();
  // Land keyboard focus on the destination's own heading rather than leaving
  // it on a control the render just replaced.
  focusContentHeading();
  return true;
}

// The two ways a Setup landing action leaves the landing, extracted (#365) so
// a plain click and a confirmed one execute the SAME code -- a confirmation
// that took a different route to the destination would be a second
// implementation of the action, free to drift from the first.
function runSetupWorkflowGo(key) {
  // "onboarding" is the guided Initial Setup wizard, a top-level view rather
  // than one of goToSetupWorkflow's six workflow destinations.
  if (key === "onboarding") { switchTab("onboarding"); focusContentHeading(); return; }
  // The REAL venue-access resolution path (#365 review, Facilities
  // fail-open). SeasonVenueAccess is not one of the six workflows and has no
  // create drawer: it is granted from the selected Season's own "Allowed
  // venues" section on the Setup hierarchy tree, which is exactly where the
  // #369 contract put the picker (and which renderSeasonParticipation gates
  // on MANAGE_SETUP -- the same permission the step's action declares, so
  // this destination is only ever offered to a role that finds a live control
  // when it arrives). Same deep-link idiom as "participation" above: set the
  // sub-view, switch, then focus the real control rather than dropping the
  // operator at the top of a long tree to hunt for it.
  if (key === "venue_access") {
    setupView = "hierarchy";
    switchTab("setup");
    focusVenueAccessControl();
    return;
  }
  // Returned, not fire-and-forget (#365): goToSetupWorkflow() is async for the
  // drawer destinations, and announceCardStatusAfter() needs the navigation's
  // own settle to know when the destination has finished rendering.
  return goToSetupWorkflow(key);
}
async function openSetupWorkflowDrawer(kind) {
  const mySeq = ++drawerSeedFetchSeq;
  const seeded = await contextSeededDrawerValues(kind);
  if (mySeq !== drawerSeedFetchSeq) return;  // a newer open already won
  if (!seeded.ok) {
    toast = seeded.needsContext
      ? "Pick a program in the context bar first, so this is created in the right one."
      : seeded.needsSeason
        ? "Pick a season in the context bar first — this is created inside one."
        : seeded.noLeagueInSeason
          ? "That season has no leagues yet — add one before adding divisions."
          : seeded.leagueNotInSeason
            ? "Your active League isn't in this season — switch the context bar's League or clear it before adding divisions."
            : "Couldn't load what's needed to open that — try again.";
    toastIsError = true;
    return render();
  }
  drawer = { kind }; drawerError = ""; drawerValues = seeded.values;
  toast = "";
  render();
}

/* ---------- Setup workflow card state (#365) ---------- */
// Each of the six workflows is its own card in the per-card store, bound to
// its own identity (card id + context tuple + generation). What matters here
// that a single page-wide flag could not express: the six do NOT all share
// one data source. "Clubs, players and staff" is the only one that needs
// /api/players; the four with inventory counts need /api/v2/setup/overview;
// Workflow 6 needs neither. So a /api/players outage is genuinely one card's
// failure, and its Retry re-fetches only that endpoint and repaints only that
// card -- the other five keep their own settled numbers on screen.
//
// `src` is what render() already fetched for the Setup view, plus an explicit
// per-ENDPOINT outcome for each. The outcome flags are the point: `sv`
// degrades to an empty shape on failure, so "the overview call failed" and
// "this Program genuinely has nothing yet" are indistinguishable from the
// payload alone -- exactly the "state inferred from missing payload fields"
// #365 forbids.
//
// The STATUS read is the same kind of claim and now arrives the same way:
// `src.progress` is a setupProgressRead() record, never a payload-or-null.
// The row is looked up HERE, from that record, rather than being resolved by
// each caller and passed in — a `statusRow` argument could arrive `null`
// because the read failed, because this role may not read it, or because the
// backend genuinely had no row, and this function could not tell which.
// The facts a `prereq` chain is evaluated against, derived ONCE from the SAME
// `src` the card's own stats come from — never re-read from a module-level
// cache at render time. That matters for the identity discipline as much as
// for correctness: an effective action computed here is committed into the
// card's model under the card's own identity, so a landing can never offer an
// action derived from one context's inventory while displaying another's.
//
// Scoped exactly as the summaries are (leagueScopedTeams / withPendingLink),
// so "this workflow says it has no teams" and "the chain says a team is
// missing" are the same claim about the same rows.
//
// COUNTS ARE NOT CAPABILITIES (#365 review, Facilities fail-open). Every
// field above is a count of rows the scoped overview VISIBLY reports, and
// that read contract deliberately includes revoked-grant history and
// creator-owned pending rows (get_setup_overview_v2). So `venues`/`rinks`
// answer "is there a Venue/Rink on this operator's screen", which is exactly
// the right question for "is this card EMPTY" and exactly the WRONG question
// for "can ice be generated here". A Rink at a Venue whose grant to the
// selected Season was revoked is visible, correctly, and is not schedulable:
// the Ice Builder refuses it with `venue_access_missing` and a preview
// provably generates zero slots. Asking only the visibility question is what
// let Facilities settle READY with `blockedBecause: null` and a dead-end "Add
// Ice" primary.
//
// ...and the same is true one layer up, which is round 3's finding: an
// ARCHIVED selected Season is equally invisible to every count on this card.
// Venues and Rinks are still there, the grant is still live, and NOTHING in
// the Season can be written. No arithmetic over visible rows answers that
// either.
//
// Claims like those cannot be recovered from row counts at all, so they are
// not inferred here: they arrive ASSERTED, from the same
// /api/v2/setup/progress read the card's status comes from, computed
// server-side from the exact state the real writes enforce.
// `assertedRows[workflowKey]` is the COMPLETE ORDERED set for that workflow
// — read from `src.progress` (a setupProgressRead record, never a
// payload-or-null), so a failed or unauthorized read yields NO assertions
// rather than a manufactured "met". See setupResolvedPrereqChain below for
// the fail-closed consumption.
function setupPrereqFacts(src) {
  const sv = (src && src.sv) || {};
  const read = (src && src.progress && src.progress.outcome)
    ? src.progress : setupProgressRead(CARD_READ.FAILED, null);
  // Kept as an ORDERED LIST, never flattened to a dictionary (#365 review
  // round 3): the backend publishes its floors in the order its own writes
  // fail them, and a client that dropped that order could not tell "the
  // Season is archived" from "no rink is reachable" when both are unmet --
  // nor notice a floor it never declared.
  const assertedRows = {};
  Object.keys(read.byKey).forEach((key) => {
    assertedRows[key] = ((read.byKey[key] || {}).prerequisites || [])
      .filter((p) => p && p.key);
  });
  return {
    programs: (sv.programs || []).length,
    seasons: (sv.seasons || []).length,
    leagues: (sv.leagues || []).length,
    teams: leagueScopedTeams(sv).length,
    divisions: (sv.divisions || []).length,
    venues: withPendingLink(sv, "venues").length,
    rinks: withPendingLink(sv, "rinks").length,
    players: ((src && src.players) || []).length,
    assertedRows: assertedRows,
  };
}

// The REAL reopen-Season path for an archived selected Season (#365 review
// round 3), offered ONLY to a role authorized to perform it.
//
// `perm: "manage_setup"` is not a guess: /api/v2/setup/seasons/<id>/reopen
// maps to MANAGE_SETUP in web/authz.py, so an Arena Manager pressing this
// would receive a 403. setupEffectiveAction withdraws the control for them
// entirely and the card shows guidance instead — the requirement is
// explicitly "expose the real reopen path only to an authorized role;
// otherwise show guidance and NO unusable mutation".
//
// It is a `reopen` action rather than a `go`/`act`/`open` one because there
// is no Setup workflow, landing or drawer that owns Season lifecycle: the
// reopen is a single reasoned write, and #159 requires a NON-EMPTY reason
// that is recorded in the audit trail. So it routes through the card's own
// CONFIRM state (#365) with a required reason field — the first derived
// action to carry a `confirm`, which is why setupCardActionFor now resolves
// "primary:0" through the committed model instead of the declared primary.
//
// Which Season: the SELECTED one, always. #367 established that reopening an
// archived Season requires that Season to be the active context, and this
// action is only ever reachable from a card whose committed identity carries
// exactly that Season in its tuple — resolveSetupCardConfirm re-checks the
// tuple before it fires, so a context switch withdraws the confirmation
// rather than reopening a Season the operator has left.
const SETUP_SEASON_REOPEN_ACTION = {
  label: "Reopen this season", reopen: true, perm: "manage_setup",
  confirm: {
    prompt: "Reopen this archived season so it can be changed again? It stays"
      + " selected, and its existing records are untouched.",
    reason: "Why is this season being reopened?",
    yes: "Reopen season", no: "Keep it archived",
    // `busy` is what the card says WHILE the write is in flight, and `done`
    // is said ONLY after the server has confirmed it (#365 review round 4).
    // Keeping both in the declaration is what makes it impossible for the
    // writer to reuse the completion sentence as a progress one.
    busy: "Reopening this season…",
    done: "Season reopened — it can be changed again.",
    // Said when the reopen SUCCEEDED but this card's own follow-up refresh
    // did not: the operator must not be told only about the refresh, because
    // the Season really did reopen and the audit trail really did record it.
    doneNoRefresh: "Season reopened, but this card's summary couldn't be"
      + " reloaded. Retry below.",
    cancelled: "The season stays archived." },
};

// The RESOLUTION each asserted prerequisite has, keyed "<workflow>/<key>".
// Deliberately a lookup rather than a field on the payload: the backend's
// prerequisite rows are ROLE-INVARIANT statements about the selected Season's
// data (both roles that can see a workflow receive byte-identical rows), so
// "who may fix this, and through which control" is a client-side permission
// question answered by the same `hasPerm` every other control is gated on.
// Putting it in the payload would create a second authority on permissions.
//
// A prerequisite with NO entry here resolves to guidance and no mutation
// control at all. That is the fail-closed default and it is what makes a
// NEWLY-published server floor safe: the chain still blocks on it, the card
// still explains it in the backend's own words, and no control is offered
// that this client has no verified path for.
const SETUP_ASSERTED_PREREQ_ACTIONS = {
  // Granting SeasonVenueAccess is a League Admin action and lives on the
  // selected Season's own "Allowed venues" section (#369), not in any create
  // drawer — runSetupWorkflowGo("venue_access") is the deep link.
  "facilities/venue_access": {
    label: "Allow a venue for this season", go: "venue_access",
    perm: "manage_setup" },
  // An archived selected Season is read-only until an authorized reopen
  // (#159). Reopening is MANAGE_SETUP — the same permission
  // /api/v2/setup/seasons/<id>/reopen requires (web/authz.py:
  // "Season lifecycle archive/reopen (#159) are setup actions") — so an Arena
  // Manager, who holds MANAGE_ARENA only, gets the guidance and NO control.
  // #367's established rule is that reopening an archived Season requires it
  // to be the SELECTED context, which it is by construction here: this step
  // is only ever reached from a card whose committed identity carries that
  // Season as its own tuple.
  "facilities/season_active": SETUP_SEASON_REOPEN_ACTION,
  "participation/season_active": SETUP_SEASON_REOPEN_ACTION,
  // Rule 7 (register_team_for_season): a Team with a permanent League can
  // only register into a LeagueSeason of that same League. Both repairs — a
  // new Team under a league this Season runs, or moving an existing Team's
  // permanent League — live on the Permanent teams workflow, so the one
  // action opens that landing rather than smuggling its create drawer here.
  "participation/team_league_eligible": {
    label: "Set up a team in this season's league", open: "teams" },
  // "season_selected" has NO action on purpose: choosing a Season is a
  // context-bar selection, not a mutation any card owns, and inventing a
  // control for it would be inventing a second context switcher. It gets
  // guidance with its own `advice` below instead.
};

// What to append to the blocker sentence when a step's action is withdrawn.
// The default ("Ask a league admin to set that up.") is the right sentence
// for a grant or a reopen an Arena Manager cannot perform, and the WRONG one
// for a Season nobody can select from a card.
const SETUP_ASSERTED_PREREQ_ADVICE = {
  season_selected: "Pick a season in the context bar to work in one.",
  season_active: "Ask a league admin to reopen it.",
};

// The client's own last-resort sentence for a floor the backend did not
// assert (a failed or unauthorized progress read, or a prerequisite key this
// build predates). Never "it's fine": an unverifiable claim blocks.
const SETUP_ASSERTED_PREREQ_FALLBACK = {
  season_selected: "No season is selected for this program yet.",
  season_active: "The selected season is archived and read-only, so nothing"
    + " in it can be changed.",
  venue_access: "No rink is reachable through active venue access for the"
    + " selected season yet, so ice can't be added to it.",
  team_league_eligible: "No permanent team is eligible to register in the"
    + " selected season yet.",
};

// ONE asserted prerequisite, as a chain step. `met` is true ONLY for an
// explicit `met === true` on a row the backend really published under this
// exact key — every other shape (absent row, absent payload, a truthy-looking
// value that is not `true`) is unmet. That is the fail-closed rule, stated
// once here so no call site can restate it more leniently.
function setupAssertedStep(workflowKey, prereqKey, row) {
  const asserted = row && row.key === prereqKey ? row : null;
  const met = !!asserted && asserted.met === true;
  const action = SETUP_ASSERTED_PREREQ_ACTIONS[workflowKey + "/" + prereqKey] || null;
  return {
    assert: prereqKey,
    met: () => met,
    action: action,
    // The BACKEND's own sentence when there is one, so the copy names the
    // exact Season the claim is scoped to; the declared fallback only when
    // the claim itself could not be read.
    why: (asserted && asserted.detail)
      || SETUP_ASSERTED_PREREQ_FALLBACK[prereqKey]
      || "This step can't be taken yet, and the reason couldn't be read.",
    advice: SETUP_ASSERTED_PREREQ_ADVICE[prereqKey] || null,
  };
}

// THE chain, with every `{ assert }` placeholder resolved against the rows
// get_setup_progress actually published for this workflow (#365 review round
// 3). Two guarantees, and the second is the one this round exists for:
//
//  1. A DECLARED assertion the backend did not publish still appears, as a
//     fail-closed unmet step. A failed or unauthorized progress read
//     therefore blocks the workflow instead of silently shortening its chain
//     to the counts — which is what "an unasserted claim is never met" has to
//     mean once the chain is assembled dynamically.
//  2. A PUBLISHED floor this client never declared is APPENDED rather than
//     dropped. The whole defect of round 2 was a client holding a strict
//     SUBSET of the server's floors; a chain built only from what the client
//     happens to know would reproduce it the next time the server learns a
//     new one. An undeclared floor has no registered action, so it resolves
//     to the backend's own sentence and no mutation control — the safe
//     answer for a claim this build cannot route.
//
// Declared steps keep their declared positions (see each workflow's `prereq`
// for why those positions are what they are), and the appended ones follow in
// the server's own published order.
function setupResolvedPrereqChain(w, facts) {
  const published = (facts && facts.assertedRows && facts.assertedRows[w.key]) || [];
  const byKey = {};
  published.forEach((p) => { if (p && p.key) byKey[p.key] = p; });
  const declared = {};
  const steps = (w.prereq || []).map((step) => {
    if (!step.assert) return step;
    declared[step.assert] = true;
    return setupAssertedStep(w.key, step.assert, byKey[step.assert]);
  });
  published.forEach((p) => {
    if (!declared[p.key]) steps.push(setupAssertedStep(w.key, p.key, p));
  });
  return steps;
}

// THE effective action: the single control a landing offers while a
// prerequisite is missing. Walks the declared chain against live facts and
// returns the first UNMET step's action plus the sentence naming why; all met
// (or no chain at all) resolves to the declared primary, which is then
// genuinely the action that resolves the state.
//
// Derived for EVERY SETTLED card, not only for EMPTY ones (#365 owner ruling,
// round 2). EMPTY was never the whole defect: "Venues, rinks and ice" with one
// venue and no rinks is READY, not EMPTY -- it has records to count -- and it
// went on offering "Add Ice", which needs a rink that does not exist. A "Rinks"
// link sitting next to it does not make that primary action live; it just means
// the operator has to work out for themselves which of the three controls is
// the one that can succeed. The same shape reaches Participation (divisions
// registered but no team to register), "Clubs, players and staff" (officials
// but, under the active League, no team to add a player to) and "Permanent
// teams" (a club, but no league to hang a team off). The chain answers all of
// them with the same question -- which record does the declared primary need
// that is not there -- so it is asked for every card whose data has settled.
//
// Scope, deliberately: the SETUP cards only. The Home/Tasks card's "Continue
// setup" CTA is not derived here and does not need to be -- it points at the
// roll-up's `next`, which walks the ORDERED REQUIRED PREFIX and therefore
// cannot recommend a workflow whose predecessor is unfinished (setupHubRollup).
// It also has no `sv` of its own to derive from: its model comes from the
// backend progress payload, and reading the setup overview on Home to answer a
// question the ordering already answers would be a new data dependency, not a
// fix.
//
// Fails CLOSED on an `open` step whose target workflow this role may not
// open: openSetupWorkflowLanding() refuses an unauthorized key, so offering
// the button would be offering a dead control. Returning no action at all
// leaves the EMPTY body's `why` sentence as the only thing on screen, which
// is the true statement — this role cannot resolve this emptiness itself.
// (Unreachable today: every cross-workflow step targets a workflow carrying
// the same `perm` as its source, so a role that can see one can see both.)
//
// ...and fails closed the SAME way on a step whose resolution needs a
// permission this role does not hold (#365 review, Facilities fail-open).
// That case is REACHABLE and is the whole point: an Arena Manager can see and
// run Facilities (MANAGE_ARENA) but cannot grant SeasonVenueAccess
// (MANAGE_SETUP), so substituting "Allow a venue for this season" for a dead
// "Add Ice" would only trade one dead end for another. They get no mutation
// control at all plus the sentence — which the card body completes with "Ask
// a league admin to set that up" — while a League Admin gets the real
// resolution path. Declared as `perm` ON THE STEP'S ACTION rather than tested
// at a call site, so a future step cannot forget to ask.
//
// `why` may be a function of the facts, so a step whose explanation is an
// ASSERTED backend sentence (naming the exact Season the claim is about)
// renders that sentence rather than a client-side paraphrase of it.
function setupEffectiveAction(w, facts) {
  // The RESOLVED chain, not the declared one: `{ assert }` placeholders are
  // replaced by the backend's published rows and any floor the backend
  // publishes that this client never declared is appended, both fail-closed
  // (setupResolvedPrereqChain).
  const step = setupResolvedPrereqChain(w, facts).find((s) => !s.met(facts));
  if (!step) return { action: w.primary, why: null, advice: null };
  const why = typeof step.why === "function" ? step.why(facts) : step.why;
  // Guidance to append when the control is withdrawn below. Carried on the
  // withdrawal paths only -- an offered action needs no apology.
  const advice = step.advice || null;
  // A step with no action at all: the blocker is real and this client has no
  // control that resolves it (a Season selection, or a floor published by a
  // newer backend than this build declares). Guidance is the whole truth.
  if (!step.action) return { action: null, why: why, advice: advice };
  if (step.action.open && !setupWorkflowsFor().some((x) => x.key === step.action.open)) {
    return { action: null, why: why, advice: advice };
  }
  if (step.action.perm && !hasPerm(step.action.perm)) {
    return { action: null, why: why, advice: advice };
  }
  return { action: step.action, why: why, advice: null };
}

function buildSetupWorkflowCardModel(w, src) {
  // Fail CLOSED on a caller that supplies no outcome at all: an unasserted
  // read is treated as a failed one, never as a silent success.
  const read = (src && src.progress && src.progress.outcome)
    ? src.progress : setupProgressRead(CARD_READ.FAILED, null);
  const statusRow = read.byKey[w.key] || null;
  // Optionality: the backend's own `status` when there is a progress payload
  // for this card, the declared contract otherwise. Never a title or index.
  const optional = statusRow ? !!statusRow.optional : !!w.optional;
  const status = statusRow ? statusRow.status : CARD_STATUS.UNKNOWN;
  const base = { status: status, optional: optional, statusRead: read.outcome,
                 attention: (statusRow && statusRow.attention) || null };
  // #365 review round 2 finding 3: a REQUIRED workflow whose done/todo status
  // could not be read is an explicit card ERROR — not a card that quietly
  // reads READY/EMPTY with an UNKNOWN status while the roll-up keeps last
  // read's completion and a retry announces "updated". The failure matters
  // exactly where the status is load-bearing: an OPTIONAL workflow's status
  // never reaches the completion arithmetic or the recommendation, so a
  // failed read cannot corrupt anything through it. UNAUTHORIZED is NOT a
  // failure — a role that may not read the route has no status, which is a
  // true statement about it, and its cards are not in the roll-up's `known`.
  if (!optional && read.outcome === CARD_READ.FAILED) {
    return Object.assign(base, { state: CARD_STATE.ERROR, stats: null,
      failed: "this workflow's setup status" });
  }
  // THE derivation, for every settled state below (#365 owner ruling round 2).
  // Computed from the SAME `src` the stats are, at the same moment, and
  // committed into the model under the card's own identity -- so the action a
  // landing offers and the counts beside it can never be answers about
  // different context tuples, and nothing is re-derived at paint time.
  //
  // Deliberately NOT reached from the ERROR branches above: an ERROR card has
  // no asserted payload to derive from (`sv` degrades to an empty shape on a
  // failed read, which would fake every count to zero and manufacture a
  // prerequisite that may well exist). ERROR keeps its declared actions and
  // says only the summary is missing, exactly as before.
  const derived = () => {
    const eff = setupEffectiveAction(w, setupPrereqFacts(src));
    // `blockedAdvice` travels with the other two so the sentence a WITHDRAWN
    // card shows is committed under the same identity as the withdrawal
    // itself -- a renderer that chose it at paint time could tell an operator
    // to ask a league admin about a blocker whose real fix is the context bar.
    return { effective: eff.action, blockedBecause: eff.why,
             blockedAdvice: eff.advice || null };
  };
  // A workflow with no inventory of its own (Workflow 6) has no data
  // dependency, so it is never loading, empty or errored -- it is simply
  // there, always reachable, which is what "optional" means here. It still
  // carries a derivation: an empty chain resolves to the declared primary, so
  // "every settled card has an effective action" holds without an exception.
  if (!w.summary) {
    return Object.assign(base, { state: CARD_STATE.READY, stats: null }, derived());
  }
  if (!src.svOk) {
    return Object.assign(base, { state: CARD_STATE.ERROR, stats: null,
      failed: "the setup overview" });
  }
  if (w.key === "roster" && !src.playersOk) {
    return Object.assign(base, { state: CARD_STATE.ERROR, stats: null,
      failed: "the player list" });
  }
  const stats = w.summary(src.sv, src.ov, src.players || []);
  // EMPTY is asserted, not inferred: every count this workflow tracks is
  // zero, so there is nothing here yet and the card says what is missing.
  //
  // ...and, since #365's owner ruling on dead ends, what to DO about it: the
  // effective action and the sentence naming the blocker are resolved HERE,
  // from the same payload these counts came from, and committed with the
  // model. The renderer reads them; it does not re-derive them, so the action
  // an EMPTY landing offers is always bound to the same identity as the
  // counts beside it.
  if (stats.every((s) => !s.n)) {
    return Object.assign(base, { state: CARD_STATE.EMPTY, stats: stats,
      reason: "no_records" }, derived());
  }
  // SUCCESS/complete for a landing is the workflow's own backend status
  // reading "done" -- the same signal the Home/Tasks card badges, so the two
  // surfaces can never disagree about whether a workflow is finished.
  //
  // Derived here too rather than assumed: "done" is the BACKEND's judgement
  // about this workflow, and the chain's question is a different one (can the
  // declared primary create anything RIGHT NOW, under this tuple). A League
  // selection that narrows teams to zero makes "Add Player" dead on a roster
  // the backend still calls done, and a done card that offers a dead action is
  // the same dead end as an empty one.
  if (status === CARD_STATUS.DONE) {
    return Object.assign(base, { state: CARD_STATE.SUCCESS, stats: stats }, derived());
  }
  // PARTIAL -- the state the ruling's second round is about. Some of this
  // workflow's counts are non-zero, so it is not EMPTY and the card has real
  // records to show; that says nothing about whether the DECLARED primary can
  // still create the next one.
  return Object.assign(base, { state: CARD_STATE.READY, stats: stats }, derived());
}

// Bind every visible workflow card to a fresh identity and commit its model.
// Called from render()'s Setup branch, inside the same contextRevision-guarded
// stretch as the fetches it consumes.
//
// THE render-driven commit, and the one the serialization rule (see cardWrites)
// exists for. A card with a current unresolved write is SKIPPED here: this
// render fetched archived data while that write was in flight, and committing
// it would supersede the write's identity, restore its withdrawn controls and
// invite a second lifecycle mutation whose first outcome cannot be known.
// beginCardRequest() returns null for exactly that card and leaves its
// generation untouched; every OTHER workflow (and every workflow under a
// different tuple) commits normally on the same pass, so one card's live
// operation never freezes its neighbours.
function commitSetupWorkflowCards(src) {
  setupWorkflowsFor().forEach((w) => {
    const identity = beginCardRequest(setupWorkflowCardId(w.key));
    if (!identity) return;   // #365: refused — a write for this card is unresolved
    commitCardState(identity, buildSetupWorkflowCardModel(w, src));
  });
}

// (4) completion and (5) next-task for the Setup hub — the client's OWN
// arithmetic, over the cards this role can actually see, and explicitly
// labelled as such so it never reads as a claim about workflows it cannot see
// (the same boundary get_setup_progress holds by returning `complete: null`
// to a partial view).
//
// Every card is read through readCardState(), so a card whose tuple has moved
// contributes STALE rather than its old status: the roll-up can never be
// computed from data bound to a context tuple that is no longer active.
// `required` is a partition, not a filtered-at-each-use list, so an optional
// workflow cannot reach the completion arithmetic or the recommendation.
function setupHubRollup() {
  const rows = setupWorkflowsFor().map((w) => {
    const entry = readCardState(setupWorkflowCardId(w.key));
    // CONFIRM and PENDING count as settled for the SAME reason: neither has
    // changed anything yet. A confirmation is a question, and a pending write
    // is a request the server has not answered — the card is still holding
    // exactly the status it settled with, so the completion count and the
    // next-task recommendation must read exactly as they did before the
    // operator pressed anything. Treating PENDING as unknown would move the
    // roll-up on the strength of a write that has not happened, which is the
    // same premature-completion claim #365 review round 4 forbids at every
    // other mutation point.
    const settled = entry.state === CARD_STATE.READY || entry.state === CARD_STATE.EMPTY
      || entry.state === CARD_STATE.SUCCESS || entry.state === CARD_STATE.CONFIRM
      || entry.state === CARD_STATE.PENDING;
    return { key: w.key, label: w.title,
             optional: entry.optional === undefined ? !!w.optional : !!entry.optional,
             status: settled ? (entry.status || CARD_STATUS.UNKNOWN) : CARD_STATUS.UNKNOWN };
  });
  const required = rows.filter((r) => !r.optional);
  const known = required.filter((r) => r.status !== CARD_STATUS.UNKNOWN);
  const done = known.filter((r) => r.status === CARD_STATUS.DONE);
  // (5) next-task, over the ORDERED REQUIRED PREFIX (#365 review round 2
  // finding 2). Walking the whole list for the first TODO SKIPS a card whose
  // status is not known — because its own read failed, because it is still in
  // flight, or because it holds another context's data — and recommends a
  // LATER workflow instead, silently reordering setup for the operator. The
  // #204 order is a prerequisite chain, so an unknown card BLOCKS every
  // recommendation behind it: the walk stops at the first card that is not a
  // known DONE, and only a TODO there is a recommendation. `blockedBy` is
  // what stopped it, so the copy can say so instead of just going quiet.
  let next = null;
  let blockedBy = null;
  for (let i = 0; i < required.length; i++) {
    const r = required[i];
    if (r.status === CARD_STATUS.UNKNOWN) { blockedBy = r; break; }
    if (r.status !== CARD_STATUS.DONE) { next = r; break; }
  }
  return {
    required: required, optional: rows.filter((r) => r.optional),
    total: required.length, known: known.length, done: done.length,
    allDone: known.length === required.length && required.length > 0
      && done.length === required.length,
    // The recommendation is drawn from `required` alone. An optional workflow
    // is not in the list being searched, so it can never be returned.
    next: next, blockedBy: blockedBy,
  };
}

// The hub's progress/next line. Text only, deliberately: a CTA here would be
// a second primary action competing with each card's own (#204's
// one-primary-action-per-screen rule).
//
// Returns "" when there is nothing true to say. That empty string is NOT a
// no-op for the caller: see setupHubProgressSlotHtml() below — the roll-up is
// written into a container that always exists, so "nothing to say" REMOVES
// the previous sentence rather than leaving it standing.
function setupHubProgressHtml() {
  const roll = setupHubRollup();
  if (!roll.total || !roll.known) return "";
  const optionalNote = roll.optional.length
    ? ` ${esc(roll.optional.map((r) => r.label).join(", "))} is optional and never blocks completion.`
    : "";
  const nextNote = roll.next
    ? ` Next: <strong>${esc(roll.next.label)}</strong>.`
    // Deliberately not silent: a blocked prefix is why there is no "Next",
    // and saying so is what stops the absence from reading as "nothing left".
    : roll.blockedBy
    ? ` No next step until <strong>${esc(roll.blockedBy.label)}</strong> is up to date.`
    : roll.allDone ? " Every required workflow you manage is done." : "";
  return `<p class="muted swf-progress" data-setup-hub-progress>${roll.done} of
    ${roll.total} required setup workflow${roll.total === 1 ? "" : "s"} you manage
    ${roll.done === 1 ? "is" : "are"} done.${nextNote}${optionalNote}</p>`;
}

// The STABLE container the roll-up is always written through (#365 review
// round 2 finding 2). The defect it removes: repaint used to replace the
// roll-up element only when the NEW html was non-empty, so a card going
// unknown — an Arena Manager retrying or failing Facilities, the sole
// required card they manage — left the previous "1 of 1 … done" sentence on
// screen beside counts that no longer supported it. "Empty html" meant "keep
// what's there" purely because there was nothing to swap in.
//
// With a container that outlives its contents, the write is unconditional and
// the empty case is a REMOVAL, not a skip. There is no longer a code path
// that can preserve a superseded roll-up, so no call site has to remember not
// to.
function setupHubProgressSlotHtml() {
  return `<div class="swf-progress-slot" data-setup-hub-progress-slot
    >${setupHubProgressHtml()}</div>`;
}

// A card's status chip. Reads the model's `optional` partition flag, not a
// key, so the Decision 9 wording follows the backend's own status.
//
// The optional chip keeps class `swf-optional` and the done/todo chips
// deliberately do NOT: that class means Decision 9's optional workflow and
// nothing else, everywhere it is looked for.
function setupCardStatusChip(entry, w) {
  const optional = entry.optional === undefined ? !!w.optional : !!entry.optional;
  if (optional) return `<span class="swf-optional">Optional</span>`;
  if (entry.status === CARD_STATUS.DONE) return `<span class="swf-status swf-done">Done</span>`;
  if (entry.status === CARD_STATUS.TODO) return `<span class="swf-status swf-todo">To do</span>`;
  return "";
}

// The per-card body: whichever of the discriminated states this card is in.
// Shared by the hub card and the landing so the two surfaces can never drift
// into showing different states for the same card. `landing` only widens the
// copy (a landing has room to explain); it never changes which state is shown.
function setupCardBodyHtml(w, landing) {
  const entry = readCardState(setupWorkflowCardId(w.key));
  const retry = (label) => `<div class="swf-card-actions">
    <button class="act ghost" data-setup-card-retry="${esc(w.key)}"
      >${label}</button></div>`;
  const stats = (rows) => rows
    ? `<div class="swf-stats">${rows.map((s) =>
        `<span class="swf-stat"><strong>${s.n}</strong> ${esc(s.label)}</span>`).join("")}</div>`
    : "";
  // The blocker sentence for a card that is NOT empty (#365 owner ruling round
  // 2). A partial card shows real counts, so the EMPTY copy ("nothing here
  // yet") would be a false statement — but the missing prerequisite is exactly
  // as load-bearing, because it is why the action below is not this workflow's
  // declared primary. Same committed field the withdrawal above reads, so the
  // explanation and the control can never describe different problems, and no
  // sentence at all when nothing is blocked.
  // The withdrawn-action sentence comes from the MODEL when the blocking step
  // declared one (#365 review round 3), falling back to the league-admin
  // wording that is correct for a grant or a reopen this role cannot perform.
  // "No season is selected" needs a different sentence -- no league admin can
  // pick a season for someone -- and the step is what knows that.
  const withdrawnAdvice = () => ` ${entry.blockedAdvice
    || "Ask a league admin to set that up."}`;
  const blockedNote = () => !entry.blockedBecause ? "" : `<p class="swf-card-blocked">
    ${esc(entry.blockedBecause)}${entry.effective === null
      ? esc(withdrawnAdvice())
      : " Start with the action below — this workflow's usual next step can't"
        + " be taken until then."}</p>`;
  if (entry.state === CARD_STATE.LOADING) {
    return `<div class="skeleton"><span class="sr-only">Loading ${
      esc(w.title.toLowerCase())}…</span></div>`;
  }
  if (entry.state === CARD_STATE.ERROR) {
    // Scoped to THIS card: the neighbouring cards keep their own numbers, and
    // Retry re-fetches only what this card needs. A real <button> in normal
    // document order, so it is reachable by Tab like any other control.
    return `<p class="swf-card-error" role="alert">Couldn't load ${
      esc(entry.failed || "this workflow's summary")}.${landing
        ? " The action below still works — only these counts are missing." : ""}</p>
      ${retry("Retry")}`;
  }
  if (entry.state === CARD_STATE.STALE) {
    // Held data from a context the operator has left. Shown, but labelled,
    // with a refresh path -- never silently re-presented as current.
    return `${stats(entry.stats)}
      <p class="swf-card-stale">These counts are from the program, season or
        league you had selected earlier.</p>
      ${retry("Refresh")}`;
  }
  if (entry.state === CARD_STATE.PENDING) {
    // A write this card started is IN FLIGHT (#365 review round 4). Three
    // things this body has to get right, all of them the reported defect:
    //  * The counts STAY. Blanking them would claim the write has already
    //    changed something, which is exactly the premature-success the
    //    pending state exists to stop.
    //  * NO control at all. The confirmation's own buttons are gone (a second
    //    press would fire a second reopen), and no retry is offered while the
    //    first attempt is unresolved.
    //  * The sentence is FOCUSABLE and is where focus is put. The controls
    //    the operator was standing on have just been replaced; without a
    //    destination, focus falls to <body> and a keyboard or screen-reader
    //    operator is stranded mid-operation with no idea anything is
    //    happening. tabindex="-1" is the same non-focusable-destination
    //    convention focusContentHeading() uses — reachable programmatically,
    //    never inserted into the tab order.
    return `${stats(entry.stats)}
      <p class="swf-card-pending" data-setup-card-pending="${esc(w.key)}"
        tabindex="-1">${esc(entry.pendingNote || "Working…")}</p>`;
  }
  if (entry.state === CARD_STATE.CONFIRM) {
    const c = entry.confirm || {};
    // A confirmation may REQUIRE a reason (#365 review round 3): reopening an
    // archived Season is a reasoned lifecycle write (#159 -- the route rejects
    // a blank one and records what it is given in the audit trail), so the
    // card collects it here rather than sending a canned string on the
    // operator's behalf. A real <label>+<input> in document order, so it is
    // reachable and named for a screen reader like any other field.
    const reason = c.reason ? `
        <label class="swf-confirm-reason" for="swf-confirm-reason-${esc(w.key)}"
          >${esc(c.reason)}</label>
        <input class="swf-confirm-reason-input" type="text" required
          id="swf-confirm-reason-${esc(w.key)}"
          data-setup-card-confirm-reason="${esc(w.key)}"
          value="${esc(entry.confirmReason || "")}">
        ${entry.confirmError
          ? `<p class="swf-card-error" role="alert">${esc(entry.confirmError)}</p>`
          : ""}` : "";
    return `${stats(entry.stats)}
      <div class="swf-confirm" role="group" aria-label="${esc(c.yes || "Confirm")}">
        <p class="swf-confirm-prompt">${esc(c.prompt || "Are you sure?")}</p>
        ${reason}
        <div class="swf-card-actions">
          <button class="act ghost" data-setup-card-confirm-yes="${esc(w.key)}"
            >${esc(c.yes || "Confirm")}</button>
          <button class="act ghost" data-setup-card-confirm-no="${esc(w.key)}"
            >${esc(c.no || "Cancel")}</button>
        </div>
      </div>`;
  }
  if (entry.state === CARD_STATE.EMPTY) {
    // Explains what is missing, in this workflow's own terms, instead of
    // showing a row of zeros and leaving the operator to infer it.
    //
    // `blockedBecause` (the owner's EMPTY dead-end ruling) names the
    // PREREQUISITE that is missing when the workflow's own declared primary
    // cannot be the resolving action — the same chain step that chose the one
    // action below, so the copy and the control can never describe different
    // problems. Absent it, the declared primary IS the resolving action and
    // the generic sentence is the whole truth.
    const missing = (entry.stats || []).map((s) => s.label.toLowerCase()).join(", ");
    const because = entry.blockedBecause
      ? ` ${esc(entry.blockedBecause)}`
      : "";
    const lead = entry.effective === null && entry.blockedBecause
      ? esc(withdrawnAdvice())
      : " Start with the action below.";
    return `${stats(entry.stats)}
      <p class="swf-card-empty">Nothing here yet — no ${esc(missing || "records")} for
        this program, season and league.${because}${lead}</p>`;
  }
  if (entry.state === CARD_STATE.SUCCESS) {
    return `${stats(entry.stats)}
      <p class="swf-card-done">✓ This workflow is set up. You can still add
        more whenever you need to.</p>${blockedNote()}`;
  }
  return `${stats(entry.stats)}${blockedNote()}`;
}

// One card body per surface, wrapped in the slot a per-card retry/refresh
// replaces. aria-busy is per card (not per page), so a card reloading on its
// own is announced as busy without implying the rest of Setup is.
// Deliberately NOT role="status"/aria-live: six polite live regions would all
// speak on every context switch. Card-scoped confirmations and successes go
// through the one existing sitewide live region instead (announceCardStatus).
function setupCardSlotHtml(w, landing) {
  const entry = readCardState(setupWorkflowCardId(w.key));
  return `<div class="swf-card-body" data-setup-card-slot="${esc(w.key)}"
    aria-busy="${cardBusy(entry) ? "true" : "false"}"
    >${setupCardBodyHtml(w, landing)}</div>`;
}

// aria-busy is true for a card with an unresolved request of EITHER kind: a
// read in flight (LOADING) or a write in flight (PENDING). Assistive
// technology's question is "is this region still changing", and the answer is
// yes in both — a pending write that reported itself idle would invite the
// second press the state exists to prevent.
function cardBusy(entry) {
  return !!entry && (entry.state === CARD_STATE.LOADING
    || entry.state === CARD_STATE.PENDING);
}

// The last thing the serialization rule (see cardWrites) has to protect: WHERE
// FOCUS IS. Refusing the model commit keeps the card PENDING, but a render
// still repaints the surface — `c.innerHTML = renderSetup(...)`, or this
// file's own per-card slot replacement — and that destroys the focusable
// pending line the operator was standing on, dropping them on <body> mid-
// operation. The pending write OWNS its card's focus until its own response
// arrives, exactly as it owns the card's model, so the operation re-asserts it
// after any repaint that took it away.
//
// Deliberately NOT a focus grab: it acts only when focus has been left
// NOWHERE — <body> or the #content region's own fallback — which is precisely
// the case where a repaint destroyed it. A destination heading, a control the
// operator tabbed to, anything real, is left alone.
function restorePendingCardWriteFocus() {
  const active = document.activeElement;
  if (active && active !== document.body && active.id !== "content") return;
  setupWorkflowsFor().forEach((w) => {
    const held = currentCardWrite(setupWorkflowCardId(w.key));
    if (!held) return;
    const slot = document.querySelector(`[data-setup-card-slot="${w.key}"]`);
    const line = slot && slot.querySelector("[data-setup-card-pending]");
    // The LEDGER entry's identity. focusCardTarget refuses a superseded one on
    // its own account, which is the right answer after a round trip: the
    // operator navigated back here deliberately and their focus is on a real
    // destination, so nothing may yank it onto the pending line.
    //
    // It is also the right answer after an authenticated-user change (#365
    // round 7): the entry survives, because the write does, but its identity
    // belongs to the departing principal, so cardIdentityCurrent() refuses and
    // the arriving principal's focus is never moved by an operation they did
    // not start. Their card is still non-actionable — that comes from the
    // ledger, not from focus.
    if (line) focusCardTarget(held.identity, line);
  });
}

// (1) DOM mutation, scoped to ONE card: repaints this workflow's slot(s) --
// its hub card and/or the open landing -- its status chip, and the hub
// roll-up line that reads from it. Nothing else on the Setup screen is
// touched, which is what "a retry replaces only the failed card generation"
// means in practice: a failed "Clubs, players and staff" card recovering must
// not blank the five cards beside it.
function repaintSetupWorkflowCard(key, identity) {
  if (identity && !cardIdentityCurrent(identity)) return false;  // #365 identity gate — DOM
  const w = setupWorkflowsFor().find((x) => x.key === key);
  if (!w) return false;
  const slots = document.querySelectorAll(`[data-setup-card-slot="${key}"]`);
  if (!slots.length) return false;  // navigated away before this resolved
  const entry = readCardState(setupWorkflowCardId(key));
  slots.forEach((slot) => {
    slot.innerHTML = setupCardBodyHtml(w, !!slot.closest(".swf-landing"));
    slot.setAttribute("aria-busy", cardBusy(entry) ? "true" : "false");
  });
  const head = document.querySelector(`[data-setup-workflow-card="${key}"] .swf-head`);
  if (head) {
    const chip = head.querySelector(".swf-optional, .swf-status");
    if (chip) chip.remove();
    const next = setupCardStatusChip(entry, w);
    if (next) head.insertAdjacentHTML("beforeend", next);
  }
  // The hub roll-up is derived from every card's model, so this one card
  // changing genuinely changes it. Written through the stable container
  // (#365 review round 2 finding 2), UNCONDITIONALLY: when the new roll-up
  // has nothing true to say, the previous sentence is REMOVED rather than
  // left standing beside counts that no longer support it. The container
  // itself persists, so a later repaint still finds its place.
  const progressSlot = document.querySelector("[data-setup-hub-progress-slot]");
  if (progressSlot) progressSlot.innerHTML = setupHubProgressHtml();
  // The open landing's action groups are state-dependent too (#365 review
  // round 2 finding 1), so a card that settles into EMPTY, STALE or CONFIRM
  // must withdraw what that state forbids NOW -- not keep whatever the last
  // full render painted until the next one. Same stable container, same
  // unconditional write.
  const landingActions = document.querySelector(`[data-setup-landing-actions="${key}"]`);
  if (landingActions) {
    landingActions.innerHTML = setupLandingActionsHtml(w);
    wireSetupLandingActions(landingActions);
  }
  wireSetupWorkflowCards(document);
  // The slot replacement above destroyed whatever was focused inside it. If
  // this card is holding an unresolved write, its pending line is where the
  // operator belongs — not <body>.
  restorePendingCardWriteFocus();
  return true;
}

// A per-card retry/refresh. Fetches ONLY what this card needs (the four cards
// with inventory counts need the setup overview, "roster" additionally needs
// the player list, Workflow 6 needs neither) and commits under this card's own
// generation, so it can never disturb a neighbour's state or be overtaken by
// an older response of its own.
//
// Deliberately does NOT write the module-level `playersList` the Records and
// hierarchy views render from: this is one card's refresh, not the Setup
// screen's.
// `opts.done` / `opts.failed` let a CALLER THAT OWNS THE OUTCOME supply the
// sentence instead of this function's generic one. The reopen writer is the
// only such caller: without this, a successful reopen announced "Season
// reopened." and was then immediately overwritten by this function's
// "Venues, rinks and ice updated." — two announcements for one operation,
// the second of which describes the refresh rather than what the operator
// actually did. It is a SUBSTITUTION, never an addition: the announcement
// still happens exactly once, at exactly this point, under exactly this
// identity gate.
//
// `opts.silent` suppresses the announcement and the focus move — and ONLY
// those two (#365 review round 7). It exists for exactly one caller: the
// settlement of a write whose AUTHENTICATED PRINCIPAL has changed underneath
// it. The arriving principal did not press anything, so there is nothing to
// tell them and nowhere they asked to be sent; a sentence in the sitewide live
// region or a focus jump into this card would both be the departing
// operator's operation speaking through the arriving operator's session. The
// fresh reads, the model commit and the repaint still happen in full, because
// the card DOES have to stop showing the neutral pending presentation and
// start showing what the server now says — reconciled, but silently.
async function retrySetupWorkflowCard(key, opts) {
  const o = opts || {};
  const w = setupWorkflowsFor().find((x) => x.key === key);
  if (!w) return;
  const held = cardStates[setupWorkflowCardId(key)] || {};
  // Refused while an operation for this card against the CURRENT tuple is
  // still unresolved — the operator's Retry press above all. The reopen
  // writer reaches this function only AFTER settling its own registration, so
  // it needs no exemption to get past the gate; nothing else may.
  const identity = beginCardRequest(setupWorkflowCardId(key), { userInitiated: true });
  if (!identity) return;   // #365: refused — a write for this card is unresolved
  commitCardState(identity, { state: CARD_STATE.LOADING, status: CARD_STATUS.UNKNOWN,
                              optional: held.optional === undefined ? !!w.optional : held.optional });
  repaintSetupWorkflowCard(key, identity);
  const needsPlayers = key === "roster";
  // Whether this caller may read the status route at all — an ASSERTED
  // distinction (#365 review round 2 finding 3), not one recovered later from
  // a null payload: "this role has no status" and "the status read failed"
  // are different facts and must not arrive at the model looking alike.
  const mayReadProgress = hasPerm("manage_arena");
  const [svr, pl, pr] = await Promise.all([
    w.summary ? getJSON("/api/v2/setup/overview") : Promise.resolve(null),
    needsPlayers ? getJSON("/api/players") : Promise.resolve(null),
    // Status comes from the same route the Home/Tasks card reads, gated on
    // exactly the permission that route requires, so an unauthorized role
    // simply has no status rather than a 403 that would read as a failure.
    mayReadProgress ? getJSON("/api/v2/setup/progress") : Promise.resolve(null),
  ]);
  const svOk = !w.summary || !!(svr && !svr.error);
  const playersOk = !needsPlayers || Array.isArray(pl);
  // The failed read used to become `null` here, which buildSetupWorkflowCardModel
  // then read as "no row for this card" — so a retry whose status read failed
  // still produced READY/EMPTY and still announced "<workflow> updated". The
  // outcome now travels with the read, so the model can (and does) refuse.
  const progress = mayReadProgress
    ? setupProgressReadOf(pr) : setupProgressRead(CARD_READ.UNAUTHORIZED, null);
  const model = buildSetupWorkflowCardModel(w, {
    sv: svOk ? svr : null, ov: null, players: Array.isArray(pl) ? pl : [],
    svOk: svOk, playersOk: playersOk, progress: progress });
  // (4) completion and (5) next-task: both are read off this model by
  // setupHubRollup(), and commitCardState refuses a superseded identity, so a
  // late loser can revise neither.
  if (!commitCardState(identity, model)) return;
  if (!repaintSetupWorkflowCard(key, identity)) return;      // (1) DOM
  // A SILENT reconcile stops here: model and DOM are updated from fresh server
  // truth under this principal's own identity and permissions, and nothing is
  // said or focused on behalf of an operation this principal never started.
  if (o.silent) return;
  const failed = model.state === CARD_STATE.ERROR;
  announceCardStatus(identity, failed                        // (3) announcement
    ? (o.failed || `Still couldn't load ${w.title}.`)
    : (o.done || `${w.title} updated.`), failed);
  // (2) focus: the operator pressed Retry inside the slot that was just
  // replaced, so land them on this card's own heading rather than <body>.
  const landing = document.querySelector(`[data-setup-workflow-landing="${key}"]`);
  focusCardTarget(identity, landing
    ? landing.querySelector(".swf-landing-title")
    : document.querySelector(`[data-setup-workflow-card="${key}"] .swf-title`));
}

// Resolve an action reference ("secondary:0") back to its declaration. Kept
// as a lookup rather than stashing the action object on the DOM node so a
// repaint can never resurrect an action from an earlier definition.
function setupCardActionFor(w, ref) {
  const parts = String(ref || "").split(":");
  // "primary" resolves through the COMMITTED model, not the declared
  // `w.primary` (#365 review round 3). The landing's primary slot renders the
  // card's EFFECTIVE action, and once a derived action can carry a `confirm`
  // -- the reopen path -- looking the reference back up in the declared list
  // would confirm one action and execute a different one. `undefined` means
  // this card never carried a derivation (ERROR, or a model committed before
  // one was computed), which falls back to the declared primary exactly as
  // setupLandingActionsHtml does.
  if (parts[0] === "primary") {
    const entry = readCardState(setupWorkflowCardId(w.key));
    return entry.effective === undefined ? (w.primary || null) : entry.effective;
  }
  const list = parts[0] === "tertiary" ? (w.tertiary || []) : (w.secondary || []);
  return list[Number(parts[1]) || 0] || null;
}

// The CONFIRM state (#365). Opens a card-scoped confirmation for an action
// that declares one, bound to this card's identity so a context switch
// withdraws it rather than leaving a prompt standing for a Program the
// operator has left.
function askSetupCardConfirm(key, ref) {
  const w = setupWorkflowsFor().find((x) => x.key === key);
  if (!w) return;
  const action = setupCardActionFor(w, ref);
  if (!action || !action.confirm) return;
  // Through readCardState(), never the raw cardStates entry: on a card the
  // operator has just come BACK to with a write still unresolved, the raw
  // entry is whatever the other tuple's renders left behind, and this opener
  // has to see the OPERATION. Reading the operation is what puts the refusal
  // below on the serialization rule rather than on which tuple happened to
  // commit last.
  const held = readCardState(setupWorkflowCardId(key));
  // A card holding data from a context the operator has left must not open a
  // confirmation bound to it.
  if (!held || !cardTupleCurrent(held.identity)) return;
  const identity = beginCardRequest(setupWorkflowCardId(key), { userInitiated: true });
  // Refused while this card's own write against this tuple is unresolved:
  // opening a second confirmation is how a second write starts. The PENDING
  // body and the withdrawn landing groups mean no control exists to reach
  // this, so this is the code-level floor under those two surfaces, not a
  // substitute for them.
  if (!identity) return;
  commitCardState(identity, Object.assign({}, held, {
    state: CARD_STATE.CONFIRM, confirm: action.confirm,
    pending: ref, resumeState: held.state }));
  if (!repaintSetupWorkflowCard(key, identity)) return;
  announceCardStatus(identity, action.confirm.prompt);
  const slot = document.querySelector(`[data-setup-card-slot="${key}"]`);
  // A confirmation that REQUIRES a reason lands focus on the field the
  // operator has to fill, not on the button that would reject it.
  focusCardTarget(identity, slot && (slot.querySelector("[data-setup-card-confirm-reason]")
    || slot.querySelector("[data-setup-card-confirm-yes]")));
}

function resolveSetupCardConfirm(key, yes) {
  const w = setupWorkflowsFor().find((x) => x.key === key);
  // Same reason as askSetupCardConfirm: the OPERATION outranks the raw entry,
  // so a card carrying an unresolved write reads PENDING here and this
  // resolver declines rather than resolving a confirmation that is not the
  // card's current state.
  const held = readCardState(setupWorkflowCardId(key));
  if (!w || !held || held.state !== CARD_STATE.CONFIRM) return;
  if (!cardTupleCurrent(held.identity)) return;
  const action = setupCardActionFor(w, held.pending);
  const c = (action && action.confirm) || {};
  // Read the reason BEFORE anything repaints the slot the field lives in.
  const field = document.querySelector(`[data-setup-card-confirm-reason="${key}"]`);
  const reason = field ? String(field.value || "").trim() : "";
  // A required reason that is blank keeps the confirmation OPEN with an
  // error, rather than firing a write the route will reject (#159 requires a
  // non-empty reason) or -- worse -- inventing one on the operator's behalf.
  if (yes && c.reason && !reason) {
    const stay = beginCardRequest(setupWorkflowCardId(key), { userInitiated: true });
    if (!stay) return;   // #365: refused — a write for this card is unresolved
    if (!commitCardState(stay, Object.assign({}, held, {
      confirmReason: "", confirmError: "Add a reason before reopening." }))) return;
    if (!repaintSetupWorkflowCard(key, stay)) return;
    announceCardStatus(stay, "Add a reason before reopening.", true);
    const slot = document.querySelector(`[data-setup-card-slot="${key}"]`);
    focusCardTarget(stay, slot && slot.querySelector("[data-setup-card-confirm-reason]"));
    return;
  }
  // A confirmed action that performs its OWN write from this card is handed
  // straight to the writer, BEFORE anything here restores a settled state or
  // announces (#365 review round 4). This is the fix for the reported defect:
  // the code below restores READY, drops the confirmation and the reason, and
  // announces `c.done` — all of which used to run BEFORE the reopen POST had
  // even left the browser. That is a success claimed in advance, a recovery
  // path thrown away while it is still needed, and a card left actionable
  // with a write outstanding. The other three action kinds (`go`, `act`,
  // `open`) navigate to a surface that owns its own write, so for them
  // "confirmed" genuinely IS the end of this card's part and the restore
  // below is correct.
  if (yes && action && action.reopen) {
    return reopenSelectedSeasonFromCard(key, held, c, reason);
  }
  const identity = beginCardRequest(setupWorkflowCardId(key), { userInitiated: true });
  if (!identity) return;   // #365: refused — a write for this card is unresolved
  commitCardState(identity, Object.assign({}, held, {
    state: held.resumeState || CARD_STATE.READY, confirm: null, pending: null,
    confirmReason: null, confirmError: null }));
  repaintSetupWorkflowCard(key, identity);
  // Success and cancellation both announce exactly once, through the one
  // sitewide live region -- the card's own visible copy is not itself a live
  // region, so neither is spoken twice.
  //
  // CANCELLATION stays on this surface: nothing navigates, nothing clears the
  // region afterwards, so it is announced here and now.
  if (!yes) {
    announceCardStatus(identity, c.cancelled || "Cancelled.");
    focusCardTarget(identity, document.querySelector(`[data-setup-card-ask="${key}"]`));
    return;
  }
  // COMPLETION, for the three action kinds that hand off to a destination
  // owning its own write. Each of them navigates, and a navigation withdraws
  // the region in the same task -- so the sentence is announced AFTER the
  // destination render instead of before it. See announceCardStatusAfter().
  const done = c.done || "Continuing.";
  if (action.go) {
    return announceCardStatusAfter(identity, done, () => runSetupWorkflowGo(action.go));
  }
  if (action.act) {
    return announceCardStatusAfter(identity, done, () => openSetupWorkflowDrawer(action.act));
  }
  if (action.open) {
    return announceCardStatusAfter(identity, done, () => openSetupWorkflowLanding(action.open));
  }
  // No destination at all: nothing is going to clear the region, so this is
  // the ordinary in-place announcement.
  announceCardStatus(identity, done);
}

// The REAL reopen-Season write, fired from a card whose blocking prerequisite
// is `season_active` (#365 review round 3). The FOURTH action kind, and the
// only one that mutates from the card itself rather than navigating to a
// surface that does -- Season lifecycle has no workflow, landing or drawer of
// its own, and #159 makes the reopen a single reasoned write.
//
// Deliberately card-scoped, all the way through:
//
//  * The target is the SELECTED Season, re-read from the live context at fire
//    time and cross-checked against the identity the card committed under.
//    #367's rule is that reopening requires the Season to be the selected
//    context; asserting it here means a switch that slipped between the
//    confirmation and the click reopens NOTHING rather than the wrong Season.
//  * Recovery is this card's OWN refresh, not render(). A full render
//    re-commits every Setup card and would bump every neighbouring
//    generation -- the exact "adjacent-card mutation" the per-card discipline
//    forbids. retrySetupWorkflowCard re-reads the overview and the progress
//    payload for this card alone, so `season_active` flips to met and the
//    chain settles on whatever the NEXT unmet floor is (or the declared
//    primary), under a fresh generation of this card and no other.
//  * No page reload anywhere: the tuple is unchanged (same Season id), so the
//    committed identity stays valid across the whole sequence.
//
// #365 REVIEW ROUND 4 — WHAT WAS WRONG AND WHAT REPLACES IT
// ---------------------------------------------------------
// The first version was written as fire-and-forget optimism: the caller had
// already restored READY, removed the reason field and announced "Season
// reopened." before this function's `await` was even reached, and this
// function checked the card's identity ONLY BEFORE that await. After it, both
// the failure and the success branch wrote the sitewide toast, and the
// success branch called retrySetupWorkflowCard() — bumping a generation,
// re-committing a model, repainting and moving focus — without ever proving
// the initiating card, tuple and generation were still the current ones. Held
// open, an older Program/Season's response therefore reached in and changed
// the NEW tuple's card: its generation, its committed model and DOM, its
// focus, its announcement, and the completion and next-task lines derived
// from it. That is precisely the stale-response race #365 requires every card
// action to discard.
//
// The correction is not a new mechanism — it is the mechanism this slice
// already uses everywhere else, applied here too:
//
//   IDENTITY IS ISSUED ONCE, UP FRONT, and carried through the WHOLE
//   operation. `beginCardRequest` stamps card id + the exact
//   Program/Season/League tuple + this card's generation, and every later
//   step is gated on THAT record — not on a fresh one taken after the await,
//   which would be trivially current and would prove nothing.
//
//   THE EXACT SEASON IS CARRIED TOO — as a pin, not as a second guarantee.
//   `seasonId` is resolved once, here, and re-verified after the await. It
//   cannot currently disagree with the identity's tuple (both read the same
//   contextOptions.selected.season_id, synchronously, one line apart), so the
//   protection above is entirely cardIdentityCurrent's; the pin exists because
//   this write names its target explicitly in the URL instead of inheriting it
//   from the tuple. See the gate itself for the full note.
//
//   NOTHING IS RESTORED OR ANNOUNCED BEFORE THE SERVER CONFIRMS. The card
//   goes to PENDING, keeps its counts, offers no control at all, and holds
//   focus on its own pending line. The only thing said out loud before the
//   response is the progress sentence (`confirm.busy`), which claims nothing.
//
//   NOTHING HAPPENS AFTER THE AWAIT UNTIL THE INITIATING IDENTITY AND SEASON
//   ARE RE-VERIFIED. Both terminal branches necessarily advance this card to
//   its next generation — the failure branch by re-opening the confirmation,
//   the success branch through retrySetupWorkflowCard's own refresh — so that
//   one check, taken against the record captured BEFORE the await, is what
//   stands between an older tuple's response and the current card's model,
//   DOM, focus, live region, completion and next task. Everything after it is
//   synchronous, so there is no second window; and each individual mutation
//   still goes through the helper that enforces the same gate on its own
//   account (commitCardState, repaintSetupWorkflowCard, announceCardStatus,
//   focusCardTarget), exactly as every other card path in this file does.
//   A superseded response returns having done nothing whatsoever — including
//   nothing to the sitewide toast, which is why the transport here is
//   postScoped() rather than post().
async function reopenSelectedSeasonFromCard(key, held, c, reason) {
  const cardId = setupWorkflowCardId(key);
  // The EXACT Season this write targets, resolved ONCE. Everything after this
  // line — the URL, the identity check before the await and the identity
  // check after it — refers to this one value, so there is no second reading
  // of live context that could disagree with the first.
  const seasonId = contextOptions && contextOptions.selected
    && contextOptions.selected.season_id;
  if (!seasonId || !held || !cardTupleCurrent(held.identity)
      || held.identity.season_id !== seasonId) {
    toast = "That season is no longer the one you're working in — nothing was"
      + " reopened.";
    toastIsError = true;
    return updateToast();
  }
  // The INITIATING identity, issued once and carried through the whole
  // operation — the record every later step is judged against. It stops being
  // current the moment any other request for this card is started or the
  // operator moves to another Program/Season/League.
  const identity = beginCardRequest(cardId, { userInitiated: true });
  // Refused when a reopen for this card is ALREADY unresolved — the duplicate
  // write itself, stopped at the source. Unreachable through the UI (neither
  // surface paints a control in PENDING), and asserted anyway: this is the one
  // line that makes "at most one unresolved write per card" true of the code
  // rather than true of the current markup.
  if (!identity) return;
  // PENDING, BEFORE the request goes out. Counts stay, every control is
  // withdrawn, and the reason travels with the state so the failure branch
  // below can hand it straight back. Committing PENDING is also what
  // REGISTERS this operation, so from here until its own response every
  // render-driven commit for this card is refused outright.
  if (!commitCardState(identity, Object.assign({}, held, {
        state: CARD_STATE.PENDING, confirmReason: reason, confirmError: null,
        pendingNote: c.busy || "Working…" }))) return;
  if (!repaintSetupWorkflowCard(key, identity)) return;
  // A progress sentence, not a completion one. The confirmation's prompt is
  // still standing in the live region otherwise, describing a question the
  // operator has already answered.
  announceCardStatus(identity, c.busy || "Working…");
  const slot = document.querySelector(`[data-setup-card-slot="${key}"]`);
  // Focus followed the controls that were just replaced; put it on the card's
  // own pending line rather than letting it fall to <body>.
  focusCardTarget(identity, slot && slot.querySelector("[data-setup-card-pending]"));
  const r = await postScoped(`/api/v2/setup/seasons/${seasonId}/reopen`,
                             { reason: reason });
  // ======================= AFTER THE AWAIT =======================
  // SETTLEMENT, FIRST AND UNCONDITIONALLY. This is the instant the operation
  // stops being unresolved, so it is the instant its ledger entry is
  // released — before any question is asked about where the operator is, what
  // the response says, or whether the initiating identity survived. Nothing
  // below can return early past it, because it is above every return.
  //
  // "Even when the initiating UI identity is stale" is the whole point. The
  // operator can be standing on another Program/Season when this lands; the
  // generation this card was on can have moved three times. None of that has
  // any bearing on whether the REQUEST settled — it did — and a ledger that
  // drained only on the path where the identity is still current would leave
  // the target tuple's card blocked forever the moment the operator glanced
  // at another Season.
  const settled = settleCardWrite(identity);
  // Nothing to settle: this operation was never registered (its PENDING
  // commit was refused) or has already settled once. Either way there is no
  // outstanding write here and nothing this response may act on.
  if (!settled) return;
  // ---- WHICH TUPLE IS ON SCREEN decides what may happen next. ----
  //
  // NOT the target's: the round-4 rule, unchanged and unweakened. The response
  // describes a Season the operator is no longer working in, so applying any
  // part of it would paint one Season's outcome under another Season's
  // heading. It returns having done NOTHING at all — no toast, no live region,
  // no repaint, no refresh, no focus, no completion or next-task change, on
  // either tuple. postScoped() is what makes "nothing" literally true: post()
  // would already have published the server's error message into the sitewide
  // toast before this line was ever reached. The ledger is drained all the
  // same, so returning to the target later reads the server rather than a
  // permanent block.
  //
  // The `season_id` conjunct is REDUNDANT TODAY and is not a second
  // guarantee — say so plainly rather than let it read as one. `seasonId` is
  // read from contextOptions.selected.season_id, and `identity.season_id` is
  // set from currentCardTuple(), which reads that same field on the next
  // synchronous line; the two cannot disagree, and cardTupleCurrent already
  // compares identity.season_id against the live tuple. It is kept because
  // this write names its target EXPLICITLY in the URL rather than inheriting
  // it from the tuple: if a future card ever reopens a Season other than the
  // selected one, the tuple check alone would stop covering the target and
  // this line would become the thing that does. Do not cite it as proof of
  // anything the tuple check is not already proving.
  if (!cardTupleCurrent(identity) || identity.season_id !== seasonId) return;
  // THE TARGET IS CURRENT, BUT THE AUTHENTICATED PRINCIPAL HAS CHANGED (#365
  // review round 7). A DIFFERENT PERSON is signed in and is standing on the
  // Season this write targets — an in-app persona switch through signIn(), or
  // a real sign-out followed by a sign-in, both with no page reload.
  //
  // Taken BEFORE cardIdentityCurrent()'s combined test, even though that test
  // now subsumes it, because the two cases need DIFFERENT handling and merging
  // them would give the arriving principal the departing one's refresh
  // sentence. Everything below this line — the held-model restore, the toast,
  // the live-region write, the repaint, the focus move, the completion claim
  // and the next-task mutation — belongs to `identity.principal`, and every
  // one of them would be an operation the arriving principal never started
  // announcing itself in their session. With a lower-privileged arriving role
  // the held restore would additionally hand back a confirmation and a control
  // that role is not authorized to exercise.
  //
  // So: reconcile from FRESH SERVER TRUTH, under a NEW identity issued to the
  // ARRIVING principal, SILENTLY. The response itself is never consumed — not
  // its status, not its message, not the model this operation was holding. The
  // card stops showing the neutral pending presentation and starts showing
  // what the server says NOW, with the actions the ARRIVING principal's own
  // permissions allow: an Arena Manager on a Season the server still reports
  // archived gets the withdrawn-action explanation, not League Admin's reopen
  // control. The ledger was drained above, unconditionally, so this refresh is
  // refused by nothing and the target is not left blocked.
  if (!cardIdentitySamePrincipal(identity)) {
    return retrySetupWorkflowCard(key, { silent: true });
  }
  // THE TARGET IS CURRENT, BUT THE INITIATING IDENTITY IS NOT. The operator
  // left this tuple and came back while the request was in flight: the DOM
  // this operation painted was destroyed, cardStates was overwritten by the
  // other tuple's renders, and this card's generation moved on without it.
  //
  // The client therefore no longer knows what the server did — and, crucially,
  // NEITHER DOES THIS RESPONSE. A 503 delivered here does not mean the write
  // did not commit (navigation cancelled nothing, and the transaction behind
  // it may have gone through); a 200 delivered here describes a Season the
  // client has since stopped tracking. So NOTHING is restored from held state
  // and no completion claim is made from the response. The card is RECONCILED
  // FROM FRESH SERVER TRUTH instead — retrySetupWorkflowCard re-reads the
  // overview and the progress payload for this card alone and rebuilds the
  // model from what comes back, so the prerequisite chain decides the outcome:
  // a Season the server still reports archived lands on the REAL recovery
  // action, and one the server reports active advances to the next valid
  // action. Its own generic sentence describes the reconcile and claims
  // nothing about the write, which is the only honest thing left to say.
  //
  // It is refused by nothing: the registration was released above, so the gate
  // this refresh has to pass is already open, and the LOADING model it commits
  // synchronously keeps every action withdrawn until the fresh read lands.
  if (!cardIdentityCurrent(identity)) return retrySetupWorkflowCard(key);
  // ---- IN PLACE: never left, DOM intact, generation untouched. ----
  // The round-4 terminal branches, exactly as they were: this response IS the
  // one the card on screen is waiting for, and the client's held state is
  // still a truthful description of the card the operator is looking at.
  if (!r || r.error) {
    // A CURRENT failure restores an actionable confirmation with the entered
    // reason retained — the operator's own words are not thrown away and made
    // them type again — plus the server's own message, and focus on the
    // reason field so the retry is one keystroke away. `confirm`, `pending`
    // and `resumeState` ride along in `held`, so the restored confirmation is
    // the same one, resolvable by the same code path.
    const next = beginCardRequest(cardId, { userInitiated: true });
    if (!next) return;
    const message = (r && r.error && r.error.message)
      || "The season could not be reopened.";
    if (!commitCardState(next, Object.assign({}, held, {   // model + (4)/(5)
          state: CARD_STATE.CONFIRM, confirmReason: reason,
          confirmError: message }))) return;
    if (!repaintSetupWorkflowCard(key, next)) return;      // (1) DOM
    announceCardStatus(next, message, true);               // (3) announcement
    const back = document.querySelector(`[data-setup-card-slot="${key}"]`);
    focusCardTarget(next, back                             // (2) focus
      && (back.querySelector("[data-setup-card-confirm-reason]")
          || back.querySelector("[data-setup-card-confirm-yes]")));
    return;
  }
  // A CURRENT success refreshes ONLY this card, and says so exactly once.
  // (4) completion and (5) next task are derived from the model that refresh
  // commits under its own identity gate, so a late loser can revise neither.
  // The refresh gets past the serialization gate because settlement above
  // already released this operation's registration — not because it carries a
  // token exempting it.
  return retrySetupWorkflowCard(key, {
    done: c.done || "Season reopened.",
    failed: c.doneNoRefresh || c.done || "Season reopened." });
}

function wireSetupWorkflowCards(root) {
  root.querySelectorAll("[data-setup-card-retry]").forEach((b) =>
    b.onclick = () => retrySetupWorkflowCard(b.dataset.setupCardRetry));
  root.querySelectorAll("[data-setup-card-ask]").forEach((b) =>
    b.onclick = () => askSetupCardConfirm(b.dataset.setupCardAsk, b.dataset.setupCardAction));
  root.querySelectorAll("[data-setup-card-confirm-yes]").forEach((b) =>
    b.onclick = () => resolveSetupCardConfirm(b.dataset.setupCardConfirmYes, true));
  root.querySelectorAll("[data-setup-card-confirm-no]").forEach((b) =>
    b.onclick = () => resolveSetupCardConfirm(b.dataset.setupCardConfirmNo, false));
}

// Hub index: the six workflows as summary cards. This is what replaces the
// undifferentiated Setup mega-page as the DEFAULT route -- the mega-page's
// two sub-views stay reachable from the toggle, so nothing that was
// reachable before becomes unreachable.
// #367 owner ruling: the whole Setup surface -- hub, every workflow landing
// and Records -- is ceilinged on the ACTIVE Season, and the copy has to SAY
// so. An operator who cannot find last season's division must be able to
// tell a SELECTION from a deletion, and one working in a Program-only
// context must know why the season-bound cards are empty rather than
// concluding the data is gone.
//
// The second sentence is the deliberate asymmetry the ruling calls out and
// requires stating explicitly: Venue/Rink/IceSlot (and their owning
// facility organization) have a Season axis, through SeasonVenueAccess, but
// NO competition-League axis at all -- so selecting a League never narrows
// them. They stay season-wide across every league, on purpose, and that is
// a fact about the domain rather than a filter someone forgot to apply.
function setupScopeNote(sv) {
  // Guard the payload, not each accessor -- the same reason (and the same
  // convention) as buildSetupWorkflowCardModel above: renderSetup is reachable before
  // or without a successful overview load (an early return, a failed fetch,
  // a view that changed while render() was awaiting), and a note about
  // scope must degrade to nothing rather than throw and blank the view.
  if (!sv) return "";
  const season = (sv.seasons || [])[0];
  if (!season) {
    return `<p class="muted setup-scope-note" data-setup-scope-note>
      No season is selected, so season records — seasons, divisions, venues,
      rinks and ice — stay hidden. Pick a season in the context bar to work
      on it. Programs, leagues, teams and clubs are not season-bound and are
      shown as usual.</p>`;
  }
  const lid = contextOptions && contextOptions.selected
    && contextOptions.selected.league_id;
  const league = lid && (sv.leagues || []).find((lg) => lg.id === lid);
  return `<p class="muted setup-scope-note" data-setup-scope-note>
    Showing the <strong>${esc(season.name)}</strong> season${league
      ? `, league <strong>${esc(league.name)}</strong>` : ", all leagues"}.
    Switch in the context bar to set up a different one. Venues, rinks and
    ice belong to a season, not a league, so they always show the whole
    ${esc(season.name)} season across every league.</p>`;
}

function renderSetupHub(sv, ov) {
  const mine = setupWorkflowsFor();
  if (!mine.length) {
    return `<div class="empty">Your role doesn't manage any setup workflows.
      Ask a league admin for access if you need to change this program's structure.</div>`;
  }
  const cards = mine.map((w) => `
    <section class="swf-card" data-setup-workflow-card="${esc(w.key)}">
      <header class="swf-head">
        <span class="swf-ico" aria-hidden="true">${w.icon}</span>
        <h3 class="swf-title">${esc(w.title)}</h3>
        ${setupCardStatusChip(readCardState(setupWorkflowCardId(w.key)), w)}
      </header>
      <p class="swf-purpose">${esc(w.purpose)}</p>
      ${setupCardSlotHtml(w, false)}
      <button class="act ghost swf-open" data-setup-workflow="${esc(w.key)}">
        Open ${esc(w.title.toLowerCase())}</button>
    </section>`).join("");
  return `${pageIntro("Setup is six focused workflows. Open the one you need — "
    + "each opens on a summary, not a form.")}
    ${setupScopeNote(sv)}
    ${setupHubProgressSlotHtml()}
    <div class="swf-grid">${cards}</div>`;
}

// WHICH action groups a landing may render, decided ONCE from the card's own
// state (#365 review round 2 finding 1). The landing used to render primary,
// secondary and tertiary unconditionally, so the state the card was in had no
// bearing on what an operator could press: an EMPTY landing offered a whole
// menu instead of the one action that starts the workflow, and a STALE one
// kept every mutation control standing beside another context's counts.
//
// A record rather than three tests at three call sites: a future action group
// gets its answer here or not at all, and no renderer can add one that forgot
// to ask. The states are exhaustive by construction — the default is the only
// permissive answer, and every withdrawal is named.
//
//   STALE    only Refresh (which lives in the card body, not here): the
//            counts belong to a context the operator has left, so every
//            control bound to them is withdrawn — a mutation fired from here
//            would act on the CURRENT tuple using the OLD one's evidence.
//   CONFIRM  only the confirmation's own Yes/No, in the card body. A
//            confirmation that leaves the action it is confirming (and its
//            neighbours) live is not a confirmation.
//   LOADING  nothing is known about this card yet; the primary is held back
//            with the rest rather than being the one control that outruns
//            its own summary.
//   EMPTY    only the authorized EFFECTIVE action — the single thing that
//            resolves THIS emptiness, derived from the workflow's declared
//            prerequisite chain against live counts (setupEffectiveAction,
//            committed into the model as `effective`) rather than the static
//            `w.primary`, which for Facilities is "Add Ice" and needs a rink
//            that needs a venue. Role authorization is already handled:
//            setupWorkflowsFor() refuses the whole workflow (and this landing
//            with it) for a role lacking its `perm`, and an `open` step
//            targeting a workflow this role cannot open resolves to no action
//            at all rather than to a dead control.
//   BLOCKED  the same withdrawal, in ANY settled state, whenever the card's
//            model carries an unmet prerequisite (#365 owner ruling round 2).
//            This is not a seventh CARD_STATE — it is a property of the
//            derivation, and it is deliberately not folded into EMPTY: a
//            PARTIAL card (venues but no rinks, divisions but no team to
//            register) genuinely has records and must keep showing them, while
//            still offering only the one action that can create the next one.
//            The owner's ruling is that a neighbouring "Rinks" link does not
//            make a dead "Add Ice" acceptable, so the demoted groups are
//            withdrawn here exactly as they are in EMPTY: while a prerequisite
//            is missing the landing exposes EXACTLY the effective action, and
//            the card body carries the `blockedBecause` sentence that explains
//            it.
//   ERROR    keeps all three, deliberately and consistently with the copy the
//            card body shows in this state: "The action below still works —
//            only these counts are missing." Nothing about the actions is
//            unsafe; only the summary is missing.
//
// One withdrawal is NOT a state at all and deliberately precedes them: while
// a context switch has been ATTEMPTED but not yet reconciled
// (contextSwitchIntentPending, set synchronously at setActiveContext()'s
// first invalidation boundary), every landing mutation control is withdrawn
// no matter what state the card is holding. The card's own state still
// describes the tuple the operator has LEFT, so no value of it can answer
// "may this control commit right now" — only the intent flag can, and it
// clears only once a reconciliation has actually happened, on every success
// and failure path. Same idiom as #369's contextHashIntentPending.
//
// `blocked` is the model's own answer (a committed `blockedBecause`), never a
// re-derivation: the withdrawal and the sentence explaining it come from the
// same committed field, so a landing can never withdraw its demoted actions
// while the copy claims nothing is missing, or the reverse. It is consulted
// AFTER the three withdrawal states above, which withdraw everything anyway --
// a STALE card keeps whatever `blockedBecause` it settled with, and that
// answer belongs to the tuple the operator has left.
function setupLandingActions(state, blocked) {
  if (contextSwitchIntentPending) {
    return { primary: false, secondary: false, tertiary: false };
  }
  // PENDING joins the withdrawal list for a reason the other three do not
  // share: a control left standing here is not merely misleading, it is a
  // SECOND WRITE one press away, against a Season whose first write has not
  // reported back. "While current and pending, keep the card non-actionable"
  // (#365 review round 4) is enforced HERE, on the landing's action groups,
  // as well as in the card body — the two surfaces render the same card and
  // either one left actionable would be the same defect.
  if (state === CARD_STATE.STALE || state === CARD_STATE.CONFIRM
      || state === CARD_STATE.LOADING || state === CARD_STATE.PENDING) {
    return { primary: false, secondary: false, tertiary: false };
  }
  if (state === CARD_STATE.EMPTY || blocked) {
    return { primary: true, secondary: false, tertiary: false };
  }
  return { primary: true, secondary: true, tertiary: true };
}

// A landing's action groups, rendered from the card's CURRENT state. Lives
// inside the stable `[data-setup-landing-actions]` container, and is what a
// per-card repaint rewrites — so a card that settles into EMPTY, STALE or
// CONFIRM withdraws the controls that state forbids RIGHT THEN, instead of
// keeping whatever the last full render happened to paint. Same stable-
// container discipline as the hub roll-up: the write is unconditional and
// "no actions" is an empty container, never a skipped update.
function setupLandingActionsHtml(w) {
  // `go` routes through goToSetupWorkflow -- the seeded, fail-closed path
  // (#331 review rounds 5/6) that binds a create drawer to the ACTIVE
  // Program/Season instead of letting it fall back to a global first option.
  // `act` opens a plain entity drawer, identical to Records' own "+ New".
  // An action declaring `confirm` routes through the card's own CONFIRM state
  // (#365) instead of firing straight away; everything else is unchanged.
  // `open` is the third action kind (#365 owner ruling on EMPTY dead ends):
  // the missing prerequisite belongs to another workflow, so the control
  // opens THAT landing through the same permission-aware transition the hub's
  // own "Open …" buttons use. `reopen` is the fourth kind (#365 review round
  // 3) and it is the first DERIVED action that carries a `confirm`, so it
  // takes the confirmation branch below like any declared one -- which is
  // exactly why setupCardActionFor resolves "primary:0" through the committed
  // model rather than the declared `w.primary`.
  const attr = (a, i, group) => a.confirm
    ? `data-setup-card-ask="${esc(w.key)}" data-setup-card-action="${esc(group)}:${i}"`
    : a.open
    ? `data-setup-workflow="${esc(a.open)}"`
    : a.go
    ? `data-setup-workflow-go="${esc(a.go)}"` : `data-setup-workflow-act="${esc(a.act)}"`;
  const act = (a, cls, i, group) =>
    `<button class="act ${cls}" ${attr(a, i, group)}>${esc(a.label)}</button>`;
  const entry = readCardState(setupWorkflowCardId(w.key));
  const allow = setupLandingActions(entry.state, !!entry.blockedBecause);
  // THE effective action, in EVERY settled state (#365 owner ruling round 2):
  // whatever the committed model resolved from the prerequisite chain — which
  // may be a demoted control, a sibling workflow, or (when this role cannot
  // resolve it) nothing at all — and the declared primary only when the chain
  // says that is genuinely the resolving action.
  //
  // Read off the MODEL, never re-derived here: re-deriving would read live
  // module state at paint time and could answer for a tuple the card is not
  // bound to. `undefined` means this card never carried a derivation (ERROR,
  // or a model committed before one was computed), which falls back to the
  // declared primary rather than silently rendering no action.
  const primaryAction = !allow.primary ? null
    : entry.effective === undefined ? w.primary : entry.effective;
  const secondary = allow.secondary
    ? (w.secondary || []).map((a, i) => act(a, "ghost", i, "secondary")).join("") : "";
  const tertiary = allow.tertiary
    ? (w.tertiary || []).map((a, i) =>
        `<button class="linklike" ${attr(a, i, "tertiary")}>${esc(a.label)}</button>`).join("")
    : "";
  return `${primaryAction || secondary ? `<div class="swf-actions">
      ${primaryAction ? act(primaryAction, "primary", 0, "primary") : ""}
      ${secondary}
    </div>` : ""}
    ${tertiary ? `<div class="swf-tertiary">${tertiary}</div>` : ""}`;
}

// The landing actions' own event wiring, shared by render()'s pass over the
// whole content element and by a per-card repaint that just replaced them --
// so a repainted action set is never a dead control.
function wireSetupLandingActions(root) {
  // Primary actions route through goToSetupWorkflow -- the seeded,
  // fail-closed path -- rather than opening a raw drawer, so a landing's
  // primary action carries the same active-Program binding the Home/Tasks
  // hub's does.
  root.querySelectorAll("[data-setup-workflow-go]").forEach((b) =>
    b.onclick = () => runSetupWorkflowGo(b.dataset.setupWorkflowGo));
  // Demoted actions go through the SAME seeded, fail-closed path as the
  // primary ones. They used to open a raw drawer with drawerValues = {},
  // mirroring Records' own "+ New" -- which is correct on Records (a flat
  // record-management surface) and wrong here, because a workflow landing is
  // scoped to the active Program and its parent selects are not.
  root.querySelectorAll("[data-setup-workflow-act]").forEach((b) =>
    b.onclick = () => openSetupWorkflowDrawer(b.dataset.setupWorkflowAct));
  // An `open` effective action (#365 owner ruling): the missing prerequisite
  // belongs to a sibling workflow, so this jumps to that landing through the
  // SAME permission-aware transition render()'s own [data-setup-workflow]
  // pass uses. Wired here too because a per-card REPAINT rewrites this
  // container without re-running render()'s wiring — a repainted cross-
  // workflow action would otherwise be a dead control.
  root.querySelectorAll("[data-setup-workflow]").forEach((b) =>
    b.onclick = () => openSetupWorkflowLanding(b.dataset.setupWorkflow || null));
}

// A single workflow's LANDING: summary first, then exactly one primary
// action, then the demoted ones. Detail lives one level in (the Records and
// Hierarchy views), per "show summary first; reveal detail progressively".
//
// The action groups themselves live in setupLandingActionsHtml() below, so
// the ONE state-dependent decision is made in one place and a per-card
// repaint re-makes it — an action set left over from an earlier state would
// be the same defect one repaint later.
function renderSetupWorkflowLanding(w, sv, ov) {
  // The optional note follows the LIVE model's partition flag (the backend's
  // own `status: "optional"`), falling back to the declared contract when no
  // progress payload is available -- same single source as the hub chip and
  // the Home/Tasks badge, so no surface can call this step optional while
  // another counts it as required work.
  const landingEntry = readCardState(setupWorkflowCardId(w.key));
  const isOptional = landingEntry.optional === undefined
    ? !!w.optional : !!landingEntry.optional;
  const optionalNote = isOptional
    ? `<p class="swf-optional-note">This step is optional — you can
        set everything up by hand instead, and skipping it never blocks the
        rest of setup.</p>` : "";
  return `
    <div class="swf-landing" data-setup-workflow-landing="${esc(w.key)}">
      <button class="linklike swf-back" data-setup-workflow="">← All setup workflows</button>
      <h2 class="swf-landing-title">${w.icon} ${esc(w.title)}</h2>
      <p class="swf-purpose">${esc(w.purpose)}</p>
      ${setupScopeNote(sv)}
      ${optionalNote}
      ${setupCardSlotHtml(w, true)}
      <div class="swf-landing-actions" data-setup-landing-actions="${esc(w.key)}"
        >${setupLandingActionsHtml(w)}</div>
      <div class="swf-detail">
        <div class="section-title">Detail</div>
        <button class="linklike" data-setup-view="records">All records</button>
        <button class="linklike" data-setup-view="hierarchy">Hierarchy view</button>
      </div>
    </div>`;
}

function renderSetup(sv, hv, ov) {
  const seg = (v, label) =>
    `<button class="seg ${setupView === v ? "active" : ""}" data-setup-view="${v}">${label}</button>`;
  const toggle = `<div class="seg-group setup-viewtoggle">
    ${seg("hub", "Workflows")}${seg("hierarchy", "Hierarchy")}${seg("records", "Records")}
  </div>`;
  let body;
  if (setupView === "hub") {
    const w = setupWorkflow
      && setupWorkflowsFor().find((x) => x.key === setupWorkflow);
    body = w ? renderSetupWorkflowLanding(w, sv, ov) : renderSetupHub(sv, ov);
  } else if (setupView === "hierarchy") {
    body = `${pageIntro("Review and fix how your venues, programs, teams, and rosters are connected.")}${renderSetupHierarchy(sv, hv, ov)}`;
  } else {
    const cards = SETUP_ENTITIES.map((ent) => setupCard(ent, sv)).join("");
    body = `<div class="setup-intro">Create your competition structure and arena. Tap
      <strong>＋ New</strong> on any card to open a form.</div>
      ${setupScopeNote(sv)}
      <div class="setup-grid">${cards}</div>`;
  }
  return `${toggle}${body}${renderDrawer(sv)}`;
}

function setupCard(ent, sv) {
  const items = ent.list ? ent.list(sv) : null;
  let body;
  if (items === null) {
    body = `<div class="setup-hint">Ice inventory lives on the
      <button class="linklike" data-goto="calendar">Arena Calendar</button>.</div>`;
  } else if (!items.length) {
    body = `<div class="empty">None yet — create the first one.</div>`;
  } else {
    body = items.map((it) => `<div class="li"><div class="li-main">
      <div class="li-title">${esc(it.title)}</div>
      ${it.sub ? `<div class="li-sub">${esc(it.sub)}</div>` : ""}</div>${
        ent.editKind && it.id ? editBtn(ent.editKind, it.id, it.title) : ""}${
        ent.activeKind && it.id
          ? activeBtn(ent.activeKind, it.id, it.title, it.active) : ""}${
        ent.delKind && it.id ? delBtn(ent.delKind, it.id, it.title) : ""}</div>`).join("");
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

function drawerField(f, sv) {
  const req = f.required ? ` <span class="req">*</span>` : "";
  // A locked field on an edit drawer (e.g. Team — reassignment is its own
  // operation) is shown for context but disabled (#268).
  const locked = drawer && drawer.mode === "edit" && f.lockOnEdit;
  const dis = locked ? " disabled" : "";
  const note = locked ? ` <span class="drawer-note-inline">— use “⇄ Move” to change</span>` : "";
  // Preserve what the user already typed/selected across an error re-render;
  // fall back to the field's default only on first open. A default may be a
  // function, evaluated per render, for fields whose sensible default depends
  // on current state rather than being fixed at load (#389 review).
  const dflt = typeof f.value === "function" ? f.value() : f.value;
  const current = f.id in drawerValues ? drawerValues[f.id] : (dflt || "");
  // A context value the operator never picks or sees (e.g. the active Season
  // a #345 context-seeded Division carries alongside its visible League
  // select) -- present in the submitted body via the same val()/drawerValues
  // pipeline as every visible field, without its own label/control.
  if (f.type === "hidden") return `<input id="${f.id}" type="hidden" value="${esc(current)}" />`;
  if (f.type === "select") {
    const rows = f.options(sv);
    if (!rows.length) {
      const noun = ofNounLabel(f);
      const article = /^[aeiou]/i.test(noun) ? "an" : "a";
      return `<label>${esc(f.label)}${req}</label>
        <div class="drawer-note">Create ${article} ${esc(noun)} first.</div>`;
    }
    const sel = current || rows[0][0];
    const opts = rows.map(([v, label]) =>
      `<option value="${esc(v)}"${v === sel ? " selected" : ""}>${esc(label)}</option>`).join("");
    return `<label>${esc(f.label)}${req}${note}</label><select id="${f.id}"${dis}>${opts}</select>`;
  }
  const type = f.type || "text";
  const attrs = `${current ? ` value="${esc(current)}"` : ""}${f.placeholder ? ` placeholder="${esc(f.placeholder)}"` : ""}`;
  return `<label>${esc(f.label)}${req}${note}</label><input id="${f.id}" type="${type}"${attrs}${dis} />`;
}

function renderDrawer(sv) {
  if (!drawer) return "";
  const ent = SETUP_ENTITIES.find((e) => e.key === drawer.kind);
  if (!ent) return "";
  const fields = ent.fields.map((f) => drawerField(f, sv)).join("");
  const err = drawerError ? `<div class="drawer-err">⚠ ${esc(drawerError)}</div>` : "";
  // An edit drawer (#268) reuses the same fields but corrects an existing
  // record in place: it never blocks on an empty parent select (its locked
  // Team is already set) and its title/action verb say "Edit"/"Save changes".
  const editing = drawer.mode === "edit";
  const noun = entNoun(ent);
  const heading = editing ? `Edit ${esc(noun)}` : `New ${esc(noun)}`;
  const action = editing ? "Save changes" : `Create ${esc(noun)}`;
  // A required select with no options can never be satisfied — block a CREATE
  // submit (an edit's parents already exist).
  const blocked = !editing && ent.fields.some(
    (f) => f.type === "select" && f.required && !f.options(sv).length);
  // tabindex="-1" (#345): makes the dialog CONTAINER programmatically
  // focusable so opening it can land focus on the dialog itself -- announcing
  // its role and accessible name -- instead of jumping straight into a form
  // control. -1 keeps it out of the sequential tab order, and
  // overlayFocusables() ignores tabindex="-1", so it never becomes a Tab stop.
  return `<div class="drawer-scrim" data-drawer-close></div>
    <aside class="drawer" role="dialog" aria-modal="true" tabindex="-1" aria-label="${heading}">
      <header class="drawer-head"><span class="drawer-ico">${ent.icon}</span>
        <span class="drawer-title">${heading}</span>
        <button class="drawer-x" data-drawer-close aria-label="Close">×</button></header>
      <div class="drawer-body">${fields}${err}</div>
      <footer class="drawer-foot">
        <button class="act ghost" data-drawer-close>Cancel</button>
        <button class="act primary" data-drawer-submit="${ent.key}"${blocked ? " disabled" : ""}>${action}</button>
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
  // Defensive fallback: an allocated game slot always carries a game_label,
  // and every other blocked slot_type is named above — this only fires for
  // a status this build doesn't otherwise recognize.
  return s.status === "allocated" ? "Allocated" : s.status === "blocked" ? "Blocked" : esc(s.status);
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
  const dropClick = (draggable && s.status === "available" && canMove)
    ? `data-slot="${esc(s.id)}" data-drop="${esc(s.id)}" role="button" tabindex="0" aria-label="${
        isTarget ? "Move game to" : "Schedule a game in"} this ${fmt(s.start_time)}–${fmt(s.end_time)} slot"` : "";
  const drag = (draggable && s.game_id && !moving && canMove) ? `draggable="true" data-game="${esc(s.game_id)}"` : "";
  // Draft/Published state comes from the schedule game, not the slot row.
  const g = (s.game_id && ctx) ? ctx.gameById[s.game_id] : null;
  const state = g ? (g.published ? " · Published" : " · Draft") : "";
  const isMovingThis = s.game_id && s.game_id === movingGameId;
  // A Move button gives touch/mobile/keyboard users a drag-free path (#move-mode).
  const moveBtn = (draggable && s.game_id && !moving && canMove)
    ? `<button class="icon-btn" data-move-game="${esc(s.game_id)}"
        title="Move game" aria-label="Move game">${ICONS.swap}</button>` : "";
  const extra = `${isTarget ? " move-target" : ""}${isMovingThis ? " moving" : ""}`;
  const cta = isTarget ? " · tap to move here" : (draggable && s.game_id && !moving && canMove ? " · drag or Move" : "");
  // An unused, future, available slot can be deleted straight from the Day
  // board (#215). The trash sits inside the card, which is itself a
  // click-to-schedule target, so the handler stops propagation.
  const canDeleteSlot = draggable && !moving && s.status === "available"
    && !s.game_id && hasPerm("manage_setup") && s.start_time
    && s.start_time > new Date().toISOString();
  const delSlot = canDeleteSlot
    ? `<button class="icon-btn danger slot-del" data-del="ice-slot" data-del-id="${esc(s.id)}"
        data-del-name="${esc(slotLabel(s))}" title="Delete this ice slot"
        aria-label="Delete this ice slot">${ICONS.trash}</button>` : "";
  // #277: playable span vs reserved facility time — when a hosted game's
  // effective policy reserves warm-up before / resurfacing after, show the
  // full blocked span so operators see what the building actually loses.
  const rsv = s.reserved
    ? `<div class="slot-reserved">reserved ${fmt(s.reserved.reserved_start_time)}–${fmt(s.reserved.reserved_end_time)} (+${s.reserved.warmup_minutes}m warm-up, +${s.reserved.resurfacing_minutes}m resurfacing)</div>`
    : "";
  return `<div class="slot-card ${cls}${extra}" ${dropClick} ${drag}><div class="t">${fmt(s.start_time)}–${fmt(s.end_time)}</div>${rsv}<div class="s">${slotLabel(s)}${state}${cta}</div>${moveBtn}${delSlot}</div>`;
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
  const head = calendarMode === "month"
    ? esc(fmtMonth(calendarDate))
    : calendarMode === "week"
    ? `Week of ${esc(fmtDate(startOfWeek(calendarDate)))}`
    : esc(fmtDate(calendarDate));
  return `
    <div class="cal-toolbar">
      <div class="cal-toprow">
        <div><div class="cal-date">${head}</div>
          <div class="cal-venue">${esc(calFilters.venueId === "all" ? "All venues" : (ov.venues.find((v) => v.id === calFilters.venueId) || {}).name || "Arena")}</div></div>
        <div class="cal-controls">
          <div class="seg-mini"><button class="segm ${calendarMode === "day" ? "active" : ""}" data-mode="day">Day</button>
            <button class="segm ${calendarMode === "week" ? "active" : ""}" data-mode="week">Week</button>
            <button class="segm ${calendarMode === "month" ? "active" : ""}" data-mode="month">Month</button></div>
          <div class="cal-nav"><button class="act ghost" data-cal="-1">‹</button>
            <button class="act ghost" data-cal="0">Today</button>
            <button class="act ghost" data-cal="1">›</button></div>
          ${hasPerm("manage_arena") ? `<button class="act ghost cal-build-ice" data-ice-builder-open>🧊 Build ice</button>` : ""}
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
      insufficient_playable_time: ["Slot too short",
        [`${target} offers only ${d.slot_minutes} playable minutes; this competition requires at least ${d.required_minutes}.`,
         "Pick a longer slot, or adjust the scheduling policy's minimum playable time."]],
      turnover_buffer_conflict: ["Turnover buffer conflict",
        [`${target} is too close to ${d.conflict_game_id ? gameName(d.conflict_game_id) : "another proposed game"} on the same rink.`,
         `The rink needs ${d.required_gap_minutes} minutes between games for warm-up and resurfacing; this gap is ${d.gap_minutes}.`]],
      slot_overlap_conflict: ["Overlapping ice",
        [`${target} overlaps ${d.conflict_game_id ? gameName(d.conflict_game_id) + "'s" : "another proposed game's"} slot on the same rink.`,
         "Two games can never share the same ice time, whatever the turnover policy — pick a slot that does not overlap."]],
      curfew_violation: ["Past curfew",
        [`${target} ends at ${d.slot_end_local} local time, past the ${d.curfew_local} curfew.`,
         "Pick an earlier slot, or adjust the curfew in the scheduling policy."]],
    };
    const [title, lines] = MAP[reason] || ["Move blocked", [res.error.message]];
    return { ok: false, title, lines };
  }
  // Success — surface consequences worth a heads-up, and carry the old slot so
  // the move can be undone (#153) by moving the game straight back.
  const m = (res && res.moved) || {};
  const lines = [`${gameName(gameId)} now plays ${slotName(m.new_slot_id || slotId)}.`];
  if (m.unpublished) lines.push("It was published, so the fixture reverted to Draft — re-publish when you're ready.");
  if (m.roster_unlocked) lines.push("The roster was locked, so it reopened — players must reconfirm the new time.");
  return { ok: true, title: "Game moved", lines,
    undo: m.old_slot_id ? { gid: gameId, oldSlotId: m.old_slot_id } : null };
}

function conflictPanelHtml() {
  if (!conflict) return "";
  const cls = conflict.ok ? "ok" : "bad";
  const icon = conflict.ok ? "✅" : "⛔";
  // Undo restores the slot/time/rink only — a move that reverted publish/lock
  // state can't silently re-apply it, so the copy says so (#153).
  const undoBtn = (conflict.ok && conflict.undo)
    ? `<div class="ca-actions"><button class="act ghost" data-move-undo>↩ Undo move</button></div>` : "";
  return `<aside class="cal-aside ${cls}">
    <div class="ca-head"><span class="ca-ico">${icon}</span><span class="ca-title">${esc(conflict.title)}</span>
      <button class="ca-x" data-conflict-dismiss aria-label="Dismiss">×</button></div>
    <div class="ca-body">${conflict.lines.map((l) => `<p>${esc(l)}</p>`).join("")}${undoBtn}</div>
  </aside>`;
}

// Pre-move confirmation (#153): moving a published/locked game silently
// reverts it to Draft / unlocks the roster, so stage the move and make the
// operator confirm — with the destination and the exact side effects spelled
// out — before it commits. A harmless draft move skips this entirely.
function movePanelHtml(ov) {
  if (!pendingMove) return "";
  const { gid, slotId, willUnpublish, willUnlock } = pendingMove;
  const g = ov.schedule.find((x) => x.game_id === gid);
  const name = g ? `${g.home_team_name} vs ${g.away_team_name}` : "this game";
  const s = ov.ice_slots.find((x) => x.id === slotId);
  const r = s && ov.rinks.find((rr) => rr.id === s.rink_id);
  const dest = s
    ? `${r ? r.name : "Rink"} · ${fmtDate(dayOf(s.start_time))} · ${fmt(s.start_time)}–${fmt(s.end_time)}`
    : "the new slot";
  const warn = [];
  if (willUnpublish) warn.push("This fixture is <strong>Published</strong> — moving it unpublishes it (reverts to Draft).");
  if (willUnlock) warn.push("The roster is <strong>locked</strong> — moving it unlocks the roster; players must reconfirm.");
  return `<aside class="cal-aside confirm">
    <div class="ca-head"><span class="ca-ico">🔀</span><span class="ca-title">Move this game?</span>
      <button class="ca-x" data-move-cancel-pending aria-label="Cancel">×</button></div>
    <div class="ca-body">
      <p><strong>${esc(name)}</strong><br>→ ${esc(dest)}</p>
      ${warn.map((w) => `<p class="ca-warn">⚠ ${w}</p>`).join("")}
      <div class="ca-actions">
        <button class="act primary" data-move-confirm>Move game</button>
        <button class="act ghost" data-move-cancel-pending>Cancel</button>
      </div>
    </div>
  </aside>`;
}

function renderCalendar(ov) {
  if (iceBuilder) return renderIceBuilder(ov);
  if (wizard) return renderWizard(ov);
  const ctx = calContext(ov);
  const rinks = visibleRinks(ov);
  const board = calendarMode === "month"
    ? renderMonth(ov, ctx, rinks)
    : calendarMode === "week"
    ? renderWeek(ov, ctx, rinks)
    : renderDay(ov, ctx, rinks);
  return calToolbar(ov) +
    `<div class="cal-layout"><div class="cal-main">${board}</div>${movePanelHtml(ov)}${conflictPanelHtml()}</div>`;
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
    // Demo-only quick-add: it posts to /api/demo/add-ice-slot, which is gated
    // off in production (#303). Hide it in production — like the other demo
    // controls — so operators use the real, attributed "＋ Add Ice" drawer
    // (/api/v2/setup/ice-slot) instead of hitting a 403 (#305/#303 follow-up).
    const addIce = (canArena && isDemo())
      ? `<div class="slot-card available" data-addslot="${esc(r.id)}"><div class="t">＋</div><div class="s">Add ice</div></div>` : "";
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
    ${drafts.map((g) => `<span class="chip-drag${g.game_id === movingGameId ? " moving" : ""}" ${moving ? "" : `draggable="true" data-game="${esc(g.game_id)}"`}>⠿ ${esc(g.home_team_name)} vs ${esc(g.away_team_name)} · ${fmt(g.start_time)}${moving ? "" : ` <button class="icon-btn" data-move-game="${esc(g.game_id)}" title="Move game" aria-label="Move game">${ICONS.swap}</button>`}</span>`).join("")}</div>` : "";
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

// Month overview (#158): a read-only calendar grid of ice density per day. Tap a
// day to open it in Day view. Six week-rows always cover any month.
function renderMonth(ov, ctx, rinks) {
  if (!rinks.length) return `<div class="empty">No rinks match the selected filters.</div>`;
  const rinkIds = new Set(rinks.map((r) => r.id));
  const byDay = {};
  ov.ice_slots.forEach((s) => {
    if (!rinkIds.has(s.rink_id) || !slotPasses(s, ctx)) return;
    const day = (s.start_time || "").slice(0, 10);
    if (!day) return;
    const b = byDay[day] || (byDay[day] = { total: 0, allocated: 0 });
    b.total += 1;
    if (s.status === "allocated") b.allocated += 1;
  });
  const monthNum = calendarDate.slice(0, 7);
  const gridStart = startOfWeek(monthNum + "-01");
  const cells = Array.from({ length: 42 }, (_, i) => addDays(gridStart, i));
  const dows = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    .map((d) => `<div class="mo-dow">${d}</div>`).join("");
  const body = cells.map((day) => {
    const inMonth = day.slice(0, 7) === monthNum;
    const b = byDay[day];
    const isToday = day === calendarDate;
    const badge = b
      ? `<span class="mo-count${b.allocated ? " has-alloc" : ""}">${b.total} ice${b.allocated ? ` · ${b.allocated} booked` : ""}</span>`
      : "";
    return `<button class="mo-cell${inMonth ? "" : " mo-out"}${isToday ? " mo-today" : ""}" data-cal-day="${day}">
      <span class="mo-num">${+day.slice(8, 10)}</span>${badge}</button>`;
  }).join("");
  return `<div class="mo-grid"><div class="mo-dows">${dows}</div><div class="mo-cells">${body}</div></div>
    <div class="privacy-note">📅 Month view is read-only — tap a day to open it in Day view.
    Counts are Available + booked Game/other ice on the visible rinks.</div>`;
}

/* ---------- Ice Availability Builder (#158) ----------
   An arena operator builds a draft ice INVENTORY from a recurring weekly block,
   previews the exact slots (with collisions, skipped exclusion dates, capacity,
   and any venue not granted to the Season), then explicitly creates the
   Available Game ice. No games or published schedule are created here — the
   planner consumes this inventory later. Backend: /api/setup/ice-availability/
   {preview,commit}. Weekdays follow the backend's Mon=0..Sun=6 convention. */
const IB_WEEKDAYS = [[0, "Mon"], [1, "Tue"], [2, "Wed"], [3, "Thu"],
                     [4, "Fri"], [5, "Sat"], [6, "Sun"]];

function defaultIceForm(ov) {
  const seasons = ov.seasons || [];
  // Prefer the #159 ACTIVE Season selection over "the first active/global
  // Season" (#331 review round 5 finding 4). When that rule was written
  // `seasons` here spanned every Program (GET /api/demo/overview was
  // unfiltered), so picking the first status==="active" row could default the
  // builder onto a DIFFERENT Program's Season than the one the Home/Tasks hub
  // CTA was scoped to, and a committed submit against that silent wrong
  // default would generate ice for the wrong Program. #369 has since narrowed
  // `ov.seasons` to the ACTIVE Season only, which independently closes the
  // cross-Program half -- but this binding stays load-bearing for the
  // fail-closed half below, and must not be relaxed back to a global default.
  //
  // Fails CLOSED, not to that same global-first Season, when none is
  // actively selected -- a Program-only context, or no context at all
  // (#331 review round 8: this used to fall back to `seasons.find(active)
  // || seasons[0]` for exactly that case, the identical unsafe default
  // Import's own Season select was already fixed to refuse in round 7).
  // renderIceBuilder()'s own <select> below renders an explicit disabled
  // placeholder rather than letting a native <select> silently pick its
  // first option when none is marked `selected`, the same fail-closed
  // pairing Import already uses. A stale/deleted selected id ALSO resolves
  // to nothing here (`selected` stays undefined), never a silent fallback.
  const selectedId = contextOptions && contextOptions.selected
    && contextOptions.selected.season_id;
  const selected = selectedId ? seasons.find((s) => s.id === selectedId) : null;
  const active = selected || null;
  const rinks = ov.rinks || [];
  return {
    season_id: active ? active.id : "",
    rink_ids: rinks.length === 1 ? [rinks[0].id] : [],
    weekdays: [1, 3],                 // Tue/Thu — the common contracted block
    // Each selected weekday carries its OWN local start/end time (#158 flow):
    // a real arena rarely runs identical hours every night. A single block is
    // just the case where every selected day shares the same window.
    windows: [{ weekday: 1, start_local: "18:00", end_local: "22:00" },
              { weekday: 3, start_local: "18:00", end_local: "22:00" }],
    start_date: "", end_date: "",     // blank => backend uses the Season range
    playable_minutes: 60, turnover_minutes: 15,
    exclusion_dates: [],
  };
}

// The window (local start/end) currently entered for a weekday, defaulting to
// the common evening block for a freshly-checked day.
function ibWindowFor(f, wd) {
  return (f.windows || []).find((w) => w.weekday === wd)
    || { start_local: "18:00", end_local: "22:00" };
}

function ibLocalTime(iso) { return (iso || "").slice(11, 16); }
// The preview's start_local/end_local ISO strings carry the resolved UTC
// offset (e.g. "...T01:00:00-04:00"); reusing it — rather than a zone
// abbreviation table — disambiguates two fall-back rows that read the same
// local clock time, for free, from data the backend already computed.
function ibLocalOffset(iso) {
  const m = /([+-]\d{2}:\d{2})$/.exec(iso || "");
  return m ? m[1] : "+00:00";
}
function ibDateOnly(v) { return v ? String(v).slice(0, 10) : "—"; }

function renderIceBuilder(ov) {
  // Rebind to the active Program/Season on any context revision (#331
  // review round 7): a form/preview cached from BEFORE the operator
  // switched Program via the #159 switcher is exactly the same
  // committable wrong-Program risk as Import's own stale Season (both
  // send season_id verbatim to their commit endpoint, and the WRITE path
  // takes that id at its word -- #367/#369 scoped the operational reads
  // and added the parent-id write gate, but a cached form still holds an
  // id chosen under the PREVIOUS selection, which nothing else here
  // re-validates). Rinks are ALSO Program/Venue-scoped, so a context change
  // discards the WHOLE form, not just season_id -- defaultIceForm(ov)
  // already prefers the active Season the same way Import's own re-seed
  // does. Clearing preview too makes it uncommittable the same way an
  // edited form already does (Create is bound to the previewed
  // template's own fingerprint, see its onclick below): with no preview,
  // Create has nothing to send.
  if (iceBuilder.contextRevision !== contextRevision) {
    iceBuilder.form = defaultIceForm(ov);
    iceBuilder.preview = null;
    iceBuilder.contextRevision = contextRevision;
  }
  const f = iceBuilder.form;
  const seasons = ov.seasons || [];
  const season = seasons.find((s) => s.id === f.season_id) || null;
  // No selected `<option>` (#331 review round 8, mirroring Import's own
  // round 7 fix) when f.season_id is unset -- fails CLOSED to an explicit,
  // disabled placeholder rather than the native <select>'s own "no option
  // marked selected -> pick the first one" default, which would silently
  // reintroduce a global-first Season exactly like defaultIceForm() now
  // deliberately omits above.
  const seasonOpts = (!f.season_id
      ? `<option value="" selected disabled>— select a season —</option>` : "")
    + seasons.map((s) =>
    `<option value="${esc(s.id)}" ${s.id === f.season_id ? "selected" : ""}>${esc(s.name)}</option>`).join("");
  const venues = ov.venues || [];
  const rinkCheck = (r) =>
    `<label class="ib-check"><input type="checkbox" class="ib-rink" value="${esc(r.id)}" ${f.rink_ids.includes(r.id) ? "checked" : ""}> ${esc(r.name)}</label>`;
  const venueGroups = venues.map((v) => {
    const rs = (ov.rinks || []).filter((r) => r.venue_id === v.id);
    return rs.length ? `<div class="ib-venue"><div class="ib-venue-name">${esc(v.name)}</div>${rs.map(rinkCheck).join("")}</div>` : "";
  }).join("");
  const orphans = (ov.rinks || []).filter((r) => !venues.some((v) => v.id === r.venue_id));
  const orphanGroup = orphans.length ? `<div class="ib-venue">${orphans.map(rinkCheck).join("")}</div>` : "";
  const weekdayChips = IB_WEEKDAYS.map(([n, lbl]) =>
    `<label class="ib-day"><input type="checkbox" class="ib-weekday" value="${n}" ${f.weekdays.includes(n) ? "checked" : ""}>${lbl}</label>`).join("");
  // One local start/end row per SELECTED weekday (sorted) — the #158 operator
  // flow gives each day its own time. Freshly-checked days default to the block.
  const perDayRows = f.weekdays.slice().sort((a, b) => a - b).map((wd) => {
    const w = ibWindowFor(f, wd);
    const lbl = (IB_WEEKDAYS.find(([n]) => n === wd) || [wd, "?"])[1];
    return `<div class="ib-wd-row" data-weekday="${wd}">
      <span class="ib-wd-name">${esc(lbl)}</span>
      <input type="time" class="ib-wd-start" data-weekday="${wd}" id="ib-start-${wd}" value="${esc(w.start_local)}" aria-label="${esc(lbl)} start time">
      <span class="ib-wd-sep">–</span>
      <input type="time" class="ib-wd-end" data-weekday="${wd}" id="ib-end-${wd}" value="${esc(w.end_local)}" aria-label="${esc(lbl)} end time">
    </div>`;
  }).join("");
  const exclusionChips = f.exclusion_dates.length
    ? f.exclusion_dates.map((d) => `<span class="ib-chip">${esc(d)}<button class="ib-chip-x" data-ib-excl-remove="${esc(d)}" aria-label="Remove ${esc(d)}">×</button></span>`).join("")
    : `<span class="ib-none">None</span>`;
  const seasonHint = season
    ? `<div class="ib-hint">Season runs ${esc(ibDateOnly(season.start_date))} → ${esc(ibDateOnly(season.end_date))}. Leave the date range blank to cover the whole Season. Times are in the Season's local timezone; Season dates are never changed here.</div>`
    : `<div class="ib-hint">Select a Season — slots generate in its local timezone.</div>`;

  return `<div class="ib-wrap">
    <div class="ib-head"><h2>🧊 Build recurring ice</h2>
      <button class="act ghost" data-ib-cancel>← Back to calendar</button></div>
    <p class="ib-lead">Generate a draft ice inventory from a recurring weekly block, preview every slot,
      then create the Available Game ice. Nothing is scheduled here.</p>
    <div class="card ib-form">
      <label class="ib-field"><span>Season</span>
        <select id="ib-season">${seasonOpts || `<option value="">No seasons yet</option>`}</select></label>
      ${seasonHint}
      <div class="ib-field"><span>Rinks</span>
        <div class="ib-rinks">${venueGroups}${orphanGroup}${(ov.rinks || []).length ? "" : `<div class="ib-none">No rinks yet — add rinks in Setup first.</div>`}</div></div>
      <div class="ib-field"><span>Weekdays</span><div class="ib-days">${weekdayChips}</div></div>
      <div class="ib-field"><span>Time per weekday</span>
        <div class="ib-wd-times">${perDayRows || `<div class="ib-none">Select at least one weekday above.</div>`}</div></div>
      <div class="ib-grid2">
        <label class="ib-field"><span>From date</span><input type="date" id="ib-from" value="${esc(f.start_date)}"></label>
        <label class="ib-field"><span>To date</span><input type="date" id="ib-to" value="${esc(f.end_date)}"></label>
        <label class="ib-field"><span>Playable minutes</span><input type="number" id="ib-playable" min="1" step="5" value="${f.playable_minutes}"></label>
        <label class="ib-field"><span>Turnover minutes</span><input type="number" id="ib-turnover" min="0" step="5" value="${f.turnover_minutes}"></label>
      </div>
      <div class="ib-field"><span>Exclusion dates</span>
        <div class="ib-excl-add"><input type="date" id="ib-excl"><button class="act ghost" data-ib-excl-add>Add</button></div>
        <div class="ib-chips">${exclusionChips}</div></div>
      <div class="dq-actions"><button class="act primary" data-ib-preview>Preview slots</button></div>
    </div>
    ${iceBuilder.preview ? renderIcePreview(iceBuilder.preview) : ""}
  </div>`;
}

function renderIcePreview(pv) {
  if (pv.error) {
    return `<div class="banner warn"><h2>Couldn't preview</h2>
      <p>${esc((pv.error && pv.error.message) || "Check the template inputs and try again.")}</p></div>`;
  }
  const t = pv.totals;
  const hrs = (m) => `${(m / 60).toFixed(m % 60 ? 1 : 0)}h`;
  const access = (pv.venue_access_missing || []);
  const accessWarn = access.length
    ? `<div class="ib-warn">⚠ ${access.length} rink(s) skipped — their venue isn't granted to this Season:
        <strong>${access.map((m) => esc(m.rink_name || m.rink_id)).join(", ")}</strong>.
        <button class="linklike" data-goto="setup">Grant Season participation → Venue access</button>, then preview again.</div>`
    : "";
  const conflictWarn = t.conflict
    ? `<div class="ib-warn">⚠ ${t.conflict} slot(s) overlap existing ice or games — each is listed below with its target, never overwritten.</div>` : "";
  const skips = (pv.skipped_dates || []).length
    ? `<div class="ib-note">Skipped ${pv.skipped_dates.length} exclusion date(s): ${pv.skipped_dates.map((s) => esc(s.date)).join(", ")}.</div>` : "";
  const short = (pv.too_short || []).length
    ? `<div class="ib-note">${pv.too_short.length} day(s) too short for one ${pv.playable_minutes}-min game: ${pv.too_short.map((s) => esc(s.date)).join(", ")}.</div>` : "";
  // DST (#315 review): a spring-forward window whose start/end falls in the
  // nonexistent local hour generates NOTHING for that day — surfaced here as an
  // actionable skip (which boundary, which day), never silent. A fall-back
  // window that resolves an ambiguous boundary is informational (the earlier
  // fold was used); the repeated-hour ROWS it can produce are disambiguated
  // below regardless of whether the WINDOW boundary itself was ambiguous.
  // #277: a generated consecutive pair sits closer than the rink's
  // effective warm-up+resurfacing requirement — those two slots can't BOTH
  // host games; warn per rink, naming the offending pair and its real gap.
  const policyNotes = (pv.policy_notes || []).length
    ? pv.policy_notes.map((n) =>
        `<div class="ib-warn">⚠ ${esc(n.rink_name)}: on ${esc(n.date)} the slot ending ${esc(n.pair_end_local)} and the next starting ${esc(n.pair_next_start_local)} are only ${n.gap_minutes} min apart, but the scheduling policy needs ${n.required_gap_minutes} min of resurfacing + warm-up between games — both cannot host games.</div>`).join("")
    : "";
  const dstSkips = (pv.dst_skipped || []).length
    ? `<div class="ib-warn">⚠ ${pv.dst_skipped.length} window(s) skipped — the local start/end time doesn't exist that day (a spring-forward gap): ${pv.dst_skipped.map((s) => esc(`${s.date} (${s.boundary})`)).join(", ")}. Adjust the window to fall outside the gap, then preview again.</div>`
    : "";
  const dstAmbig = (pv.dst_ambiguous || []).length
    ? `<div class="ib-note">${pv.dst_ambiguous.length} window(s) cross a repeated local hour (fall-back clocks change): ${pv.dst_ambiguous.map((s) => esc(`${s.date} (${s.boundary})`)).join(", ")}. The earlier occurrence is used; any rows below sharing a clock time show their UTC offset to tell them apart.</div>`
    : "";
  const rinkRows = (pv.rinks || []).map((r) =>
    `<div class="ib-rink-row"><span>${esc(r.rink_name)}</span><span>${r.new} new · ${r.duplicate} exist · ${r.conflict} conflict</span></div>`).join("");
  // Every generated row is reviewable before commit (#158 review): new, duplicate
  // AND conflict, across the WHOLE range — no day cap, so a season-long template
  // can't hide later days or exact collisions. The list scrolls (CSS) instead of
  // truncating; the commit stays bound to the full resolved snapshot regardless
  // of what is scrolled into view. Conflicts carry their exact target (the
  // colliding Game id, or the existing slot's type/status).
  const byDate = {};
  (pv.slots || []).forEach((s) => { (byDate[s.date] || (byDate[s.date] = [])).push(s); });
  const days = Object.keys(byDate).sort();
  const lastDay = days.length ? days[days.length - 1] : "";
  const conflictTarget = (s) => s.conflict_has_game
    ? `game ${s.conflict_game_id || "?"}`
    : `${s.conflict_slot_type || "existing"} slot${s.conflict_slot_status ? ` (${s.conflict_slot_status})` : ""}`;
  // A fall-back day's repeated local hour means two distinct UTC slots can read
  // the SAME wall-clock start or end (#315 review) — count each clock reading
  // among the day's rows (start/end separately) so a repeated one is labeled
  // with its UTC offset; a non-repeated reading stays the plain HH:MM it always
  // was. data-ib-start-offset/-end-offset are always present (cheap, and let a
  // reader confirm two same-clock rows are genuinely different instants).
  //
  // A DIFFERENT case (#313 follow-up review): a single row can cross the DST
  // change itself — its OWN start and end sit in different UTC offsets, so the
  // clock-repeat check above never fires (nothing else that day repeats either
  // boundary), yet the plain HH:MM misstates the real duration: a spring-
  // forward 01:00-03:00 row is a real 60-minute slot (not 2h), and a fall-back
  // 01:00-02:00 row can be a real 120-minute slot (not 1h). Qualify BOTH
  // boundaries whenever a row's own start/end offsets differ, regardless of
  // any repeat, and call out the transition explicitly rather than leaving the
  // operator to notice two different offsets on their own.
  const slotSpan = (s, startAmbiguous, endAmbiguous) => {
    const startOffset = ibLocalOffset(s.start_local), endOffset = ibLocalOffset(s.end_local);
    const crossesDst = startOffset !== endOffset;
    const showStart = startAmbiguous || crossesDst;
    const showEnd = endAmbiguous || crossesDst;
    const startLbl = showStart
      ? `${esc(ibLocalTime(s.start_local))} (UTC${esc(startOffset)})` : esc(ibLocalTime(s.start_local));
    const endLbl = showEnd
      ? `${esc(ibLocalTime(s.end_local))} (UTC${esc(endOffset)})` : esc(ibLocalTime(s.end_local));
    const time = `${startLbl}–${endLbl}`;
    const dstNote = crossesDst ? " ⏱ DST change mid-slot — see UTC offsets" : "";
    const clockAttrs = ` data-ib-start-clock="${esc(ibLocalTime(s.start_local))}" data-ib-start-offset="${esc(startOffset)}" data-ib-end-clock="${esc(ibLocalTime(s.end_local))}" data-ib-end-offset="${esc(endOffset)}"${crossesDst ? ' data-ib-dst-cross="1"' : ""}`;
    if (s.status === "conflict") {
      const target = conflictTarget(s);
      return `<span class="ib-slot ib-slot-conflict" data-ib-slot-status="conflict"${clockAttrs}${s.conflict_game_id ? ` data-ib-conflict-game="${esc(s.conflict_game_id)}"` : ""} title="conflicts with ${esc(target)}">${time}${dstNote} · ${esc(s.rink_name)} · ⚠ ${esc(target)}</span>`;
    }
    if (s.status === "duplicate") {
      return `<span class="ib-slot ib-slot-duplicate" data-ib-slot-status="duplicate"${clockAttrs}>${time}${dstNote} · ${esc(s.rink_name)} · already exists</span>`;
    }
    return `<span class="ib-slot ib-slot-new" data-ib-slot-status="new"${clockAttrs}>${time}${dstNote} · ${esc(s.rink_name)}</span>`;
  };
  const slotList = days.map((d, i) => {
    const daySlots = byDate[d];
    const startCounts = {}, endCounts = {};
    daySlots.forEach((s) => {
      const sc = ibLocalTime(s.start_local), ec = ibLocalTime(s.end_local);
      startCounts[sc] = (startCounts[sc] || 0) + 1;
      endCounts[ec] = (endCounts[ec] || 0) + 1;
    });
    const rows = daySlots.map((s) => slotSpan(
      s, startCounts[ibLocalTime(s.start_local)] > 1, endCounts[ibLocalTime(s.end_local)] > 1)).join("");
    return `<div class="ib-day-row" data-ib-day="${esc(d)}"${i === days.length - 1 ? ' data-ib-last-day="1"' : ""}>
      <div class="ib-day-date">${esc(d)}</div>
      <div class="ib-day-slots">${rows}</div></div>`;
  }).join("");
  const listNote = days.length
    ? `<div class="ib-note">All ${days.length} generated day(s) listed (${(pv.slots || []).length} slot(s)) — scroll to review every day and conflict; the last day is ${esc(lastDay)}.</div>`
    : "";
  return `<div class="card ib-preview" data-ib-new="${t.new}" data-ib-duplicate="${t.duplicate}" data-ib-conflict="${t.conflict}" data-ib-access-missing="${(pv.venue_access_missing || []).length}" data-ib-skipped="${(pv.skipped_dates || []).length}" data-ib-dst-skipped="${(pv.dst_skipped || []).length}" data-ib-dst-ambiguous="${(pv.dst_ambiguous || []).length}" data-ib-days="${days.length}" data-ib-slots="${(pv.slots || []).length}" data-ib-last-day-date="${esc(lastDay)}">
    <div class="section-title" style="margin-top:0">Preview — ${t.capacity_games} game slot(s) to create</div>
    <div class="ib-stats">
      <div class="ib-stat"><b>${t.new}</b><span>new</span></div>
      <div class="ib-stat"><b>${t.duplicate}</b><span>already exist</span></div>
      <div class="ib-stat"><b>${t.conflict}</b><span>conflicts</span></div>
      <div class="ib-stat"><b>${hrs(t.playable_minutes)}</b><span>playable</span></div>
      <div class="ib-stat"><b>${hrs(t.reserved_minutes)}</b><span>reserved</span></div>
    </div>
    ${accessWarn}${conflictWarn}${skips}${short}${policyNotes}${dstSkips}${dstAmbig}
    ${rinkRows ? `<div class="ib-rink-rows">${rinkRows}</div>` : ""}
    <div class="ib-slot-list">${slotList || `<div class="empty">No slots generated — adjust the template above.</div>`}</div>
    ${listNote}
    <div class="dq-actions">
      <button class="act success" data-ib-commit ${t.new ? "" : "disabled"}>Create ${t.new} slot(s)</button>
      <button class="act ghost" data-ib-preview>Re-preview</button>
    </div>
  </div>`;
}

// Read the builder form out of the DOM (weekday/rink checkboxes + inputs),
// preserving exclusion_dates which are managed via chips on the state object.
function readIceBuilderForm(c) {
  const val = (sel) => { const el = c.querySelector(sel); return el ? el.value : ""; };
  const num = (sel, dflt) => { const v = parseInt(val(sel), 10); return isNaN(v) ? dflt : v; };
  const weekdays = Array.from(c.querySelectorAll(".ib-weekday:checked")).map((e) => +e.value);
  // Preserve each weekday's previously-entered window across re-renders, then
  // overlay whatever the currently-rendered per-day time inputs hold. A day
  // that was just checked has no row yet and falls back to the default block.
  const winByDay = {};
  ((iceBuilder.form && iceBuilder.form.windows) || []).forEach((w) => {
    winByDay[w.weekday] = { start_local: w.start_local, end_local: w.end_local };
  });
  c.querySelectorAll(".ib-wd-start").forEach((el) => {
    const wd = +el.dataset.weekday;
    (winByDay[wd] = winByDay[wd] || {}).start_local = el.value || "18:00";
  });
  c.querySelectorAll(".ib-wd-end").forEach((el) => {
    const wd = +el.dataset.weekday;
    (winByDay[wd] = winByDay[wd] || {}).end_local = el.value || "22:00";
  });
  const windows = weekdays.slice().sort((a, b) => a - b).map((wd) => ({
    weekday: wd,
    start_local: (winByDay[wd] && winByDay[wd].start_local) || "18:00",
    end_local: (winByDay[wd] && winByDay[wd].end_local) || "22:00",
  }));
  return {
    ...iceBuilder.form,
    season_id: val("#ib-season"),
    rink_ids: Array.from(c.querySelectorAll(".ib-rink:checked")).map((e) => e.value),
    weekdays,
    windows,
    start_date: val("#ib-from"),
    end_date: val("#ib-to"),
    playable_minutes: num("#ib-playable", 60),
    turnover_minutes: num("#ib-turnover", 15),
  };
}

// The venue of the SLOT being scheduled, not just the league's first venue
// (#118 Phase 7) — a league with more than one venue was showing the wrong
// arena name in the wizard's review step regardless of which rink/slot the
// operator actually picked.
function slotVenueName(ov, slot) {
  const rink = ov.rinks.find((r) => r.id === slot.rink_id);
  return (rink && rink.venue_name) || "";
}
function renderWizard(ov) {
  const slot = ov.ice_slots.find((s) => s.id === wizard.slot_id);
  if (!slot) { wizard = null; return renderCalendar(ov); }
  // League (required) + Division (optional) — v2 canonical game scope (#233
  // B2c): `ov.levels` is the grouping League list (frozen internal key
  // "level" = canonical League); `ov.divisions[].level_id` is each
  // division's owning League. The Division picker offers "No division" and
  // is scoped to the chosen League — never a flat cross-league list.
  //
  // The League is NEVER implicitly picked when more than one exists (#233
  // B2c review): silently defaulting to the first risks writing the game to
  // the wrong competitive grouping (#212). Auto-selecting is safe only when
  // there is exactly ONE unambiguous League to choose from — with zero or
  // several, the select starts on a disabled "Select league…" placeholder
  // and every downstream control (Division/Home/Away/Create) stays disabled
  // until the operator picks one explicitly.
  const leagues = ov.levels || [];
  const seasons = ov.seasons || [];
  // #283 Slice D: an Exhibition (friendly) game may cross League lines. It has
  // no League/Division scope and never counts toward standings — it is
  // Season-scoped, and its two teams are any active participants in that
  // Season. A regular game keeps the League → Division cascade below.
  const isExhibition = !!wizard.exhibition;
  if (isExhibition && !wizard.season_id && seasons.length === 1) wizard.season_id = seasons[0].id;
  if (!isExhibition && !wizard.league_id && leagues.length === 1) wizard.league_id = leagues[0].id;
  const leagueChosen = !!wizard.league_id;
  const seasonChosen = !!wizard.season_id;
  const ready = isExhibition ? seasonChosen : leagueChosen;
  const divs = (!isExhibition && leagueChosen) ? ov.divisions.filter((d) => d.level_id === wizard.league_id) : [];
  if (wizard.division_id && !divs.find((d) => d.id === wizard.division_id)) wizard.division_id = "";
  // Teams eligible for this game are those with an ACTIVE SeasonTeamRegistration
  // in the chosen League (#180, #233 B2c) — and, when a Division is also
  // chosen, in that exact Division too (the server requires an exact match
  // once a Division is given). Never the legacy Team.division_id. This
  // mirrors the server's registration-based game-creation guard, so the
  // picker only offers teams the server will accept. Empty until a League is
  // explicitly chosen.
  // Exhibition: any team registered in the chosen Season (any League). Regular:
  // teams registered in the chosen League (and Division, when one is chosen).
  const registeredIds = isExhibition
    ? new Set((ov.registrations || [])
        .filter((r) => r.season_id === wizard.season_id).map((r) => r.team_id))
    : (leagueChosen ? new Set((ov.registrations || [])
        .filter((r) => r.league_id === wizard.league_id
          && (!wizard.division_id || r.division_id === wizard.division_id))
        .map((r) => r.team_id)) : new Set());
  const teams = ov.teams.filter((t) => registeredIds.has(t.id));
  if (!teams.find((t) => t.id === wizard.home_id)) wizard.home_id = teams[0] ? teams[0].id : "";
  const awayTeams = teams.filter((t) => t.id !== wizard.home_id);
  if (!awayTeams.find((t) => t.id === wizard.away_id)) wizard.away_id = awayTeams[0] ? awayTeams[0].id : "";

  const bothChosen = wizard.home_id && wizard.away_id;
  const distinct = wizard.home_id && wizard.away_id && wizard.home_id !== wizard.away_id;
  const ok = ready && bothChosen && distinct && slot.status === "available";
  const v = (good, t) => `<div class="valid ${good ? "ok" : "bad"}">${good ? "✓" : "✕"} ${t}</div>`;
  const dis = ready ? "" : "disabled";
  return `
    <div class="wizard">
      <h3>Schedule Game</h3>
      <div class="step">1 · Competition</div>
      <label class="wiz-exhibition"><input type="checkbox" id="w-exhibition"${isExhibition ? " checked" : ""}> Exhibition (friendly — may cross leagues, never counts toward standings)</label>
      <div style="height:8px"></div>
      ${isExhibition
        ? `<select id="w-season" aria-label="Season">${
            seasonChosen ? "" : `<option value="" disabled selected>Select season…</option>`}${
            seasons.map((s) => opt(s.id, s.name, s.id === wizard.season_id)).join("")}</select>`
        : `<select id="w-league" aria-label="League">${
            leagueChosen ? "" : `<option value="" disabled selected>Select league…</option>`}${
            leagues.map((lv) => opt(lv.id, lv.name, lv.id === wizard.league_id)).join("")}</select>
          <div style="height:8px"></div>
          <select id="w-div" aria-label="Division" ${dis}><option value="">No division</option>${
            divs.map((d) => opt(d.id, d.name, d.id === wizard.division_id)).join("")}</select>`}
      <div class="step">2 · Teams</div>
      <select id="w-home" aria-label="Home team" ${dis}>${teams.map((t) => opt(t.id, t.name, t.id === wizard.home_id)).join("")}</select>
      <div style="height:8px"></div>
      <select id="w-away" aria-label="Away team" ${dis}>${awayTeams.map((t) => opt(t.id, t.name, t.id === wizard.away_id)).join("") || opt("", "—")}</select>
      <div class="step">3 · Ice</div>
      <div class="li"><span class="li-time">${fmt(slot.start_time)}–${fmt(slot.end_time)}</span>
        <div class="li-main"><div class="li-title">${esc(slot.rink_name)}</div>
          <div class="li-sub">${esc(slotVenueName(ov, slot))}</div></div></div>
      <div class="step">4 · Validation</div>
      ${v(ready, isExhibition ? "Season selected" : "League selected")}
      ${v(!!teams.length, isExhibition ? "Teams registered this season" : "Same league")}
      ${v(distinct, "Home and away are different teams")}
      ${v(slot.status === "available", "Ice slot is available")}
      ${v(true, "Public-safe junior fixture (no PII)")}
      <div class="step">5 · Review</div>
      <div class="review">
        ${isExhibition
          ? `<div class="kv"><span class="k">Type</span><span class="v">Exhibition (friendly)</span></div>
             <div class="kv"><span class="k">Season</span><span class="v">${esc((seasons.find((s) => s.id === wizard.season_id) || {}).name || "")}</span></div>`
          : `<div class="kv"><span class="k">League</span><span class="v">${esc((leagues.find((lv) => lv.id === wizard.league_id) || {}).name || "")}</span></div>
             <div class="kv"><span class="k">Division</span><span class="v">${esc((divs.find((d) => d.id === wizard.division_id) || {}).name || "No division")}</span></div>`}
        <div class="kv"><span class="k">Home</span><span class="v">${esc((teams.find((t) => t.id === wizard.home_id) || {}).name || "—")}</span></div>
        <div class="kv"><span class="k">Away</span><span class="v">${esc((awayTeams.find((t) => t.id === wizard.away_id) || {}).name || "—")}</span></div>
        <div class="kv"><span class="k">Venue · Rink</span><span class="v">${esc(slotVenueName(ov, slot))} · ${esc(slot.rink_name)}</span></div>
        <div class="kv"><span class="k">Time</span><span class="v">${esc(fmtDateTime(slot.start_time))}–${fmt(slot.end_time)}</span></div>
      </div>
      <div class="actions">
        <button class="act ghost" data-wizcancel="1">Cancel</button>
        <button class="act primary" data-wizcreate="1" ${ok ? "" : "disabled"}>Create Draft Game</button>
      </div>
    </div>`;
}

/* ---------- Games + operations checklist ---------- */
// One compact Games-list row: matchup, time, rink, and a triage status badge,
// with the full game-operations checklist + actions revealed on expand (#152).
// The head is a <button> (the whole row toggles); the detail is a sibling, so
// its own Open-Roster/Publish buttons are never nested inside a button.
function gamesRow(g) {
  const t = gameTriage(g);
  const badgeCls = { final: "gray", needs: "blocked", ready: "available" }[t.badge] || "gray";
  const expanded = gamesExpanded.has(g.game_id);
  const head = `<button class="li li-btn games-row" data-games-toggle="${esc(g.game_id)}" aria-expanded="${expanded}">
    <span class="li-time">${fmt(g.start_time)}</span>
    <div class="li-main"><div class="li-title">${esc(g.home_team_name)} vs ${esc(g.away_team_name || "TBD")}</div>
      <div class="li-sub">${esc(g.division_name || "")} · ${esc(g.rink_name || "")}</div></div>
    <span class="pill ${badgeCls}">${esc(t.label)}</span>
    <span class="games-caret" aria-hidden="true">${expanded ? "▲" : "▼"}</span></button>`;
  if (!expanded) return head;
  const confirmed = g.roster_status === "roster_confirmed" || g.roster_status === "locked";
  const ck = (ok, lbl, meta) => `<div class="check ${ok ? "ok" : "todo"}"><span class="ic">${ok ? "✓" : "○"}</span>
    <span class="lbl">${lbl}</span>${meta ? `<span class="meta">${meta}</span>` : ""}</div>`;
  // #277: the schedule review shows the same derived reserved span as the
  // calendar and the scheduler's draft rows (one backend derivation).
  const rsv = g.reserved
    ? `<div class="slot-reserved">reserved ${fmt(g.reserved.reserved_start_time)}–${fmt(g.reserved.reserved_end_time)} (+${g.reserved.warmup_minutes}m warm-up, +${g.reserved.resurfacing_minutes}m resurfacing)</div>`
    : "";
  const detail = `<div class="games-detail">
    ${ck(true, "Ice slot allocated")}${rsv}
    ${ck(confirmed, "Roster", prettyStatus(g.roster_status))}
    ${ck((g.officials_assigned || 0) > 0 && (g.officials_accepted || 0) === g.officials_assigned, "Officials",
         g.officials_assigned ? `${g.officials_accepted}/${g.officials_assigned} accepted` : "None assigned")}
    ${ck(false, "Locker rooms", "Follow-up")}
    ${ck(g.result_status === "final", "Result",
         g.result_status === "final" ? "Final" : g.result_status === "draft" ? "Draft — approve to finalize" : "Not entered")}
    ${ck(g.published, "Public fixture", g.published ? "Published" : "Draft — not public")}
    <div class="actions">
      <button class="act primary" data-openroster="${esc(g.game_id)}">Open Roster</button>
      ${g.published ? "" : `<button class="act success" data-publish="${esc(g.game_id)}">Publish</button>`}
      ${!g.cancelled && hasPerm("manage_schedule")
        ? `<button class="icon-btn danger" data-game-cancel="${esc(g.game_id)}"
            data-game-name="${esc(g.home_team_name + " vs " + (g.away_team_name || "TBD"))}"
            title="Cancel game" aria-label="Cancel game ${esc(g.home_team_name + " vs " + (g.away_team_name || "TBD"))}">${ICONS.circleX}</button>` : ""}
    </div>
    ${g.cancelled ? `<div class="muted" style="padding:6px 2px">This game is cancelled; its fixture and result history are preserved.</div>` : ""}
  </div>`;
  return head + detail;
}

function renderGames(ov) {
  const intro = pageIntro("Every scheduled game and its game-day readiness — ice, roster, officials, and result.");
  const all = ov.schedule || [];
  if (!all.length) return `${intro}<div class="empty">No games scheduled yet. Use the Calendar to schedule one.</div>`;

  // Filters (#152): a single endless card-per-game scroll doesn't survive a
  // real 40-games-a-weekend league, so filter + group + collapse. Options are
  // derived from the data actually present; status uses gameTriage's own label
  // so the filter and the row badge can never disagree.
  const f = gamesFilter;
  const STATUSES = ["Needs staff", "Roster open", "Ready", "Final"];
  const rinkNames = [...new Set(all.map((g) => g.rink_name).filter(Boolean))].sort();
  const shown = all.filter((g) => {
    if (f.division !== "all" && g.division_id !== f.division) return false;
    if (f.team !== "all" && g.home_team_id !== f.team && g.away_team_id !== f.team) return false;
    if (f.rink !== "all" && g.rink_name !== f.rink) return false;
    if (f.status !== "all" && gameTriage(g).label !== f.status) return false;
    const day = dayOf(g.start_time);
    if (f.from && day && day < f.from) return false;   // date-less games ignore the range
    if (f.to && day && day > f.to) return false;
    return true;
  });

  const opt = (v, label, sel) => `<option value="${esc(v)}" ${v === sel ? "selected" : ""}>${esc(label)}</option>`;
  const active = f.division !== "all" || f.team !== "all" || f.rink !== "all"
    || f.status !== "all" || f.from || f.to;
  const filterBlock = `<div class="games-filters">
    <select id="games-f-div"><option value="all">All divisions</option>${(ov.divisions || []).map((d) => opt(d.id, d.name, f.division)).join("")}</select>
    <select id="games-f-team"><option value="all">All teams</option>${(ov.teams || []).map((t) => opt(t.id, t.name, f.team)).join("")}</select>
    <select id="games-f-rink"><option value="all">All rinks</option>${rinkNames.map((r) => opt(r, r, f.rink)).join("")}</select>
    <select id="games-f-status"><option value="all">All statuses</option>${STATUSES.map((s) => opt(s, s, f.status)).join("")}</select>
    <input type="date" id="games-f-from" value="${esc(f.from)}" aria-label="Games from date">
    <input type="date" id="games-f-to" value="${esc(f.to)}" aria-label="Games to date">
    ${active ? `<button class="act ghost" id="games-f-clear">Clear filters</button>` : ""}
  </div>`;

  let body;
  if (!shown.length) {
    body = `<div class="card"><div class="empty">No games match these filters.</div></div>`;
  } else {
    // ov.schedule isn't guaranteed sorted, so sort before grouping by day.
    const sorted = shown.slice().sort((a, b) =>
      (a.start_time || "") < (b.start_time || "") ? -1 : (a.start_time || "") > (b.start_time || "") ? 1 : 0);
    const groups = [];
    let cur = null;
    sorted.forEach((g) => {
      const day = dayOf(g.start_time);
      if (!cur || cur.day !== day) { cur = { day, items: [] }; groups.push(cur); }
      cur.items.push(g);
    });
    body = groups.map((grp) => {
      const heading = grp.day ? fmtDate(grp.day) : "Date to be confirmed";
      return `<div class="section-title">${esc(heading)}</div>
        <div class="card">${grp.items.map(gamesRow).join("")}</div>`;
    }).join("");
  }
  return `${intro}${filterBlock}
    <div class="games-count">Showing ${shown.length} of ${all.length} game${all.length === 1 ? "" : "s"}</div>
    ${body}`;
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
    <span class="pill ${AVAIL_PILL[p.status] || "gray"}">${AVAIL_LABEL[p.status] || esc(p.status)}</span>
  </div>`).join("");
  const canRemind = hasPerm("manage_roster");
  return `<div class="card">
    <div class="section-title" style="margin-top:0">Availability
      ${canRemind && c.no_response ? `<button class="act ghost" data-avail-remind>Remind ${c.no_response} unresponded</button>` : ""}</div>
    <div class="seg-group">${["all", "available", "unavailable", "maybe", "no_response"].map(chip).join("")}</div>
    <div class="row-list">${rows || '<p class="muted">No players in this filter.</p>'}</div>
  </div>`;
}

// The games a signed-in user may open a roster for — mirrors the backend
// private-game read gate at the role level (scope.py): operators see all,
// a coach/player only their own team's. An official/other isn't cheaply
// pre-filterable here, so they get the full list and an out-of-scope pick
// falls through to the existing "Restricted" guard (#154).
function accessibleGames(ov) {
  const all = ov.schedule || [];
  const u = currentUser;
  if (!u || u.role === "league_admin" || u.role === "arena_manager") return all;
  const tid = (u.scope || {}).team_id;
  if ((u.role === "coach" || u.role === "player") && tid)
    return all.filter((g) => g.home_team_id === tid || g.away_team_id === tid);
  return all;
}

// A visible game picker + selected-game context at the top of the Roster
// screen (#154): the editor used to operate silently on "the current game"
// (whatever was clicked last, defaulting to the first in the schedule), so a
// coach with several games could edit the wrong one without noticing. Per-side
// roster status already shows in the lineup-switch tabs below, so this header
// carries the identifying context only — matchup, date/time, rink, division.
function rosterGamePicker(ov) {
  const g = (ov.schedule || []).find((x) => x.game_id === currentGame);
  if (!g) return "";
  const games = accessibleGames(ov);
  const opt = (x) => `<option value="${esc(x.game_id)}" ${x.game_id === currentGame ? "selected" : ""}>${
    esc(x.home_team_name)} vs ${esc(x.away_team_name || "TBD")} · ${esc(fmtDateTime(x.start_time))}</option>`;
  const picker = games.length > 1
    ? `<select id="roster-game" aria-label="Select which game's roster to edit">${games.map(opt).join("")}</select>`
    : "";
  return `<div class="roster-game-head">
    <div class="rg-ctx">
      <div class="rg-title">${esc(g.home_team_name)} vs ${esc(g.away_team_name || "TBD")}</div>
      <div class="rg-sub">${esc(fmtDateTime(g.start_time))}${g.rink_name ? " · " + esc(g.rink_name) : ""}${g.division_name ? " · " + esc(g.division_name) : ""}</div>
    </div>
    ${picker}
  </div>`;
}

function renderRoster(lineups, ov) {
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
  return `${rosterGamePicker(ov)}<div class="lineup-switch">${tab("home", "🏠", "Home")}${tab("away", "✈️", "Away")}</div>
    <div class="segmented">
      <button class="seg ${gameView === "coach" ? "active" : ""}" data-view="coach">Coach</button>
      <button class="seg ${gameView === "player" ? "active" : ""}" data-view="player">Player</button>
    </div><div style="padding-top:8px">${gameView === "coach" ? coachBody(side, ov) : playerBody(side)}</div>
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
      <span class="badge ${GS_STATUS_BADGE[bannerClass(s.status)] || "gray"}">${prettyStatus(s.status)}</span>
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
    <span class="pill ${a.status === "unavailable" ? "blocked" : "available"}">${a.status === "unavailable" ? "Unavailable" : "Available"}</span>
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
      ${renderAvailability()}${renderCalendarFeed()}`;
  }
  // Pending-response count (#146): "upcoming assignments" already told an
  // official how many games they're on; it didn't say how many still need
  // an accept/decline from them specifically.
  const pending = rows.filter((a) => a.status === "proposed" && !a.cancelled).length;
  const roleLabel = { referee: "👨‍⚖️ Referee", linesperson: "🚩 Linesperson", scorekeeper: "📝 Scorekeeper" };
  const badge = (st) => st === "accepted" ? `<span class="badge green">Accepted</span>`
    : st === "declined" ? `<span class="badge red">Declined</span>`
    : `<span class="badge orange">Proposed</span>`;
  const cards = rows.map((a) => {
    const matchup = `${esc(a.home_team_name)} vs ${esc(a.away_team_name || "TBD")}`;
    const where = `${esc(fmtDateTime(a.start_time))}${a.rink ? " · " + esc(a.rink) : ""}${a.venue_name ? " · " + esc(a.venue_name) : ""}`;
    const actions = a.status === "proposed" && !a.cancelled
      ? `<button class="act success" data-accept="${esc(a.assignment_id)}">Accept</button>
         <button class="act danger" data-decline="${esc(a.assignment_id)}">Decline</button>` : "";
    const cancelled = a.cancelled ? `<span class="badge red">Game cancelled</span>` : "";
    return `<div class="inbox-card">
      <div class="inbox-top"><span class="inbox-role">${roleLabel[a.role] || esc(a.role)}</span>${badge(a.status)}${cancelled}</div>
      <div class="inbox-match">${matchup}</div>
      <div class="inbox-where">${where}</div>
      <div class="inbox-actions">${actions}
        <button class="act ghost" data-open-sheet="${esc(a.game_id)}">Open game sheet</button></div>
    </div>`;
  }).join("");
  const pendingBadge = pending
    ? `<span class="badge orange" style="margin-left:8px">${pending} pending your response</span>` : "";
  return `<div class="section-title">Your upcoming assignments (${rows.length})${pendingBadge}</div>
    <div class="inbox-list">${cards}</div>${renderAvailability()}${renderCalendarFeed()}`;
}

/* ---------- Player Home (#107) ---------- */
const PH_ATTENDANCE = {
  not_responded: ["gray", "Not Responded"], confirmed: ["green", "Confirmed"],
  checked_out: ["red", "Checked Out"], pending: ["orange", "Pending"],
};
const PH_TEAM_STATUS = {
  full: ["green", "Full"], short: ["red", "Short"],
  sub_search: ["orange", "Sub Search"], not_responded: ["gray", "Not Responded"],
};
function phBadge(map, key) {
  const [cls, label] = map[key] || ["gray", key];
  return `<span class="badge ${cls}">${esc(label)}</span>`;
}
// Status feed (#107 doc §6.6): small alert rows derived from the same data
// the cards show — roster state, own attendance, and open opportunities.
function phStatusFeed(ng, oppCount) {
  const rows = [];
  if (ng) {
    if (ng.attendance_status === "not_responded")
      rows.push(["orange", "You have not responded to your next game."]);
    if (ng.attendance_status === "checked_out")
      rows.push(["red", "You checked out of your next game."]);
    if (ng.team_status === "full")
      rows.push(["green", "Your team's roster is full."]);
    if (ng.team_status === "sub_search")
      rows.push(["orange", "Substitute search is active for your next game."]);
  }
  if (oppCount) rows.push(["blue", "New substitute opportunity available."]);
  if (!rows.length) return "";
  return `<div class="card"><div class="section-title" style="margin-top:0">Status</div>
    ${rows.map(([cls, msg]) => `<div class="li"><span class="badge ${cls}" aria-hidden="true">●</span>
      <div class="li-main"><div class="li-sub">${esc(msg)}</div></div></div>`).join("")}</div>`;
}
// Substitute opportunity detail (#110): a scoped detail surface reached from
// the Home opportunities list via "View Opportunity". Shows the game context
// and either an Accept/Withdraw action or the plain reason the player can't
// respond; the response reuses the existing enroll/withdraw workflow.
function renderOppDetail(detail) {
  if (!detail || detail.error) {
    return `<div class="banner alert"><h2>Opportunity unavailable</h2>
      <p>${esc((detail && detail.error && detail.error.message) || "This opportunity could not be loaded.")}</p></div>
      <div class="actions"><button class="act ghost" data-opp-back>Back to Home</button></div>`;
  }
  const rows = [
    ["Team", detail.team_name],
    ["Opponent", detail.opponent_name || "TBD"],
    ["When", fmtDateTime(detail.start_time)],
    ["Venue", [detail.venue_name, detail.rink_name].filter(Boolean).join(" · ") || "—"],
    ["Position needed", detail.position_needed],
  ].map(([k, v]) => `<div class="li"><div class="li-main">
      <div class="li-sub">${esc(k)}</div><div class="li-title">${esc(v)}</div></div></div>`).join("")
    + `<div class="li"><div class="li-main"><div class="li-sub">Team status</div></div>
        ${phBadge(PH_TEAM_STATUS, detail.team_status)}</div>`;
  const offered = detail.enrollment_status === "offered";
  let note = "", actions = "";
  if (offered) {
    // A coach has offered this player the slot (#112) — respond to the offer.
    const btns = [];
    if (detail.can_accept_offer) btns.push(`<button class="act success" data-opp-accept-offer="${esc(detail.game_id)}">Accept Offer</button>`);
    if (detail.can_decline_offer) btns.push(`<button class="act danger" data-opp-decline-offer="${esc(detail.game_id)}">Decline Offer</button>`);
    actions = btns.join("");
    if (detail.blocked_reason)
      note = `<div class="banner ${btns.length ? "warn" : "neutral"}"><p>${esc(detail.blocked_reason)}</p></div>`;
  } else if (detail.can_withdraw) {
    actions = `<button class="act danger" data-opp-withdraw="${esc(detail.game_id)}">Withdraw</button>`;
  } else if (detail.can_accept) {
    actions = `<button class="act success" data-opp-accept="${esc(detail.game_id)}">Accept — Enroll as Sub</button>`;
  } else {
    note = `<div class="banner neutral"><p>${esc(detail.blocked_reason || "You can't respond to this opportunity right now.")}</p></div>`;
  }
  return `<div class="section-title" style="margin-top:0">${offered ? "Substitute Offer" : "Substitute Opportunity"}</div>
    ${note}
    <div class="card">${rows}</div>
    <div class="actions">${actions}
      <button class="act ghost" data-opp-back>Back to Home</button>
    </div>`;
}

function renderPlayerHome(playerHome) {
  // An opportunity detail is open — show it in place of the Home dashboard.
  // Checked before the player_id guard: while the detail is open the
  // player-home payload is intentionally not re-fetched (render() skips it).
  if (oppDetailGame) return renderOppDetail(oppDetail);
  if (!playerHome || !playerHome.player_id) {
    return `<div class="empty">Sign in as a <strong>Player</strong> to see your next game, attendance, and substitute opportunities.</div>`;
  }
  const ng = playerHome.next_game;
  const welcome = `<div class="section-title" style="margin-top:0">Hi, ${esc(playerHome.player_name || "there")}</div>
    <p class="muted">Ready for your next game?</p>`;
  // "Tonight" + "Team Status" summary cards (#107 doc §6.3), reusing the
  // dashboard's stat-tile classes (responsive stacking comes with them).
  const summary = `<div class="dash-stats">
      <div class="dash-stat"><div class="ds-label">Tonight</div>
        <div class="ds-n">${playerHome.today_count || 0}</div>
        <div class="ds-sub">game${playerHome.today_count === 1 ? "" : "s"} today</div></div>
      <div class="dash-stat"><div class="ds-label">Team Status</div>
        <div class="ds-n">${ng ? phBadge(PH_TEAM_STATUS, ng.team_status)
          : `<span class="badge gray">No game</span>`}</div></div>
    </div>`;
  let nextGameBlock;
  if (!ng) {
    nextGameBlock = `<div class="banner neutral"><h2>No upcoming game</h2>
      <p>You have no scheduled games right now.</p></div>
      <div class="actions"><button class="act ghost" data-goto="public">View Schedule</button></div>`;
  } else if (checkoutConfirm && checkoutConfirm.game_id === ng.game_id) {
    nextGameBlock = `<div class="banner warn"><h2>Confirm checkout</h2>
      <p>Are you sure you can't play? ${esc(ng.team_name)} vs ${esc(ng.opponent_name || "TBD")} — ${esc(fmtDateTime(ng.start_time))}</p></div>
      <div class="actions">
        <button class="act danger" data-ph-confirm-checkout>Confirm Can't Play</button>
        <button class="act ghost" data-ph-cancel-checkout>Stay In</button>
      </div>`;
  } else {
    const confirmed = ng.attendance_status === "confirmed";
    nextGameBlock = `<div class="li">
        <div class="li-main"><div class="li-title">${esc(ng.team_name)} vs ${esc(ng.opponent_name || "TBD")}</div>
          <div class="li-sub">${esc(fmtDateTime(ng.start_time))}
            ${ng.venue_name ? " · " + esc(ng.venue_name) : ""}${ng.rink_name ? " · " + esc(ng.rink_name) : ""}</div></div>
      </div>
      <div class="li">${phBadge(PH_ATTENDANCE, ng.attendance_status)}${phBadge(PH_TEAM_STATUS, ng.team_status)}</div>
      <div class="actions">
        <button class="act success" data-ph-confirm ${confirmed ? "disabled" : ""}>${confirmed ? "You're In ✓" : "I'm In"}</button>
        <button class="act danger" data-ph-backout>Can't Play</button>
        <button class="act ghost" data-open-roster="${esc(ng.game_id)}">View Roster</button>
      </div>`;
  }
  // A row for a substitute opportunity/offer, with its call-to-action button.
  const subRow = (o, label) => `<div class="li">
      <span class="li-time">${fmt(o.start_time)}</span>
      <div class="li-main"><div class="li-title">${esc(o.team_name)} vs ${esc(o.opponent_name || "TBD")}</div>
        <div class="li-sub">${esc(fmtDateTime(o.start_time))}
          ${o.rink_name ? " · " + esc(o.rink_name) : ""} · needs ${esc(o.position_needed)}</div></div>
      <button class="act primary" data-ph-view-opp="${esc(o.game_id)}">${label}</button>
    </div>`;
  // Slots a coach has OFFERED this player (#112) — surfaced first, since they
  // are time-sensitive and need an explicit accept/decline.
  const offers = playerHome.substitute_offers || [];
  const offersCard = offers.length ? `<div class="card">
      <div class="section-title" style="margin-top:0">Substitute Offers (${offers.length})</div>
      ${offers.map((o) => subRow(o, "View Offer")).join("")}
    </div>` : "";
  const opps = playerHome.substitute_opportunities || [];
  const shown = opps.slice(0, 3);  // up to 3 on Home (#107 §17)
  const oppRows = shown.map((o) => subRow(o, "View Opportunity")).join("")
    + (opps.length > 3 ? `<div class="li"><div class="li-main">
        <div class="li-sub">+ ${opps.length - 3} more opportunit${opps.length - 3 === 1 ? "y" : "ies"}</div></div></div>` : "");
  return `${welcome}${summary}
    <div class="card">
      <div class="section-title" style="margin-top:0">Next Game</div>
      ${nextGameBlock}
    </div>
    ${offersCard}
    <div class="card">
      <div class="section-title" style="margin-top:0">Substitute Opportunities (${opps.length})</div>
      ${oppRows || '<div class="empty">No open opportunities right now.</div>'}
    </div>
    ${phStatusFeed(ng, opps.length)}
    <button class="row-btn" data-goto="notifications">
      <span class="row-main">Notifications</span>
      <span class="row-sub">${playerHome.unread_notifications} unread</span>
    </button>`;
}

/* ---------- Guardian linked-junior surface (#26) ---------- */
// A guardian responds ON BEHALF OF each verified junior. Every card reuses the
// same shape as the player's own Home (next game + attendance, offers,
// opportunities) but its action buttons carry the junior's player_id so the
// server can record the guardian as actor and the junior as subject. No
// guardian PII is ever rendered — the payload is entirely player-scoped.
function renderGuardianHome(gh) {
  // A junior's opportunity detail is open — show it in place of the list.
  if (gOpp) return renderGuardianOppDetail(gOppDetail);
  if (!gh || !gh.juniors) {
    return `<div class="empty">Sign in as a <strong>Guardian</strong> to respond for your linked players.</div>`;
  }
  if (!gh.juniors.length) {
    return `<div class="empty">No linked players yet. A league operator links a junior player to your guardian account before you can respond for them.</div>`;
  }
  const header = `<div class="section-title" style="margin-top:0">My Players</div>
    <p class="muted">Respond to games and substitute requests for your linked players.</p>`;
  return `${header}${gh.juniors.map(renderJuniorCard).join("")}`;
}

function renderJuniorCard(j) {
  const jid = j.player_id;
  const ng = j.next_game;
  let nextGameBlock;
  if (!ng) {
    nextGameBlock = `<div class="banner neutral"><h2>No upcoming game</h2>
      <p>${esc(j.player_name)} has no scheduled games right now.</p></div>`;
  } else if (gCheckout && gCheckout.jid === jid && gCheckout.game_id === ng.game_id) {
    nextGameBlock = `<div class="banner warn"><h2>Confirm checkout</h2>
      <p>Confirm ${esc(j.player_name)} can't play? ${esc(ng.team_name)} vs ${esc(ng.opponent_name || "TBD")} — ${esc(fmtDateTime(ng.start_time))}</p></div>
      <div class="actions">
        <button class="act danger" data-g-confirm-checkout="${esc(jid)}">Confirm Can't Play</button>
        <button class="act ghost" data-g-cancel-checkout="${esc(jid)}">Stay In</button>
      </div>`;
  } else {
    const confirmed = ng.attendance_status === "confirmed";
    nextGameBlock = `<div class="li">
        <div class="li-main"><div class="li-title">${esc(ng.team_name)} vs ${esc(ng.opponent_name || "TBD")}</div>
          <div class="li-sub">${esc(fmtDateTime(ng.start_time))}
            ${ng.venue_name ? " · " + esc(ng.venue_name) : ""}${ng.rink_name ? " · " + esc(ng.rink_name) : ""}</div></div>
      </div>
      <div class="li">${phBadge(PH_ATTENDANCE, ng.attendance_status)}${phBadge(PH_TEAM_STATUS, ng.team_status)}</div>
      <div class="actions">
        <button class="act success" data-g-confirm="${esc(jid)}" ${confirmed ? "disabled" : ""}>${confirmed ? "In ✓" : "I'm In"}</button>
        <button class="act danger" data-g-backout="${esc(jid)}">Can't Play</button>
      </div>`;
  }
  // Offer/opportunity row with a per-junior call-to-action. `verb` chooses the
  // wiring: offers get inline Accept/Decline, opportunities get "View".
  const subRow = (o, kind) => {
    let cta;
    if (kind === "offer") {
      cta = `<button class="act success" data-g-accept-offer="${esc(jid)}|${esc(o.game_id)}">Accept</button>
        <button class="act danger" data-g-decline-offer="${esc(jid)}|${esc(o.game_id)}">Decline</button>`;
    } else {
      cta = `<button class="act primary" data-g-view-opp="${esc(jid)}|${esc(o.game_id)}">View</button>`;
    }
    return `<div class="li">
      <span class="li-time">${fmt(o.start_time)}</span>
      <div class="li-main"><div class="li-title">${esc(o.team_name)} vs ${esc(o.opponent_name || "TBD")}</div>
        <div class="li-sub">${esc(fmtDateTime(o.start_time))}
          ${o.rink_name ? " · " + esc(o.rink_name) : ""} · needs ${esc(o.position_needed)}</div></div>
      <div class="actions" style="margin:0">${cta}</div>
    </div>`;
  };
  const offers = j.substitute_offers || [];
  const offersCard = offers.length ? `<div class="section-title">Substitute Offers (${offers.length})</div>
    ${offers.map((o) => subRow(o, "offer")).join("")}` : "";
  const opps = j.substitute_opportunities || [];
  const shown = opps.slice(0, 3);
  const oppRows = shown.map((o) => subRow(o, "opp")).join("")
    + (opps.length > 3 ? `<div class="li"><div class="li-main">
        <div class="li-sub">+ ${opps.length - 3} more opportunit${opps.length - 3 === 1 ? "y" : "ies"}</div></div></div>` : "");
  return `<div class="card">
      <div class="section-title" style="margin-top:0">${esc(j.player_name)}</div>
      ${nextGameBlock}
      ${offersCard}
      ${opps.length ? `<div class="section-title">Substitute Opportunities (${opps.length})</div>${oppRows}` : ""}
    </div>`;
}

// The junior's opportunity detail (#26), reached via "View" on a guardian
// card. Offers can be accepted/declined here; an open opportunity is shown
// read-only (guardian self-enrolment is out of this slice's scope).
function renderGuardianOppDetail(detail) {
  const jid = gOpp ? gOpp.jid : "";
  if (!detail || detail.error) {
    return `<div class="banner alert"><h2>Opportunity unavailable</h2>
      <p>${esc((detail && detail.error && detail.error.message) || "This opportunity could not be loaded.")}</p></div>
      <div class="actions"><button class="act ghost" data-g-opp-back>Back</button></div>`;
  }
  const rows = [
    ["Team", detail.team_name],
    ["Opponent", detail.opponent_name || "TBD"],
    ["When", fmtDateTime(detail.start_time)],
    ["Venue", [detail.venue_name, detail.rink_name].filter(Boolean).join(" · ") || "—"],
    ["Position needed", detail.position_needed],
  ].map(([k, v]) => `<div class="li"><div class="li-main">
      <div class="li-sub">${esc(k)}</div><div class="li-title">${esc(v)}</div></div></div>`).join("")
    + `<div class="li"><div class="li-main"><div class="li-sub">Team status</div></div>
        ${phBadge(PH_TEAM_STATUS, detail.team_status)}</div>`;
  const offered = detail.enrollment_status === "offered";
  let note = "", actions = "";
  if (offered) {
    const btns = [];
    if (detail.can_accept_offer) btns.push(`<button class="act success" data-g-accept-offer="${esc(jid)}|${esc(detail.game_id)}">Accept Offer</button>`);
    if (detail.can_decline_offer) btns.push(`<button class="act danger" data-g-decline-offer="${esc(jid)}|${esc(detail.game_id)}">Decline Offer</button>`);
    actions = btns.join("");
    if (detail.blocked_reason)
      note = `<div class="banner ${btns.length ? "warn" : "neutral"}"><p>${esc(detail.blocked_reason)}</p></div>`;
  } else {
    note = `<div class="banner neutral"><p>This is an open substitute opportunity. Enrolling ${esc(detail.team_name)}'s roster is handled by the player or a coach.</p></div>`;
  }
  return `<div class="section-title" style="margin-top:0">${offered ? "Substitute Offer" : "Substitute Opportunity"}</div>
    ${note}
    <div class="card">${rows}</div>
    <div class="actions">${actions}
      <button class="act ghost" data-g-opp-back>Back</button>
    </div>`;
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
// Drives the single fixed toast container directly (#118 Phase 5) — a light
// DOM update, not a full render(), so a success toast can auto-clear itself
// without refetching every API call render() makes for the current view.
function updateToast() {
  const root = document.getElementById("toast-root");
  if (!root) return;
  if (toastTimer) { clearTimeout(toastTimer); toastTimer = null; }
  if (!toast) { root.hidden = true; return; }
  root.hidden = false;
  root.classList.toggle("error", !!toastIsError);
  root.innerHTML = `<span class="toast-msg">${esc(toast)}</span>
    <button class="toast-close" aria-label="Dismiss" data-toast-close>×</button>`;
  root.querySelector("[data-toast-close]").onclick = () => {
    toast = ""; toastIsError = false; updateToast();
  };
  if (!toastIsError) {
    toastTimer = setTimeout(() => {
      toast = ""; toastIsError = false; toastTimer = null; updateToast();
    }, 4000);
  }
}
// The delivery recipient_ref the signed-in user speaks for (#81), used for the
// self-service notification-preference toggles. Mirrors the server's mapping.
function ownRecipientRef() {
  const u = currentUser;
  if (!u) return null;
  if (u.role === "official" && u.scope && u.scope.official_id) return "official:" + u.scope.official_id;
  // Only a coach speaks for the shared team channel; a player has no own
  // delivery target in this slice, so they get no self-service prefs panel
  // (and cannot mute the whole team's notifications). Mirrors the server.
  if (u.role === "coach" && u.scope && u.scope.team_id) return "team:" + u.scope.team_id;
  // A guardian has no session scope at all (#26 — authority comes solely
  // from the verified link), but now DOES have their own delivery target
  // (#32 — a linked junior's notifications fan out to them) keyed by their
  // own account id instead.
  if (u.role === "guardian" && u.id) return "guardian:" + u.id;
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
    ? `<div class="feed-url"><code>${esc(location.origin + newFeedUrl)}</code>${feedCopyBtn(location.origin + newFeedUrl)}
        <p class="muted">Copy this URL into your calendar app. It is shown once —
        it won't be displayed again.</p></div>`
    : "";
  const active = feedTokens.length
    ? `<div class="row-list">${feedTokens.map((t) => {
        // Lifecycle metadata (#131): who minted it (an operator sees "anonymous"
        // for a publicly-minted team/division feed) and whether it's actually
        // being polled by a calendar app, not just sitting unused.
        const lastUsed = t.last_used_at ? `last used ${fmtDateTime(t.last_used_at)}` : "never used";
        const mintedBy = t.created_by ? ` · minted by ${esc(t.created_by)}` : "";
        return `
        <div class="session-row">
          <span class="row-main">Feed created ${fmtDateTime(t.created_at)}</span>
          <span class="row-sub">${lastUsed}${mintedBy}</span>
          <button class="act ghost" data-feed-revoke="${esc(t.id)}">Revoke</button>
        </div>`;
      }).join("")}</div>`
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
  const intro = pageIntro("Assignment offers, roster changes, and results that affect you or your team.");
  const head = `<div class="notif-head"><div class="section-title" style="margin:0">Notifications${unread ? ` · ${unread} unread` : ""}</div>
    ${unread ? `<button class="act ghost" data-notif-readall>Mark all read</button>` : ""}</div>`;
  if (!rows.length) {
    return `${intro}${head}${renderNotifPrefs()}${renderCalendarFeed()}<div class="banner neutral"><h2>You're all caught up</h2>
      <p>Assignment offers, roster alerts, and final results will show up here.</p></div>`;
  }
  const cards = rows.map((n) => {
    const link = n.game_id
      ? `<button class="act ghost" data-notif-open="${esc(n.game_id)}">Open game</button>` : "";
    return `<div class="notif-card ${n.read ? "read" : "unread"}" data-notif-read="${esc(n.id)}"
        role="button" tabindex="0" aria-label="${n.read ? "" : "Mark read: "}${esc(n.title)}">
      <span class="notif-ico">${NOTIF_ICON[n.kind] || "🔔"}</span>
      <div class="notif-body"><div class="notif-title">${esc(n.title)}${n.read ? "" : ` <span class="notif-dot" aria-hidden="true"></span>`}</div>
        <div class="notif-msg">${esc(n.message)}</div>
        <div class="notif-meta">${esc(fmtDateTime(n.at))}</div></div>
      ${link}</div>`;
  }).join("");
  return `${intro}${head}${renderNotifPrefs()}${renderCalendarFeed()}<div class="notif-list">${cards}</div>`;
}

/* ---------- Delivery admin: contacts + queue monitor (#61) ---------- */
const CHAN_ICON = { email: "✉️", push: "📲" };
const DELIV_BADGE = { pending: "gray", sent: "green", failed: "red",
  dead_lettered: "red", ignored: "gray" };
const DELIV_LABEL = { pending: "Pending", sent: "Sent", failed: "Failed",
  dead_lettered: "Dead-lettered", ignored: "Ignored" };

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
      <td><span class="badge ${DELIV_BADGE[d.status] || "gray"}">${DELIV_LABEL[d.status] || esc(d.status)}</span></td>
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
      <td><span class="badge ${DELIV_BADGE[d.status] || "gray"}">${DELIV_LABEL[d.status] || esc(d.status)}</span></td>
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
  return pageIntro("Where each notification actually went — sent, pending, or failed — and who it's sent to.")
    + `${renderDeliveryMonitor()}${renderContactsPanel(ov)}${renderDeviceTokensPanel(ov)}`;
}

/* ---------- Users / sessions admin (#78) ---------- */
// Role-specific scope field for the create-account form (#135) — a
// dropdown, never raw JSON, mirroring the actual UserAccount.scope shape
// each role's session resolution expects (web/server.py's role-specific
// scope keys: team_id/player_id/official_id).
const NEW_ACCOUNT_SCOPE_FIELD = {
  coach: { key: "team_id", label: "Team", inputId: "new-account-team" },
  player: { key: "player_id", label: "Player", inputId: "new-account-player" },
  official: { key: "official_id", label: "Official", inputId: "new-account-official" },
};
function renderCreateAccountForm(ov) {
  const roleOpts = roleCatalog
    .map((r) => opt(r.id, r.label, r.id === newAccountForm.role)).join("");
  const scopeSpec = NEW_ACCOUNT_SCOPE_FIELD[newAccountForm.role];
  let scopeField = "";
  if (scopeSpec) {
    const options = scopeSpec.key === "team_id" ? (ov.teams || []).map((t) => [t.id, t.name])
      : scopeSpec.key === "player_id" ? playersList.map((p) => [p.id, p.name])
      : officialsPool.map((o) => [o.id, o.name]);
    const opts = options.length
      ? options.map(([id, name]) => opt(id, name, id === newAccountForm[scopeSpec.key])).join("")
      : `<option value="">None available yet</option>`;
    scopeField = `<label class="cd-field"><span>${scopeSpec.label}</span>
      <select id="${scopeSpec.inputId}" class="cd-input">${opts}</select></label>`;
  }
  const guardianNote = newAccountForm.role === "guardian"
    ? `<p class="muted">Guardian access is granted by verified guardian links after account creation.</p>`
    : "";
  const err = newAccountError
    ? `<div class="banner alert"><p>${esc(newAccountError)}</p></div>` : "";
  return `<div class="card cd-form">
    <div class="section-title" style="margin-top:0">Create account</div>
    <p class="muted">Create accounts for staff, coaches, players, guardians, and officials.
      Passwords are temporary; share them out of band.</p>
    ${err}
    <div class="cd-grid">
      <label class="cd-field"><span>Username</span>
        <input id="new-account-username" class="cd-input" placeholder="coach1"
          value="${esc(newAccountForm.username)}" /></label>
      <label class="cd-field"><span>Temporary password</span>
        <input id="new-account-password" type="password" class="cd-input" autocomplete="new-password" /></label>
      <label class="cd-field"><span>Role</span>
        <select id="new-account-role" class="cd-input">
          <option value="">Select a role</option>${roleOpts}
        </select></label>
      ${scopeField}
      ${guardianNote}
      <div class="cd-submit"><button class="act primary" data-account-create>Create account</button></div>
    </div>
  </div>`;
}
function renderUsers(ov) {
  if (!hasPerm("manage_users")) {
    return `<div class="banner neutral"><h2>League admins only</h2>
      <p>Account and session administration is limited to league admins.</p></div>`;
  }
  const accts = usersState.accounts;
  const roleLabelFor = (roleId) =>
    (roleCatalog.find((r) => r.id === roleId) || {}).label || esc(roleId);
  const accountList = accts.length
    ? accts.map((a) => `<button class="row-btn ${a.id === usersSelected ? "active" : ""}"
        data-user-sessions="${esc(a.id)}">
        <span class="row-main">${esc(a.username)}</span>
        <span class="row-sub">${roleLabelFor(a.role)}${a.active ? "" : " · inactive"}</span>
      </button>`).join("")
    : `<div class="empty">No accounts yet. Use the "Create account" form above to add one.</div>`;
  const sessionPanel = usersSelected
    ? renderUserSessions()
    : `<p class="muted">Select an account to view its login sessions.</p>`;
  return `
    ${pageIntro("Create staff/coach/player/guardian/official accounts and review who's signed in.")}
    ${renderCreateAccountForm(ov)}
    <div class="card">
      <div class="section-title">Accounts</div>
      <div class="row-list">${accountList}</div>
    </div>
    ${renderCoachScopePanel(ov)}
    <div class="card">
      <div class="section-title">Sessions</div>
      ${sessionPanel}
    </div>
    ${renderGuardianLinks()}`;
}

// Coach scope remediation (#266): the supported admin action to rebind a
// selected Coach account to a valid team — the repair path readiness points at
// for a legacy/misconfigured coach with a missing or dangling team scope (which
// the scope gate refuses and which can't be reactivated until rebound). Shown
// only for a selected Coach; a coach whose current team is missing/invalid is
// flagged so the operator knows it needs repair before it can do anything.
function renderCoachScopePanel(ov) {
  if (!usersSelected) return "";
  const acct = (usersState.accounts || []).find((a) => a.id === usersSelected);
  if (!acct || acct.role !== "coach") return "";
  const teams = ov.teams || [];
  const currentTeam = (acct.scope || {}).team_id || "";
  const valid = currentTeam && teams.some((t) => t.id === currentTeam);
  const opts = teams.length
    ? teams.map((t) => opt(t.id, t.name, t.id === currentTeam)).join("")
    : `<option value="">No teams available</option>`;
  const warn = valid ? "" : `<div class="banner alert"><p>This coach account has
      no valid team assigned, so it can't manage any roster until it's rebound.</p></div>`;
  return `<div class="card cd-form">
    <div class="section-title">Coach team scope — ${esc(acct.username)}</div>
    ${warn}
    <div class="cd-grid">
      <label class="cd-field"><span>Team</span>
        <select id="rebind-team" class="cd-input">${opts}</select></label>
      <div class="cd-submit"><button class="act primary" data-rebind-scope="${esc(acct.id)}"
        ${teams.length ? "" : "disabled"}>Save team</button></div>
    </div>
  </div>`;
}

// Guardian↔junior links (#35): create an (unverified) link, then verify it
// with a real consent record — the GDPR Art. 8 gate the issue calls for.
// Previously this had no HTTP path at all; a link could only come from
// deterministic demo seeding.
function renderGuardianLinks() {
  const guardians = usersState.accounts.filter((a) => a.role === "guardian");
  const nameForPlayer = (pid) => (playersList.find((p) => p.id === pid) || {}).name || pid;
  const nameForGuardian = (uid) =>
    (usersState.accounts.find((a) => a.id === uid) || {}).username || uid;
  const guardianOpts = guardians.length
    ? guardians.map((g) => opt(g.id, g.username, g.id === guardianLinkForm.guardian_user_id)).join("")
    : `<option value="">No guardian accounts yet</option>`;
  const playerOpts = playersList.length
    ? playersList.map((p) => opt(p.id, p.name, p.id === guardianLinkForm.player_id)).join("")
    : `<option value="">No players yet</option>`;
  const form = `<div class="card cd-form">
    <div class="cd-grid">
      <label class="cd-field"><span>Guardian account</span>
        <select id="glink-guardian" class="cd-input">${guardianOpts}</select></label>
      <label class="cd-field"><span>Junior player</span>
        <select id="glink-player" class="cd-input">${playerOpts}</select></label>
      <div class="cd-submit"><button class="act primary" data-glink-create
        ${guardians.length && playersList.length ? "" : "disabled"}>Link guardian</button></div>
    </div>
  </div>`;
  const rows = guardianLinksState.length
    ? guardianLinksState.map((l) => {
        const status = l.verified
          ? `<span class="pill scheduled">Verified${l.consent_method ? " · " + esc(l.consent_method) : ""}</span>`
          : `<span class="pill gray">Unverified</span>`;
        const verifyForm = l.verified ? "" : `
          <input id="glink-consent-${esc(l.id)}" class="cd-input" style="max-width:180px"
            placeholder="e.g. signed_form" />
          <button class="act ghost" data-glink-verify="${esc(l.id)}">Verify</button>`;
        return `<div class="li">
          <div class="li-main">
            <div class="li-title">${esc(nameForGuardian(l.guardian_user_id))} → ${esc(nameForPlayer(l.player_id))}</div>
            <div class="li-sub">${status}</div>
          </div>
          ${verifyForm}
        </div>`;
      }).join("")
    : `<div class="empty">No guardian links yet. Use the form above to link a guardian to a junior player.</div>`;
  return `<div class="card">
    <div class="section-title">Guardian Links</div>
    ${form}
    <div class="row-list">${rows}</div>
  </div>`;
}

const SESSION_STATUS_LABEL = { active: "Active", revoked: "Revoked", expired: "Expired" };
function renderUserSessions() {
  const rows = usersState.sessions;
  if (!rows.length) return `<p class="muted">No sessions on record for this account.</p>`;
  return `<div class="row-list">` + rows.map((s) => `
    <div class="session-row">
      <span class="row-main">${esc(s.user_agent || "Unknown device")}</span>
      <span class="pill ${esc(s.status)}">${SESSION_STATUS_LABEL[s.status] || esc(s.status)}</span>
      <span class="row-sub">issued ${fmtDateTime(s.issued_at)}</span>
      ${s.status === "active"
        ? `<button class="act danger" data-revoke-session="${esc(s.id)}">Revoke</button>`
        : ""}
    </div>`).join("") + `</div>`;
}

/* ---------- Pilot readiness checklist (#104) ----------
   Read-only operator view assembled entirely from data already on hand —
   envStatus (#72, fetched once at bootstrap), the demo overview every view
   already loads, and one lazy /api/readiness call for the db/migration/
   admin/cookie checks. No new backend endpoint. */
const READINESS_CHECK_LABEL = {
  database_reachable: "Database reachable",
  migrations_current: "Migrations current",
  active_admin: "Active league admin",
  cookie_hardening: "Secure cookie hardening",
  persistent_store: "Persistent store",
  coach_scope_bound: "Coach team scoping",
};
const RD_BADGE_CLASS = { pass: "green", warn: "orange", fail: "red", info: "gray" };
const RD_BADGE_TEXT = { pass: "PASS", warn: "WARN", fail: "FAIL", info: "INFO" };
function rdRow(kind, label, detail) {
  return `<div class="li">
    <span class="badge ${RD_BADGE_CLASS[kind]}">${RD_BADGE_TEXT[kind]}</span>
    <div class="li-main"><div class="li-title">${esc(label)}</div><div class="li-sub">${esc(detail)}</div></div>
  </div>`;
}
function renderReadiness(ov) {
  if (!hasPerm("manage_setup")) {
    return `<div class="banner neutral"><h2>Operators only</h2>
      <p>The pilot readiness checklist is available to league admins.</p></div>`;
  }
  const rd = readinessCheck;
  if (!rd) {
    return `<div class="banner alert"><h2>Could not load readiness</h2>
      <p>The backend may not be running.</p></div>`;
  }
  const rows = [
    rdRow("info", "App mode", envStatus ? appModeLabel(envStatus.app_mode) : "Unknown"),
    rdRow("info", "Store backend",
      envStatus ? (STORE_LABEL[envStatus.store] || envStatus.store) : "unknown"),
  ];
  (rd.checks || []).forEach((c) => rows.push(
    rdRow(c.ok ? "pass" : "fail", READINESS_CHECK_LABEL[c.name] || c.name, c.detail)));
  const leagueCount = (ov.leagues || []).length;
  const teamCount = (ov.teams || []).length;
  rows.push(rdRow(leagueCount && teamCount ? "pass" : "warn", "Demo/pilot data loaded",
    `${leagueCount} league(s), ${teamCount} team(s)`));
  const fixtureCount = (ov.public_fixtures || []).length;
  rows.push(rdRow(fixtureCount ? "pass" : "warn", "Public schedule visible",
    fixtureCount ? `${fixtureCount} published fixture(s)` : "No published fixtures yet"));
  const canImport = hasPerm("manage_arena");
  rows.push(rdRow(canImport ? "pass" : "info", "Import wizard available",
    canImport ? "Available to this role" : "Not available to this role"));
  const emailLive = !!envStatus && deliveryLive(envStatus.email_mode, "smtp");
  const pushLive = !!envStatus && deliveryLive(envStatus.push_mode, "live");
  rows.push(rdRow(emailLive && pushLive ? "pass" : "warn", "Notification delivery",
    `email ${emailLive ? "live" : "dry-run"}, push ${pushLive ? "live" : "dry-run"}`));
  rows.push(rdRow("info", "Mobile readiness",
    "Phone layout implemented and verified down to a 390px viewport (#100/#101)."));
  rows.push(rdRow("info", "Known limitations",
    "CSV-only import (no native app or PWA yet); scheduler optimizer not built."));
  return `${pageIntro("A pre-flight checklist for running this league in production: data, delivery, and deployment posture.")}
    <div class="card"><div class="section-title" style="margin-top:0">Pilot Readiness</div>
    ${rows.join("")}</div>
    ${renderDangerZone()}`;
}

// Administration → Danger zone (#256): the single, guarded entry point to the
// production factory reset. Shown only when the deployment has opted in
// (`factory_reset_enabled`, i.e. production mode AND the ALLOW_PRODUCTION_
// FACTORY_RESET flag) and the signed-in user is a League Admin with both
// manage_setup and manage_users — the same conditions the backend enforces.
// The button opens a multi-step confirmation modal (preview → acknowledge →
// re-authenticate → typed phrase → execute); it never wipes on a single click.
function renderDangerZone() {
  if (!canFactoryReset()) return "";
  return `<div class="card danger-zone">
    <div class="section-title" style="margin-top:0">Danger zone</div>
    <div class="dz-body">
      <div class="dz-copy">
        <div class="dz-title">Factory reset production data</div>
        <p class="muted">Permanently erase all business and operational data —
          setup hierarchy, teams, players, registrations, games, rosters,
          officials, notifications, imports and more. The database schema and
          this installation stay intact; your admin account is preserved and
          every session is signed out. <strong>This cannot be undone.</strong></p>
      </div>
      <button class="act danger" data-factory-reset>Factory reset production data…</button>
    </div>
  </div>`;
}

/* ---------- Draft scheduler review + publish (#86/#106) ---------- */
// Reconciles schedulerState.selected against the latest drafts fetch against
// `previousDrafts` (the prior fetch's snapshot, taken by the caller before
// overwriting schedulerState.drafts): default-selects only NEWLY-seen clean
// (issue-free) drafts, and drops a selection if a game the operator hadn't
// looked at yet silently went from clean to flagged between fetches — so
// publish can't include a problem game the operator never consciously
// approved in its broken state. A game the operator explicitly selected
// DESPITE a pre-existing issue (e.g. via "Select all") stays selected; only
// a clean → flagged transition on an untouched selection clears it.
function reconcileDraftSelection(drafts, previousDrafts) {
  const previousIssues = new Map((previousDrafts || []).map((g) => [g.game_id, g.issues]));
  const next = new Set();
  for (const g of drafts) {
    const wasSeen = previousIssues.has(g.game_id);
    const wasClean = wasSeen && previousIssues.get(g.game_id).length === 0;
    if (schedulerState.selected.has(g.game_id)) {
      if (g.issues.length && wasClean) continue;  // drifted clean -> flagged
      next.add(g.game_id);
    } else if (!wasSeen && !g.issues.length) {
      next.add(g.game_id);
    }
  }
  schedulerState.selected = next;
}
const SCHED_ISSUE_LABEL = {
  missing_officials: "Missing officials", officials_pending: "Officials pending",
  roster_not_ready: "Roster not ready", slot_conflict: "Slot conflict",
  team_double_booked: "Team double-booked",
};
// Blocking issues (no official assigned at all, or a hard scheduling clash)
// read red; softer in-progress states (pending acceptance, roster not yet
// confirmed) read as a warning — same red/orange split as everywhere else.
const SCHED_ISSUE_SEVERE = new Set(["missing_officials", "slot_conflict", "team_double_booked"]);
function schedDraftRow(g) {
  const checked = schedulerState.selected.has(g.game_id);
  const badges = g.issues.map((i) =>
    `<span class="badge ${SCHED_ISSUE_SEVERE.has(i) ? "red" : "orange"}">${esc(SCHED_ISSUE_LABEL[i] || i)}</span>`).join(" ");
  // #277: a committed draft physically blocks warm-up/resurfacing facility
  // time — the review row shows the same derived reserved span the
  // calendar's slot card does (one backend derivation feeds both).
  const rsv = g.reserved
    ? `<div class="slot-reserved">reserved ${fmt(g.reserved.reserved_start_time)}–${fmt(g.reserved.reserved_end_time)} (+${g.reserved.warmup_minutes}m warm-up, +${g.reserved.resurfacing_minutes}m resurfacing)</div>`
    : "";
  return `<div class="li">
    <input type="checkbox" class="sched-pick" data-sched-pick="${esc(g.game_id)}" ${checked ? "checked" : ""} />
    <span class="li-when"><span class="li-date">${esc(fmtRowDate(g.start_time))}</span><span class="li-time">${fmt(g.start_time)}</span></span>
    <div class="li-main"><div class="li-title">${esc(g.home_team_name)} vs ${esc(g.away_team_name)}</div>
      <div class="li-sub">${esc(g.division_name || "")} · ${esc(g.rink_name || "")}${badges ? " · " + badges : ""}</div>${rsv}</div>
    <span class="pill gray">Draft</span>${delBtn("game", g.game_id,
      g.home_team_name + " vs " + g.away_team_name, "Delete draft")}</div>`;
}
function renderScheduler(ov) {
  if (!hasPerm("manage_schedule")) {
    return `<div class="banner neutral"><h2>Operators only</h2>
      <p>The draft scheduler is available to league admins and arena managers.</p></div>`;
  }
  const divs = ov.divisions || [];
  const divOptions = (selectedId) => divs.map((d) =>
    `<option value="${esc(d.id)}" ${d.id === selectedId ? "selected" : ""}>${esc(d.name)}</option>`).join("");
  const pv = schedulerState.preview;
  let previewBlock = "";
  if (pv) {
    const games = (pv.draft_games || pv.created || []);
    const unsched = (pv.unscheduled || []);
    // The draft service reports how many eligible seasonal registrations it
    // resolved (#311). A round robin needs at least two Teams, so team_count < 2
    // can only ever yield 0 games / 0 conflicts — surface that explicitly rather
    // than as a bare, misleading "0 game(s), 0 conflict(s)". Commit is gated on
    // there being games to commit, so there is never an invalid commit path.
    const teamCount = (typeof pv.team_count === "number") ? pv.team_count : null;
    const notEnoughTeams = teamCount !== null && teamCount < 2;
    const commitBtn = `<div class="dq-actions">
      <button class="act primary" data-sched-commit ${games.length ? "" : "disabled"}>Commit as draft</button></div>`;

    let head, cardBody;
    if (notEnoughTeams) {
      // Name the selected context (Division, and League when the overview
      // carries it) so the operator can see exactly which Season/League/Division
      // resolved too few registrations, and point at the corrective path — the
      // same "Season participation" target the Setup tree already links to.
      const selDiv = divs.find((d) => d.id === schedulerState.division);
      const divName = selDiv ? selDiv.name : "the selected division";
      const leagueName = selDiv && (selDiv.league_name || selDiv.level_name);
      const ctx = leagueName ? `${esc(divName)} (${esc(leagueName)})` : esc(divName);
      const lead = teamCount === 0
        ? "No Teams are registered for this schedule yet."
        : "Only one Team is registered — a schedule needs at least two.";
      head = `<div class="section-title">Preview — not enough registered Teams</div>`;
      cardBody = `<div class="sched-empty">
        <div class="sched-empty-lead">${esc(lead)}</div>
        <p>Generate found <strong>${teamCount}</strong> eligible Team${teamCount === 1 ? "" : "s"} for <strong>${ctx}</strong>.</p>
        <p>Teams appear here only when they are <strong>actively registered</strong> for the selected Season, League, and Division — permanent Team or League membership is not enough.</p>
        <p>Register at least two Teams under <button class="linklike" data-goto="setup">Setup → Season participation</button>, then Generate again.</p>
      </div>`;
    } else {
      // #328 review — a pairing that already has a real Game (#206 slice 1)
      // is neither a proposed game nor a conflict; it must still be named,
      // not silently folded into a misleading "No games generated." when
      // every pairing in the round robin is already on the calendar.
      const already = (pv.already_scheduled || []);
      // #390 — the DATE joins the clock in a .li-when wrapper, never in
      // .li-title: every other scheduler journey compares that title with
      // ===, and a proposal's day is a property of when it is, not of who is
      // playing. .li-time keeps holding exactly the clock.
      const gRows = games.map((g) => `<div class="li">
        <span class="li-when"><span class="li-date">${esc(fmtRowDate(g.start_time))}</span><span class="li-time">${fmt(g.start_time)}</span></span>
        <div class="li-main"><div class="li-title">${esc(g.home_team_name)} vs ${esc(g.away_team_name)}</div>
          <div class="li-sub">${esc(g.rink_name || "")}</div></div></div>`).join("");
      const aRows = already.map((a) => `<div class="li">
        <div class="li-main"><div class="li-title">${esc(a.home_team_name)} vs ${esc(a.away_team_name)}</div>
          <div class="li-sub">✓ Already scheduled — Game ${esc(a.existing_game_id)}</div></div></div>`).join("");
      const uRows = unsched.map((u) => `<div class="li">
        <div class="li-main"><div class="li-title">${esc(u.home_team_name)} vs ${esc(u.away_team_name)}</div>
          <div class="li-sub conflict">⚠ ${esc(u.reason)}</div></div></div>`).join("");
      const alreadyPart = already.length
        ? `, ${already.length} already scheduled` : "";
      head = `<div class="section-title">Preview — ${games.length} game(s), ${unsched.length} conflict(s)${alreadyPart}</div>`;
      // "Nothing missing" only when there is also nothing genuinely
      // blocked (unsched) -- a mixed batch that is partly already-scheduled
      // and partly conflicted still has something missing, just not free
      // yet, so it must not claim victory.
      const nothingMissing = !games.length && !unsched.length && already.length > 0;
      const intro = nothingMissing
        ? '<div class="li"><div class="li-main"><div class="li-sub">Every pairing is already scheduled — nothing missing to generate.</div></div></div>'
        : "";
      const rows = intro + gRows + aRows + uRows;
      cardBody = rows || '<div class="empty">No games generated.</div>';
    }
    const alreadyCount = (pv.already_scheduled || []).length;
    previewBlock = `<div id="sched-preview" class="sched-preview" data-team-count="${teamCount === null ? "" : teamCount}" data-games="${games.length}" data-conflicts="${unsched.length}" data-already-scheduled="${alreadyCount}" data-not-enough-teams="${notEnoughTeams ? "1" : "0"}">
      ${head}
      <div class="card">${cardBody}</div>
      ${commitBtn}</div>`;
  }

  const allDrafts = schedulerState.drafts || [];
  const summary = schedulerState.summary;
  const f = schedulerState.filters;
  const rinkOpts = (ov.rinks || []).map((r) =>
    `<option value="${esc(r.id)}" ${r.id === f.rink ? "selected" : ""}>${esc(r.name)}</option>`).join("");
  const drafts = allDrafts.filter((g) => {
    if (f.division !== "all" && g.division_id !== f.division) return false;
    if (f.rink !== "all" && g.rink_id !== f.rink) return false;
    if (f.issue === "issues" && !g.issues.length) return false;
    if (f.issue === "clean" && g.issues.length) return false;
    return true;
  });

  const summaryBlock = summary ? `<div class="section-title" style="margin-top:0">
      Review summary — ${summary.draft_count} draft, ${summary.published_count} published,
      ${summary.issue_count} with issue${summary.issue_count === 1 ? "" : "s"}</div>
    <div class="li-sub" style="padding:0 4px 12px">
      By division: ${Object.entries(summary.by_division).map(([k, v]) => `${esc(k)} (${v})`).join(", ") || "—"}
      &nbsp;·&nbsp; By rink: ${Object.entries(summary.by_rink).map(([k, v]) => `${esc(k)} (${v})`).join(", ") || "—"}
    </div>` : "";

  const filterBlock = allDrafts.length ? `<div class="dq-actions">
      <select id="sched-filter-div"><option value="all">All divisions</option>${divOptions(f.division === "all" ? null : f.division)}</select>
      <select id="sched-filter-rink"><option value="all">All rinks</option>${rinkOpts}</select>
      <select id="sched-filter-issue">
        <option value="all" ${f.issue === "all" ? "selected" : ""}>All</option>
        <option value="issues" ${f.issue === "issues" ? "selected" : ""}>With issues</option>
        <option value="clean" ${f.issue === "clean" ? "selected" : ""}>Clean only</option>
      </select>
    </div>` : "";

  const dRows = drafts.map(schedDraftRow).join("");
  const selectedCount = schedulerState.selected.size;
  const draftBlock = `<div class="section-title">Draft games (${drafts.length}${drafts.length !== allDrafts.length ? ` of ${allDrafts.length}` : ""})</div>
    ${filterBlock}
    <div class="card">${dRows || '<div class="empty">No draft games match these filters.</div>'}</div>
    ${allDrafts.length ? `<div class="dq-actions">
      <button class="act ghost" data-sched-select-all>Select all</button>
      <button class="act ghost" data-sched-select-clean>Select clean only</button>
      <button class="act ghost" data-sched-select-none>Select none</button>
    </div>
    <div class="dq-actions">
      <button class="act success" data-sched-publish ${selectedCount ? "" : "disabled"}>Publish ${selectedCount} of ${allDrafts.length}</button>
      <button class="act ghost danger" data-sched-discard ${selectedCount ? "" : "disabled"}>Discard ${selectedCount} of ${allDrafts.length}</button>
    </div>` : ""}`;

  return `${pageIntro("Generate draft games from open ice slots, review for conflicts, then publish.")}
    <div class="card">
      <div class="section-title" style="margin-top:0">Generate draft schedule</div>
      <div class="dq-actions">
        <select id="sched-div">${divOptions(schedulerState.division)}</select>
        <label class="sr-only" for="sched-meetings">Games against each opponent</label>
        <select id="sched-meetings" title="Games against each opponent">${
          [1, 2, 3, 4].map((n) => `<option value="${n}"${
            n === schedulerState.meetings ? " selected" : ""
          }>${n === 1 ? "1 game" : `${n} games`} vs each opponent</option>`).join("")
        }</select>
        <label class="sr-only" for="sched-turnaround">Minimum turnaround between a team's games</label>
        <select id="sched-turnaround" title="Minimum turnaround between a team's games">${
          [0, 15, 30, 45, 60, 90, 120].map((m) => `<option value="${m}"${
            m === schedulerState.turnaround ? " selected" : ""
          }>${m === 0 ? "No minimum turnaround" : `${m} min turnaround`}</option>`).join("")
        }</select>
        <button class="act" data-sched-generate>Generate</button>
      </div></div>
    ${previewBlock}${summaryBlock}${draftBlock}`;
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
  // #331 review round 18: several structured errors/warnings name the
  // EXACT conflicting row ids (`affected_registration_ids`,
  // `affected_game_ids`) so an operator can resolve them precisely instead
  // of guessing which of several rows for the same team/season is at
  // fault -- team_registration_conflict is the round-18 motivating case
  // (two active registrations for one team, only distinguishable by their
  // League, which Season participation's own rows now render — see
  // renderSeasonParticipation), but this stays reason-agnostic like the
  // rest of this renderer: any entry carrying either id list gets it shown,
  // with no reason-specific branch to leave uncovered later.
  const idList = (it) => {
    const regIds = it.affected_registration_ids || [];
    const gameIds = it.affected_game_ids || [];
    if (!regIds.length && !gameIds.length) return "";
    const parts = [];
    if (regIds.length) parts.push(`registration(s) ${regIds.map((id) => `<code>${esc(id)}</code>`).join(", ")}`);
    if (gameIds.length) parts.push(`game(s) ${gameIds.map((id) => `<code>${esc(id)}</code>`).join(", ")}`);
    return `<div class="li-sub muted">Affected: ${parts.join(" · ")}</div>`;
  };
  return items.map((it) => `<div class="li"><div class="li-main">
    <div class="li-title">${esc(it.sheet || "")}${it.row != null ? ` — row ${it.row}` : ""}${it.field ? ` (${esc(it.field)})` : ""}</div>
    <div class="li-sub ${cls}">${cls === "error" ? "⚠" : "ℹ"} ${esc(it.message)}</div>${idList(it)}</div></div>`).join("");
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
  // No selected `<option>` (#331 review round 7) when importState.seasonId
  // is unset -- fails CLOSED to an explicit, disabled placeholder rather
  // than the native <select>'s own "no option marked selected -> pick the
  // first one" default, which would silently reintroduce a global-first
  // Season exactly like the fallback this state now deliberately omits.
  const seasonField = !type.needsSeason ? "" : !seasons.length
    ? `<label>Season <span class="req">*</span></label>
       <div class="drawer-note">Create a season first.</div>`
    : `<label>Season <span class="req">*</span></label>
       <select id="import-season">${!importState.seasonId
          ? `<option value="" selected disabled>— select a season —</option>` : ""}
         ${seasons.map((s) => `<option value="${esc(s.id)}"`
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
    ${reportHtml}${resultHtml}`;
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
    `;
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
            ? `<button class="act success" data-accept="${esc(a.assignment_id)}">Accept</button>
               <button class="act danger" data-decline="${esc(a.assignment_id)}">Decline</button>` : ""}
          ${canManage ? `<button class="act danger ghost xbtn" data-unassign="${esc(a.assignment_id)}" title="Unassign" aria-label="Unassign">✕</button>` : ""}</div>`;
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
      `${posTag(p)}${locked ? "" : `<button class="act primary" data-act="select" data-id="${esc(p.id)}">Add</button>`}`)).join("");
    return `<div class="avail-group"><div class="avail-head">${label} ${need}</div>${rows}</div>`;
  };
  const body = available.length
    ? mk("Goalies", "goalie", s.open_goalie_slots) + mk("Skaters", "skater", s.open_skater_slots)
    : `<div class="empty">All eligible players are on the roster or in the sub pool.</div>`;
  return `<div class="section-title">Available players (${available.length})</div>
    <div class="card">${body}</div>`;
}

// Coach substitute outreach queue (#112): the ordered candidate list from
// /api/games/{gid}/substitute-candidates, with a Send Offer for each enrolled
// candidate that fits an open slot (offer → the player accepts/declines), plus
// the existing Add-now coach override. Falls back to nothing until loaded.
const SUB_STATUS_BADGE = {
  enrolled: '<span class="badge blue">Enrolled</span>',
  offered: '<span class="badge orange">Offered</span>',
  accepted: '<span class="badge green">Accepted</span>',
  declined: '<span class="badge gray">Declined</span>',
  expired: '<span class="badge red">Expired</span>',
  withdrawn: '<span class="badge gray">Withdrawn</span>',
  cancelled: '<span class="badge gray">Cancelled</span>',
};
function outreachPanel(canEdit) {
  const q = subCandidates;
  const slotLine = `${q.open_goalie_slots} goalie slot${q.open_goalie_slots === 1 ? "" : "s"} · `
    + `${q.open_skater_slots} skater slot${q.open_skater_slots === 1 ? "" : "s"} open`;
  const addable = (addableSubs && addableSubs.addable) || [];
  const noSubsMessage = canEdit && addable.length
    ? "No substitutes enrolled yet. Add an eligible player from the Eligible Players list below, or wait for a player to self-enroll."
    : "No substitutes enrolled yet.";
  const rows = (q.candidates || []).map((c) => {
    const badge = SUB_STATUS_BADGE[c.status] || `<span class="badge gray">${esc(c.status)}</span>`;
    // A slot fits this candidate's position and the game is still mutable, so
    // the coach's "Add now" override (#4 coach-override-always-wins) applies to
    // enrolled AND already-offered candidates.
    const canAddNow = canEdit && !q.locked && !q.cancelled
      && (c.slot_type === "goalie" ? q.open_goalie_slots > 0 : q.open_skater_slots > 0);
    const addBtn = canAddNow ? `<button class="act ghost" data-act="add" data-id="${esc(c.player_id)}">Add now</button>` : "";
    let btn = "";
    if (canEdit && c.status === "offered") btn = `<span class="li-sub">Awaiting response</span>${addBtn}`;
    else if (c.can_offer) btn = `<button class="act primary" data-act="offer" data-id="${esc(c.player_id)}">Send Offer</button>${addBtn}`;
    return `<div class="li"><div class="li-main">
        <div class="li-title">${esc(c.name)}</div>
        <div class="li-sub">${esc(c.position)}</div></div>${badge}${btn}</div>`;
  }).join("") || `<div class="empty">${esc(noSubsMessage)}</div>`;
  // Eligible-but-not-yet-enrolled team players (#114) — a coach can add one
  // straight into the pool above without waiting for the player to
  // self-enroll. Own card so it reads as a distinct "who could I add?"
  // question from "who's already enrolled?" above.
  const addableRows = addable.map((p) => `<div class="li"><div class="li-main">
      <div class="li-title">${esc(p.name)}</div>
      <div class="li-sub">${esc(p.position)}</div></div>
      ${canEdit ? `<button class="act primary" data-act="add-candidate" data-id="${esc(p.player_id)}">Add as candidate</button>` : ""}
    </div>`).join("");
  const addableCard = addable.length ? `<div class="section-title">Eligible Players</div>
    <div class="card">${addableRows}</div>` : "";
  return `<div class="section-title">Substitute Outreach</div>
    <div class="li-sub" style="padding:0 4px 8px">${slotLine}</div>
    <div class="card">${rows}</div>
    ${addableCard}`;
}

// Reschedule request/approval workflow (#29): request a move for a
// PUBLISHED game, the opponent coach accepts/rejects, then a league
// admin/arena manager approves (picking a replacement slot — reusing the
// same one-game-per-slot guarantee move_game already enforces) or denies.
// A coach viewing either side of the game can always tell "am I the
// opponent?" from `myTeam !== requested_by_team_id` — if they can see this
// game at all, their team must be one of its only two teams.
function renderReschedulePanel(canRequest, ov) {
  if (!rescheduleRequests) return "";
  const open = rescheduleRequests.find((r) =>
    r.status === "pending_opponent" || r.status === "pending_league_approval");
  const isOperator = hasPerm("manage_schedule");
  const myTeam = (currentUser && currentUser.scope) ? currentUser.scope.team_id : null;

  if (!open) {
    if (!canRequest && !isOperator) return "";
    return `<div class="section-title">Reschedule</div>
      <div class="card cd-form">
        <div class="cd-grid">
          <label class="cd-field" style="flex:1"><span>Reason</span>
            <input id="resched-reason" class="cd-input" placeholder="e.g. Rink unavailable" /></label>
          <div class="cd-submit"><button class="act ghost" data-resched-request>Request reschedule</button></div>
        </div>
      </div>`;
  }

  const isOpponent = !!myTeam && myTeam !== open.requested_by_team_id;
  if (open.status === "pending_opponent") {
    if (isOpponent || isOperator) {
      return `<div class="section-title">Reschedule</div>
        <div class="card"><div class="li"><div class="li-main">
          <div class="li-title">Reschedule requested</div>
          <div class="li-sub">${esc(open.reason)}</div></div>
          <button class="act primary" data-resched-respond="${esc(open.id)}" data-resched-accept="1">Accept</button>
          <button class="act danger" data-resched-respond="${esc(open.id)}" data-resched-accept="0">Reject</button>
        </div></div>`;
    }
    return `<div class="section-title">Reschedule</div>
      <div class="card"><div class="li"><div class="li-main">
        <div class="li-title">Reschedule requested</div>
        <div class="li-sub">Awaiting the opponent's response · ${esc(open.reason)}</div></div></div></div>`;
  }

  // pending_league_approval
  if (isOperator) {
    const slots = (ov.ice_slots || []).filter(
      (s) => s.status === "available" && s.slot_type === "game");
    const rinkName = (id) => nameById(ov.rinks, id);
    const slotOpts = slots.length
      ? slots.map((s) => opt(s.id, `${fmtDateTime(s.start_time)} · ${rinkName(s.rink_id)}`, false)).join("")
      : `<option value="">No open ice slots</option>`;
    return `<div class="section-title">Reschedule — awaiting your decision</div>
      <div class="card cd-form">
        <div class="li-sub" style="padding:4px">${esc(open.reason)}</div>
        <div class="cd-grid">
          <label class="cd-field" style="flex:1"><span>Replacement ice slot</span>
            <select id="resched-slot" class="cd-input">${slotOpts}</select></label>
          <div class="cd-submit">
            <button class="act primary" data-resched-decide="${esc(open.id)}"
              data-approve="1" ${slots.length ? "" : "disabled"}>Approve</button>
            <button class="act ghost" data-resched-decide="${esc(open.id)}" data-approve="0">Deny</button>
          </div>
        </div>
      </div>`;
  }
  return `<div class="section-title">Reschedule</div>
    <div class="card"><div class="li"><div class="li-main">
      <div class="li-title">Reschedule accepted</div>
      <div class="li-sub">Awaiting league approval · ${esc(open.reason)}</div></div></div></div>`;
}
function coachBody(board, ov) {
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
      management is a follow-up: #25).</p></div>`;
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
          ? `<button class="act success" data-act="select" data-id="${esc(p.id)}">Add back</button>`
          : `<button class="act success" data-act="confirm" data-id="${esc(p.id)}">Re-confirm</button>`;
      } else if (p.availability === "available") {
        btns = `<button class="act danger" data-act="backout" data-id="${esc(p.id)}">Can't play</button>`;
      } else {
        btns = `<button class="act ghost" data-act="confirm" data-id="${esc(p.id)}">Confirm</button>`;
      }
      btns += `<button class="act danger ghost xbtn" data-act="remove" data-id="${esc(p.id)}" title="Remove from roster" aria-label="Remove from roster">✕</button>`;
    }
    return playerRow(p, `${posTag(p)}${statusBadge(p)}${btns}`);
  }).join("") || `<div class="empty">No players on the roster yet — add from Available below.</div>`;

  // Fallback substitute pool for when the outreach queue hasn't loaded (a
  // non-operator viewer, or a candidate-fetch failure) — built lazily so it is
  // not computed on the common coach path where outreachPanel replaces it.
  const subPoolCard = () => `<div class="section-title">Substitute pool</div>
    <div class="card">${subs.length ? subs.map((p) => {
      const canAdd = canEdit && (p.slot_type === "goalie" ? s.open_goalie_slots > 0 : s.open_skater_slots > 0);
      const ctrl = SUB_STATUS_BADGE[p.sub_status] || SUB_STATUS_BADGE.enrolled;
      const btn = !canEdit ? "" : canAdd ? `<button class="act primary" data-act="add" data-id="${esc(p.id)}">Add</button>`
        : `<button class="act ghost" disabled>No slot</button>`;
      return playerRow(p, `${posTag(p)}${ctrl}${btn}`);
    }).join("") : `<div class="empty">No substitutes enrolled.</div>`}</div>`;

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
    ${subCandidates ? outreachPanel(canEdit) : subPoolCard()}
    ${footer}
    ${renderReschedulePanel(canRoster, ov)}
    `;
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
  let card = `<div class="empty">No players on this game's roster yet.</div>`;
  if (p) {
    if (p.group === "selected" && !p.backed_out)
      card = `<div class="banner ok"><h2>You are selected</h2><p>Status: confirmed</p></div>
        ${acts(`<button class="act success" data-act="confirm" data-id="${esc(p.id)}">I'm Available</button>
                <button class="act danger" data-act="backout" data-id="${esc(p.id)}">I Can't Play</button>`)}`;
    else if (p.group === "selected" && p.backed_out)
      card = `<div class="banner alert"><h2>You marked yourself unavailable</h2><p>Coach notified.</p></div>
        ${acts(`<button class="act success" data-act="confirm" data-id="${esc(p.id)}">I'm Available again</button>`)}`;
    else if (p.group === "substitute" && p.sub_status === "offered")
      card = `<div class="banner warn"><h2>A slot is available</h2><p>Accept?</p></div>
        ${acts(`<button class="act success" data-act="accept" data-id="${esc(p.id)}">Accept</button>
                <button class="act danger" data-act="decline" data-id="${esc(p.id)}">Decline</button>`)}`;
    else if (p.group === "substitute")
      card = `<div class="banner neutral"><h2>Enrolled as substitute</h2><p>Waiting for a slot.</p></div>
        ${acts(`<button class="act danger" data-act="withdraw" data-id="${esc(p.id)}">Withdraw</button>`)}`;
    else
      card = `<div class="banner neutral"><h2>Not selected</h2><p>Not enrolled.</p></div>
        ${acts(`<button class="act primary" data-act="enroll" data-id="${esc(p.id)}">Enroll as Substitute</button>`)}`;
  }
  const picker = boundHere
    ? `<div class="scope-note">Signed in as <strong>${esc((p && p.name) || currentUser.scope.player_name || "you")}</strong> — you can only respond for yourself.</div>`
    : `<div class="section-title">View as player</div>
       <select class="player-picker" id="player-picker">${options}</select>`;
  return `${picker}${card}
    <div class="privacy-note">👪 Guardians respond for juniors — workflow in <a href="${REPO}/26" target="_blank">#26</a>.</div>`;
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
  league_created: "Program created", season_created: "Season created",
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
  if (!topLevel.length) return `<div class="empty">No setup activity yet — league, team, and player changes will appear here.</div>`;
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
  // No game selected, or the selected game's private data is out of this
  // user's scope (a coach/operator can land on Activity for a game their team
  // isn't in — the board comes back as a 403 error payload, not null). Either
  // way, show the operator setup feed plus a plain note instead of reading
  // board.players off an error object and crashing the whole view.
  if (!board || board.error) {
    const note = board && board.error
      ? esc(board.error.message || "You don't have access to this game's activity.")
      : "Open a game roster to see its game activity.";
    return operatorSection + `<div class="section-title">Game</div><div class="card"><div class="empty">${note}</div></div>`;
  }
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
  const lgName = ps.league_name || (ov.league || {}).name || "Program";
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
    const divName = (divs.find((d) => d.id === publicState.division) || {}).name || "Division";
    const opts = divs.map((d) =>
      `<option value="${esc(d.id)}" ${d.id === publicState.division ? "selected" : ""}>${esc(d.name)}</option>`).join("");
    const rows = ((publicState.standings && publicState.standings.standings) || []);
    const trs = rows.length ? rows.map((r, i) => `<tr>
        <td class="st-rank">${i + 1}</td>
        <td class="st-team">${esc(r.team_name)}
          <button type="button" class="feed-sub-btn" data-feed-subscribe="team:${esc(r.team_id)}" data-feed-label="${esc(r.team_name)} calendar" title="Subscribe to ${esc(r.team_name)}'s calendar" aria-label="Subscribe to ${esc(r.team_name)}'s calendar">📅</button>
        </td>
        <td>${r.gp}</td><td>${r.w}</td><td>${r.l}</td><td>${r.t}</td>
        <td>${r.gf}</td><td>${r.ga}</td><td>${r.gd > 0 ? "+" + r.gd : r.gd}</td>
        <td class="st-pts">${r.pts}</td></tr>`).join("")
      : `<tr><td colspan="10" class="st-empty">No results yet.</td></tr>`;
    const feedMsg = publicState.feedUrl
      ? `<div class="feed-url"><code>${esc(location.origin + publicState.feedUrl)}</code>${feedCopyBtn(location.origin + publicState.feedUrl)}
          <p class="muted">Copy this URL into your calendar app${publicState.feedLabel ? ` — ${esc(publicState.feedLabel)}` : ""}.
          It is shown once — it won't be displayed again, but the subscription keeps working.</p></div>`
      : "";
    body = `${divs.length ? `<div class="actions"><select id="public-div">${opts}</select>
        <button type="button" class="act ghost" data-feed-subscribe="division:${esc(publicState.division || "")}" data-feed-label="${esc(divName)} calendar">📅 Subscribe to ${esc(divName)} calendar</button>
        </div>` : ""}
      ${feedMsg}
      <div class="card st-card"><table class="st-table">
        <thead><tr><th>#</th><th>Team</th><th>GP</th><th>W</th><th>L</th><th>T</th>
          <th>GF</th><th>GA</th><th>GD</th><th>Pts</th></tr></thead>
        <tbody>${trs}</tbody></table></div>`;
  } else {
    const fixtures = ps.fixtures || [];
    const rowHtml = (f) => {
      const score = (f.home_score != null) ? ` <strong>${f.home_score}–${f.away_score}</strong>` : "";
      const cls = f.status === "Final" ? "gray" : f.status === "Cancelled" ? "blocked" : "scheduled";
      return `<button class="li li-btn" data-public-game="${esc(f.game_id)}">
        <span class="li-time">${fmt(f.start_time)}</span>
        <div class="li-main"><div class="li-title">${esc(f.home_team_name)} vs ${esc(f.away_team_name || "TBD")}${score}</div>
          <div class="li-sub">${esc(f.division_name || "")} · ${esc(f.venue_name || "")} · ${esc(f.rink_name || "")}</div></div>
        <span class="pill ${cls}">${esc(f.status)}</span></button>`;
    };
    if (!fixtures.length) {
      body = `<div class="card"><div class="empty">No games scheduled yet — check back once the league publishes its fixtures.</div></div>`;
    } else {
      // A row that shows only "18:00" leaves a fan unable to tell which day a
      // game falls on (#151). Fixtures arrive already sorted by start_time
      // (server-side), so one pass buckets them into day groups in order,
      // each under a dated header; the row keeps just the time. dayOf/fmt both
      // read the literal wall-clock, so a group's header date and its rows'
      // times never disagree across a midnight boundary.
      const groups = [];
      let cur = null;
      fixtures.forEach((f) => {
        const day = dayOf(f.start_time);   // "" for a fixture with no start_time
        if (!cur || cur.day !== day) { cur = { day, items: [] }; groups.push(cur); }
        cur.items.push(f);
      });
      const groupsHtml = groups.map((g) => {
        const heading = g.day ? fmtDate(g.day) : "Date to be confirmed";
        return `<div class="pub-day-head">${esc(heading)}</div>
          <div class="card">${g.items.map(rowHtml).join("")}</div>`;
      }).join("");
      // Times are stored/shown as UTC wall-clock (no per-venue time zone in the
      // model yet), so label it once rather than leave a bare ambiguous time.
      body = `${groupsHtml}<div class="pub-tznote">🕑 All game times are shown in UTC.</div>`;
    }
  }
  return `<div class="hero"><div class="when">Public</div><h2>${esc(lgName)}</h2></div>
    ${tabs}${body}
    <div class="privacy-note">🔒 Public view shows fixtures, scores, and standings only.
      Player names and all personal data are never exposed (policy: #35).</div>`;
}

// Wire "Subscribe to calendar" buttons (#33) rendered by renderPublic()'s
// standings tab — shared by the signed-in "Public" tab (render()) and the
// anonymous guest portal (renderPublicGuest()), since both render the exact
// same markup. Team/division only: minting is unauthenticated, matching the
// same public-safe fixtures already served at /api/public/schedule.
function wirePublicFeedSubscribe(container, rerender) {
  container.querySelectorAll("[data-feed-subscribe]").forEach((b) => b.onclick = async () => {
    const [actorType, actorRef] = b.dataset.feedSubscribe.split(":");
    if (!actorRef) return;
    const res = await post("/api/public/calendar-feeds",
      { actor_type: actorType, actor_ref: actorRef, label: b.dataset.feedLabel });
    publicState.feedUrl = (res && res.url) || null;
    publicState.feedLabel = publicState.feedUrl ? (b.dataset.feedLabel || null) : null;
    rerender();
  });
}

// Markup for a copy-to-clipboard button next to a freshly-minted feed URL
// (#133) — shared by the authenticated "Calendar subscription" card
// (renderCalendarFeed) and the public/guest subscribe flow (renderPublic).
function feedCopyBtn(url) {
  return `<button type="button" class="act ghost feed-copy-btn" data-copy-feed-url="${esc(url)}" aria-label="Copy calendar subscription URL">Copy</button>`;
}
// Wire copy-to-clipboard buttons rendered by feedCopyBtn() (#133). Clipboard
// access requires a secure context (https, or http://localhost for local
// dev/demo) — falls back to a clear error toast rather than failing silently
// when unavailable (e.g. plain-HTTP production without TLS).
function wireCopyFeedUrl(container) {
  container.querySelectorAll("[data-copy-feed-url]").forEach((b) => b.onclick = async () => {
    try {
      if (!navigator.clipboard) throw new Error("Clipboard access unavailable.");
      await navigator.clipboard.writeText(b.dataset.copyFeedUrl);
      toast = "Copied to clipboard."; toastIsError = false;
    } catch (e) {
      toast = "Couldn't copy automatically — select and copy the URL manually.";
      toastIsError = true;
    }
    updateToast();
  });
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
  const editing = drawer && drawer.mode === "edit";
  toast = "";
  captureDrawerValues(ent);
  // Validate required fields client-side; the backend stays authoritative.
  const missing = ent.fields.filter((f) => f.required && !val(f.id));
  if (missing.length) {
    drawerError = `Please fill in: ${missing.map((f) => f.label).join(", ")}.`;
    return render();
  }
  drawerError = "";
  // Edit (#268) routes to the entity's /<id>/update endpoint (Team + lifecycle
  // stay separate); create routes to the entity's POST.
  const res = editing ? await SETUP_EDIT[kind](drawer.id) : await SETUP_POST[kind]();
  if (res && res.error) {
    // Keep the drawer open, preserve input, surface the server's message.
    drawerError = res.error.message;
    toast = "";
    return render();
  }
  drawer = null; drawerError = ""; drawerValues = {};
  // Creating a Program or a Season changes the set of contexts this account
  // may select, and `contextOptions` is otherwise loaded ONCE per page load
  // (never re-polled by render()) -- so without this the context bar could
  // not offer the Season that was just created until a full reload.
  //
  // That went from a papercut to a dead end under the #367 owner ruling: the
  // active Season is now a hard ceiling on this surface, so a Season the
  // context bar cannot offer is one the operator cannot switch to, and the
  // League create drawer's Season picker (sourced from these same options,
  // see contextSeasonOptions) would have nothing to point at right after the
  // Season was made. Refreshing here is what keeps "create the Season, then
  // its League" a single uninterrupted flow.
  if (!editing && (kind === "season" || kind === "league")) {
    await loadContextOptions();
    renderContextSwitcher();
  }
  const noun = entNoun(ent);
  toast = `${noun[0].toUpperCase() + noun.slice(1)} ${editing ? "updated" : "created"}.`;
  await render();
}

async function rosterAction(act, id) {
  toast = "";
  const B = `/api/games/${currentGame}`;
  if (act === "build") await post(`${B}/build-roster`, { team_id: rosterTeamId });
  else if (act === "select") await post(`${B}/roster/select`, { player_ids: [id] });
  else if (act === "remove") await post(`${B}/roster/remove`, { player_id: id });
  else if (act === "copy") { const r = await post(`${B}/roster/copy-previous`, { team_id: rosterTeamId }); if (r && !r.error) toast = `Copied ${r.copied} player${r.copied === 1 ? "" : "s"} from the previous game.`; }
  else if (act === "confirm") await post(`${B}/availability`, { player_id: id, availability_status: "available" });
  else if (act === "backout") await post(`${B}/availability`, { player_id: id, availability_status: "unavailable" });
  else if (act === "enroll") await post(`${B}/substitutes/enroll`, { player_id: id });
  else if (act === "withdraw") await post(`${B}/substitutes/withdraw`, { player_id: id });
  else if (act === "add-candidate") { const r = await post(`${B}/substitutes/add-candidate`, { player_id: id }); if (r && !r.error) toast = "Added as a substitute candidate."; }
  else if (act === "add") await post(`${B}/substitutes/${id}/add-to-roster`, {});
  else if (act === "offer") { const r = await post(`${B}/substitutes/${id}/offer`, {}); if (r && !r.error) toast = "Offer sent to the substitute."; }
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
    // Same 7-day window AND same coach-team scoping as the dashboard's own
    // "Games this week" stat tile (#118 Phase 7 / #145 review) — both used
    // to count ov.schedule's full length instead, and before the #145
    // review fix this breadcrumb kept showing the league-wide count while
    // the card right below it already read the coach-scoped one.
    const coachTeamId = (currentRole === "coach" && currentUser
      && currentUser.scope && currentUser.scope.team_id) || null;
    const weekEnd = addDays(calendarDate, 6);
    const games = (ov.schedule || []).filter((g) => {
      const d = dayOf(g.start_time);
      return d >= calendarDate && d <= weekEnd
        && (!coachTeamId || g.home_team_id === coachTeamId || g.away_team_id === coachTeamId);
    }).length;
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
  updateToast();
  const c = document.getElementById("content");
  document.body.dataset.view = view;
  // #367 review: a completed factory reset (#256) already cleared this
  // browser's session cookie server-side (the execute response signs every
  // session out) BEFORE this render() is called to show the success modal --
  // so /api/demo/overview's now-session-required read would always 401 here,
  // throwing and replacing the success confirmation with an error banner the
  // operator never asked for, even though the reset itself genuinely
  // succeeded. The success modal needs no overview data at all (it's a
  // static "you're signed out" confirmation), so it paints on its own,
  // skipping the normal ov-dependent pipeline entirely rather than reworking
  // that pipeline to tolerate a caller it already knows is signed out.
  if (modal && modal.type === "factory-reset" && modal.step === "success") {
    setPageTitle(view);
    c.innerHTML = renderModal();
    wireModal(c);  // same generic modal wiring every other modal gets
    // #369 review: this early return must still run the SAME shared
    // dialog focus lifecycle every other render() path gets (line ~8332) --
    // skipping it left focus on the removed "Resetting…" control (or on
    // <body>), so keyboard/screen-reader users lost the dialog/focus-trap
    // contract even though the success modal was visibly on-screen.
    syncOverlayFocus();
    return;
  }
  // #345 review: set BEFORE the awaited overview load and before either
  // early return below (backend-error banner, restricted roster/sheet), so an
  // error or restricted state still announces the destination the user chose
  // rather than the previous view's title.
  setPageTitle(view);
  // #367: snapshotted before any fetch below, so a context switch that lands
  // WHILE this render() is still awaiting its now-context-scoped reads
  // (/api/demo/overview, /api/v2/setup/overview, /api/standings/*) can be
  // detected -- same idiom as importState/iceBuilder's own contextRevision
  // checks. setActiveContext()/sendContextSwitch() guarantee a fresh render()
  // always follows a switch (success or failure path), so bailing out here
  // is safe: the newer render() already in flight (or about to fire) repaints
  // correctly: this stale one just leaves the loading skeleton up briefly
  // rather than flashing another Program/Season/League's data.
  const myRenderContext = contextRevision;
  let ov, sv, hv, board, lineups, standings, inbox, playerHome;
  // #365 (no duplicate speech): the Home/Tasks live region carried THROUGH
  // this render rather than rebuilt by it. Decided before the blank below
  // (which would otherwise destroy the node) and re-checked at the paint --
  // see both sites.
  let spSlotNode = null;
  let carrySpSlot = false;
  // Per-ENDPOINT outcomes for the Setup view's own reads (#365) — see the
  // Setup branch below for why the payloads alone cannot carry this.
  let svOk = false, playersOk = false;
  // #365 review round 11: did THIS pass perform the Setup hierarchy
  // destination's own reads? It is the settlement signal a deep-link focus
  // intent resolves on (settleDestinationFocus), and it is per-PASS rather
  // than a module flag on purpose: `view` is module-level and re-read
  // throughout this function, so an older pass that flipped to Setup only
  // after it had already gone past the fetches below can still reach the
  // paint and render an EMPTY hierarchy from the default `sv`/`hv` shapes.
  // That pass proves nothing about whether a picker can exist, so it reports
  // nothing, and the intent waits for the pass that actually read.
  let hierarchyReadsSettled = false;
  try {
    // #365 owner correction — the render LIFECYCLE, not just the card model.
    // This line used to blank #content unconditionally, and every card was
    // re-committed only after the awaited reads below. Between the two there
    // was no Setup DOM at all, so a settled card could never be PAINTED while
    // its tuple was superseded: STALE was representable in the model and
    // unreachable on all six Setup landings, which is exactly the state #365
    // requires them to render.
    //
    // So when this render is re-entering a Setup surface that is already
    // painted, the existing card DOM is RETAINED across the fetches instead
    // of being blanked, and every card is repainted from its HELD model
    // first. readCardState() answers STALE for anything bound to a tuple the
    // operator has left, and setupLandingActions() withdraws that state's
    // action groups -- so what stands during the fetch window is last-good
    // data, labelled as earlier, with only its Refresh. When the reads land,
    // the ordinary full paint below replaces it exactly as before.
    //
    // Conditioned on the SAME Setup surface already being on screen: the same
    // workflow landing, or the same hub index. A NAVIGATION between Setup
    // surfaces (landing -> index, index -> Records) is not a re-entry and must
    // not leave the surface being left behind standing while the next one
    // loads -- that would be a stale destination, not retained data. Every
    // other view, and a first arrival on Setup, keeps the immediate skeleton
    // it had.
    // #365 (no duplicate speech). #sp-card-slot is `role="status"
    // aria-live="polite"`, and the comment on its own markup further down has
    // always claimed the wrapper "persists across every re-render" -- but it
    // did not: the blank below and the paint at the end of this function each
    // re-serialized it. On a context switch that meant the SAME stale card was
    // written into the SAME polite region twice in a row: once by
    // repaintContextScopedCardsAsStale(), synchronously and before the awaited
    // reads, and once by this render, from the same still-stale model. Two
    // byte-identical writes to one polite region is duplicate speech.
    //
    // The SECOND one is the redundant half. The first is the synchronous stale
    // withdrawal -- it must keep running before any await, which is the whole
    // reason it exists -- so it is this render that stands down: the existing
    // node is CARRIED, never detached, never re-serialized, never touched.
    //
    // Conditioned on BYTE-IDENTICAL content, so carrying can never leave a
    // card standing that says something other than what this render would have
    // painted. Every other case rebuilds exactly as before -- including the
    // LOADING skeleton an ordinary re-render still paints over settled data.
    spSlotNode = view === "dashboard"
      && (hasPerm("manage_setup") || hasPerm("manage_arena"))
      ? document.getElementById("sp-card-slot") : null;
    if (spSlotNode && spSlotNode.parentNode === c) {
      const held = readCardState(HOME_TASKS_CARD);
      carrySpSlot = homeCardPaintedHtml === renderSetupProgressCard(
        held.state === CARD_STATE.STALE ? held : { state: CARD_STATE.LOADING });
    }
    const paintedLanding = c.querySelector("[data-setup-workflow-landing]");
    const retainSetupCards = view === "setup" && setupView === "hub"
      && (setupWorkflow
        ? !!(paintedLanding
             && paintedLanding.dataset.setupWorkflowLanding === setupWorkflow)
        : !!c.querySelector(".swf-grid"));
    if (retainSetupCards) {
      // Retaining the SURFACE must not retain an overlay that has since been
      // CLOSED. Blanking used to remove a dismissed drawer/modal as a side
      // effect, synchronously, before any fetch -- a modal dialog left
      // standing (still trapping focus) until a refetch resolves would be a
      // strictly worse regression than the skeleton flash this removes. The
      // open cases need nothing here: the paint below rebuilds them, exactly
      // as it did when the surface was blanked first.
      if (!drawer) {
        c.querySelectorAll(".drawer-scrim, .drawer").forEach((el) => el.remove());
      }
      if (!modal) {
        c.querySelectorAll(".modal-scrim, .modal").forEach((el) => el.remove());
      }
      setupWorkflowsFor().forEach((w) => repaintSetupWorkflowCard(w.key, null));
    } else if (carrySpSlot) {
      // Same blank as below, around the carried live region instead of over
      // it: the slot node is left exactly where it is, with exactly the
      // content it already had.
      Array.from(c.children).forEach((el) => { if (el !== spSlotNode) el.remove(); });
      spSlotNode.insertAdjacentHTML("afterend",
        `<div class="skeleton"></div><div class="skeleton"></div>`);
    } else {
      c.innerHTML = `<div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div>`;
    }
    ov = await getJSON("/api/demo/overview");
    if (contextRevision !== myRenderContext) return;  // #367: superseded, a fresh render() is already coming
    if (ov && ov.error) throw new Error(ov.error.message);
    // Default the working game to the first one this user can actually open —
    // for a coach that's their own team's game, not an arbitrary game[0] that
    // would land Roster/Sheet on the "Restricted" guard before the game
    // picker (#154) could even offer a way out. A game already chosen (deep
    // link, prior pick) is kept as long as it still exists in the schedule.
    if ((!currentGame || !(ov.schedule || []).some((g) => g.game_id === currentGame))
        && ov.schedule[0]) {
      const acc = accessibleGames(ov);
      currentGame = (acc[0] || ov.schedule[0]).game_id;
    }
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
    // Coach substitute outreach queue for the shown side (#112), operator-only.
    subCandidates = null;
    addableSubs = null;
    if (view === "roster" && gameView === "coach" && hasPerm("manage_roster")
        && lineups && lineups[rosterSide] && !lineups.error) {
      const tid = lineups[rosterSide].team_id;
      const q = await getJSON(
        `/api/games/${currentGame}/substitute-candidates?team_id=${tid}`);
      if (q && !q.error) subCandidates = q;
      // Eligible-but-not-yet-enrolled team players a coach can add directly
      // (#114) — a separate list from the outreach queue above, which only
      // ever shows players who already have some enrollment.
      const a = await getJSON(
        `/api/games/${currentGame}/substitute-addable?team_id=${tid}`);
      if (a && !a.error) addableSubs = a;
    }
    // Coach dashboard action card (#146): the next upcoming game's
    // availability/substitute-queue detail, reusing the same team-scoped
    // endpoints the Roster tab already calls — team_id is omitted so the
    // route defaults to the coach's own team.
    dashAvailability = null;
    dashSubQueue = null;
    if (view === "dashboard" && currentRole === "coach" && currentUser
        && currentUser.scope && currentUser.scope.team_id) {
      const next = nextUpcomingGame(coachTeamGames(ov, currentUser.scope.team_id));
      if (next) {
        const av = await getJSON(`/api/games/${next.game_id}/availability-summary`);
        if (av && !av.error) dashAvailability = av;
        const sq = await getJSON(`/api/games/${next.game_id}/substitute-candidates`);
        if (sq && !sq.error) dashSubQueue = sq;
      }
    }
    // Reschedule request/approval state for the currently viewed game (#29)
    // — visible to either team's coach or an operator (same audience as the
    // outreach queue above, but not manage_roster-only: a league admin
    // needs it here too, to decide a pending approval).
    rescheduleRequests = null;
    if (view === "roster" && gameView === "coach"
        && (hasPerm("manage_roster") || hasPerm("manage_schedule"))
        && currentGame) {
      const rr = await getJSON(`/api/games/${currentGame}/reschedule`);
      if (rr && !rr.error) rescheduleRequests = rr.requests;
    }
    // The Setup view itself isn't permission-hidden (any signed-in role can
    // land on it; setupCard/renderSetupHierarchy hide actions per-entity), so
    // `sv`/`hv` always default to an empty shape here — never `undefined` —
    // regardless of which role reaches view==="setup" (#233 B2a review r1).
    if (view === "setup") {
      sv = { programs: [], seasons: [], leagues: [], divisions: [], teams: [], clubs: [], organizations: [], venues: [], rinks: [], ice_slots: [], officials: [] };
      hv = { programs: [] };
      // #365: the empty shape above is exactly why the per-card model needs an
      // explicit per-ENDPOINT outcome rather than reading the payload. `sv`
      // degrades to zeroes on failure, so "the overview call failed" and "this
      // Program genuinely has nothing yet" are indistinguishable from `sv`
      // alone -- the "state inferred from missing payload fields" the issue
      // forbids. These two flags carry the distinction to the cards.
      svOk = false;
      playersOk = false;
    }
    // Canonical Setup structure (#233 B2a). `sv` (the flat overview) is gated
    // MANAGE_ARENA server-side — both League Admin and Arena Manager hold it
    // — because Arena Managers need it for their own Organization/Venue/
    // Rink/Ice-slot cards and the Facility tree (#233 B2a review r1: this
    // used to crash for them, since it was fetched only under manage_setup).
    if (view === "setup" && (hasPerm("manage_setup") || hasPerm("manage_arena"))) {
      const svr = await getJSON("/api/v2/setup/overview");
      if (contextRevision !== myRenderContext) return;  // #367: superseded, a fresh render() is already coming
      svOk = !!(svr && !svr.error);
      if (svOk) sv = svr;
    }
    // The Competition tree, Setup Player card, and season participation are
    // MANAGE_SETUP-only (roster-adjacent / competition-structure data an
    // Arena Manager doesn't manage) — their own authenticated calls, never
    // bundled into the public demo overview.
    if (view === "setup" && hasPerm("manage_setup")) {
      const pl = await getJSON("/api/players");
      // #367 owner ruling / observed flake: this generation check was
      // MISSING here, and `playersList` is MODULE-level state read at paint
      // time rather than a local like `ov`/`sv`. So a superseded render()
      // that was still awaiting /api/players could assign the OLD context's
      // player list after a newer render() had already fetched everything
      // else -- and the newer render then painted its own correctly-scoped
      // Programs/Seasons/Leagues/Divisions/Teams/Clubs cards NEXT TO another
      // Program's player NAMES. It reproduced as exactly that mixed grid in
      // e2e/setup-v2-context-scope.js. Bailing before the assignment (the
      // same idiom used after the two fetches above) is what makes the
      // guard cover module-level state too; every module-level assignment
      // below this line is inside the same guarded stretch.
      if (contextRevision !== myRenderContext) return;  // superseded
      playersOk = Array.isArray(pl);
      playersList = playersOk ? pl : [];
      // The canonical Program→Season→League→Division tree (#233 B2a review
      // r1): consumed as-is rather than reconstructed from flat `sv` lists,
      // so needs_assignment/teams_without_division match the canonical
      // parentage rules exactly instead of a client-side reinterpretation.
      const hvr = await getJSON("/api/v2/setup/hierarchy");
      if (contextRevision !== myRenderContext) return;  // superseded
      if (hvr && !hvr.error) hv = hvr;
      // Season participation (#180, cut to v2 canonical #233 Slice B2b): a
      // program's permanent teams and each season's registrations, each its
      // own authenticated call (like the player list above), so the Setup
      // page can show which permanent team plays which season/league/
      // division — never derived from the legacy Team.division_id. Iterates
      // hv (already fetched above) rather than ov.leagues/ov.seasons so the
      // requested ids are consistently canonical; hv is only populated under
      // manage_setup, which already gates this whole block.
      leagueTeams = {}; seasonRegs = {}; leagueDivisions = {}; seasonVenueAccess = {};
      // Reset alongside seasonVenueAccess (#369 owner ruling): both maps are
      // keyed by Season id and both are now filled for the SELECTED Season
      // only, so a stale entry left over from a previous context is the one
      // way a Season could still paint venue data it no longer fetched.
      seasonVenueCandidates = {};
      permLeaguesByProgram = {}; allPermLeagues = []; teamPermLeague = {};
      // #369 owner ruling: `/venue-access` and `/venue-candidates` are
      // ceilinged to the EXACT persisted selected Season -- a sibling Season of
      // the active Program is refused just like a foreign one. This loop used
      // to fetch BOTH for every Season in the tree, which is precisely the
      // "Records using the endpoint as an all-Seasons inventory" the ruling
      // ends: it would now 404 once per non-selected Season. So they are
      // fetched for the selected Season alone, and every other Season renders a
      // non-identifying placeholder instead (see renderSeasonParticipation) --
      // no venue ids, no venue names, and no request issued. Deliberately NOT
      // replaced by a new batch endpoint: that would re-create the inventory
      // the ruling removed, one HTTP hop further down.
      const selectedSeasonId = (contextOptions && contextOptions.selected
        && contextOptions.selected.season_id) || null;
      // ...and an ARCHIVED selection fetches no candidates at all (#369 owner
      // ruling, follow-up). An archived Season is read-only history: the grant
      // this list feeds fails `season_archived`, so asking for candidates both
      // advertised an impossible mutation and pulled in the one facility list
      // that deliberately reaches ACROSS the Program ceiling. The server now
      // refuses that read generically, so issuing it would only produce a 404
      // per render; the gate is here so the request is never made and the
      // read-only surface holds nothing to render a picker from.
      // `read_only` is the SAME signal the context bar's "archived
      // (read-only)" option label and #ctx-ro badge already use -- it comes
      // straight off /api/context/options' `selected`.
      const selectionIsReadOnly = !!(contextOptions && contextOptions.selected
        && contextOptions.selected.read_only);
      for (const program of (hv.programs || [])) {
        const r = await getJSON(`/api/v2/setup/programs/${program.id}/teams`);
        // Same reason as the player list above, and more acute: this loop
        // awaits once per Program and twice per Season, so it is the widest
        // window in render() for a context switch to land mid-flight while
        // module-level state is still being filled in.
        if (contextRevision !== myRenderContext) return;  // superseded
        leagueTeams[program.id] = (r && r.teams) || [];
        // #283 Slice B: the program's permanent Leagues (from the canonical
        // hierarchy) back both the permanent-team tree and the team transfer/
        // create pickers — each carries its program name for a qualified label.
        const permLgs = (program.leagues || []).map((lg) => ({
          id: lg.id, name: lg.name, programName: program.name }));
        permLeaguesByProgram[program.id] = permLgs;
        permLgs.forEach((lg) => allPermLeagues.push(lg));
        // #283 Slice E: map each Team to its permanent League (from the
        // canonical permanent-league tree) so the Records team card can show it.
        for (const lg of (program.leagues || [])) {
          for (const t of (lg.teams || [])) {
            teamPermLeague[t.id] = { id: lg.id, name: lg.name };
          }
        }
        for (const s of (program.seasons || [])) {
          const rr = await getJSON(`/api/v2/setup/seasons/${s.id}/team-registrations`);
          if (contextRevision !== myRenderContext) return;  // superseded
          seasonRegs[s.id] = (rr && rr.registrations) || [];
          // The SELECTED Season only -- see the ruling note above. Every other
          // Season of this Program is skipped outright, so nothing about its
          // venues is requested, held or painted.
          if (s.id === selectedSeasonId) {
            // Allowed venues (#233 Slice E): which Venues this Season may use
            // for game ice, independent of any Venue-Program ownership.
            const va = await getJSON(`/api/v2/setup/seasons/${s.id}/venue-access`);
            if (contextRevision !== myRenderContext) return;  // superseded
            seasonVenueAccess[s.id] = (va && va.venue_access) || [];
            // Grant CANDIDATES for this Season (#369 review): its own
            // MANAGE_SETUP-gated route, not a field on the overview. The
            // candidate list is the one facility contract that reaches across
            // the Program ceiling, so it is fetched only where it is used and
            // only by a caller that could actually perform the grant -- an
            // Arena Manager gets a 403 here and simply has no picker, rather
            // than being handed every linked Venue in the installation by the
            // overview.
            // Skipped outright for a READ-ONLY (archived) selection: no
            // request is issued, so the archived surface can render no picker
            // even by accident.
            if (!selectionIsReadOnly) {
              const vc = await getJSON(
                `/api/v2/setup/seasons/${s.id}/venue-candidates`);
              if (contextRevision !== myRenderContext) return;  // superseded
              seasonVenueCandidates[s.id] = (vc && vc.candidates) || [];
            }
          }
        }
      }
      // #365 review round 11 — THE SETTLEMENT SIGNAL for the hierarchy
      // destination's deep-link focus intents. Reached only after this pass
      // has read the canonical hierarchy, every Program's teams, every
      // Season's registrations and (for the SELECTED Season, which is the
      // only one that can host either control) its venue-access grants and
      // grant candidates -- i.e. after every read the "Register" add row and
      // the "Allow a venue" picker are built from. It is below the last
      // `contextRevision` guard in the loop, so a superseded pass returns
      // above this line and reports nothing at all.
      //
      // From here on, whatever the paint below produces IS what this Season
      // has: a picker, or renderSeasonParticipation()'s explanatory copy for
      // a Season with no grantable venue left. That is what lets a missing
      // control be CONCLUDED absent rather than waited out.
      hierarchyReadsSettled = true;
    } else if (view === "setup") {
      // No MANAGE_SETUP: renderSeasonParticipation() returns "" outright, so
      // this surface has no Season rows, no Register add row and no Allow
      // picker, and none of the reads above would change that. A pass that
      // legitimately skipped them has still settled the question -- an Arena
      // Manager waiting forever for a control their permission set can never
      // render would be the same silent loss, one branch over.
      hierarchyReadsSettled = true;
    }
    // #365: bind each visible Setup workflow card to its OWN identity (card id
    // + context tuple + generation) and commit its model, inside the same
    // contextRevision-guarded stretch as the fetches it consumes — so the
    // cards a paint renders were bound under the same context that paint was.
    //
    // The per-workflow done/todo/optional status comes from the SAME route the
    // Home/Tasks card reads, so the hub chip, the landing's optional note and
    // the Home/Tasks badge can never disagree. Gated on exactly the permission
    // that route requires (MANAGE_ARENA — web/authz.py), so a role that cannot
    // read it simply has no status rather than a 403 that would read as a
    // per-card failure.
    //
    // #365 review round 2 finding 3: the outcome of that read is ASSERTED and
    // carried into the model (setupProgressRead), never flattened to `null`.
    // A `null` progress could not be told apart from "this role may not read
    // it", so a failed read produced cards that were READY/EMPTY with an
    // UNKNOWN status — a transport failing OPEN, presented as a settled hub.
    if (view === "setup" && setupWorkflowsFor().length) {
      let progress = setupProgressRead(CARD_READ.UNAUTHORIZED, null);
      if (hasPerm("manage_arena")) {
        const pr = await getJSON("/api/v2/setup/progress");
        if (contextRevision !== myRenderContext) return;  // superseded
        progress = setupProgressReadOf(pr);
      }
      commitSetupWorkflowCards({ sv: sv, ov: ov, players: playersList,
        svOk: svOk, playersOk: playersOk, progress: progress });
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
    // The signed-in player's own home screen (#107): next game, attendance,
    // substitute opportunities, unread count — all scoped server-side.
    if (view === "player_home") {
      // Skip the heavy player-home read while an opportunity detail is open —
      // renderPlayerHome short-circuits to the detail and discards playerHome.
      if (!oppDetailGame) playerHome = await getJSON("/api/me/player-home");
      oppDetail = oppDetailGame
        ? await getJSON(`/api/me/substitute-opportunities/${encodeURIComponent(oppDetailGame)}`)
        : null;
    }
    // The signed-in guardian's linked-junior surface (#26): each verified
    // junior's Player Home payload, plus a junior-scoped opportunity detail
    // when one is open. Same skip-the-list-while-detail-open shape as above.
    if (view === "guardian_home") {
      if (!gOpp) guardianHome = await getJSON("/api/me/guardian/home");
      gOppDetail = gOpp
        ? await getJSON(`/api/me/guardian/${encodeURIComponent(gOpp.jid)}/substitute-opportunities/${encodeURIComponent(gOpp.game_id)}`)
        : null;
    }
    // Notifications feed drives the bell badge on every view (#32).
    const nf = await getJSON("/api/notifications");
    if (nf && !nf.error) notifState = nf;
    // Pilot readiness checklist (#104), operator-only. Everything else the
    // card needs (app mode/store/delivery mode, demo data + fixture counts)
    // already rides on envStatus and the overview fetched above — this is
    // the one extra call, reusing /api/readiness's own check list rather
    // than re-deriving db/migration/admin/cookie posture on the client.
    // Kept as its own await (not bundled into Promise.all with the fetch
    // above): Promise.all rejects the whole group on one failure, which
    // would silently drop an otherwise-successful notifications refresh
    // whenever this operator-only endpoint has a hiccup.
    if (view === "readiness" && hasPerm("manage_setup")) {
      const rd = await getJSON("/api/readiness");
      readinessCheck = (rd && !rd.error) ? rd : null;
    }
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
    }
    // Calendar feed tokens: the Notifications tab's full manage UI, and (#146)
    // the Official inbox's calendar-subscription shortcut, both need the
    // signed-in user's own feed state.
    if (view === "notifications" || view === "inbox") {
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
    // Draft scheduler review (#86/#106), operator-only.
    if (view === "scheduler" && hasPerm("manage_schedule")) {
      // #386 — re-seed when the stored selection is no longer one the ACTIVE
      // tuple offers, not only when it is empty. `schedulerState.division` is
      // module-level and survives a context switch, while `ov.divisions` is
      // narrowed to the active Program/Season/League. Keeping a stale id would
      // leave the picker rendering a valid-looking Division of the NEW context
      // while Generate/Commit still sent the OLD one — which the backend now
      // refuses as not-found (it is a foreign hierarchy), so the screen and the
      // request would disagree with no way for the operator to see why. Before
      // this endpoint was bound, that same stale id silently returned the other
      // Program's proposal, which is the defect itself.
      const offered = ov.divisions || [];
      if (!offered.some((d) => d.id === schedulerState.division)) {
        schedulerState.division = offered[0] ? offered[0].id : null;
        schedulerState.preview = null;
      }
      const dr = await getJSON("/api/scheduler/drafts");
      const drafts = (dr && dr.draft_games) || [];
      const previousDrafts = schedulerState.drafts;
      schedulerState.drafts = drafts;
      schedulerState.summary = (dr && dr.summary) || null;
      reconcileDraftSelection(drafts, previousDrafts);
    }
    // Import wizard (#96): bind the season picker to the ACTIVE #159
    // Season, not "the first Season that happens to exist" (#331 review
    // round 7). When that rule was written `ov.seasons` was unfiltered
    // (every Program), so the old fallback was a silent, COMMITTABLE
    // cross-Program default: needsSeason import types send seasonId
    // verbatim to commit_import, and #159's context was display-only, not
    // a backend filter, so nothing else would have caught it. #369 has
    // since made `ov.seasons` the ACTIVE Season only, which independently
    // closes the cross-Program half — but this binding stays load-bearing:
    // goToSetupWorkflow("import") still does no seeding of its own (it
    // only switches tabs), so without it the picker is simply unseeded.
    // Re-binds on
    // ANY context revision, not just the first visit, since the operator
    // can reach Import once and then switch Program via the switcher
    // while still on this view. Fails CLOSED, not to a fresh global
    // default, when no Season is actively selected (a Program-only
    // context) — importCommitState() already refuses to enable Commit
    // without a real importState.seasonId, and renderImport()'s own
    // season <select> below renders no `selected` option in that case,
    // so a native browser default can't silently stand in for one either.
    // A stale Season also makes any ALREADY-validated report/committed
    // result suspect (it was reviewed by a person looking at a different
    // Program), so those are invalidated here too, not just re-seeded —
    // the operator must re-validate against whatever Season they land on
    // under the new context before Commit can enable again.
    if (view === "import" && importState.contextRevision !== contextRevision) {
      importState.seasonId = (contextOptions && contextOptions.selected
        && contextOptions.selected.season_id) || null;
      importState.report = null;
      importState.validatedKey = null;
      importState.committed = null;
      importState.contextRevision = contextRevision;
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
      // Guardian↔junior links (#35) — same view, same operator. Player names
      // come from the same /api/players call the Setup Players card uses
      // (#114); manage_users implies manage_setup for every role that holds
      // it today (only league admin), so this fetch never 403s in practice.
      const gl = await getJSON("/api/guardians/links");
      guardianLinksState = Array.isArray(gl) ? gl : [];
      const pl = await getJSON("/api/players");
      playersList = Array.isArray(pl) ? pl : [];
      // Officials pool for the create-account form's Official scope dropdown
      // (#135) — same data the game sheet's assign control already uses.
      const op = await getJSON("/api/officials");
      officialsPool = (op && op.officials) || [];
    }
    // Public surface (#83): schedule + standings from public-safe endpoints.
    if (view === "public") {
      const sch = await getJSON("/api/public/schedule");
      // A 5xx/non-JSON outage (now surfaced by getJSON as {error}) routes to the
      // shared "Could not load data" + Retry banner below, not an empty schedule.
      if (sch && sch.error) throw new Error(sch.error.message);
      publicState.schedule = sch || { fixtures: [], divisions: [] };
      if (!publicState.division && publicState.schedule.divisions[0]) {
        publicState.division = publicState.schedule.divisions[0].id;
      }
      if (publicTab === "standings" && publicState.division) {
        const st = await getJSON(`/api/public/standings/${publicState.division}`);
        if (st && st.error) throw new Error(st.error.message);
        publicState.standings = st;
      }
    }
    // Standings for the selected division (#31).
    if (view === "standings") {
      if (!standingsDivision || !ov.divisions.some((d) => d.id === standingsDivision)) {
        standingsDivision = ov.divisions[0] ? ov.divisions[0].id : null;
      }
      standings = standingsDivision
        ? await getJSON(`/api/standings/${standingsDivision}`) : null;
      if (contextRevision !== myRenderContext) return;  // #367: superseded, a fresh render() is already coming
    }
    // The Dashboard shows a standings snapshot for the first division.
    if (view === "dashboard" && ov.divisions[0]) {
      standings = await getJSON(`/api/standings/${ov.divisions[0].id}`);
      if (contextRevision !== myRenderContext) return;  // #367: superseded, a fresh render() is already coming
    }
    // Home/Tasks hub setup-progress card (#330) — only for a role that can
    // act on Setup (League Admin/Arena Manager); a Coach also lands on
    // "dashboard" (canSeeOpsConsole) but has nothing to do with this. The
    // fetch itself happens independently, in loadSetupProgressCard() below
    // (#331 review round 2 finding 3) — not awaited inline here, so it
    // never blocks the rest of the Dashboard from painting. Nothing is
    // cleared here any more (#365): the card's own store entry is replaced by
    // that load under its own identity, and readCardState() already labels a
    // held model whose tuple has moved as stale, so blanking it from here
    // would only discard data the stale contract says to keep showing.
  } catch (e) {
    setChrome(ov);
    c.innerHTML = `<div class="banner alert"><h2>Could not load data</h2>
      <p>The backend may not be running. ${esc(e.message || e)}</p></div>
      <div class="actions"><button class="act primary" id="retry-btn">Retry</button></div>`;
    const retry = document.getElementById("retry-btn");
    if (retry) retry.onclick = () => render();  // no inline handler (CSP)
    // #365 review round 11: this pass CONCLUDED the destination too -- into a
    // failure, but conclusively: the tree is gone, the banner is what stands,
    // and no control the deep link was promising can appear without another
    // render (the Retry above). So a standing focus intent resolves here
    // rather than waiting for a render that is not coming, and its fallback
    // lands on this banner's own <h2>. Reported for every intent
    // (`setupHierarchy` included), because the failure is the destination's,
    // not one endpoint's.
    settleDestinationFocus({ setupHierarchy: true });
    return;
  }

  setChrome(ov);
  updateNotifBadge();
  // Keep the header demo control's Load↔Reset label in step with the actual
  // data (#215): the demo boots empty and can be populated by the demo Load, a
  // manual build, or an import — all of which flow through render(), whereas
  // envStatus.demo_empty is only refetched on session/lifecycle changes. The
  // overview is authoritative for what leagues/teams exist right now.
  if (isDemo() && envStatus) {
    envStatus.demo_empty = !(ov.leagues || []).length && !(ov.teams || []).length;
  }
  renderDemoMenu();
  renderContextSwitcher();  // active Program/Season switcher (#159), state-aware
  // Roster/Sheet expose private player data — a signed-in user outside the
  // game's scope gets a 403 (#73). Show a clear "restricted" state instead of
  // the generic backend-error banner.
  if (["roster", "sheet"].includes(view) && lineups && lineups.error) {
    // #345 review: role="alert" so this is announced without requiring
    // focus, AND focus moves onto the heading as the destination a nav
    // click just asked for -- but only the FIRST time this state is
    // entered. Every other cause of a render() re-entry (a toast update,
    // an unrelated poll) rebuilds this exact banner too; re-focusing and
    // re-announcing it each time would be a spurious repeat, not a new
    // status. Checked against the OLD DOM, before it is replaced below.
    const alreadyHere = !!(document.activeElement && document.activeElement.closest
      && document.activeElement.closest(".banner.neutral[role=alert] h2"));
    c.innerHTML = `<div class="banner neutral" role="alert"><h2>Restricted</h2>
      <p>${esc(lineups.error.message
        || "You don't have access to this game's roster.")}</p></div>`;
    if (!alreadyHere) {
      const h = c.querySelector(".banner.neutral h2");
      if (h) { h.setAttribute("tabindex", "-1"); h.focus(); }
    }
    // A concluded pass, on a view no deep-link intent of ours targets. No
    // proof is offered: the `view` check inside cancels any standing intent
    // outright (the operator is on Roster/Sheet, not the Setup tree), which
    // is the right answer and not the fallback one.
    settleDestinationFocus(null);
    return;
  }
  // Per-card loading boundary (#331 review round 2 finding 3): the card's
  // own fetch is NOT part of this render() cycle's await chain (see
  // loadSetupProgressCard()) -- paint an immediate loading skeleton into
  // its slot here, in step with the rest of the Dashboard, then let that
  // fetch fill the slot in on its own schedule.
  const showSetupCard = view === "dashboard"
    && (hasPerm("manage_setup") || hasPerm("manage_arena"));
  // role="status"/aria-live="polite" (#331 review round 5 finding 5): this
  // wrapper persists across every re-render of renderSetupProgressCard()'s
  // OWN output (loadSetupProgressCard() only ever replaces its innerHTML),
  // so it is the one stable point a screen reader can watch to have
  // loading -> success/blocked/error, and a later retry's own settle,
  // announced automatically. aria-busy starts true (a fetch is about to
  // start, per the loading skeleton painted on the same line) and
  // loadSetupProgressCard() clears it once real content lands.
  //
  // #365: the one exception to "always start from the skeleton" is a held
  // model whose context tuple has moved (readCardState returns STALE). That
  // is precisely the case the stale contract covers -- last-good data stays
  // visible, labelled as belonging to an earlier selection and with its own
  // refresh path, while the fresh load for the new tuple runs -- instead of
  // being blanked to a skeleton that says nothing. Every other case (no held
  // model at all, or one for the tuple that is still active) starts from the
  // loading state exactly as before, so an ordinary reload never shows one
  // context's numbers under another's heading.
  const spInitial = readCardState(HOME_TASKS_CARD);
  // Re-checked here, not trusted from the pre-fetch decision alone: an early
  // return, the identity boundary's own DOM pass or a per-card repaint could
  // have taken the node out of #content in between, and emitting no slot
  // markup for a node that is no longer there would leave the card unpainted.
  carrySpSlot = carrySpSlot && !!spSlotNode && spSlotNode.parentNode === c;
  // `null` when this render paints no slot of its own — either because it is
  // carrying the live one, or because this view/role has no card at all.
  const spHtml = (view === "dashboard" && showSetupCard && !carrySpSlot)
    ? renderSetupProgressCard(spInitial.state === CARD_STATE.STALE
        ? spInitial : { state: CARD_STATE.LOADING })
    : null;
  const viewHtml =
    view === "dashboard" ? (spHtml !== null
        ? `<div id="sp-card-slot" role="status" aria-live="polite" aria-busy="true"
             >${spHtml}</div>` : "")
      + renderDashboard(ov, standings)
    : view === "setup" ? renderSetup(sv, hv, ov)
    : view === "import" ? renderImport(ov)
    : view === "calendar" ? renderCalendar(ov)
    : view === "games" ? renderGames(ov)
    : view === "roster" ? renderRoster(lineups, ov)
    : view === "sheet" ? renderGameSheet(lineups)
    : view === "inbox" ? renderInbox(inbox)
    : view === "player_home" ? renderPlayerHome(playerHome)
    : view === "guardian_home" ? renderGuardianHome(guardianHome)
    : view === "notifications" ? renderNotifications()
    : view === "delivery" ? renderDelivery(ov)
    : view === "users" ? renderUsers(ov)
    : view === "readiness" ? renderReadiness(ov)
    : view === "scheduler" ? renderScheduler(ov)
    : view === "standings" ? renderStandings(ov, standings)
    : view === "activity" ? renderActivity(board, ov)
    : renderPublic(ov);
  if (carrySpSlot) {
    // Everything except the carried live region is replaced around it. The
    // node itself is never detached and never re-serialized, so this render
    // writes nothing at all into #sp-card-slot -- the synchronous stale paint
    // that already ran stands, and the card's own load is what replaces it.
    Array.from(c.children).forEach((el) => { if (el !== spSlotNode) el.remove(); });
    spSlotNode.insertAdjacentHTML("afterend", viewHtml);
  } else {
    c.innerHTML = viewHtml;
  }
  // Keep the "what is currently painted into the live region" record honest,
  // since this is the one write to it that does not go through paintHomeCard():
  // the slot is created as part of #content's own markup here.
  if (spHtml !== null) homeCardPaintedHtml = spHtml;
  else if (!carrySpSlot) homeCardPaintedHtml = null;   // no slot on this surface
  // The themed confirm/blocked modal (#215) overlays whatever view is showing
  // (it can be opened from the header's Reset demo action too), so append it
  // after the view content on every render and wire it below.
  //
  // APPENDED, never `c.innerHTML += ...` (#365): the += form reads #content's
  // whole serialized markup and re-parses it, which destroyed and rebuilt
  // #sp-card-slot -- a polite live region -- with byte-identical content
  // immediately after the paint above, on EVERY render. That is the same
  // sentence written to the same region twice in a row, which is exactly the
  // duplicate speech #365 forbids; it was invisible only because it was the
  // very next mutation. insertAdjacentHTML leaves every existing node alone.
  c.insertAdjacentHTML("beforeend", renderModal());

  wireModal(c);
  c.querySelectorAll("[data-goto]").forEach((b) => b.onclick = () => switchTab(b.dataset.goto));
  // Home/Tasks hub setup-progress card (#330): its own fetch, content, and
  // click-handler wiring all happen independently in loadSetupProgressCard
  // (#331 review round 2 finding 3) -- the slot painted above is only ever
  // the loading skeleton at this point, so there's nothing of the card's
  // own to wire here yet.
  // ...with ONE exception (#365): a stale card painted above already carries
  // its own refresh control, which must work during the very window it is
  // on screen, before that load settles and rewires the slot itself.
  const spStaleRefresh = c.querySelector("[data-setup-progress-retry]");
  if (spStaleRefresh) spStaleRefresh.onclick =
    () => loadSetupProgressCard({ userInitiated: true });
  if (showSetupCard) loadSetupProgressCard();
  // Administration → Danger zone (#256): opens the guarded factory-reset modal.
  const frBtn = c.querySelector("[data-factory-reset]");
  if (frBtn) frBtn.onclick = () => { if (canFactoryReset()) startFactoryReset(); };
  // Public surface (#83): tab switch, division select, game detail, back.
  c.querySelectorAll("[data-public-tab]").forEach((b) => b.onclick = () => {
    publicTab = b.dataset.publicTab; publicState.game = null;
    publicState.feedUrl = null; publicState.feedLabel = null; render();
  });
  const pubDiv = c.querySelector("#public-div");
  if (pubDiv) pubDiv.onchange = () => {
    publicState.division = pubDiv.value;
    publicState.feedUrl = null; publicState.feedLabel = null;  // stale URL was for the OLD division (#33 review)
    render();
  };
  c.querySelectorAll("[data-public-game]").forEach((b) => b.onclick = async () => {
    const g = await getJSON(`/api/public/games/${b.dataset.publicGame}`);
    publicState.game = (g && !g.error) ? g : null; render();
  });
  const pubBack = c.querySelector("[data-public-back]");
  if (pubBack) pubBack.onclick = () => { publicState.game = null; render(); };
  wirePublicFeedSubscribe(c, render);
  wireCopyFeedUrl(c);
  // Setup sub-view toggle (#165): Hierarchy tree vs the record cards.
  c.querySelectorAll("[data-setup-view]").forEach((b) => b.onclick = () => {
    setupView = b.dataset.setupView; setupWorkflow = null; toast = ""; render();
  });
  // Setup drawers (#44): open, close, submit. A drawer opened from the
  // hierarchy tree (#165) can carry a data-prefill-field/value to preselect
  // the parent record (e.g. the venue for a new rink) in the drawer.
  c.querySelectorAll("[data-drawer]").forEach((b) => b.onclick = () => {
    drawer = { kind: b.dataset.drawer }; drawerError = ""; drawerValues = {};
    if (b.dataset.prefillField) drawerValues[b.dataset.prefillField] = b.dataset.prefillValue || "";
    if (b.dataset.prefillField2) drawerValues[b.dataset.prefillField2] = b.dataset.prefillValue2 || "";
    toast = ""; render();
  });
  // Edit a Player in place (#268): open the same drawer in edit mode, prefilled
  // from the record (Team locked — reassignment is its own action). The Player
  // list is the only editable entity today.
  c.querySelectorAll("[data-edit]").forEach((b) => b.onclick = () => {
    const p = (playersList || []).find((x) => x.id === b.dataset.editId);
    if (!p) return;
    drawer = { kind: b.dataset.edit, mode: "edit", id: p.id };
    drawerError = "";
    drawerValues = {
      "f-player-team": p.team_id || "",
      "f-player-name": p.name || "",
      "f-player-position": p.position || "",
      "f-player-shoots": p.shoots || "",
      "f-player-jersey": p.jersey_number == null ? "" : String(p.jersey_number),
      "f-player-email": p.email || "",
    };
    toast = ""; render();
  });
  // Empty-state "Load demo data" (#215): builds the sample dataset then refreshes.
  const demoLoad = c.querySelector("[data-demo-load]");
  if (demoLoad) demoLoad.onclick = async () => {
    const res = await post("/api/demo/load", {});
    if (res && res.error) return render();
    await afterDemoLifecycleChange("Sample demo data loaded.");
  };
  c.querySelectorAll("[data-drawer-close]").forEach((b) => b.onclick = () => {
    drawer = null; drawerError = ""; drawerValues = {}; render();
  });
  c.querySelectorAll("[data-drawer-submit]").forEach((b) => b.onclick = () => submitSetup(b.dataset.drawerSubmit));
  // Setup reassignment (#166): "⇄ Move" opens the confirm panel seeded with
  // the record's current parent; confirm posts the chosen parent.
  c.querySelectorAll("[data-reassign]").forEach((b) => b.onclick = (e) => {
    // Some ⇄ Move buttons live inside a <summary>; stop the click from also
    // toggling the parent <details> open/closed.
    e.preventDefault();
    const [kind, parent] = b.dataset.reassign.split(":");
    pendingReassign = { kind, parent, id: b.dataset.rzId, name: b.dataset.rzName,
                        curId: b.dataset.rzCur || "", seasonId: b.dataset.rzSeason || "",
                        programId: b.dataset.rzProgram || "" };
    drawer = null; toast = ""; render();
  });
  c.querySelectorAll("[data-reassign-cancel]").forEach((b) => b.onclick = () => {
    pendingReassign = null; render();
  });
  c.querySelectorAll("[data-reassign-confirm]").forEach((b) => b.onclick = () => {
    const sel = c.querySelector("#reassign-target");
    commitReassign(sel ? sel.value : "");
  });
  // Season participation (#180, cut to v2 canonical #233 Slice B2b): register
  // a program team for a season under a chosen League (required) and optional
  // Division, change an existing registration's League and/or Division, or
  // remove it. Each posts to the v2 registration routes then re-renders; the
  // toast reflects the server's structured error (e.g. a team with scheduled
  // games can't be removed/reassigned).
  // IDs below are keyed by Season+League together, not League alone (#331
  // review round 3 finding 2): two Seasons sharing one League used to render
  // IDENTICAL element ids for their own "Register" add-rows, so querying by
  // League id alone returned whichever Season's row happened to come first
  // in DOM order -- silently reading (and submitting) the WRONG Season's
  // chosen team/league/division.
  c.querySelectorAll("[data-reg-add]").forEach((b) => b.onclick = async () => {
    const lvId = b.dataset.regAdd;
    const sid = b.dataset.regAddSeason;
    const key = `${sid}-${lvId}`;
    const team = c.querySelector(`#reg-team-${key}`);
    const league = c.querySelector(`#reg-league-add-${key}`);
    const div = c.querySelector(`#reg-div-add-${key}`);
    if (!team || !team.value) { toast = "Choose a program team to register."; toastIsError = true; return render(); }
    if (!league || !league.value) { toast = "Choose a league to register the team under."; toastIsError = true; return render(); }
    toast = "";
    const res = await post(`/api/v2/setup/seasons/${sid}/team-registrations`,
      { team_id: team.value, league_id: league.value, division_id: (div && div.value) || null });
    if (res && !res.error) toast = "Team registered for the season.";
    await render();
  });
  // A "Register" control's League select rescopes its own Division select to
  // the newly-chosen League's divisions (never guessing a same-named one).
  // data-reg-league-add already carries the same Season+League composite key
  // as the ids above (#331 review round 3 finding 2), so this stays scoped
  // to the exact same row even when another Season shares this League.
  c.querySelectorAll("[data-reg-league-add]").forEach((sel) => sel.onchange = () => {
    const key = sel.dataset.regLeagueAdd;
    const divSel = c.querySelector(`#reg-div-add-${key}`);
    if (!divSel) return;
    const divs = leagueDivisions[sel.value] || [];
    divSel.innerHTML = `<option value="">No division</option>${divs.map((d) => opt(d.id, d.name)).join("")}`;
  });
  // A registered team's row: the League select rescopes its paired Division
  // select the same way, so a League move always forces an explicit
  // re-pick of the Division rather than carrying over a now-foreign one.
  c.querySelectorAll("[data-reg-league-for]").forEach((sel) => sel.onchange = () => {
    const rid = sel.dataset.regLeagueFor;
    const divSel = c.querySelector(`#reg-div-${rid}`);
    if (!divSel) return;
    const divs = leagueDivisions[sel.value] || [];
    divSel.innerHTML = `<option value="">No division</option>${divs.map((d) => opt(d.id, d.name)).join("")}`;
  });
  c.querySelectorAll("[data-reg-save]").forEach((b) => b.onclick = async () => {
    const rid = b.dataset.regSave;
    const leagueSel = c.querySelector(`#reg-league-${rid}`);
    const divSel = c.querySelector(`#reg-div-${rid}`);
    const newLeague = leagueSel ? leagueSel.value : "";
    const newDiv = (divSel && divSel.value) || "";
    const origLeague = b.dataset.regOrigLeague || "";
    const origDiv = b.dataset.regOrigDiv || "";
    if (!newLeague) { toast = "Choose a league."; toastIsError = true; return render(); }
    const leagueChanged = newLeague !== origLeague;
    const divChanged = newDiv !== origDiv;
    if (!leagueChanged && !divChanged) { toast = "No changes to save."; return render(); }
    // Write-order logic (clear Division first when League is changing and one
    // is set, then assign-league, then assign-division) lives in the shared
    // saveRegistrationPlacement() helper — see its comment for why — so it's
    // not duplicated between this control and the repair row's Save below.
    const result = await saveRegistrationPlacement(rid, newLeague, newDiv, origLeague, origDiv);
    placementSaveToast(result, "Season registration updated.");
    await render();
  });
  c.querySelectorAll("[data-reg-remove]").forEach((b) => b.onclick = async () => {
    toast = "";
    const res = await post(`/api/v2/setup/season-team-registration/${b.dataset.regRemove}/remove`, {});
    if (res && !res.error) toast = "Team removed from the season.";
    await render();
  });
  // Allowed venues (#233 Slice E): grant/revoke a Venue's SeasonVenueAccess
  // for a Season, independent of any Venue-Program ownership.
  c.querySelectorAll("[data-va-add]").forEach((b) => b.onclick = async () => {
    const sid = b.dataset.vaAdd;
    const sel = c.querySelector(`#va-add-${sid}`);
    if (!sel || !sel.value) { toast = "Choose a venue to allow."; toastIsError = true; return render(); }
    toast = "";
    const res = await post(`/api/v2/setup/seasons/${sid}/venue-access`, { venue_id: sel.value });
    if (res && !res.error) toast = "Venue allowed for this season.";
    await render();
  });
  c.querySelectorAll("[data-va-revoke]").forEach((b) => b.onclick = async () => {
    toast = "";
    const res = await post(`/api/v2/setup/season-venue-access/${b.dataset.vaRevoke}/remove`, {});
    if (res && !res.error) toast = "Venue access revoked.";
    await render();
  });
  // Needs-assignment repair row (#233 B2b review): an invalid season
  // registration's League→Division cascade and Save/Remove — see
  // renderSetupHierarchy's regIssueRow for how these controls are built.
  // Distinct data-repair-* attributes keep this selector space separate from
  // Season participation's own data-reg-* controls above (no id collision in
  // practice — an issue registration is by construction excluded from the
  // valid tree — but kept distinct for clarity).
  c.querySelectorAll("[data-repair-league-for]").forEach((sel) => sel.onchange = () => {
    const rid = sel.dataset.repairLeagueFor;
    const divSel = c.querySelector(`#repair-div-${rid}`);
    if (!divSel) return;
    // Only reached with the NEWLY selected league, which is always one of
    // this issue's real season_leagues options — so leagueDivisions (built
    // from hv.programs[].seasons[].leagues[] during this same render pass)
    // is guaranteed to have it, even for a registration_league_not_in_season
    // issue whose ORIGINAL league_id might not be covered by it.
    const divs = leagueDivisions[sel.value] || [];
    divSel.innerHTML = `<option value="">No division</option>${divs.map((d) => opt(d.id, d.name)).join("")}`;
  });
  c.querySelectorAll("[data-repair-save]").forEach((b) => b.onclick = async () => {
    const rid = b.dataset.repairSave;
    const leagueSel = c.querySelector(`#repair-league-${rid}`);
    const divSel = c.querySelector(`#repair-div-${rid}`);
    const newLeague = leagueSel ? leagueSel.value : "";
    const newDiv = (divSel && divSel.value) || "";
    const origLeague = b.dataset.repairOrigLeague || "";
    const origDiv = b.dataset.repairOrigDiv || "";
    if (!newLeague) { toast = "Choose a league to repair this registration."; toastIsError = true; return render(); }
    const result = await saveRegistrationPlacement(rid, newLeague, newDiv, origLeague, origDiv);
    placementSaveToast(result, "Registration repaired — moved into the selected league/division.");
    await render();
  });
  c.querySelectorAll("[data-repair-remove]").forEach((b) => b.onclick = async () => {
    toast = "";
    const res = await post(`/api/v2/setup/season-team-registration/${b.dataset.repairRemove}/remove`, {});
    if (res && !res.error) toast = "Invalid registration removed from the season.";
    await render();
  });
  // Safe destructive delete (#215): a Delete control opens the themed confirm
  // modal; the modal's confirm handler (wireModal) posts the delete and shows
  // the dependency breakdown if the server refuses.
  c.querySelectorAll("[data-del]").forEach((b) => b.onclick = (e) => {
    // A delete control can sit inside a click target (e.g. an available slot
    // card that schedules on click), so don't let the click bubble to it.
    e.stopPropagation();
    modal = { type: "confirm-delete", kind: b.dataset.del,
              id: b.dataset.delId, name: b.dataset.delName };
    render();
  });
  // Deactivate/reactivate a Player (#270): open a confirmation, never toggle
  // outright — same "click opens a themed modal" convention as delete.
  c.querySelectorAll("[data-player-active]").forEach((b) => b.onclick = (e) => {
    e.stopPropagation();
    modal = { type: "player-active", id: b.dataset.playerActive,
              next: b.dataset.playerActiveNext === "1",
              name: b.dataset.playerActiveName };
    render();
  });
  // Cancel game (#215): committed/published fixtures are cancelled, not deleted.
  c.querySelectorAll("[data-game-cancel]").forEach((b) => b.onclick = (e) => {
    e.stopPropagation();
    modal = { type: "cancel-game", game_id: b.dataset.gameCancel,
              name: b.dataset.gameName };
    render();
  });
  // Season rollover (#180, cut to v2 canonical #233 Slice B2b): the
  // program/season pickers reset the selection and any stale result so the
  // preview always matches the chosen pair; commit gathers the checked teams
  // with their target League (required) and optional Division and posts to
  // the hardened v2 rollover route, then re-renders (which reloads season
  // registrations, so carried teams move into "already registered").
  const roProgram = c.querySelector("[data-rollover-program]");
  if (roProgram) roProgram.onchange = () => {
    rollover.programId = roProgram.value;
    rollover.fromSeasonId = ""; rollover.toSeasonId = ""; rollover.result = null;
    render();
  };
  const roFrom = c.querySelector("[data-rollover-from]");
  if (roFrom) roFrom.onchange = () => {
    rollover.fromSeasonId = roFrom.value;
    if (rollover.toSeasonId === roFrom.value) rollover.toSeasonId = "";
    rollover.result = null; render();
  };
  const roTo = c.querySelector("[data-rollover-to]");
  if (roTo) roTo.onchange = () => {
    rollover.toSeasonId = roTo.value; rollover.result = null; render();
  };
  const roAll = c.querySelector("[data-rollover-all]");
  if (roAll) roAll.onchange = () => {
    c.querySelectorAll("[data-rollover-pick]").forEach((cb) => { cb.checked = roAll.checked; });
    updateRolloverCommitState(c);
  };
  // Every checkbox/league/division change re-evaluates the commit gate in
  // place, so inline row errors and the button's enabled state track the
  // selection without a re-render dropping it (review #216). A row's League
  // select also rescopes its own Division select's options (never guessing a
  // same-named division under the newly-picked League).
  c.querySelectorAll("[data-rollover-pick]").forEach((cb) =>
    cb.onchange = () => updateRolloverCommitState(c));
  c.querySelectorAll("[data-rollover-league]").forEach((sel) => sel.onchange = () => {
    // #331 review round 19: scoped to THIS row -- two rows can share the
    // same permanent-league VALUE (see updateRolloverCommitState above),
    // so a value-based query would rescope the wrong row's Division select
    // whenever that happens.
    const row = sel.closest(".reg-row");
    const divSel = row && row.querySelector("[data-rollover-div]");
    if (divSel) {
      const divs = leagueDivisions[sel.value] || [];
      divSel.innerHTML = `<option value="">No division</option>${divs.map((d) => opt(d.id, d.name)).join("")}`;
    }
    updateRolloverCommitState(c);
  });
  c.querySelectorAll("[data-rollover-div]").forEach((sel) =>
    sel.onchange = () => updateRolloverCommitState(c));
  const roCommit = c.querySelector("[data-rollover-commit]");
  if (roCommit) {
    updateRolloverCommitState(c);  // set the initial disabled state
    roCommit.onclick = async () => {
      const selections = [];
      c.querySelectorAll("[data-rollover-pick]").forEach((cb) => {
        if (!cb.checked) return;
        // #331 review round 19: scoped to THIS row (never a value-matched
        // global query -- two rows can share the same league VALUE, see
        // updateRolloverCommitState above), and the outgoing selection now
        // names its SOURCE registration explicitly rather than leaving the
        // backend to guess which of a team's possibly-several source rows
        // this selection meant.
        const row = cb.closest(".reg-row");
        const league = row && row.querySelector("[data-rollover-league]");
        const div = row && row.querySelector("[data-rollover-div]");
        if (league && league.value) {
          selections.push({
            team_id: cb.dataset.rolloverTeam, registration_id: cb.dataset.rolloverPick,
            league_id: league.value, division_id: (div && div.value) || null });
        }
      });
      // Defensive: the button is disabled unless every checked team has a
      // League, but re-check before sending so an unassigned selection can
      // never reach the server.
      const checked = c.querySelectorAll("[data-rollover-pick]:checked").length;
      if (!selections.length || selections.length !== checked) {
        updateRolloverCommitState(c);
        return;
      }
      toast = "";
      rollover.result = await post(`/api/v2/setup/seasons/${rollover.toSeasonId}/roll-forward`,
        { from_season_id: rollover.fromSeasonId, selections });
      if (rollover.result && !rollover.result.error) toast = "Season rollover complete.";
      await render();
    };
  }
  // (#345) The unconditional `if (drawer) { firstField.focus(); }` that used
  // to live here has moved into syncOverlayFocus(), which is now the SINGLE
  // owner of dialog focus. Two reasons it could not stay:
  //   * it re-focused on EVERY render, not on open. render() rewrites
  //     #content wholesale, so an open drawer re-runs this on any state
  //     change -- dragging focus back to field 1 out from under an operator
  //     who had tabbed to field 4, and firing focus/blur/change on a
  //     data-bound input each time;
  //   * two independent focus owners race, and whichever runs last wins.
  // syncOverlayFocus() targets the dialog CONTAINER instead of a form
  // control, and only on the closed -> open transition (or to re-anchor
  // focus a re-render orphaned), so neither problem survives.
  // Setup workflow hub (#345 batch 2). Opening/closing a landing is pure view
  // state; the actions delegate to the SAME handlers the rest of Setup uses,
  // so a workflow landing can never drift from the behaviour of the control
  // it fronts.
  // Delegates to the SAME permission-aware transition the Facilities nav
  // destination uses, so a landing opened from the hub and one opened from the
  // sidebar are byte-for-byte the same state (#345 review).
  c.querySelectorAll("[data-setup-workflow]").forEach((b) => b.onclick = () => {
    openSetupWorkflowLanding(b.dataset.setupWorkflow || null);
  });
  // A landing's primary/secondary/tertiary actions -- the same wiring a
  // per-card repaint re-applies to the action set it just re-rendered.
  wireSetupLandingActions(c);
  // Per-card retry/refresh and the card-scoped confirmation (#365).
  wireSetupWorkflowCards(c);
  // A full repaint of #content just destroyed every focused node in it. A card
  // holding an unresolved write keeps its PENDING model through this render
  // (the serialization rule refused the commit) and must keep its focus too,
  // or an ordinary re-entry into the same destination strands a keyboard
  // operator on <body> with a write still outstanding.
  if (view === "setup") restorePendingCardWriteFocus();
  c.querySelectorAll("button[data-act]").forEach((b) => b.onclick = () => rosterAction(b.dataset.act, b.dataset.id));
  c.querySelectorAll(".seg[data-view]").forEach((b) => b.onclick = () => { gameView = b.dataset.view; toast = ""; render(); });
  c.querySelectorAll("[data-side]").forEach((b) => b.onclick = () => { rosterSide = b.dataset.side; toast = ""; render(); });
  // Roster game picker (#154): switching the game resets the per-game view
  // state (side, availability filter, the coach's fetched sub queues) so the
  // new game's roster never renders against the previous game's data.
  const rosterGameSel = c.querySelector("#roster-game");
  if (rosterGameSel) rosterGameSel.onchange = () => {
    currentGame = rosterGameSel.value;
    rosterSide = "home"; availFilter = "all";
    availSummary = null; subCandidates = null; addableSubs = null; rescheduleRequests = null;
    toast = ""; render();
  };
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
  // Reschedule request/approval workflow (#29).
  const reschedRequestBtn = c.querySelector("[data-resched-request]");
  if (reschedRequestBtn) reschedRequestBtn.onclick = async () => {
    toast = "";
    const res = await post(`/api/games/${currentGame}/reschedule/request`, {
      team_id: rosterTeamId, reason: val("resched-reason") });
    if (res && !res.error) toast = "Reschedule requested — the opponent has been notified.";
    await render();
  };
  c.querySelectorAll("[data-resched-respond]").forEach((b) => b.onclick = async () => {
    toast = "";
    const res = await post(
      `/api/games/${currentGame}/reschedule/${b.dataset.reschedRespond}/respond`,
      { accept: b.dataset.reschedAccept === "1" });
    if (res && !res.error) toast = b.dataset.reschedAccept === "1"
      ? "Accepted — awaiting league approval." : "Reschedule request rejected.";
    await render();
  });
  c.querySelectorAll("[data-resched-decide]").forEach((b) => b.onclick = async () => {
    toast = "";
    const approve = b.dataset.approve === "1";
    const res = await post(
      `/api/games/${currentGame}/reschedule/${b.dataset.reschedDecide}/decide`,
      { approve, new_ice_slot_id: approve ? val("resched-slot") : null });
    if (res && !res.error) toast = approve
      ? "Reschedule approved — game republished." : "Reschedule request denied.";
    await render();
  });
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
    if (!oid || !start || !end) { toast = "Pick a start and end time."; toastIsError = true; return render(); }
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
  // Player Home: jump to a game's roster (#107), same convention as
  // data-open-sheet above.
  c.querySelectorAll("[data-open-roster]").forEach((b) => b.onclick = () => {
    currentGame = b.dataset.openRoster; switchTab("roster");
  });
  // Player Home attendance actions (#107) — dedicated handlers (not the
  // shared rosterAction dispatcher, which keys off the global currentGame)
  // so acting from Home never depends on or mutates whatever game the
  // Roster/Sheet screens currently have in focus. ownPlayerId guards the
  // scope read at the call site, matching the rest of the file's
  // (currentUser && currentUser.scope) convention.
  const ownPlayerId = () =>
    (currentUser && currentUser.scope) ? currentUser.scope.player_id : null;
  const phConfirm = c.querySelector("[data-ph-confirm]");
  if (phConfirm) phConfirm.onclick = async () => {
    const pid = ownPlayerId();
    if (!pid || !playerHome || !playerHome.next_game) return;
    await post(`/api/games/${playerHome.next_game.game_id}/availability`,
      { player_id: pid, availability_status: "available" });
    await render();
  };
  const phBackout = c.querySelector("[data-ph-backout]");
  if (phBackout) phBackout.onclick = () => {
    if (!playerHome || !playerHome.next_game) return;
    checkoutConfirm = { game_id: playerHome.next_game.game_id };
    render();
  };
  const phConfirmCheckout = c.querySelector("[data-ph-confirm-checkout]");
  if (phConfirmCheckout) phConfirmCheckout.onclick = async () => {
    const pid = ownPlayerId();
    if (!pid || !checkoutConfirm) return;
    await post(`/api/games/${checkoutConfirm.game_id}/availability`,
      { player_id: pid, availability_status: "unavailable" });
    checkoutConfirm = null;
    await render();
  };
  const phCancelCheckout = c.querySelector("[data-ph-cancel-checkout]");
  if (phCancelCheckout) phCancelCheckout.onclick = () => {
    checkoutConfirm = null; render();
  };
  // Substitute opportunity detail + response (#110). The response actions hit
  // signed-in-player scoped routes, so no player_id is sent from the browser.
  c.querySelectorAll("[data-ph-view-opp]").forEach((b) => b.onclick = () => {
    oppDetailGame = b.dataset.phViewOpp; oppDetail = null; toast = ""; render();
  });
  const oppBack = c.querySelector("[data-opp-back]");
  if (oppBack) oppBack.onclick = () => { oppDetailGame = null; oppDetail = null; render(); };
  // The four player opportunity actions all POST to the same scoped route
  // family, toast on success, close the detail, and re-render (#110/#112).
  const oppAction = (attr, verb, okMsg) => {
    const btn = c.querySelector(`[${attr}]`);
    if (!btn) return;
    const gid = btn.getAttribute(attr);
    btn.onclick = async () => {
      toast = "";
      const r = await post(`/api/me/substitute-opportunities/${encodeURIComponent(gid)}/${verb}`, {});
      if (r && !r.error) { toast = okMsg; oppDetailGame = null; oppDetail = null; }
      await render();
    };
  };
  oppAction("data-opp-accept", "enroll", "You're enrolled as a substitute.");
  oppAction("data-opp-withdraw", "withdraw", "You've withdrawn from this opportunity.");
  oppAction("data-opp-accept-offer", "accept-offer", "Offer accepted — you're on the roster.");
  oppAction("data-opp-decline-offer", "decline-offer", "Offer declined.");

  // Guardian actions for a linked junior (#26). Every button carries the
  // junior's player_id ("jid|game_id") so the request is scoped to that
  // specific junior — a guardian may have several on screen — and the server
  // records the guardian as actor, the junior as subject. All routes are
  // /api/me/guardian/{jid}/... which re-check the verified link server-side.
  // querySelectorAll, not querySelector: a guardian may have several junior
  // cards on screen at once, each with its own "I'm In"/"Can't Play" button —
  // binding only the first would silently dead-button every other junior.
  c.querySelectorAll("[data-g-confirm]").forEach((btn) => btn.onclick = async () => {
    const jid = btn.getAttribute("data-g-confirm");
    const j = (guardianHome.juniors || []).find((x) => x.player_id === jid);
    if (!j || !j.next_game) return;
    await post(`/api/me/guardian/${encodeURIComponent(jid)}/games/${encodeURIComponent(j.next_game.game_id)}/availability`,
      { availability_status: "available" });
    await render();
  });
  c.querySelectorAll("[data-g-backout]").forEach((btn) => btn.onclick = () => {
    const jid = btn.getAttribute("data-g-backout");
    const j = (guardianHome.juniors || []).find((x) => x.player_id === jid);
    if (!j || !j.next_game) return;
    gCheckout = { jid, game_id: j.next_game.game_id };
    render();
  });
  const gConfirmCo = c.querySelector("[data-g-confirm-checkout]");
  if (gConfirmCo) gConfirmCo.onclick = async () => {
    if (!gCheckout) return;
    const { jid, game_id } = gCheckout;
    await post(`/api/me/guardian/${encodeURIComponent(jid)}/games/${encodeURIComponent(game_id)}/availability`,
      { availability_status: "unavailable" });
    gCheckout = null;
    await render();
  };
  const gCancelCo = c.querySelector("[data-g-cancel-checkout]");
  if (gCancelCo) gCancelCo.onclick = () => { gCheckout = null; render(); };
  // "View" opens the junior-scoped opportunity detail.
  c.querySelectorAll("[data-g-view-opp]").forEach((b) => b.onclick = () => {
    const [jid, game_id] = b.getAttribute("data-g-view-opp").split("|");
    gOpp = { jid, game_id }; gOppDetail = null; toast = ""; render();
  });
  const gOppBack = c.querySelector("[data-g-opp-back]");
  if (gOppBack) gOppBack.onclick = () => { gOpp = null; gOppDetail = null; render(); };
  // Offer accept/decline, both from the card and the detail. There may be
  // several accept/decline buttons on the list, so bind them all.
  const gOfferAction = (attr, verb, okMsg) => {
    c.querySelectorAll(`[${attr}]`).forEach((btn) => {
      btn.onclick = async () => {
        toast = "";
        const [jid, game_id] = btn.getAttribute(attr).split("|");
        const r = await post(
          `/api/me/guardian/${encodeURIComponent(jid)}/substitute-opportunities/${encodeURIComponent(game_id)}/${verb}`, {});
        if (r && !r.error) { toast = okMsg; gOpp = null; gOppDetail = null; }
        await render();
      };
    });
  };
  gOfferAction("data-g-accept-offer", "accept-offer", "Offer accepted — added to the roster.");
  gOfferAction("data-g-decline-offer", "decline-offer", "Offer declined.");
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
  // #375 — the configurable format. Recorded exactly like the Division
  // select above (no re-render): the value is read fresh by BOTH the
  // Generate and Commit handlers below, and the backend binds it into
  // draft_fingerprint, so changing it after a Generate makes Commit fail
  // preview_stale — already handled by the error branch, which clears the
  // stale preview and returns focus to Generate.
  const schedMeetings = c.querySelector("#sched-meetings");
  if (schedMeetings) schedMeetings.onchange = () => {
    schedulerState.meetings = Number(schedMeetings.value) || 1;
  };
  // #390 — the configurable turnaround, recorded exactly like the two selects
  // above (no re-render). It is an input to the backend's own regeneration at
  // Commit, and therefore bound into draft_fingerprint, so changing it after a
  // Generate makes Commit fail preview_stale — already handled by the error
  // branch below, which clears the stale preview and returns focus to
  // Generate.
  const schedTurnaround = c.querySelector("#sched-turnaround");
  if (schedTurnaround) schedTurnaround.onchange = () => {
    schedulerState.turnaround = Number(schedTurnaround.value) || 0;
  };
  const schedGen = c.querySelector("[data-sched-generate]");
  // #328 review round 8 finding 4 -- consume the flag set by the PREVIOUS
  // render's Commit error branch below, now that this render's fresh
  // content (with a live Generate button again) is in the DOM. A stale
  // preview always disables Commit and re-enables Generate, so this
  // control exists whenever the flag does.
  if (schedFocusGenerateAfterRender) {
    schedFocusGenerateAfterRender = false;
    if (schedGen) schedGen.focus();
  }
  if (schedGen) schedGen.onclick = async () => {
    toast = "";
    const res = await post("/api/scheduler/draft", {
      division_id: schedulerState.division,
      meetings_per_opponent: schedulerState.meetings,
      // #390 — ALWAYS sent, including the 0 an untouched control means.
      // Omitting the key entirely is what the pre-#390 screen did, and the
      // backend's default then made the whole capability unreachable: an
      // operator had no way to ask for a turnaround at all.
      constraints: { min_turnaround_minutes: schedulerState.turnaround },
    });
    schedulerState.preview = (res && !res.error) ? res : null;
    await render();
  };
  const schedCommit = c.querySelector("[data-sched-commit]");
  if (schedCommit) schedCommit.onclick = async () => {
    toast = "";
    // #328 review round 5 -- bind Commit to the exact preview on screen:
    // the backend re-derives its own proposal fresh at commit time and
    // refuses (rather than silently diverging from what was reviewed) if
    // this fingerprint no longer matches that fresh regeneration.
    const res = await post("/api/scheduler/commit", {
      division_id: schedulerState.division,
      // #375 — the format the PREVIEW was generated with, read off that
      // preview and not off the live select, exactly like the
      // draft_fingerprint below it. What Commit writes must be what is on
      // screen: the select is the input to the NEXT Generate, so an
      // operator who nudges it while reading a still-valid proposal must
      // not thereby redefine the batch they are about to commit (nor be
      // forced to regenerate an unchanged one).
      meetings_per_opponent: (schedulerState.preview
        && schedulerState.preview.meetings_per_opponent) || 1,
      // #390 — the turnaround the PREVIEW was generated with, echoed back by
      // the server on that proposal and read off it rather than off the live
      // select, for exactly the reason above: the select is the input to the
      // NEXT Generate. The commit's own regeneration takes this value, so
      // dropping it here would regenerate with no turnaround, produce a
      // different fingerprint, and refuse every legitimate commit as
      // preview_stale.
      constraints: {
        min_turnaround_minutes: (schedulerState.preview
          && schedulerState.preview.min_turnaround_minutes) || 0,
      },
      draft_fingerprint: schedulerState.preview && schedulerState.preview.draft_fingerprint,
    });
    if (res && !res.error) {
      schedulerState.preview = null;
      toast = `Committed ${res.created.length} draft game(s).`;
    } else if (res && res.error && res.error.details
               && (res.error.details.reason === "pairing_already_scheduled"
                   || res.error.details.reason === "preview_stale")) {
      // #328 review round 3 -- a concurrent commit already scheduled one
      // of this batch's pairings. post()'s generic toast surfaces
      // error.message alone (never error.details), so the backend builds
      // that message itself with both team names and the winning Game id
      // ("Team A vs Team B is already scheduled as Game G123 -- generate
      // a fresh preview...") -- nothing further to extract here.
      // #328 review round 5 -- a Game was created or cancelled somewhere
      // in the (possibly long) gap between Generate and this click,
      // silently changing what "missing" means; the backend's own
      // generic-but-actionable message ("Generate a fresh preview...")
      // is likewise complete on its own. Either way the reviewed preview
      // is now stale; clear it rather than leave a now-wrong proposal on
      // screen, so Commit cannot be retried without a fresh Generate.
      schedulerState.preview = null;
      // #328 review round 8 finding 4 -- render() below replaces #content
      // wholesale, so the just-focused Commit button is simply gone;
      // nothing otherwise moves focus anywhere, silently dropping a
      // keyboard user back to the document body even though the toast
      // (a live region OUTSIDE #content, so it survives) told them what
      // to do next. Move focus to Generate once the fresh content render
      // completes, below.
      schedFocusGenerateAfterRender = true;
    }
    await render();
  };
  // Review filters (#106) — re-render() like every other interaction in this
  // app; reconcileDraftSelection preserves the current selection across it.
  const schedFilterDiv = c.querySelector("#sched-filter-div");
  if (schedFilterDiv) schedFilterDiv.onchange = () => {
    schedulerState.filters.division = schedFilterDiv.value; render();
  };
  const schedFilterRink = c.querySelector("#sched-filter-rink");
  if (schedFilterRink) schedFilterRink.onchange = () => {
    schedulerState.filters.rink = schedFilterRink.value; render();
  };
  const schedFilterIssue = c.querySelector("#sched-filter-issue");
  if (schedFilterIssue) schedFilterIssue.onchange = () => {
    schedulerState.filters.issue = schedFilterIssue.value; render();
  };
  // Games list filters + expand/collapse (#152).
  const gamesF = (id, key) => {
    const el = c.querySelector(id);
    if (el) el.onchange = () => { gamesFilter[key] = el.value; render(); };
  };
  gamesF("#games-f-div", "division"); gamesF("#games-f-team", "team");
  gamesF("#games-f-rink", "rink"); gamesF("#games-f-status", "status");
  gamesF("#games-f-from", "from"); gamesF("#games-f-to", "to");
  const gamesClear = c.querySelector("#games-f-clear");
  if (gamesClear) gamesClear.onclick = () => {
    gamesFilter = { division: "all", team: "all", rink: "all", status: "all", from: "", to: "" };
    render();
  };
  c.querySelectorAll("[data-games-toggle]").forEach((b) => b.onclick = () => {
    const id = b.dataset.gamesToggle;
    if (gamesExpanded.has(id)) gamesExpanded.delete(id); else gamesExpanded.add(id);
    render();
  });
  // Per-game publish/discard selection (#106).
  c.querySelectorAll("[data-sched-pick]").forEach((el) => el.onchange = () => {
    const id = el.dataset.schedPick;
    if (el.checked) schedulerState.selected.add(id); else schedulerState.selected.delete(id);
    render();
  });
  const schedSelectAll = c.querySelector("[data-sched-select-all]");
  if (schedSelectAll) schedSelectAll.onclick = () => {
    schedulerState.selected = new Set((schedulerState.drafts || []).map((g) => g.game_id));
    render();
  };
  const schedSelectClean = c.querySelector("[data-sched-select-clean]");
  if (schedSelectClean) schedSelectClean.onclick = () => {
    schedulerState.selected = new Set((schedulerState.drafts || [])
      .filter((g) => !g.issues.length).map((g) => g.game_id));
    render();
  };
  const schedSelectNone = c.querySelector("[data-sched-select-none]");
  if (schedSelectNone) schedSelectNone.onclick = () => {
    schedulerState.selected = new Set(); render();
  };
  const schedPub = c.querySelector("[data-sched-publish]");
  if (schedPub) schedPub.onclick = async () => {
    if (!schedulerState.selected.size) return;
    toast = "";
    const ids = Array.from(schedulerState.selected);
    const res = await post("/api/scheduler/drafts/publish", { game_ids: ids });
    if (res && !res.error) toast = `Published ${res.published} game(s).`;
    ids.forEach((id) => schedulerState.selected.delete(id));
    await render();
  };
  const schedDis = c.querySelector("[data-sched-discard]");
  if (schedDis) schedDis.onclick = async () => {
    if (!schedulerState.selected.size) return;
    toast = "";
    const ids = Array.from(schedulerState.selected);
    const res = await post("/api/scheduler/drafts/discard", { game_ids: ids });
    if (res && !res.error) toast = `Discarded ${res.discarded} draft(s).`;
    ids.forEach((id) => schedulerState.selected.delete(id));
    await render();
  };
  // Pilot onboarding import wizard (#96): switch type, validate, commit.
  c.querySelectorAll("[data-import-type]").forEach((b) => b.onclick = () => {
    importState.type = b.dataset.importType;
    importState.sheetsText = {};
    importState.report = null;
    importState.validatedKey = null;
    importState.committed = null;
    // #331 review round 10: importState.type is a small, reusable string (not
    // a monotonic counter like contextRevision), so switching away and back
    // to the SAME type before a stale Validate/Commit resolves would make
    // `importState.type !== type.key` coincidentally pass again -- and if the
    // freshly re-rendered sheets happen to hold the same text too (e.g. both
    // empty), importSnapshotKey() could coincidentally match as well. Bump
    // unconditionally so a type switch is always recognized as a fresh
    // operation, the same as it would be for the Ice Builder.
    importOperationSeq += 1;
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
    // #331 review round 10, same reasoning as the type switch above: loading
    // sample data resets the sheets to a fixed, reusable string, so a second
    // load (or a load that lands on content matching an earlier snapshot)
    // must still be treated as a fresh operation, not left to chance on
    // whether importSnapshotKey() happens to differ.
    importOperationSeq += 1;
    toast = "Sample data loaded — click Validate to preview it.";
    toastIsError = true;  // instructional, not a completed action — don't auto-clear
    render();
  };
  const importSeason = c.querySelector("#import-season");
  // #331 review round 11: a Commit captured for the PREVIOUSLY selected
  // Season can still be in flight when the operator switches to a different
  // Season in the same context -- contextRevision alone doesn't catch this
  // (no context change), so without an operation bump here the late response
  // would apply its result (or clear report/validatedKey) onto the NEWLY
  // selected Season's own UI even though the write it actually reports on
  // targeted the OLD Season. Validate's own dry-run body deliberately stays
  // season-agnostic (unchanged) -- this only invalidates RESPONSE ownership,
  // same as every other same-context operation-boundary event above.
  if (importSeason) importSeason.onchange = () => {
    importState.seasonId = importSeason.value;
    importOperationSeq += 1;
  };
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
    // Snapshot the context generation too (#331 review round 9): the checks
    // above only ever detected the SHEETS changing under a slow response,
    // never a context switch -- importState.report/validatedKey are cleared
    // at switch time (invalidateContextScopedMutations()), but nothing
    // stopped an ALREADY-IN-FLIGHT Validate that started before the switch
    // from landing afterward and reattaching a report/validatedKey the
    // operator never reviewed under the NEW context, silently re-enabling
    // Commit with no fresh B validation. type/text are deliberately still
    // checked too: a context switch clears report/validatedKey/committed
    // only, not sheetsText or the type selector, so either kind of
    // staleness needs its own check.
    const requestRevision = contextRevision;
    // A newer identical-input Validate click (#331 review round 10) changes
    // none of the checks above -- same type, same text, same context -- so
    // without its own token an OLDER response released after a NEWER one
    // could still overwrite it (e.g. the newer click's own request failed
    // and THIS one happens to succeed, silently re-enabling Commit against
    // a review the operator's own latest click already superseded).
    const requestOp = ++importOperationSeq;
    importState.committed = null;
    const res = await post("/api/import/dry-run", body);
    if (importState.type !== type.key || importSnapshotKey(type) !== requestKey
        || contextRevision !== requestRevision || requestOp !== importOperationSeq) return;
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
      toastIsError = true;
      return render();
    }
    const requestRevision = contextRevision;  // #331 review round 9, consistency with Validate above
    const requestOp = ++importOperationSeq;  // #331 review round 10, same reasoning as Validate above
    const body = buildImportBody(type);
    if (type.needsSeason) body.season_id = importState.seasonId;
    const res = await post(type.commitPath, body);
    // Same stale-response guard as Validate above — discard this response
    // if the sheets, the selected type, the context, or a newer operation
    // superseded it while the request was in flight, rather than showing a
    // commit result for content that's no longer what's on screen (#331
    // review round 9 added the context leg, round 10 the operation leg --
    // the commit ITSELF already went to whichever season_id this click
    // actually captured, so this only guards what the client does with the
    // RESPONSE, same as the others below it).
    if (importState.type !== type.key || importSnapshotKey(type) !== requestKey
        || contextRevision !== requestRevision || requestOp !== importOperationSeq) return;
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
  // Coach scope rebind (#266 remediation): send the picked team inside `scope`
  // to the audited rebind route; the server validates the team and records the
  // change. A re-render refetches the accounts list so the panel reflects the
  // new (now-valid) scope.
  const rebindBtn = c.querySelector("[data-rebind-scope]");
  if (rebindBtn) rebindBtn.onclick = async () => {
    const teamSel = c.querySelector("#rebind-team");
    const teamId = teamSel ? teamSel.value : "";
    if (!teamId) return;
    toast = "";
    const res = await post(`/api/accounts/${rebindBtn.dataset.rebindScope}/scope`,
                           { scope: { team_id: teamId } });
    if (res && !res.error) toast = "Coach team updated.";
    await render();
  };
  // Create-account form (#135): the Users tab re-renders on plenty of
  // OTHER actions too (viewing an account's sessions, revoking a session,
  // guardian-link create/verify) — every render() call replaces #content's
  // whole innerHTML, so without this, typing into the username field (or
  // picking a scope value) and then clicking something unrelated would
  // silently wipe it (self-review). Keep newAccountForm continuously
  // synced from the live DOM on every input, not just on submit/role-change.
  const acctUsername = c.querySelector("#new-account-username");
  if (acctUsername) acctUsername.oninput = () => { newAccountForm.username = acctUsername.value; };
  const acctScope = c.querySelector("#new-account-team, #new-account-player, #new-account-official");
  if (acctScope) acctScope.onchange = () => {
    const spec = NEW_ACCOUNT_SCOPE_FIELD[newAccountForm.role];
    if (spec) newAccountForm[spec.key] = acctScope.value;
  };
  // The role select needs its own change handler (not just the sync above)
  // so the role-specific scope dropdown (team/player/official) appears
  // immediately. Deliberately never captures the password into
  // newAccountForm/state (read fresh via val() only at actual submit
  // time) — but a re-render still replaces the password <input> with a
  // fresh, empty one, so restore whatever was already typed via a local
  // variable that lives only for this handler call, not via state, so
  // filling password before switching role doesn't silently lose it.
  const acctRoleSelect = c.querySelector("#new-account-role");
  if (acctRoleSelect) acctRoleSelect.onchange = async () => {
    const inProgressPassword = val("new-account-password");
    newAccountForm.role = acctRoleSelect.value;
    await render();
    const pwInput = document.getElementById("new-account-password");
    if (pwInput) pwInput.value = inProgressPassword;
  };
  const acctCreate = c.querySelector("[data-account-create]");
  if (acctCreate) acctCreate.onclick = async () => {
    const username = val("new-account-username");
    const password = val("new-account-password");
    const role = val("new-account-role");
    const scopeSpec = NEW_ACCOUNT_SCOPE_FIELD[role];
    const scopeValue = scopeSpec ? val(scopeSpec.inputId) : "";
    newAccountForm = {
      username, role,
      team_id: role === "coach" ? scopeValue : "",
      player_id: role === "player" ? scopeValue : "",
      official_id: role === "official" ? scopeValue : "",
    };
    if (!username || !role) {
      newAccountError = "Username and role are required.";
    } else if (!password) {
      newAccountError = "A temporary password is required.";
    } else if (scopeSpec && !scopeValue) {
      newAccountError = `Select a ${scopeSpec.label.toLowerCase()} for this role.`;
    } else {
      newAccountError = "";
      toast = "";
      const res = await post("/api/accounts",
        { username, password, role, scope: scopeSpec ? { [scopeSpec.key]: scopeValue } : {} });
      if (res && !res.error) {
        newAccountForm = { username: "", role: "", team_id: "", player_id: "", official_id: "" };
        usersSelected = res.id;
        toast = "Account created.";
      } else {
        newAccountError = (res && res.error && res.error.message) || "Could not create account.";
      }
    }
    await render();
  };
  // Guardian↔junior links (#35): create an unverified link, then verify one
  // with a real consent_method — the operator-facing consent record.
  const glCreate = c.querySelector("[data-glink-create]");
  if (glCreate) glCreate.onclick = async () => {
    guardianLinkForm = { guardian_user_id: val("glink-guardian"), player_id: val("glink-player") };
    toast = "";
    const res = await post("/api/guardians/links", guardianLinkForm);
    if (res && !res.error) toast = "Guardian link created — verify it below to grant authority.";
    await render();
  };
  c.querySelectorAll("[data-glink-verify]").forEach((b) => b.onclick = async () => {
    const linkId = b.dataset.glinkVerify;
    const consentMethod = val(`glink-consent-${linkId}`);
    toast = "";
    const res = await post(`/api/guardians/links/${linkId}/verify`,
      { consent_method: consentMethod });
    if (res && !res.error) toast = "Guardian link verified.";
    await render();
  });
  // Commit a move to the server, then show the outcome panel (with Undo).
  // Shared by confirmed moves, harmless direct moves, and undo (#43/#153).
  const commitMove = async (gid, slotId) => {
    toast = "";
    const res = await post(`/api/games/${gid}/move`, { ice_slot_id: slotId, reason: "Moved on arena calendar" });
    conflict = buildConflict(res, ov, gid, slotId);   // #43 side panel explains outcome + offers undo
    pendingMove = null; movingGameId = null;
    await render();
  };
  // Moving a published/locked game silently reverts it to Draft / unlocks the
  // roster (#153). Stage those and confirm first so a stray drag can't undo
  // game-day-ready state unnoticed; a harmless draft move commits immediately
  // so routine scheduling keeps its one-gesture flow. Both offer Undo after.
  const requestMove = (gid, slotId) => {
    const g = ov.schedule.find((x) => x.game_id === gid);
    const willUnpublish = !!(g && g.published);
    const willUnlock = !!(g && g.roster_status === "locked");
    if (willUnpublish || willUnlock) {
      pendingMove = { gid, slotId, willUnpublish, willUnlock };
      conflict = null; movingGameId = null; toast = ""; render();
    } else {
      commitMove(gid, slotId);
    }
  };
  // Tapping an Available slot: complete a pending move, else open the wizard.
  c.querySelectorAll("[data-slot]").forEach((b) => b.onclick = () => {
    if (movingGameId != null) return requestMove(movingGameId, b.dataset.slot);
    wizard = { slot_id: b.dataset.slot }; toast = ""; render();
  });
  // Enter move mode (drag-free path for touch/mobile/keyboard).
  c.querySelectorAll("[data-move-game]").forEach((b) => b.onclick = (e) => {
    e.stopPropagation();
    movingGameId = b.dataset.moveGame; conflict = null; pendingMove = null; toast = ""; render();
  });
  c.querySelectorAll("[data-move-cancel]").forEach((b) => b.onclick = () => { movingGameId = null; render(); });
  // Confirm / cancel a staged move, and undo a committed one (#153).
  c.querySelectorAll("[data-move-confirm]").forEach((b) => b.onclick = () => {
    if (pendingMove) commitMove(pendingMove.gid, pendingMove.slotId);
  });
  c.querySelectorAll("[data-move-cancel-pending]").forEach((b) => b.onclick = () => { pendingMove = null; render(); });
  c.querySelectorAll("[data-move-undo]").forEach((b) => b.onclick = () => {
    if (conflict && conflict.undo) commitMove(conflict.undo.gid, conflict.undo.oldSlotId);
  });
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
    if (v === 0) calendarDate = todayISO();
    else if (calendarMode === "month") calendarDate = addMonths(calendarDate, v);
    else shiftDate(v * (calendarMode === "week" ? 7 : 1));
    toast = ""; conflict = null; movingGameId = null; pendingMove = null; render();
  });
  c.querySelectorAll("[data-mode]").forEach((b) => b.onclick = () => { calendarMode = b.dataset.mode; toast = ""; conflict = null; movingGameId = null; pendingMove = null; render(); });
  // Month-view day cell -> open that day in Day view (#158).
  c.querySelectorAll("[data-cal-day]").forEach((b) => b.onclick = () => {
    calendarDate = b.dataset.calDay; calendarMode = "day"; toast = ""; render();
  });
  // Ice Availability Builder (#158): open/cancel, preview, commit, exclusions.
  const ibOpen = c.querySelector("[data-ice-builder-open]");
  // Opening a builder bumps iceOperationSeq (#331 review round 10): a
  // Preview/Commit issued by a PRIOR builder instance -- canceled, then
  // this one opened fresh -- must never be mistaken for current just
  // because contextRevision hasn't changed (same context throughout) and
  // `iceBuilder` is non-null again by the time it resolves.
  if (ibOpen) ibOpen.onclick = () => {
    iceOperationSeq += 1;
    iceBuilder = { form: null, preview: null }; toast = ""; render();
  };
  const ibCancel = c.querySelector("[data-ib-cancel]");
  if (ibCancel) ibCancel.onclick = () => {
    iceOperationSeq += 1;
    iceBuilder = null; toast = ""; render();
  };
  // Any change to the template (season, rinks, weekdays, per-day times, dates,
  // buffers) INVALIDATES a shown preview, so Create can never post a form
  // edited after Preview — the create button re-reads the live form, and the
  // server rejects a mismatched fingerprint as the authoritative backstop. A
  // weekday toggle additionally re-renders to show/hide its per-day time row.
  const ibFormEl = c.querySelector(".ib-form");
  if (ibFormEl) ibFormEl.addEventListener("change", (e) => {
    if (!iceBuilder) return;
    if (e.target && e.target.id === "ib-excl") return;  // staging input, not the template
    const isWeekday = e.target.classList && e.target.classList.contains("ib-weekday");
    const hadPreview = !!iceBuilder.preview;
    // Bump unconditionally, not only when clearing an existing preview
    // (#331 review round 10): a Preview issued BEFORE this edit but still
    // in flight when it lands must be recognized as stale even if no
    // preview existed yet to clear -- otherwise it would still write its
    // now-outdated slots onto the freshly-edited form once it resolves.
    iceOperationSeq += 1;
    iceBuilder.form = readIceBuilderForm(c);
    if (hadPreview) { iceBuilder.preview = null; toast = ""; }
    if (hadPreview || isWeekday) render();
  });
  // #331 review round 11: text/date/time controls fire `input` continuously
  // WHILE FOCUSED but don't fire `change` until blur -- a Preview/Commit held
  // in flight across an edit the operator hasn't blurred yet would still see
  // iceOperationSeq unchanged (only `change` bumped it), so the stale
  // response passes the guard above and render() replaces what's live in the
  // focused field with the old value readIceBuilderForm() captured at click
  // time. Bump the op token on every keystroke too, so that response is
  // discarded before it ever reaches render() -- and if a preview panel is
  // already showing, drop its DOM node DIRECTLY rather than calling the full
  // render() this listener's own edit is trying to avoid mid-keystroke
  // (matches the drawer-removal idiom from round 8: a full render() here
  // would also rebuild the very field the operator is still typing in and
  // steal focus/cursor out from under them).
  if (ibFormEl) ibFormEl.addEventListener("input", (e) => {
    if (!iceBuilder) return;
    if (e.target && e.target.id === "ib-excl") return;  // staging input, not the template
    iceOperationSeq += 1;
    iceBuilder.form = readIceBuilderForm(c);
    if (iceBuilder.preview) {
      iceBuilder.preview = null;
      const pv = c.querySelector(".ib-preview");
      if (pv) pv.remove();
    }
  });
  c.querySelectorAll("[data-ib-preview]").forEach((b) => b.onclick = async () => {
    // A context/identity switch invalidated (round 8) or fully closed
    // (identity switch, round 8) the builder between this button's last
    // render and this click -- see invalidateContextScopedMutations()'s and
    // resetTransientUiState()'s own comments. Guards the same way the form
    // `change` listener above already does.
    if (!iceBuilder) return;
    toast = "";
    // Snapshot the context generation (#331 review round 9): unlike the
    // guard above, this one covers the AWAIT below, not just the click.
    // iceBuilder.preview was previously assigned straight from the response
    // with no staleness check at all -- a held preview held across a switch
    // and then released restores stale (even other-context) slots into a
    // preview that looks live, re-enabling Create. Re-check `iceBuilder`
    // itself too: an identity switch nulls it wholesale, and a plain context
    // switch could in principle let a new builder exist by the time this
    // resolves.
    const requestRevision = contextRevision;
    // Snapshot the op token too (#331 review round 10): contextRevision
    // alone only catches a CONTEXT change, not e.g. canceling and
    // reopening the builder, editing the form, or a second Preview click
    // -- all same-context events that must also obsolete this one. Each of
    // those bumps iceOperationSeq at the point it happens; a mismatch here
    // means one of them happened while this request was in flight.
    const requestOp = ++iceOperationSeq;
    iceBuilder.form = readIceBuilderForm(c);
    const preview = await post("/api/setup/ice-availability/preview", iceBuilder.form);
    if (!iceBuilder || contextRevision !== requestRevision || requestOp !== iceOperationSeq) return;
    iceBuilder.preview = preview;
    render();
  });
  const ibCommit = c.querySelector("[data-ib-commit]");
  if (ibCommit) ibCommit.onclick = async () => {
    if (!iceBuilder) return;
    toast = "";
    // Bind the commit to the previewed template: send the fingerprint the
    // preview returned so the server refuses a form edited since (belt to the
    // frontend's suspenders, which already drops the preview on any edit).
    const fingerprint = iceBuilder.preview && iceBuilder.preview.template_fingerprint;
    // No preview to bind to -- a context switch cleared it since this button
    // was rendered (#331 review round 8: invalidateContextScopedMutations()
    // clears iceBuilder.preview the instant a switch is even attempted, well
    // before this button's own DOM node is removed by the next render). Bail
    // out client-side rather than sending a doomed request with a null
    // fingerprint and relying on the server's own rejection of it — the same
    // belt-and-suspenders Import's own Commit handler already applies below.
    if (!fingerprint) {
      toast = "The preview changed — refresh it before creating.";
      toastIsError = true;
      return render();
    }
    // Snapshot the context generation (#331 review round 9): the commit
    // itself is already correctly bound to whatever template this click
    // captured -- the fingerprint check above is the authoritative guard for
    // THAT, and the server independently rejects a mismatched fingerprint --
    // but the RESPONSE handling below reaches into the live `iceBuilder`,
    // including nulling it out on success or writing a fresh preview into it
    // on a mismatch. If a context or identity switch happened while this
    // request was in flight, the operator may already be looking at a
    // brand-new B-context builder by the time it resolves; without this
    // check an A-context commit's late response would wipe out that
    // in-progress B builder, attach an A-context re-preview onto it, or
    // crash outright against a `null` left by an identity switch.
    const requestRevision = contextRevision;
    // #331 review round 10, same reasoning as Preview above: contextRevision
    // alone doesn't catch a same-context cancel/reopen of the builder --
    // without this, a stale Commit success landing after the operator
    // canceled and reopened a fresh builder in the SAME context would still
    // pass the guard below and null out that brand-new builder out from
    // under them.
    const requestOp = ++iceOperationSeq;
    iceBuilder.form = readIceBuilderForm(c);
    const res = await post("/api/setup/ice-availability/commit",
      { ...iceBuilder.form, template_fingerprint: fingerprint });
    if (!iceBuilder || contextRevision !== requestRevision || requestOp !== iceOperationSeq) return;
    const reason = res && res.error && res.error.details && res.error.details.reason;
    if (res && !res.error) {
      toast = `Created ${res.totals.created} ice slot(s).`; iceBuilder = null;
    } else if (reason === "preview_mismatch") {
      // The proposal changed since Preview — a slipped-through form edit, or a
      // concurrent Season/timezone change moved the resolved slots. Refresh the
      // preview so the operator reviews the CURRENT slots before creating again;
      // never commit the stale set.
      toast = "The schedule changed since preview — showing the updated proposal. Review, then create again.";
      // Same guard around this second await (#331 review round 9/10) -- a
      // switch, cancel/reopen, or edit could just as easily land during the
      // re-preview as during the commit above. This re-preview counts as
      // its own fresh operation (a new ++, not a re-read of requestOp):
      // a genuinely independent Preview click racing it must still be able
      // to supersede it.
      const rePreviewRevision = contextRevision;
      const rePreviewOp = ++iceOperationSeq;
      const rePreview = await post("/api/setup/ice-availability/preview", iceBuilder.form);
      if (!iceBuilder || contextRevision !== rePreviewRevision || rePreviewOp !== iceOperationSeq) return;
      iceBuilder.preview = rePreview;
    } else {
      iceBuilder.preview = res;
    }
    render();
  };
  const ibExclAdd = c.querySelector("[data-ib-excl-add]");
  if (ibExclAdd) ibExclAdd.onclick = () => {
    if (!iceBuilder) return;
    iceBuilder.form = readIceBuilderForm(c);
    const el = c.querySelector("#ib-excl");
    const d = el && el.value;
    if (d && !iceBuilder.form.exclusion_dates.includes(d)) iceBuilder.form.exclusion_dates.push(d);
    iceBuilder.preview = null;   // exclusions changed the template — re-preview
    iceOperationSeq += 1;  // #331 review round 10, same reasoning as the form change listener above
    render();
  };
  c.querySelectorAll("[data-ib-excl-remove]").forEach((b) => b.onclick = () => {
    if (!iceBuilder) return;
    iceBuilder.form = readIceBuilderForm(c);
    iceBuilder.form.exclusion_dates =
      iceBuilder.form.exclusion_dates.filter((x) => x !== b.dataset.ibExclRemove);
    iceBuilder.preview = null;   // exclusions changed the template — re-preview
    iceOperationSeq += 1;  // #331 review round 10, same reasoning as the form change listener above
    render();
  });
  c.querySelectorAll("[data-filter]").forEach((sel) => sel.onchange = (e) => {
    const key = sel.dataset.filter;
    calFilters[key] = e.target.value;
    if (key === "venueId") calFilters.rinkId = "all";  // rink list depends on venue
    toast = ""; conflict = null; movingGameId = null; pendingMove = null; render();
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
      // Same confirm-if-destructive + move + #43/#153 panel as the click path.
      requestMove(gid, el.dataset.drop);
    });
  });
  const dismiss = c.querySelector("[data-conflict-dismiss]");
  if (dismiss) dismiss.onclick = () => { conflict = null; render(); };
  c.querySelectorAll("[data-publish]").forEach((b) => b.onclick = async () => { await post(`/api/games/${b.dataset.publish}/publish`, {}); toast = "Game published."; await render(); });
  c.querySelectorAll("[data-openroster]").forEach((b) => b.onclick = () => { currentGame = b.dataset.openroster; switchTab("roster"); });
  const picker = document.getElementById("player-picker");
  if (picker) picker.onchange = (e) => { pickedPlayer = e.target.value; toast = ""; render(); };
  // wizard wiring (#233 B2c: League required, Division optional)
  // #283 Slice D: the Exhibition toggle switches the game to a Season-scoped
  // friendly (crosses leagues, no standings), so it resets the competition
  // scope and team picks; a Season picker replaces the League/Division cascade.
  const wex = document.getElementById("w-exhibition");
  if (wex) wex.onchange = (e) => { wizard.exhibition = e.target.checked; wizard.league_id = ""; wizard.division_id = ""; wizard.home_id = null; wizard.away_id = null; render(); };
  const ws = document.getElementById("w-season");
  if (ws) ws.onchange = (e) => { wizard.season_id = e.target.value; wizard.home_id = null; wizard.away_id = null; render(); };
  const wl = document.getElementById("w-league");
  if (wl) wl.onchange = (e) => { wizard.league_id = e.target.value; wizard.division_id = ""; wizard.home_id = null; wizard.away_id = null; render(); };
  const wd = document.getElementById("w-div");
  if (wd) wd.onchange = (e) => { wizard.division_id = e.target.value; wizard.home_id = null; wizard.away_id = null; render(); };
  const wh = document.getElementById("w-home");
  if (wh) wh.onchange = (e) => { wizard.home_id = e.target.value; wizard.away_id = null; render(); };
  const wa = document.getElementById("w-away");
  if (wa) wa.onchange = (e) => { wizard.away_id = e.target.value; render(); };
  const wc = c.querySelector("[data-wizcancel]"); if (wc) wc.onclick = () => { wizard = null; render(); };
  const wcr = c.querySelector("[data-wizcreate]");
  if (wcr) wcr.onclick = async () => {
    let body;
    if (wizard.exhibition) {
      // #283 Slice D: an Exhibition friendly is Season-scoped with no owning
      // League/Division — it may pair teams from different Leagues and never
      // counts toward standings.
      body = { season_id: wizard.season_id, game_type: "exhibition",
               home_team_id: wizard.home_id, away_team_id: wizard.away_id,
               ice_slot_id: wizard.slot_id };
    } else {
      // v2: League is REQUIRED (game scope); Division is optional (#233 B2c).
      // season_id comes from the selected League, not a Division (which may be
      // unset when the game is league-only).
      const league = (ov.levels || []).find((lv) => lv.id === wizard.league_id);
      body = { season_id: league ? league.season_id : (ov.seasons[0] || {}).id,
               league_id: wizard.league_id, division_id: wizard.division_id || null,
               home_team_id: wizard.home_id, away_team_id: wizard.away_id,
               ice_slot_id: wizard.slot_id };
    }
    const res = await post("/api/v2/setup/game", body);
    if (res && !res.error) { toast = wizard.exhibition ? "Exhibition game scheduled." : "Game scheduled."; currentGame = res.id; wizard = null; view = "games"; }
    render();
  };
  // #345: last thing every render does -- after ALL wiring above, so the
  // dialog's controls exist and are bound before focus lands on one. Also
  // handles the close half: when the render that just ran removed the
  // dialog, focus returns to whatever opened it.
  syncOverlayFocus();
  // #365 review round 11: THE settlement, and the last thing of all. After
  // the paint and after every wiring pass, so a control this intent is
  // waiting for is in the document AND bound before focus reaches it; after
  // syncOverlayFocus() so the dialog lifecycle keeps its precedence (an
  // intent that finds an overlay open cancels rather than reaching into it).
  //
  // `hierarchyReadsSettled` is this PASS's own answer, never a module flag --
  // a pass that painted the Setup tree without having read for it proves
  // nothing, reports nothing, and leaves the intent alive for the pass that
  // did. That is the whole difference between waiting for an EVENT and
  // waiting out a CLOCK.
  settleDestinationFocus({ setupHierarchy: hierarchyReadsSettled });
}

// Per-view page title (#345). A single-page app never reloads, so without
// this every view reports the same static <title> -- screen-reader users get
// no announcement that the destination changed, and browser history/tab
// labels are useless. Format is "<View> — Hockey Scheduler": the view first,
// because assistive tech announces the beginning of the title and that is
// the part that actually changed.
const APP_TITLE = "Hockey Scheduler";
// #345 review: the title must follow the surface that is actually VISIBLE,
// not just the authenticated view. There are three shell states -- the
// authenticated console, the sign-in card, and the anonymous public portal --
// and only the first has a NAV view at all. Coupling the title to a
// successful authenticated render() left "Initial Setup — Hockey Scheduler"
// on screen after Sign out and on the public portal (reproduced), so
// assistive tech announced a destination the user had already left.
// Each shell entry point sets its own title SYNCHRONOUSLY, before any awaited
// load or early return.
function setShellTitle(state, v) {
  if (state === "login") { document.title = `Sign in — ${APP_TITLE}`; return; }
  if (state === "public") {
    document.title = `Public Schedule — ${APP_TITLE}`;
    return;
  }
  const label = NAV[v];
  document.title = label ? `${label} — ${APP_TITLE}` : APP_TITLE;
}
function setPageTitle(v) { setShellTitle("app", v); }

// ---- dialog focus management (#345) --------------------------------------
// The modal/drawer markup already carries role="dialog" aria-modal="true",
// but aria-modal alone does NOT constrain the keyboard -- browsers still tab
// out into the page behind. These three pieces complete the contract:
// remember what opened the dialog, move focus into it, cycle Tab inside it,
// and put focus back where it came from on close.
const OVERLAY_SEL = ".modal, .drawer";
const FOCUSABLE_SEL = [
  "a[href]", "button:not([disabled])", "input:not([disabled])",
  "select:not([disabled])", "textarea:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
].join(",");

function openOverlayElement() {
  // The LAST match wins: renderModal() is appended after the drawer, so a
  // confirm modal opened on top of a drawer is the one that owns focus.
  const all = document.querySelectorAll(OVERLAY_SEL);
  return all.length ? all[all.length - 1] : null;
}

function overlayFocusables(root) {
  return Array.from(root.querySelectorAll(FOCUSABLE_SEL)).filter((el) =>
    // offsetParent is null for display:none subtrees; a zero-size box is
    // also unreachable in practice. Cheaper and more reliable here than
    // checking computed visibility on every candidate.
    el.offsetParent !== null || el.getClientRects().length > 0);
}

// #345 focus lifecycle. The earlier attempt in this PR focused the dialog's
// first CONTROL from every render, which on these forms is a data-bound input
// -- the resulting focus/blur/change events destabilised the held-response
// journeys in home-tasks-hub.js. This version follows the reviewed shape and
// avoids that entirely:
//   * the trigger is captured BEFORE state mutation, by a capture-phase
//     listener that runs ahead of the opener's own click handler;
//   * each open dialog has a logical INSTANCE KEY derived from app state
//     (not from the DOM node, which innerHTML replaces on every render);
//   * focus moves ONCE per instance, onto the dialog CONTAINER
//     (tabindex="-1") -- never onto a form control, so no change events;
//   * the queued frame re-checks the key, so a dialog that closed or changed
//     before the frame ran never steals focus;
//   * on close, focus returns once to the captured trigger if it is still
//     connected and visible, otherwise to the view-level fallback.
// Two DISTINCT pieces of state, and the distinction is the whole fix:
//   overlayOpenKey   -- the instance observed open, recorded SYNCHRONOUSLY;
//   overlayFocusedKey -- the instance whose focus frame actually executed.
// Ownership used to be recorded only inside the queued frame, so an overlay
// closed before that frame ran left both null: the close branch below saw no
// owned instance, skipped the restore entirely, and focus stayed on <body>.
// The queued frame then found no overlay and returned. Recording the open
// synchronously means a close ALWAYS has an instance to restore for, whether
// or not its frame ever ran.
let overlayOpenKey = null;         // instance observed open (synchronous)
let overlayFocusedKey = null;      // instance whose focus frame actually ran
let overlayFocusHandle = 0;        // pending frame handle, so close can cancel
let overlayReturnFocus = null;     // element to restore to on close
let overlayReturnSelector = null;  // ...and how to re-find it after a re-render
let overlayFocusQueued = false;    // a frame is already pending

// Last element the user activated OUTSIDE any dialog. Capture phase, so it
// records the trigger before the opener's handler mutates `drawer`/`modal`.
let lastActivatedTrigger = null;
function noteTrigger(el) {
  if (el && el.closest && !el.closest(OVERLAY_SEL)) lastActivatedTrigger = el;
}
document.addEventListener("pointerdown", (e) => {
  const el = e.target && e.target.closest
    && e.target.closest("button, a[href], [role=\"button\"], input, select");
  noteTrigger(el);
}, true);
document.addEventListener("keydown", (e) => {
  if (e.key !== "Enter" && e.key !== " ") return;
  noteTrigger(document.activeElement);
}, true);

// Logical identity of the topmost open dialog. Derived from state so it is
// stable across the re-renders that replace the DOM node, and distinct for a
// modal opened OVER a drawer (renderModal is appended after renderDrawer, so
// the modal is topmost in both this key and openOverlayElement()).
function overlayKey() {
  if (modal) return `modal:${modal.type || ""}:${modal.id || ""}`;
  if (drawer) {
    return `drawer:${drawer.kind || ""}:${drawer.mode || ""}:${drawer.id || ""}`;
  }
  return null;
}

function isUsableTarget(el) {
  return !!(el && el.isConnected && typeof el.focus === "function"
    && (el.offsetParent !== null || el.getClientRects().length > 0));
}

// A selector that can re-find the trigger after a re-render.
//
// Holding the element alone is not enough. render() rewrites #content
// wholesale, so any trigger INSIDE the view -- a Setup card's "+ New", a
// row's delete control, a hub card's action -- is destroyed by the very
// render that paints the dialog. By the time the dialog closes the captured
// node is detached, so restore would fall through to the view heading for
// every in-view trigger and only ever really work for the topbar/sidebar
// controls that live outside #content. Triggers here are identified by id or
// by stable data-* attributes, so rebuild a selector from those.
//
// Attribute form for the id too (rather than "#id") so one escaping rule
// covers every case and no CSS.escape dependency is needed.
function triggerSelector(el) {
  if (!el || !el.tagName || !el.attributes) return null;
  const q = (v) => String(v).replace(/["\\]/g, "\\$&");
  const tag = el.tagName.toLowerCase();
  if (el.id) return `${tag}[id="${q(el.id)}"]`;
  const parts = [];
  for (let i = 0; i < el.attributes.length; i += 1) {
    const a = el.attributes[i];
    if (a.name.slice(0, 5) !== "data-") continue;
    parts.push(`[${a.name}="${q(a.value)}"]`);
  }
  return parts.length ? tag + parts.join("") : null;
}

// Prefer the original node when it survived; otherwise re-resolve the
// selector. A UNIQUE match is required: an ambiguous selector could land
// focus on a different row's control, which is worse than falling back to
// the view heading.
function resolveReturnFocus() {
  if (isUsableTarget(overlayReturnFocus)) return overlayReturnFocus;
  if (!overlayReturnSelector) return null;
  let matches;
  try {
    matches = document.querySelectorAll(overlayReturnSelector);
  } catch (err) {
    return null;  // an attribute value we failed to escape
  }
  if (matches.length !== 1) return null;
  return isUsableTarget(matches[0]) ? matches[0] : null;
}

// True when nothing meaningful holds focus: no activeElement, <body> (where
// the browser parks focus after the focused node is removed from the
// document), or a node that has since been detached.
//
// This is the whole basis for telling "the render orphaned focus" apart from
// "focus is deliberately somewhere else". render() rewrites #content
// wholesale on every pass, so an OPEN dialog's container is destroyed and
// rebuilt each time and focus falls back to <body> -- that case must be
// re-anchored or the dialog silently loses the keyboard. But when something
// real still holds focus (an operator typing in a drawer field, or the
// #ctx-select whose own change dismissed the drawer) moving it would be the
// bug, not the fix.
function focusIsOrphaned() {
  const a = document.activeElement;
  return !a || a === document.body || !a.isConnected;
}

// Focus the dialog CONTAINER, never a control inside it. The container
// carries tabindex="-1" from its markup; the setAttribute is a cheap
// belt-and-braces for any overlay that lacks it.
function focusOverlayContainer(overlay, key) {
  overlayFocusedKey = key;
  overlay.setAttribute("tabindex", "-1");
  overlay.focus();
}

function syncOverlayFocus() {
  const key = overlayKey();
  if (!key) {
    // Closed. Restore once, then clear. Gated on overlayOpenKey, NOT
    // overlayFocusedKey: an instance closed before its focus frame ran is
    // still an instance we owe a restore for.
    if (overlayOpenKey) {
      // Obsolete any frame still pending for the instance just closed, so it
      // cannot fire after the restore and move focus back into a dialog that
      // no longer exists.
      if (overlayFocusHandle) {
        cancelAnimationFrame(overlayFocusHandle);
        overlayFocusHandle = 0;
      }
      overlayFocusQueued = false;
      // Resolve BEFORE clearing -- resolveReturnFocus() reads both fields.
      const back = resolveReturnFocus();
      overlayOpenKey = null;
      overlayFocusedKey = null;
      overlayReturnFocus = null;
      overlayReturnSelector = null;
      // Only when the close actually orphaned focus. A context switch that
      // dismisses an open drawer from the #ctx-select the operator is still
      // sitting on must leave them there -- yanking focus back to the stale
      // trigger that opened the now-irrelevant drawer would strand them
      // somewhere they never navigated to.
      if (focusIsOrphaned()) {
        // A trigger removed by the very action that closed the dialog
        // (delete/confirm flows) is not restorable -- fall back to the view's
        // own heading rather than leaving focus on <body>.
        if (back) back.focus();
        else focusContentHeading();
      }
    }
    return;
  }
  if (key === overlayOpenKey) {
    // Same instance. Only RE-ANCHOR once its frame has actually focused the
    // container -- otherwise the pending frame still owns the first focus and
    // must not be pre-empted here.
    if (key !== overlayFocusedKey) return;
    // Same dialog instance, already focused once. The render that just ran
    // replaced its container node, so if focus was on the dialog (or on any
    // control inside it) it is now on <body>. Re-anchor to the rebuilt
    // container -- this is not a second "focus on open", it is keeping the
    // position the open transition already established. Still the container
    // and never a form control, so no focus/blur/change reaches a data-bound
    // input. Focus that is genuinely elsewhere is left untouched.
    if (focusIsOrphaned()) {
      const overlay = openOverlayElement();
      if (overlay) focusOverlayContainer(overlay, key);
    }
    return;
  }
  // Capture the return target only for the OUTERMOST open, so a modal opened
  // over a drawer still restores to whatever opened the drawer. Closing that
  // modal leaves the drawer open, which reads here as a new instance key and
  // lands focus back on the drawer container.
  if (!overlayOpenKey) {
    overlayReturnFocus = lastActivatedTrigger;
    overlayReturnSelector = triggerSelector(lastActivatedTrigger);
  }
  // Take ownership NOW, before awaiting a frame.
  overlayOpenKey = key;
  // A newly-observed instance supersedes any frame queued for the previous
  // one (e.g. a modal opening over a drawer), so cancel rather than skip --
  // "already queued" must not mean the new instance never gets focused.
  if (overlayFocusHandle) cancelAnimationFrame(overlayFocusHandle);
  overlayFocusQueued = true;
  overlayFocusHandle = requestAnimationFrame(() => {
    overlayFocusQueued = false;
    overlayFocusHandle = 0;
    // Re-check at fire time against the SYNCHRONOUS owner: the dialog may
    // have closed, or another overlay opened, between queueing and running.
    const nowKey = overlayKey();
    if (!nowKey || nowKey !== overlayOpenKey || nowKey === overlayFocusedKey) return;
    const overlay = openOverlayElement();
    if (!overlay) return;
    focusOverlayContainer(overlay, nowKey);
  });
}

// Tab/Shift+Tab cycle within the open dialog.
document.addEventListener("keydown", (e) => {
  if (e.key !== "Tab") return;
  const overlay = openOverlayElement();
  if (!overlay) return;
  const focusables = overlayFocusables(overlay);
  if (!focusables.length) { e.preventDefault(); return; }
  const first = focusables[0], last = focusables[focusables.length - 1];
  // ENTRY BOUNDARY. Focus is not on one of the dialog's sequential stops --
  // either it is outside the dialog entirely (the browser moved it there), or
  // it is on the tabindex="-1" CONTAINER, which is where every open and every
  // re-render re-anchor puts it and which overlayFocusables() deliberately
  // excludes. Enter at the appropriate edge in both cases.
  //
  // The container case used to fall through to the wrap rules below, and they
  // key off the first/last CONTROL, so neither ever matched and the event was
  // left unprevented. Forward looked correct by accident -- native sequential
  // navigation from a tabindex="-1" element goes to its first focusable
  // descendant -- but BACKWARD walked to whatever preceded the container in
  // the document, i.e. straight out of the dialog to the page behind it, on
  // the very first keystroke after open. `overlay.contains()` did not catch
  // it because Node.contains() is true for the node itself.
  if (focusables.indexOf(document.activeElement) === -1) {
    e.preventDefault();
    (e.shiftKey ? last : first).focus();
    return;
  }
  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault(); last.focus();
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault(); first.focus();
  }
});

// Per-view transient UI state that must NOT survive a destination change,
// factored out of switchTab() (#345 review) so every transition into a
// destination applies the identical discipline.
//
// This exists because it already drifted once: openSetupWorkflowLanding()
// deliberately bypasses switchTab() (which would clear the `setupWorkflow`
// half it is establishing), and in doing so it silently skipped ALL of the
// resets below — so navigating to Facilities from the Calendar carried a live
// Ice Builder/move/conflict across, and from Player or Guardian Home carried a
// pending checkout confirmation across. A stale overlay rendering over a
// different destination is exactly what these guards exist to prevent, so the
// rule now lives in one place both callers use rather than being re-derived.
//
// `next` is the destination's VIEW half; the conditions are unchanged from
// their original sites, including preserving an open drawer for `setup` (the
// topbar "+ Add Ice"/"＋ New" shortcuts set `drawer` and THEN switch to setup,
// so clearing it here would break them).
function resetTransientViewState(next) {
  if (next !== "calendar") {
    wizard = null; conflict = null; movingGameId = null; pendingMove = null;
    iceBuilder = null;
  }
  if (next !== "setup") {
    drawer = null; drawerError = ""; drawerValues = {}; pendingReassign = null;
  }
  // A pending checkout confirmation doesn't survive leaving Home (#107) —
  // so a stale "are you sure?" never reappears over changed attendance state.
  if (next !== "player_home") {
    checkoutConfirm = null; oppDetailGame = null; oppDetail = null;
  }
  // Same discipline for the guardian surface (#26): leaving "My Players"
  // clears any open junior checkout confirm / opportunity detail.
  if (next !== "guardian_home") { gCheckout = null; gOpp = null; gOppDetail = null; }
}

function switchTab(next) {
  view = next; toast = "";
  resetTransientViewState(next);
  // Clicking the top-level Setup destination always returns to the workflow
  // INDEX (#345 batch 2), never to whichever landing happened to be open last
  // -- same reset discipline the drawer/wizard state above gets, and for the
  // same reason: a nav click means "take me to Setup", not "resume where I
  // was three screens deep". Unconditional, so it also clears on re-entry.
  setupWorkflow = null;
  syncActiveNav(next);
  setPageTitle(next);
  render();
}

// Active-destination highlight, shared by switchTab() and
// openSetupWorkflowLanding() so the two transitions can never disagree.
//
// #345: two nav entries now carry data-tab="setup" — Administration's plain
// Setup (the workflow index) and Facilities (the "Venues, rinks and ice"
// landing). They are distinct destinations sharing one view, so matching on
// `data-tab` alone would light BOTH whenever either is open. The composite
// identity is (view, setupWorkflow), and a nav entry declares its own workflow
// half via data-setup-workflow-nav; an entry without that attribute means "no
// workflow", which is exactly the index's state.
function syncActiveNav(next) {
  const activeView = next || view;
  document.querySelectorAll(".tab").forEach((x) => {
    const wants = x.dataset.setupWorkflowNav || null;
    x.classList.toggle(
      "active",
      x.dataset.tab === activeView
        && (activeView !== "setup" || wants === (setupWorkflow || null)));
  });
}
document.querySelectorAll(".tab").forEach((b) => b.onclick = () => {
  // A nav entry that declares a workflow half is a composite destination and
  // goes through the shared landing opener; switchTab() would clear the very
  // state it needs. Everything else is an ordinary view switch.
  const workflow = b.dataset.setupWorkflowNav;
  if (workflow) { openSetupWorkflowLanding(workflow); return; }
  switchTab(b.dataset.tab);
});
// Topbar command actions (web shell) — outside #content, wired once.
document.querySelectorAll(".topbar [data-goto]").forEach((b) => b.onclick = () => switchTab(b.dataset.goto));
// Topbar shortcut: jump to Setup and open a create drawer directly (#44).
document.querySelectorAll(".topbar [data-open-drawer]").forEach((b) => b.onclick = () => {
  drawer = { kind: b.dataset.openDrawer }; drawerError = ""; drawerValues = {};
  switchTab("setup");
});
// Demo data control (#215): one database-icon button + dropdown whose contents
// depend on whether the demo is a clean slate — Load when empty; Reset + Clear
// when populated. Load runs immediately (non-destructive build); Reset/Clear
// open a typed-confirm modal. The button state is (re)painted by gateChrome via
// renderDemoMenu(); here we wire the click behaviour once.
const demoBtn = document.getElementById("demo-btn");
const demoDropdown = document.getElementById("demo-dropdown");
if (demoBtn) demoBtn.onclick = (e) => {
  e.stopPropagation();
  if (!hasPerm("manage_setup") || !isDemo()) return;
  demoMenuOpen = !demoMenuOpen;
  renderDemoMenu();
};
if (demoDropdown) demoDropdown.onclick = async (e) => {
  const item = e.target.closest("[data-demo-action]");
  if (!item) return;
  e.stopPropagation();
  demoMenuOpen = false;
  const action = item.dataset.demoAction;
  if (action === "load") {
    const res = await post("/api/demo/load", {});
    if (res && res.error) return renderDemoMenu();
    await afterDemoLifecycleChange("Sample demo data loaded.");
  } else {
    modal = { type: "demo-confirm", action };  // reset | clear
    renderDemoMenu();
    render();
  }
};
// A click anywhere else closes the dropdown.
document.addEventListener("click", () => {
  if (demoMenuOpen) { demoMenuOpen = false; renderDemoMenu(); }
});

// Paint the header demo control for the current state. Hidden entirely outside
// demo mode or without manage_setup.
function renderDemoMenu() {
  const menu = document.getElementById("demo-menu");
  const btn = document.getElementById("demo-btn");
  const dd = document.getElementById("demo-dropdown");
  if (!menu || !btn || !dd) return;
  const show = hasPerm("manage_setup") && isDemo();
  menu.hidden = !show;
  if (!show) { demoMenuOpen = false; dd.hidden = true; return; }
  const empty = isDemoEmpty();
  const primary = empty ? "Load demo data" : "Reset demo data";
  btn.innerHTML = ICONS.database;
  btn.title = primary;
  btn.setAttribute("aria-label", primary);
  btn.setAttribute("aria-expanded", demoMenuOpen ? "true" : "false");
  const items = empty
    ? [["load", "Load demo data"]]
    : [["reset", "Reset demo data"], ["clear", "Clear demo data"]];
  dd.innerHTML = items.map(([a, label]) =>
    `<button class="demo-item" role="menuitem" data-demo-action="${a}">${esc(label)}</button>`).join("");
  dd.hidden = !demoMenuOpen;
}

// -- active Program/Season context switcher (#159) -----------------------
// A structured, ENCODED hash (versioned JSON in URL-safe Base64URL — RFC 4648
// §5: +/ → -_, no "=" padding, so it needs no extra percent-encoding) rather
// than a plain "#ctx=program:season". It round-trips program/season without a
// fragile delimiter and coexists with the existing "#public" guest route
// (different prefix), which we never clobber.
function b64urlEncode(s) {
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
function b64urlDecode(s) {
  s = s.replace(/-/g, "+").replace(/_/g, "/");
  while (s.length % 4) s += "=";
  return atob(s);
}
// v2 (#345/#364) adds the League axis as `l`. A v1 hash (no `l` field at
// all, from a bookmark/share link minted before this change) still decodes
// correctly -- `l` reads as `undefined` -> `null` below, the exact "no
// League selected" first-class state -- so an old link is honored, not
// rejected. encodeContextHash always writes the current (v2) shape; only
// decode needs to understand both.
function encodeContextHash(programId, seasonId, leagueId) {
  try {
    return "#ctx=" + b64urlEncode(
      JSON.stringify({ v: 2, p: programId || null, s: seasonId || null, l: leagueId || null }));
  } catch (_) { return ""; }
}
function decodeContextHash(hash) {
  if (!hash || hash.indexOf("#ctx=") !== 0) return null;
  try {
    const o = JSON.parse(b64urlDecode(hash.slice(5)));
    if (!o || (o.v !== 1 && o.v !== 2) || !o.p) return null;
    return { program_id: o.p, season_id: o.s || null, league_id: o.l || null };
  } catch (_) { return null; }
}
// Reflect the current selection in the URL (replaceState, like the #public
// precedent) so a reload/bookmark restores it; never touch the #public route.
function syncContextHash() {
  if (!currentUser || location.hash === "#public") return;
  const sel = contextOptions && contextOptions.selected;
  const want = (sel && sel.program_id)
    ? encodeContextHash(sel.program_id, sel.season_id, sel.league_id) : "";
  writeContextHash(want);
}
// #369 root cause (reproduced deterministically by holding POST /api/context's
// RESPONSE while letting the request itself reach the server, at desktop AND
// 390px). syncContextHash() above can only ever run AFTER a switch's POST
// response is delivered, because it reads the POST echo out of
// contextOptions.selected. That leaves a window in which the SERVER has
// already accepted the new context while location.hash still encodes the OLD
// one -- and the whole window is exactly as long as that response takes to
// arrive (unbounded on a slow/mobile connection; this is why CI's phone leg
// was where it bit). A reload landing inside that window boots into
// restoreContextDeepLink(), which sees hash != persisted selection, cannot
// tell "my own hash has not caught up yet" from "someone handed me a deep
// link", and so applies the documented deep-link-wins rule to a STALE hash --
// POSTing the old context back and silently reverting a switch the user made
// and the server already took. Observed reverting request is
// restoreContextDeepLink()'s own three-key body {program_id, season_id,
// league_id}, the same fingerprint this bug left in venue-sharing.js.
//
// Fix: mirror the INTENDED selection into the hash immediately BEFORE the POST
// goes out (below, in sendContextSwitch), so the hash always leads or equals
// the server and can never lag it. A reload anywhere in the window then finds
// a hash describing what the user actually chose: if the POST already landed,
// hash == persisted and boot does nothing; if it did not, deep-link-wins
// re-applies the user's own intent instead of undoing it. Either way the
// switch converges on what was asked for. Deliberately NOT done in
// setActiveContext(): a switch that is merely QUEUED there may still be
// discarded by resetTransientUiState() on an identity change, and must not
// leave a phantom hash behind for the next identity's boot to adopt.
function writeContextHash(want) {
  if (!currentUser || location.hash === "#public") return;
  if (location.hash !== want) {
    history.replaceState(null, "", location.pathname + location.search + want);
  }
}
// Load the caller's AUTHORIZED options + current selection. Session-only; a
// signed-out user has no context.
//
// #369 review (root cause, reproduced via delayed /api/context/options
// responses, not just delayed requests): this is called from several
// independent places -- bootstrap()/signIn(), restoreContextDeepLink(), and
// BOTH branches of sendContextSwitch() -- and two of its GETs can genuinely
// be in flight at once (sendContextSwitch() only serializes the /api/context
// POSTs themselves via contextSwitchInFlight/contextSwitchQueued; that flag
// is already reset to false, and the NEXT switch's own POST+load already
// under way, well before THIS call's trailing GET here resolves). Without a
// sequence guard, whichever response happens to be DELIVERED last wins the
// unconditional assignment below, regardless of which call was issued last
// -- a slow, already-superseded load can clobber `contextOptions` with a
// stale selection AFTER a newer switch's load already set the correct one.
// sendContextSwitch()'s own `mySeq !== contextSwitchSeq` check catches this
// far too late: it only skips the stale call's OWN render()/hash-sync, not
// the assignment here, so the corruption sits silently in `contextOptions`
// until the next unrelated render() (a toast, a poll, any of this file's
// many other render() call sites) reads it and visibly reverts the switcher
// to the stale choice with no user action. Fixed with the same
// "only the latest-issued call may write" idiom already used for
// contextSwitchSeq/iceOperationSeq/importOperationSeq elsewhere in this
// file: bump a dedicated sequence BEFORE the await, and refuse to write
// `contextOptions` if a newer load has started since.
let contextOptionsLoadSeq = 0;
async function loadContextOptions() {
  if (!currentUser) { contextOptions = null; return; }
  const mySeq = ++contextOptionsLoadSeq;
  const o = await getJSON("/api/context/options");
  if (mySeq !== contextOptionsLoadSeq) return;  // superseded by a newer load
  contextOptions = (o && !o.error) ? o : null;
}
// Restore on load: the persisted context is already loaded above; if the URL
// carries a DIFFERENT context, adopt it — delegating the authorization decision
// to the backend (no client-side role logic). An unauthorized OR non-existent
// link both come back as the same generic not-found, which we normalize to the
// persisted context with a generic message (no existence oracle), then rewrite
// the hash to the resolved selection.
async function restoreContextDeepLink() {
  if (!currentUser) return;
  const link = decodeContextHash(location.hash);
  const sel = (contextOptions && contextOptions.selected) || {};
  if (!link
      || (link.program_id === sel.program_id
          && (link.season_id || null) === (sel.season_id || null)
          && (link.league_id || null) === (sel.league_id || null))) {
    syncContextHash();
    return;
  }
  const r = await post("/api/context",
    { program_id: link.program_id, season_id: link.season_id, league_id: link.league_id });
  if (r && !r.error) {
    // Re-fetch the whole option set (not just patch `selected`): the Season we
    // adopted may have been archived/reopened, or newly created/authorized,
    // between the options load above and this POST. A fresh GET reconciles the
    // canonical label/status/read-only rows AND the selection under the
    // backend's serializable snapshot, so `selected` is always one of the
    // rendered options with the correct badge — never a stale row.
    await loadContextOptions();
    toast = "";
  } else {
    // POST failed: the persisted context is unchanged, so the already-loaded
    // options still describe it. Normalize with a generic message (no oracle).
    toast = "That shared context isn't available — showing your saved context.";
    toastIsError = true;
  }
  syncContextHash();
}
// Make every context-scoped mutation control uncommittable the instant a
// context switch is even ATTEMPTED (#331 review round 8), not once its own
// POST resolves -- the native <select> already shows the new choice before
// setActiveContext()'s very first line runs, so a drawer submit, an Import
// Commit, or an Ice Builder Create landing in the gap before that POST
// settles must find nothing left to send, not race the network to get there
// first. Also called from resetTransientUiState() (a no-reload sign-out/
// sign-in/persona switch is the identity-bound flavor of the identical gap).
//
// Import's Commit handler already re-checks importState.validatedKey fresh
// against the live DOM at click time (pre-existing belt-and-suspenders, see
// its own comment below); clearing it here is enough on its own to make that
// handler bail out with no request sent, however the click landed. Ice
// Builder's Create gets the identical treatment (clearing iceBuilder.preview,
// the source of the fingerprint its own handler now refuses to commit
// without, below).
//
// An open drawer is different: submitSetup() does not re-verify anything
// about the drawer before posting, so the only airtight guard is removing
// its submit control from the DOM before a click can reach it -- directly,
// not via the full render() pipeline, which would also rebuild #ctx-select's
// own option list from the STILL-OLD contextOptions.selected (not updated
// until setActiveContext()'s POST succeeds) and visibly snap the switcher
// back to the prior choice for the duration of the round trip.
//
// importState.seasonId and iceBuilder.form are deliberately left alone here:
// round 7's existing contextRevision-mismatch check in render() and
// renderIceBuilder() already re-seeds both once the NEW canonical context is
// confirmed (setActiveContext()'s second bump, below) -- reseeding them here
// too would only seed from the STILL-OLD selection this function runs
// before that POST updates.
//
// #365 (owner): the drawer/Import/Ice-Builder trio was not the whole set. The
// Home/Tasks "Continue setup" CTA (`[data-setup-progress-action]`) and every
// button inside a Setup landing's `[data-setup-landing-actions]` container are
// context-scoped mutation entry points too, and neither was withdrawn here --
// so between the native <select> already showing Program B and the first
// repaint (three awaits later: /api/context, /api/context/options, and the
// next render's Setup reads), Program A's "Add Ice"/"Venues"/"Rinks" and A's
// "Continue setup" stayed enabled and keyboard-activatable, able to open a
// flow against a tuple changing underneath them.
//
// Withdrawn by REMOVAL, for the same reason the drawer's submit control is:
// disabling can be undone by any repaint that runs before reconciliation,
// while a removed control cannot receive pointer or keyboard activation
// however the event was already on its way. The containers themselves
// (`[data-setup-landing-actions]`, `#sp-card-slot`) are stable and survive,
// so the later repaint refills them rather than having to recreate structure.
//
// Removal alone is not sufficient, and is not the guarantee: any render or
// per-card repaint in the window would paint the controls straight back. The
// standing guarantee is contextSwitchIntentPending, which setupLandingActions()
// and renderSetupProgressCard() both consult -- this DOM pass only closes the
// gap before the first such paint.
function withdrawContextScopedActionControls() {
  document.querySelectorAll(
    "[data-setup-progress-action],"
    + "[data-setup-landing-actions] button,"
    + "[data-setup-card-ask],[data-setup-card-confirm-yes],[data-setup-card-confirm-no]"
  ).forEach((el) => el.remove());
}
function invalidateContextScopedMutations() {
  importState.report = null;
  importState.validatedKey = null;
  importState.committed = null;
  if (iceBuilder) iceBuilder.preview = null;
  withdrawContextScopedActionControls();
  if (drawer) {
    document.querySelectorAll(".drawer-scrim, .drawer").forEach((el) => el.remove());
    drawer = null; drawerError = ""; drawerValues = {};
  }
  // Belt-and-suspenders alongside the contextRevision bump the caller does
  // separately (#331 review round 10): a context switch is itself one of
  // the events that should obsolete any in-flight Validate/Preview/Commit,
  // same as an in-context edit or a newer request of the same kind does.
  iceOperationSeq += 1;
  importOperationSeq += 1;
}
// Coalescing queue for context-switch POSTs (#331 review round 9): every
// setActiveContext() call used to send its own /api/context POST
// immediately, so a rapid A->B->C could leave all three genuinely in flight
// at once. contextSwitchSeq (above) already makes the BROWSER ignore a
// superseded response, but each POST still reaches ContextService.set(),
// which persists ActiveContext as a plain unconditional last-write-wins
// with no generation/ordering guard at all -- whichever of the three
// requests the SERVER happens to finish processing last wins there,
// regardless of which one the browser ends up displaying. The operator
// could see C in the header while the server -- and everything that reads
// its OWN persisted context, starting with /api/v2/setup/progress -- is
// scoped to a completely different Program. Fixed by never letting more
// than one /api/context POST be in flight at once: a switch requested
// while one is already outstanding is queued (overwriting any earlier
// still-queued one, since only the LATEST intent is ever worth sending),
// and is sent immediately once the in-flight one settles, before that
// response gets any chance to reconcile anything. With at most one such
// POST ever in flight, the server's own last-write-wins persistence is
// trivially equivalent to "last intent wins" -- there is no window left
// for the two to disagree, on any backend.
let contextSwitchInFlight = false;
// #369: "the URL currently advertises an intent the server has NOT yet
// confirmed", tracked INDEPENDENTLY of the network-in-flight flag. The two are
// not the same window and conflating them left a hole: contextSwitchInFlight
// clears the instant the POST resolves, but on the FAILURE path the rejected
// intent stays in the hash across the awaited loadContextOptions()
// reconciliation. An identity change in that window saw the flag already
// false, left the hash alone, and the next identity's restoreContextDeepLink()
// adopted the previous identity's REJECTED choice -- persisting it silently
// whenever the new identity happened to be authorized for it. This flag stays
// set from the moment intent is published until a syncContextHash() has
// reconciled the URL with canonical truth, on EVERY success and failure path.
let contextHashIntentPending = false;
// #365, and structurally the SAME defect #369 fixed one layer over: "a
// context switch has been intended but not yet reconciled". contextHashIntentPending
// answers it for the URL; this answers it for every context-scoped ACTION
// CONTROL -- the Home/Tasks setup CTA and the six Setup landings' mutation
// buttons.
//
// Why it cannot be contextSwitchInFlight, and cannot be the cards' own state:
// contextSwitchInFlight clears the instant the POST resolves, while the
// repaint that rebinds those controls is still two awaits away
// (loadContextOptions, then render()'s Setup reads); and a card's CARD_STATE
// still describes the tuple the operator has LEFT, so no value of it can
// answer "may this control commit right now". Set SYNCHRONOUSLY at
// setActiveContext()'s first line -- before any await, before
// invalidateContextScopedMutations()'s own DOM pass -- and cleared only where
// a reconciliation has actually happened, which is every path that reaches a
// syncContextHash() (success AND failure), never on the merely-queued path.
let contextSwitchIntentPending = false;
let contextSwitchQueued = null;  // {programId, seasonId, leagueId, mySeq} -- the one pending switch not yet sent, if any

// Repaint the context-scoped cards from their HELD models, at the moment the
// POST has confirmed and contextOptions.selected has moved (#365 owner
// correction). readCardState() now answers STALE for every model still bound
// to the tuple the operator left, so this is what turns retained data from
// "silently presented as current, with live controls" into "explicitly
// labelled as earlier data, with only a Refresh". Runs BEFORE the awaited
// options/progress reads rather than after them, because that window is the
// whole defect: three awaits during which A's numbers sat under B's heading.
//
// Deliberately in-place rather than through render(): render() would rebuild
// #ctx-select from a contextOptions whose `programs` list has not been
// refreshed yet, and its own reads are exactly what this is trying not to
// wait for.
function repaintContextScopedCardsAsStale() {
  const slot = document.getElementById("sp-card-slot");
  if (slot) {
    // aria-busy stays true: a load for the NEW tuple is coming, and the card
    // on screen is not the answer to it.
    slot.setAttribute("aria-busy", "true");
    paintHomeCard(slot, renderSetupProgressCard(readCardState(HOME_TASKS_CARD)));
    const refresh = slot.querySelector("[data-setup-progress-retry]");
    if (refresh) refresh.onclick = () => loadSetupProgressCard({ userInitiated: true });
  }
  // `null` identity: this is not a response reconciling its own request, it
  // is the tuple itself moving, so there is no generation to check -- only
  // the held model and the new tuple, which readCardState() already compares.
  setupWorkflowsFor().forEach((w) => repaintSetupWorkflowCard(w.key, null));
}

// ============ THE IDENTITY BOUNDARY'S OWN DOM PASS (#365 round 8) ==========
// The TUPLE boundary above has a synchronous repaint. The IDENTITY boundary had
// none, and that was this round's defect. Verbatim from the review:
//
//   "setUser() adopts the new principal BEFORE those awaits.
//   resetTransientUiState() destroys/quarantines the JS stores and clears the
//   toast, but it does not synchronously repaint or blank the already-rendered
//   card DOM. With /api/context/options delayed, the new session can remain on
//   the departing principal's painted PENDING card and its permission-scoped
//   counts until the later render replaces it."
//
// Round 7 fixed the STORES and only the stores: cardStates destroyed, the
// ledger's held models overwritten with foreignCardWriteModel(), the live
// region emptied. Every one of those surfaces was ALREADY PAINTED, so what the
// arriving principal actually looked at did not change at all. A clean model
// standing behind a stale painted card is the same disclosure by the same
// route -- a rendered value standing in for an asserted one, inverted.
//
// IT CANNOT FETCH, and it cannot go through render(). It runs inside
// resetTransientUiState(), which setUser() calls BEFORE it assigns
// `currentUser` -- and everything after that point is an await (the options
// load, the deep-link reconciliation, and only then a render). Anything
// asynchronous here would sit inside the very window it exists to close.
//
// SO IT BLANKS RATHER THAN REPAINTS. Repainting would mean deriving a view for
// a principal nobody has identified yet from data that belongs to the one
// leaving -- and by the time this runs there is nothing left to derive from
// anyway. Empty discloses nothing, and it is short-lived: the render() that
// follows setUser() paints the arriving principal's own surfaces from their own
// reads, under their own permissions.
//
// WHAT IS BLANKED, and why each one is context- or permission-scoped:
//   * every context-scoped MUTATION CONTROL, by the same REMOVAL the tuple
//     boundary uses (withdrawContextScopedActionControls) -- disabling can be
//     undone by any repaint, a removed control cannot be activated however the
//     event was already on its way.
//   * the Home/Tasks card body (#sp-card-slot) -- workflow rows and completion
//     counts read under the DEPARTING principal's permissions.
//   * every Setup card slot ([data-setup-card-slot] -- the hub grid AND the
//     open landing, which is why this is a querySelectorAll and not one lookup)
//     -- counts, the blocker sentence, the server's error text, the departing
//     operator's typed reason inside an open confirmation, and the busy copy of
//     their own unresolved write.
//   * every landing ACTION container, emptied WHOLE rather than only of its
//     buttons: the withdrawn-action advice beside them is prose about the
//     departing principal's blocked state.
//   * every hub STATUS CHIP -- "Done"/"To do" is a permission-scoped assertion
//     about this Program made by the previous reader, and it lives in the card
//     header, outside the slot blanked above.
//   * the hub ROLL-UP / NEXT-TASK line -- the same counts, one derivation up.
//   * KEYBOARD FOCUS, when it is standing inside any of the above. Blanking
//     destroys the focused node, and "wherever the removal happened to drop it"
//     is not a place to hand a new principal: it is moved out deliberately,
//     before the removal, so the arriving principal starts from the document
//     rather than from inside a card that no longer exists.
//
// aria-busy stays TRUE on both card wrappers: this principal has not read these
// cards yet and a load for them is what comes next, so "idle" would be a lie
// told to assistive technology for the length of the window.
function blankContextScopedCardSurfaces() {
  withdrawContextScopedActionControls();
  const scoped = "#sp-card-slot,[data-setup-card-slot],[data-setup-landing-actions],"
    + "[data-setup-hub-progress-slot],[data-setup-workflow-card]";
  const active = document.activeElement;
  if (active && active !== document.body && active.closest
      && active.closest(scoped)) {
    active.blur();
  }
  const homeSlot = document.getElementById("sp-card-slot");
  if (homeSlot) {
    homeSlot.setAttribute("aria-busy", "true");
    paintHomeCard(homeSlot, "");
  }
  document.querySelectorAll("[data-setup-card-slot]").forEach((slot) => {
    slot.setAttribute("aria-busy", "true");
    slot.innerHTML = "";
  });
  document.querySelectorAll("[data-setup-landing-actions]")
    .forEach((box) => { box.innerHTML = ""; });
  document.querySelectorAll("[data-setup-hub-progress-slot]")
    .forEach((box) => { box.innerHTML = ""; });
  document.querySelectorAll("[data-setup-workflow-card] .swf-status,"
    + "[data-setup-workflow-card] .swf-optional")
    .forEach((chip) => chip.remove());
}

// Persist a switcher pick, then reflect it in the hash and re-render.
// `leagueId` is the third axis (#345/#364), additive like the backend's own
// resolve_with_league/set_with_league -- every existing two-argument call
// site (the Program/Season <select>'s own onchange) still means exactly what
// it did before: omitting it is `undefined`, which the POST below already
// normalizes to `null` (no League), never silently reinterpreted as "keep
// whatever League was active." Explicit no-League is likewise `null`, not
// omission, so a League select reset to "No League" clears it rather than
// leaving the previous League to survive a Program/Season-only switch.
async function setActiveContext(programId, seasonId, leagueId) {
  const mySeq = ++contextSwitchSeq;
  // FIRST statement, before every await and before the DOM pass below (#365
  // owner correction): from this instant no Setup landing action group and no
  // Home/Tasks setup CTA may be painted, whatever state any card is holding
  // and whatever repaint runs next. Cleared only once a reconciliation has
  // actually happened -- see the flag's own comment.
  contextSwitchIntentPending = true;
  // Invalidate SYNCHRONOUSLY, before anything below (#331 review round 8) --
  // see invalidateContextScopedMutations()'s own comment for why.
  // contextRevision bumps here too, not just after success further down:
  // contextSeededDrawerValues()'s own in-flight-hierarchy-fetch guard and
  // round 7's render()-time rebind checks both compare against this counter,
  // which must already read "changed" the instant a switch is ATTEMPTED, not
  // only once it's confirmed.
  invalidateContextScopedMutations();
  contextRevision += 1;
  // Focus work asked for under the tuple being LEFT dies here, before every
  // await and before the early return below (#365 owner correction, round 13).
  // Placing this after the /api/context response -- where the confirmed-switch
  // cancellation sits -- is one boundary too late: during a slow response the
  // old tuple still satisfies destinationFocusIntentCurrent(), so a standing
  // intent or a still-pending generic poll can fire onto a surface the
  // operator has already navigated away from in the native control. The early
  // return matters just as much: a switch that queues behind an in-flight one
  // would otherwise not reach any cancellation at all until its predecessor
  // settled.
  abandonFocusWorkForContextSwitch();
  if (contextSwitchInFlight) {
    contextSwitchQueued = { programId, seasonId, leagueId, mySeq };
    return;
  }
  await sendContextSwitch(mySeq, programId, seasonId, leagueId);
}
async function sendContextSwitch(mySeq, programId, seasonId, leagueId) {
  contextSwitchInFlight = true;
  // #369: publish the intent to the hash BEFORE the server can act on it, so
  // the hash never lags an already-mutated server. See writeContextHash().
  writeContextHash(encodeContextHash(programId, seasonId || null, leagueId || null));
  contextHashIntentPending = true;
  const r = await post("/api/context",
    { program_id: programId, season_id: seasonId || null, league_id: leagueId || null });
  contextSwitchInFlight = false;
  // A newer intent queued while this POST was in flight is strictly more
  // current than whatever this response says -- send it immediately, before
  // doing ANYTHING with this response (including reconciling a failure), so
  // the server only ever receives requests in the operator's own real order
  // (#331 review round 9). resetTransientUiState() clears this on an
  // identity change, so an old identity's pending switch can never fire
  // under a new one.
  if (contextSwitchQueued) {
    const next = contextSwitchQueued;
    contextSwitchQueued = null;
    return sendContextSwitch(next.mySeq, next.programId, next.seasonId, next.leagueId);
  }
  // Superseded some other way -- e.g. resetTransientUiState() bumped
  // contextSwitchSeq directly on an identity change while this POST (the
  // OLD identity's own) was still in flight.
  if (mySeq !== contextSwitchSeq) return;
  if (!r || r.error) {
    // Generic, no existence oracle (the backend returns the same not-found
    // whether it doesn't exist or isn't ours). Refresh options in case the
    // authorized set shifted underneath us. Everything invalidated above
    // STAYS invalidated -- reconciling the canonical context on failure must
    // never restore a stale enabled action (#331 review round 8), and must
    // converge on whatever the server actually has, never a context this
    // failed POST never got the server to accept (#331 review round 9).
    toast = "That Program/Season/League isn't available."; toastIsError = true;
    await loadContextOptions();
    if (mySeq !== contextSwitchSeq || contextSwitchQueued) return;
    // loadContextOptions() just proved canonical truth -- sync the hash to
    // it even on THIS failure path (#331 review round 10). This request's
    // own target was rejected, but that does not mean the hash is already
    // correct: if an EARLIER sibling in the same coalesced burst succeeded
    // (this one was queued behind it and its own success reconciliation
    // was skipped in favor of dequeuing straight to this one, per the
    // comment above), the server is already sitting on THAT sibling's
    // context while the hash still shows whatever was live before the
    // whole burst started. Without this, a reload runs
    // restoreContextDeepLink(), treats the stale hash as an intentional
    // deep link, and silently POSTs the persisted context back to it.
    syncContextHash();
    contextHashIntentPending = false;
    // #365 owner correction: "a failed switch may restore the still-current
    // tuple only through a fresh render." The tuple never moved, so nothing
    // went stale and nothing may be repainted as actionable in place -- the
    // withdrawal is released here and the render() below is the ONE thing
    // that puts the controls back, rebuilt from the tuple that is still
    // current rather than resurrected from the DOM they were removed from.
    contextSwitchIntentPending = false;
    render();
    return;
  }
  // Reflect the canonical selection in the hash IMMEDIATELY from the POST echo,
  // before the options refresh below. The refresh is a second round-trip; if we
  // waited until after it to sync the hash, a very fast reload in that window
  // could still observe a prior hash (e.g. the persisted default mirrored on
  // load) and adopt it over what we just persisted.
  if (contextOptions) {
    contextOptions.selected = { program_id: r.program_id,
      season_id: r.season_id, league_id: r.league_id, read_only: !!r.read_only };
  }
  // Second bump (#331 review round 8): the FIRST bump above only made the
  // PRIOR selection's mutations uncommittable at intent time, before
  // contextOptions.selected itself had changed. A SECOND bump, now that the
  // canonical NEW selection is known, is what makes the render() below
  // actually rebind Import/Ice Builder to it instead of finding its own
  // stamp already "current" (from the first bump) and skipping the reseed.
  contextRevision += 1;
  // NOT the primary invalidation, and must not be read as one (#365 owner
  // correction, round 13). Focus work is abandoned at the moment a switch is
  // ATTEMPTED -- see abandonFocusWorkForContextSwitch() at the top of
  // setActiveContext(). Doing it only here was one boundary too late: until
  // /api/context answers, contextOptions.selected still holds the OLD tuple,
  // so the old intent goes on satisfying destinationFocusIntentCurrent() and
  // a standing intent or in-flight poll could still fire while the operator
  // was already looking at their new selection in the native control.
  //
  // Kept because it is not merely a repeat: a switch QUEUED behind an
  // in-flight one is drained straight into this function, and focus work
  // started in the interval between queueing and draining has not passed the
  // attempt-time call. Both are cheap; neither alone covers every path.
  cancelSupersededDestinationFocus();
  // ...and the SAME sentence about the generic poll (#365 round 13). The
  // ticket below is the only binding focusContentHeading()'s chain has, and
  // until this line it was bumped by newer focus REQUESTS only -- so a poll
  // started under the tuple the operator has just left survived the switch
  // and could still take its #content landing on the arriving Season's
  // surface, which is the same stale-work-crossing-a-boundary defect the
  // intent above is dropped here to avoid. Bumping the ticket is the whole
  // cancellation: every in-flight chain compares against it on its next tick
  // and returns without focusing anything. Silent, not redirected -- nothing
  // is focused on the new surface on the old poll's behalf, and a
  // focusContentHeading() called AFTER this line (the render below, an
  // overlay close) takes a fresh ticket and keeps its floor in full.
  newFocusRequest();
  syncContextHash();
  // The tuple has now genuinely moved, so every held card model is bound to a
  // context the operator has left. Repaint them as explicitly STALE HERE --
  // before the awaited options reload below and the Setup reads after it --
  // rather than leaving A's counts standing under B's heading for the length
  // of two more round trips (#365 owner correction). The action controls stay
  // withdrawn throughout: contextSwitchIntentPending is still set, and STALE
  // withdraws them on its own account too.
  repaintContextScopedCardsAsStale();
  // Then reconcile the whole option set from a fresh GET so the label/status/
  // read-only badge reflect canonical state at POST time — a Season may have
  // been archived/reopened or newly authorized since options loaded. Rendering
  // from the pre-POST rows would show a stale row (a now-archived Season shown
  // as writable, or `selected` absent from the offered options). Re-sync after,
  // in case the canonical selection differs from the POST echo (a concurrent
  // change), so the hash always matches what is rendered.
  await loadContextOptions();
  if (mySeq !== contextSwitchSeq) return;  // superseded during the refetch
  syncContextHash();
  contextHashIntentPending = false;
  // Reconciliation has actually happened: canonical options are loaded and the
  // hash matches them, so a control painted from here on is bound to the tuple
  // that is genuinely current. Released immediately before the render() that
  // repaints them, and never on a path that returns early -- a superseded or
  // queued switch leaves it set for its successor to clear.
  contextSwitchIntentPending = false;
  toast = "";
  render();
}
// The switcher's flat option list: EVERY authorized Program is Program-only-
// selectable (a "no season" entry, season_id=null), plus one entry per authorized
// Season. Encoded as "program_id|season_id" (season blank ⇒ Program-only). This
// is the single source for both the count (static-chip decision) and the markup.
function contextEntries(opts) {
  const multi = opts.programs.length > 1;
  const out = [];
  opts.programs.forEach((p) => {
    out.push({ value: p.id + "|", label: "Program overview (no season)",
      programId: p.id, seasonId: null, readOnly: false, programName: p.name });
    p.seasons.forEach((s) => out.push({
      value: p.id + "|" + s.id,
      label: s.name + (s.read_only ? " · archived (read-only)" : ""),
      programId: p.id, seasonId: s.id, readOnly: !!s.read_only,
      programName: p.name }));
  });
  return { entries: out, multi };
}
// Paint the switcher for the current authorized options + selection. One native
// <select> for every role (full keyboard/AT semantics for free); a static chip
// only when there is a single selectable context. The read-only badge and the
// persistent scope note (#ctx-scope-note) reflect the current state in the
// CLOSED view.
function renderContextSwitcher() {
  const wrap = document.getElementById("context-switcher");
  const select = document.getElementById("ctx-select");
  const chip = document.getElementById("ctx-static");
  const roBadge = document.getElementById("ctx-ro");
  if (!wrap || !select || !chip || !roBadge) return;
  const opts = contextOptions;
  const show = !!(currentUser && opts && opts.programs && opts.programs.length);
  wrap.hidden = !show;
  if (!show) { renderLeagueSelect(null, null); return; }
  const { entries, multi } = contextEntries(opts);
  const sel = opts.selected || {};
  const curValue = (sel.program_id || "") + "|" + (sel.season_id || "");
  const curEntry = entries.find((e) => e.value === curValue) || null;
  // Read-only badge is a persistent, always-visible reflection of the selection.
  roBadge.hidden = !(curEntry && curEntry.readOnly);
  const single = entries.length <= 1;
  if (single) {
    select.hidden = true; chip.hidden = false;
    const e = entries[0];
    // The chip is the only selected-context indicator in this state, so it
    // must identify the Program even when only one Program is authorized.
    // Omitting it left every seasonless single-Program account with the
    // indistinguishable label "Program overview (no season)".
    chip.textContent = `${e.programName} · ${e.label}`;
  } else {
    chip.hidden = true; select.hidden = false;
    const optionTag = (e) => `<option value="${esc(e.value)}"`
      + `${e.value === curValue ? " selected" : ""}>${esc(e.label)}</option>`;
    if (multi) {
      const groups = [];
      opts.programs.forEach((p) => {
        const items = entries.filter((e) => e.programId === p.id).map(optionTag);
        groups.push(`<optgroup label="${esc(p.name)}">${items.join("")}</optgroup>`);
      });
      select.innerHTML = groups.join("");
    } else {
      select.innerHTML = entries.map(optionTag).join("");
    }
  }
  // The League select's own visibility/options are independent of whether the
  // Program/Season half collapsed to a chip above (#345/#364) -- a single-
  // Program/single-Season account can still have several Leagues to choose
  // from, and a multi-Program account's ACTIVE Program might have none.
  renderLeagueSelect(opts, sel);
}
// The League select is a SEPARATE, Program-scoped control (#345/#364, the
// third context axis): its options come from the ACTIVE Program's own
// `leagues` list only (never every Program's, unlike the Program/Season
// entries above, which enumerate every authorized Program at once).
// Switching Program re-renders this with a fresh option set -- an active
// League that does not belong to the new Program cannot survive, by
// construction (it is simply never in the new list to select), mirroring
// set_with_league's own cross-Program rejection rather than merely hiding a
// now-invalid choice. Hidden entirely when the active Program has no
// Leagues at all -- "nothing to choose from" is a different UI state than
// "Leagues exist but none is chosen" (the explicit "No League" option).
function renderLeagueSelect(opts, sel) {
  const leagueSelect = document.getElementById("ctx-league-select");
  if (!leagueSelect) return;
  const program = opts && sel
    && (opts.programs || []).find((p) => p.id === sel.program_id);
  const leagues = (program && program.leagues) || [];
  if (!leagues.length) { leagueSelect.hidden = true; leagueSelect.innerHTML = ""; return; }
  leagueSelect.hidden = false;
  const curLeagueId = sel.league_id || "";
  const optionTag = (id, label) => `<option value="${esc(id)}"`
    + `${id === curLeagueId ? " selected" : ""}>${esc(label)}</option>`;
  leagueSelect.innerHTML = [optionTag("", "No League")]
    .concat(leagues.map((lg) => optionTag(lg.id, lg.name))).join("");
}
// Wire the native selects once. A native <select> gives the full keyboard /
// screen-reader contract (focus, Arrow/Home/End, type-ahead, Enter/Escape) for
// free, so no custom menu-radio handling is needed.
const ctxSelect = document.getElementById("ctx-select");
if (ctxSelect) ctxSelect.onchange = (e) => {
  const raw = e.target.value || "";
  const bar = raw.indexOf("|");
  const p = bar >= 0 ? raw.slice(0, bar) : raw;
  const s = bar >= 0 ? raw.slice(bar + 1) : "";
  const sel = (contextOptions && contextOptions.selected) || {};
  // A same-Program Season change CARRIES the active League forward (#345/
  // #364): options_with_league's own docstring calls a Program-scoped League
  // "a legitimate way to move to a Season+League pair" precisely so this
  // reverse direction also reaches an existing binding -- e.g. a League bound
  // to both S1 and S2 must be reachable by switching S1 -> S2 while it is
  // active, not just by re-picking the League after. set_with_league
  // validates Program/Season/League together in one transaction and writes
  // NOTHING on any failure, so an invalid carry (League not bound to the new
  // Season) leaves the prior context untouched -- never a half-applied
  // Season-changed-but-League-silently-dropped state. A Program change always
  // drops the League instead of attempting the carry: authorized_league_ids
  // never authorizes a cross-Program League, so carrying one forward would
  // just guarantee a doomed request.
  const carryLeague = p === sel.program_id ? (sel.league_id || null) : null;
  setActiveContext(p, s || null, carryLeague);
};
const ctxLeagueSelect = document.getElementById("ctx-league-select");
if (ctxLeagueSelect) ctxLeagueSelect.onchange = (e) => {
  const sel = (contextOptions && contextOptions.selected) || {};
  // Selecting a League CARRIES the active Season forward and asks
  // set_with_league to validate the (Season, League) pair together, rather
  // than pre-clearing Season to the one combination guaranteed valid
  // (Program+League alone): options_with_league's docstring is explicit that
  // a Program-scoped League "not bound to the currently-selected Season is
  // still offered... because selecting it is a legitimate way to move to a
  // Season+League pair -- the binding requirement is enforced at selection
  // time." set_with_league's NotFoundError is DELIBERATELY one generic
  // reason for every rejection cause (its own docstring: distinguishing them
  // "would leak exactly what the scope rules exist to hide"), so a rejected
  // carry cannot be attributed to "the Season" specifically -- but rejection
  // already leaves the prior context untouched (validated before any write)
  // and the existing failure path (toast + loadContextOptions + render)
  // snaps both selects back to the true persisted state, so no client-side
  // guess at which Seasons a League is bound to is needed.
  setActiveContext(sel.program_id, sel.season_id || null, e.target.value || null);
};

// Sign out ends the server session and returns to the sign-in screen (#71).
const signoutBtn = document.getElementById("signout-btn");
if (signoutBtn) signoutBtn.onclick = async () => {
  await post("/api/auth/logout", {});
  // Remember the explicit sign-out so a refresh does NOT silently re-run the
  // zero-friction demo auto-login — logout must stick until the user signs in.
  try { localStorage.setItem("hs_signed_out", "1"); } catch (_) {}
  setUser(null); toast = "";
  // Drop the active-context selection + its URL hash with the session (#159).
  contextOptions = null;
  if (location.hash.indexOf("#ctx=") === 0) {
    history.replaceState(null, "", location.pathname + location.search);
  }
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
  if (modal) { modal = null; render(); }
  else if (drawer) { drawer = null; drawerError = ""; drawerValues = {}; render(); }
  else if (movingGameId != null) { movingGameId = null; render(); }
});
// Activate any role="button" element (a clickable card/row that isn't a real
// <button>, e.g. a dashboard game row or notification card) on Enter/Space,
// same as a native button (#118 Phase 6) — reuses whatever onclick each
// render already bound, rather than duplicating that logic here.
document.addEventListener("keydown", (e) => {
  if (e.key !== "Enter" && e.key !== " ") return;
  const el = e.target.closest('[role="button"]');
  if (!el) return;
  e.preventDefault();
  el.click();
});

// -- sign-in / session (demo auth, #50) ----------------------------------
function applyRolePerms() {
  const r = roleCatalog.find((x) => x.id === currentRole);
  rolePerms = new Set(r ? r.permissions : []);
  gateChrome();
}
// Role-level mirror of the backend's private-game read gate (scope.py's
// can_read_private_game_data), ignoring the specific game: operators read
// every game, a coach/player their own team's, an official their assigned
// games; a viewer/guardian never any. The Roster and Game Sheet tabs show
// nothing but private per-game data, so hiding them from roles that can
// never read any game's private data avoids luring those users to a
// guaranteed "Restricted" screen.
function canReadAnyPrivateGame() {
  if (!currentUser) return true;               // headerless demo operator
  const role = currentUser.role, s = currentUser.scope || {};
  if (role === "league_admin" || role === "arena_manager") return true;
  if (role === "coach" || role === "player") return !!s.team_id;
  if (role === "official") return !!s.official_id;
  return false;                                // viewer, guardian
}
// The Dashboard and Activity tabs are operator/coach consoles — league-wide
// (or coach-team) ops stats, alerts, and audit feeds. A player, guardian,
// official, or viewer has their own home surface and no use for that operator
// noise, so both tabs are gated to roles that manage rosters or the schedule.
function canSeeOpsConsole() {
  return hasPerm("manage_roster") || hasPerm("manage_schedule");
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
  // Player Home is only meaningful for a signed-in, bound player (#107).
  const isPlayer = !!(currentUser && currentUser.scope && currentUser.scope.player_id);
  toggle('.tab[data-tab="player_home"]', isPlayer);
  // "My Players" is the guardian's linked-junior surface (#26) — only for a
  // signed-in guardian. A guardian holds no player/team scope; the role alone
  // gates the tab, and each junior link is checked per action server-side.
  const isGuardian = !!(currentUser && currentUser.role === "guardian");
  toggle('.tab[data-tab="guardian_home"]', isGuardian);
  // The Delivery admin tab is operator-only (#61).
  toggle('.tab[data-tab="delivery"]', hasPerm("manage_schedule"));
  toggle('.tab[data-tab="users"]', hasPerm("manage_users"));
  toggle('.tab[data-tab="scheduler"]', hasPerm("manage_schedule"));
  // Pilot Readiness is an operator-facing summary card (#104) — same gate
  // as the Setup entities it reports on.
  toggle('.tab[data-tab="readiness"]', hasPerm("manage_setup"));
  // The Import wizard tab mirrors /api/import/dry-run's own gate (#96):
  // manage_arena is the one permission both League Admin and Arena Manager
  // hold, and it's the entry point for all three import types.
  toggle('.tab[data-tab="import"]', hasPerm("manage_arena"));
  // Setup (#233 B2a review r2): a role with neither manage_setup nor
  // manage_arena has nothing to manage there — the tab itself isn't a
  // dead end (setupCard/renderSetupHierarchy already hide per-entity
  // actions), but it shouldn't be reachable at all for such a role.
  //
  // #345: scoped with :not([data-setup-workflow-nav]) so this gate governs
  // ONLY Administration's plain Setup (the workflow index). The Facilities
  // entry below shares data-tab="setup" but is a different destination with a
  // different, narrower permission — an unscoped selector would silently
  // re-grant it to every manage_setup role.
  toggle('.tab[data-tab="setup"]:not([data-setup-workflow-nav])',
         hasPerm("manage_setup") || hasPerm("manage_arena"));
  // Facilities → "Venues, rinks and ice" (#345). manage_arena exactly matches
  // SETUP_WORKFLOWS.facilities.perm, which is also what openSetupWorkflowLanding()
  // fails closed on — so the nav gate and the transition guard cannot drift.
  toggle('.tab[data-setup-workflow-nav="facilities"]', hasPerm("manage_arena"));
  // Reset wipes and reseeds all demo data — shown only in demo mode and only to
  // a League Admin (MANAGE_SETUP), matching the server, which hard-disables the
  // reset route in production (#215).
  renderDemoMenu();  // header demo (database) control, state-aware (#215)
  // Sign out only makes sense with a live session.
  toggle("#signout-btn", !!currentUser);
  // Dashboard + Activity are operator/coach ops consoles; Roster + Game Sheet
  // are private per-game data. Hide each from roles that would only ever land
  // on operator noise or a "Restricted" screen (crash-fixed for Activity, but
  // still a dead end). Each hidden role has its own home to land on instead.
  toggle('.tab[data-tab="dashboard"]', canSeeOpsConsole());
  toggle('.tab[data-tab="activity"]', canSeeOpsConsole());
  toggle('.tab[data-tab="roster"]', canReadAnyPrivateGame());
  toggle('.tab[data-tab="sheet"]', canReadAnyPrivateGame());
  // Hide a nav area once all of its destinations are hidden, so its label
  // (Home/Tasks, Schedule, Teams & People, …) doesn't hang orphaned above
  // nothing. #345: this also covers Facilities, which is declared in the
  // seven-area IA but carries no destination until the `setup` split makes
  // "Venues, rinks and ice" separately addressable — an area with zero tabs
  // is hidden by exactly the same rule as one whose tabs a role cannot see.
  document.querySelectorAll(".nav-group").forEach((g) => {
    const anyTab = Array.from(g.querySelectorAll(".tab"))
      .some((t) => t.style.display !== "none");
    g.style.display = anyTab ? "" : "none";
  });
}
// One-time/private per-identity UI state (#116 Phase 0.2): a checkout
// confirmation, an open opportunity detail, or a just-minted "shown once"
// feed URL belongs to the signed-in user, not the browser tab — carrying it
// across a persona switch would show one user's confirmation/secret to the
// next signed-in user.
function resetTransientUiState() {
  checkoutConfirm = null; oppDetailGame = null; oppDetail = null;
  gCheckout = null; gOpp = null; gOppDetail = null;
  newFeedUrl = null;
  publicState.game = null;
  publicState.feedUrl = null; publicState.feedLabel = null;
  publicTab = "schedule";
  // Per-view filters/selections below aren't secrets, but carrying them into
  // the next signed-in identity is stale at best (#118 Phase 2) and unsafe at
  // worst: a coach's pre-checked draft-game selection surviving into a
  // different operator's session could get published by a click the new
  // operator never meant to make.
  rosterSide = "home";
  availFilter = "all";
  gamesFilter = { division: "all", team: "all", rink: "all", status: "all", from: "", to: "" };
  gamesExpanded = new Set();
  usersSelected = null;
  schedulerState.selected = new Set();
  // playersList (#114) is real player names/teams fetched only for a
  // manage_setup session — the Setup view itself isn't permission-gated
  // (setupCard only hides the "+ New" button), so if it survived an
  // identity switch to a lower-privileged role that lands back on "setup"
  // without a page reload, that role would see the previous operator's
  // fetched player roster.
  playersList = [];
  // guardianLinksState (#35) isn't currently reachable by a lower-privileged
  // role either — renderUsers() gates its whole body (including the
  // Guardian Links card) on hasPerm("manage_users") before touching this
  // state, same as playersList's setupCard gate should but currently
  // doesn't. Cleared here anyway for consistency/defense-in-depth with the
  // playersList precedent above.
  guardianLinksState = [];
  // New-account form (#135): same defense-in-depth as guardianLinksState —
  // renderUsers() already gates this, but an in-progress username/role
  // shouldn't survive an identity switch regardless. Never held a password.
  newAccountForm = { username: "", role: "", team_id: "", player_id: "", official_id: "" };
  newAccountError = "";
  // Import wizard / Ice Availability Builder / hub create-drawer (#331
  // review round 8): round 7 bound these to contextRevision, which closes a
  // Program/Season switch under the SAME identity, but this function's own
  // job is the identity switching -- a persona swap via the demo role-switch
  // dropdown, or a sign-out immediately followed by a different sign-in,
  // with no page reload in between. An in-progress paste (sheetsText can
  // hold real player names/emails), a validated Import report, or a live
  // Ice preview/Create action must not survive into the next signed-in
  // identity, lower-privileged or not -- resetTransientUiState() already
  // fires on exactly this transition (the prevId !== nextId guard in
  // setUser() below), so it's the correct, already-identity-gated place for
  // this too. Reset importState to its own module-level initial shape
  // (mirrored here, not imported, since it's a plain object literal) and
  // fully CLOSE the Ice Builder (iceBuilder = null, its own "not open"
  // sentinel — see its declaration comment) rather than just clearing its
  // preview: the next identity, even if equally privileged, didn't open it.
  // Both Ice Builder click handlers guard against iceBuilder being null
  // (round 8) for exactly this reason. The drawer gets the identical
  // synchronous DOM removal invalidateContextScopedMutations() uses and for
  // the identical reason: submitSetup() doesn't itself re-check anything
  // about the drawer before posting, so the only airtight guard is removing
  // its submit control from the DOM before a click can reach it. Bumping
  // contextRevision last means a render() of either view (if the NEXT
  // identity can even reach it) re-seeds fresh rather than finding an
  // already-"current" stamp and skipping the reseed.
  importState = { type: "teams_players", seasonId: null, sheetsText: {},
    report: null, validatedKey: null, committed: null, contextRevision: null };
  iceBuilder = null;
  if (drawer) {
    document.querySelectorAll(".drawer-scrim, .drawer").forEach((el) => el.remove());
    drawer = null; drawerError = ""; drawerValues = {};
  }
  // ===== PER-CARD OPERATION/UI STATE (#365 review round 7) =================
  // The three stores the review names — cardWrites, cardStates,
  // cardGenerations — audited one by one, because they do NOT all deserve the
  // same treatment and the round-6 duplicate-write fix depends on that.
  //
  // (a) THE EPOCH, ADVANCED FIRST. Every identity already issued was stamped
  //     with the OLD value, so from this line on cardIdentitySamePrincipal()
  //     is false for all of them and cardIdentityCurrent() therefore refuses
  //     every model commit, repaint, announcement, focus move, completion and
  //     next-task mutation they could still attempt. This is the guard, and
  //     the four call-sites downstream re-check it after their own await.
  uiIdentityEpoch += 1;
  // (a2) THE STANDING DEEP-LINK FOCUS INTENT (#365 round 11). Same reasoning
  //     as (a) and enforced by the same epoch: an unresolved "take me to this
  //     Season's Allow-a-venue picker" belongs to the DEPARTING operator, and
  //     the arriving one must not have their focus pulled onto a control the
  //     departing one asked for. settleDestinationFocus() would refuse it on
  //     its own account (it asks the same question at resolution time, which
  //     is what makes the refusal airtight); this drops the record here so a
  //     dead promise is not left standing across the boundary at all.
  cancelSupersededDestinationFocus();
  // (a3) THE GENERIC FOCUS POLL (#365 round 13). The intent above carries an
  //     identity and is refused at settlement on its own account; the poll
  //     carries NOTHING but the ticket, so the epoch bumped at (a) means
  //     nothing to it and a chain started under the DEPARTING principal
  //     would keep ticking and land focus on the ARRIVING one's surface --
  //     the same boundary crossing (a) and (a2) exist to stop, in the one
  //     piece of async focus work that had no binding to identity at all.
  //     Bumping the ticket here IS the invalidation: the next tick of every
  //     in-flight chain sees a newer request and returns having focused
  //     nothing. It costs the arriving identity nothing -- any
  //     focusContentHeading() called after this line takes a fresh ticket
  //     and keeps its #content floor unchanged.
  newFocusRequest();
  // (b) cardStates — DESTROYED. A committed card model is this operator's
  //     read: counts fetched under their permissions, and on a card mid-
  //     confirmation their typed `confirmReason` and the server's error text.
  //     readCardState() would already withhold it from the next identity, but
  //     private text that is merely unread is still private text that is
  //     still there. Deleted key by key rather than rebound, so every existing
  //     reference to the same object keeps seeing the truth.
  Object.keys(cardStates).forEach((k) => { delete cardStates[k]; });
  // (c) cardWrites — KEPT, PAYLOAD QUARANTINED. This is the whole tension of
  //     the round. The ENTRY is serialization bookkeeping: a write is in
  //     flight against that card and target tuple, navigation and sign-out
  //     cancelled neither the HTTP request nor the transaction behind it, and
  //     dropping the record here would let the arriving principal fire a
  //     SECOND lifecycle write against the same Season while the first is
  //     unresolved — the round-6 hole, reopened. So the entry stays and the
  //     card stays non-actionable for whoever arrives. What does NOT stay is
  //     the held model: the reason, the error, the counts and the
  //     confirmation are overwritten with the neutral presentation, so the
  //     departing operator's words stop existing rather than stop being
  //     painted.
  Object.keys(cardWrites).forEach((cardId) => {
    const ledger = cardWrites[cardId];
    Object.keys(ledger).forEach((key) => {
      ledger[key].model = foreignCardWriteModel();
    });
  });
  // (c2) THE ONE SITEWIDE LIVE REGION. #toast-root is where announceCardStatus
  //     and every other operation sentence lands, and updateToast() only HIDES
  //     it when there is nothing left to say — the previous sentence stays in
  //     the markup. So the departing operator's last announcement
  //     ("Reopening this season…", a server error message, anything they were
  //     told) is still sitting in the document the arriving principal is
  //     handed. Hidden is not gone: it is in the DOM, and a live region is
  //     exactly the surface this round's correction names alongside the held
  //     model and the focus target. Emptied outright here, at the identity
  //     boundary, rather than merely hidden.
  toast = ""; toastIsError = false;
  const identityToastRoot = document.getElementById("toast-root");
  if (identityToastRoot) {
    identityToastRoot.innerHTML = "";
    identityToastRoot.classList.remove("error");
  }
  updateToast();
  // (c3) THE ALREADY-PAINTED CARD SURFACES (#365 round 8). (b) and (c) above
  //     destroy the STORES; the live region got its own DOM pass in (c2)
  //     precisely because "hidden is not gone". Every OTHER context-scoped
  //     surface was left exactly as the departing principal painted it, and a
  //     painted card is what the operator actually sees -- the review's
  //     "post-auth/pre-render privacy window". Blanked SYNCHRONOUSLY here,
  //     before setUser() assigns `currentUser` and therefore before the
  //     arriving principal is exposed at all, because everything after this
  //     point is an await. See blankContextScopedCardSurfaces().
  blankContextScopedCardSurfaces();
  // (d) cardGenerations — DELIBERATELY KEPT, and the reasoning is the reason
  //     the epoch had to exist at all. The counters hold no operator text:
  //     they are monotone integers, so there is nothing here to disclose.
  //     Resetting them would be worse than useless — it would hand the
  //     arriving principal's first request for a card the SAME generation
  //     number a departing principal's outstanding identity is already
  //     holding, manufacturing collisions that only the epoch could then tell
  //     apart. Keeping them monotone means a generation number is never
  //     reused, and the epoch is what makes a departed identity stale
  //     regardless of what the counter says — which is exactly the property
  //     the review found missing, since under the serialization rule the
  //     counter cannot move for a card with an unresolved write.
  contextRevision += 1;
  // A context switch this identity initiated but had not yet gotten a POST
  // out for (queued behind one already in flight, see setActiveContext()'s
  // own comment) belongs to nobody now -- discard it rather than letting it
  // fire against the NEXT signed-in identity's session once the in-flight
  // request settles (#331 review round 9). Bumping contextSwitchSeq too
  // means that in-flight request's OWN completion -- necessarily still the
  // OLD identity's -- recognizes on arrival that it's been superseded and
  // skips its own reconciliation entirely, the same way an ordinary
  // superseded switch already does.
  contextSwitchQueued = null;
  contextSwitchSeq += 1;
  // ...and the same for a switch whose POST has already LEFT but has not yet
  // been ANSWERED. #369 made sendContextSwitch() publish the intended
  // selection into location.hash BEFORE its POST, so the hash can never lag
  // an already-mutated server. The flip side is that an unacknowledged switch
  // now leaves the DEPARTING identity's raw intent sitting in the URL.
  // Bumping contextSwitchSeq above only makes that POST's own completion skip
  // its reconciliation; it does nothing about the hash. So the next
  // identity's boot runs restoreContextDeepLink(), sees hash != its persisted
  // selection, applies deep-link-wins, and re-POSTs the OLD identity's
  // never-acknowledged choice as the NEW identity's context -- precisely the
  // cross-identity leak discarding contextSwitchQueued above exists to
  // prevent, just carried by a stronger vehicle.
  //
  // contextOptions.selected is still the last SERVER-CONFIRMED selection
  // (sendContextSwitch only overwrites it after its POST resolves), so
  // syncContextHash() here rewinds the hash to the context the server
  // actually holds. It is a no-op on an ordinary identity transition: a
  // reconciled switch's hash already equals contextOptions.selected, and
  // inheriting a CONFIRMED context across a persona switch is the
  // pre-existing, intended deep-link behavior -- only an unconfirmed phantom
  // is withdrawn.
  //
  // Keyed on contextHashIntentPending, NOT contextSwitchInFlight. Those are
  // different windows, and an earlier revision of this guard used the wrong
  // one: contextSwitchInFlight clears the moment the POST resolves, but on
  // the REJECTED path the hash still advertises the refused intent across the
  // awaited loadContextOptions() reconciliation that follows. An identity
  // change in that window found the flag already false, left the phantom in
  // the URL, and handed the next identity a rejected choice to adopt --
  // silently persisted if that identity happened to be authorized for it.
  // The intent flag stays set until a syncContextHash() has actually
  // reconciled, on every path, so no window is left uncovered.
  if (contextHashIntentPending) {
    syncContextHash();
    contextHashIntentPending = false;
  }
  // ...and the identical reasoning for the ACTION-CONTROL withdrawal (#365).
  // The switch this flag was set for has just been orphaned: its POST's own
  // completion will hit the `mySeq !== contextSwitchSeq` guard above and
  // return without reaching either clearing point, so leaving the flag set
  // would withdraw the NEXT identity's Setup landing actions and Home CTA
  // permanently. Released here, where the departing identity's whole
  // transient state is; the render() that follows setUser() is what paints
  // the new identity's own controls.
  contextSwitchIntentPending = false;
}
function setUser(user) {
  const prevId = currentUser ? currentUser.username : null;
  const nextId = user ? user.username : null;
  if (prevId !== nextId) resetTransientUiState();
  currentUser = user;
  currentRole = user ? user.role : "viewer";
  applyRolePerms();
  // Players land on their Home page (#107 / HOME-001) instead of the
  // operator dashboard — only while still on the untouched default view,
  // so an explicit tab choice mid-session is never overridden. The inverse
  // guard sends a non-player OFF the player-only Home (persona switches).
  // Officials get the same treatment (#145): the generic operator
  // dashboard's stats/alerts are all league-wide, not "what do I need to
  // do" for an official — My Assignments (inbox) is their actual task
  // list, mirroring player_home/guardian_home rather than leaving them on
  // a dashboard with nothing relevant to them (research finding for #145).
  //
  // Two phases, not a single if/else-if chain (review finding for #145):
  // with three mutually-exclusive special views, a direct persona switch
  // (role-switch dropdown or sign-out/in with no page reload) can jump
  // straight from one to another — e.g. Player (on player_home) becomes an
  // Official in one setUser() call. A single-shot chain only ever applies
  // ONE branch per call, so it would bounce player_home -> dashboard and
  // stop there, never reaching the official_id branch below it. Phase 1
  // unconditionally leaves any special view that no longer belongs to this
  // user; phase 2 then lands on this user's own special view if — after
  // phase 1 — view is still the untouched default, so an explicit tab
  // choice mid-session is still never overridden.
  const isPlayerUser = !!(user && user.scope && user.scope.player_id);
  const isGuardianUser = !!(user && user.role === "guardian");
  const isOfficialUser = !!(user && user.scope && user.scope.official_id);
  const priorView = view;
  if (!isPlayerUser && view === "player_home") view = "dashboard";
  if (!isGuardianUser && view === "guardian_home") view = "dashboard";
  if (!isOfficialUser && view === "inbox") view = "dashboard";
  // Phase 1b: a no-reload persona switch (role-switch dropdown / sign-out+in)
  // can leave a user on a tab their new role can no longer see — an operator
  // on Activity/Dashboard becoming a viewer, say. Bounce off those to the
  // default so phase 2 can re-land them; the tab itself is already hidden by
  // gateChrome, but the *view* state has to follow or they'd stare at a
  // now-invisible screen.
  if (["dashboard", "activity"].includes(view) && !canSeeOpsConsole()) view = "dashboard";
  if (["roster", "sheet"].includes(view) && !canReadAnyPrivateGame()) view = "dashboard";
  // #233 B2a review r2: a no-reload persona switch off an operator role can
  // otherwise leave a viewer/coach/player/official staring at a Setup screen
  // whose tab gateChrome just hid out from under them.
  if (view === "setup" && !(hasPerm("manage_setup") || hasPerm("manage_arena"))) view = "dashboard";
  // #331 review round 8: same reasoning for Import — renderImport() already
  // self-guards with its own "Operators only" banner rather than the
  // previous operator's actual state (importState is cleared regardless,
  // above), but bouncing off the view entirely is still correct so a
  // lower-privileged persona lands on ITS OWN home/dashboard instead of a
  // dead-end banner for a nav item its own sidebar no longer shows.
  if (view === "import" && !hasPerm("manage_arena")) view = "dashboard";
  if (isPlayerUser && view === "dashboard") view = "player_home";
  else if (isGuardianUser && view === "dashboard") view = "guardian_home";
  else if (isOfficialUser && view === "dashboard") view = "inbox";
  // A viewer has no dedicated home and can't see the operator Dashboard —
  // Standings is public info everyone can read, so it's their landing.
  else if (view === "dashboard" && !canSeeOpsConsole()) view = "standings";
  if (view === priorView) return;
  document.querySelectorAll(".tab").forEach((x) =>
    x.classList.toggle("active", x.dataset.tab === view));
}

// Show/hide the full-screen sign-in overlay (#71). ``body.signed-out`` hides
// the console shell so a signed-out visitor only ever sees the login card.
function showLogin(message) {
  hidePublicGuest();
  // #345 review: title follows the VISIBLE surface, set before anything else
  // here can early-return or await.
  setShellTitle("login");
  const screen = document.getElementById("login-screen");
  document.body.classList.add("signed-out");
  if (screen) screen.hidden = false;
  const err = document.getElementById("login-error");
  if (err) { err.hidden = !message; err.textContent = message || ""; }
  renderLoginPersonas();
  const u = document.getElementById("login-user");
  if (u) u.focus();
  const guestLink = document.getElementById("guest-public-link");
  if (guestLink) guestLink.onclick = () => showPublicGuest();
}
function hideLogin() {
  hidePublicGuest();
  document.body.classList.remove("signed-out");
  const screen = document.getElementById("login-screen");
  if (screen) screen.hidden = true;
}

// Public portal (#34): the schedule/standings the backend already serves
// unauthenticated (/api/public/*) weren't reachable by an anonymous visitor
// — the whole console shell, "Public" tab included, sits behind the sign-in
// wall. This is a lightweight guest surface, shown instead of (not inside)
// the authenticated shell, that reuses the same renderPublic() markup and
// publicState the signed-in "Public" tab already uses.
function hidePublicGuest() {
  const screen = document.getElementById("public-screen");
  if (screen) screen.hidden = true;
  if (location.hash === "#public") history.replaceState(null, "", location.pathname + location.search);
}
// Monotonic guard (#345 review) -- same pattern as setupProgressFetchSeq --
// so a held/slow response from an EARLIER call (e.g. a Standings click whose
// fetch is still in flight) cannot clobber a NEWER call's already-rendered
// content or focus once it finally resolves.
let publicRenderSeq = 0;
async function renderPublicGuest() {
  const box = document.getElementById("public-content");
  if (!box) return;
  updateToast();  // the guest screen has no other render() path to surface post() errors
  const mySeq = ++publicRenderSeq;
  // #345 review: every state this function renders wholesale-replaces
  // #public-content's children, which destroys whichever control (a
  // segment tab, Retry) the operator just clicked to GET here -- capture
  // whether focus was already inside the box before wiping it, so the box
  // itself can be given focus instead of losing it to <body>. #public-
  // content itself is never replaced (only its children are), so once
  // focused here it stays focused across every later state in this same
  // render cycle without needing to be re-applied. Never done on the
  // FIRST, uninteracted load (focusWasInBox is false then), so landing on
  // the public screen never steals focus from wherever the visitor was.
  const focusWasInBox = !!(document.activeElement && box.contains(document.activeElement));
  box.setAttribute("role", "status");
  box.setAttribute("aria-live", "polite");
  box.setAttribute("aria-busy", "true");
  box.innerHTML = `<div class="skeleton"><span class="sr-only">Loading public schedule…</span></div>`
    + `<div class="skeleton"></div>`;
  if (focusWasInBox) { box.setAttribute("tabindex", "-1"); box.focus(); }
  // A network failure or a non-JSON/5xx response — getJSON now surfaces the
  // latter as {error} rather than throwing — must give an anonymous visitor the
  // same clear error + Retry a signed-in user gets, not an empty schedule or an
  // infinite skeleton (#133).
  const fail = (message) => {
    if (mySeq !== publicRenderSeq) return;  // an obsolete response must not clobber a newer render
    box.setAttribute("aria-busy", "false");
    box.innerHTML = `<div class="banner alert" role="alert"><h2>Could not load the public schedule</h2>
      <p>${esc(message)}</p></div>
      <div class="actions"><button class="act primary" id="public-retry-btn">Retry</button></div>`;
    const retry = document.getElementById("public-retry-btn");
    if (retry) retry.onclick = () => renderPublicGuest();
  };
  try {
    const sch = await getJSON("/api/public/schedule");
    if (mySeq !== publicRenderSeq) return;  // obsolete -- a newer call already owns the box
    if (sch && sch.error) return fail(sch.error.message);
    publicState.schedule = sch || { fixtures: [], divisions: [] };
    if (!publicState.division && publicState.schedule.divisions[0]) {
      publicState.division = publicState.schedule.divisions[0].id;
    }
    if (publicTab === "standings" && publicState.division) {
      const st = await getJSON(`/api/public/standings/${publicState.division}`);
      if (mySeq !== publicRenderSeq) return;  // obsolete
      if (st && st.error) return fail(st.error.message);
      publicState.standings = st;
    }
  } catch (e) {
    if (mySeq !== publicRenderSeq) return;  // obsolete
    return fail(e.message || String(e));
  }
  box.setAttribute("aria-busy", "false");
  box.innerHTML = renderPublic({});
  box.querySelectorAll("[data-public-tab]").forEach((b) => b.onclick = () => {
    publicTab = b.dataset.publicTab; publicState.game = null;
    publicState.feedUrl = null; publicState.feedLabel = null; renderPublicGuest();
  });
  const pubDiv = box.querySelector("#public-div");
  if (pubDiv) pubDiv.onchange = () => {
    publicState.division = pubDiv.value;
    publicState.feedUrl = null; publicState.feedLabel = null;  // stale URL was for the OLD division (#33 review)
    renderPublicGuest();
  };
  box.querySelectorAll("[data-public-game]").forEach((b) => b.onclick = async () => {
    const g = await getJSON(`/api/public/games/${b.dataset.publicGame}`);
    if (g && g.error) { toast = g.error.message; toastIsError = true; }
    publicState.game = (g && !g.error) ? g : null; renderPublicGuest();
  });
  const pubBack = box.querySelector("[data-public-back]");
  if (pubBack) pubBack.onclick = () => { publicState.game = null; renderPublicGuest(); };
  wirePublicFeedSubscribe(box, renderPublicGuest);
  wireCopyFeedUrl(box);
}
function showPublicGuest() {
  // Same shell-hiding rule as showLogin(): body.signed-out keeps the
  // authenticated sidebar/nav out of the tab order and off-screen for
  // assistive tech, not just visually covered by the fixed overlay.
  // #345 review: the title changes with the shell state, synchronously --
  // renderPublicGuest() below is async and must not own this.
  setShellTitle("public");
  document.body.classList.add("signed-out");
  const login = document.getElementById("login-screen");
  if (login) login.hidden = true;
  const screen = document.getElementById("public-screen");
  if (screen) screen.hidden = false;
  if (location.hash !== "#public") history.replaceState(null, "", location.pathname + location.search + "#public");
  const signIn = document.getElementById("public-signin-link");
  if (signIn) signIn.onclick = () => showLogin();
  // #345 review: mirrors showLogin()'s unconditional u.focus() -- whatever
  // triggered this entry (the guest link on the just-hidden sign-in card,
  // or nothing on a fresh #public boot) is gone or never existed, so focus
  // would otherwise fall to <body>. #public-signin-link is a persistent,
  // always-rendered control (unlike anything inside #public-content, which
  // renderPublicGuest() below is still about to fetch/paint).
  if (signIn) signIn.focus();
  renderPublicGuest();
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
  // Clear the calendar's transient move state on sign-in for the same
  // cross-identity reason movingGameId/conflict are cleared here (#153): a
  // move staged by the previous operator must not carry into the new session.
  // pendingReassign is cleared for the same reason (#166).
  drawer = null; movingGameId = null; conflict = null; pendingMove = null; pendingReassign = null;
  hideLogin();
  // Load this identity's authorized context (a persona switch re-scopes it),
  // then reconcile the URL: adopt an authorized deep link, else reflect the
  // persisted selection (#159).
  await loadContextOptions();
  await restoreContextDeepLink();
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
// Real deployment posture (#72): app mode, store backend, delivery modes.
// Shared by the topbar env chips and the Pilot Readiness card (#104).
const STORE_LABEL = { memory: "in-memory", sqlite: "sqlite", postgres: "postgres" };
const appModeLabel = (mode) => mode === "production" ? "Production" : "Demo";
const deliveryLive = (mode, liveVal) => mode === liveVal;
function renderEnvChips() {
  const box = document.getElementById("env-chips");
  if (!box || !envStatus) return;
  const s = envStatus;
  const storeLabel = STORE_LABEL[s.store] || s.store;
  const deliv = (label, mode, liveVal) => {
    const live = deliveryLive(mode, liveVal);
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

// A server/proxy outage (5xx or unreachable) — distinct from a 4xx such as the
// 401 that simply means "no session".
function isServerError(response) {
  return !response || response.status === 0 || response.status >= 500;
}

// A startup outage must NOT masquerade as a normal signed-out/empty-account
// state (which would silently clear roles and drop the user to the sign-in wall
// as if nothing were wrong). Show the sign-in screen with a clear, retryable
// outage message instead.
function showBootstrapOutage() {
  roleCatalog = []; accounts = []; setUser(null);
  applyRolePerms();
  renderRoleSwitch();
  renderEnvChips();
  showLogin("The server is temporarily unavailable. Please refresh in a moment.");
}

async function bootstrap() {
  let rolesR, acctR, statusR, meR;
  try {
    [rolesR, acctR, statusR, meR] = await Promise.all([
      fetch("/api/auth/roles", { credentials: "same-origin" }),
      fetch("/api/auth/accounts", { credentials: "same-origin" }),
      fetch("/api/status", { credentials: "same-origin" }),
      fetch("/api/auth/me", { credentials: "same-origin" }),
    ]);
  } catch (_) {
    return showBootstrapOutage();  // network unreachable — an outage, not "signed out"
  }
  // A 5xx / non-JSON proxy error on ANY of the four bootstrap calls is an
  // outage, not a normal state — surface it rather than letting it masquerade:
  // a roles/me failure would look "signed out", an accounts failure would skip
  // the demo auto-login into a bare sign-in screen, and a status failure would
  // leave envStatus null so isDemo() fails OPEN (demo controls in production).
  if ([rolesR, acctR, statusR, meR].some(isServerError)) return showBootstrapOutage();

  const rolesRes = await readApiResponse(rolesR);
  const acctRes = await readApiResponse(acctR);
  const statusRes = await readApiResponse(statusR);
  roleCatalog = (rolesRes && rolesRes.roles) || [];
  accounts = (acctRes && acctRes.accounts) || [];
  envStatus = statusRes && !statusRes.error ? statusRes : null;

  const meRes = meR.ok ? await readApiResponse(meR) : null;
  if (meRes && meRes.user) {
    setUser(meRes.user);
  } else if (meR.status !== 401 && accounts.length && !signedOutSticky()) {
    // Demo mode, fresh visit (no session, personas available, and the user has
    // not explicitly signed out — /api/auth/me answers 200 with no user, not a
    // 401): keep the zero-friction auto-login as League Admin.
    const r = await post("/api/auth/login",
      { username: "admin", password: DEMO_PASSWORD });
    if (r && !r.error) setUser(r.user); else setUser(null);
  } else {
    // Production (empty picker), an expired/invalid session (401), or an
    // explicit prior sign-out: no silent login — show the sign-in screen (#71)
    // so logout is meaningful across a refresh.
    setUser(null);
  }
  applyRolePerms();
  renderRoleSwitch();
  renderEnvChips();
  if (currentUser) {
    // Load the persisted active context first, then adopt a different authorized
    // deep link if the URL carries one (#159).
    await loadContextOptions();
    await restoreContextDeepLink();
    hideLogin();
    render();
  }
  // A bookmarked/shared #public link (#34) drops a signed-out visitor
  // straight into the guest schedule instead of the sign-in wall.
  else if (location.hash === "#public") { showPublicGuest(); }
  else { showLogin(); }
}
bootstrap();
