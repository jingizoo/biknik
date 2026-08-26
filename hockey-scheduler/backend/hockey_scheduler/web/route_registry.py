"""The RouteSpec inventory: one entry per live (method, path) the server
dispatches (#202 step 1).

NO LONGER INERT (#202 wiring step). Through the routespec-inventory step
nothing in ``server.py`` imported this module and no response could change as
a result -- that was true then and is not true now: ``server.py`` imports
``REGISTRY`` to build ``_GET_ROUTES``/``_POST_ROUTES``, the 405/Allow
admission source (see server.py's own comment above their definition, and
``tests/test_route_registry.py``'s ``KindClassificationTests`` for what makes
that safe). The registry exists so LATER steps of #202 (full auth/schema/
rate-limit enforcement) have one declared place to enforce from, and so CI
already refuses two kinds of drift:

  UNCLASSIFIED — a live dispatch branch with no ``RouteSpec``
  DEAD         — a ``RouteSpec`` matching no live dispatch branch

Both are decided against the DISPATCH ITSELF, not against another hand-written
list: ``route_extract.py`` parses ``server.py`` with ``ast`` and reports the
branches it actually contains, and ``tests/test_route_registry.py`` compares
the two sets. A registry checked against a transcription would only prove that
two prose lists agree.

Identity: ``template``
----------------------
Each spec carries BOTH

  ``pattern``   the full-path regex, faithful to what the dispatch tests --
                a literal ``assign-organization`` stays a LITERAL, not the
                looser ``assign-\\w+`` a naive reading of the outer dispatch
                regex would suggest (see #202 repair root cause 1: the
                target is resolved by ``_handle_reassign``'s own combo
                schema, which is exactly the set of literals a spec here
                may claim -- a bare ``\\w+``/``[^/]+`` wildcard is never a
                real leaf on its own); this is what a policy engine will
                match a real request path against;
  ``template``  the canonical shape — ``{}`` for a ``[^/]+`` segment, ``{w}``
                for the narrower ``\\w+`` (kept distinct: #202 repair root
                cause 4), ``{*}`` for a non-empty free tail (``.+``), ``{*0}``
                for a possibly-empty one (``.*``, or a ``startswith()``
                prefix route) — the key both sides can derive, so the gate
                compares templates and separately proves that each
                ``pattern`` expands to exactly its own ``template``.

What is NOT here
----------------
* ``CONTEXT_SCOPED_READ_ROUTES`` in ``server.py`` still exists as a separate,
  hand-maintained table and is still the code path that runs; this file only
  CROSS-CHECKS it against this registry (see the gate). Rewiring it is
  separate, later work (#202 enforcement) -- narrower in scope than this step.
* ``_GET_ROUTES`` / ``_POST_ROUTES`` in ``server.py`` are NO LONGER a second
  hand-maintained table (#202 wiring step): they are now derived directly from
  ``REGISTRY`` at import time -- every ``kind="route"`` entry whose pattern is
  scoped to ``/api/`` (see server.py's own comment above their definition for
  the exact filter and why each exclusion reproduces this table's PRE-EXISTING
  scope rather than silently widening it). ``RouteSpec.kind`` is therefore
  LOAD-BEARING now, in a way the dispatch-vs-registry gate above does not
  check (that gate compares ``(method, template)`` set membership only, never
  ``kind``) -- see ``tests/test_route_registry.py``'s ``KindClassificationTests``
  for the two structural invariants that close that gap: no ``kind="route"``
  entry may carry a free-tail token (``{*}``/``{*0}`` -- that is exactly what
  distinguishes a family/fallthrough/the static tail from a single concrete
  leaf, #202 repair root cause 1's own lesson), and every non-``"route"``
  entry is pinned by name so retagging even one is a conspicuous, reviewed
  diff line, the same discipline ``_AUDIT_WAIVERS`` uses in route_extract.py.
* ``auth`` and ``scope_axis`` were declared slots for that later work when
  this section was first written; they are NOT ANY MORE (#202 repair round
  4, finding 4). Every REACHABLE spec now carries a real classification --
  238 of them, as of this writing -- and ``tests/test_route_registry.py``'s
  ``_VALID_AUTH_VALUES``/``_EXPECTED_CLASSIFICATION`` CI-GATES both fields
  for every reachable spec: an unclassified reachable entry, a value outside
  the declared vocabulary, or a classification that drifts from what
  ``_EXPECTED_CLASSIFICATION`` independently expects all fail CI. What
  remains true from the ORIGINAL design intent is only "nothing reads
  either field AT RUNTIME" -- classification and CI-gating are done;
  wiring auth/scope enforcement into the request path itself is still
  LATER #202 work, not this repair round's. The one entry that stays
  ``UNCLASSIFIED``, deliberately, is ``get_empty_path`` -- an impossible
  fallback shape (unreachable over HTTP; see its own note below) with no
  real request ever able to reach it to classify.
* ``do_POST``'s tail (``_unmatched_route("POST")``) is an UNCONDITIONAL
  fallthrough, not a branch, and so has no spec. ``_dispatch_get``'s
  equivalent tail hands any non-``/api/`` path to ``_serve_static`` -- which
  DOES get a spec now (``get_static_tail``, #202 repair root cause 6): unlike
  the POST tail, ``_serve_static``'s own else branch re-derives a filename
  from the path and serves a REAL file or 404s, which is a reachable leaf
  (the current files under ``web/static/``), not an unreachable fallthrough.
  The ``if``/``elif`` branches INSIDE ``_serve_static`` (the ``/setup`` and
  ``/`` shells) also appear, as ``kind="static"``.

Reading a POST entry
--------------------
``do_POST`` refuses anything ``_supported_methods`` (i.e. ``_POST_ROUTES``)
does not admit BEFORE the dispatch chain runs, so a POST spec here describes a
branch that exists in the dispatch — not, on its own, a path that answers. The
gate's cross-check reports where the two disagree.
"""
from dataclasses import dataclass

#: #202 repair round 6, finding 4: NOT "a slot no one has filled in yet" --
#: every reachable spec IS classified (see the module docstring's own
#: accounting, above). This is the value reserved for the one spec that
#: is deliberately, permanently EXCLUDED from classification --
#: ``get_empty_path``, an impossible fallback shape unreachable over HTTP
#: (see its own note in REGISTRY) -- and it is not "read by nothing"
#: either: ``tests/test_route_registry.py``'s own CI gate
#: (``_VALID_AUTH_VALUES``/``_EXPECTED_CLASSIFICATION``,
#: ``RegistryInternalConsistencyTests.
#: test_no_spec_is_half_classified_or_reverted``) reads and enforces it
#: directly. What remains true is narrower: nothing in server.py reads
#: this value AT RUNTIME while dispatching a request -- see the module
#: docstring's own note on that.
UNCLASSIFIED = "unclassified"


@dataclass(frozen=True)
class RouteSpec:
    """One live (method, path-pattern) the dispatch selects.

    method   ``GET`` or ``POST`` — the two verbs with a dispatch chain. HEAD
             mirrors GET through ``do_GET``; PUT/PATCH/DELETE/OPTIONS answer
             from ``_supported_methods`` and select no route of their own.
    pattern  full-path regex, faithful to the dispatch's own test.
    template ``{}``/``{*}`` canonical shape — the identity the gate compares.
    name     stable identifier, derived from the path so it stays stable while
             the path does.
    handler  the ``Handler`` method whose body holds the branch (verified
             against the extractor, so it cannot rot into decoration).
    kind     structural: ``route`` (a resource), ``family`` (a branch broader
             than the real routes under it), ``fallthrough`` (an answer for
             paths no route claimed) or ``static`` (a file/shell).
    auth / scope_axis
             CLASSIFIED and CI-GATED for every reachable spec (#202 repair
             round 4, finding 4; see tests/test_route_registry.py's
             ``_VALID_AUTH_VALUES``/``_EXPECTED_CLASSIFICATION``) but NOT
             YET runtime-enforced -- nothing in server.py reads either
             field while dispatching a request. Only ``get_empty_path``
             (an unreachable-over-HTTP fallback shape) stays UNCLASSIFIED.
    note     free text; provenance for the odd entries.
    """

    method: str
    pattern: str
    template: str
    name: str
    handler: str
    kind: str = "route"
    auth: str = UNCLASSIFIED
    scope_axis: str = UNCLASSIFIED
    note: str = ""

    @property
    def key(self) -> tuple:
        return (self.method, self.template)


REGISTRY = (

    # -- GET ---------------------------------------------------------------
    RouteSpec("GET", r"^$", "", "get_empty_path", "_serve_static",
              kind="static",
              note=("unreachable over HTTP — a request line always "
                    "carries at least '/'; listed because the branch "
                    "exists. #202: deliberately EXCLUDED from auth/"
                    "scope_axis classification -- an impossible fallback "
                    "shape, not a reachable leaf to classify (see "
                    "test_route_registry.py's own pin on this entry, "
                    "RegistryInternalConsistencyTests."
                    "test_no_spec_is_half_classified_or_reverted).")),
    RouteSpec("GET", r"^/$", "/", "get_index", "_serve_static", kind="static",
              auth="none", scope_axis="none",
              note=("#202: _serve_static (server.py:1273-1309) -- no "
                    "_resolve_role call anywhere in the branch, "
                    "unconditionally serves index.html.")),
    RouteSpec("GET", r"^/api/accounts$", "/api/accounts", "get_accounts",
              "_dispatch_get",
              auth="operator_only", scope_axis="none",
              note=("#202: _operator_only('/api/accounts') (server.py:"
                    "1718). list_user_accounts (service.py:5448-5450) is "
                    "self.accounts.list_accounts() -- installation-wide, "
                    "no P/S/L filter.")),
    RouteSpec("GET", r"^/api/accounts/[^/]+/sessions$",
              "/api/accounts/{}/sessions", "get_accounts_id_sessions",
              "_dispatch_get",
              auth="operator_only", scope_axis="none",
              note=("#202: _operator_only(path) (server.py:1725-1726). "
                    "list_account_sessions (service.py:5486-5495) filters "
                    "by account_id only -- no P/S/L concept.")),
    RouteSpec("GET", r"^/api/auth/accounts$", "/api/auth/accounts",
              "get_auth_accounts", "_dispatch_get",
              auth="none", scope_axis="none",
              note=("#202: no _resolve_role/_operator_only call anywhere "
                    "in the branch (server.py:1703-1714) -- gated only by "
                    "app_mode: production returns an empty list (avoids "
                    "exposing real usernames/roles, #68), non-production "
                    "returns the full demo login-picker list "
                    "unauthenticated. list_user_accounts is "
                    "installation-wide -- no P/S/L filter, not "
                    "applicable.")),
    RouteSpec("GET", r"^/api/auth/me$", "/api/auth/me", "get_auth_me",
              "_dispatch_get",
              auth="optional_session", scope_axis="none",
              note=("#202 repair round 4, finding 5: relabelled from "
                    "'session' -- that label means REFUSED with no "
                    "session (401), which this route does NOT do. "
                    "Investigated (not assumed): server.py:2045-2057 is a "
                    "deliberate, documented 'who am I' pattern -- direct "
                    "cookie/session lookup (self._cookie(SESSION_COOKIE) + "
                    "SESSIONS.resolve), NOT _resolve_role. No cookie -> "
                    "signed-out view (200, {'user': None}) -- the exact "
                    "contract every SPA needs on load to tell signed-out "
                    "from signed-in without an error. A PRESENT but "
                    "invalid/expired cookie still -> 401 (must re-auth), "
                    "so this is not a blanket public route either -- "
                    "'optional_session' names that middle contract "
                    "explicitly. Verified over real HTTP, all three cookie "
                    "states, in test_server_authz.py's "
                    "OptionalSessionRouteTests. Returns only the caller's "
                    "own account (user_view) -- no P/S/L concept, not "
                    "applicable.")),
    RouteSpec("GET", r"^/api/auth/roles$", "/api/auth/roles",
              "get_auth_roles", "_dispatch_get",
              auth="none", scope_axis="none",
              note=("#202: no guard, no identity read at all (server.py:"
                    "1692-1702) -- returns the static role/permission "
                    "catalog, identical for every caller. Installation-"
                    "wide constant, no P/S/L concept.")),
    RouteSpec("GET", r"^/api/bootstrap/status$", "/api/bootstrap/status",
              "get_bootstrap_status", "_dispatch_get",
              auth="none", scope_axis="none",
              note=("#202: rate-limited only, no _resolve_role (server.py:"
                    "1643-1649). installation_claim_status is a one-time "
                    "claim posture bit -- no P/S/L concept.")),
    RouteSpec("GET", r"^/api/calendar-feeds$", "/api/calendar-feeds",
              "get_calendar_feeds", "_dispatch_get",
              auth="session+MANAGE_SCHEDULE-or-self", scope_axis="none",
              note=("#202: _feed_guard (server.py:677-692, called at "
                    "1818-1819) -- operator (can(role, "
                    "Permission.MANAGE_SCHEDULE)) manages any actor, else "
                    "only the caller's own actor (_own_feed_actor). "
                    "list_calendar_feed_tokens is keyed by (actor_type, "
                    "actor_ref) -- not a Program/Season/League concept, "
                    "not applicable.")),
    RouteSpec("GET", r"^/api/context$", "/api/context", "get_context",
              "_dispatch_get",
              auth="session", scope_axis="none",
              note=("#202: _resolve_role (server.py:1408), user_id is "
                    "None -> 401 (server.py:1412-1415) -- a real session "
                    "is required, never the identity-less demo fallback "
                    "alone. get_active_context (service.py:271-285) "
                    "resolves and returns the caller's OWN active "
                    "Program/Season/League selection -- it enumerates the "
                    "axes rather than filtering a resource by one of "
                    "them, not applicable.")),
    RouteSpec("GET", r"^/api/context/options$", "/api/context/options",
              "get_context_options", "_dispatch_get",
              auth="session", scope_axis="none",
              note=("#202: _resolve_role (server.py:1423), user_id is "
                    "None -> 401 (server.py:1427-1430). "
                    "get_context_options (service.py:322-375) enumerates "
                    "every Program/Season/League the caller is authorized "
                    "for ('filtered through the SAME scope rules as "
                    "get/set') -- it IS the definition of the caller's "
                    "available axes, not a single-axis filter over "
                    "another resource, not applicable.")),
    RouteSpec("GET", r"^/api/demo/overview$", "/api/demo/overview",
              "get_demo_overview", "_dispatch_get",
              auth="session", scope_axis="cross",
              note=("#202: _resolve_role (server.py:1390), user_id is "
                    "None -> 401 (server.py:1394-1397). get_demo_overview "
                    "(service.py:8649-8676) narrows every Program/Season/"
                    "League-joined collection to the resolved active "
                    "context: mandatory Program, the resolved Season as a "
                    "HARD ceiling, and the selected League (else the "
                    "broader 'No League' view) -- validates every axis it "
                    "touches, exactly #409's 'cross' class.")),
    RouteSpec("GET", r"^/api/games/[^/]+$", "/api/games/{}", "get_games_id",
              "_dispatch_get",
              auth="none", scope_axis="none",
              note=("#202: server.py:2058-2065 -- the 'sub is None' branch "
                    "returns api.get_game(gid) directly, no _resolve_role "
                    "call: the bare game record (teams/time/rink/score) "
                    "is a deliberate public fixture (#73), no player "
                    "data. get_game is keyed by game id only -- no P/S/L "
                    "filter, not applicable.")),
    RouteSpec("GET", r"^/api/games/[^/]+/availability-summary$",
              "/api/games/{}/availability-summary",
              "get_games_id_availability_summary", "_dispatch_get",
              auth="session+own-side-projection", scope_axis="none",
              note=("#202: _resolve_role (server.py:2851) then "
                    "can_read_private_game_data (scope.py:230-259 -- "
                    "operators any, coach/player only their own team via "
                    "the game, official only if assigned; server.py:2859) "
                    "gates every sub-branch. "
                    "#427 final blocker round 2: this leaf's own extra "
                    "narrowing used to be spelled inline here and named "
                    "only COACH and PLAYER, so an assigned OFFICIAL fell "
                    "straight through it -- and this is the ONLY leaf of "
                    "the private-game family that reads a side from the "
                    "QUERY STRING, so the client hint was the sole side "
                    "selector: measured tri-store over a real session, an "
                    "official's un-hinted call was 400 while "
                    "?team_id=<either side> was 200 with that side's whole "
                    "candidate pool, NAMES and per-player availability "
                    "included -- the two classes the /lineups projection "
                    "strips for that same role. It is now projected by the "
                    "SAME lineup_visibility.route_audience (server.py:2948-"
                    "2991 -> service.py:4227-4307) its four siblings use, "
                    "on the SAME trusted server-resolved own side: an "
                    "unscoped operator keeps the hint (unchanged), a "
                    "Coach/Player has the hint IGNORED in favour of their "
                    "trusted side (ignored, not refused -- the siblings "
                    "answer a hinted call identically to an un-hinted one), "
                    "and an assigned official is REFUSED 403 rather than "
                    "handed players: [] with zero counts. "
                    "get_availability_summary is keyed by (game_id, "
                    "team_id) -- no P/S/L concept, not applicable.")),
    RouteSpec("GET", r"^/api/games/[^/]+/board$", "/api/games/{}/board",
              "get_games_id_board", "_dispatch_get",
              auth="session+own-side-projection", scope_axis="none",
              note=("#202: _resolve_role then can_read_private_game_data "
                    "(scope.py) -- operators any, coach/player only their "
                    "own team via the game, official only if assigned. "
                    "#427 blocker: that gate proves the caller belongs to "
                    "*a* team in this game but does NOT bound WHICH side "
                    "they may read, and get_board used to hard-code "
                    "game.home_team_id for everybody -- an AWAY Coach got "
                    "HOME's private pool. The server now resolves the "
                    "caller's own game-scoped team "
                    "(game_scoped_own_team_id, hoisted to serve this whole "
                    "sub-family) and passes that TRUSTED side plus the "
                    "session role into the read; a Coach/Player is "
                    "answered for their own side only, an assigned official "
                    "gets the submitted-lineup projection "
                    "(services/lineup_visibility.py). #427 round 2 blocker "
                    "1: admission and projection are now ONE decision "
                    "(services/game_side_scope.resolve_private_game_read) "
                    "taken against a single fetch, carried into this leaf; "
                    "can_read_private_game_data is a fast-denial preflight "
                    "only. The HOME default survives for an unscoped "
                    "operator/official/in-process caller and NOT for a "
                    "Coach/Player, who are restricted instead -- losing an "
                    "authority mid-request is a refusal, not a fallback. "
                    "Still game-keyed -- no P/S/L concept, not "
                    "applicable.")),
    RouteSpec("GET", r"^/api/games/[^/]+/lineups$", "/api/games/{}/lineups",
              "get_games_id_lineups", "_dispatch_get",
              auth="session+own-side-projection", scope_axis="none",
              note=("#202: same _resolve_role + can_read_private_game_data "
                    "gate as get_games_id_board (scope.py). #427 blocker: "
                    "the same trusted side is passed here too, and each "
                    "side is PROJECTED per role -- an unscoped operator "
                    "reads both sides in full (unchanged), a Coach/Player "
                    "reads their own side in full with the opponent marked "
                    "restricted (public team metadata kept, private "
                    "status/players null -- never [], which both screens "
                    "already render as 'no lineup submitted'), an assigned "
                    "official reads both sides' submitted lineup but "
                    "neither side's unselected candidates, availability or "
                    "substitute state (services/lineup_visibility.py). "
                    "#427 round 2 blocker 3: 'submitted' means the rows "
                    "that OCCUPY a slot (RosterEntryStatus.occupies_slot), "
                    "not the display group -- a seated row whose occupant "
                    "went unavailable/removed stays in the `selected` group "
                    "flagged backed_out and is that side's roster HISTORY, "
                    "which an official does not receive. Same rule on "
                    "/board and /roster, one helper. Still game-keyed -- no "
                    "P/S/L concept, not applicable.")),
    RouteSpec("GET", r"^/api/games/[^/]+/officials$",
              "/api/games/{}/officials", "get_games_id_officials",
              "_dispatch_get",
              auth="session", scope_axis="none",
              note=("#202: same _resolve_role + can_read_private_game_data "
                    "gate as get_games_id_board (server.py:2070-2078, "
                    "2087-2088; scope.py:122-152). "
                    "get_officials_for_game is game-keyed -- no P/S/L "
                    "concept, not applicable.")),
    RouteSpec("GET", r"^/api/games/[^/]+/reschedule$",
              "/api/games/{}/reschedule", "get_games_id_reschedule",
              "_dispatch_get",
              auth="session", scope_axis="none",
              note=("#202: same _resolve_role + can_read_private_game_data "
                    "gate as get_games_id_board (server.py:2070-2078, "
                    "2095-2101; scope.py:122-152). "
                    "list_reschedule_requests(gid) is game-keyed -- no "
                    "P/S/L concept, not applicable.")),
    RouteSpec("GET", r"^/api/games/[^/]+/roster$", "/api/games/{}/roster",
              "get_games_id_roster", "_dispatch_get",
              auth="session+own-side-projection", scope_axis="none",
              note=("#202: same _resolve_role + can_read_private_game_data "
                    "gate as get_games_id_board (server.py:2070-2078, "
                    "2091-2092; scope.py:122-152). "
                    "#427 final blocker: that gate proves membership of *a* "
                    "team and carries no team-level narrowing, so this leaf "
                    "returned BOTH sides' seated rows -- measured, an AWAY "
                    "Coach reading HOME's seat, side and seated position. "
                    "The response is now projected on the SAME "
                    "server-resolved own side /board and /lineups use "
                    "(lineup_visibility.route_audience): a Coach/Player gets "
                    "rows this game DURABLY attributes to their own side, an "
                    "assigned official the two-side submitted-lineup "
                    "projection, an unscoped operator the unchanged full "
                    "read. get_roster is still game-keyed -- no P/S/L "
                    "concept, not applicable.")),
    RouteSpec("GET", r"^/api/games/[^/]+/roster-status$",
              "/api/games/{}/roster-status", "get_games_id_roster_status",
              "_dispatch_get",
              auth="session+own-side-projection", scope_axis="none",
              note=("#202: same _resolve_role + can_read_private_game_data "
                    "gate as get_games_id_board (server.py:2070-2078, "
                    "2089-2090; scope.py:122-152). "
                    "#427 final blocker: this leaf called "
                    "compute_roster_status(game_id) with NO team and so "
                    "hard-coded HOME for every caller -- the same defect "
                    "get_board carried, measured returning team_id=HOME and "
                    "substitutes_enrolled to an AWAY Coach. It now answers "
                    "for the server-resolved own side only; an assigned "
                    "official is REFUSED (403) because no frontend file "
                    "fetches this route -- the Game Sheet reads /lineups, "
                    "whose official projection already carries the slot "
                    "counts with substitute state removed. An unscoped "
                    "operator keeps the unchanged home-side default. "
                    "get_roster_status is still game-keyed -- no P/S/L "
                    "concept, not applicable.")),
    RouteSpec("GET", r"^/api/games/[^/]+/substitute-addable$",
              "/api/games/{}/substitute-addable",
              "get_games_id_substitute_addable", "_dispatch_get",
              auth="session+MANAGE_ROSTER+own-side-projection",
              scope_axis="none",
              note=("#202: _resolve_role + can_read_private_game_data gate "
                    "the whole games/{id}/... family first (server.py:"
                    "2851-2864; scope.py:230-259), then this leaf's own "
                    "can(role, Permission.MANAGE_ROSTER) capability gate (a "
                    "player and an assigned official must not see this even "
                    "for a game they are in) -- both UNCHANGED. "
                    "#427 final blocker round 3: what changed is the SIDE. "
                    "This leaf bound it twice -- a local `own_team = "
                    "scope.get('team_id')` beside the family's one trusted "
                    "resolution, and an inline coach check that answered a "
                    "HINTED call DIFFERENTLY from an un-hinted one (403 for "
                    "the opponent's id), contradicting the contract round 2 "
                    "shipped for this very family ('a ?team_id= naming the "
                    "opponent is ignored ... a hinted request returns "
                    "exactly what the un-hinted one returns'). It is now "
                    "projected by the SAME lineup_visibility.route_audience "
                    "as its six siblings, on the SAME trusted "
                    "server-resolved own side: hint kept for an unscoped "
                    "operator, IGNORED for a Coach in favour of their "
                    "trusted side, refused for any other audience rather "
                    "than answered with addable: []. "
                    "get_addable_substitutes is (game_id, team_id)-keyed "
                    "-- no P/S/L concept, not applicable.")),
    RouteSpec("GET", r"^/api/games/[^/]+/substitute-candidates$",
              "/api/games/{}/substitute-candidates",
              "get_games_id_substitute_candidates", "_dispatch_get",
              auth="session+MANAGE_ROSTER+own-side-projection",
              scope_axis="none",
              note=("#202: _resolve_role + can_read_private_game_data gate "
                    "the whole games/{id}/... family first (server.py:"
                    "2851-2864; scope.py:230-259), then this leaf's own "
                    "can(role, Permission.MANAGE_ROSTER) capability gate -- "
                    "both UNCHANGED. "
                    "#427 final blocker round 3: the SIDE moves to the "
                    "facade, exactly as for substitute-addable above and "
                    "for the same reasons -- one trusted resolution instead "
                    "of a second local `scope.get('team_id')`, and a client "
                    "hint that is IGNORED for a Coach rather than answered "
                    "with a 403 that varies with the side named. The ROWS "
                    "were already fixed in round 2: this queue and "
                    "/substitutes now name the SAME set because both key on "
                    "SubstituteEnrollment.team_id, so a pre-060 NULL-owner "
                    "row appears in NEITHER (it used to be served here, by "
                    "LIVE membership, with can_offer: true, to whichever "
                    "Coach its occupant belongs to today). "
                    "get_substitute_candidates is (game_id, team_id)-keyed "
                    "-- no P/S/L concept, not applicable.")),
    RouteSpec("GET", r"^/api/games/[^/]+/substitutes$",
              "/api/games/{}/substitutes", "get_games_id_substitutes",
              "_dispatch_get",
              auth="session+own-side-projection", scope_axis="none",
              note=("#202: same _resolve_role + can_read_private_game_data "
                    "gate as get_games_id_board (server.py:2070-2078, "
                    "2093-2094; scope.py:122-152). "
                    "#427 final blocker: that gate carries no team-level "
                    "narrowing, so this leaf returned BOTH sides' substitute "
                    "workflow -- measured, an AWAY Coach AND an assigned "
                    "official reading a HOME enrollment's player, status and "
                    "owning team. A Coach/Player now gets only rows DURABLY "
                    "OWNED by their own side (enrollment.team_id; a legacy "
                    "NULL owner is omitted from both, never guessed), and an "
                    "assigned official is REFUSED (403) -- the enrollment IS "
                    "the substitute workflow, so there is no official-shaped "
                    "projection of it, and [] would falsely assert 'no "
                    "substitutes are enrolled'. An unscoped operator keeps "
                    "the unchanged full read. get_substitutes is still "
                    "game-keyed -- no P/S/L concept, not applicable.")),
    RouteSpec("GET", r"^/api/guardians/links$", "/api/guardians/links",
              "get_guardians_links", "_dispatch_get",
              auth="operator_only", scope_axis="none",
              note=("#202: _operator_only('/api/accounts') (server.py:"
                    "1732). list_guardian_links (service.py:5729-5730) is "
                    "self.guardians.all_links() -- installation-wide, no "
                    "P/S/L filter.")),
    RouteSpec("GET", r"^/api/health$", "/api/health", "get_health",
              "_dispatch_get",
              auth="none", scope_axis="none",
              note=("#202: no guard (server.py:1671-1679) -- public "
                    "liveness/dependency snapshot (#90).")),
    RouteSpec("GET", r"^/api/import/hierarchy-codes$",
              "/api/import/hierarchy-codes", "get_import_hierarchy_codes",
              "_dispatch_get",
              auth="operator_only", scope_axis="none",
              note=("#202: _operator_only('/api/import/hierarchy-codes') "
                    "(server.py:1445). get_hierarchy_import_codes "
                    "(hierarchy_import_service.py:29-44ish) reads "
                    "all_programs/all_leagues/all_venues with "
                    "external_ref set -- installation-wide, no P/S/L "
                    "filter.")),
    RouteSpec("GET", r"^/api/me/assignments$", "/api/me/assignments",
              "get_me_assignments", "_dispatch_get",
              auth="optional_session", scope_axis="none",
              note=("#202 repair round 4, finding 5: relabelled from "
                    "'session' -- same investigation and same reasoning as "
                    "get_auth_me's own entry above (the SAME three-cookie-"
                    "state contract, not a distinct pattern). Direct "
                    "cookie lookup (server.py:1827-1843, "
                    "self._cookie(SESSION_COOKIE) + SESSIONS.resolve) -- "
                    "NOT _resolve_role: no cookie -> empty inbox shape "
                    "(200, {'official_id': None, 'assignments': []}), "
                    "invalid/expired cookie -> 401, a valid session with "
                    "no official binding -> the SAME empty shape (verified "
                    "as its own real-HTTP case, not merely assumed "
                    "identical). get_official_inbox(oid) is keyed by the "
                    "caller's own bound official_id -- no P/S/L concept, "
                    "not applicable. Verified over real HTTP, all three "
                    "cookie states plus the bound-vs-unbound valid-session "
                    "distinction, in test_server_authz.py's "
                    "OptionalSessionRouteTests.")),
    RouteSpec("GET", r"^/api/me/guardian/home$", "/api/me/guardian/home",
              "get_me_guardian_home", "_dispatch_get",
              auth="session+guardian-scope", scope_axis="none",
              note=("#202 repair round 4, finding 4: relabelled from bare "
                    "'session' -- the gate this route actually runs "
                    "(_require_guardian_scope) is the SAME one its own "
                    "POST siblings (post_me_guardian_...) are already "
                    "labelled 'session+guardian-scope[+verified-link]' "
                    "for; leaving this GET sibling at the coarser 'session' "
                    "was an inconsistency with this entry's own cited "
                    "evidence, not a deliberate distinction. "
                    "_require_guardian_scope (server.py:1177-1203, called "
                    "at 1881) -- no/expired cookie -> 401, "
                    "non-guardian session -> 403. NOT '+verified-link': "
                    "unlike the substitute-opportunity-detail sibling below, "
                    "this route returns EVERY linked junior's home data "
                    "(service.py's own get_guardian_home enumerates the "
                    "guardian's links itself) -- there is no single named "
                    "junior for _guardian_link_or_403 to check a link "
                    "against. get_guardian_home(guid) is keyed by the "
                    "caller's own guardian user id -- no P/S/L concept, not "
                    "applicable.")),
    RouteSpec("GET",
              r"^/api/me/guardian/[^/]+/substitute-opportunities/[^/]+$",
              "/api/me/guardian/{}/substitute-opportunities/{}",
              "get_me_guardian_id_substitute_opportunities_id",
              "_dispatch_get",
              auth="session+guardian-scope+verified-link", scope_axis="none",
              note=("#202 repair round 4, finding 4: relabelled from bare "
                    "'session' to match this entry's own cited evidence "
                    "and its POST siblings' identical-shape gate (same "
                    "inconsistency as get_me_guardian_home immediately "
                    "above). _require_guardian_scope (server.py:1891) then "
                    "_guardian_link_or_403 (server.py:1205-1215, called "
                    "at 1895) -- the guardian must hold a VERIFIED link "
                    "to the named junior. get_substitute_opportunity(jid, "
                    "game_id) (service.py:5733+) is keyed by player+game "
                    "id -- no P/S/L concept, not applicable.")),
    RouteSpec("GET", r"^/api/me/player-home$", "/api/me/player-home",
              "get_me_player_home", "_dispatch_get",
              auth="optional_session", scope_axis="none",
              note=("#202 repair round 4, finding 5: relabelled from "
                    "'session' -- same investigation and reasoning as "
                    "get_auth_me/get_me_assignments above. Direct cookie "
                    "lookup (server.py:1844-1863) -- NOT _resolve_role: no "
                    "cookie -> empty shape (200), invalid/expired cookie "
                    "-> 401. get_player_home(pid, ...) is keyed by the "
                    "caller's own bound player_id -- no P/S/L concept, not "
                    "applicable. Verified over real HTTP, all three cookie "
                    "states, in test_server_authz.py's "
                    "OptionalSessionRouteTests.")),
    RouteSpec("GET", r"^/api/me/substitute-opportunities/[^/]+$",
              "/api/me/substitute-opportunities/{}",
              "get_me_substitute_opportunities_id", "_dispatch_get",
              auth="session+player-scope", scope_axis="none",
              note=("#202 repair round 4, finding 4: relabelled from bare "
                    "'session' -- the gate this route actually runs "
                    "(_require_player_scope) is the SAME one its own POST "
                    "siblings (post_me_substitute_opportunities_id_...) "
                    "are already labelled 'session+player-scope' for; "
                    "leaving this GET sibling at the coarser 'session' was "
                    "an inconsistency with this entry's own cited "
                    "evidence, not a deliberate distinction. "
                    "_require_player_scope (server.py:1152-1175, "
                    "called at 1871) -- no/expired cookie -> 401, session "
                    "without a player binding -> 403. "
                    "get_substitute_opportunity(pid, game_id) is keyed by "
                    "player+game id -- no P/S/L concept, not "
                    "applicable.")),
    RouteSpec("GET", r"^/api/notifications$", "/api/notifications",
              "get_notifications", "_dispatch_get",
              auth="session",
              scope_axis="none",
              note=("#202: _resolve_role (server.py:1785), no further `user_id is None` check -- accepts the identity-less X-Demo-Role/headerless demo fallback in non-production (production's _resolve_role has no such fallback, server.py 817-820). Visibility is audience-based (role + team_id/player_id/official_id, service.py _notif_visible 4177-4195), not a Program/Season/League concept -- scope_axis not applicable.")),
    RouteSpec("GET", r"^/api/notifications/contacts$",
              "/api/notifications/contacts", "get_notifications_contacts",
              "_dispatch_get",
              auth="operator_only",
              scope_axis="none",
              note=("#202: _operator_only('/api/notifications/contacts') (server.py:1800). list_contact_destinations (service.py:5058) is an installation-wide registry, no P/S/L filter.")),
    RouteSpec("GET", r"^/api/notifications/deliveries$",
              "/api/notifications/deliveries", "get_notifications_deliveries",
              "_dispatch_get",
              auth="operator_only",
              scope_axis="none",
              note=("#202: _operator_only(gated on the '.../deliveries/process' POST permission) (server.py:1795). get_delivery_overview (service.py:4351) is an installation-wide queue view, no P/S/L filter.")),
    RouteSpec("GET", r"^/api/notifications/device-tokens$",
              "/api/notifications/device-tokens",
              "get_notifications_device_tokens", "_dispatch_get",
              auth="operator_only",
              scope_axis="none",
              note=("#202: _operator_only('/api/notifications/device-tokens') (server.py:1824). list_device_tokens (service.py:5368) is an installation-wide registry, no P/S/L filter.")),
    RouteSpec("GET", r"^/api/notifications/preferences$",
              "/api/notifications/preferences",
              "get_notifications_preferences", "_dispatch_get",
              auth="session+MANAGE_SCHEDULE-or-self",
              scope_axis="none",
              note=("#202: _prefs_guard (server.py:1242-1259) -- operator (can(role, Permission.MANAGE_SCHEDULE)) manages any recipient_ref, else only the caller's own. Preferences are keyed by recipient_ref, not by Program/Season/League -- not applicable.")),
    RouteSpec("GET", r"^/api/officials$", "/api/officials", "get_officials",
              "_dispatch_get",
              auth="none",
              scope_axis="none",
              note=("#202: no guard at all in the dispatch branch (server.py 1735-1736, `if path == '/api/officials': return self._send_api({'officials': api.get_officials()})`) -- unauthenticated. get_officials (service.py:4168-4169) is `self.store.all_officials()`, installation-wide, no P/S/L filter applied anywhere in the call.")),
    RouteSpec("GET", r"^/api/officials/[^/]+/availability$",
              "/api/officials/{}/availability",
              "get_officials_id_availability", "_dispatch_get",
              auth="session+MANAGE_SCHEDULE-or-self",
              scope_axis="none",
              note=("#202: _official_guard (server.py:723-739) -- operator (can(role, Permission.MANAGE_SCHEDULE)) manages any official, else only scope['official_id'] == the requested id. list_official_availability (service.py:5860-5863) is keyed by official_id, no P/S/L concept.")),
    RouteSpec("GET", r"^/api/onboarding/status$", "/api/onboarding/status",
              "get_onboarding_status", "_dispatch_get",
              auth="operator_only", scope_axis="none",
              note=("#202: _operator_only('/api/onboarding/status') "
                    "(server.py:1476). get_onboarding_status (service.py:"
                    "4462-4489ish) reads installation-wide records "
                    "(orgs/leagues/venues/rinks/seasons/...) -- no P/S/L "
                    "filter.")),
    RouteSpec("GET", r"^/api/players$", "/api/players", "get_players",
              "_dispatch_get",
              auth="operator_only",
              scope_axis="cross",
              note=("#202: _operator_only('/api/setup/player') (server.py:1743, MANAGE_SETUP). scope_axis: list_players (service.py:11146-11198) calls self.context.resolve_with_league(user_id, role, scope) (service.py:11172) and filters BOTH by the resolved Program (t.program_id == program.id) AND, when a League is selected, by the Team's League (t.league_id == league.id) -- validates every axis it touches (Program + League; no Season concept here since Players aren't Season-owned).")),
    RouteSpec("GET", r"^/api/public/games/[^/]+$", "/api/public/games/{}",
              "get_public_games_id", "_dispatch_get",
              auth="none",
              scope_axis="none",
              note=('#202: rate-limited only, no _resolve_role (server.py 1983-1985). get_public_game (service.py:6341-6346) is a direct id lookup filtered only by g.published, no P/S/L concept.')),
    RouteSpec("GET", r"^/api/public/schedule$", "/api/public/schedule",
              "get_public_schedule", "_dispatch_get",
              auth="none",
              scope_axis="none",
              note=('#202: rate-limited only, no _resolve_role (server.py:1965-1968). get_public_schedule (service.py:6318-6332) reads self.store.all_programs()/all_divisions()/all_games() installation-wide -- no P/S/L filter.')),
    RouteSpec("GET", r"^/api/public/standings/league-season/[^/]+/[^/]+$",
              "/api/public/standings/league-season/{}/{}",
              "get_public_standings_league_season_id_id", "_dispatch_get",
              auth="none", scope_axis="none",
              note=("#202: rate-limited only, no _resolve_role "
                    "(server.py:1974-1980). "
                    "get_public_league_season_standings (service.py:"
                    "6193-6...) filters only by g.published -- no P/S/L "
                    "concept (public-safe fields).")),
    RouteSpec("GET", r"^/api/public/standings/[^/]+$",
              "/api/public/standings/{}", "get_public_standings_id",
              "_dispatch_get",
              auth="none", scope_axis="none",
              note=("#202: rate-limited only, no _resolve_role "
                    "(server.py:1969-1973). get_public_standings "
                    "(service.py:6335+) calls _standings_for_division"
                    "(division_id, public_only=True) -- filtered only by "
                    "g.published, no P/S/L concept.")),
    RouteSpec("GET", r"^/api/readiness$", "/api/readiness", "get_readiness",
              "_dispatch_get",
              auth="none", scope_axis="none",
              note=("#202: no guard (server.py:1680-1691) -- public "
                    "deployment readiness snapshot (#90).")),
    RouteSpec("GET", r"^/api/reschedule/pending$", "/api/reschedule/pending",
              "get_reschedule_pending", "_dispatch_get",
              auth="operator_only", scope_axis="none",
              note=("#202: _operator_only('/api/reschedule/pending') "
                    "(server.py:1769). list_reschedule_requests() "
                    "(service.py:4048-4049) filtered only by "
                    "status=='pending_league_approval' -- "
                    "installation-wide, no P/S/L filter.")),
    RouteSpec("GET", r"^/api/scheduler/drafts$", "/api/scheduler/drafts",
              "get_scheduler_drafts", "_dispatch_get",
              auth="operator_only", scope_axis="cross",
              note=("#202: _operator_only('/api/scheduler/commit') "
                    "(server.py:1996) then _resolve_role (server.py:"
                    "1998). list_draft_games (service.py:8117-8133) -> "
                    "_games_in_active_tuple (service.py:8095-8114) "
                    "filters games against the caller's resolved "
                    "Program+Season+League tuple, failing closed to "
                    "empty when no Program resolves -- validates every "
                    "axis, exactly #409's 'cross' class.")),
    RouteSpec("GET", r"^/api/scheduler/scenarios$",
              "/api/scheduler/scenarios", "get_scheduler_scenarios",
              "_dispatch_get",
              auth="operator_only", scope_axis="cross",
              note=("#202: _operator_only('/api/scheduler/commit') "
                    "(server.py:2012), user_id is None -> 401 (server.py:"
                    "2018-2021). list_schedule_scenarios (service.py:"
                    "6887+) filters via _scenario_in_active_tuple "
                    "(service.py:6599-6614), which compares each "
                    "scenario's (program_id, season_id, league_id) edge "
                    "against the caller's active tuple -- validates every "
                    "axis, #409's 'cross' class.")),
    RouteSpec("GET", r"^/api/scheduler/scenarios/[^/]+$",
              "/api/scheduler/scenarios/{}", "get_scheduler_scenarios_id",
              "_dispatch_get",
              auth="operator_only", scope_axis="cross",
              note=("#202: _operator_only('/api/scheduler/commit') "
                    "(server.py:2026), user_id is None -> 401 (server.py:"
                    "2032-2035). get_schedule_scenario (service.py:"
                    "6871-6886) uses the SAME _scenario_in_active_tuple "
                    "predicate (service.py:6599-6614) as the list route "
                    "-- cross.")),
    RouteSpec("GET", r"^/api/setup/hierarchy$", "/api/setup/hierarchy",
              "get_setup_hierarchy", "_dispatch_get",
              auth="operator_only", scope_axis="none",
              note=("#202: _operator_only('/api/setup/player') "
                    "(server.py:1438). get_setup_hierarchy (service.py:"
                    "10031-10050ish) reads all_organizations/all_venues/"
                    ".../all_teams -- installation-wide, no P/S/L filter "
                    "(the v1 unceilinged tree).")),
    RouteSpec("GET", r"^/api/setup/leagues/[^/]+/teams$",
              "/api/setup/leagues/{}/teams", "get_setup_leagues_id_teams",
              "_dispatch_get",
              auth="operator_only", scope_axis="program",
              note=("#202: _operator_only('/api/setup/player') "
                    "(server.py:1453). v1 'league' IS today's Program "
                    "(_V1_SETUP_KIND, same convention as "
                    "post_setup_league_id_delete). list_program_teams"
                    "(program_id) (service.py:10828-10837) filters "
                    "strictly by the named program_id -- one axis, no "
                    "cross-check against the caller's active tuple.")),
    RouteSpec("GET", r"^/api/setup/scheduling-policy$",
              "/api/setup/scheduling-policy", "get_setup_scheduling_policy",
              "_dispatch_get",
              auth="operator_only", scope_axis="none",
              note=("#202: _operator_only('/api/setup/scheduling-policy') "
                    "(server.py:1487). get_scheduling_policy(scope_type, "
                    "scope_id, season_id) (service.py:11113-11128) "
                    "returns whatever (scope_type, scope_id) the caller "
                    "supplies verbatim -- no comparison against the "
                    "caller's active Program/Season/League tuple at all, "
                    "not applicable.")),
    RouteSpec("GET", r"^/api/setup/seasons/[^/]+/team-registrations$",
              "/api/setup/seasons/{}/team-registrations",
              "get_setup_seasons_id_team_registrations", "_dispatch_get",
              auth="operator_only", scope_axis="season",
              note=("#202: _operator_only('/api/setup/player') "
                    "(server.py:1460). list_season_team_registrations"
                    "(season_id) (service.py:10822-10826) filters "
                    "strictly by the named season_id -- one axis, no "
                    "cross-check against the caller's active tuple.")),
    RouteSpec("GET", r"^/api/standings/league-season/[^/]+/[^/]+$",
              "/api/standings/league-season/{}/{}",
              "get_standings_league_season_id_id", "_dispatch_get",
              auth="session", scope_axis="cross",
              note=("#202: _resolve_role (server.py:1945), user_id is "
                    "None -> 401 (server.py:1949-1952). "
                    "get_league_season_standings (service.py:6149-6191) "
                    "compares the named LeagueSeason against the caller's "
                    "ACTIVE resolved (Program, Season, League) tuple via "
                    "_league_season_matches_active_context -- validates "
                    "every axis, #409's 'cross' class.")),
    RouteSpec("GET", r"^/api/standings/[^/]+$", "/api/standings/{}",
              "get_standings_id", "_dispatch_get",
              auth="session", scope_axis="cross",
              note=("#202: _resolve_role (server.py:1904), user_id is "
                    "None -> 401 (server.py:1908-1911). get_standings "
                    "(service.py:6012-6048) compares the named Division's "
                    "LeagueSeason->Season->Program chain against the "
                    "caller's ACTIVE resolved tuple via "
                    "_division_matches_active_context -- validates every "
                    "axis, #409's 'cross' class.")),
    RouteSpec("GET", r"^/api/status$", "/api/status", "get_status",
              "_dispatch_get",
              auth="none",
              scope_axis="none",
              note=("#202: server.py:1650-1670 -- no _resolve_role call anywhere in the branch; the branch's own comment: 'Non-sensitive, no auth -- like a health endpoint.'")),
    RouteSpec("GET", r"^/api/v2/onboarding/status$",
              "/api/v2/onboarding/status", "get_v2_onboarding_status",
              "_dispatch_get",
              auth="operator_only", scope_axis="none",
              note=(
                    "#202: _operator_only('/api/v2/onboarding/status') "
                    "(server.py:1639-1642) -> required_permission (authz.py:262-263) "
                    "-> MANAGE_SETUP. get_onboarding_status_v2(_app_mode()) "
                    "(service.py:4711) takes no user_id/role/scope -- an "
                    "installation-wide aggregate (admin count, migration status), no "
                    "P/S/L filter."
              )),
    RouteSpec("GET", r"^/api/v2/setup/hierarchy$", "/api/v2/setup/hierarchy",
              "get_v2_setup_hierarchy", "_dispatch_get",
              auth="operator_only", scope_axis="program",
              note=(
                    "#202: _operator_only('/api/setup/player') (server.py:1557-1559, "
                    "'player' in _LEAGUE_SETUP) -> MANAGE_SETUP, then role is re- "
                    "resolved and only 'role is None' is rejected "
                    "(server.py:1569-1576) -- user_id may still be None via the "
                    "identity-less X-Demo-Role fallback, same posture as "
                    "operator_only elsewhere. get_setup_hierarchy_v2(user_id, role, "
                    "scope) (service.py:10209) ceilings the tree to the caller's "
                    "ACTIVE Program when role is not None (service.py:10212-10217)."
              )),
    RouteSpec("GET", r"^/api/v2/setup/overview$", "/api/v2/setup/overview",
              "get_v2_setup_overview", "_dispatch_get",
              auth="session+MANAGE_ARENA", scope_axis="cross",
              note=(
                    "#202: hand-rolled gate, NOT _operator_only (server.py:1538-1554) "
                    "-- resolves role/scope/user_id then REQUIRES a real session "
                    "(user_id is None -> 401, server.py:1542-1545, rejecting the "
                    "identity-less X-Demo-Role/headerless fallback _operator_only "
                    "would accept), then authorize(role, '/api/v2/setup/overview') -> "
                    "required_permission ('overview' rest, authz.py:72-73) -> "
                    "MANAGE_ARENA. get_setup_overview_v2 (service.py:9490) ceilings "
                    "BOTH the active Program (collapses 'programs' to "
                    "[active_program]) AND the active Season ('seasons' to the active "
                    "Season alone) when role is supplied (service.py:9503-9520) -- "
                    "leagues/organizations deliberately stay cross-Program "
                    "(service.py just below) -- validates more than one axis, so "
                    "'cross'."
              )),
    RouteSpec("GET", r"^/api/v2/setup/programs/[^/]+/teams$",
              "/api/v2/setup/programs/{}/teams",
              "get_v2_setup_programs_id_teams", "_dispatch_get",
              auth="operator_only", scope_axis="program",
              note=(
                    "#202: _operator_only('/api/v2/setup/player') "
                    "(server.py:1605-1608) -> _v2_setup_permission('player') "
                    "(authz.py:88-89) -> MANAGE_SETUP. "
                    "list_program_teams_v2(program_id) (service.py:10840-10844) reads "
                    "self.store.teams_for_program(program_id) -- the path's "
                    "program_id names the one Program directly; the id is trusted as- "
                    "is (no cross-check against the caller's resolved active Program, "
                    "unlike the venue-access/venue-candidates siblings below)."
              )),
    RouteSpec("GET", r"^/api/v2/setup/progress$", "/api/v2/setup/progress",
              "get_v2_setup_progress", "_dispatch_get",
              auth="session+MANAGE_ARENA", scope_axis="cross",
              note=(
                    "#202: hand-rolled gate (server.py:1587-1603) -- resolves "
                    "role/scope/user_id, REQUIRES a real session (user_id is None -> "
                    "401, server.py:1591-1594, same real-session requirement as "
                    "overview above), authorize(role, '/api/v2/setup/progress') -> "
                    "required_permission (authz.py:270-271, an explicit override "
                    "before the generic '/api/v2/setup/' prefix fallback) -> "
                    "MANAGE_ARENA. get_setup_progress(user_id, role, scope) "
                    "(service.py:2998) resolves BOTH the active Program AND the "
                    "active Season from the same active-context selection "
                    "(service.py:3013-3016) -- validates more than one axis, so "
                    "'cross'."
              )),
    RouteSpec("GET", r"^/api/v2/setup/seasons/[^/]+/team-registrations$",
              "/api/v2/setup/seasons/{}/team-registrations",
              "get_v2_setup_seasons_id_team_registrations", "_dispatch_get",
              auth="operator_only", scope_axis="season",
              note=(
                    "#202: _operator_only('/api/v2/setup/player') "
                    "(server.py:1610-1613) -> MANAGE_SETUP. "
                    "list_season_team_registrations(season_id) "
                    "(service.py:10823-10826) reads "
                    "self.store.registrations_for_season(season_id) directly from the "
                    "path id -- NO cross-check against the caller's resolved active "
                    "Season (unlike the venue-access/venue-candidates siblings just "
                    "below, which pin season_id to the resolved active Season, "
                    "service.py:10770-10775 / 9410-9420); Season-keyed data, so "
                    "'season', but the ceiling itself is unvalidated here."
              )),
    RouteSpec("GET", r"^/api/v2/setup/seasons/[^/]+/venue-access$",
              "/api/v2/setup/seasons/{}/venue-access",
              "get_v2_setup_seasons_id_venue_access", "_dispatch_get",
              auth="operator_only", scope_axis="season",
              note=(
                    "#202: _operator_only('/api/setup/player') (server.py:1627-1628) "
                    "-> MANAGE_SETUP, then role/user_id are resolved again for the "
                    "service call (server.py:1629-1637, no user_id-None check here, "
                    "so the identity-less demo fallback is still accepted). "
                    "list_season_venue_access(season_id, user_id, role, scope) "
                    "(service.py:10733-10784) refuses unless season_id equals the "
                    "caller's resolved active Season (service.py:10770-10775, a "
                    "generic NotFoundError otherwise) -- pinned to the exact active "
                    "Season."
              )),
    RouteSpec("GET", r"^/api/v2/setup/seasons/[^/]+/venue-candidates$",
              "/api/v2/setup/seasons/{}/venue-candidates",
              "get_v2_setup_seasons_id_venue_candidates", "_dispatch_get",
              auth="operator_only", scope_axis="season",
              note=(
                    "#202: _operator_only(guard) (server.py:1510-1511) -> "
                    "MANAGE_SETUP, role/user_id resolved again (server.py:1512-1515, "
                    "no user_id-None check, demo fallback accepted). "
                    "get_venue_grant_candidates(season_id, user_id, role, scope) "
                    "(service.py:9361-9420) refuses unless season_id equals the "
                    "caller's resolved active Season, identically to venue_access "
                    "above; the candidate VENUE set itself is deliberately cross- "
                    "Program (service.py:9398-9402 -- that is what facility sharing "
                    "means) but the DESTINATION Season is pinned -- the ceiling is on "
                    "the Season axis."
              )),
    RouteSpec("GET", r"^/api/.*$", "/api/{*0}", "get_api_unmatched",
              "_dispatch_get", kind="fallthrough",
              auth="none", scope_axis="none",
              note=("no resource: the tail that answers 405 (known "
                    "path, wrong method) or 404 for anything under "
                    "/api/. Template uses {*0} (.* -- POSSIBLY EMPTY), "
                    "not {*} (.+): /api/ alone matches, distinctly from "
                    "the games-action family below, which is .+ (#202 "
                    "repair root cause 4). "
                    "#202: _dispatch_get's own tail (server.py:2165-2168) -> "
                    "_unmatched_route('GET') (server.py:2223-2236) -- no "
                    "_resolve_role call anywhere in either function; answers a bare "
                    "405+Allow (a known POST-only path) or a bare 404 JSON body, no "
                    "data of any kind, so no P/S/L concept applies."
              )),
    RouteSpec("GET", r"^/calendar/division/[^/]+\.ics$",
              "/calendar/division/{}.ics", "get_calendar_division_id_ics",
              "_dispatch_get",
              auth='none',
              scope_axis='none',
              note=('#202: _dispatch_get (server.py:1370-1383) -- bearer token in the URL (the id) IS the identity; no session/_resolve_role call. calendar_feed_ics matches the token or returns None -> 404; scope_axis not applicable (token-scoped, not a P/S/L concept).')),
    RouteSpec("GET", r"^/calendar/official/[^/]+\.ics$",
              "/calendar/official/{}.ics", "get_calendar_official_id_ics",
              "_dispatch_get",
              auth='none',
              scope_axis='none',
              note=('#202: same branch as get_calendar_division_id_ics (server.py:1370-1383) -- bearer token in the URL, no session.')),
    RouteSpec("GET", r"^/calendar/player/[^/]+\.ics$",
              "/calendar/player/{}.ics", "get_calendar_player_id_ics",
              "_dispatch_get",
              auth='none',
              scope_axis='none',
              note=('#202: same branch as get_calendar_division_id_ics (server.py:1370-1383) -- bearer token in the URL, no session.')),
    RouteSpec("GET", r"^/calendar/team/[^/]+\.ics$", "/calendar/team/{}.ics",
              "get_calendar_team_id_ics", "_dispatch_get",
              auth='none',
              scope_axis='none',
              note=('#202: same branch as get_calendar_division_id_ics (server.py:1370-1383) -- bearer token in the URL, no session.')),
    RouteSpec("GET", r"^/favicon\.ico$", "/favicon.ico", "get_favicon_ico",
              "_dispatch_get",
              auth="none", scope_axis="none",
              note=(
                    "#202: _dispatch_get (server.py:1365-1367) -- unconditional 204 "
                    "No Content for '/favicon.ico', no _resolve_role call, no body."
              )),
    RouteSpec("GET", r"^/mobile$", "/mobile", "get_mobile_shell",
              "_serve_static", kind="static",
              auth="none", scope_axis="none",
              note=("#202: _serve_static (server.py:1273-1309), path in "
                    "('/', '', '/mobile', '/mobile/') -> index.html, no "
                    "auth check anywhere in the branch.")),
    RouteSpec("GET", r"^/mobile/$", "/mobile/", "get_mobile_shell_slash",
              "_serve_static", kind="static",
              auth="none", scope_axis="none",
              note=("#202: _serve_static (server.py:1273-1309), same "
                    "branch as get_mobile_shell -- no auth check.")),
    RouteSpec("GET", r"^/setup$", "/setup", "get_setup_shell",
              "_serve_static", kind="static",
              auth="none", scope_axis="none",
              note=("#202: _serve_static (server.py:1273-1309), path in "
                    "('/setup', '/setup/') -> setup.html, no auth "
                    "check.")),
    RouteSpec("GET", r"^/setup/$", "/setup/", "get_setup_shell_slash",
              "_serve_static", kind="static",
              auth="none", scope_axis="none",
              note=("#202: _serve_static (server.py:1273-1309), same "
                    "branch as get_setup_shell -- no auth check.")),
    RouteSpec("GET", r"^/.+$", "/{*}", "get_static_tail", "_serve_static",
              kind="static",
              auth="none", scope_axis="none",
              note=("the UNCONDITIONAL static tail (#202 repair root "
                    "cause 6): any path not matched above -- and not "
                    "starting with /api/, which _dispatch_get claims "
                    "first -- reaches _serve_static's else branch "
                    "(rel = path.lstrip('/')) and serves whatever real "
                    "file under web/static/ that name resolves to, or "
                    "404s. This is what makes the current files under "
                    "web/static/ reachable; it was previously omitted "
                    "from the inventory entirely, not merely mis-typed. "
                    "#202: no auth check anywhere in _serve_static "
                    "(server.py:1273-1309).")),

    # -- POST --------------------------------------------------------------
    RouteSpec("POST", r"^/api/accounts$", "/api/accounts", "post_accounts",
              "do_POST",
              auth="session+MANAGE_USERS", scope_axis="none",
              note=(
                    "#202: generic do_POST gate (server.py:2646-2660) -- "
                    "authorize(role, path) -> required_permission (authz.py:213-214) "
                    "-> MANAGE_USERS; scope_violation (server.py:2667, scope.py:44+) "
                    "is a no-op for LEAGUE_ADMIN, the only role holding MANAGE_USERS. "
                    "create_user_account (service.py:5425-5430) writes an "
                    "installation login record -- its own 'scope' argument binds a "
                    "Coach/Player subject (team_id/player_id), not a "
                    "Program/Season/League axis."
              )),
    RouteSpec("POST", r"^/api/accounts/[^/]+/active$",
              "/api/accounts/{}/active", "post_accounts_id_active", "do_POST",
              auth="session+MANAGE_USERS", scope_axis="none",
              note=(
                    "#202: generic do_POST gate (server.py:2646-2660) -> "
                    "required_permission (authz.py:215-216) -> MANAGE_USERS. "
                    "set_user_account_active (service.py:5432-5435) flips an "
                    "installation account's active flag -- no P/S/L concept."
              )),
    RouteSpec("POST", r"^/api/accounts/[^/]+/scope$",
              "/api/accounts/{}/scope", "post_accounts_id_scope", "do_POST",
              auth="session+MANAGE_USERS", scope_axis="none",
              note=(
                    "#202: generic do_POST gate (server.py:2646-2660) -> "
                    "required_permission (authz.py:219-220) -> MANAGE_USERS. "
                    "rebind_user_account_scope (service.py:5438-5444) rebinds an "
                    "account's Coach/Player subject binding (team_id/player_id) -- "
                    "not a Program/Season/League axis."
              )),
    RouteSpec("POST", r"^/api/accounts/[^/]+/sessions/[^/]+/revoke$",
              "/api/accounts/{}/sessions/{}/revoke",
              "post_accounts_id_sessions_id_revoke", "do_POST",
              auth="session+MANAGE_USERS", scope_axis="none",
              note=(
                    "#202: generic do_POST gate (server.py:2646-2660) -> "
                    "required_permission (authz.py:225-226) -> MANAGE_USERS. "
                    "revoke_account_session (service.py:5498) ends one login session "
                    "-- no P/S/L concept."
              )),
    RouteSpec("POST", r"^/api/admin/factory-reset/execute$",
              "/api/admin/factory-reset/execute",
              "post_admin_factory_reset_execute", "do_POST",
              auth="session+MANAGE_USERS", scope_axis="none",
              note=(
                    "#202: generic do_POST gate (server.py:2646-2660) -> "
                    "required_permission (authz.py:240-242) -> MANAGE_USERS (the "
                    "HTTP-layer permission; FactoryResetService re-checks "
                    "MANAGE_SETUP AND MANAGE_USERS itself, server.py:2735-2739 "
                    "comment, defense in depth). factory_reset_execute "
                    "(service.py:5458) wipes the WHOLE installation -- a full-store "
                    "reset has no single Program/Season/League to ceiling against."
              )),
    RouteSpec("POST", r"^/api/admin/factory-reset/preview$",
              "/api/admin/factory-reset/preview",
              "post_admin_factory_reset_preview", "do_POST",
              auth="session+MANAGE_USERS", scope_axis="none",
              note=(
                    "#202: same gate as execute above -- generic do_POST gate "
                    "(server.py:2646-2660) -> required_permission (authz.py:240-242) "
                    "-> MANAGE_USERS. factory_reset_preview (service.py:5454-5456) "
                    "reports what a whole-installation wipe would remove -- no single "
                    "Program/Season/League to ceiling against."
              )),
    RouteSpec("POST", r"^/api/auth/login$", "/api/auth/login",
              "post_auth_login", "do_POST",
              auth="none", scope_axis="none",
              note=(
                    "#202: do_POST's own early special case (server.py:2322-2371), "
                    "BEFORE the generic gate -- credentials are the only identity "
                    "presented; no _resolve_role call. verify_login (service.py:5524) "
                    "checks a username/password pair against the installation's "
                    "account table, no P/S/L concept."
              )),
    RouteSpec("POST", r"^/api/auth/logout$", "/api/auth/logout",
              "post_auth_logout", "do_POST",
              auth="none", scope_axis="none",
              note=(
                    "#202: do_POST's own early special case (server.py:2372-2376), "
                    "BEFORE the generic gate -- ends whatever session cookie is "
                    "present (or no-ops if none), no _resolve_role call, no P/S/L "
                    "concept."
              )),
    RouteSpec("POST", r"^/api/bootstrap/claim$", "/api/bootstrap/claim",
              "post_bootstrap_claim", "do_POST",
              auth="none", scope_axis="none",
              note=(
                    "#202: do_POST's own early special case (server.py:2299-2321), "
                    "BEFORE the generic gate -- the one anonymous setup mutation "
                    "(#174), gated only by the one-time setup code and a rate limit, "
                    "no _resolve_role call. claim_installation mints the FIRST "
                    "installation-wide admin account -- no P/S/L concept yet exists."
              )),
    RouteSpec("POST", r"^/api/calendar-feeds$", "/api/calendar-feeds",
              "post_calendar_feeds", "do_POST",
              auth="session+MANAGE_SCHEDULE-or-self", scope_axis="none",
              note=(
                    "#202: _feed_guard(actor_type, actor_ref) (server.py:677-692, "
                    "2500-2508) -- operator (can(role, Permission.MANAGE_SCHEDULE)) "
                    "mints for any actor, else only the caller's own. "
                    "create_calendar_feed_token (service.py:5276) is keyed by "
                    "actor_type/actor_ref (team/division/official/player), not a "
                    "Program/Season/League concept -- not applicable."
              )),
    RouteSpec("POST", r"^/api/calendar-feeds/[^/]+/revoke$",
              "/api/calendar-feeds/{}/revoke",
              "post_calendar_feeds_id_revoke", "do_POST",
              auth="session+MANAGE_SCHEDULE-or-self", scope_axis="none",
              note=(
                    "#202: same _feed_guard as create above (server.py:677-692, "
                    "2509-2519), keyed off the token's own actor_type/actor_ref. "
                    "revoke_calendar_feed_token (service.py:5318) -- no "
                    "Program/Season/League concept."
              )),
    RouteSpec("POST", r"^/api/context$", "/api/context", "post_context",
              "do_POST",
              auth="session", scope_axis="none",
              note=(
                    "#202: server.py:2598-2629 -- resolves role/scope/user_id, "
                    "REQUIRES a real session (user_id is None -> 401, "
                    "server.py:2617-2620), no specific Permission required (any "
                    "signed-in role may set their own context). set_active_context "
                    "(service.py:288) IS the mechanism that defines the active "
                    "Program/Season/League selection, so the write itself is not "
                    "scoped BY one -- matches get_context's own auth='session', "
                    "scope_axis='none' above."
              )),
    RouteSpec("POST", r"^/api/demo/add-ice-slot$", "/api/demo/add-ice-slot",
              "post_demo_add_ice_slot", "do_POST",
              auth="session+MANAGE_ARENA", scope_axis="program",
              note=(
                    "#202: generic do_POST gate (server.py:2646-2660) -> "
                    "required_permission (authz.py:117-118) -> MANAGE_ARENA. "
                    "server.py:2800-2803's own comment: '#409: an ice-slot CREATE is "
                    "PROGRAM-AXIS whichever route mints it' -- "
                    "_guarded_create('ice_slot', [('rink', rink_id)], ...) "
                    "(server.py:2804-2806), same PROGRAM-AXIS target kind as "
                    "/api/setup/ice-slot above (service.py ice_slot in "
                    "_PROGRAM_AXIS_TARGET_KINDS)."
              )),
    RouteSpec("POST", r"^/api/demo/clear$", "/api/demo/clear",
              "post_demo_clear", "do_POST",
              auth="session+MANAGE_SETUP", scope_axis="none",
              note=(
                    "#202: generic do_POST gate (server.py:2646-2660) -> "
                    "required_permission (authz.py:114-116) -> MANAGE_SETUP. "
                    "STATE.reset(seed=False, ...) (server.py:2675-2702) wipes the "
                    "WHOLE demo store to a clean slate -- no single "
                    "Program/Season/League to ceiling against."
              )),
    RouteSpec("POST", r"^/api/demo/load$", "/api/demo/load", "post_demo_load",
              "do_POST",
              auth="session+MANAGE_SETUP", scope_axis="none",
              note=(
                    "#202: generic do_POST gate (server.py:2646-2660) -> "
                    "required_permission (authz.py:114-116) -> MANAGE_SETUP. "
                    "STATE.reset(seed=True, ...) (server.py:2675-2702) rebuilds the "
                    "WHOLE canonical sample dataset -- no single "
                    "Program/Season/League to ceiling against."
              )),
    RouteSpec("POST", r"^/api/demo/reset$", "/api/demo/reset",
              "post_demo_reset", "do_POST",
              auth="session+MANAGE_SETUP", scope_axis="none",
              note=(
                    "#202: generic do_POST gate (server.py:2646-2660) -> "
                    "required_permission (authz.py:114-116) -> MANAGE_SETUP. "
                    "STATE.reset(seed=True, ...) (server.py:2675-2702) wipes and "
                    "rebuilds the WHOLE demo store -- no single Program/Season/League "
                    "to ceiling against."
              )),
    RouteSpec("POST", r"^/api/games/[^/]+/availability$",
              "/api/games/{}/availability", "post_games_id_availability",
              "do_POST",
              auth="session+RESPOND_AVAILABILITY", scope_axis="none",
              note=(
                    "#202: generic do_POST gate (server.py:2646-2660) -> "
                    "required_permission -> the games-action table "
                    "(authz.py:302-310), 'availability' in _AVAILABILITY_ACTIONS -> "
                    "RESPOND_AVAILABILITY. scope_violation (scope.py:44+) then checks "
                    "player_id ownership (coach/own-team, player/self) -- resource "
                    "ownership, not a Program/Season/League axis; set_availability "
                    "(server.py:3353-3355) acts on one game by id directly, no "
                    "active-context read."
              )),
    RouteSpec("POST", r"^/api/games/[^/]+/availability/remind$",
              "/api/games/{}/availability/remind",
              "post_games_id_availability_remind", "do_POST",
              auth="session+MANAGE_ROSTER", scope_axis="none",
              note=(
                    "#202: generic do_POST gate (server.py:2646-2660) -> "
                    "required_permission -- 'availability/remind' does not exactly "
                    "match 'availability' in _AVAILABILITY_ACTIONS, nor any other "
                    "named set (authz.py:302-332), so it falls to the unknown-action "
                    "default -> MANAGE_ROSTER (authz.py:332). remind_unresponded "
                    "(server.py:3358-3359) acts on one game by id, no active-context "
                    "read -- not applicable."
              )),
    RouteSpec("POST", r"^/api/games/[^/]+/build-roster$",
              "/api/games/{}/build-roster", "post_games_id_build_roster",
              "do_POST",
              auth="session+MANAGE_ROSTER", scope_axis="none",
              note=(
                    "#202: generic do_POST gate (server.py:2646-2660) -> "
                    "required_permission -- 'build-roster' in _ROSTER_ACTIONS "
                    "(authz.py:94-97, 307-308) -> MANAGE_ROSTER. auto_build_roster "
                    "(server.py:3360-3362) acts on one game by id, no active-context "
                    "read -- not applicable."
              )),
    RouteSpec("POST", r"^/api/games/[^/]+/cancel$", "/api/games/{}/cancel",
              "post_games_id_cancel", "do_POST",
              auth="session+MANAGE_ROSTER", scope_axis="none",
              note=(
                    "#202: generic do_POST gate (server.py:2646-2660) -> "
                    "required_permission -- 'cancel' in _ROSTER_ACTIONS "
                    "(authz.py:94-97, 307-308) -> MANAGE_ROSTER; scope_violation "
                    "(scope.py:71-74) separately refuses a coach cancelling the WHOLE "
                    "game (game-wide action), so only an operator or the sole-team "
                    "coach reaches cancel_game (server.py:3425-3429) -- no active- "
                    "context read, not applicable."
              )),
    RouteSpec("POST", r"^/api/games/[^/]+/move$", "/api/games/{}/move",
              "post_games_id_move", "do_POST",
              auth="session+MANAGE_SCHEDULE", scope_axis="none",
              note=(
                    "#202: generic do_POST gate (server.py:2646-2660) -> "
                    "required_permission -- 'move' in _SCHEDULE_ACTIONS (authz.py:93, "
                    "305-306) -> MANAGE_SCHEDULE. move_game (server.py:3382-3384) "
                    "acts on one game by id, no active-context read -- not "
                    "applicable."
              )),
    RouteSpec("POST", r"^/api/games/[^/]+/officials/assign$",
              "/api/games/{}/officials/assign",
              "post_games_id_officials_assign", "do_POST",
              auth="session+MANAGE_SCHEDULE", scope_axis="none",
              note=(
                    "#202: generic do_POST gate (server.py:2646-2660) -> "
                    "required_permission -- action.startswith('officials/') "
                    "(authz.py:305-306) -> MANAGE_SCHEDULE. assign_official "
                    "(server.py:3371-3374) acts on one game by id, no active-context "
                    "read -- not applicable."
              )),
    RouteSpec("POST", r"^/api/games/[^/]+/publish$", "/api/games/{}/publish",
              "post_games_id_publish", "do_POST",
              auth="session+MANAGE_SCHEDULE", scope_axis="none",
              note=(
                    "#202: generic do_POST gate (server.py:2646-2660) -> "
                    "required_permission -- 'publish' in _SCHEDULE_ACTIONS "
                    "(authz.py:93, 305-306) -> MANAGE_SCHEDULE. publish_game "
                    "(server.py:3380-3381) acts on one game by id, no active-context "
                    "read -- not applicable."
              )),
    RouteSpec("POST", r"^/api/games/[^/]+/reschedule/request$",
              "/api/games/{}/reschedule/request",
              "post_games_id_reschedule_request", "do_POST",
              auth="session+MANAGE_ROSTER", scope_axis="none",
              note=(
                    "#202: generic do_POST gate (server.py:2646-2660) -> "
                    "required_permission -- action == 'reschedule/request' "
                    "(authz.py:320-321) -> MANAGE_ROSTER. request_reschedule "
                    "(server.py:3385-3393) acts on one game by id, no active-context "
                    "read -- not applicable."
              )),
    RouteSpec("POST", r"^/api/games/[^/]+/reschedule/[^/]+/decide$",
              "/api/games/{}/reschedule/{}/decide",
              "post_games_id_reschedule_id_decide", "do_POST",
              auth="session+MANAGE_SCHEDULE", scope_axis="none",
              note=(
                    "#202: generic do_POST gate (server.py:2646-2660) -> "
                    "required_permission -- 'reschedule/<id>/decide' "
                    "(authz.py:322-324) -> MANAGE_SCHEDULE. decide_reschedule "
                    "(server.py:3398-3403) acts on one reschedule request by id, no "
                    "active-context read -- not applicable."
              )),
    RouteSpec("POST", r"^/api/games/[^/]+/reschedule/[^/]+/respond$",
              "/api/games/{}/reschedule/{}/respond",
              "post_games_id_reschedule_id_respond", "do_POST",
              auth="session+MANAGE_SCHEDULE-or-self", scope_axis="none",
              note=(
                    "#202: _reschedule_opponent_guard (server.py:694-721, 2490-2496), "
                    "BEFORE the generic gate -- an operator (MANAGE_SCHEDULE) may "
                    "respond to any request, else only the OPPONENT team's coach "
                    "(scope['team_id'] == the request's opponent, not the requester). "
                    "respond_to_reschedule (server.py:2495-2496) acts on one request "
                    "by id -- resource ownership, not a Program/Season/League axis."
              )),
    RouteSpec("POST", r"^/api/games/[^/]+/result$", "/api/games/{}/result",
              "post_games_id_result", "do_POST",
              auth="session+MANAGE_SCHEDULE", scope_axis="none",
              note=(
                    "#202: generic do_POST gate (server.py:2646-2660) -> "
                    "required_permission -- 'result' in _SCHEDULE_ACTIONS "
                    "(authz.py:93, 305-306) -> MANAGE_SCHEDULE. record_result "
                    "(server.py:3375-3377) acts on one game by id, no active-context "
                    "read -- not applicable."
              )),
    RouteSpec("POST", r"^/api/games/[^/]+/result/approve$",
              "/api/games/{}/result/approve", "post_games_id_result_approve",
              "do_POST",
              auth="session+MANAGE_SCHEDULE", scope_axis="none",
              note=(
                    "#202: generic do_POST gate (server.py:2646-2660) -> "
                    "required_permission -- 'result/approve' in _SCHEDULE_ACTIONS "
                    "(authz.py:93, 305-306) -> MANAGE_SCHEDULE. approve_result "
                    "(server.py:3378-3379) acts on one game by id, no active-context "
                    "read -- not applicable."
              )),
    RouteSpec("POST", r"^/api/games/[^/]+/roster/copy-previous$",
              "/api/games/{}/roster/copy-previous",
              "post_games_id_roster_copy_previous", "do_POST",
              auth="session+MANAGE_ROSTER", scope_axis="none",
              note=(
                    "#202: generic do_POST gate (server.py:2646-2660) -> "
                    "required_permission -- 'roster/copy-previous' in _ROSTER_ACTIONS "
                    "(authz.py:94-97, 307-308) -> MANAGE_ROSTER. copy_previous_roster "
                    "(server.py:3368-3370) acts on one game by id, no active-context "
                    "read -- not applicable."
              )),
    RouteSpec("POST", r"^/api/games/[^/]+/roster/lock$",
              "/api/games/{}/roster/lock", "post_games_id_roster_lock",
              "do_POST",
              auth="session+MANAGE_ROSTER", scope_axis="none",
              note=(
                    "#202: generic do_POST gate (server.py:2646-2660) -> "
                    "required_permission -- 'roster/lock' in _ROSTER_ACTIONS "
                    "(authz.py:94-97, 307-308) -> MANAGE_ROSTER. lock_roster "
                    "(server.py:3425-3429) acts on one game by id, no active-context "
                    "read -- not applicable."
              )),
    RouteSpec("POST", r"^/api/games/[^/]+/roster/remove$",
              "/api/games/{}/roster/remove", "post_games_id_roster_remove",
              "do_POST",
              auth="session+MANAGE_ROSTER", scope_axis="none",
              note=(
                    "#202: generic do_POST gate (server.py:2646-2660) -> "
                    "required_permission -- 'roster/remove' in _ROSTER_ACTIONS "
                    "(authz.py:94-97, 307-308) -> MANAGE_ROSTER. remove_player "
                    "(server.py:3366-3367) acts on one game/player pair by id, no "
                    "active-context read -- not applicable."
              )),
    RouteSpec("POST", r"^/api/games/[^/]+/roster/select$",
              "/api/games/{}/roster/select", "post_games_id_roster_select",
              "do_POST",
              auth="session+MANAGE_ROSTER", scope_axis="none",
              note=(
                    "#202: generic do_POST gate (server.py:2646-2660) -> "
                    "required_permission -- 'roster/select' in _ROSTER_ACTIONS "
                    "(authz.py:94-97, 307-308) -> MANAGE_ROSTER. select_roster "
                    "(server.py:3363-3365) acts on one game by id, no active-context "
                    "read -- not applicable."
              )),
    RouteSpec("POST", r"^/api/games/[^/]+/roster/unlock$",
              "/api/games/{}/roster/unlock", "post_games_id_roster_unlock",
              "do_POST",
              auth="session+MANAGE_ROSTER", scope_axis="none",
              note=(
                    "#202: generic do_POST gate (server.py:2646-2660) -> "
                    "required_permission -- 'roster/unlock' in _ROSTER_ACTIONS "
                    "(authz.py:94-97, 307-308) -> MANAGE_ROSTER. unlock_roster "
                    "(server.py:3425-3429) acts on one game by id, no active-context "
                    "read -- not applicable."
              )),
    RouteSpec("POST", r"^/api/games/[^/]+/substitutes/add-candidate$",
              "/api/games/{}/substitutes/add-candidate",
              "post_games_id_substitutes_add_candidate", "do_POST",
              auth="session+MANAGE_ROSTER", scope_axis="none",
              note=(
                    "#202: generic do_POST gate (server.py:2646-2660) -> "
                    "required_permission -- 'substitutes/add-candidate' in "
                    "_ROSTER_ACTIONS (authz.py:94-97, 307-308) -> MANAGE_ROSTER. "
                    "add_substitute_candidate (server.py:3408-3414) acts on one "
                    "game/player pair by id, no active-context read -- not "
                    "applicable."
              )),
    RouteSpec("POST", r"^/api/games/[^/]+/substitutes/enroll$",
              "/api/games/{}/substitutes/enroll",
              "post_games_id_substitutes_enroll", "do_POST",
              auth="session+RESPOND_AVAILABILITY", scope_axis="none",
              note=(
                    "#202: generic do_POST gate (server.py:2646-2660) -> "
                    "required_permission -- 'substitutes/enroll' in "
                    "_AVAILABILITY_ACTIONS (authz.py:98-100, 309-310) -> "
                    "RESPOND_AVAILABILITY. enroll_substitute (server.py:3404-3405) "
                    "acts on one game/player pair by id, no active-context read -- "
                    "not applicable."
              )),
    RouteSpec("POST", r"^/api/games/[^/]+/substitutes/withdraw$",
              "/api/games/{}/substitutes/withdraw",
              "post_games_id_substitutes_withdraw", "do_POST",
              auth="session+RESPOND_AVAILABILITY",
              scope_axis="none",
              note=("#202: generic gate (server.py:2648-2660) -> authz.py:309-310 (`_AVAILABILITY_ACTIONS`, action=='substitutes/withdraw') -> RESPOND_AVAILABILITY. server.py:3406-3407 -> `withdraw_substitute(gid, pid, user_id)` (service.py:3930-3933) -- no user_id/role/scope parameter, no P/S/L check.")),
    RouteSpec("POST", r"^/api/games/[^/]+/substitutes/[^/]+/accept$",
              "/api/games/{}/substitutes/{}/accept",
              "post_games_id_substitutes_id_accept", "do_POST",
              auth="session+RESPOND_AVAILABILITY",
              scope_axis="none",
              note=("#202: generic gate -> authz.py:325-331 (`substitutes/[^/]+/(offer|accept|decline|add-to-roster)`, op=='accept') -> RESPOND_AVAILABILITY. server.py:3415-3424 -> `accept_substitute(gid, player_id, user_id)` (service.py:3944-3947) -- no P/S/L check.")),
    RouteSpec("POST", r"^/api/games/[^/]+/substitutes/[^/]+/add-to-roster$",
              "/api/games/{}/substitutes/{}/add-to-roster",
              "post_games_id_substitutes_id_add_to_roster", "do_POST",
              auth="session+MANAGE_ROSTER",
              scope_axis="none",
              note=("#202: generic gate -> authz.py:325-331, op=='add-to-roster' -> MANAGE_ROSTER (coach controls the pool). server.py:3415-3424 -> `add_substitute_to_roster(gid, player_id, user_id)` (service.py:3954-3958) -- no P/S/L check.")),
    RouteSpec("POST", r"^/api/games/[^/]+/substitutes/[^/]+/decline$",
              "/api/games/{}/substitutes/{}/decline",
              "post_games_id_substitutes_id_decline", "do_POST",
              auth="session+RESPOND_AVAILABILITY",
              scope_axis="none",
              note=("#202: generic gate -> authz.py:325-331, op=='decline' -> RESPOND_AVAILABILITY. server.py:3415-3424 -> `decline_substitute(gid, player_id, user_id)` (service.py:3949-3952) -- no P/S/L check.")),
    RouteSpec("POST", r"^/api/games/[^/]+/substitutes/[^/]+/offer$",
              "/api/games/{}/substitutes/{}/offer",
              "post_games_id_substitutes_id_offer", "do_POST",
              auth="session+MANAGE_ROSTER",
              scope_axis="none",
              note=("#202: generic gate -> authz.py:325-331, op=='offer' -> MANAGE_ROSTER. server.py:3415-3420 -> `offer_substitute(gid, player_id, user_id, expires_at=...)` (service.py:3935-3942) -- no P/S/L check.")),
    RouteSpec("POST", r"^/api/games/[^/]+/.+$", "/api/games/{}/{*}",
              "post_games_id_action", "do_POST", kind="family",
              auth="none", scope_axis="none",
              note=("the family regex; every real action is a sibling "
                    "spec below it. #202: a structural marker only -- the "
                    "bare `^/api/games/[^/]+/.+$` regex is never "
                    "independently dispatched (do_POST's own `/api/games/"
                    "([^/]+)/(.+)$` match, server.py:3327-3329, always "
                    "falls into one of the concrete `if action == ...` "
                    "branches below it); this entry reaches no handler "
                    "code and touches no resource of its own.")),
    RouteSpec("POST", r"^/api/guardians/links$", "/api/guardians/links",
              "post_guardians_links", "do_POST",
              auth="session+MANAGE_USERS",
              scope_axis="none",
              note=("#202: generic gate (server.py:2648-2660) -> authz.py:231-232 -> MANAGE_USERS. server.py:3293-3296 -> `create_guardian_link(...)`, no P/S/L concept (identity administration).")),
    RouteSpec("POST", r"^/api/guardians/links/[^/]+/verify$",
              "/api/guardians/links/{}/verify",
              "post_guardians_links_id_verify", "do_POST",
              auth="session+MANAGE_USERS",
              scope_axis="none",
              note=("#202: generic gate -> authz.py:233-234 -> MANAGE_USERS. server.py:3297-3300 -> `verify_guardian_link(...)`, no P/S/L concept.")),
    RouteSpec("POST", r"^/api/import/commit/officials-availability$",
              "/api/import/commit/officials-availability",
              "post_import_commit_officials_availability", "do_POST",
              auth="session+MANAGE_SCHEDULE",
              scope_axis="program",
              note=("#202: generic gate -> authz.py:140-141 -> MANAGE_SCHEDULE. server.py:2990-2993 -- per its own #411 comment, this commit resolves EXISTING Program-scoped Officials/Clubs by name/external-ref and is gated by `ApiService.setup_guarded_import` (service.py:2916-2960) on the Program axis, not a parent id.")),
    RouteSpec("POST", r"^/api/import/commit/rinks-ice-slots$",
              "/api/import/commit/rinks-ice-slots",
              "post_import_commit_rinks_ice_slots", "do_POST",
              auth="session+MANAGE_ARENA",
              scope_axis="program",
              note=("#202: generic gate -> authz.py:147-148 -> MANAGE_ARENA. server.py:3001-3004 -- per its own #411 comment, `rink_code`/`venue_name` resolve EXISTING Program-scoped Rinks/Venues and the commit is gated by `ApiService.setup_guarded_import` (service.py:2916-2960) on the Program axis.")),
    RouteSpec("POST", r"^/api/import/commit/teams-players$",
              "/api/import/commit/teams-players",
              "post_import_commit_teams_players", "do_POST",
              auth="session+MANAGE_SETUP",
              scope_axis="season",
              note=("#202: generic gate -> authz.py:131-132 -> MANAGE_SETUP. server.py:2967-2971 -- `_guarded_create('division', [('season', body.get('season_id') or None)], ...)`, per its own #409 comment 'a Division is SEASON-OWNED' -- `ApiService._CREATE_TWO_AXIS`, same class as post_setup_division.")),
    RouteSpec("POST", r"^/api/import/dry-run$", "/api/import/dry-run",
              "post_import_dry_run", "do_POST",
              auth="session+MANAGE_ARENA",
              scope_axis="none",
              note=("#202: generic gate -> authz.py:125-126 -> MANAGE_ARENA. server.py:2942-2943 -> `get_import_dry_run(body)` validates CSV-shaped rows and writes nothing; no target record to compare an axis against.")),
    RouteSpec("POST", r"^/api/me/guardian/[^/]+/games/[^/]+/availability$",
              "/api/me/guardian/{}/games/{}/availability",
              "post_me_guardian_id_games_id_availability", "do_POST",
              auth="session+guardian-scope+verified-link",
              scope_axis="none",
              note=("#202: `_require_guardian_scope` (server.py:2560-2562) THEN `_guardian_link_or_403(guid, jid)` (server.py:2564-2565) -- a guardian session alone is not enough; the guardian must hold a VERIFIED link to the named junior. server.py:2574-2581 -> `set_availability(gid, jid, ..., 'guardian', guid)`.")),
    RouteSpec("POST",
              r"^/api/me/guardian/[^/]+/substitute-opportunities/[^/]+/accept-offer$",
              "/api/me/guardian/{}/substitute-opportunities/{}/accept-offer",
              "post_me_guardian_id_substitute_opportunities_id_accept_offer",
              "do_POST",
              auth="session+guardian-scope+verified-link",
              scope_axis="none",
              note=("#202: same `_require_guardian_scope` + `_guardian_link_or_403` gate as post_me_guardian_id_games_id_availability (server.py:2586-2590). server.py:2592-2594 -> `accept_substitute(gid, jid, actor_id=guid)`.")),
    RouteSpec("POST",
              r"^/api/me/guardian/[^/]+/substitute-opportunities/[^/]+/decline-offer$",
              "/api/me/guardian/{}/substitute-opportunities/{}/decline-offer",
              "post_me_guardian_id_substitute_opportunities_id_decline_offer",
              "do_POST",
              auth="session+guardian-scope+verified-link",
              scope_axis="none",
              note=("#202: same gate as post_me_guardian_id_substitute_opportunities_id_accept_offer (server.py:2586-2590). server.py:2595-2596 -> `decline_substitute(gid, jid, actor_id=guid)`.")),
    RouteSpec("POST",
              r"^/api/me/substitute-opportunities/[^/]+/accept-offer$",
              "/api/me/substitute-opportunities/{}/accept-offer",
              "post_me_substitute_opportunities_id_accept_offer", "do_POST",
              auth="session+player-scope",
              scope_axis="none",
              note=("#202: `_require_player_scope` (server.py:2531-2533) -- the browser never passes a player_id; both target and actor come from the session. server.py:2544-2546 -> `accept_substitute(gid, ppid, actor_id=uid)`.")),
    RouteSpec("POST",
              r"^/api/me/substitute-opportunities/[^/]+/decline-offer$",
              "/api/me/substitute-opportunities/{}/decline-offer",
              "post_me_substitute_opportunities_id_decline_offer", "do_POST",
              auth="session+player-scope",
              scope_axis="none",
              note=("#202: same `_require_player_scope` gate (server.py:2531-2533). server.py:2547-2548 -> `decline_substitute(gid, ppid, actor_id=uid)`.")),
    RouteSpec("POST", r"^/api/me/substitute-opportunities/[^/]+/enroll$",
              "/api/me/substitute-opportunities/{}/enroll",
              "post_me_substitute_opportunities_id_enroll", "do_POST",
              auth="session+player-scope",
              scope_axis="none",
              note=("#202: same `_require_player_scope` gate (server.py:2531-2533). server.py:2538-2540 -> `enroll_substitute(gid, ppid, actor_id=uid)`.")),
    RouteSpec("POST", r"^/api/me/substitute-opportunities/[^/]+/withdraw$",
              "/api/me/substitute-opportunities/{}/withdraw",
              "post_me_substitute_opportunities_id_withdraw", "do_POST",
              auth="session+player-scope",
              scope_axis="none",
              note=("#202: same `_require_player_scope` gate (server.py:2531-2533). server.py:2541-2543 -> `withdraw_substitute(gid, ppid, actor_id=uid)`.")),
    RouteSpec("POST", r"^/api/notifications/contacts$",
              "/api/notifications/contacts", "post_notifications_contacts",
              "do_POST",
              auth="session+MANAGE_SCHEDULE",
              scope_axis="none",
              note=("#202: generic gate -> authz.py:192-194 -> MANAGE_SCHEDULE. server.py:3177-3180 -> `set_contact_destination(...)`, an installation-wide registry row keyed by recipient_ref, no P/S/L concept.")),
    RouteSpec("POST", r"^/api/notifications/contacts/[^/]+/active$",
              "/api/notifications/contacts/{}/active",
              "post_notifications_contacts_id_active", "do_POST",
              auth="session+MANAGE_SETUP",
              scope_axis="none",
              note=("#202: generic gate -> authz.py:198-200 -> MANAGE_SETUP (the branch's own comment: 'takes the League-Admin-only MANAGE_SETUP permission that gates Player/Official deletion itself'). server.py:3185-3189 -> `set_contact_destination_active(...)`.")),
    RouteSpec("POST", r"^/api/notifications/deliveries/process$",
              "/api/notifications/deliveries/process",
              "post_notifications_deliveries_process", "do_POST",
              auth="session+MANAGE_SCHEDULE",
              scope_axis="none",
              note=("#202: generic gate -> authz.py:187-188 -> MANAGE_SCHEDULE. server.py:3160-3161 -> `process_notification_deliveries()`, an installation-wide queue drain.")),
    RouteSpec("POST", r"^/api/notifications/deliveries/[^/]+/ignore$",
              "/api/notifications/deliveries/{}/ignore",
              "post_notifications_deliveries_id_ignore", "do_POST",
              auth="session+MANAGE_SCHEDULE",
              scope_axis="none",
              note=("#202: generic gate -> authz.py:190-191 (`retry|ignore`) -> MANAGE_SCHEDULE. server.py:3167-3169 -> `ignore_notification_delivery(...)`, keyed by delivery id.")),
    RouteSpec("POST", r"^/api/notifications/deliveries/[^/]+/retry$",
              "/api/notifications/deliveries/{}/retry",
              "post_notifications_deliveries_id_retry", "do_POST",
              auth="session+MANAGE_SCHEDULE",
              scope_axis="none",
              note=("#202: generic gate -> authz.py:190-191 (`retry|ignore`) -> MANAGE_SCHEDULE. server.py:3164-3166 -> `retry_notification_delivery(...)`, keyed by delivery id.")),
    RouteSpec("POST", r"^/api/notifications/device-tokens$",
              "/api/notifications/device-tokens",
              "post_notifications_device_tokens", "do_POST",
              auth="session+MANAGE_SCHEDULE",
              scope_axis="none",
              note=("#202: generic gate -> authz.py:202-203 -> MANAGE_SCHEDULE. server.py:3192-3195 -> `register_device_token(...)`, an installation-wide registry row.")),
    RouteSpec("POST", r"^/api/notifications/device-tokens/[^/]+/active$",
              "/api/notifications/device-tokens/{}/active",
              "post_notifications_device_tokens_id_active", "do_POST",
              auth="session+MANAGE_SCHEDULE",
              scope_axis="none",
              note=("#202: generic gate -> authz.py:204-205 -> MANAGE_SCHEDULE. server.py:3196-3199 -> `set_device_token_active(...)`.")),
    RouteSpec("POST", r"^/api/notifications/preferences$",
              "/api/notifications/preferences",
              "post_notifications_preferences", "do_POST",
              auth="session+MANAGE_SCHEDULE-or-self",
              scope_axis="none",
              note=("#202: custom `_prefs_guard` (server.py:1242-1259, 2423-2426), checked BEFORE the generic gate -- operator (MANAGE_SCHEDULE) manages any recipient_ref, else only the caller's own, same rule as the GET sibling. server.py:2427-2433 -> `set_notification_preference(...)`, keyed by recipient_ref, not P/S/L.")),
    RouteSpec("POST", r"^/api/notifications/preferences/[^/]+/active$",
              "/api/notifications/preferences/{}/active",
              "post_notifications_preferences_id_active", "do_POST",
              auth="session+MANAGE_SETUP",
              scope_axis="none",
              note=("#202: generic gate -> authz.py:206-210 -> MANAGE_SETUP (same League-Admin-only rationale as the contact-destination retire). server.py:3204-3208 -> `set_notification_preference_active(...)`.")),
    RouteSpec("POST", r"^/api/notifications/read-all$",
              "/api/notifications/read-all", "post_notifications_read_all",
              "do_POST",
              auth="session",
              scope_axis="none",
              note=("#202: generic gate resolves role/session (server.py:2648-2651) but `required_permission('/api/notifications/read-all')` matches none of authz.py's explicit rules and falls through to `return None` (authz.py:334) -- `authorize()` then returns True for any role. server.py:3303-3305 -> `mark_all_notifications_read(role.value, scope, user_id=user_id)`, audience-scoped (own notifications), not P/S/L.")),
    RouteSpec("POST", r"^/api/notifications/[^/]+/read$",
              "/api/notifications/{}/read", "post_notifications_id_read",
              "do_POST",
              auth="session",
              scope_axis="none",
              note=("#202: same as post_notifications_read_all -- `required_permission('/api/notifications/{id}/read')` matches no authz.py rule, falls to `None` (authz.py:334), any signed-in role. server.py:3306-3309 -> `mark_notification_read(...)`.")),
    RouteSpec("POST", r"^/api/officials/assignments/[^/]+/accept$",
              "/api/officials/assignments/{}/accept",
              "post_officials_assignments_id_accept", "do_POST",
              auth="session+RESPOND_ASSIGNMENT-or-self",
              scope_axis="none",
              note=("#202: generic gate -> authz.py:245-248 (accept/decline) -> RESPOND_ASSIGNMENT, PLUS the generic `scope_violation` (server.py:2667-2673 -> scope.py:108-118 `_ASSIGN_RESPOND`) restricts an OFFICIAL to their OWN assignment only. server.py:3312-3324 -> `respond_assignment(aid, True, user_id)`.")),
    RouteSpec("POST", r"^/api/officials/assignments/[^/]+/decline$",
              "/api/officials/assignments/{}/decline",
              "post_officials_assignments_id_decline", "do_POST",
              auth="session+RESPOND_ASSIGNMENT-or-self",
              scope_axis="none",
              note=("#202: same gate as post_officials_assignments_id_accept (authz.py:245-248, scope.py:108-118). server.py:3312-3324 -> `respond_assignment(aid, False, user_id)`.")),
    RouteSpec("POST", r"^/api/officials/assignments/[^/]+/unassign$",
              "/api/officials/assignments/{}/unassign",
              "post_officials_assignments_id_unassign", "do_POST",
              auth="session+MANAGE_SCHEDULE",
              scope_axis="none",
              note=("#202: generic gate -> authz.py:245-248 (op=='unassign') -> MANAGE_SCHEDULE, an operator-only action (`scope_violation`'s `_ASSIGN_RESPOND` self-check only covers accept/decline, scope.py:110-118, so unassign is gated on permission alone). server.py:3321-3323 -> `unassign_official(aid, user_id)`.")),
    RouteSpec("POST", r"^/api/officials/availability/[^/]+/delete$",
              "/api/officials/availability/{}/delete",
              "post_officials_availability_id_delete", "do_POST",
              auth="session+MANAGE_SCHEDULE-or-self",
              scope_axis="none",
              note=("#202: custom `_official_guard(avail.official_id)` (server.py:723-739, 2476-2477), checked BEFORE the generic gate -- operator (MANAGE_SCHEDULE) manages any official, else only their own, same rule as the GET availability sibling. server.py:2480-2481 -> `delete_official_availability(...)`.")),
    RouteSpec("POST", r"^/api/officials/[^/]+/availability$",
              "/api/officials/{}/availability",
              "post_officials_id_availability", "do_POST",
              auth="session+MANAGE_SCHEDULE-or-self",
              scope_axis="none",
              note=("#202: custom `_official_guard` (server.py:2438-2439), checked BEFORE the generic gate. server.py:2467-2469 -> `set_official_availability(...)`, same rule as the GET availability sibling.")),
    RouteSpec("POST", r"^/api/public/calendar-feeds$",
              "/api/public/calendar-feeds", "post_public_calendar_feeds",
              "do_POST",
              auth="none",
              scope_axis="none",
              note=("#202: anonymous, only `_rate_limited('public_feed_mint', ...)` (server.py:2409-2410), no `_resolve_role` call -- the branch's own comment: 'anyone may mint a team or division feed token, no session required'. server.py:2417-2418 -> `create_calendar_feed_token(actor_type, actor_ref, ...)`, keyed by (team|division) id, not P/S/L.")),
    RouteSpec("POST", r"^/api/reset$", "/api/reset", "post_reset", "do_POST",
              auth="session+MANAGE_SETUP",
              scope_axis="none",
              note=("#202: generic gate -> authz.py:114-116 -> MANAGE_SETUP (League-Admin-only, per the branch's own comment: '#215... League-Admin-only operation'). server.py:2675-2718 -- a back-compat alias for /api/demo/reset; wipes and reseeds the WHOLE installation, no single P/S/L target to compare against.")),
    RouteSpec("POST", r"^/api/scheduler/commit$", "/api/scheduler/commit",
              "post_scheduler_commit", "do_POST",
              auth="session+MANAGE_SCHEDULE",
              scope_axis="cross",
              note=("#202: generic gate -> authz.py:178-181 -> MANAGE_SCHEDULE. server.py:3106-3144 -> `commit_draft_schedule(..., user_id, role, scope)` (service.py:7261-...) -- bound to the caller's active tuple via the same `_authorize_schedule_target` edge check `draft_season_schedule` uses (service.py:6370, 6542) -- Program+Season+League as one edge, #409's 'cross' class, per the branch's own #386 comment.")),
    RouteSpec("POST", r"^/api/scheduler/draft$", "/api/scheduler/draft",
              "post_scheduler_draft", "do_POST",
              auth="session+MANAGE_SCHEDULE",
              scope_axis="cross",
              note=("#202: generic gate -> authz.py:172-174 -> MANAGE_SCHEDULE. server.py:3024-3053 -> `draft_season_schedule(..., user_id=user_id, role=role, scope=scope)` (service.py:6503-6556) -- `self._authorize_schedule_target(...)` (service.py:6370, called 6542) compares the requested (division/season/league) against the caller's persisted active tuple as ONE edge -- #409's 'cross' class.")),
    RouteSpec("POST", r"^/api/scheduler/drafts/discard$",
              "/api/scheduler/drafts/discard",
              "post_scheduler_drafts_discard", "do_POST",
              auth="session+MANAGE_SCHEDULE",
              scope_axis="cross",
              note=("#202: generic gate -> authz.py:178-181 -> MANAGE_SCHEDULE. server.py:3154-3157 -> `discard_draft_games(..., user_id=user_id, role=role, scope=scope)` (service.py:8512-...) -- 'Bound to the caller's active tuple (#386)' (docstring), same Program+Season+League edge as its publish sibling, #409's 'cross' class.")),
    RouteSpec("POST", r"^/api/scheduler/drafts/publish$",
              "/api/scheduler/drafts/publish",
              "post_scheduler_drafts_publish", "do_POST",
              auth="session+MANAGE_SCHEDULE",
              scope_axis="cross",
              note=("#202: generic gate -> authz.py:178-181 -> MANAGE_SCHEDULE. server.py:3150-3153 -> `publish_draft_games(..., user_id=user_id, role=role, scope=scope)` (service.py:8407-...) -- 'Bound to the caller's active tuple (#386)' (docstring), Program+Season+League edge, #409's 'cross' class.")),
    RouteSpec("POST", r"^/api/scheduler/scenarios$",
              "/api/scheduler/scenarios", "post_scheduler_scenarios",
              "do_POST",
              auth="session+MANAGE_SCHEDULE+real-account",
              scope_axis="cross",
              note=("#202: generic gate -> authz.py:175-177 -> MANAGE_SCHEDULE, PLUS `if user_id is None: 401` (server.py:3072-3075). server.py:3079-3087 -> `create_schedule_scenario(..., user_id=user_id, role=role, scope=scope)` (service.py:6690-...) -- the ACTIVE-TUPLE SCOPING block (service.py:6556-6598) judges the scenario's (program_id, season_id, league_id) as ONE WHOLE EDGE, #409's 'cross' class.")),
    RouteSpec("POST", r"^/api/scheduler/scenarios/[^/]+/commit$",
              "/api/scheduler/scenarios/{}/commit",
              "post_scheduler_scenarios_id_commit", "do_POST",
              auth="session+MANAGE_SCHEDULE+real-account",
              scope_axis="cross",
              note=("#202: generic gate -> authz.py:176 -> MANAGE_SCHEDULE, PLUS `if user_id is None: 401` (server.py:3095-3098). server.py:3099-3101 -> `commit_schedule_scenario(..., user_id=user_id, role=role, scope=scope)` (service.py:7120-...) -- re-resolves and checks the caller's active tuple against the stored scenario's edge on every retry attempt, same 'cross' class as get_scheduler_scenarios_id.")),
    RouteSpec("POST", r"^/api/setup/club$", "/api/setup/club",
              "post_setup_club", "_handle_setup",
              auth="session+MANAGE_SETUP", scope_axis="zero_axis",
              note=("#202: authorize(role, path) (server.py:2652) -> "
                    "required_permission (authz.py:298, 'club' in "
                    "_LEAGUE_SETUP) -> MANAGE_SETUP. scope_axis: "
                    "_guarded_create('club', [], ...) (server.py:3900-3904) "
                    "-- an empty parents list, ApiService._CREATE_ZERO_AXIS "
                    "(api/service.py:2174) -- a Club has no parent FK, so "
                    "nothing is compared (a fresh root, #369/#409's "
                    "'ZERO-AXIS ROOT')")),
    RouteSpec("POST", r"^/api/setup/club/[^/]+/delete$",
              "/api/setup/club/{}/delete", "post_setup_club_id_delete",
              "_handle_setup",
              auth="session+MANAGE_SETUP", scope_axis="league",
              note=("#202: authorize(role, path) (server.py:2652) -> "
                    "required_permission -- the delete PATH ('club/{id}/"
                    "delete') matches none of authz.py's specific rules "
                    "(279-290) and is not itself a bare '_LEAGUE_SETUP' "
                    "member, so it falls to the generic '/api/setup/' "
                    "catch-all's unknown-entity default (authz.py:300) -> "
                    "MANAGE_SETUP -- same permission the bare 'club' create "
                    "gets via the _LEAGUE_SETUP branch (298), by "
                    "coincidence of the same default, not by a dedicated "
                    "rule. scope_axis: _guarded_mutation targets the "
                    "existing Club (server.py:3792-3795) through "
                    "_reject_target_outside_scope -> setup_target_accessible "
                    "-> _setup_target_edges kind=='club' (api/service.py:"
                    "921-930), which unions its Teams' edges "
                    "(_team_edges, api/service.py:556-623): every edge is "
                    "(program, None, league) -- Program+League, no "
                    "Season")),
    RouteSpec("POST", r"^/api/setup/division$", "/api/setup/division",
              "post_setup_division", "_handle_setup",
              auth="session+MANAGE_SETUP", scope_axis="season",
              note=("#202: authorize(role, path) (server.py:2652) -> "
                    "required_permission (authz.py:298, 'division' in "
                    "_LEAGUE_SETUP) -> MANAGE_SETUP. scope_axis: "
                    "_guarded_create('division', [('season', ...), "
                    "('league', ...)], ...) (server.py:3887-3896, comment "
                    "'SEASON-OWNED (#409)') -- ApiService._CREATE_TWO_AXIS "
                    "(api/service.py:2186) -- both the parent Season AND "
                    "its Program are compared as one unit")),
    RouteSpec("POST", r"^/api/setup/division/[^/]+/assign-level$",
              "/api/setup/division/{}/assign-level",
              "post_setup_division_id_assign_level", "_handle_reassign",
              auth="session+MANAGE_SETUP", scope_axis="cross",
              note=("#202 repair root cause 1: the concrete leaf, not "
                    "the assign-\\w+ wildcard family that used to stand "
                    "in for it. _handle_reassign's own "
                    "_V1_REASSIGN_SCHEMA/_V1_REASSIGN_CALL admit ONLY "
                    "(division, level) for this entity -- no other "
                    "assign-<word> on /api/setup/division/<id> reaches "
                    "anything but a 404. #202 auth: authz.py:288-290 "
                    "((division|team|player)/[^/]+/assign-\\w+) -> "
                    "MANAGE_SETUP. #202 scope_axis: _handle_reassign builds "
                    "TWO _guarded_mutation targets (server.py:3501-3503, "
                    "no writable_parent leg -- ('division','level') is not "
                    "in _REASSIGN_PARENTS, server.py:204-210) each judged "
                    "independently by setup_target_accessible: the SOURCE "
                    "Division (cross -- delegates to its LeagueSeason, "
                    "api/service.py:862-866,834-860 -- Program+Season+"
                    "League) and the DESTINATION League/'level' (league -- "
                    "api/service.py:825-832 -- Program+League). Validating "
                    "every axis either end touches is exactly #409's "
                    "'cross' class")),
    RouteSpec("POST", r"^/api/setup/division/[^/]+/delete$",
              "/api/setup/division/{}/delete",
              "post_setup_division_id_delete", "_handle_setup",
              auth="session+MANAGE_SETUP", scope_axis="cross",
              note=("#202: authorize(role, path) (server.py:2652) -> "
                    "required_permission -- 'division/{id}/delete' matches "
                    "no specific authz.py rule, falls to the generic "
                    "unknown-entity default (authz.py:300) -> MANAGE_SETUP "
                    "(same value the bare 'division' create gets via "
                    "_LEAGUE_SETUP, authz.py:298, by coincidence not by a "
                    "dedicated delete rule). scope_axis: _guarded_mutation "
                    "-> setup_target_accessible -> _setup_target_edges "
                    "kind=='division' (api/service.py:862-866) delegates "
                    "to its LeagueSeason (834-860): edge = (program, "
                    "season, league) -- all three axes")),
    RouteSpec("POST", r"^/api/setup/game$", "/api/setup/game",
              "post_setup_game", "_handle_setup",
              auth="session+MANAGE_SCHEDULE", scope_axis="season",
              note=("#202: authorize(role, path) (server.py:2652) -> "
                    "required_permission (authz.py:294-295, bare entity "
                    "'game') -> MANAGE_SCHEDULE, NOT MANAGE_SETUP (Game is "
                    "the one v1 setup entity gated on the scheduling "
                    "permission, matching _handle_setup's own game/{id}/"
                    "delete asymmetry noted below). scope_axis: "
                    "_guarded_create('game', [('season', ...), "
                    "('division', ...)], ...) (server.py:3970-3987, "
                    "comment 'SEASON-OWNED (#409)') -- "
                    "ApiService._CREATE_TWO_AXIS (api/service.py:2187)")),
    RouteSpec("POST", r"^/api/setup/game/[^/]+/delete$",
              "/api/setup/game/{}/delete", "post_setup_game_id_delete",
              "_handle_setup",
              auth="session+MANAGE_SETUP", scope_axis="cross",
              note=("#202: authorize(role, path) -- 'game/{id}/delete' "
                    "matches no specific authz.py rule (294-295 tests the "
                    "BARE entity 'game' only) and falls to the generic "
                    "unknown-entity default (authz.py:300) -> MANAGE_SETUP "
                    "-- asymmetric with the MANAGE_SCHEDULE the create "
                    "above requires; both are real, current behavior, not "
                    "reconciled here (enforcement/matrix is the separate "
                    "second #202 PR). scope_axis: _setup_target_edges "
                    "kind=='game' (api/service.py:877-919) folds EVERY "
                    "non-null parent (_game_parent_constraints, "
                    "api/service.py:625-716: season_id, league_id, "
                    "league_season_id, division_id) into one edge, failing "
                    "closed on disagreement -- Program+Season+League when "
                    "fully parented, exactly #409's 'cross' class")),
    RouteSpec("POST", r"^/api/setup/ice-availability/commit$",
              "/api/setup/ice-availability/commit",
              "post_setup_ice_availability_commit", "do_POST",
              auth="session+MANAGE_ARENA",
              scope_axis="season",
              note=("#202: generic gate -> authz.py:155-157 -> MANAGE_ARENA. server.py:2871-2878 requires an active Program+Season first (409 otherwise), then server.py:2897-2902 -> `commit_ice_availability_in_active_season(user_id, role, scope, season_id=season_id, ...)` -- the named season_id must equal the caller's exact active Season (#393 PR A owner ruling, server.py:2852-2878 comment) -- single-axis Season pin.")),
    RouteSpec("POST", r"^/api/setup/ice-availability/preview$",
              "/api/setup/ice-availability/preview",
              "post_setup_ice_availability_preview", "do_POST",
              auth="session+MANAGE_ARENA",
              scope_axis="season",
              note=("#202: generic gate -> authz.py:155-157 -> MANAGE_ARENA. server.py:2871-2878 requires an active Program+Season first, then server.py:2883-2886 -> `_reject_target_outside_scope('season', season_id, user_id, role, scope, 'active_season')` -- the named season_id must equal the caller's exact active Season.")),
    RouteSpec("POST", r"^/api/setup/ice-slot$", "/api/setup/ice-slot",
              "post_setup_ice_slot", "_handle_setup",
              auth="session+MANAGE_ARENA", scope_axis="program",
              note=("#202: authorize(role, path) -> required_permission "
                    "(authz.py:296-297, 'ice-slot' in _ARENA_SETUP) -> "
                    "MANAGE_ARENA. scope_axis: _guarded_create('ice_slot', "
                    "[('rink', ...)], ...) (server.py:3963-3969) -- "
                    "ApiService._CREATE_PROGRAM_AXIS (api/service.py:2179) "
                    "-- only the parent Rink's Program is compared")),
    RouteSpec("POST", r"^/api/setup/ice-slot/[^/]+/delete$",
              "/api/setup/ice-slot/{}/delete",
              "post_setup_ice_slot_id_delete", "_handle_setup",
              auth="session+MANAGE_SETUP", scope_axis="season",
              note=("#202: authorize(role, path) -- 'ice-slot/{id}/delete' "
                    "matches no specific authz.py rule and falls to the "
                    "generic unknown-entity default (authz.py:300) -> "
                    "MANAGE_SETUP -- asymmetric with the MANAGE_ARENA the "
                    "create above requires (real, current behavior; not "
                    "reconciled here). scope_axis: _setup_target_edges "
                    "kind=='ice_slot' (api/service.py:964-970) delegates "
                    "to its Rink -> Venue edges (_venue_edges, "
                    "api/service.py:728-767): Program+Season via grants, "
                    "or Program-only via the legacy link -- never League")),
    RouteSpec("POST", r"^/api/setup/league$", "/api/setup/league",
              "post_setup_league", "_handle_setup",
              auth="session+MANAGE_SETUP", scope_axis="zero_axis",
              note=("#202: v1 'league' IS today's Program "
                    "(_V1_SETUP_KIND, server.py:953). authorize(role, "
                    "path) -> required_permission (authz.py:298, 'league' "
                    "in _LEAGUE_SETUP) -> MANAGE_SETUP. scope_axis: "
                    "_guarded_create('program', [], ...) (server.py:"
                    "3852-3863, comment 'ZERO-AXIS ROOT (#409)') -- "
                    "ApiService._CREATE_ZERO_AXIS (api/service.py:2173) "
                    "-- no parent, nothing compared")),
    RouteSpec("POST", r"^/api/setup/league/[^/]+/assign-organization$",
              "/api/setup/league/{}/assign-organization",
              "post_setup_league_id_assign_organization", "_handle_reassign",
              auth="session+MANAGE_SETUP", scope_axis="cross",
              note=("#202 repair root cause 1: the concrete leaf. "
                    "_V1_REASSIGN_SCHEMA/_V1_REASSIGN_CALL admit ONLY "
                    "(league, organization) for this entity. #202 auth: "
                    "authz.py:279-280, an explicit override (checked "
                    "BEFORE the venue|rink arena rule at 285-287, so this "
                    "one league->organization move is MANAGE_SETUP -- "
                    "'cross-domain league[Program]->owner move ... spans "
                    "both domains' per that rule's own comment -- not the "
                    "MANAGE_ARENA its venue/rink assign-organization "
                    "siblings get). #202 scope_axis: THREE "
                    "_guarded_mutation targets (server.py:3501-3519): the "
                    "SOURCE Program (v1 'league' translated, program -- "
                    "api/service.py:817-818, itself only), the DESTINATION "
                    "Organization (season -- api/service.py:972-986, "
                    "Program+Season via its Programs/Venues, never "
                    "League), and a THIRD 'writable_parent' check on that "
                    "same Organization (setup_parent_writable, "
                    "server.py:3659-3661; _REASSIGN_PARENTS[('league', "
                    "'organization')], server.py:205) -- validating every "
                    "axis each end touches")),
    RouteSpec("POST", r"^/api/setup/league/[^/]+/delete$",
              "/api/setup/league/{}/delete", "post_setup_league_id_delete",
              "_handle_setup",
              auth="session+MANAGE_SETUP", scope_axis="program",
              note=("#202: v1 'league' IS today's Program (_V1_SETUP_KIND, "
                    "server.py:953,3788,3793). authorize(role, path) -- "
                    "'league/{id}/delete' matches no specific authz.py "
                    "rule and falls to the generic unknown-entity default "
                    "(authz.py:300) -> MANAGE_SETUP (same value the bare "
                    "'league' create gets via _LEAGUE_SETUP, authz.py:298, "
                    "by coincidence). scope_axis: _setup_target_edges "
                    "kind=='program' (api/service.py:817-818): edge = "
                    "(record.id, None, None) -- only the Program axis is "
                    "compared, unlike the create above which compares "
                    "nothing at all (a genuinely different operation, "
                    "hence a different axis class for the same "
                    "translated kind)")),
    RouteSpec("POST", r"^/api/setup/level$", "/api/setup/level",
              "post_setup_level", "_handle_setup",
              auth="session+MANAGE_SETUP", scope_axis="season",
              note=("#202: v1 'level' IS today's competition League "
                    "(_V1_SETUP_KIND, server.py:953). authorize(role, "
                    "path) -> required_permission (authz.py:298, 'level' "
                    "in _LEAGUE_SETUP) -> MANAGE_SETUP. scope_axis: "
                    "_guarded_create('league', [('season', ...)], ...) "
                    "(server.py:3874-3886, comment 'SEASON-OWNED (#409)') "
                    "-- ApiService._CREATE_TWO_AXIS (api/service.py:2184) "
                    "-- the create also mints the LeagueSeason, so it "
                    "consumes the Season named by season_id even though a "
                    "League record's own edges (below) never carry one")),
    RouteSpec("POST", r"^/api/setup/level/[^/]+/delete$",
              "/api/setup/level/{}/delete", "post_setup_level_id_delete",
              "_handle_setup",
              auth="session+MANAGE_SETUP", scope_axis="league",
              note=("#202: v1 'level' IS today's competition League "
                    "(_V1_SETUP_KIND). authorize(role, path) -- "
                    "'level/{id}/delete' matches no specific authz.py rule "
                    "and falls to the generic unknown-entity default "
                    "(authz.py:300) -> MANAGE_SETUP (same value the bare "
                    "'level' create gets via _LEAGUE_SETUP, by "
                    "coincidence). scope_axis: _setup_target_edges "
                    "kind=='league' (api/service.py:825-832): edge = "
                    "(record.program_id, None, record.id) -- Program+"
                    "League, no Season (a League is PERMANENT across "
                    "Seasons)")),
    RouteSpec("POST", r"^/api/setup/official$", "/api/setup/official",
              "post_setup_official", "_handle_setup",
              auth="session+MANAGE_SCHEDULE", scope_axis="program",
              note=("#202: authorize(role, path) -> required_permission "
                    "(authz.py:294-295, bare entity 'official') -> "
                    "MANAGE_SCHEDULE. scope_axis: _guarded_create("
                    "'official', [('club', ...)], ...) (server.py:"
                    "3988-3999, comment 'PROGRAM-AXIS (#409)') -- "
                    "ApiService._CREATE_PROGRAM_AXIS (api/service.py:2180) "
                    "-- an Official's home Club gives Program+League, but "
                    "the CREATE-side comparison is Program-only (the "
                    "record's own richer edge, below, is a mutation-side "
                    "question)")),
    RouteSpec("POST", r"^/api/setup/official/[^/]+/delete$",
              "/api/setup/official/{}/delete",
              "post_setup_official_id_delete", "_handle_setup",
              auth="session+MANAGE_SETUP", scope_axis="none",
              note=("#202: authorize(role, path) -- 'official/{id}/delete' "
                    "matches no specific authz.py rule and falls to the "
                    "generic unknown-entity default (authz.py:300) -> "
                    "MANAGE_SETUP. scope_axis=none, not applicable: the v1 "
                    "delete regex (server.py:3760-3762) deliberately "
                    "EXCLUDES 'official'/'player' -- this path matches the "
                    "separate `mmv` branch (server.py:3801-3816) instead, "
                    "which check_body()s then immediately returns a 409 "
                    "moved_to_v2 pointing at the v2 delete route. No "
                    "_guarded_mutation, no setup_target_accessible, no "
                    "scope read of any kind runs on this path -- the real "
                    "delete (and its real scope_axis) lives at "
                    "post_v2_setup_official_id_delete")),
    RouteSpec("POST", r"^/api/setup/organization$", "/api/setup/organization",
              "post_setup_organization", "_handle_setup",
              auth="session+MANAGE_ARENA", scope_axis="zero_axis",
              note=("#202: authorize(role, path) -> required_permission "
                    "(authz.py:296-297, 'organization' in _ARENA_SETUP) -> "
                    "MANAGE_ARENA. scope_axis: _guarded_create("
                    "'organization', [], ...) (server.py:3922-3929, "
                    "comment 'ZERO-AXIS ROOT (#409)') -- "
                    "ApiService._CREATE_ZERO_AXIS (api/service.py:2172) "
                    "-- no parent FK at all")),
    RouteSpec("POST", r"^/api/setup/organization/[^/]+/delete$",
              "/api/setup/organization/{}/delete",
              "post_setup_organization_id_delete", "_handle_setup",
              auth="session+MANAGE_SETUP", scope_axis="season",
              note=("#202: authorize(role, path) -- 'organization/{id}/"
                    "delete' matches no specific authz.py rule and falls "
                    "to the generic unknown-entity default (authz.py:300) "
                    "-> MANAGE_SETUP -- asymmetric with the MANAGE_ARENA "
                    "the create above requires (real, current behavior; "
                    "not reconciled here). scope_axis: _setup_target_edges "
                    "kind=='organization' (api/service.py:972-986) unions "
                    "(program, None, None) edges for every Program it "
                    "OPERATES with (program, season, None) edges for every "
                    "Venue it owns (via _venue_edges' grants) -- touches "
                    "Program and Season, never League")),
    RouteSpec("POST", r"^/api/setup/player$", "/api/setup/player",
              "post_setup_player", "_handle_setup",
              auth="session+MANAGE_SETUP", scope_axis="program",
              note=("#202: authorize(role, path) -> required_permission "
                    "(authz.py:298, 'player' in _LEAGUE_SETUP) -> "
                    "MANAGE_SETUP. scope_axis: _guarded_create('player', "
                    "[('team', ...)], ...) (server.py:4015-4023, comment "
                    "'PROGRAM-AXIS (#409)') -- ApiService."
                    "_CREATE_PROGRAM_AXIS (api/service.py:2182) -- a "
                    "Player carries its Team's edges verbatim and a Team "
                    "is PERMANENT, so only the Program is compared at "
                    "create time")),
    RouteSpec("POST", r"^/api/setup/player/[^/]+/assign-team$",
              "/api/setup/player/{}/assign-team",
              "post_setup_player_id_assign_team", "_handle_reassign",
              auth="session+MANAGE_SETUP", scope_axis="cross",
              note=("#202 repair root cause 1: the concrete leaf. "
                    "_V1_REASSIGN_SCHEMA/_V1_REASSIGN_CALL admit ONLY "
                    "(player, team) for this entity. #202 auth: "
                    "authz.py:288-290 ((division|team|player)/[^/]+/"
                    "assign-\\w+) -> MANAGE_SETUP. #202 scope_axis: TWO "
                    "_guarded_mutation targets (server.py:3501-3503, no "
                    "writable_parent leg -- ('player','team') is not in "
                    "_REASSIGN_PARENTS): the SOURCE Player (league -- "
                    "_team_edges via its Team, api/service.py:871-875, "
                    "556-623 -- Program+League) and the DESTINATION Team "
                    "(league -- api/service.py:868-869, same shape) -- "
                    "validating every axis either end touches")),
    RouteSpec("POST", r"^/api/setup/player/[^/]+/delete$",
              "/api/setup/player/{}/delete", "post_setup_player_id_delete",
              "_handle_setup",
              auth="session+MANAGE_SETUP", scope_axis="none",
              note=("#202: authorize(role, path) -- 'player/{id}/delete' "
                    "matches no specific authz.py rule and falls to the "
                    "generic unknown-entity default (authz.py:300) -> "
                    "MANAGE_SETUP. scope_axis=none, not applicable: same "
                    "as post_setup_official_id_delete -- the v1 delete "
                    "regex excludes 'player' (server.py:3760-3762), this "
                    "path matches the `mmv` 409 moved_to_v2 branch "
                    "(server.py:3801-3816) instead, and no "
                    "_guarded_mutation/setup_target_accessible call ever "
                    "runs; the real delete lives at "
                    "post_v2_setup_player_id_delete")),
    RouteSpec("POST", r"^/api/setup/rink$", "/api/setup/rink",
              "post_setup_rink", "_handle_setup",
              auth="session+MANAGE_ARENA", scope_axis="program",
              note=("#202: authorize(role, path) -> required_permission "
                    "(authz.py:296-297, 'rink' in _ARENA_SETUP) -> "
                    "MANAGE_ARENA. scope_axis: _guarded_create('rink', "
                    "[('venue', ...)], ...) (server.py:3957-3962) -- "
                    "ApiService._CREATE_PROGRAM_AXIS (api/service.py:2178) "
                    "-- only the parent Venue's Program is compared")),
    RouteSpec("POST", r"^/api/setup/rink/[^/]+/assign-venue$",
              "/api/setup/rink/{}/assign-venue",
              "post_setup_rink_id_assign_venue", "_handle_reassign",
              auth="session+MANAGE_ARENA", scope_axis="cross",
              note=("#202 repair root cause 1: the concrete leaf. "
                    "_V1_REASSIGN_SCHEMA/_V1_REASSIGN_CALL admit ONLY "
                    "(rink, venue) for this entity. #202 auth: authz.py:"
                    "285-287 ((venue|rink)/[^/]+/assign-\\w+) -> "
                    "MANAGE_ARENA. #202 scope_axis: THREE "
                    "_guarded_mutation targets (server.py:3501-3519): the "
                    "SOURCE Rink (season -- delegates to its Venue's "
                    "edges, api/service.py:958-962,728-767 -- Program+"
                    "Season, never League), the DESTINATION Venue (same "
                    "shape, season), and a THIRD 'writable_parent' check "
                    "on that Venue (setup_parent_writable, server.py:"
                    "3659-3661; _REASSIGN_PARENTS[('rink','venue')], "
                    "server.py:208) -- validating every axis either end "
                    "touches")),
    RouteSpec("POST", r"^/api/setup/rink/[^/]+/delete$",
              "/api/setup/rink/{}/delete", "post_setup_rink_id_delete",
              "_handle_setup",
              auth="session+MANAGE_SETUP", scope_axis="season",
              note=("#202: authorize(role, path) -- 'rink/{id}/delete' "
                    "matches no specific authz.py rule and falls to the "
                    "generic unknown-entity default (authz.py:300) -> "
                    "MANAGE_SETUP -- asymmetric with the MANAGE_ARENA the "
                    "create above requires (real, current behavior; not "
                    "reconciled here). scope_axis: _setup_target_edges "
                    "kind=='rink' (api/service.py:958-962) delegates to "
                    "its Venue's edges (_venue_edges, api/service.py:"
                    "728-767): Program+Season via grants, or Program-only "
                    "via the legacy link -- never League")),
    RouteSpec("POST", r"^/api/setup/scheduling-policy$",
              "/api/setup/scheduling-policy", "post_setup_scheduling_policy",
              "do_POST",
              auth="session+MANAGE_ARENA",
              scope_axis="none",
              note=("#202: generic gate -> authz.py:164-165 -> MANAGE_ARENA. server.py:2925-2932 -> `set_scheduling_policy(scope_type=..., scope_id=..., ...)` (service.py:11096-11110) takes scope_type/scope_id straight from the body with no user_id/role/scope parameter and no comparison to the caller's active context -- no scope check of any kind runs, matching its GET sibling.")),
    RouteSpec("POST", r"^/api/setup/season$", "/api/setup/season",
              "post_setup_season", "_handle_setup",
              auth="session+MANAGE_SETUP", scope_axis="program",
              note=("#202: authorize(role, path) -> required_permission "
                    "(authz.py:298, 'season' in _LEAGUE_SETUP) -> "
                    "MANAGE_SETUP. scope_axis: _guarded_create('season', "
                    "[('program', ...)], ...) (server.py:3864-3873, "
                    "comment 'PROGRAM-AXIS (#409): the Season axis is "
                    "MINTED here, not consumed') -- ApiService."
                    "_CREATE_PROGRAM_AXIS (api/service.py:2176)")),
    RouteSpec("POST",
              r"^/api/setup/season-team-registration/[^/]+/assign-division$",
              "/api/setup/season-team-registration/{}/assign-division",
              "post_setup_season_team_registration_id_assign_division",
              "_handle_setup",
              auth="session+MANAGE_SETUP", scope_axis="cross",
              note=("#202: authorize(role, path) -- the full path matches "
                    "none of authz.py's specific rules (279-290 name "
                    "specific entity words, not 'season-team-"
                    "registration') and falls to the generic '/api/setup/' "
                    "unknown-entity default (authz.py:300) -> MANAGE_SETUP "
                    "(matches the comment at server.py:3689-3692, 'All "
                    "League-Admin (MANAGE_SETUP) via the /api/setup/ authz "
                    "catch-all'). scope_axis: _guarded_mutation targets "
                    "the 'registration' bridge (server.py:3736-3742), "
                    "which carries no Program of its own and is judged by "
                    "its LeagueSeason parent (_SETUP_BRIDGE_TARGETS"
                    "['registration'], api/service.py:413-420) -- edge = "
                    "(program, season, league) via _setup_target_edges "
                    "kind=='league_season' (api/service.py:834-860). The "
                    "body's own 'division_id' destination is NOT a second "
                    "_guarded_mutation target here (only the registration "
                    "itself is, per server.py:3721-3742) but the "
                    "registration's own edge already spans all three "
                    "axes")),
    RouteSpec("POST", r"^/api/setup/season-team-registration/[^/]+/remove$",
              "/api/setup/season-team-registration/{}/remove",
              "post_setup_season_team_registration_id_remove",
              "_handle_setup",
              auth="session+MANAGE_SETUP", scope_axis="cross",
              note=("#202: same generic-fallback auth as .../"
                    "assign-division above (authz.py:300) -> MANAGE_SETUP. "
                    "scope_axis: same bridge resolution -- "
                    "_guarded_mutation targets the 'registration' "
                    "(server.py:3750-3754), judged by its LeagueSeason "
                    "parent -- edge = (program, season, league)")),
    RouteSpec("POST", r"^/api/setup/season/[^/]+/delete$",
              "/api/setup/season/{}/delete", "post_setup_season_id_delete",
              "_handle_setup",
              auth="session+MANAGE_SETUP", scope_axis="season",
              note=("#202: authorize(role, path) -- 'season/{id}/delete' "
                    "matches no specific authz.py rule and falls to the "
                    "generic unknown-entity default (authz.py:300) -> "
                    "MANAGE_SETUP (same value the bare 'season' create "
                    "gets via _LEAGUE_SETUP, authz.py:298, by "
                    "coincidence). scope_axis: _setup_target_edges "
                    "kind=='season' (api/service.py:820-823): edge = "
                    "(record.program_id, record.id, None) -- Program+"
                    "Season, no League")),
    RouteSpec("POST", r"^/api/setup/seasons/[^/]+/roll-forward$",
              "/api/setup/seasons/{}/roll-forward",
              "post_setup_seasons_id_roll_forward", "_handle_setup",
              auth="session+MANAGE_SETUP", scope_axis="season",
              note=("#202: authorize(role, path) -- 'seasons/{id}/"
                    "roll-forward' matches no specific authz.py rule and "
                    "falls to the generic unknown-entity default "
                    "(authz.py:300) -> MANAGE_SETUP. scope_axis: "
                    "_guarded_create('registration', [('season', ...), "
                    "('season', from_season_id, 'program')], ...) "
                    "(server.py:3820-3846, comment 'SEASON-OWNED (#409), "
                    "and the ONE create that names TWO Seasons') -- "
                    "ApiService._CREATE_TWO_AXIS (api/service.py:2188, "
                    "'registration')")),
    RouteSpec("POST", r"^/api/setup/seasons/[^/]+/team-registrations$",
              "/api/setup/seasons/{}/team-registrations",
              "post_setup_seasons_id_team_registrations", "_handle_setup",
              auth="session+MANAGE_SETUP", scope_axis="season",
              note=("#202: authorize(role, path) -- 'seasons/{id}/team-"
                    "registrations' matches no specific authz.py rule and "
                    "falls to the generic unknown-entity default "
                    "(authz.py:300) -> MANAGE_SETUP. scope_axis: "
                    "_guarded_create('registration', [('season', ...), "
                    "('team', ...), ('division', ...)], ...) (server.py:"
                    "3696-3720, comment 'SEASON-OWNED (#409)') -- "
                    "ApiService._CREATE_TWO_AXIS (api/service.py:2188)")),
    RouteSpec("POST", r"^/api/setup/team$", "/api/setup/team",
              "post_setup_team", "_handle_setup",
              auth="session+MANAGE_SETUP", scope_axis="program",
              note=("#202: authorize(role, path) -> required_permission "
                    "(authz.py:298, 'team' in _LEAGUE_SETUP) -> "
                    "MANAGE_SETUP. scope_axis: _guarded_create('team', "
                    "[('program', ...), ('club', ...), ('division', ...)], "
                    "...) (server.py:3905-3921, comment 'PROGRAM-AXIS "
                    "(#409)') -- ApiService._CREATE_PROGRAM_AXIS "
                    "(api/service.py:2181)")),
    RouteSpec("POST", r"^/api/setup/team/[^/]+/assign-club$",
              "/api/setup/team/{}/assign-club",
              "post_setup_team_id_assign_club", "_handle_reassign",
              auth="session+MANAGE_SETUP", scope_axis="cross",
              note=("#202 repair root cause 1: the concrete leaf. "
                    "_V1_REASSIGN_SCHEMA/_V1_REASSIGN_CALL admit ONLY "
                    "(team, club) for this entity. #202 auth: authz.py:"
                    "288-290 ((division|team|player)/[^/]+/assign-\\w+) -> "
                    "MANAGE_SETUP. #202 scope_axis: THREE "
                    "_guarded_mutation targets (server.py:3501-3519): the "
                    "SOURCE Team (league -- _team_edges, api/service.py:"
                    "868-869,556-623 -- Program+League), the DESTINATION "
                    "Club (league -- unions its Teams' edges, api/"
                    "service.py:921-930 -- same Program+League shape), and "
                    "a THIRD 'writable_parent' check on that Club "
                    "(setup_parent_writable, server.py:3659-3661; "
                    "_REASSIGN_PARENTS[('team','club')], server.py:209) -- "
                    "validating every axis either end touches")),
    RouteSpec("POST", r"^/api/setup/team/[^/]+/delete$",
              "/api/setup/team/{}/delete", "post_setup_team_id_delete",
              "_handle_setup",
              auth="session+MANAGE_SETUP", scope_axis="league",
              note=("#202: authorize(role, path) -- 'team/{id}/delete' "
                    "matches no specific authz.py rule and falls to the "
                    "generic unknown-entity default (authz.py:300) -> "
                    "MANAGE_SETUP (same value the bare 'team' create gets "
                    "via _LEAGUE_SETUP, authz.py:298, by coincidence). "
                    "scope_axis: _setup_target_edges kind=='team' "
                    "(api/service.py:868-869) -> _team_edges "
                    "(api/service.py:556-623): edge = (program, None, "
                    "league_id) -- Program+League, no Season (a Team is "
                    "PERMANENT; its Season lives on its registration)")),
    RouteSpec("POST", r"^/api/setup/venue$", "/api/setup/venue",
              "post_setup_venue", "_handle_setup",
              auth="session+MANAGE_ARENA", scope_axis="program",
              note=("#202: authorize(role, path) -> required_permission "
                    "(authz.py:296-297, 'venue' in _ARENA_SETUP) -> "
                    "MANAGE_ARENA. scope_axis: _guarded_create('venue', "
                    "[('organization', ...), ('program', ...)], ...) "
                    "(server.py:3930-3956) -- ApiService."
                    "_CREATE_PROGRAM_AXIS (api/service.py:2177) -- both "
                    "caller-supplied parents (organization_id and the "
                    "legacy league_id/Program link) are compared against "
                    "the same Program axis, not two different axes")),
    RouteSpec("POST", r"^/api/setup/venue/[^/]+/assign-organization$",
              "/api/setup/venue/{}/assign-organization",
              "post_setup_venue_id_assign_organization", "_handle_reassign",
              auth="session+MANAGE_ARENA", scope_axis="cross",
              note=("#202 repair root cause 1: the concrete leaf. "
                    "_V1_REASSIGN_SCHEMA/_V1_REASSIGN_CALL admit ONLY "
                    "(venue, organization) for this entity. #202 auth: "
                    "authz.py:285-287 ((venue|rink)/[^/]+/assign-\\w+) -> "
                    "MANAGE_ARENA (the venue|rink group; contrast the "
                    "league->organization move above, which authz.py's "
                    "279-280 override pulls into MANAGE_SETUP instead). "
                    "#202 scope_axis: THREE _guarded_mutation targets "
                    "(server.py:3501-3519): the SOURCE Venue (season -- "
                    "_venue_edges, api/service.py:955-956,728-767 -- "
                    "Program+Season via grants or Program-only via the "
                    "legacy link, never League), the DESTINATION "
                    "Organization (season -- api/service.py:972-986, same "
                    "Program+Season shape), and a THIRD 'writable_parent' "
                    "check on that Organization (setup_parent_writable, "
                    "server.py:3659-3661; _REASSIGN_PARENTS[('venue', "
                    "'organization')], server.py:207) -- validating every "
                    "axis either end touches")),
    RouteSpec("POST", r"^/api/setup/venue/[^/]+/delete$",
              "/api/setup/venue/{}/delete", "post_setup_venue_id_delete",
              "_handle_setup",
              auth="session+MANAGE_SETUP", scope_axis="season",
              note=("#202: authorize(role, path) -- 'venue/{id}/delete' "
                    "matches no specific authz.py rule and falls to the "
                    "generic unknown-entity default (authz.py:300) -> "
                    "MANAGE_SETUP -- asymmetric with the MANAGE_ARENA the "
                    "create above requires (real, current behavior; not "
                    "reconciled here). scope_axis: _setup_target_edges "
                    "kind=='venue' (api/service.py:955-956) -> "
                    "_venue_edges (api/service.py:728-767): Program+Season "
                    "via grants, or Program-only via the legacy link -- "
                    "never League")),
    RouteSpec("POST", r"^/api/v2/setup/club$", "/api/v2/setup/club",
              "post_v2_setup_club", "_handle_setup_v2",
              auth="session+MANAGE_SETUP",
              scope_axis='zero_axis',
              note=("#202: Permission.MANAGE_SETUP (authz.py:88-89, 'club' in _V2_SETUP_SETUP) -- League Admin only. _guarded_create('club', [], ...) (server.py:4531-4537, 'ZERO-AXIS ROOT (#409)'); service.py:2174")),
    RouteSpec("POST", r"^/api/v2/setup/club/[^/]+/delete$",
              "/api/v2/setup/club/{}/delete", "post_v2_setup_club_id_delete",
              "_handle_setup_v2",
              auth="session+MANAGE_SETUP",
              scope_axis='program',
              note=("#202: Permission.MANAGE_SETUP (authz.py:74-82 default) -- League Admin only. _guarded_mutation([('club', id)], ...) (server.py:4446-4482); service.py:1860-1862 club in _PROGRAM_AXIS_TARGET_KINDS")),
    RouteSpec("POST", r"^/api/v2/setup/division$", "/api/v2/setup/division",
              "post_v2_setup_division", "_handle_setup_v2",
              auth="session+MANAGE_SETUP",
              scope_axis='cross',
              note=("#202: Permission.MANAGE_SETUP (authz.py:88-89, 'division' in _V2_SETUP_SETUP) -- League Admin only. _guarded_create('division', [('league', lid), ('season', sid)], ...) (server.py:4513-4530, 'SEASON-OWNED (#409)'); service.py:2186")),
    RouteSpec("POST", r"^/api/v2/setup/division/[^/]+/assign-league$",
              "/api/v2/setup/division/{}/assign-league",
              "post_v2_setup_division_id_assign_league", "_handle_reassign_v2",
              auth="session+MANAGE_SETUP",
              scope_axis='cross',
              note=("#202: Permission.MANAGE_SETUP (authz.py:33-34). targets = [('division', id), ('league', dest)] (server.py:4143-4144); service.py:1858-1859 division in _SEASON_OWNED_TARGET_KINDS -> kinds - _PROGRAM_AXIS_TARGET_KINDS is nonempty -> Program+Season both required (service.py:1961-1968) || ORIGINAL: ""#202 repair root cause 1: the concrete leaf. "
                    "_V2_REASSIGN_SCHEMA/_V2_REASSIGN_CALL admit ONLY "
                    "(division, league) for this entity")),
    RouteSpec("POST", r"^/api/v2/setup/division/[^/]+/delete$",
              "/api/v2/setup/division/{}/delete",
              "post_v2_setup_division_id_delete", "_handle_setup_v2",
              auth="session+MANAGE_SETUP",
              scope_axis='cross',
              note=("#202: Permission.MANAGE_SETUP (authz.py:74-82 default) -- League Admin only. _guarded_mutation([('division', id)], ...) (server.py:4446-4482); service.py:1858-1859 division in _SEASON_OWNED_TARGET_KINDS")),
    RouteSpec("POST", r"^/api/v2/setup/game$", "/api/v2/setup/game",
              "post_v2_setup_game", "_handle_setup_v2",
              auth="session+MANAGE_SCHEDULE",
              scope_axis='cross',
              note=("#202: Permission.MANAGE_SCHEDULE (authz.py:84-85, rest in ('game','official')) -- League Admin or Arena Manager. _guarded_create('game', [('season', sid), ('league', lid), ('division', did)], ...) (server.py:4597-4621, 'SEASON-OWNED (#409)'); service.py:2187. NOTE: an EXHIBITION game_type supplies no season_id/league_id/division_id, so for that request shape no axis-bearing parent is named and the #409 preflight (service.py:2338-2342) reads no context at all; the REGULAR shape (league_id required, server.py:4602-4604) always names an axis-bearing parent and is refused without an explicit Program+Season. Both shapes share one (method,template) leaf -- the divergence is BODY-driven, not path-driven, so it is documented here rather than split into a second RouteSpec; 'cross' is this leaf's dominant/REGULAR-path policy")),
    RouteSpec("POST", r"^/api/v2/setup/game/[^/]+/delete$",
              "/api/v2/setup/game/{}/delete", "post_v2_setup_game_id_delete",
              "_handle_setup_v2",
              auth="session+MANAGE_SCHEDULE",
              scope_axis='cross',
              note=("#202: Permission.MANAGE_SCHEDULE (authz.py:77-79, kind=='game') -- League Admin or Arena Manager. _guarded_mutation([('game', id)], ...) (server.py:4446-4482); service.py:1858-1859 game in _SEASON_OWNED_TARGET_KINDS")),
    RouteSpec("POST", r"^/api/v2/setup/ice-slot$", "/api/v2/setup/ice-slot",
              "post_v2_setup_ice_slot", "_handle_setup_v2",
              auth="session+MANAGE_ARENA",
              scope_axis='program',
              note=("#202: Permission.MANAGE_ARENA (authz.py:86-87, 'ice-slot' in _V2_ARENA_SETUP). _guarded_create('ice_slot', [('rink', rid)], ...) (server.py:4590-4596); service.py:2179")),
    RouteSpec("POST", r"^/api/v2/setup/ice-slot/[^/]+/delete$",
              "/api/v2/setup/ice-slot/{}/delete",
              "post_v2_setup_ice_slot_id_delete", "_handle_setup_v2",
              auth="session+MANAGE_ARENA",
              scope_axis='program',
              note=("#202: Permission.MANAGE_ARENA (authz.py:74-81, kind 'ice-slot' folds to 'ice_slot', in _V2_ARENA_SETUP). _guarded_mutation([('ice_slot', id)], ...) (server.py:4446-4482); service.py:1860-1862 ice_slot in _PROGRAM_AXIS_TARGET_KINDS")),
    RouteSpec("POST", r"^/api/v2/setup/league$", "/api/v2/setup/league",
              "post_v2_setup_league", "_handle_setup_v2",
              auth="session+MANAGE_SETUP",
              scope_axis='cross',
              note=("#202: Permission.MANAGE_SETUP (authz.py:88-89, 'league' in _V2_SETUP_SETUP) -- League Admin only. _guarded_create('league', [('season', sid)], ...) (server.py:4502-4512, 'SEASON-OWNED (#409): the create also mints the LeagueSeason'); service.py:2184 _CREATE_TWO_AXIS -- both Program AND Season required (contrast the MUTATION table, service.py:1860-1862, where an existing League is PROGRAM-AXIS only -- create and delete of the SAME kind carry different axis classes here, by design)")),
    RouteSpec("POST", r"^/api/v2/setup/league-season/[^/]+/delete$",
              "/api/v2/setup/league-season/{}/delete",
              "post_v2_setup_league_season_id_delete", "_handle_setup_v2",
              auth="session+MANAGE_SETUP",
              scope_axis='cross',
              note=("#202: Permission.MANAGE_SETUP (authz.py:74-82 default, kind 'league-season' folds to 'league_season') -- League Admin only. _guarded_mutation([('league_season', id)], ...) (server.py:4446-4482); service.py:1858-1859 league_season in _SEASON_OWNED_TARGET_KINDS")),
    RouteSpec("POST", r"^/api/v2/setup/league/[^/]+/delete$",
              "/api/v2/setup/league/{}/delete",
              "post_v2_setup_league_id_delete", "_handle_setup_v2",
              auth="session+MANAGE_SETUP",
              scope_axis='program',
              note=("#202: Permission.MANAGE_SETUP (authz.py:74-82 default) -- League Admin only. _guarded_mutation([('league', id)], ...) (server.py:4446-4482); service.py:1860-1862 league in _PROGRAM_AXIS_TARGET_KINDS for MUTATION (contrast the CREATE table, service.py:2184, where creating a League is two-axis because it also mints a LeagueSeason)")),
    RouteSpec("POST", r"^/api/v2/setup/official$", "/api/v2/setup/official",
              "post_v2_setup_official", "_handle_setup_v2",
              auth="session+MANAGE_SCHEDULE",
              scope_axis='program',
              note=("#202: Permission.MANAGE_SCHEDULE (authz.py:84-85, rest in ('game','official')) -- League Admin or Arena Manager. _guarded_create('official', [('club', cid)], ...) (server.py:4622-4628, 'PROGRAM-AXIS (#409)'); service.py:2180")),
    RouteSpec("POST", r"^/api/v2/setup/official/[^/]+/delete$",
              "/api/v2/setup/official/{}/delete",
              "post_v2_setup_official_id_delete", "_handle_setup_v2",
              auth="session+MANAGE_SETUP",
              scope_axis='program',
              note=("#202: Permission.MANAGE_SETUP (authz.py:74-82, 'official' not in _V2_ARENA_SETUP -> default) -- League Admin only (contrast official CREATE, MANAGE_SCHEDULE at authz.py:84-85 -- create and delete of the SAME kind carry different PERMISSIONS here, by design). _guarded_mutation([('official', id)], ...) (server.py:4446-4482); service.py:1860-1862 official in _PROGRAM_AXIS_TARGET_KINDS")),
    RouteSpec("POST", r"^/api/v2/setup/organization$",
              "/api/v2/setup/organization", "post_v2_setup_organization",
              "_handle_setup_v2",
              auth="session+MANAGE_ARENA",
              scope_axis='zero_axis',
              note=("#202: Permission.MANAGE_ARENA (authz.py:86-87, 'organization' in _V2_ARENA_SETUP) -- League Admin or Arena Manager (roles.py:37-45). _guarded_create('organization', [], ...) (server.py:4563-4569, 'ZERO-AXIS ROOT (#409)'); service.py:2172")),
    RouteSpec("POST", r"^/api/v2/setup/organization/[^/]+/delete$",
              "/api/v2/setup/organization/{}/delete",
              "post_v2_setup_organization_id_delete", "_handle_setup_v2",
              auth="session+MANAGE_ARENA",
              scope_axis='program',
              note=("#202: Permission.MANAGE_ARENA (authz.py:74-81, kind in _V2_ARENA_SETUP). _guarded_mutation([('organization', id)], ...) (server.py:4446-4482); service.py:1860-1862 organization in _PROGRAM_AXIS_TARGET_KINDS")),
    RouteSpec("POST", r"^/api/v2/setup/player$", "/api/v2/setup/player",
              "post_v2_setup_player", "_handle_setup_v2",
              auth="session+MANAGE_SETUP",
              scope_axis='program',
              note=("#202: Permission.MANAGE_SETUP (authz.py:88-89, 'player' in _V2_SETUP_SETUP) -- League Admin only. _guarded_create('player', [('team', tid)], ...) (server.py:4629-4649, 'PROGRAM-AXIS (#409)'); service.py:2182")),
    RouteSpec("POST", r"^/api/v2/setup/player/[^/]+/active$",
              "/api/v2/setup/player/{}/active",
              "post_v2_setup_player_id_active", "_handle_setup_v2",
              auth="session+MANAGE_SETUP",
              scope_axis='program',
              note=("#202: Permission.MANAGE_SETUP (authz.py:90, catch-all default -- same as /update, 'player/<id>/active' matches no dedicated regex) -- League Admin only. _guarded_mutation([('player', id)], ...) (server.py:4245-4260); service.py:1860-1862")),
    RouteSpec("POST", r"^/api/v2/setup/player/[^/]+/assign-team$",
              "/api/v2/setup/player/{}/assign-team",
              "post_v2_setup_player_id_assign_team", "_handle_reassign_v2",
              auth="session+MANAGE_SETUP",
              scope_axis='program',
              note=("#202: Permission.MANAGE_SETUP (authz.py:33-34). targets = [('player', id), ('team', dest)] (server.py:4156-4157); service.py:1860-1862 player AND team both in _PROGRAM_AXIS_TARGET_KINDS || ORIGINAL: ""#202 repair root cause 1: the concrete leaf. "
                    "_V2_REASSIGN_SCHEMA/_V2_REASSIGN_CALL admit ONLY "
                    "(player, team) for this entity")),
    RouteSpec("POST", r"^/api/v2/setup/player/[^/]+/delete$",
              "/api/v2/setup/player/{}/delete",
              "post_v2_setup_player_id_delete", "_handle_setup_v2",
              auth="session+MANAGE_SETUP",
              scope_axis='program',
              note=("#202: Permission.MANAGE_SETUP (authz.py:74-82 default) -- League Admin only. _guarded_mutation([('player', id)], ...) (server.py:4446-4482); service.py:1860-1862 player in _PROGRAM_AXIS_TARGET_KINDS")),
    RouteSpec("POST", r"^/api/v2/setup/player/[^/]+/update$",
              "/api/v2/setup/player/{}/update",
              "post_v2_setup_player_id_update", "_handle_setup_v2",
              auth="session+MANAGE_SETUP",
              scope_axis='program',
              note=("#202: Permission.MANAGE_SETUP (authz.py:90, catch-all default -- no dedicated regex in _v2_setup_permission matches 'player/<id>/update') -- League Admin only. _guarded_mutation([('player', id)], ...) (server.py:4222-4239); service.py:1860-1862 player in _PROGRAM_AXIS_TARGET_KINDS")),
    RouteSpec("POST", r"^/api/v2/setup/program$", "/api/v2/setup/program",
              "post_v2_setup_program", "_handle_setup_v2",
              auth="session+MANAGE_SETUP",
              scope_axis='zero_axis',
              note=("#202: Permission.MANAGE_SETUP (authz.py:88-89, 'program' in _V2_SETUP_SETUP) -- League Admin only (roles.py:37-42). _guarded_create('program', [], ...) (server.py:4485-4493, 'ZERO-AXIS ROOT (#409)'); service.py:2172 _CREATE_CONSUMED_AXES['program'] = _CREATE_ZERO_AXIS -- no parent, no context read at all (service.py:2338-2342)")),
    RouteSpec("POST", r"^/api/v2/setup/program/[^/]+/assign-organization$",
              "/api/v2/setup/program/{}/assign-organization",
              "post_v2_setup_program_id_assign_organization",
              "_handle_reassign_v2",
              auth="session+MANAGE_SETUP",
              scope_axis='program',
              note=("#202: Permission.MANAGE_SETUP (authz.py:33-34, (program|division|team|player)/[^/]+/assign-\\w+) -- League Admin only. targets = [('program', id), ('organization', dest)] (server.py:4098-4114, 4140-4141); service.py:1860-1862 program AND organization both in _PROGRAM_AXIS_TARGET_KINDS || ORIGINAL: ""#202 repair root cause 1: the concrete leaf. "
                    "_V2_REASSIGN_SCHEMA/_V2_REASSIGN_CALL admit ONLY "
                    "(program, organization) for this entity")),
    RouteSpec("POST", r"^/api/v2/setup/program/[^/]+/delete$",
              "/api/v2/setup/program/{}/delete",
              "post_v2_setup_program_id_delete", "_handle_setup_v2",
              auth="session+MANAGE_SETUP",
              scope_axis='program',
              note=("#202: Permission.MANAGE_SETUP (authz.py:74-82, kind not in _V2_ARENA_SETUP nor 'game' -> default) -- League Admin only. _guarded_mutation([('program', id)], ...) (server.py:4446-4482); service.py:1860-1862 program in _PROGRAM_AXIS_TARGET_KINDS")),
    RouteSpec("POST", r"^/api/v2/setup/rink$", "/api/v2/setup/rink",
              "post_v2_setup_rink", "_handle_setup_v2",
              auth="session+MANAGE_ARENA",
              scope_axis='program',
              note=("#202: Permission.MANAGE_ARENA (authz.py:86-87, 'rink' in _V2_ARENA_SETUP). _guarded_create('rink', [('venue', vid)], ...) (server.py:4584-4589); service.py:2178")),
    RouteSpec("POST", r"^/api/v2/setup/rink/[^/]+/assign-venue$",
              "/api/v2/setup/rink/{}/assign-venue",
              "post_v2_setup_rink_id_assign_venue", "_handle_reassign_v2",
              auth="session+MANAGE_ARENA",
              scope_axis='program',
              note=("#202: Permission.MANAGE_ARENA (authz.py:31-32, (venue|rink)/[^/]+/assign-\\w+). targets = [('rink', id), ('venue', dest)] (server.py:4158-4159) (+ writable_parent venue check via _REASSIGN_PARENTS, server.py:208); service.py:1860-1862 rink AND venue both in _PROGRAM_AXIS_TARGET_KINDS || ORIGINAL: ""#202 repair root cause 1: the concrete leaf. "
                    "_V2_REASSIGN_SCHEMA/_V2_REASSIGN_CALL admit ONLY "
                    "(rink, venue) for this entity")),
    RouteSpec("POST", r"^/api/v2/setup/rink/[^/]+/delete$",
              "/api/v2/setup/rink/{}/delete", "post_v2_setup_rink_id_delete",
              "_handle_setup_v2",
              auth="session+MANAGE_ARENA",
              scope_axis='program',
              note=("#202: Permission.MANAGE_ARENA (authz.py:74-81, kind in _V2_ARENA_SETUP). _guarded_mutation([('rink', id)], ...) (server.py:4446-4482); service.py:1860-1862 rink in _PROGRAM_AXIS_TARGET_KINDS")),
    RouteSpec("POST", r"^/api/v2/setup/season$", "/api/v2/setup/season",
              "post_v2_setup_season", "_handle_setup_v2",
              auth="session+MANAGE_SETUP",
              scope_axis='program',
              note=("#202: Permission.MANAGE_SETUP (authz.py:88-89, 'season' in _V2_SETUP_SETUP) -- League Admin only. _guarded_create('season', [('program', pid)], ...) (server.py:4494-4501, 'PROGRAM-AXIS (#409): the Season axis is MINTED, not consumed'); service.py:2176 _CREATE_PROGRAM_AXIS")),
    RouteSpec("POST",
              r"^/api/v2/setup/season-team-registration/[^/]+/assign-division$",
              "/api/v2/setup/season-team-registration/{}/assign-division",
              "post_v2_setup_season_team_registration_id_assign_division",
              "_handle_setup_v2",
              auth="session+MANAGE_SETUP",
              scope_axis='cross',
              note=("#202: Permission.MANAGE_SETUP (authz.py:37-39). _guarded_mutation([('registration', id), ('division', did)], ...) (server.py:4303-4324); service.py:413-416, 1858-1859")),
    RouteSpec("POST",
              r"^/api/v2/setup/season-team-registration/[^/]+/assign-league$",
              "/api/v2/setup/season-team-registration/{}/assign-league",
              "post_v2_setup_season_team_registration_id_assign_league",
              "_handle_setup_v2",
              auth="session+MANAGE_SETUP",
              scope_axis='cross',
              note=("#202: Permission.MANAGE_SETUP (authz.py:37-39). _guarded_mutation([('registration', id), ('league', lid)], ...) (server.py:4286-4302); service.py:413-416 'registration' bridges to 'league_season'; service.py:1858-1859 league_season in _SEASON_OWNED_TARGET_KINDS")),
    RouteSpec("POST",
              r"^/api/v2/setup/season-team-registration/[^/]+/delete$",
              "/api/v2/setup/season-team-registration/{}/delete",
              "post_v2_setup_season_team_registration_id_delete",
              "_handle_setup_v2",
              auth="session+MANAGE_SETUP",
              scope_axis='cross',
              note=("#202: Permission.MANAGE_SETUP (authz.py:74-82 generic delete catch-all, kind='season-team-registration', not in _V2_ARENA_SETUP nor 'game'). _guarded_mutation([('registration', id)], ...) (server.py:4339-4351); service.py:413-416, 1858-1859")),
    RouteSpec("POST",
              r"^/api/v2/setup/season-team-registration/[^/]+/remove$",
              "/api/v2/setup/season-team-registration/{}/remove",
              "post_v2_setup_season_team_registration_id_remove",
              "_handle_setup_v2",
              auth="session+MANAGE_SETUP",
              scope_axis='cross',
              note=("#202: Permission.MANAGE_SETUP (authz.py:37-39). _guarded_mutation([('registration', id)], ...) (server.py:4325-4335); service.py:413-416, 1858-1859")),
    RouteSpec("POST", r"^/api/v2/setup/season-venue-access/[^/]+/delete$",
              "/api/v2/setup/season-venue-access/{}/delete",
              "post_v2_setup_season_venue_access_id_delete",
              "_handle_setup_v2",
              auth="session+MANAGE_SETUP",
              scope_axis='cross',
              note=("#202: Permission.MANAGE_SETUP (authz.py:49-50). _guarded_mutation([('season_venue_access', id)], ...) (server.py:4400-4412); service.py:417-419, 1858-1859")),
    RouteSpec("POST", r"^/api/v2/setup/season-venue-access/[^/]+/remove$",
              "/api/v2/setup/season-venue-access/{}/remove",
              "post_v2_setup_season_venue_access_id_remove",
              "_handle_setup_v2",
              auth="session+MANAGE_SETUP",
              scope_axis='cross',
              note=("#202: Permission.MANAGE_SETUP (authz.py:49-50). _guarded_mutation([('season_venue_access', id)], ...) (server.py:4386-4399); service.py:417-419 'season_venue_access' bridges to 'season'; service.py:1858-1859")),
    RouteSpec("POST", r"^/api/v2/setup/season/[^/]+/delete$",
              "/api/v2/setup/season/{}/delete",
              "post_v2_setup_season_id_delete", "_handle_setup_v2",
              auth="session+MANAGE_SETUP",
              scope_axis='cross',
              note=("#202: Permission.MANAGE_SETUP (authz.py:74-82 default) -- League Admin only. _guarded_mutation([('season', id)], ...) (server.py:4446-4482); service.py:1858-1859 season in _SEASON_OWNED_TARGET_KINDS -> both Program AND Season required (service.py:1899-1972 _mutation_context_error)")),
    RouteSpec("POST", r"^/api/v2/setup/seasons/copy-forward/commit$",
              "/api/v2/setup/seasons/copy-forward/commit",
              "post_v2_setup_seasons_copy_forward_commit", "_handle_setup_v2",
              auth="session+MANAGE_SETUP",
              scope_axis='program',
              note=("#159: Permission.MANAGE_SETUP (authz.py new "
                    "seasons/copy-forward/(preview|commit) rule, mirroring "
                    "the roll-forward/archive/reopen rule just above it). "
                    "_guarded_create('season', [('program', pid), "
                    "('season', source_season_id, 'program')], ...) "
                    "(server.py:5163-5191); service.py:2369 season in "
                    "_CREATE_PROGRAM_AXIS -- SAME kind create_season itself "
                    "mints (server.py:5291), reused here because the write "
                    "target is a Season this call MINTS, not one it "
                    "consumes, exactly like create_season. source_season_id "
                    "is READ (its registrations are carried forward), "
                    "narrowed to the 'program' rule per the roll-forward "
                    "precedent just above -- see setup_service.py:3050 "
                    "_resolve_copy_forward_plan / 3266 "
                    "commit_new_season_copy_forward for the full contract, "
                    "including the fingerprint-preview-binding gate "
                    "(mirrors preview_ice_availability/"
                    "commit_ice_availability, #158)")),
    RouteSpec("POST", r"^/api/v2/setup/seasons/copy-forward/preview$",
              "/api/v2/setup/seasons/copy-forward/preview",
              "post_v2_setup_seasons_copy_forward_preview",
              "_handle_setup_v2",
              auth="session+MANAGE_SETUP",
              scope_axis='program',
              note=("#159: Permission.MANAGE_SETUP (authz.py new "
                    "seasons/copy-forward/(preview|commit) rule). "
                    "_guarded_create('season', [('program', pid), "
                    "('season', source_season_id, 'program')], ...) "
                    "(server.py:5163-5191) -- SAME targets/kind as the "
                    "commit entry just above (one _guarded_create call "
                    "ternary-selects the mutation callable on "
                    "mcf.group(1), server.py:5179-5191); service.py:2369 "
                    "season in _CREATE_PROGRAM_AXIS. Side-effect-free aside "
                    "from a server-attributed preview audit row on success "
                    "(setup_service.py:3234 preview_new_season_copy_"
                    "forward) -- mirrors preview_ice_availability's own "
                    "identical role (#158)")),
    RouteSpec("POST", r"^/api/v2/setup/seasons/[^/]+/archive$",
              "/api/v2/setup/seasons/{}/archive",
              "post_v2_setup_seasons_id_archive", "_handle_setup_v2",
              auth="session+MANAGE_SETUP",
              scope_axis='cross',
              note=("#202: Permission.MANAGE_SETUP (authz.py:42-44). _guarded_mutation([('season', id)], ...) (server.py:4426-4445); service.py:1858-1859 season in _SEASON_OWNED_TARGET_KINDS")),
    RouteSpec("POST", r"^/api/v2/setup/seasons/[^/]+/reopen$",
              "/api/v2/setup/seasons/{}/reopen",
              "post_v2_setup_seasons_id_reopen", "_handle_setup_v2",
              auth="session+MANAGE_SETUP",
              scope_axis='cross',
              note=("#202: Permission.MANAGE_SETUP (authz.py:42-44). _guarded_mutation([('season', id)], ...) (server.py:4426-4445); service.py:1858-1859")),
    RouteSpec("POST", r"^/api/v2/setup/seasons/[^/]+/roll-forward$",
              "/api/v2/setup/seasons/{}/roll-forward",
              "post_v2_setup_seasons_id_roll_forward", "_handle_setup_v2",
              auth="session+MANAGE_SETUP",
              scope_axis='cross',
              note=("#202: Permission.MANAGE_SETUP (authz.py:42-44). _guarded_create('registration', [('season', to_id), ('season', from_id, 'program')], ...) (server.py:4413-4425); service.py:2189 registration in _CREATE_TWO_AXIS (the from_season_id parent is narrowed to a Program-only comparison per its 'program' rule tag, but the create's own axis class stays two-axis)")),
    RouteSpec("POST", r"^/api/v2/setup/seasons/[^/]+/team-registrations$",
              "/api/v2/setup/seasons/{}/team-registrations",
              "post_v2_setup_seasons_id_team_registrations",
              "_handle_setup_v2",
              auth="session+MANAGE_SETUP",
              scope_axis='cross',
              note=("#202: Permission.MANAGE_SETUP (authz.py:42-44, 'seasons/<id>/team-registrations') -- League Admin only. _guarded_create('registration', [('season', sid), ('team', tid), ('league', lid), ('division', did)], ...) (server.py:4263-4285); service.py:2188 registration in _CREATE_TWO_AXIS")),
    RouteSpec("POST", r"^/api/v2/setup/seasons/[^/]+/venue-access$",
              "/api/v2/setup/seasons/{}/venue-access",
              "post_v2_setup_seasons_id_venue_access", "_handle_setup_v2",
              auth="session+MANAGE_SETUP",
              scope_axis='cross',
              note=("#202: Permission.MANAGE_SETUP (authz.py:51-52, 'seasons/<id>/venue-access'). _guarded_mutation([('season', id), ('venue', vid, 'grantable')], ...) (server.py:4352-4385); service.py:1858-1859 season in _SEASON_OWNED_TARGET_KINDS so the #409 preflight requires Program+Season. The Venue END is checked under the narrower 'grantable' facility-tree exception (service.py:1637-1678), not the generic ceiling -- a per-target RULE, not the mutation's overall axis class")),
    RouteSpec("POST", r"^/api/v2/setup/team$", "/api/v2/setup/team",
              "post_v2_setup_team", "_handle_setup_v2",
              auth="session+MANAGE_SETUP",
              scope_axis='program',
              note=("#202: Permission.MANAGE_SETUP (authz.py:88-89, 'team' in _V2_SETUP_SETUP) -- League Admin only. NOT a _guarded_create: server.py:4538-4562 routes through _guarded_mutation([('league', league_id, 'active_program_league')], ...) instead -- #367's create-side League rule, 'the supplied League must belong to the caller's ACTIVE Program'. 'league' as a MUTATION target is in _PROGRAM_AXIS_TARGET_KINDS (service.py:1860-1862); the narrower predicate is service.py:1079 setup_league_in_active_program, dispatched at service.py:1678")),
    RouteSpec("POST", r"^/api/v2/setup/team/[^/]+/assign-club$",
              "/api/v2/setup/team/{}/assign-club",
              "post_v2_setup_team_id_assign_club", "_handle_reassign_v2",
              auth="session+MANAGE_SETUP",
              scope_axis='program',
              note=("#202: Permission.MANAGE_SETUP (authz.py:33-34). targets = [('team', id), ('club', dest)] (server.py:4147-4148); service.py:1860-1862 team AND club both in _PROGRAM_AXIS_TARGET_KINDS || ORIGINAL: ""#202 repair root cause 1: one of TWO concrete "
                    "leaves for this entity (see assign-league below) -- "
                    "_V2_REASSIGN_SCHEMA/_V2_REASSIGN_CALL admit BOTH "
                    "(team, club) and (team, league); a wildcard target "
                    "silently conflated both into one over-broad spec")),
    RouteSpec("POST", r"^/api/v2/setup/team/[^/]+/assign-league$",
              "/api/v2/setup/team/{}/assign-league",
              "post_v2_setup_team_id_assign_league", "_handle_reassign_v2",
              auth="session+MANAGE_SETUP",
              scope_axis='program',
              note=("#202: Permission.MANAGE_SETUP (authz.py:33-34). targets = [('team', id), ('league', dest)] (server.py:4153-4155), both in _PROGRAM_AXIS_TARGET_KINDS (service.py:1860-1862) so the #409 HTTP-boundary preflight requires only an explicit Program. NOTE the documented mid-transaction exception (server.py:4149-4155, service.py:1327-1361 _season_axis_guard): transfer_team_to_league's OWN transaction may discover, under its row locks, ACTIVE SeasonTeamRegistrations to rewrite, and at that instant the mutation becomes CROSS-AXIS and the two-axis rule is re-applied INSIDE the transaction. Data-dependent, not request-shape-dependent, so documented here rather than split into a second RouteSpec || ORIGINAL: ""#202 repair root cause 1: the other of the two "
                    "concrete leaves for this entity -- #283 Slice B, "
                    "promotion/relegation/transfer to a different "
                    "PERMANENT League")),
    RouteSpec("POST", r"^/api/v2/setup/team/[^/]+/delete$",
              "/api/v2/setup/team/{}/delete", "post_v2_setup_team_id_delete",
              "_handle_setup_v2",
              auth="session+MANAGE_SETUP",
              scope_axis='program',
              note=("#202: Permission.MANAGE_SETUP (authz.py:74-82 default) -- League Admin only. _guarded_mutation([('team', id)], ...) (server.py:4446-4482); service.py:1860-1862 team in _PROGRAM_AXIS_TARGET_KINDS")),
    RouteSpec("POST", r"^/api/v2/setup/venue$", "/api/v2/setup/venue",
              "post_v2_setup_venue", "_handle_setup_v2",
              auth="session+MANAGE_ARENA",
              scope_axis='program',
              note=("#202: Permission.MANAGE_ARENA (authz.py:86-87, 'venue' in _V2_ARENA_SETUP). _guarded_create('venue', [('organization', oid)], ...) (server.py:4570-4583, 'PROGRAM-AXIS for #409'); service.py:2177")),
    RouteSpec("POST", r"^/api/v2/setup/venue/[^/]+/assign-organization$",
              "/api/v2/setup/venue/{}/assign-organization",
              "post_v2_setup_venue_id_assign_organization",
              "_handle_reassign_v2",
              auth="session+MANAGE_ARENA",
              scope_axis='program',
              note=("#202: Permission.MANAGE_ARENA (authz.py:31-32). targets = [('venue', id), ('organization', dest)] (server.py:4161-4163) (+ writable_parent organization via _REASSIGN_PARENTS, server.py:207); service.py:1860-1862 venue AND organization both in _PROGRAM_AXIS_TARGET_KINDS || ORIGINAL: ""#202 repair root cause 1: the concrete leaf. "
                    "_V2_REASSIGN_SCHEMA/_V2_REASSIGN_CALL admit ONLY "
                    "(venue, organization) for this entity")),
    RouteSpec("POST", r"^/api/v2/setup/venue/[^/]+/delete$",
              "/api/v2/setup/venue/{}/delete",
              "post_v2_setup_venue_id_delete", "_handle_setup_v2",
              auth="session+MANAGE_ARENA",
              scope_axis='program',
              note=("#202: Permission.MANAGE_ARENA (authz.py:74-81, kind in _V2_ARENA_SETUP). _guarded_mutation([('venue', id)], ...) (server.py:4446-4482); service.py:1860-1862 venue in _PROGRAM_AXIS_TARGET_KINDS")),
)

#: (method, template) -> RouteSpec.
BY_KEY = {spec.key: spec for spec in REGISTRY}
#: name -> RouteSpec.
BY_NAME = {spec.name: spec for spec in REGISTRY}
