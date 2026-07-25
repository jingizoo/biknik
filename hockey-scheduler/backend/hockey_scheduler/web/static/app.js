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
let calendarDate = "2026-09-05";  // YYYY-MM-DD shown on the arena calendar
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
// scoped user can neither select nor enumerate an unrelated context). A saved
// DISPLAY context only — existing screens are not filtered by it yet.
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
let publicState = { schedule: null, standings: null, division: null, game: null,
  feedUrl: null, feedLabel: null };  // feedUrl/feedLabel: freshly-minted public calendar subscription (#33)
let publicTab = "schedule";        // "schedule" | "standings" (#83)
let schedulerState = {
  division: null, preview: null, drafts: [], summary: null,
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
let setupProgress = null;  // fetched /api/v2/setup/progress payload, Home/Tasks hub (#330)
let setupProgressError = false;  // the fetch above failed (distinct from "no data")
// Discards a stale /api/v2/setup/progress response (#330 review round 1
// finding 4): a NEWER render() call — e.g. after a context switch — may
// already have committed its own result while an OLDER call's fetch is
// still in flight. No existing generation-counter/AbortController
// convention exists elsewhere in this file to reuse (the closest precedent,
// the CSV import flow's snapshot-before/compare-after style, depends on
// re-derivable DOM form state that doesn't fit a server-resolved context
// fetch), so a minimal monotonic sequence number is used directly instead.
let setupProgressFetchSeq = 0;
let setupView = "hierarchy";  // "hierarchy" | "records" — Setup sub-view (#165)
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
// once the WHOLE Program's setup is done (`complete`); renders nothing if
// there's simply nothing left for THIS role to act on AND nothing blocked
// to explain either, while other, not-this-role's workflows remain (three
// different claims — see get_setup_progress's docstring).
function renderSetupProgressCard(progress, hadError, loading) {
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
  if (loading) {
    return `<div class="dash-card sp-card" style="margin-bottom:16px">
      <div class="dash-card-head"><h3>Setup progress</h3></div>
      <div class="skeleton"><span class="sr-only">Loading setup progress…</span></div>
    </div>`;
  }
  if (hadError) {
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
  if (!progress || !progress.program_id) return "";
  if (progress.complete) {
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
    const importWf = progress.workflows.find((w) => w.key === "import");
    return `<div class="dash-card sp-card" style="margin-bottom:16px">
      <div class="dash-card-head"><h3>✓ All setup steps complete</h3></div>
      <p class="muted">Every Setup workflow is done for ${esc(progress.program.name)}.</p>
      <div class="actions">
        <button class="act primary" data-goto="calendar">Go to Schedule</button>
        ${importWf ? `<button class="act ghost" data-setup-progress-action="import"
          >${esc(importWf.primary_action)}</button>` : ""}
      </div>
    </div>`;
  }
  const rows = progress.workflows.map((w) => {
    // "optional" (Imports and onboarding, #331 review round 1 finding 5) is
    // a standing alternative entry point, not a required step -- its badge
    // must read as neither "Done" nor a to-do nag.
    const cls = w.status === "done" ? "green"
      : w.status === "optional" ? "blue" : "gray";
    const text = w.status === "done" ? "Done"
      : w.status === "optional" ? "Optional" : "To do";
    return `<div class="li">
      <span class="badge ${cls}">${text}</span>
      <div class="li-main"><div class="li-title">${esc(w.label)}</div>
        <div class="li-sub">${esc(w.detail)}</div></div>
    </div>`;
  }).join("");
  const next = progress.next;
  if (next) {
    return `<div class="dash-card sp-card" style="margin-bottom:16px">
      <div class="dash-card-head"><span class="dch-dot"></span><h3>Continue setup</h3></div>
      <div class="na-row">
        <div class="na-ico blue">📋</div>
        <div class="na-body"><div class="na-title">${esc(next.label)}</div>
          <div class="na-sub">${esc(next.detail)}</div></div>
      </div>
      <div class="actions">
        <button class="act primary" data-setup-progress-action="${esc(next.key)}"
          >${esc(next.primary_action)}</button>
      </div>
      <div class="section-title">Setup workflows</div>
      ${rows}
    </div>`;
  }
  // Nothing is both permitted AND safe to execute right now (#331 review
  // round 3 finding 1). Two different reasons look the same to a naive
  // "next is null" check but must not be conflated: `next_blocked` names a
  // workflow this caller COULD act on except for an unmet Season
  // prerequisite (guidance to surface, not a CTA that would just fail), vs.
  // truly nothing left for this role while other, not-this-role's workflows
  // remain (renders nothing -- see get_setup_progress's docstring).
  const blocked = progress.next_blocked;
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
async function loadSetupProgressCard() {
  const mySeq = ++setupProgressFetchSeq;
  // aria-busy (#331 review round 5 finding 5): re-asserted here, not just
  // left over from render()'s initial paint, so a RETRY (the slot's own
  // aria-busy already flipped back to "false" after the failed fetch that
  // preceded it) is exposed as busy again too, not just the very first load.
  const busySlot = document.getElementById("sp-card-slot");
  if (busySlot) busySlot.setAttribute("aria-busy", "true");
  const sp = await getJSON("/api/v2/setup/progress");
  if (mySeq !== setupProgressFetchSeq) return;  // a newer load already won
  if (sp && !sp.error) { setupProgress = sp; setupProgressError = false; }
  else { setupProgress = null; setupProgressError = true; }
  const slot = document.getElementById("sp-card-slot");
  if (!slot) return;  // navigated away from Dashboard before this resolved
  slot.innerHTML = renderSetupProgressCard(setupProgress, setupProgressError, false);
  slot.setAttribute("aria-busy", "false");
  const spAction = slot.querySelector("[data-setup-progress-action]");
  if (spAction) spAction.onclick = () =>
    goToSetupWorkflow(spAction.dataset.setupProgressAction);
  const spRetry = slot.querySelector("[data-setup-progress-retry]");
  if (spRetry) spRetry.onclick = () => loadSetupProgressCard();
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
    // GLOBALLY (these fields' option lists span every Program, unfiltered
    // -- #159's contextOptions is a display-only selection today, existing
    // screens are not filtered by it), not the one this hub is scoped to.
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
// are not Program-scoped by design; contextOptions is documented as
// "existing screens are not filtered by it yet"). Seeding only the DEFAULT
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
  if (!programId) return { ok: true, values: {} };
  if (kind === "season") return { ok: true, values: { "f-season-league": programId } };
  const hvr = await getJSON("/api/v2/setup/hierarchy");
  const stillCurrent = !!(contextOptions && contextOptions.selected
    && contextOptions.selected.program_id === programId);
  if (!hvr || hvr.error || !stillCurrent) return { ok: false };
  const program = (hvr.programs || []).find((p) => p.id === programId);
  if (!program) return { ok: true, values: {} };
  if (kind === "team") {
    const lgs = program.leagues || [];
    return { ok: true, values: lgs.length ? { "f-team-perm-league": lgs[0].id } : {} };
  }
  if (kind === "player") {
    const teams = (program.leagues || []).flatMap((lg) => lg.teams || [])
      .concat(program.teams_without_league || []);
    return { ok: true, values: teams.length ? { "f-player-team": teams[0].id } : {} };
  }
  return { ok: true, values: {} };
}
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
function focusContentHeading(attempt) {
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
  if ((attempt || 0) < 40) setTimeout(() => focusContentHeading((attempt || 0) + 1), 50);
}

// Destination focus for "participation" (#331 review round 2 finding 4):
// landing generically on the Setup hierarchy tree isn't enough -- focus
// must reach the ACTUAL registration control for the currently-selected
// Season (contextOptions.selected.season_id, #159), the same "Register" add
// row renderSetupHierarchy's league sections render per (season, league)
// (data-reg-add/data-reg-add-season). Same poll-while-loading shape as
// focusContentHeading() (switchTab()'s render() is async, so neither
// target necessarily exists yet on the next tick), but once loading has
// genuinely finished with no matching control -- no league yet, or every
// permanent team is already registered for this Season -- falls back to
// focusContentHeading()'s generic content-region landing rather than
// polling forever for something that will never appear.
function focusParticipationRegisterControl(attempt) {
  const seasonId = contextOptions && contextOptions.selected
    && contextOptions.selected.season_id;
  const btn = seasonId && document.querySelector(
    `[data-reg-add][data-reg-add-season="${CSS.escape(seasonId)}"]`);
  if (btn) { btn.focus(); return; }
  const content = document.getElementById("content");
  const stillLoading = !content || content.querySelector(".skeleton");
  if (stillLoading && (attempt || 0) < 40) {
    setTimeout(() => focusParticipationRegisterControl((attempt || 0) + 1), 50);
    return;
  }
  focusContentHeading();
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
  // ov.schedule is every non-draft game in the whole demo, so counting its
  // full length mislabeled arbitrarily-far-past/future games as "this week".
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
        options: (ov) => [["", "— none —"]].concat((ov.organizations || []).map((o) => [o.id, o.name])) },
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
    list: (ov) => (ov.leagues || []).map((lv) => ({
      id: lv.id, title: lv.name, sub: nameById(ov.seasons, lv.season_id) })),
    fields: [
      { id: "f-level-season", label: "Season", type: "select", required: true, ofNoun: "season",
        options: (ov) => (ov.seasons || []).map((s) => [s.id, s.name]) },
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
      { id: "f-div-league", label: "League", type: "select", required: true, ofNoun: "level",
        options: (ov) => (ov.leagues || []).map((lv) => [lv.id, `${nameById(ov.seasons, lv.season_id)} · ${lv.name}`]) },
      { id: "f-div", label: "Division name", required: true, placeholder: "e.g. Gold" },
      { id: "f-div-age", label: "Age group", placeholder: "e.g. U14 (optional)" }] },
  { key: "club", title: "Clubs", icon: "🏒", noun: "club", perm: "manage_setup",
    delKind: "club",  // a club with no team can be deleted from here (#215)
    list: (ov) => (ov.clubs || []).map((c) => ({ id: c.id, title: c.name })),
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
        options: (ov) => [["", "— none —"]].concat((ov.clubs || []).map((c) => [c.id, c.name])) },
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
    list: (ov) => (ov.organizations || []).map((o) => ({
      id: o.id, title: o.name, sub: o.short_name || "" })),
    fields: [
      { id: "f-org", label: "Facility owner name", required: true, placeholder: "e.g. Summit Ice Facilities" },
      { id: "f-org-short", label: "Short name (optional)", placeholder: "e.g. Summit" }] },
  { key: "venue", title: "Venues", icon: "🏟️", noun: "venue", perm: "manage_arena",
    delKind: "venue",
    // A venue is owned by an organization (#233 canonical) — show its facility
    // owner. Which Seasons may use a venue's ice is a separate, independent
    // grant (SeasonVenueAccess, #233 Slice E) managed under each Season.
    list: (ov) => (ov.venues || []).map((v) => ({
      id: v.id, title: v.name,
      sub: [v.organization_name].filter(Boolean).join(" · ") || "Unassigned" })),
    // No Program field on this form (#233 B2a review r1): canonical Venue
    // create is org-owned only.
    fields: [
      { id: "f-venue", label: "Venue name", required: true, placeholder: "e.g. South Arena" },
      { id: "f-venue-org", label: "Facility owner (organization)", type: "select", ofNoun: "organization", ofNounDisplay: "facility owner",
        options: (ov) => [["", "— none —"]].concat((ov.organizations || []).map((o) => [o.id, o.name])) }] },
  { key: "rink", title: "Rinks", icon: "⛸️", noun: "rink", perm: "manage_arena",
    delKind: "rink",
    list: (ov) => (ov.rinks || []).map((r) => ({
      id: r.id, title: r.name, sub: r.venue_name || "" })),
    fields: [
      { id: "f-rink-venue", label: "Venue", type: "select", required: true, ofNoun: "venue",
        options: (ov) => (ov.venues || []).map((v) => [v.id, v.name]) },
      { id: "f-rink", label: "Rink name", required: true, placeholder: "e.g. Rink 3" }] },
  { key: "ice-slot", title: "Ice slots", icon: "🧊", noun: "ice slot", perm: "manage_arena",
    list: null,  // ice inventory is managed visually on the Arena Calendar
    fields: [
      { id: "f-slot-rink", label: "Rink", type: "select", required: true, ofNoun: "rink",
        options: (ov) => (ov.rinks || []).map((r) => [r.id, `${r.venue_name ? r.venue_name + " · " : ""}${r.name}`]) },
      { id: "f-slot-date", label: "Date", type: "date", required: true, value: "2026-09-05" },
      { id: "f-slot-start", label: "Start", type: "time", required: true, value: "21:00" },
      { id: "f-slot-end", label: "End", type: "time", required: true, value: "22:30" },
      { id: "f-slot-type", label: "Type", type: "select", required: true,
        options: () => [["game", "Game"], ["practice", "Practice"], ["public_skate", "Public skate"],
                        ["maintenance", "Maintenance"], ["tournament", "Tournament"]] }] },
  { key: "official", title: "Officials", icon: "🧑‍⚖️", noun: "official", perm: "manage_schedule",
    delKind: "official",
    list: (ov) => (ov.officials || []).map((o) => ({
      id: o.id, title: o.name, sub: o.home_club_name || "" })),
    fields: [
      { id: "f-official", label: "Official name", required: true, placeholder: "e.g. Riley Whistle" },
      { id: "f-official-club", label: "Home club (optional — for conflict checks)", type: "select",
        options: (ov) => [["", "— none —"]].concat((ov.clubs || []).map((c) => [c.id, c.name])) }] },
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
  division: () => post("/api/v2/setup/division", { league_id: val("f-div-league"), name: val("f-div"), age_group: val("f-div-age") }),
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
    options: (ov) => (ov.organizations || []).map((o) => [o.id, o.name]) },
  "rink:venue": {
    perm: "manage_arena", noun: "venue", nullable: false, risky: false,
    options: (ov) => (ov.venues || []).map((v) => [v.id, v.name]) },
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
    options: (ov) => (ov.clubs || []).map((c) => [c.id, c.name]) },
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
    options: (ov) => (ov.organizations || []).map((o) => [o.id, o.name]) },
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
function reassignPanelHtml(ov) {
  if (!pendingReassign) return "";
  const pr = pendingReassign;
  const cfg = REASSIGN[`${pr.kind}:${pr.parent}`];
  if (!cfg) return "";
  const rows = (cfg.nullable ? [["", "— none —"]] : []).concat(cfg.options(ov, pr));
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
    <div class="modal ${kind}" role="dialog" aria-modal="true" aria-label="${esc(title)}">
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
      && !(sv.teams || []).length && !(sv.organizations || []).length) {
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
  const rinksByVenue = groupBy(sv.rinks, "venue_id");
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
  const venuesByOrg = groupBy(sv.venues, "organization_id");
  const orgSections = (sv.organizations || []).map((o) => {
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

  return `${reassignPanelHtml(ov)}<div class="setup-trees">${facility}${permanentTeams}${competition}${renderSeasonParticipation(hv, ov, sv)}${renderRollover(hv, ov)}${roster}${needsAssignment}</div>`;
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
  const allVenues = (sv && sv.venues) || [];

  const programBlocks = programs.map((program) => {
    const permanentTeams = leagueTeams[program.id] || [];
    const seasons = program.seasons || [];

    const seasonBlocks = seasons.map((s) => {
      const regs = (seasonRegs[s.id] || []).filter((r) => r.active);
      const regByTeam = {};
      regs.forEach((r) => { regByTeam[r.team_id] = r; });
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
      const venueNameById = {};
      allVenues.forEach((v) => { venueNameById[v.id] = v.name; });
      const grantedAccess = (seasonVenueAccess[s.id] || []).filter((a) => a.active);
      const grantedVenueIds = new Set(grantedAccess.map((a) => a.venue_id));
      const venueAccessRows = grantedAccess.map((a) => `<div class="tn-leaf reg-row">
          <span class="tn-label">🏟️ ${esc(venueNameById[a.venue_id] || a.venue_id)}</span>
          <button class="icon-btn danger" data-va-revoke="${esc(a.id)}"
            title="Revoke venue access" aria-label="Revoke ${esc(venueNameById[a.venue_id] || a.venue_id)} from ${esc(s.name)}">${ICONS.circleMinus}</button></div>`).join("")
        || `<div class="tn-empty">No venues allowed for this season yet — games can't be scheduled until one is added.</div>`;
      const availableVenues = allVenues.filter((v) => !grantedVenueIds.has(v.id));
      const venueAddCtl = availableVenues.length
        ? `<div class="tn-leaf reg-add">
            <select id="va-add-${esc(s.id)}"><option value="">Add a venue…</option>${
              availableVenues.map((v) => opt(v.id, v.name)).join("")}</select>
            <button class="act primary" data-va-add="${esc(s.id)}">Allow</button></div>`
        : (allVenues.length
            ? `<div class="tn-empty">Every venue is already allowed for this season.</div>`
            : `<div class="tn-empty">Create a venue on the Facility tree first, then allow it here.</div>`);
      const venueAccessSection = `<details class="tn" open><summary class="tn-sum">
          <span class="tn-label">🏟️ Allowed venues</span>
          <span class="tn-meta">${grantedAccess.length} venue${grantedAccess.length === 1 ? "" : "s"}</span></summary>
        <div class="tn-children">${venueAccessRows}${venueAddCtl}</div></details>`;

      // Revoked venue access (#233 Slice E, mirrors #251's inactive
      // registrations exactly): revoke only deactivates a row, preserving
      // history — but delete_season()/delete_venue() block on a matching
      // access row REGARDLESS of active status, so a revoked row still needs
      // this explicit, audited permanent-cleanup path before the Season or
      // Venue it references can ever be deleted.
      const revokedAccess = (seasonVenueAccess[s.id] || []).filter((a) => !a.active);
      const revokedAccessRows = revokedAccess.map((a) => {
        const venueName = venueNameById[a.venue_id] || a.venue_id;
        return `<div class="tn-leaf reg-row inactive-reg">
          <span class="tn-label">🏟️ ${esc(venueName)}</span>
          <span class="tn-meta">revoked · <code>${esc(a.id)}</code></span>
          ${delBtn("season-venue-access", a.id, `${venueName} access`,
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
        // scoped to this season) plus Save/Remove. `reg` is looked up by team
        // id — structurally guaranteed present since t only appears here
        // because a valid registration resolved it into this League/Division.
        const regRow = (t, divId) => {
          const reg = regByTeam[t.id];
          if (!reg) return "";
          return `<div class="tn-leaf reg-row">
            <span class="tn-label">👥 ${esc(t.name)}</span>
            <select id="reg-league-${esc(reg.id)}" data-reg-league-for="${esc(reg.id)}">${leagueOptsFor(lv.id)}</select>
            <select id="reg-div-${esc(reg.id)}" data-reg-div-for="${esc(reg.id)}"><option value="">No division</option>${divOptsFor(divId)}</select>
            <button class="act" data-reg-save="${esc(reg.id)}" data-reg-orig-league="${esc(lv.id)}" data-reg-orig-div="${esc(divId || "")}">Save</button>
            <button class="icon-btn danger" data-reg-remove="${esc(reg.id)}"
              title="Remove from season" aria-label="Remove ${esc(t.name)} from ${esc(s.name)}">${ICONS.circleMinus}</button></div>`;
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
    const eligible = [], already = [], ineligible = [];
    srcRegs.forEach((r) => {
      const team = teamById(r.team_id);
      if (!team) ineligible.push(r);
      else if (targetActive.has(r.team_id)) already.push(team);
      else eligible.push(team);
    });
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
    const carryRows = eligible.map((team) => {
      const perm = teamPermLeague[team.id];
      const permInTo = perm && toLeagues.find((lv) => lv.id === perm.id);
      const leagueCell = perm
        ? `<select class="reg-league" data-rollover-league="${esc(team.id)}"><option value="${esc(perm.id)}" selected>${esc(perm.name)}</option></select>`
        : `<select class="reg-league" data-rollover-league="${esc(team.id)}"><option value="">No permanent league</option></select>`;
      const initialDivs = permInTo ? (permInTo.divisions || []) : [];
      return `<div class="tn-leaf reg-row">
        <label class="ro-pick"><input type="checkbox" data-rollover-pick="${esc(team.id)}">
          <span class="tn-label">👥 ${esc(team.name)}</span></label>
        ${leagueCell}
        <select class="reg-div" data-rollover-div="${esc(team.id)}"><option value="">No division</option>${
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
    const league = c.querySelector(`[data-rollover-league="${cb.dataset.rolloverPick}"]`);
    const assigned = !!(league && league.value);
    const row = cb.closest(".reg-row");
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

function renderSetup(sv, hv, ov) {
  const toggle = `<div class="seg-group setup-viewtoggle">
    <button class="seg ${setupView === "hierarchy" ? "active" : ""}" data-setup-view="hierarchy">Hierarchy</button>
    <button class="seg ${setupView === "records" ? "active" : ""}" data-setup-view="records">Records</button>
  </div>`;
  let body;
  if (setupView === "hierarchy") {
    body = `${pageIntro("Review and fix how your venues, programs, teams, and rosters are connected.")}${renderSetupHierarchy(sv, hv, ov)}`;
  } else {
    const cards = SETUP_ENTITIES.map((ent) => setupCard(ent, sv)).join("");
    body = `<div class="setup-intro">Create your competition structure and arena. Tap
      <strong>＋ New</strong> on any card to open a form.</div>
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
  // fall back to the field's default only on first open.
  const current = f.id in drawerValues ? drawerValues[f.id] : (f.value || "");
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
  return `<div class="drawer-scrim" data-drawer-close></div>
    <aside class="drawer" role="dialog" aria-modal="true" aria-label="${heading}">
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
  // Season" (#331 review round 5 finding 4): `seasons` here spans every
  // Program (GET /api/demo/overview is unfiltered), so picking the first
  // status==="active" row could default the builder onto a DIFFERENT
  // Program's Season than the one the Home/Tasks hub CTA was scoped to. A
  // committed submit against that silent wrong default would generate ice
  // for the wrong Program. Falls back to the old global-first behavior only
  // when no Season is actively selected (a Program-only context, or no
  // context at all) or the selected id no longer resolves (a stale/deleted
  // Season) -- never left unset when a real active/global one exists.
  const selectedId = contextOptions && contextOptions.selected
    && contextOptions.selected.season_id;
  const selected = selectedId ? seasons.find((s) => s.id === selectedId) : null;
  const active = selected
    || seasons.find((s) => s.status === "active") || seasons[0] || null;
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
  // send season_id verbatim to their commit endpoint, and the switcher is
  // display-only -- nothing else validates "matches the active
  // context"). Rinks are ALSO Program/Venue-scoped, so a context change
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
  const seasonOpts = seasons.map((s) =>
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
    <span class="li-time">${fmt(g.start_time)}</span>
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
      const gRows = games.map((g) => `<div class="li">
        <span class="li-time">${fmt(g.start_time)}</span>
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
  let ov, sv, hv, board, lineups, standings, inbox, playerHome;
  try {
    c.innerHTML = `<div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div>`;
    ov = await getJSON("/api/demo/overview");
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
    }
    // Canonical Setup structure (#233 B2a). `sv` (the flat overview) is gated
    // MANAGE_ARENA server-side — both League Admin and Arena Manager hold it
    // — because Arena Managers need it for their own Organization/Venue/
    // Rink/Ice-slot cards and the Facility tree (#233 B2a review r1: this
    // used to crash for them, since it was fetched only under manage_setup).
    if (view === "setup" && (hasPerm("manage_setup") || hasPerm("manage_arena"))) {
      const svr = await getJSON("/api/v2/setup/overview");
      if (svr && !svr.error) sv = svr;
    }
    // The Competition tree, Setup Player card, and season participation are
    // MANAGE_SETUP-only (roster-adjacent / competition-structure data an
    // Arena Manager doesn't manage) — their own authenticated calls, never
    // bundled into the public demo overview.
    if (view === "setup" && hasPerm("manage_setup")) {
      const pl = await getJSON("/api/players");
      playersList = Array.isArray(pl) ? pl : [];
      // The canonical Program→Season→League→Division tree (#233 B2a review
      // r1): consumed as-is rather than reconstructed from flat `sv` lists,
      // so needs_assignment/teams_without_division match the canonical
      // parentage rules exactly instead of a client-side reinterpretation.
      const hvr = await getJSON("/api/v2/setup/hierarchy");
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
      permLeaguesByProgram = {}; allPermLeagues = []; teamPermLeague = {};
      for (const program of (hv.programs || [])) {
        const r = await getJSON(`/api/v2/setup/programs/${program.id}/teams`);
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
          seasonRegs[s.id] = (rr && rr.registrations) || [];
          // Allowed venues (#233 Slice E): which Venues this Season may use
          // for game ice, independent of any Venue-Program ownership.
          const va = await getJSON(`/api/v2/setup/seasons/${s.id}/venue-access`);
          seasonVenueAccess[s.id] = (va && va.venue_access) || [];
        }
      }
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
      if (!schedulerState.division && ov.divisions[0]) {
        schedulerState.division = ov.divisions[0].id;
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
    // round 7) — `ov.seasons` is unfiltered (every Program), and
    // goToSetupWorkflow("import") does no seeding of its own (it only
    // switches tabs), so the old fallback was a silent, COMMITTABLE
    // cross-Program default: needsSeason import types send seasonId
    // verbatim to commit_import, and #159's context is display-only, not
    // a backend filter, so nothing else would have caught it. Re-binds on
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
    }
    // The Dashboard shows a standings snapshot for the first division.
    if (view === "dashboard" && ov.divisions[0]) {
      standings = await getJSON(`/api/standings/${ov.divisions[0].id}`);
    }
    // Home/Tasks hub setup-progress card (#330) — only for a role that can
    // act on Setup (League Admin/Arena Manager); a Coach also lands on
    // "dashboard" (canSeeOpsConsole) but has nothing to do with this. The
    // fetch itself happens independently, in loadSetupProgressCard() below
    // (#331 review round 2 finding 3) — not awaited inline here, so it
    // never blocks the rest of the Dashboard from painting.
    setupProgress = null;
    setupProgressError = false;
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
    c.innerHTML = `<div class="banner neutral"><h2>Restricted</h2>
      <p>${esc(lineups.error.message
        || "You don't have access to this game's roster.")}</p></div>`;
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
  c.innerHTML =
    view === "dashboard" ? (showSetupCard
        ? `<div id="sp-card-slot" role="status" aria-live="polite" aria-busy="true">${renderSetupProgressCard(null, false, true)}</div>` : "")
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
  // The themed confirm/blocked modal (#215) overlays whatever view is showing
  // (it can be opened from the header's Reset demo action too), so append it
  // after the view content on every render and wire it below.
  c.innerHTML += renderModal();

  wireModal(c);
  c.querySelectorAll("[data-goto]").forEach((b) => b.onclick = () => switchTab(b.dataset.goto));
  // Home/Tasks hub setup-progress card (#330): its own fetch, content, and
  // click-handler wiring all happen independently in loadSetupProgressCard
  // (#331 review round 2 finding 3) -- the slot painted above is only ever
  // the loading skeleton at this point, so there's nothing of the card's
  // own to wire here yet.
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
    setupView = b.dataset.setupView; toast = ""; render();
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
    const teamId = sel.dataset.rolloverLeague;
    const divSel = c.querySelector(`[data-rollover-div="${teamId}"]`);
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
        const league = c.querySelector(`[data-rollover-league="${cb.dataset.rolloverPick}"]`);
        const div = c.querySelector(`[data-rollover-div="${cb.dataset.rolloverPick}"]`);
        if (league && league.value) {
          selections.push({ team_id: cb.dataset.rolloverPick, league_id: league.value,
                             division_id: (div && div.value) || null });
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
  if (drawer) {
    const first = c.querySelector(".drawer-body input, .drawer-body select");
    if (first) first.focus();
  }
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
    const res = await post("/api/scheduler/draft", { division_id: schedulerState.division });
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
    toastIsError = true;  // instructional, not a completed action — don't auto-clear
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
      toastIsError = true;
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
    if (v === 0) calendarDate = "2026-09-05";
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
  if (ibOpen) ibOpen.onclick = () => { iceBuilder = { form: null, preview: null }; toast = ""; render(); };
  const ibCancel = c.querySelector("[data-ib-cancel]");
  if (ibCancel) ibCancel.onclick = () => { iceBuilder = null; toast = ""; render(); };
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
    iceBuilder.form = readIceBuilderForm(c);
    if (hadPreview) { iceBuilder.preview = null; toast = ""; }
    if (hadPreview || isWeekday) render();
  });
  c.querySelectorAll("[data-ib-preview]").forEach((b) => b.onclick = async () => {
    toast = "";
    iceBuilder.form = readIceBuilderForm(c);
    iceBuilder.preview = await post("/api/setup/ice-availability/preview", iceBuilder.form);
    render();
  });
  const ibCommit = c.querySelector("[data-ib-commit]");
  if (ibCommit) ibCommit.onclick = async () => {
    toast = "";
    // Bind the commit to the previewed template: send the fingerprint the
    // preview returned so the server refuses a form edited since (belt to the
    // frontend's suspenders, which already drops the preview on any edit).
    const fingerprint = iceBuilder.preview && iceBuilder.preview.template_fingerprint;
    iceBuilder.form = readIceBuilderForm(c);
    const res = await post("/api/setup/ice-availability/commit",
      { ...iceBuilder.form, template_fingerprint: fingerprint });
    const reason = res && res.error && res.error.details && res.error.details.reason;
    if (res && !res.error) {
      toast = `Created ${res.totals.created} ice slot(s).`; iceBuilder = null;
    } else if (reason === "preview_mismatch") {
      // The proposal changed since Preview — a slipped-through form edit, or a
      // concurrent Season/timezone change moved the resolved slots. Refresh the
      // preview so the operator reviews the CURRENT slots before creating again;
      // never commit the stale set.
      toast = "The schedule changed since preview — showing the updated proposal. Review, then create again.";
      iceBuilder.preview = await post("/api/setup/ice-availability/preview", iceBuilder.form);
    } else {
      iceBuilder.preview = res;
    }
    render();
  };
  const ibExclAdd = c.querySelector("[data-ib-excl-add]");
  if (ibExclAdd) ibExclAdd.onclick = () => {
    iceBuilder.form = readIceBuilderForm(c);
    const el = c.querySelector("#ib-excl");
    const d = el && el.value;
    if (d && !iceBuilder.form.exclusion_dates.includes(d)) iceBuilder.form.exclusion_dates.push(d);
    iceBuilder.preview = null;   // exclusions changed the template — re-preview
    render();
  };
  c.querySelectorAll("[data-ib-excl-remove]").forEach((b) => b.onclick = () => {
    iceBuilder.form = readIceBuilderForm(c);
    iceBuilder.form.exclusion_dates =
      iceBuilder.form.exclusion_dates.filter((x) => x !== b.dataset.ibExclRemove);
    iceBuilder.preview = null;   // exclusions changed the template — re-preview
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
}

function switchTab(next) {
  view = next; toast = ""; if (next !== "calendar") { wizard = null; conflict = null; movingGameId = null; pendingMove = null; iceBuilder = null; }
  if (next !== "setup") { drawer = null; drawerError = ""; drawerValues = {}; pendingReassign = null; }
  // A pending checkout confirmation doesn't survive leaving Home (#107) —
  // same reset discipline as drawer/wizard above, so a stale "are you
  // sure?" never reappears over changed attendance state.
  if (next !== "player_home") { checkoutConfirm = null; oppDetailGame = null; oppDetail = null; }
  // Same discipline for the guardian surface (#26): leaving "My Players"
  // clears any open junior checkout confirm / opportunity detail.
  if (next !== "guardian_home") { gCheckout = null; gOpp = null; gOppDetail = null; }
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
function encodeContextHash(programId, seasonId) {
  try {
    return "#ctx=" + b64urlEncode(
      JSON.stringify({ v: 1, p: programId || null, s: seasonId || null }));
  } catch (_) { return ""; }
}
function decodeContextHash(hash) {
  if (!hash || hash.indexOf("#ctx=") !== 0) return null;
  try {
    const o = JSON.parse(b64urlDecode(hash.slice(5)));
    if (!o || o.v !== 1 || !o.p) return null;
    return { program_id: o.p, season_id: o.s || null };
  } catch (_) { return null; }
}
// Reflect the current selection in the URL (replaceState, like the #public
// precedent) so a reload/bookmark restores it; never touch the #public route.
function syncContextHash() {
  if (!currentUser || location.hash === "#public") return;
  const sel = contextOptions && contextOptions.selected;
  const want = (sel && sel.program_id)
    ? encodeContextHash(sel.program_id, sel.season_id) : "";
  if (location.hash !== want) {
    history.replaceState(null, "", location.pathname + location.search + want);
  }
}
// Load the caller's AUTHORIZED options + current selection. Session-only; a
// signed-out user has no context.
async function loadContextOptions() {
  if (!currentUser) { contextOptions = null; return; }
  const o = await getJSON("/api/context/options");
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
          && (link.season_id || null) === (sel.season_id || null))) {
    syncContextHash();
    return;
  }
  const r = await post("/api/context",
    { program_id: link.program_id, season_id: link.season_id });
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
// Persist a switcher pick, then reflect it in the hash and re-render.
async function setActiveContext(programId, seasonId) {
  const r = await post("/api/context",
    { program_id: programId, season_id: seasonId || null });
  if (!r || r.error) {
    // Generic, no existence oracle (the backend returns the same not-found
    // whether it doesn't exist or isn't ours). Refresh options in case the
    // authorized set shifted underneath us.
    toast = "That Program/Season isn't available."; toastIsError = true;
    await loadContextOptions();
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
      season_id: r.season_id, read_only: !!r.read_only };
  }
  // Bump BEFORE render() below (#331 review round 7) so any view painted by
  // this same cycle -- the Import wizard, an open Ice Builder -- already
  // sees the new generation and rebinds instead of reusing what it cached
  // under the selection this call just moved away from.
  contextRevision += 1;
  syncContextHash();
  // Then reconcile the whole option set from a fresh GET so the label/status/
  // read-only badge reflect canonical state at POST time — a Season may have
  // been archived/reopened or newly authorized since options loaded. Rendering
  // from the pre-POST rows would show a stale row (a now-archived Season shown
  // as writable, or `selected` absent from the offered options). Re-sync after,
  // in case the canonical selection differs from the POST echo (a concurrent
  // change), so the hash always matches what is rendered.
  await loadContextOptions();
  syncContextHash();
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
// persistent "display only" note reflect the current state in the CLOSED view.
function renderContextSwitcher() {
  const wrap = document.getElementById("context-switcher");
  const select = document.getElementById("ctx-select");
  const chip = document.getElementById("ctx-static");
  const roBadge = document.getElementById("ctx-ro");
  if (!wrap || !select || !chip || !roBadge) return;
  const opts = contextOptions;
  const show = !!(currentUser && opts && opts.programs && opts.programs.length);
  wrap.hidden = !show;
  if (!show) return;
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
    return;
  }
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
// Wire the native select once. A native <select> gives the full keyboard /
// screen-reader contract (focus, Arrow/Home/End, type-ahead, Enter/Escape) for
// free, so no custom menu-radio handling is needed.
const ctxSelect = document.getElementById("ctx-select");
if (ctxSelect) ctxSelect.onchange = (e) => {
  const raw = e.target.value || "";
  const bar = raw.indexOf("|");
  const p = bar >= 0 ? raw.slice(0, bar) : raw;
  const s = bar >= 0 ? raw.slice(bar + 1) : "";
  setActiveContext(p, s || null);
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
  toggle('.tab[data-tab="setup"]', hasPerm("manage_setup") || hasPerm("manage_arena"));
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
  // Hide a nav group once all of its tabs are hidden, so its section label
  // (Home / Schedule / People / …) doesn't hang orphaned above nothing.
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
async function renderPublicGuest() {
  const box = document.getElementById("public-content");
  if (!box) return;
  updateToast();  // the guest screen has no other render() path to surface post() errors
  box.innerHTML = `<div class="skeleton"></div><div class="skeleton"></div>`;
  // A network failure or a non-JSON/5xx response — getJSON now surfaces the
  // latter as {error} rather than throwing — must give an anonymous visitor the
  // same clear error + Retry a signed-in user gets, not an empty schedule or an
  // infinite skeleton (#133).
  const fail = (message) => {
    box.innerHTML = `<div class="banner alert"><h2>Could not load the public schedule</h2>
      <p>${esc(message)}</p></div>
      <div class="actions"><button class="act primary" id="public-retry-btn">Retry</button></div>`;
    const retry = document.getElementById("public-retry-btn");
    if (retry) retry.onclick = () => renderPublicGuest();
  };
  try {
    const sch = await getJSON("/api/public/schedule");
    if (sch && sch.error) return fail(sch.error.message);
    publicState.schedule = sch || { fixtures: [], divisions: [] };
    if (!publicState.division && publicState.schedule.divisions[0]) {
      publicState.division = publicState.schedule.divisions[0].id;
    }
    if (publicTab === "standings" && publicState.division) {
      const st = await getJSON(`/api/public/standings/${publicState.division}`);
      if (st && st.error) return fail(st.error.message);
      publicState.standings = st;
    }
  } catch (e) {
    return fail(e.message || String(e));
  }
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
  document.body.classList.add("signed-out");
  const login = document.getElementById("login-screen");
  if (login) login.hidden = true;
  const screen = document.getElementById("public-screen");
  if (screen) screen.hidden = false;
  if (location.hash !== "#public") history.replaceState(null, "", location.pathname + location.search + "#public");
  const signIn = document.getElementById("public-signin-link");
  if (signIn) signIn.onclick = () => showLogin();
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
