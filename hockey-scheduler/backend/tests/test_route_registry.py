"""THE GATE (#202 step 1): the registry and the live dispatch must agree.

Two asymmetries fail CI, and each names the offending entries:

  UNCLASSIFIED  a live dispatch branch with no ``RouteSpec``
  DEAD          a ``RouteSpec`` matching no live dispatch branch

"Live" is not a second hand-written list: ``route_extract`` parses
``web/server.py`` and reports the branches the dispatch actually contains, so
adding a route without registering it fails, and registering a route that was
deleted fails. See ``test_route_extract.py`` for the extractor's own proof that
it finds every branch shape and refuses the ones it cannot read.

The exact-Season context-read fence is also registry-driven now. Its five
``RouteSpec.context_read_fence`` markers are resolved by production before
dispatch and pinned in both directions below.

The sensitive POST transport-denial audit class follows the same contract:
category and purpose live on the four matching ``RouteSpec`` rows, while the
production selector and the exhaustive tests below refuse narrowing,
widening, malformed metadata, and ambiguous matches.

The writable-parent half of ``assign-*`` authorization is registry-driven as
well. Its eight concrete v1/v2 routes carry the parent kind and request-body
field together; production and the exhaustive tests below refuse narrowing,
widening, half-filled metadata, wrong route shapes, and ambiguous matches.

The generic destination half is also registry-driven. All sixteen concrete
v1/v2 ``assign-*`` routes carry the destination kind and request-body field;
load-time validation requires complete coverage and exact agreement with the
narrower writable-parent declaration wherever both apply.

``_GET_ROUTES``/``_POST_ROUTES`` were the same kind of hand-maintained table
through the #202 routespec-inventory step; the #202 WIRING step replaced
their SOURCE with a live derivation from this registry (every concrete GET
``kind="route"`` entry, plus every POST ``kind="route"`` entry under
``/api/`` -- see server.py's own comment), so what this file
checks for them now is that the derivation still reproduces their exact
deliberate current scope and omissions (``MethodTableNarrowingTests`` below),
plus two structural invariants ``kind`` itself needs now that it is load-bearing
(``KindClassificationTests``) -- not that a hand-written list happens to agree
with the parser, which was the old question and no longer applies.
"""

import dataclasses
import re
import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.domain import Permission, SensitiveFieldCategory
from hockey_scheduler.web import authz
from hockey_scheduler.web import server as srv
from hockey_scheduler.web.route_extract import (
    SERVER_PATH, extract_walker, sample_path, templates_of_pattern,
)
from hockey_scheduler.web.route_registry import (
    BY_KEY, BY_NAME, CONTEXT_SCOPED_READ_SPECS, REGISTRY, UNCLASSIFIED,
    REASSIGNMENT_DESTINATION_SPECS, REASSIGNMENT_PARENT_SPECS,
    SENSITIVE_POST_DENIAL_SPECS,
    context_scoped_read_specs, runtime_context_read_spec,
    reassignment_destination_specs, reassignment_parent_specs,
    runtime_get_auth_spec, runtime_reassignment_destination_spec,
    runtime_reassignment_parent_spec, runtime_sensitive_post_denial_spec,
    sensitive_post_denial_specs,
)
from hockey_scheduler.web.validation import BodyError

WALKER = extract_walker()
LIVE = {route.key: route for route in WALKER.routes.values()}

_EXPECTED_RUNTIME_GET_AUTH = {
    "get_accounts": "MANAGE_USERS",
    "get_accounts_id_sessions": "MANAGE_USERS",
    "get_guardians_links": "MANAGE_USERS",
}

_EXPECTED_CONTEXT_SCOPED_READS = {
    "get_scheduler_scenarios_id": "/api/scheduler/scenarios/{}",
    "get_standings_id": "/api/standings/{}",
    "get_standings_league_season_id_id":
        "/api/standings/league-season/{}/{}",
    "get_v2_setup_seasons_id_venue_access":
        "/api/v2/setup/seasons/{}/venue-access",
    "get_v2_setup_seasons_id_venue_candidates":
        "/api/v2/setup/seasons/{}/venue-candidates",
}

_EXPECTED_SENSITIVE_POST_DENIALS = {
    "post_notifications_contacts_id_active": (
        SensitiveFieldCategory.CONTACT_DESTINATION,
        "set_contact_destination_active"),
    "post_notifications_deliveries_id_ignore": (
        SensitiveFieldCategory.CONTACT_DESTINATION,
        "ignore_notification_delivery"),
    "post_notifications_deliveries_id_retry": (
        SensitiveFieldCategory.CONTACT_DESTINATION,
        "retry_notification_delivery"),
    "post_notifications_device_tokens_id_active": (
        SensitiveFieldCategory.CONTACT_DESTINATION,
        "set_device_token_active"),
}

_EXPECTED_REASSIGNMENT_PARENTS = {
    "post_setup_league_id_assign_organization": (
        "organization", "organization_id"),
    "post_setup_rink_id_assign_venue": ("venue", "venue_id"),
    "post_setup_team_id_assign_club": ("club", "club_id"),
    "post_setup_venue_id_assign_organization": (
        "organization", "organization_id"),
    "post_v2_setup_program_id_assign_organization": (
        "organization", "operator_organization_id"),
    "post_v2_setup_rink_id_assign_venue": ("venue", "venue_id"),
    "post_v2_setup_team_id_assign_club": ("club", "club_id"),
    "post_v2_setup_venue_id_assign_organization": (
        "organization", "organization_id"),
}

_EXPECTED_REASSIGNMENT_DESTINATIONS = {
    "post_setup_division_id_assign_level": ("league", "level_id", True),
    "post_setup_league_id_assign_organization": (
        "organization", "organization_id", True),
    "post_setup_player_id_assign_team": ("team", "team_id", False),
    "post_setup_rink_id_assign_venue": ("venue", "venue_id", False),
    "post_setup_season_team_registration_id_assign_division": (
        "division", "division_id", True),
    "post_setup_team_id_assign_club": ("club", "club_id", True),
    "post_setup_venue_id_assign_organization": (
        "organization", "organization_id", True),
    "post_v2_setup_division_id_assign_league": (
        "league", "league_id", False),
    "post_v2_setup_player_id_assign_team": ("team", "team_id", False),
    "post_v2_setup_program_id_assign_organization": (
        "organization", "operator_organization_id", True),
    "post_v2_setup_rink_id_assign_venue": ("venue", "venue_id", False),
    "post_v2_setup_season_team_registration_id_assign_division": (
        "division", "division_id", True),
    "post_v2_setup_season_team_registration_id_assign_league": (
        "league", "league_id", False),
    "post_v2_setup_team_id_assign_club": ("club", "club_id", True),
    "post_v2_setup_team_id_assign_league": (
        "league", "league_id", False),
    "post_v2_setup_venue_id_assign_organization": (
        "organization", "organization_id", True),
}


def _runtime_get_auth_map(registry):
    """Resolve every concrete GET template through the production selector."""
    selected = {}
    for source in registry:
        if source.method != "GET" or source.kind != "route":
            continue
        spec = runtime_get_auth_spec(sample_path(source.template), registry)
        if spec is not None:
            selected[spec.name] = spec.runtime_permission_name
    return selected


def _runtime_context_read_map(registry):
    """Resolve every concrete GET through the production fence selector."""
    selected = {}
    for source in registry:
        if source.method != "GET" or source.kind != "route":
            continue
        spec = runtime_context_read_spec(sample_path(source.template), registry)
        if spec is not None:
            selected[spec.name] = spec.template
    return selected


def _runtime_sensitive_post_denial_map(registry):
    """Resolve every concrete POST through the production audit selector."""
    selected = {}
    for source in registry:
        if source.method != "POST" or source.kind != "route":
            continue
        spec = runtime_sensitive_post_denial_spec(
            sample_path(source.template), registry)
        if spec is not None:
            selected[spec.name] = (
                spec.transport_denial_audit_category,
                spec.transport_denial_audit_purpose)
    return selected


def _runtime_reassignment_parent_map(registry):
    """Resolve every concrete POST through the production parent selector."""
    selected = {}
    for source in registry:
        if source.method != "POST" or source.kind != "route":
            continue
        spec = runtime_reassignment_parent_spec(
            sample_path(source.template), registry)
        if spec is not None:
            selected[spec.name] = (
                spec.reassignment_parent_kind,
                spec.reassignment_parent_field)
    return selected


def _runtime_reassignment_destination_map(registry):
    """Resolve every concrete POST through the destination selector."""
    selected = {}
    for source in registry:
        if source.method != "POST" or source.kind != "route":
            continue
        spec = runtime_reassignment_destination_spec(
            sample_path(source.template), registry)
        if spec is not None:
            selected[spec.name] = (
                spec.reassignment_destination_kind,
                spec.reassignment_destination_field,
                spec.reassignment_destination_nullable)
    return selected


def _describe(keys, source):
    return "\n".join(f"  {method:5s} {template}   ({source})"
                     for method, template in sorted(keys))


class RegistryCoversTheDispatchTests(unittest.TestCase):
    maxDiff = None

    def test_no_unclassified_dispatch_branch(self):
        """Every live (method, path) the dispatch selects has a RouteSpec."""
        missing = set(LIVE) - set(BY_KEY)
        self.assertEqual(missing, set(), "\n\nUNCLASSIFIED — these dispatch "
                         "branches have no RouteSpec in route_registry.py:\n"
                         + "\n".join(
                             f"  {LIVE[key].method:5s} {LIVE[key].template}"
                             f"   ({LIVE[key].handler}:{LIVE[key].lineno}, "
                             f"{LIVE[key].shape}: {LIVE[key].test})"
                             for key in sorted(missing)))

    def test_no_dead_route_spec(self):
        """Every RouteSpec matches a branch that is still in the dispatch."""
        dead = set(BY_KEY) - set(LIVE)
        self.assertEqual(dead, set(), "\n\nDEAD — these RouteSpecs match no "
                         "live dispatch branch in web/server.py:\n"
                         + "\n".join(
                             f"  {BY_KEY[key].method:5s} {BY_KEY[key].template}"
                             f"   ({BY_KEY[key].name}, declared handler "
                             f"{BY_KEY[key].handler})"
                             for key in sorted(dead)))

    def test_declared_handler_is_where_the_branch_lives(self):
        """``handler`` is verified, so it cannot rot into decoration."""
        wrong = [(spec.name, spec.handler, LIVE[spec.key].handler)
                 for spec in REGISTRY
                 if spec.key in LIVE and spec.handler != LIVE[spec.key].handler]
        self.assertEqual(wrong, [], "\n\nRouteSpec.handler disagrees with the "
                                    "dispatch (name, declared, actual)")

    def test_counts(self):
        """A visible total, so a silent halving of the inventory is not silent.

        #202 repair: 74 GET -> 75 (root cause 6, the static tail: +1) and
        163 POST -> 164 (root cause 1: -12 assign-\\w+ wildcard families +
        13 concrete combo leaves = +1).

        #159: 164 POST -> 166 (new-Season copy-forward preview/commit, two
        new literal leaves under ``seasons/copy-forward/``).
        """
        self.assertEqual(len(REGISTRY), len(LIVE))
        self.assertEqual(sum(1 for s in REGISTRY if s.method == "GET"), 75)
        self.assertEqual(sum(1 for s in REGISTRY if s.method == "POST"), 166)


class RegistryInternalConsistencyTests(unittest.TestCase):
    maxDiff = None

    def test_keys_and_names_are_unique(self):
        self.assertEqual(len(BY_KEY), len(REGISTRY))
        self.assertEqual(len(BY_NAME), len(REGISTRY))

    def test_pattern_expands_to_exactly_its_template(self):
        """The regex and the canonical template are two views of ONE route.

        Without this a spec could carry a pattern that matches something else
        entirely and still pass the template comparison above.
        """
        wrong = [(spec.name, spec.pattern, spec.template,
                  templates_of_pattern(spec.pattern))
                 for spec in REGISTRY
                 if templates_of_pattern(spec.pattern) != [spec.template]]
        self.assertEqual(wrong, [])

    def test_pattern_matches_a_sample_of_its_own_template(self):
        for spec in REGISTRY:
            with self.subTest(spec=spec.name):
                self.assertRegex(sample_path(spec.template), spec.pattern)

    # #202 repair round 6, finding 4: classification is NOT batch-by-batch
    # any more, and has not been since round 4, finding 4 landed (this
    # comment block, and the test below, used to describe the EARLIER,
    # mid-migration state -- "landing in independent, reviewed batches",
    # "cannot pin the whole registry" -- which stopped being true two
    # rounds ago and was left stale here, exactly the kind of drift this
    # finding exists to catch). The CURRENT, actual state: every REACHABLE
    # spec other than the deliberately excluded
    # ``get_empty_path`` (see below) carries a real auth/
    # scope_axis classification, gated per-route against an independently
    # re-derived expected value by ``_EXPECTED_CLASSIFICATION``'s own
    # comprehensive check further down this file (THE CLASSIFICATION
    # GATE, below) -- THAT gate is what actually proves each value is
    # CORRECT, not merely present. What THIS test still separately checks
    # -- structural invariants the comprehensive gate does not restate,
    # plus a handful of specific pins kept as a direct, cheap regression
    # anchor rather than because they are otherwise unverified:
    #   * a spec never carries HALF a classification (auth filled, scope_axis
    #     still UNCLASSIFIED, or vice versa) — that is exactly the
    #     "half-populated policy field reads as authority" failure mode the
    #     original guard existed to catch;
    #   * every filled value is one of the axis's own declared classes, not a
    #     typo or an invented one;
    #   * a handful of always-public static/calendar leaves carry exactly
    #     auth="none", scope_axis="none";
    #   * ``get_empty_path`` — the impossible fallback (unreachable over HTTP,
    #     see its note) — is deliberately EXCLUDED and stays UNCLASSIFIED.
    # Both fields are CLASSIFIED and CI-GATED for every reachable spec. The
    # exact unscoped ``session+<Permission>`` GET class is now also consumed
    # by server.py before dispatch; complex/resource-scoped labels remain in
    # their handler until a later bounded #202 slice moves their resolver.
    _KNOWN_NONE_NONE_LEAVES = frozenset({
        "get_index", "get_mobile_shell", "get_mobile_shell_slash",
        "get_setup_shell", "get_setup_shell_slash", "get_static_tail",
        "get_calendar_division_id_ics", "get_calendar_official_id_ics",
        "get_calendar_player_id_ics", "get_calendar_team_id_ics",
    })
    _VALID_SCOPE_AXES = frozenset({
        "zero_axis", "program", "season", "league", "cross", "none",
        UNCLASSIFIED,
    })

    def test_no_spec_is_half_classified_or_reverted(self):
        """Structural invariants over EVERY spec's ``auth``/``scope_axis``
        pairing, plus a handful of specific pins -- see the block comment
        above for the full accounting of what is (and, since round 4
        finding 4, is no longer) checkable here versus by
        ``_EXPECTED_CLASSIFICATION``'s own comprehensive gate further down
        this file."""
        half_filled = [(s.name, s.auth, s.scope_axis) for s in REGISTRY
                        if (s.auth != UNCLASSIFIED)
                        != (s.scope_axis != UNCLASSIFIED)]
        self.assertEqual(half_filled, [], "\n\na spec must be classified on "
                         "BOTH auth and scope_axis, or neither")

        bad_axis = [(s.name, s.scope_axis) for s in REGISTRY
                    if s.scope_axis not in self._VALID_SCOPE_AXES]
        self.assertEqual(bad_axis, [])

        by_name = {s.name: (s.auth, s.scope_axis) for s in REGISTRY}
        for name in self._KNOWN_NONE_NONE_LEAVES:
            with self.subTest(name=name):
                self.assertEqual(by_name[name], ("none", "none"))

        self.assertEqual(by_name["get_empty_path"],
                         (UNCLASSIFIED, UNCLASSIFIED),
                         "get_empty_path is an impossible fallback shape "
                         "(unreachable over HTTP) and must stay excluded "
                         "from classification, not guessed at")

    def test_the_registry_is_now_wired_not_inert(self):
        """server.py DOES import the registry now -- the #202 wiring step.

        Through the routespec-inventory step this test asserted the OPPOSITE
        (``assertNotIn``): that nothing read the registry, which is what made
        "no behaviour change" checkable rather than claimed for THAT step.
        The wiring step's whole point is to stop that being true -- RouteSpec
        is now the 405/Allow admission source (server.py's ``_GET_ROUTES``/
        ``_POST_ROUTES``) -- so what is worth pinning now is that the import
        is real and did not quietly get reverted, not that it is absent.
        """
        self.assertIs(srv.REGISTRY, REGISTRY)


# --------------------------------------------------------------------------- #
# #202 repair round 4, finding 4: THE CLASSIFICATION GATE.                     #
#                                                                              #
# Everything above this line checks that the registry covers the LIVE         #
# DISPATCH (admission). Nothing above it checks that a filled-in auth/        #
# scope_axis value is actually CORRECT, or even that it is a value this       #
# module recognises at all -- test_no_spec_is_half_classified_or_reverted     #
# (above -- named test_classification_slots_are_still_empty before round 6    #
# finding 4's own rename, once classification stopped landing batch-by-batch) #
# only pins a small set of entries by name and checks that a spec             #
# never carries HALF a classification; every other reachable spec, and any    #
# auth STRING WHATSOEVER (a typo, 'unclassified', a value                     #
# from a different axis entirely), passed silently. Reviewer-demonstrated:    #
# get_officials's auth/scope_axis can be mutated operator_only/none ->        #
# none/none, or reverted to (UNCLASSIFIED, UNCLASSIFIED) outright, and every  #
# test above -- all five of RegistryInternalConsistencyTests included --      #
# stays green.                                                                #
#                                                                              #
# _EXPECTED_CLASSIFICATION below is checked in per every reachable spec, so a #
# silent reversion or substitution on ANY of them -- not just the ten pinned  #
# names -- now fails here. It is NOT copied from RouteSpec.auth/scope_axis:   #
# that would beg exactly the question this gate exists to answer, matching   #
# always and catching nothing (a literal copy is what let the 3 finding-5     #
# mislabels AND the 37 auth bugs this round's own investigation found sit     #
# undetected through the batch classification that first populated these     #
# fields). It was built, and is re-verified below, from GROUND TRUTH          #
# independent of route_registry.py itself:                                   #
#                                                                              #
#   * every POST leaf that reaches do_POST's GENERIC gate (i.e. is not one    #
#     of the ~18 custom-early-gated routes named in test_the_post_permission_ #
#     derivation_matches_authz_ground_truth below) is checked against         #
#     ``authz.required_permission(sample_path(spec.template))`` DIRECTLY --   #
#     a pure function, called here again at test time, not a value taken on   #
#     faith from a prior run;                                                 #
#   * every handler-local GET leaf gated by ``self._operator_only(...)`` is   #
#     detected by PARSING server.py itself. The first centrally-enforced      #
#     class (exact ``session+MANAGE_USERS`` + no scope) is separately pinned  #
#     by _EXPECTED_RUNTIME_GET_AUTH and live matching/mutation tests below;   #
#   * every other leaf (custom-gated, or genuinely public) was independently  #
#     read from server.py's actual handler code (which guard function it     #
#     calls, and what THAT function's own contract is -- no cookie -> 401 vs  #
#     200, which role the scope check requires, ...), the same way the 13-   #
#     entry fix in the prior #202 round was derived, cited inline in each     #
#     RouteSpec's own ``note`` field, independently re-confirmed against the  #
#     current source while building this table (this round's own             #
#     investigation corrected 37 entries this way -- 34 ``/api/v2/setup/*``   #
#     POST leaves labelled 'operator_only' despite their OWN notes already    #
#     citing MANAGE_SETUP -- generic-gate mislabelled as the GET-only         #
#     operator_only convention -- plus 3 GET routes labelled bare 'session'   #
#     despite their own notes citing the SAME player-scope/guardian-scope     #
#     gate their POST siblings are already labelled with).                   #
#                                                                              #
# scope_axis values are carried over from the existing, individually-cited   #
# notes (re-read, not re-derived from a fresh Program/Season/League model     #
# per route -- a materially larger undertaking this round did not attempt)   #
# -- stated here plainly rather than silently claimed: the auth column is     #
# the one independently RE-DERIVED end to end; the scope_axis column is the   #
# one independently RE-VERIFIED against its own cited evidence. Both are      #
# gated identically below regardless -- a silent drift in EITHER field, by    #
# ANY of the four named mutation shapes, is caught the same way.             #
# --------------------------------------------------------------------------- #

_VALID_AUTH_VALUES = frozenset({
    "none",
    "operator_only",
    "optional_session",
    "session",
    "session+MANAGE_ARENA",
    "session+MANAGE_ROSTER",
    # #427 final blocker round 3 RETIRED "session+MANAGE_ROSTER-or-self" and
    # replaced it with this. The old label's "-or-self" named an own-team
    # comparison the leaf made ITSELF, from its own second reading of the
    # session scope, and answered a mismatching hint with a 403 -- which is
    # exactly what round 3 removed. Both leaves still require MANAGE_ROSTER
    # (a role capability: a Player and an assigned Official are refused
    # outright, unchanged), and the SIDE is now decided by the same
    # projection the rest of the family uses. Two facts, so two components:
    # dropping either half would understate the gate.
    "session+MANAGE_ROSTER+own-side-projection",
    "session+MANAGE_SCHEDULE",
    "session+MANAGE_SCHEDULE+real-account",
    "session+MANAGE_SCHEDULE-or-self",
    "session+MANAGE_SETUP",
    "session+MANAGE_USERS",
    "session+RESPOND_ASSIGNMENT-or-self",
    "session+RESPOND_AVAILABILITY",
    "session+guardian-scope",
    "session+guardian-scope+verified-link",
    # #427 blocker: the private-game gate admits the caller, then the SERVER
    # resolves their own game-scoped side and the response is PROJECTED to
    # it -- own side in full for a Coach/Player with the opponent marked
    # restricted, submitted-lineup only for an assigned official (or a 403
    # where no official-shaped projection exists), both sides in full for an
    # unscoped operator. Round 2 added the fifth and last leaf of the family,
    # the availability rollup, which is also the only one that reads a side
    # hint -- so this label now additionally means "a client-supplied side is
    # adjudicated, never trusted". A narrowing of the same shape as
    # `session+player-scope`: nobody's 403 changes, what changes is which
    # subject's private data the 200 may contain.
    "session+own-side-projection",
    "session+player-scope",
})

# (method, template) -> (auth, scope_axis) for every REACHABLE spec
# (get_empty_path -- the impossible, unreachable-over-HTTP fallback -- is
# deliberately excluded, matching RegistryInternalConsistencyTests' own
# pin above). See the module comment block immediately above for how this
# was derived and independently re-verified, not copied.
_EXPECTED_CLASSIFICATION = {
    ("GET", '/'): ('none', 'none'),  # get_index
    ("GET", '/api/accounts'): ('session+MANAGE_USERS', 'none'),  # get_accounts
    ("GET", '/api/accounts/{}/sessions'): ('session+MANAGE_USERS', 'none'),  # get_accounts_id_sessions
    ("GET", '/api/auth/accounts'): ('none', 'none'),  # get_auth_accounts
    ("GET", '/api/auth/me'): ('optional_session', 'none'),  # get_auth_me
    ("GET", '/api/auth/roles'): ('none', 'none'),  # get_auth_roles
    ("GET", '/api/bootstrap/status'): ('none', 'none'),  # get_bootstrap_status
    ("GET", '/api/calendar-feeds'): ('session+MANAGE_SCHEDULE-or-self', 'none'),  # get_calendar_feeds
    ("GET", '/api/context'): ('session', 'none'),  # get_context
    ("GET", '/api/context/options'): ('session', 'none'),  # get_context_options
    ("GET", '/api/demo/overview'): ('session', 'cross'),  # get_demo_overview
    ("GET", '/api/games/{}'): ('none', 'none'),  # get_games_id
    # #427 final blocker round 2: was bare 'session' while this leaf's inline
    # narrowing named only COACH and PLAYER, so an assigned OFFICIAL fell
    # through it -- and this is the ONE private-game leaf that reads a side
    # from the QUERY STRING, so `?team_id=` was the sole side selector for
    # that role. It is now projected by the SAME route_audience as its four
    # siblings: hint kept for an operator, IGNORED for a Coach/Player in
    # favour of the trusted side, 403 for an official.
    ("GET", '/api/games/{}/availability-summary'):
        ('session+own-side-projection', 'none'),  # get_games_id_availability_summary
    # #427 blocker: both were bare 'session' while get_board hard-coded the
    # HOME side and get_lineups returned both sides' private state to either
    # Coach. The gate is unchanged; the response is now projected to the
    # server-resolved own side.
    ("GET", '/api/games/{}/board'):
        ('session+own-side-projection', 'none'),  # get_games_id_board
    ("GET", '/api/games/{}/lineups'):
        ('session+own-side-projection', 'none'),  # get_games_id_lineups
    ("GET", '/api/games/{}/officials'): ('session', 'none'),  # get_games_id_officials
    ("GET", '/api/games/{}/reschedule'): ('session', 'none'),  # get_games_id_reschedule
    # #427 final blocker: the three flat-list siblings of the two leaves
    # above. All three were bare 'session' while /roster returned both sides'
    # seats, /substitutes both sides' substitute workflow (to officials too),
    # and /roster-status hard-coded HOME for everybody. The gate is unchanged;
    # each response is now projected on the server-resolved own side.
    ("GET", '/api/games/{}/roster'):
        ('session+own-side-projection', 'none'),  # get_games_id_roster
    ("GET", '/api/games/{}/roster-status'):
        ('session+own-side-projection', 'none'),  # get_games_id_roster_status
    # #427 final blocker round 3: the sixth and seventh leaves of the same
    # dispatch, and the last two binding a side their own way. The
    # MANAGE_ROSTER capability gate is unchanged -- what changed is that the
    # side comes from the family's one trusted resolution and a client hint
    # is IGNORED for a Coach rather than answered with a 403 that varies with
    # the side named (which contradicted the contract round 2 shipped for
    # this very family, in the same commit, naming this very route).
    ("GET", '/api/games/{}/substitute-addable'):
        ('session+MANAGE_ROSTER+own-side-projection', 'none'),  # get_games_id_substitute_addable
    ("GET", '/api/games/{}/substitute-candidates'):
        ('session+MANAGE_ROSTER+own-side-projection', 'none'),  # get_games_id_substitute_candidates
    ("GET", '/api/games/{}/substitutes'):
        ('session+own-side-projection', 'none'),  # get_games_id_substitutes
    ("GET", '/api/guardians/links'): ('session+MANAGE_USERS', 'none'),  # get_guardians_links
    ("GET", '/api/health'): ('none', 'none'),  # get_health
    ("GET", '/api/import/hierarchy-codes'): ('operator_only', 'none'),  # get_import_hierarchy_codes
    ("GET", '/api/me/assignments'): ('optional_session', 'none'),  # get_me_assignments
    ("GET", '/api/me/guardian/home'): ('session+guardian-scope', 'none'),  # get_me_guardian_home
    ("GET", '/api/me/guardian/{}/substitute-opportunities/{}'): ('session+guardian-scope+verified-link', 'none'),  # get_me_guardian_id_substitute_opportunities_id
    ("GET", '/api/me/player-home'): ('optional_session', 'none'),  # get_me_player_home
    ("GET", '/api/me/substitute-opportunities/{}'): ('session+player-scope', 'none'),  # get_me_substitute_opportunities_id
    ("GET", '/api/notifications'): ('session', 'none'),  # get_notifications
    ("GET", '/api/notifications/contacts'): ('operator_only', 'none'),  # get_notifications_contacts
    ("GET", '/api/notifications/deliveries'): ('operator_only', 'none'),  # get_notifications_deliveries
    ("GET", '/api/notifications/device-tokens'): ('operator_only', 'none'),  # get_notifications_device_tokens
    ("GET", '/api/notifications/preferences'): ('session+MANAGE_SCHEDULE-or-self', 'none'),  # get_notifications_preferences
    ("GET", '/api/officials'): ('operator_only', 'none'),  # get_officials
    ("GET", '/api/officials/{}/availability'): ('session+MANAGE_SCHEDULE-or-self', 'none'),  # get_officials_id_availability
    ("GET", '/api/onboarding/status'): ('operator_only', 'none'),  # get_onboarding_status
    ("GET", '/api/players'): ('operator_only', 'cross'),  # get_players
    ("GET", '/api/public/games/{}'): ('none', 'none'),  # get_public_games_id
    ("GET", '/api/public/schedule'): ('none', 'none'),  # get_public_schedule
    ("GET", '/api/public/standings/league-season/{}/{}'): ('none', 'none'),  # get_public_standings_league_season_id_id
    ("GET", '/api/public/standings/{}'): ('none', 'none'),  # get_public_standings_id
    ("GET", '/api/readiness'): ('none', 'none'),  # get_readiness
    ("GET", '/api/reschedule/pending'): ('operator_only', 'none'),  # get_reschedule_pending
    ("GET", '/api/scheduler/drafts'): ('operator_only', 'cross'),  # get_scheduler_drafts
    ("GET", '/api/scheduler/scenarios'): ('operator_only', 'cross'),  # get_scheduler_scenarios
    ("GET", '/api/scheduler/scenarios/{}'): ('operator_only', 'cross'),  # get_scheduler_scenarios_id
    ("GET", '/api/setup/hierarchy'): ('operator_only', 'none'),  # get_setup_hierarchy
    ("GET", '/api/setup/leagues/{}/teams'): ('operator_only', 'program'),  # get_setup_leagues_id_teams
    ("GET", '/api/setup/scheduling-policy'): ('operator_only', 'none'),  # get_setup_scheduling_policy
    ("GET", '/api/setup/seasons/{}/team-registrations'): ('operator_only', 'season'),  # get_setup_seasons_id_team_registrations
    ("GET", '/api/standings/league-season/{}/{}'): ('session', 'cross'),  # get_standings_league_season_id_id
    ("GET", '/api/standings/{}'): ('session', 'cross'),  # get_standings_id
    ("GET", '/api/status'): ('none', 'none'),  # get_status
    ("GET", '/api/v2/onboarding/status'): ('operator_only', 'none'),  # get_v2_onboarding_status
    ("GET", '/api/v2/setup/hierarchy'): ('operator_only', 'program'),  # get_v2_setup_hierarchy
    ("GET", '/api/v2/setup/overview'): ('session+MANAGE_ARENA', 'cross'),  # get_v2_setup_overview
    ("GET", '/api/v2/setup/programs/{}/teams'): ('operator_only', 'program'),  # get_v2_setup_programs_id_teams
    ("GET", '/api/v2/setup/progress'): ('session+MANAGE_ARENA', 'cross'),  # get_v2_setup_progress
    ("GET", '/api/v2/setup/seasons/{}/team-registrations'): ('operator_only', 'season'),  # get_v2_setup_seasons_id_team_registrations
    ("GET", '/api/v2/setup/seasons/{}/venue-access'): ('operator_only', 'season'),  # get_v2_setup_seasons_id_venue_access
    ("GET", '/api/v2/setup/seasons/{}/venue-candidates'): ('operator_only', 'season'),  # get_v2_setup_seasons_id_venue_candidates
    ("GET", '/api/{*0}'): ('none', 'none'),  # get_api_unmatched
    ("GET", '/calendar/division/{}.ics'): ('none', 'none'),  # get_calendar_division_id_ics
    ("GET", '/calendar/official/{}.ics'): ('none', 'none'),  # get_calendar_official_id_ics
    ("GET", '/calendar/player/{}.ics'): ('none', 'none'),  # get_calendar_player_id_ics
    ("GET", '/calendar/team/{}.ics'): ('none', 'none'),  # get_calendar_team_id_ics
    ("GET", '/favicon.ico'): ('none', 'none'),  # get_favicon_ico
    ("GET", '/mobile'): ('none', 'none'),  # get_mobile_shell
    ("GET", '/mobile/'): ('none', 'none'),  # get_mobile_shell_slash
    ("GET", '/setup'): ('none', 'none'),  # get_setup_shell
    ("GET", '/setup/'): ('none', 'none'),  # get_setup_shell_slash
    ("GET", '/{*}'): ('none', 'none'),  # get_static_tail
    ("POST", '/api/accounts'): ('session+MANAGE_USERS', 'none'),  # post_accounts
    ("POST", '/api/accounts/{}/active'): ('session+MANAGE_USERS', 'none'),  # post_accounts_id_active
    ("POST", '/api/accounts/{}/scope'): ('session+MANAGE_USERS', 'none'),  # post_accounts_id_scope
    ("POST", '/api/accounts/{}/sessions/{}/revoke'): ('session+MANAGE_USERS', 'none'),  # post_accounts_id_sessions_id_revoke
    ("POST", '/api/admin/factory-reset/execute'): ('session+MANAGE_USERS', 'none'),  # post_admin_factory_reset_execute
    ("POST", '/api/admin/factory-reset/preview'): ('session+MANAGE_USERS', 'none'),  # post_admin_factory_reset_preview
    ("POST", '/api/auth/login'): ('none', 'none'),  # post_auth_login
    ("POST", '/api/auth/logout'): ('none', 'none'),  # post_auth_logout
    ("POST", '/api/bootstrap/claim'): ('none', 'none'),  # post_bootstrap_claim
    ("POST", '/api/calendar-feeds'): ('session+MANAGE_SCHEDULE-or-self', 'none'),  # post_calendar_feeds
    ("POST", '/api/calendar-feeds/{}/revoke'): ('session+MANAGE_SCHEDULE-or-self', 'none'),  # post_calendar_feeds_id_revoke
    ("POST", '/api/context'): ('session', 'none'),  # post_context
    ("POST", '/api/demo/add-ice-slot'): ('session+MANAGE_ARENA', 'program'),  # post_demo_add_ice_slot
    ("POST", '/api/demo/clear'): ('session+MANAGE_SETUP', 'none'),  # post_demo_clear
    ("POST", '/api/demo/load'): ('session+MANAGE_SETUP', 'none'),  # post_demo_load
    ("POST", '/api/demo/reset'): ('session+MANAGE_SETUP', 'none'),  # post_demo_reset
    ("POST", '/api/games/{}/availability'): ('session+RESPOND_AVAILABILITY', 'none'),  # post_games_id_availability
    ("POST", '/api/games/{}/availability/remind'): ('session+MANAGE_ROSTER', 'none'),  # post_games_id_availability_remind
    ("POST", '/api/games/{}/build-roster'): ('session+MANAGE_ROSTER', 'none'),  # post_games_id_build_roster
    ("POST", '/api/games/{}/cancel'): ('session+MANAGE_ROSTER', 'none'),  # post_games_id_cancel
    ("POST", '/api/games/{}/move'): ('session+MANAGE_SCHEDULE', 'none'),  # post_games_id_move
    ("POST", '/api/games/{}/officials/assign'): ('session+MANAGE_SCHEDULE', 'none'),  # post_games_id_officials_assign
    ("POST", '/api/games/{}/publish'): ('session+MANAGE_SCHEDULE', 'none'),  # post_games_id_publish
    ("POST", '/api/games/{}/reschedule/request'): ('session+MANAGE_ROSTER', 'none'),  # post_games_id_reschedule_request
    ("POST", '/api/games/{}/reschedule/{}/decide'): ('session+MANAGE_SCHEDULE', 'none'),  # post_games_id_reschedule_id_decide
    ("POST", '/api/games/{}/reschedule/{}/respond'): ('session+MANAGE_SCHEDULE-or-self', 'none'),  # post_games_id_reschedule_id_respond
    ("POST", '/api/games/{}/result'): ('session+MANAGE_SCHEDULE', 'none'),  # post_games_id_result
    ("POST", '/api/games/{}/result/approve'): ('session+MANAGE_SCHEDULE', 'none'),  # post_games_id_result_approve
    ("POST", '/api/games/{}/roster/copy-previous'): ('session+MANAGE_ROSTER', 'none'),  # post_games_id_roster_copy_previous
    ("POST", '/api/games/{}/roster/lock'): ('session+MANAGE_ROSTER', 'none'),  # post_games_id_roster_lock
    ("POST", '/api/games/{}/roster/remove'): ('session+MANAGE_ROSTER', 'none'),  # post_games_id_roster_remove
    ("POST", '/api/games/{}/roster/select'): ('session+MANAGE_ROSTER', 'none'),  # post_games_id_roster_select
    ("POST", '/api/games/{}/roster/unlock'): ('session+MANAGE_ROSTER', 'none'),  # post_games_id_roster_unlock
    ("POST", '/api/games/{}/substitutes/add-candidate'): ('session+MANAGE_ROSTER', 'none'),  # post_games_id_substitutes_add_candidate
    ("POST", '/api/games/{}/substitutes/enroll'): ('session+RESPOND_AVAILABILITY', 'none'),  # post_games_id_substitutes_enroll
    ("POST", '/api/games/{}/substitutes/withdraw'): ('session+RESPOND_AVAILABILITY', 'none'),  # post_games_id_substitutes_withdraw
    ("POST", '/api/games/{}/substitutes/{}/accept'): ('session+RESPOND_AVAILABILITY', 'none'),  # post_games_id_substitutes_id_accept
    ("POST", '/api/games/{}/substitutes/{}/add-to-roster'): ('session+MANAGE_ROSTER', 'none'),  # post_games_id_substitutes_id_add_to_roster
    ("POST", '/api/games/{}/substitutes/{}/decline'): ('session+RESPOND_AVAILABILITY', 'none'),  # post_games_id_substitutes_id_decline
    ("POST", '/api/games/{}/substitutes/{}/offer'): ('session+MANAGE_ROSTER', 'none'),  # post_games_id_substitutes_id_offer
    ("POST", '/api/games/{}/{*}'): ('none', 'none'),  # post_games_id_action
    ("POST", '/api/guardians/links'): ('session+MANAGE_USERS', 'none'),  # post_guardians_links
    ("POST", '/api/guardians/links/{}/verify'): ('session+MANAGE_USERS', 'none'),  # post_guardians_links_id_verify
    ("POST", '/api/import/commit/officials-availability'): ('session+MANAGE_SCHEDULE', 'program'),  # post_import_commit_officials_availability
    ("POST", '/api/import/commit/rinks-ice-slots'): ('session+MANAGE_ARENA', 'program'),  # post_import_commit_rinks_ice_slots
    ("POST", '/api/import/commit/teams-players'): ('session+MANAGE_SETUP', 'season'),  # post_import_commit_teams_players
    ("POST", '/api/import/dry-run'): ('session+MANAGE_ARENA', 'none'),  # post_import_dry_run
    ("POST", '/api/me/guardian/{}/games/{}/availability'): ('session+guardian-scope+verified-link', 'none'),  # post_me_guardian_id_games_id_availability
    ("POST", '/api/me/guardian/{}/substitute-opportunities/{}/accept-offer'): ('session+guardian-scope+verified-link', 'none'),  # post_me_guardian_id_substitute_opportunities_id_accept_offer
    ("POST", '/api/me/guardian/{}/substitute-opportunities/{}/decline-offer'): ('session+guardian-scope+verified-link', 'none'),  # post_me_guardian_id_substitute_opportunities_id_decline_offer
    ("POST", '/api/me/substitute-opportunities/{}/accept-offer'): ('session+player-scope', 'none'),  # post_me_substitute_opportunities_id_accept_offer
    ("POST", '/api/me/substitute-opportunities/{}/decline-offer'): ('session+player-scope', 'none'),  # post_me_substitute_opportunities_id_decline_offer
    ("POST", '/api/me/substitute-opportunities/{}/enroll'): ('session+player-scope', 'none'),  # post_me_substitute_opportunities_id_enroll
    ("POST", '/api/me/substitute-opportunities/{}/withdraw'): ('session+player-scope', 'none'),  # post_me_substitute_opportunities_id_withdraw
    ("POST", '/api/notifications/contacts'): ('session+MANAGE_SCHEDULE', 'none'),  # post_notifications_contacts
    ("POST", '/api/notifications/contacts/{}/active'): ('session+MANAGE_SETUP', 'none'),  # post_notifications_contacts_id_active
    ("POST", '/api/notifications/deliveries/process'): ('session+MANAGE_SCHEDULE', 'none'),  # post_notifications_deliveries_process
    ("POST", '/api/notifications/deliveries/{}/ignore'): ('session+MANAGE_SCHEDULE', 'none'),  # post_notifications_deliveries_id_ignore
    ("POST", '/api/notifications/deliveries/{}/retry'): ('session+MANAGE_SCHEDULE', 'none'),  # post_notifications_deliveries_id_retry
    ("POST", '/api/notifications/device-tokens'): ('session+MANAGE_SCHEDULE', 'none'),  # post_notifications_device_tokens
    ("POST", '/api/notifications/device-tokens/{}/active'): ('session+MANAGE_SCHEDULE', 'none'),  # post_notifications_device_tokens_id_active
    ("POST", '/api/notifications/preferences'): ('session+MANAGE_SCHEDULE-or-self', 'none'),  # post_notifications_preferences
    ("POST", '/api/notifications/preferences/{}/active'): ('session+MANAGE_SETUP', 'none'),  # post_notifications_preferences_id_active
    ("POST", '/api/notifications/read-all'): ('session', 'none'),  # post_notifications_read_all
    ("POST", '/api/notifications/{}/read'): ('session', 'none'),  # post_notifications_id_read
    ("POST", '/api/officials/assignments/{}/accept'): ('session+RESPOND_ASSIGNMENT-or-self', 'none'),  # post_officials_assignments_id_accept
    ("POST", '/api/officials/assignments/{}/decline'): ('session+RESPOND_ASSIGNMENT-or-self', 'none'),  # post_officials_assignments_id_decline
    ("POST", '/api/officials/assignments/{}/unassign'): ('session+MANAGE_SCHEDULE', 'none'),  # post_officials_assignments_id_unassign
    ("POST", '/api/officials/availability/{}/delete'): ('session+MANAGE_SCHEDULE-or-self', 'none'),  # post_officials_availability_id_delete
    ("POST", '/api/officials/{}/availability'): ('session+MANAGE_SCHEDULE-or-self', 'none'),  # post_officials_id_availability
    ("POST", '/api/public/calendar-feeds'): ('none', 'none'),  # post_public_calendar_feeds
    ("POST", '/api/reset'): ('session+MANAGE_SETUP', 'none'),  # post_reset
    ("POST", '/api/scheduler/commit'): ('session+MANAGE_SCHEDULE', 'cross'),  # post_scheduler_commit
    ("POST", '/api/scheduler/draft'): ('session+MANAGE_SCHEDULE', 'cross'),  # post_scheduler_draft
    ("POST", '/api/scheduler/drafts/discard'): ('session+MANAGE_SCHEDULE', 'cross'),  # post_scheduler_drafts_discard
    ("POST", '/api/scheduler/drafts/publish'): ('session+MANAGE_SCHEDULE', 'cross'),  # post_scheduler_drafts_publish
    ("POST", '/api/scheduler/scenarios'): ('session+MANAGE_SCHEDULE+real-account', 'cross'),  # post_scheduler_scenarios
    ("POST", '/api/scheduler/scenarios/{}/commit'): ('session+MANAGE_SCHEDULE+real-account', 'cross'),  # post_scheduler_scenarios_id_commit
    ("POST", '/api/setup/club'): ('session+MANAGE_SETUP', 'zero_axis'),  # post_setup_club
    ("POST", '/api/setup/club/{}/delete'): ('session+MANAGE_SETUP', 'league'),  # post_setup_club_id_delete
    ("POST", '/api/setup/division'): ('session+MANAGE_SETUP', 'season'),  # post_setup_division
    ("POST", '/api/setup/division/{}/assign-level'): ('session+MANAGE_SETUP', 'cross'),  # post_setup_division_id_assign_level
    ("POST", '/api/setup/division/{}/delete'): ('session+MANAGE_SETUP', 'cross'),  # post_setup_division_id_delete
    ("POST", '/api/setup/game'): ('session+MANAGE_SCHEDULE', 'season'),  # post_setup_game
    ("POST", '/api/setup/game/{}/delete'): ('session+MANAGE_SETUP', 'cross'),  # post_setup_game_id_delete
    ("POST", '/api/setup/ice-availability/commit'): ('session+MANAGE_ARENA', 'season'),  # post_setup_ice_availability_commit
    ("POST", '/api/setup/ice-availability/preview'): ('session+MANAGE_ARENA', 'season'),  # post_setup_ice_availability_preview
    ("POST", '/api/setup/ice-slot'): ('session+MANAGE_ARENA', 'program'),  # post_setup_ice_slot
    ("POST", '/api/setup/ice-slot/{}/delete'): ('session+MANAGE_SETUP', 'season'),  # post_setup_ice_slot_id_delete
    ("POST", '/api/setup/league'): ('session+MANAGE_SETUP', 'zero_axis'),  # post_setup_league
    ("POST", '/api/setup/league/{}/assign-organization'): ('session+MANAGE_SETUP', 'cross'),  # post_setup_league_id_assign_organization
    ("POST", '/api/setup/league/{}/delete'): ('session+MANAGE_SETUP', 'program'),  # post_setup_league_id_delete
    ("POST", '/api/setup/level'): ('session+MANAGE_SETUP', 'season'),  # post_setup_level
    ("POST", '/api/setup/level/{}/delete'): ('session+MANAGE_SETUP', 'league'),  # post_setup_level_id_delete
    ("POST", '/api/setup/official'): ('session+MANAGE_SCHEDULE', 'program'),  # post_setup_official
    ("POST", '/api/setup/official/{}/delete'): ('session+MANAGE_SETUP', 'none'),  # post_setup_official_id_delete
    ("POST", '/api/setup/organization'): ('session+MANAGE_ARENA', 'zero_axis'),  # post_setup_organization
    ("POST", '/api/setup/organization/{}/delete'): ('session+MANAGE_SETUP', 'season'),  # post_setup_organization_id_delete
    ("POST", '/api/setup/player'): ('session+MANAGE_SETUP', 'program'),  # post_setup_player
    ("POST", '/api/setup/player/{}/assign-team'): ('session+MANAGE_SETUP', 'cross'),  # post_setup_player_id_assign_team
    ("POST", '/api/setup/player/{}/delete'): ('session+MANAGE_SETUP', 'none'),  # post_setup_player_id_delete
    ("POST", '/api/setup/rink'): ('session+MANAGE_ARENA', 'program'),  # post_setup_rink
    ("POST", '/api/setup/rink/{}/assign-venue'): ('session+MANAGE_ARENA', 'cross'),  # post_setup_rink_id_assign_venue
    ("POST", '/api/setup/rink/{}/delete'): ('session+MANAGE_SETUP', 'season'),  # post_setup_rink_id_delete
    ("POST", '/api/setup/scheduling-policy'): ('session+MANAGE_ARENA', 'none'),  # post_setup_scheduling_policy
    ("POST", '/api/setup/season'): ('session+MANAGE_SETUP', 'program'),  # post_setup_season
    ("POST", '/api/setup/season-team-registration/{}/assign-division'): ('session+MANAGE_SETUP', 'cross'),  # post_setup_season_team_registration_id_assign_division
    ("POST", '/api/setup/season-team-registration/{}/remove'): ('session+MANAGE_SETUP', 'cross'),  # post_setup_season_team_registration_id_remove
    ("POST", '/api/setup/season/{}/delete'): ('session+MANAGE_SETUP', 'season'),  # post_setup_season_id_delete
    ("POST", '/api/setup/seasons/{}/roll-forward'): ('session+MANAGE_SETUP', 'season'),  # post_setup_seasons_id_roll_forward
    ("POST", '/api/setup/seasons/{}/team-registrations'): ('session+MANAGE_SETUP', 'season'),  # post_setup_seasons_id_team_registrations
    ("POST", '/api/setup/team'): ('session+MANAGE_SETUP', 'program'),  # post_setup_team
    ("POST", '/api/setup/team/{}/assign-club'): ('session+MANAGE_SETUP', 'cross'),  # post_setup_team_id_assign_club
    ("POST", '/api/setup/team/{}/delete'): ('session+MANAGE_SETUP', 'league'),  # post_setup_team_id_delete
    ("POST", '/api/setup/venue'): ('session+MANAGE_ARENA', 'program'),  # post_setup_venue
    ("POST", '/api/setup/venue/{}/assign-organization'): ('session+MANAGE_ARENA', 'cross'),  # post_setup_venue_id_assign_organization
    ("POST", '/api/setup/venue/{}/delete'): ('session+MANAGE_SETUP', 'season'),  # post_setup_venue_id_delete
    ("POST", '/api/v2/setup/club'): ('session+MANAGE_SETUP', 'zero_axis'),  # post_v2_setup_club
    ("POST", '/api/v2/setup/club/{}/delete'): ('session+MANAGE_SETUP', 'program'),  # post_v2_setup_club_id_delete
    ("POST", '/api/v2/setup/division'): ('session+MANAGE_SETUP', 'cross'),  # post_v2_setup_division
    ("POST", '/api/v2/setup/division/{}/assign-league'): ('session+MANAGE_SETUP', 'cross'),  # post_v2_setup_division_id_assign_league
    ("POST", '/api/v2/setup/division/{}/delete'): ('session+MANAGE_SETUP', 'cross'),  # post_v2_setup_division_id_delete
    ("POST", '/api/v2/setup/game'): ('session+MANAGE_SCHEDULE', 'cross'),  # post_v2_setup_game
    ("POST", '/api/v2/setup/game/{}/delete'): ('session+MANAGE_SCHEDULE', 'cross'),  # post_v2_setup_game_id_delete
    ("POST", '/api/v2/setup/ice-slot'): ('session+MANAGE_ARENA', 'program'),  # post_v2_setup_ice_slot
    ("POST", '/api/v2/setup/ice-slot/{}/delete'): ('session+MANAGE_ARENA', 'program'),  # post_v2_setup_ice_slot_id_delete
    ("POST", '/api/v2/setup/league'): ('session+MANAGE_SETUP', 'cross'),  # post_v2_setup_league
    ("POST", '/api/v2/setup/league-season/{}/delete'): ('session+MANAGE_SETUP', 'cross'),  # post_v2_setup_league_season_id_delete
    ("POST", '/api/v2/setup/league/{}/delete'): ('session+MANAGE_SETUP', 'program'),  # post_v2_setup_league_id_delete
    ("POST", '/api/v2/setup/official'): ('session+MANAGE_SCHEDULE', 'program'),  # post_v2_setup_official
    ("POST", '/api/v2/setup/official/{}/delete'): ('session+MANAGE_SETUP', 'program'),  # post_v2_setup_official_id_delete
    ("POST", '/api/v2/setup/organization'): ('session+MANAGE_ARENA', 'zero_axis'),  # post_v2_setup_organization
    ("POST", '/api/v2/setup/organization/{}/delete'): ('session+MANAGE_ARENA', 'program'),  # post_v2_setup_organization_id_delete
    ("POST", '/api/v2/setup/player'): ('session+MANAGE_SETUP', 'program'),  # post_v2_setup_player
    ("POST", '/api/v2/setup/player/{}/active'): ('session+MANAGE_SETUP', 'program'),  # post_v2_setup_player_id_active
    ("POST", '/api/v2/setup/player/{}/assign-team'): ('session+MANAGE_SETUP', 'program'),  # post_v2_setup_player_id_assign_team
    ("POST", '/api/v2/setup/player/{}/delete'): ('session+MANAGE_SETUP', 'program'),  # post_v2_setup_player_id_delete
    ("POST", '/api/v2/setup/player/{}/update'): ('session+MANAGE_SETUP', 'program'),  # post_v2_setup_player_id_update
    ("POST", '/api/v2/setup/program'): ('session+MANAGE_SETUP', 'zero_axis'),  # post_v2_setup_program
    ("POST", '/api/v2/setup/program/{}/assign-organization'): ('session+MANAGE_SETUP', 'program'),  # post_v2_setup_program_id_assign_organization
    ("POST", '/api/v2/setup/program/{}/delete'): ('session+MANAGE_SETUP', 'program'),  # post_v2_setup_program_id_delete
    ("POST", '/api/v2/setup/rink'): ('session+MANAGE_ARENA', 'program'),  # post_v2_setup_rink
    ("POST", '/api/v2/setup/rink/{}/assign-venue'): ('session+MANAGE_ARENA', 'program'),  # post_v2_setup_rink_id_assign_venue
    ("POST", '/api/v2/setup/rink/{}/delete'): ('session+MANAGE_ARENA', 'program'),  # post_v2_setup_rink_id_delete
    ("POST", '/api/v2/setup/season'): ('session+MANAGE_SETUP', 'program'),  # post_v2_setup_season
    ("POST", '/api/v2/setup/season-team-registration/{}/assign-division'): ('session+MANAGE_SETUP', 'cross'),  # post_v2_setup_season_team_registration_id_assign_division
    ("POST", '/api/v2/setup/season-team-registration/{}/assign-league'): ('session+MANAGE_SETUP', 'cross'),  # post_v2_setup_season_team_registration_id_assign_league
    ("POST", '/api/v2/setup/season-team-registration/{}/delete'): ('session+MANAGE_SETUP', 'cross'),  # post_v2_setup_season_team_registration_id_delete
    ("POST", '/api/v2/setup/season-team-registration/{}/remove'): ('session+MANAGE_SETUP', 'cross'),  # post_v2_setup_season_team_registration_id_remove
    ("POST", '/api/v2/setup/season-venue-access/{}/delete'): ('session+MANAGE_SETUP', 'cross'),  # post_v2_setup_season_venue_access_id_delete
    ("POST", '/api/v2/setup/season-venue-access/{}/remove'): ('session+MANAGE_SETUP', 'cross'),  # post_v2_setup_season_venue_access_id_remove
    ("POST", '/api/v2/setup/season/{}/delete'): ('session+MANAGE_SETUP', 'cross'),  # post_v2_setup_season_id_delete
    ("POST", '/api/v2/setup/seasons/copy-forward/commit'): ('session+MANAGE_SETUP', 'program'),  # post_v2_setup_seasons_copy_forward_commit
    ("POST", '/api/v2/setup/seasons/copy-forward/preview'): ('session+MANAGE_SETUP', 'program'),  # post_v2_setup_seasons_copy_forward_preview
    ("POST", '/api/v2/setup/seasons/{}/archive'): ('session+MANAGE_SETUP', 'cross'),  # post_v2_setup_seasons_id_archive
    ("POST", '/api/v2/setup/seasons/{}/reopen'): ('session+MANAGE_SETUP', 'cross'),  # post_v2_setup_seasons_id_reopen
    ("POST", '/api/v2/setup/seasons/{}/roll-forward'): ('session+MANAGE_SETUP', 'cross'),  # post_v2_setup_seasons_id_roll_forward
    ("POST", '/api/v2/setup/seasons/{}/team-registrations'): ('session+MANAGE_SETUP', 'cross'),  # post_v2_setup_seasons_id_team_registrations
    ("POST", '/api/v2/setup/seasons/{}/venue-access'): ('session+MANAGE_SETUP', 'cross'),  # post_v2_setup_seasons_id_venue_access
    ("POST", '/api/v2/setup/team'): ('session+MANAGE_SETUP', 'program'),  # post_v2_setup_team
    ("POST", '/api/v2/setup/team/{}/assign-club'): ('session+MANAGE_SETUP', 'program'),  # post_v2_setup_team_id_assign_club
    ("POST", '/api/v2/setup/team/{}/assign-league'): ('session+MANAGE_SETUP', 'program'),  # post_v2_setup_team_id_assign_league
    ("POST", '/api/v2/setup/team/{}/delete'): ('session+MANAGE_SETUP', 'program'),  # post_v2_setup_team_id_delete
    ("POST", '/api/v2/setup/venue'): ('session+MANAGE_ARENA', 'program'),  # post_v2_setup_venue
    ("POST", '/api/v2/setup/venue/{}/assign-organization'): ('session+MANAGE_ARENA', 'program'),  # post_v2_setup_venue_id_assign_organization
    ("POST", '/api/v2/setup/venue/{}/delete'): ('session+MANAGE_ARENA', 'program'),  # post_v2_setup_venue_id_delete
}


def _classification_mismatches(specs):
    """(name, field, expected, actual) for every reachable spec in ``specs``
    whose auth/scope_axis is not a recognised value, or disagrees with the
    independently-derived ``_EXPECTED_CLASSIFICATION``.

    The ONE function every test below calls -- the real-registry gate AND
    each mutation test -- so a mutation test proves it is caught by the
    SAME mechanism a real drift would hit, not a parallel, hand-rolled
    comparison that could quietly diverge from the real gate over time.
    """
    mismatches = []
    for spec in specs:
        if spec.name == "get_empty_path":
            continue
        if spec.auth not in _VALID_AUTH_VALUES:
            mismatches.append((spec.name, "auth", "<a valid auth value>", spec.auth))
            continue
        if spec.scope_axis not in RegistryInternalConsistencyTests._VALID_SCOPE_AXES:
            mismatches.append((spec.name, "scope_axis",
                               "<a valid scope_axis value>", spec.scope_axis))
            continue
        expected = _EXPECTED_CLASSIFICATION.get(spec.key)
        if expected is None:
            mismatches.append((spec.name, "key",
                               "<present in _EXPECTED_CLASSIFICATION>",
                               "missing -- a NEW route with no expected "
                               "value checked in; add one"))
            continue
        if (spec.auth, spec.scope_axis) != expected:
            mismatches.append((spec.name, "(auth, scope_axis)", expected,
                               (spec.auth, spec.scope_axis)))
    return mismatches


class RouteClassificationTests(unittest.TestCase):
    """The gate itself: every reachable spec, every run, against ground
    truth -- not ten pinned names, not a copy of the field being checked."""

    maxDiff = None

    def test_every_reachable_spec_is_a_valid_and_expected_classification(self):
        self.assertEqual(_classification_mismatches(REGISTRY), [])

    def test_expected_classification_covers_exactly_the_reachable_registry(self):
        """Anti-vacuity: _EXPECTED_CLASSIFICATION must name EVERY reachable
        spec (never fewer -- a spec missing from it would silently never be
        checked above) and NO extra ones (a stale entry for a route that no
        longer exists would silently never be exercised either)."""
        reachable_keys = {s.key for s in REGISTRY if s.name != "get_empty_path"}
        self.assertEqual(set(_EXPECTED_CLASSIFICATION), reachable_keys)

    def test_get_empty_path_stays_excluded_from_the_gate(self):
        spec = BY_NAME["get_empty_path"]
        self.assertEqual((spec.auth, spec.scope_axis),
                         (UNCLASSIFIED, UNCLASSIFIED))
        self.assertNotIn(spec.key, _EXPECTED_CLASSIFICATION)

    def test_the_post_permission_derivation_matches_authz_ground_truth(self):
        """Re-derive, live, the PRIMARY source _EXPECTED_CLASSIFICATION's
        POST entries were built from: authz.required_permission() is a pure
        function, callable again right here, not a value trusted from a
        prior run. Excludes the small set of custom-early-gated POST routes
        (do_POST's own special cases BEFORE the generic authorize()/
        scope_violation() gate -- see server.py:2299-2646) that
        required_permission() cannot see, and the one non-"route"-kind
        family placeholder, which is not a single leaf."""
        custom_gated_or_family = {
            "/api/bootstrap/claim", "/api/auth/login", "/api/auth/logout",
            "/api/public/calendar-feeds", "/api/notifications/preferences",
            "/api/officials/{}/availability",
            "/api/officials/availability/{}/delete",
            "/api/games/{}/reschedule/{}/respond", "/api/calendar-feeds",
            "/api/calendar-feeds/{}/revoke",
            "/api/me/substitute-opportunities/{}/enroll",
            "/api/me/substitute-opportunities/{}/withdraw",
            "/api/me/substitute-opportunities/{}/accept-offer",
            "/api/me/substitute-opportunities/{}/decline-offer",
            "/api/me/guardian/{}/games/{}/availability",
            "/api/me/guardian/{}/substitute-opportunities/{}/accept-offer",
            "/api/me/guardian/{}/substitute-opportunities/{}/decline-offer",
            "/api/context", "/api/games/{}/{*}",
        }
        checked = 0
        for spec in REGISTRY:
            if spec.method != "POST" or spec.kind != "route":
                continue
            if spec.template in custom_gated_or_family:
                continue
            with self.subTest(spec=spec.name):
                perm = authz.required_permission(sample_path(spec.template))
                base = "session" if perm is None else f"session+{perm.name}"
                # -or-self / +real-account are scope.py-layer refinements
                # required_permission() alone cannot see; strip them for
                # this specific cross-check, which is about the PERMISSION
                # half only -- the full label (refinements included) is
                # already checked byte-for-byte above.
                bare = spec.auth.split("-or-self")[0].split("+real-account")[0]
                self.assertEqual(bare, base)
                checked += 1
        # Anti-vacuity: this subTest loop must have actually run against a
        # substantial fraction of the registry, not silently matched zero
        # specs because every route happened to be excluded.
        self.assertGreater(checked, 100)

    def test_operator_only_detection_matches_the_real_dispatch(self):
        """Re-derive the still-handler-local operator policy source: does THIS
        SPECIFIC route's own ``ast.If`` call ``self._operator_only(...)``,
        parsed directly out of server.py -- not a hand-typed list, and not
        a naive source-line window (which can spill into the NEXT sibling
        branch for a short body: DEMONSTRATED against get_officials, whose
        immediately-following sibling get_players DOES call
        _operator_only, and a 12-line-window scan false-positived on it)."""
        import ast as _ast

        tree = _ast.parse(SERVER_PATH.read_text())
        by_line = {}
        for node in _ast.walk(tree):
            if isinstance(node, _ast.If):
                by_line.setdefault(node.lineno, []).append(node)

        def calls_operator_only(route) -> bool:
            for node in by_line.get(route.lineno, ()):
                if _ast.unparse(node.test) != route.test:
                    continue
                for child in _ast.walk(node):
                    if (isinstance(child, _ast.Call)
                            and isinstance(child.func, _ast.Attribute)
                            and child.func.attr == "_operator_only"
                            and isinstance(child.func.value, _ast.Name)
                            and child.func.value.id == "self"):
                        return True
            return False

        checked = 0
        for spec in REGISTRY:
            if spec.method != "GET" or spec.kind != "route":
                continue
            route = LIVE.get(spec.key)
            if route is None:
                continue
            with self.subTest(spec=spec.name):
                self.assertEqual(calls_operator_only(route),
                                 spec.auth == "operator_only")
                checked += 1
        self.assertGreater(checked, 40)

    def test_runtime_get_auth_is_the_complete_manage_users_class(self):
        """The first runtime class is derived from metadata, but independently
        pinned so deleting or widening one label cannot silently change which
        handlers the central gate owns."""
        self.assertEqual(_runtime_get_auth_map(REGISTRY),
                         _EXPECTED_RUNTIME_GET_AUTH)
        for name, permission_name in _EXPECTED_RUNTIME_GET_AUTH.items():
            with self.subTest(name=name):
                spec = BY_NAME[name]
                path = sample_path(spec.template)
                self.assertIs(runtime_get_auth_spec(path), spec)
                self.assertIs(runtime_get_auth_spec(path + "?ignored=1"), spec)
                self.assertIs(Permission[permission_name],
                              Permission.MANAGE_USERS)

    def test_runtime_get_auth_metadata_mutations_are_detected(self):
        """Removing, weakening, or changing the scope of an enrolled policy
        changes the independently expected class and therefore fails CI."""
        target = BY_NAME["get_accounts"]
        for changes in (
                {"auth": "operator_only"},
                {"auth": "session+MANAGE_SCHEDULE"},
                {"scope_axis": "cross"}):
            with self.subTest(changes=changes):
                mutated = tuple(
                    dataclasses.replace(spec, **changes)
                    if spec is target else spec
                    for spec in REGISTRY)
                self.assertNotEqual(_runtime_get_auth_map(mutated),
                                    _EXPECTED_RUNTIME_GET_AUTH)

    def test_runtime_get_auth_ambiguity_fails_closed(self):
        spec = BY_NAME["get_accounts"]
        duplicate = dataclasses.replace(spec, name="duplicate_get_accounts")
        with self.assertRaisesRegex(RuntimeError, "Ambiguous RouteSpec"):
            runtime_get_auth_spec("/api/accounts", (spec, duplicate))


class ClassificationMutationTests(unittest.TestCase):
    """The four REQUIRED mutation shapes, each proven independently caught
    by the SAME gate (_classification_mismatches) the real-registry tests
    above use -- not a separate, weaker check that could pass for a
    different reason. Every mutation is applied to a FRESH COPY of
    REGISTRY (dataclasses.replace on a frozen RouteSpec, substituted into a
    new tuple) -- the real, module-level REGISTRY is never mutated."""

    @staticmethod
    def _mutate(name: str, **fields) -> tuple:
        return tuple(
            dataclasses.replace(s, **fields) if s.name == name else s
            for s in REGISTRY)

    def test_baseline_the_real_registry_has_no_mismatches(self):
        """Control: proves the three mutation tests below fail for the
        STATED reason (the mutation), not because the gate always fails."""
        self.assertEqual(_classification_mismatches(REGISTRY), [])

    def test_mutation_private_to_public_is_caught(self):
        """The privacy-sensitive directory cannot drift back to public."""
        spec = BY_NAME["get_officials"]
        self.assertEqual(spec.auth, "operator_only")
        mutated = self._mutate("get_officials", auth="none")
        mismatches = _classification_mismatches(mutated)
        names = {m[0] for m in mismatches}
        self.assertIn("get_officials", names)

    def test_mutation_permission_substitution_is_caught(self):
        """A route's PERMISSION swapped for a different, equally-valid one
        -- not a public/private flip, not touching scope_axis at all."""
        spec = BY_NAME["post_setup_league"]
        self.assertEqual(spec.auth, "session+MANAGE_SETUP")
        mutated = self._mutate("post_setup_league",
                               auth="session+MANAGE_ARENA")
        mismatches = _classification_mismatches(mutated)
        names = {m[0] for m in mismatches}
        self.assertIn("post_setup_league", names)

    def test_mutation_scope_axis_substitution_is_caught(self):
        """scope_axis swapped for a different, equally-valid one -- auth
        untouched."""
        spec = BY_NAME["post_scheduler_draft"]
        self.assertEqual(spec.scope_axis, "cross")
        mutated = self._mutate("post_scheduler_draft", scope_axis="program")
        mismatches = _classification_mismatches(mutated)
        names = {m[0] for m in mismatches}
        self.assertIn("post_scheduler_draft", names)

    def test_mutation_both_fields_reverted_to_unclassified_is_caught(self):
        """The reviewer's own named example, second half: get_officials
        both fields -> UNCLASSIFIED. Caught by the VOCABULARY check (
        UNCLASSIFIED is not in _VALID_AUTH_VALUES) even before the
        expected-value comparison runs -- this is also what proves
        get_empty_path could never sneak an unreviewed classification in
        merely by staying UNCLASSIFIED forever: it works ONLY because
        get_empty_path is explicitly excluded by name, not because
        UNCLASSIFIED passes the vocabulary check."""
        mutated = self._mutate("get_officials", auth=UNCLASSIFIED,
                               scope_axis=UNCLASSIFIED)
        mismatches = _classification_mismatches(mutated)
        names = {m[0] for m in mismatches}
        self.assertIn("get_officials", names)

    def test_all_four_mutations_are_independently_distinguishable(self):
        """Not just "each mutation is caught somehow" -- each produces a
        DIFFERENT diagnosis, proving the gate identifies WHAT changed
        rather than a single blanket "something is wrong" signal that
        could mask which check actually fired."""
        base = _classification_mismatches(REGISTRY)
        self.assertEqual(base, [])

        private_public = _classification_mismatches(self._mutate(
            "get_officials", auth="none"))
        permission = _classification_mismatches(self._mutate(
            "post_setup_league", auth="session+MANAGE_ARENA"))
        scope_axis = _classification_mismatches(self._mutate(
            "post_scheduler_draft", scope_axis="program"))
        both_unclassified = _classification_mismatches(self._mutate(
            "get_officials", auth=UNCLASSIFIED, scope_axis=UNCLASSIFIED))

        for result in (private_public, permission, scope_axis, both_unclassified):
            self.assertEqual(len(result), 1)
        self.assertEqual(private_public[0][0], "get_officials")
        self.assertEqual(permission[0][0], "post_setup_league")
        self.assertEqual(scope_axis[0][0], "post_scheduler_draft")
        self.assertEqual(both_unclassified[0][0], "get_officials")
        # public<->private and both-unclassified hit the SAME spec, but for
        # DIFFERENT reasons -- one is a value mismatch, the other a
        # vocabulary rejection -- distinguished by the field/expected
        # columns, not merely by which name is present.
        self.assertEqual(private_public[0][1], "(auth, scope_axis)")
        self.assertEqual(both_unclassified[0][1], "auth")


class DispatchHasNoDeadBranchesTests(unittest.TestCase):
    def test_no_unreachable_nested_branch(self):
        """A nested branch no live shape can reach is dead code in the dispatch.

        (e.g. ``if sub == "roster"`` under a regex whose alternation has no
        ``roster``.) The walker collects these; there are none today.
        """
        self.assertEqual(
            [f"{handler}:{lineno}  {test}"
             for handler, lineno, test in WALKER.unreachable], [])


# --------------------------------------------------------------------------- #
# Cross-checks against server.py's method tables. The exact-Season scoped-    #
# read contract is no longer another server table; its production selector   #
# is tested separately below.                                                 #
# --------------------------------------------------------------------------- #
TABLES = (("_GET_ROUTES", srv._GET_ROUTES, "GET"),
          ("_POST_ROUTES", srv._POST_ROUTES, "POST"))

COMPILED = {method: [(spec, re.compile(spec.pattern)) for spec in REGISTRY
                     if spec.method == method]
            for method in ("GET", "POST")}


class TableCrossCheckTests(unittest.TestCase):
    """Every path either method table claims is a known RouteSpec.

    A pattern in the 405 table with no corresponding RouteSpec means that table
    has drifted away from the dispatch — the exact failure #202 exists to make
    impossible.
    """

    maxDiff = None

    def test_every_table_pattern_has_a_route_spec(self):
        orphans = []
        for label, table, method in TABLES:
            for rx in table:
                for template in templates_of_pattern(rx.pattern):
                    probe = sample_path(template)
                    if not any(compiled.match(probe)
                               for _, compiled in COMPILED[method]):
                        orphans.append(f"{label}: {method} {template} "
                                       f"(from {rx.pattern})")
        self.assertEqual(orphans, [])

    def test_is_context_scoped_read_agrees_with_the_registry(self):
        """The predicate the dispatch actually calls, exercised on real paths."""
        scoped = {spec.template for spec in CONTEXT_SCOPED_READ_SPECS}
        for spec in REGISTRY:
            if spec.method != "GET" or spec.kind != "route":
                continue
            with self.subTest(spec=spec.name):
                self.assertEqual(srv.is_context_scoped_read(
                    sample_path(spec.template)), spec.template in scoped)


class ContextReadFenceContractTests(unittest.TestCase):
    """The exact-Season fence is one fail-closed RouteSpec contract."""

    def test_runtime_context_read_fence_is_the_complete_class(self):
        self.assertEqual(_runtime_context_read_map(REGISTRY),
                         _EXPECTED_CONTEXT_SCOPED_READS)
        self.assertEqual(
            {spec.name: spec.template for spec in CONTEXT_SCOPED_READ_SPECS},
            _EXPECTED_CONTEXT_SCOPED_READS)
        for name in _EXPECTED_CONTEXT_SCOPED_READS:
            with self.subTest(name=name):
                spec = BY_NAME[name]
                path = sample_path(spec.template)
                self.assertIs(runtime_context_read_spec(path), spec)
                self.assertIs(runtime_context_read_spec(
                    path + "?ignored=1"), spec)

    def test_runtime_context_read_fence_detects_narrowing_and_widening(self):
        enrolled = BY_NAME["get_scheduler_scenarios_id"]
        unenrolled = BY_NAME["get_scheduler_scenarios"]
        for target, value in ((enrolled, False), (unenrolled, True)):
            with self.subTest(target=target.name, value=value):
                mutated = tuple(
                    dataclasses.replace(spec, context_read_fence=value)
                    if spec is target else spec
                    for spec in REGISTRY)
                self.assertNotEqual(_runtime_context_read_map(mutated),
                                    _EXPECTED_CONTEXT_SCOPED_READS)

    def test_runtime_context_read_fence_rejects_a_non_get_marker(self):
        target = BY_NAME["post_context"]
        mutated = tuple(
            dataclasses.replace(spec, context_read_fence=True)
            if spec is target else spec
            for spec in REGISTRY)
        with self.assertRaisesRegex(RuntimeError, "concrete GET routes"):
            context_scoped_read_specs(mutated)

    def test_runtime_context_read_fence_rejects_a_non_boolean_marker(self):
        target = BY_NAME["get_scheduler_scenarios_id"]
        mutated = tuple(
            dataclasses.replace(spec, context_read_fence="yes")
            if spec is target else spec
            for spec in REGISTRY)
        with self.assertRaisesRegex(RuntimeError, "must be a boolean"):
            context_scoped_read_specs(mutated)

    def test_runtime_context_read_fence_ambiguity_fails_closed(self):
        spec = BY_NAME["get_scheduler_scenarios_id"]
        duplicate = dataclasses.replace(
            spec, name="duplicate_get_scheduler_scenarios_id")
        with self.assertRaisesRegex(RuntimeError,
                                    "Ambiguous RouteSpec context-read fence"):
            runtime_context_read_spec(
                "/api/scheduler/scenarios/example", (spec, duplicate))


class SensitivePostDenialContractTests(unittest.TestCase):
    """The transport-denial audit class is one fail-closed RouteSpec fact."""

    @staticmethod
    def _mutate(target, **changes):
        return tuple(
            dataclasses.replace(spec, **changes)
            if spec is target else spec
            for spec in REGISTRY)

    def test_runtime_sensitive_post_denial_is_the_complete_class(self):
        self.assertEqual(_runtime_sensitive_post_denial_map(REGISTRY),
                         _EXPECTED_SENSITIVE_POST_DENIALS)
        self.assertEqual(
            {spec.name: (spec.transport_denial_audit_category,
                         spec.transport_denial_audit_purpose)
             for spec in SENSITIVE_POST_DENIAL_SPECS},
            _EXPECTED_SENSITIVE_POST_DENIALS)
        self.assertFalse(hasattr(srv, "_SENSITIVE_POST_AUDIT_ROUTES"))

        for name, expected in _EXPECTED_SENSITIVE_POST_DENIALS.items():
            with self.subTest(name=name):
                spec = BY_NAME[name]
                path = sample_path(spec.template)
                self.assertIs(runtime_sensitive_post_denial_spec(path), spec)
                self.assertIs(runtime_sensitive_post_denial_spec(
                    path + "?ignored=1"), spec)
                self.assertEqual(srv._sensitive_post_audit_target(path),
                                 expected)

    def test_runtime_sensitive_post_denial_detects_narrowing_and_widening(self):
        enrolled = BY_NAME["post_notifications_deliveries_id_retry"]
        narrowed = self._mutate(
            enrolled,
            transport_denial_audit_category=None,
            transport_denial_audit_purpose=None)
        self.assertNotEqual(_runtime_sensitive_post_denial_map(narrowed),
                            _EXPECTED_SENSITIVE_POST_DENIALS)

        unenrolled = BY_NAME["post_notifications_deliveries_process"]
        widened = self._mutate(
            unenrolled,
            transport_denial_audit_category=(
                SensitiveFieldCategory.CONTACT_DESTINATION),
            transport_denial_audit_purpose="process_notification_deliveries")
        self.assertNotEqual(_runtime_sensitive_post_denial_map(widened),
                            _EXPECTED_SENSITIVE_POST_DENIALS)

    def test_runtime_sensitive_post_denial_rejects_half_filled_metadata(self):
        target = BY_NAME["post_notifications_deliveries_process"]
        mutations = (
            {"transport_denial_audit_category":
             SensitiveFieldCategory.CONTACT_DESTINATION},
            {"transport_denial_audit_purpose": "unexpected_purpose"},
        )
        for changes in mutations:
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(RuntimeError, "must be paired"):
                    sensitive_post_denial_specs(
                        self._mutate(target, **changes))

    def test_runtime_sensitive_post_denial_rejects_invalid_values(self):
        target = BY_NAME["post_notifications_deliveries_id_retry"]
        mutations = (
            {"transport_denial_audit_category": "contact_destination"},
            {"transport_denial_audit_purpose": ""},
            {"transport_denial_audit_purpose": 7},
        )
        for changes in mutations:
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(RuntimeError, "metadata is invalid"):
                    sensitive_post_denial_specs(
                        self._mutate(target, **changes))

    def test_runtime_sensitive_post_denial_rejects_wrong_route_shapes(self):
        category = SensitiveFieldCategory.CONTACT_DESTINATION
        for target in (BY_NAME["get_accounts"],
                       BY_NAME["post_games_id_action"]):
            with self.subTest(target=target.name):
                mutated = self._mutate(
                    target,
                    transport_denial_audit_category=category,
                    transport_denial_audit_purpose="unexpected_purpose")
                with self.assertRaisesRegex(RuntimeError,
                                            "concrete POST routes"):
                    sensitive_post_denial_specs(mutated)

    def test_runtime_sensitive_post_denial_ambiguity_fails_closed(self):
        spec = BY_NAME["post_notifications_deliveries_id_retry"]
        duplicate = dataclasses.replace(
            spec, name="duplicate_post_notifications_delivery_retry")
        with self.assertRaisesRegex(
                RuntimeError,
                "Ambiguous RouteSpec sensitive POST denial audit"):
            runtime_sensitive_post_denial_spec(
                "/api/notifications/deliveries/example/retry",
                (spec, duplicate))


class ReassignmentParentContractTests(unittest.TestCase):
    """The writable-parent reassign class is one fail-closed RouteSpec fact."""

    @staticmethod
    def _mutate(target, **changes):
        return tuple(
            dataclasses.replace(spec, **changes)
            if spec is target else spec
            for spec in REGISTRY)

    def test_runtime_reassignment_parent_is_the_complete_class(self):
        self.assertEqual(_runtime_reassignment_parent_map(REGISTRY),
                         _EXPECTED_REASSIGNMENT_PARENTS)
        self.assertEqual(
            {spec.name: (spec.reassignment_parent_kind,
                         spec.reassignment_parent_field)
             for spec in REASSIGNMENT_PARENT_SPECS},
            _EXPECTED_REASSIGNMENT_PARENTS)
        self.assertFalse(hasattr(srv, "_REASSIGN_PARENTS"))

        for name, expected in _EXPECTED_REASSIGNMENT_PARENTS.items():
            with self.subTest(name=name):
                spec = BY_NAME[name]
                path = sample_path(spec.template)
                self.assertIs(runtime_reassignment_parent_spec(path), spec)
                self.assertIs(runtime_reassignment_parent_spec(
                    path + "?ignored=1"), spec)
                parent_id = "parent-for-" + name
                self.assertEqual(
                    srv._reassignment_parent_target(
                        path, {expected[1]: parent_id}),
                    (expected[0], parent_id, "writable_parent"))
                self.assertEqual(
                    srv._reassignment_parent_target(
                        path + "?ignored=1", {expected[1]: parent_id}),
                    (expected[0], parent_id, "writable_parent"))
                self.assertEqual(
                    srv._reassignment_parent_target(
                        path, {expected[1]: None}),
                    (expected[0], None, "writable_parent"))

        self.assertIsNone(srv._reassignment_parent_target(
            "/api/v2/setup/team/example/assign-league",
            {"league_id": "league-example"}))

    def test_runtime_reassignment_parent_detects_narrowing_and_widening(self):
        enrolled = BY_NAME["post_setup_rink_id_assign_venue"]
        narrowed = self._mutate(
            enrolled, reassignment_parent_kind=None,
            reassignment_parent_field=None)
        self.assertNotEqual(_runtime_reassignment_parent_map(narrowed),
                            _EXPECTED_REASSIGNMENT_PARENTS)

        unenrolled = BY_NAME["post_v2_setup_team_id_assign_league"]
        widened = self._mutate(
            unenrolled, reassignment_parent_kind="league",
            reassignment_parent_field="league_id")
        self.assertNotEqual(_runtime_reassignment_parent_map(widened),
                            _EXPECTED_REASSIGNMENT_PARENTS)

    def test_runtime_reassignment_parent_rejects_half_filled_metadata(self):
        target = BY_NAME["post_v2_setup_team_id_assign_league"]
        mutations = (
            {"reassignment_parent_kind": "league"},
            {"reassignment_parent_field": "league_id"},
        )
        for changes in mutations:
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(RuntimeError, "must be paired"):
                    reassignment_parent_specs(
                        self._mutate(target, **changes))

    def test_runtime_reassignment_parent_rejects_invalid_values(self):
        target = BY_NAME["post_setup_rink_id_assign_venue"]
        mutations = (
            {"reassignment_parent_kind": ""},
            {"reassignment_parent_kind": "   "},
            {"reassignment_parent_kind": 7},
            {"reassignment_parent_field": ""},
            {"reassignment_parent_field": "   "},
            {"reassignment_parent_field": 7},
        )
        for changes in mutations:
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(RuntimeError,
                                            "metadata is invalid"):
                    reassignment_parent_specs(
                        self._mutate(target, **changes))

    def test_runtime_reassignment_parent_rejects_wrong_route_shapes(self):
        cases = (
            (BY_NAME["get_accounts"], {}),
            (BY_NAME["post_games_id_action"], {}),
            (BY_NAME["post_setup_rink_id_assign_venue"],
             {"kind": "family"}),
        )
        for target, extra in cases:
            with self.subTest(target=target.name, extra=extra):
                mutated = self._mutate(
                    target, reassignment_parent_kind="organization",
                    reassignment_parent_field="organization_id", **extra)
                with self.assertRaisesRegex(RuntimeError,
                                            "concrete POST assign routes"):
                    reassignment_parent_specs(mutated)

    def test_runtime_reassignment_parent_ambiguity_fails_closed(self):
        spec = BY_NAME["post_setup_rink_id_assign_venue"]
        duplicate = dataclasses.replace(
            spec, name="duplicate_post_setup_rink_id_assign_venue")
        with self.assertRaisesRegex(
                RuntimeError, "Ambiguous RouteSpec reassignment parent"):
            runtime_reassignment_parent_spec(
                "/api/setup/rink/example/assign-venue", (spec, duplicate))


class ReassignmentDestinationContractTests(unittest.TestCase):
    """Every assign destination is one complete fail-closed RouteSpec fact."""

    @staticmethod
    def _mutate(target, **changes):
        return tuple(
            dataclasses.replace(spec, **changes)
            if spec is target else spec
            for spec in REGISTRY)

    def test_runtime_reassignment_destination_is_the_complete_class(self):
        self.assertEqual(
            _runtime_reassignment_destination_map(REGISTRY),
            _EXPECTED_REASSIGNMENT_DESTINATIONS)
        self.assertEqual(
            {spec.name: (spec.reassignment_destination_kind,
                         spec.reassignment_destination_field,
                         spec.reassignment_destination_nullable)
             for spec in REASSIGNMENT_DESTINATION_SPECS},
            _EXPECTED_REASSIGNMENT_DESTINATIONS)
        server_source = SERVER_PATH.read_text()
        for retired in ("_V1_REASSIGN_DEST", "_V2_REASSIGN_DEST",
                        "_V1_REASSIGN_SCHEMA", "_V2_REASSIGN_SCHEMA"):
            with self.subTest(retired=retired):
                self.assertNotIn(retired, server_source)

    def test_reassignment_destination_target_reads_received_body_field(self):
        self.assertEqual(
            {spec.name for spec in REASSIGNMENT_DESTINATION_SPECS},
            set(_EXPECTED_REASSIGNMENT_DESTINATIONS))
        for name, expected in _EXPECTED_REASSIGNMENT_DESTINATIONS.items():
            with self.subTest(name=name):
                spec = BY_NAME[name]
                path = sample_path(spec.template)
                self.assertIs(
                    runtime_reassignment_destination_spec(path), spec)
                self.assertIs(
                    runtime_reassignment_destination_spec(
                        path + "?ignored=1"), spec)
                destination_id = "destination-for-" + name
                self.assertEqual(
                    srv._reassignment_destination_target(
                        path, {expected[1]: destination_id}),
                    (expected[0], destination_id))
                self.assertEqual(
                    srv._reassignment_destination_target(
                        path + "?ignored=1",
                        {expected[1]: destination_id}),
                    (expected[0], destination_id))
                self.assertEqual(
                    srv._reassignment_destination_target(
                        path, {expected[1]: None}),
                    (expected[0], None))

        self.assertIsNone(srv._reassignment_destination_target(
            "/api/v2/setup/team/example/delete", {}))

    def test_reassignment_body_validation_uses_the_declared_field_and_nullability(self):
        """All 16 assign routes enforce their exact one-field wire contract."""
        self.assertEqual(
            {spec.name for spec in REASSIGNMENT_DESTINATION_SPECS},
            set(_EXPECTED_REASSIGNMENT_DESTINATIONS))
        self.assertEqual(
            {value[2] for value in _EXPECTED_REASSIGNMENT_DESTINATIONS.values()},
            {False, True})

        def reason(path, body):
            with self.assertRaises(BodyError) as raised:
                srv._validate_reassignment_body(path, body)
            return raised.exception.payload["error"]["details"]["reason"]

        for name, (_, field, nullable) in \
                _EXPECTED_REASSIGNMENT_DESTINATIONS.items():
            with self.subTest(name=name):
                path = sample_path(BY_NAME[name].template)
                valid = {field: "destination-id"}
                self.assertIs(
                    srv._validate_reassignment_body(path, valid), valid)
                self.assertIs(
                    srv._validate_reassignment_body(
                        path + "?ignored=1", valid), valid)
                self.assertEqual(reason(path, {}), "field_required")
                self.assertEqual(reason(path, {field: 7}), "wrong_type")
                self.assertEqual(
                    reason(path, {field: "destination-id", "extra": True}),
                    "unknown_field")
                if nullable:
                    explicit_null = {field: None}
                    self.assertIs(
                        srv._validate_reassignment_body(path, explicit_null),
                        explicit_null)
                    # Preserve the existing check_body contract exactly: a
                    # nullable field is presence-checked and string-typed, so
                    # an empty string remains accepted and is normalized to
                    # no destination by the target helper.
                    empty_string = {field: ""}
                    self.assertIs(
                        srv._validate_reassignment_body(path, empty_string),
                        empty_string)
                else:
                    self.assertEqual(
                        reason(path, {field: None}), "field_required")
                    self.assertEqual(
                        reason(path, {field: ""}), "field_required")

        unknown = {"anything": "unchanged"}
        self.assertIs(
            srv._validate_reassignment_body(
                "/api/v2/setup/team/example/delete", unknown), unknown)

    def test_runtime_reassignment_destination_refuses_narrowing(self):
        enrolled = BY_NAME["post_v2_setup_team_id_assign_league"]
        narrowed = self._mutate(
            enrolled, reassignment_destination_kind=None,
            reassignment_destination_field=None,
            reassignment_destination_nullable=None)
        with self.assertRaisesRegex(RuntimeError, "require.*metadata"):
            reassignment_destination_specs(narrowed)

    def test_runtime_reassignment_destination_refuses_widening(self):
        unenrolled = BY_NAME["post_games_id_action"]
        widened = self._mutate(
            unenrolled, reassignment_destination_kind="league",
            reassignment_destination_field="league_id",
            reassignment_destination_nullable=False)
        with self.assertRaisesRegex(RuntimeError,
                                    "concrete POST assign routes"):
            reassignment_destination_specs(widened)

    def test_runtime_reassignment_destination_rejects_half_filled_metadata(self):
        target = BY_NAME["post_v2_setup_team_id_assign_league"]
        mutations = (
            {"reassignment_destination_kind": None},
            {"reassignment_destination_field": None},
            {"reassignment_destination_nullable": None},
        )
        for changes in mutations:
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(RuntimeError, "must be paired"):
                    reassignment_destination_specs(
                        self._mutate(target, **changes))

    def test_runtime_reassignment_destination_rejects_invalid_values(self):
        target = BY_NAME["post_v2_setup_team_id_assign_league"]
        mutations = (
            {"reassignment_destination_kind": ""},
            {"reassignment_destination_kind": "   "},
            {"reassignment_destination_kind": 7},
            {"reassignment_destination_field": ""},
            {"reassignment_destination_field": "   "},
            {"reassignment_destination_field": 7},
            {"reassignment_destination_nullable": 0},
            {"reassignment_destination_nullable": 1},
            {"reassignment_destination_nullable": "true"},
        )
        for changes in mutations:
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(RuntimeError,
                                            "metadata is invalid"):
                    reassignment_destination_specs(
                        self._mutate(target, **changes))

    def test_runtime_reassignment_destination_rejects_wrong_route_shapes(self):
        cases = (
            (BY_NAME["get_accounts"], {}),
            (BY_NAME["post_games_id_action"], {}),
            (BY_NAME["post_v2_setup_team_id_assign_league"],
             {"kind": "family"}),
        )
        for target, extra in cases:
            with self.subTest(target=target.name, extra=extra):
                mutated = self._mutate(
                    target,
                    reassignment_destination_kind="organization",
                    reassignment_destination_field="organization_id",
                    reassignment_destination_nullable=True,
                    **extra)
                with self.assertRaisesRegex(RuntimeError,
                                            "concrete POST assign routes"):
                    reassignment_destination_specs(mutated)

    def test_runtime_reassignment_destination_requires_parent_agreement(self):
        target = BY_NAME["post_setup_rink_id_assign_venue"]
        for changes in (
                {"reassignment_destination_kind": "organization"},
                {"reassignment_destination_field": "organization_id"}):
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(RuntimeError,
                                            "must agree"):
                    reassignment_destination_specs(
                        self._mutate(target, **changes))

    def test_runtime_reassignment_destination_ambiguity_fails_closed(self):
        spec = BY_NAME["post_v2_setup_team_id_assign_league"]
        duplicate = dataclasses.replace(
            spec, name="duplicate_post_v2_setup_team_id_assign_league")
        with self.assertRaisesRegex(
                RuntimeError,
                "Ambiguous RouteSpec reassignment destination"):
            runtime_reassignment_destination_spec(
                "/api/v2/setup/team/example/assign-league",
                (spec, duplicate))


class MethodTableNarrowingTests(unittest.TestCase):
    """The OTHER direction: which live branches are method-admitted.

    Every concrete GET ``kind="route"`` is now admitted, including the four
    calendar feeds and ``/favicon.ico`` outside ``/api/``. Static shells,
    fallthroughs, and broad families remain deliberately narrower than the
    registry: they are not concrete endpoints whose existence the method
    contract may claim. What must not happen is a NEW divergence appearing
    unnoticed — for POST that is severe, because
    ``do_POST`` refuses anything ``_supported_methods`` does not admit BEFORE
    the dispatch chain runs, so a live POST branch whose spec stops being
    admitted (e.g. by a ``kind`` retag -- see ``KindClassificationTests``) is
    unreachable code that answers 404.

    The deliberate non-concrete divergence is pinned exactly below.
    """

    maxDiff = None

    #: Live GET branches ``_GET_ROUTES`` does not admit. Every entry is
    #: deliberately non-concrete: a static shell/tail or API fallthrough.
    #: Concrete non-API routes are not listed because ``kind="route"`` is the
    #: admission boundary; path prefix is not.
    GET_NOT_IN_TABLE = {
        "", "/", "/mobile", "/mobile/", "/setup", "/setup/",
        "/api/{*0}", "/{*}",
    }

    CONCRETE_NON_API_GETS = {
        "/favicon.ico",
        "/calendar/division/{}.ics", "/calendar/official/{}.ics",
        "/calendar/player/{}.ics", "/calendar/team/{}.ics",
    }

    #: Live POST branches ``_POST_ROUTES`` does not admit.
    #:
    #: #202 repair root cause 1: this set SHRANK from 12 entries to 1. The 12
    #: assign-\w+ WILDCARD templates that used to be here are gone -- they
    #: were never real leaves, and their replacement (the 13 CONCRETE combo
    #: templates _handle_reassign's own schema admits) are all kind="route"
    #: and /api/-scoped, so the POST derivation admits them
    #: automatically -- proof, independent of this registry, that the
    #: concrete leaves (and not the wildcard) were always the intended
    #: reachable set: whoever wrote the ORIGINAL 405 table by hand had
    #: already worked out the real combos, and the OLD registry disagreed
    #: with its own neighbour table without either side ever being checked
    #: against the other. Only the games family remains excluded, now by
    #: ``kind == "family"`` rather than simply not being hand-transcribed: it
    #: matches ANY subpath, including nonexistent ones (the real actions are
    #: each their own kind="route" spec, and each IS admitted).
    POST_NOT_IN_TABLE = {
        "/api/games/{}/{*}",
    }

    def _unadmitted(self, table, method):
        return {spec.template for spec in REGISTRY if spec.method == method
                and not any(rx.match(sample_path(spec.template))
                            for rx in table)}

    def test_get_table_omissions_are_the_known_set(self):
        self.assertEqual(self._unadmitted(srv._GET_ROUTES, "GET"),
                         self.GET_NOT_IN_TABLE)

    def test_post_table_omissions_are_the_known_set(self):
        self.assertEqual(self._unadmitted(srv._POST_ROUTES, "POST"),
                         self.POST_NOT_IN_TABLE)

    def test_every_concrete_non_api_get_route_is_method_admitted(self):
        """A real GET route publishes GET/HEAD/OPTIONS whatever its prefix."""
        for template in self.CONCRETE_NON_API_GETS:
            path = sample_path(template)
            with self.subTest(template=template):
                self.assertTrue(any(rx.match(path) for rx in srv._GET_ROUTES))
                self.assertTrue(any(re.compile(spec.pattern).match(path)
                                    for spec in REGISTRY
                                    if spec.method == "GET"
                                    and spec.kind == "route"))


class KindClassificationTests(unittest.TestCase):
    """#202 wiring step, go-beyond finding: ``kind`` is now LOAD-BEARING.

    server.py's ``_GET_ROUTES`` admits every GET ``kind == "route"`` spec;
    ``_POST_ROUTES`` admits POST route specs under ``/api/`` -- but
    ``RegistryCoversTheDispatchTests`` above only ever compares ``(method,
    template)`` SET MEMBERSHIP between the registry and the live dispatch; it
    never looks at ``kind``. So retagging a genuine concrete leaf from
    ``"route"`` to ``"static"``/``"fallthrough"``/``"family"`` -- or the
    reverse, retagging the games family or the static tail TO ``"route"`` --
    would sail through every existing test in this file: the (method,
    template) key is unchanged, only its ``kind`` moved, and nothing checked
    that. The live consequence is real either way: mistag a concrete POST
    leaf as non-"route" and it silently stops being admitted (a 405/Allow
    regression, exactly the shape ``MethodTableNarrowingTests`` pins for the
    entries that ARE deliberately excluded); mistag ``post_games_id_action``
    (or the static tail) TO "route" and ``_GET_ROUTES``/``_POST_ROUTES``
    would over-claim every path under it, including nonexistent game actions
    and every static path on the wire -- the OPPOSITE failure from the one
    #202's post-merge review found (a wildcard silently standing in for a
    finite set), and just as invisible to the dispatch-vs-registry gate.

    These two invariants close that gap independently of the hand-typed
    label itself.
    """

    #: Every non-``"route"`` RouteSpec, pinned by (method, name, kind) --
    #: exactly like ``_AUDIT_WAIVERS`` in route_extract.py, retagging one is
    #: now a conspicuous, reviewed diff line instead of a silent set-member
    #: move nothing here would otherwise notice.
    NON_ROUTE_KINDS = {
        ("GET", "get_empty_path", "static"),
        ("GET", "get_index", "static"),
        ("GET", "get_api_unmatched", "fallthrough"),
        ("GET", "get_mobile_shell", "static"),
        ("GET", "get_mobile_shell_slash", "static"),
        ("GET", "get_setup_shell", "static"),
        ("GET", "get_setup_shell_slash", "static"),
        ("GET", "get_static_tail", "static"),
        ("POST", "post_games_id_action", "family"),
    }

    def test_every_non_route_kind_is_exactly_the_pinned_set(self):
        actual = {(s.method, s.name, s.kind) for s in REGISTRY
                 if s.kind != "route"}
        self.assertEqual(actual, self.NON_ROUTE_KINDS)

    def test_no_route_kind_spec_carries_a_free_tail_token(self):
        """A free, unbounded tail (``{*}``/``{*0}``) is exactly what makes a
        family/fallthrough/the static tail NOT a single concrete leaf --
        #202 repair root cause 1's own lesson, that a wildcard is not a real
        route on its own. Every entry that carries one today IS one of the
        three non-"route" kinds (this is what makes them non-"route" in the
        first place); this pins the converse too, mechanically, independent
        of ``kind`` -- so a future ``kind="route"`` spec that reintroduces a
        free tail (which WOULD be silently admitted and over-claim every
        path under it) fails here even if someone forgets to update
        ``NON_ROUTE_KINDS`` at all.
        """
        offenders = [spec.name for spec in REGISTRY if spec.kind == "route"
                    and ("{*}" in spec.template or "{*0}" in spec.template)]
        self.assertEqual(offenders, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
