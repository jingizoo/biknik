"""HTTP-boundary authorization for the demo server (#24).

Maps a mutating request path to the :class:`Permission` it requires, then
checks the acting :class:`Role` against the domain policy. Pure functions so the
mapping is unit-testable without spinning up the HTTP server. GET requests are
read-only and always allowed (everyone has VIEW).
"""

import re

from ..domain import Permission, can

# League-structure setup entities vs arena entities (both under /api/setup/).
# Organization owns venues, so it's arena-side (MANAGE_ARENA), like venue/rink (#166).
_ARENA_SETUP = {"organization", "venue", "rink", "ice-slot"}
_LEAGUE_SETUP = {"league", "season", "level", "division", "club", "team", "player"}

# The v2 canonical setup surface (#233 Slice C2). Same operator authz as v1, over
# canonical entity names: v1 "league"(umbrella)→"program", v1 "level"(grouping)→
# "league". The required *permission* is unchanged by the rename (both are
# MANAGE_SETUP), so only the entity-name sets differ.
_V2_ARENA_SETUP = {"organization", "venue", "rink", "ice-slot"}
_V2_SETUP_SETUP = {"program", "season", "league", "division", "club", "team",
                   "player"}


def _v2_setup_permission(rest: str):
    """The Permission a ``/api/v2/setup/<rest>`` POST requires (#233 Slice C2)."""
    # Reassignment: facility-side moves (venue/rink parents) are arena actions;
    # competition-structure moves (division/team/player parents) require setup.
    if re.match(r"^(venue|rink)/[^/]+/assign-\w+$", rest):
        return Permission.MANAGE_ARENA
    if re.match(r"^(program|division|team|player)/[^/]+/assign-\w+$", rest):
        return Permission.MANAGE_SETUP
    # Season team-registration operations (register / assign-league /
    # assign-division / remove / roll-forward) are setup actions.
    if re.match(r"^season-team-registration/[^/]+/"
                r"(assign-league|assign-division|remove)$", rest):
        return Permission.MANAGE_SETUP
    if re.match(r"^seasons/[^/]+/(team-registrations|roll-forward)$", rest):
        return Permission.MANAGE_SETUP
    # Season-venue access (#233 Slice E): granting/revoking which Venues a
    # Season may use is a competition-side setup action, same permission as
    # season team registrations above (not MANAGE_ARENA — the Venue itself is
    # untouched, only the join to a Season is created/removed).
    if re.match(r"^season-venue-access/[^/]+/(remove|delete)$", rest):
        return Permission.MANAGE_SETUP
    if re.match(r"^seasons/[^/]+/venue-access$", rest):
        return Permission.MANAGE_SETUP
    if re.match(r"^programs/[^/]+/teams$", rest):
        return Permission.MANAGE_SETUP
    if rest == "hierarchy":
        return Permission.MANAGE_SETUP
    # The flat overview (#233 B2a review) mixes arena entities (organization/
    # venue/rink/ice_slot) with competition ones, but carries no PII or
    # roster-adjacent counts (unlike hierarchy's player_count) — so unlike
    # hierarchy it's gated to MANAGE_ARENA, the one permission both League
    # Admin and Arena Manager hold (unauthenticated ov.venues/etc. is no
    # longer the Setup facility source, so Arena Managers need this read).
    if rest == "overview":
        return Permission.MANAGE_ARENA
    # Delete: /api/v2/setup/<entity>/<id>/delete — per-entity permission.
    md = re.match(r"^([\w-]+)/[^/]+/delete$", rest)
    if md:
        kind = md.group(1)
        if kind == "game":
            return Permission.MANAGE_SCHEDULE
        if kind in _V2_ARENA_SETUP:
            return Permission.MANAGE_ARENA
        return Permission.MANAGE_SETUP
    # Plain entity create.
    if rest in ("game", "official"):
        return Permission.MANAGE_SCHEDULE
    if rest in _V2_ARENA_SETUP:
        return Permission.MANAGE_ARENA
    if rest in _V2_SETUP_SETUP:
        return Permission.MANAGE_SETUP
    return Permission.MANAGE_SETUP  # unknown v2 setup entity: require setup

# Game sub-actions grouped by the permission they require.
_SCHEDULE_ACTIONS = {"move", "publish", "result", "result/approve"}
_ROSTER_ACTIONS = {
    "build-roster", "roster/select", "roster/remove", "roster/copy-previous",
    "roster/lock", "roster/unlock", "cancel", "substitutes/add-candidate",
}
_AVAILABILITY_ACTIONS = {
    "availability", "substitutes/enroll", "substitutes/withdraw",
}


def required_permission(path: str):
    """Return the Permission a POST ``path`` requires, or None if unguarded.

    None means "no special permission" (e.g. the demo reset control) — still
    allowed for any role.
    """
    # Resetting wipes and reseeds ALL demo data — a League-Admin-only operation
    # (#215): gated on MANAGE_SETUP so a scheduler/arena role can't invoke it
    # directly even though the header button is hidden from them. The canonical
    # route is /api/demo/reset; /api/reset is kept as a back-compat alias with
    # the same gate.
    if path in ("/api/reset", "/api/demo/reset",
                "/api/demo/load", "/api/demo/clear"):
        return Permission.MANAGE_SETUP
    if path == "/api/demo/add-ice-slot":
        return Permission.MANAGE_ARENA
    # Onboarding import dry-run (#92): read-only against the store, but the
    # rows describe league/arena setup, so gate it like those setup routes.
    # Reuse MANAGE_ARENA (not MANAGE_SETUP) since it is the one permission the
    # two intended operator roles — League Admin and Arena Manager — both
    # hold, and it already covers the arena-side entities (rinks/ice slots)
    # in this same import set.
    if path == "/api/import/dry-run":
        return Permission.MANAGE_ARENA
    # Onboarding import COMMIT (#93): unlike its dry-run sibling above, this
    # one actually writes real league/team/player records, so it requires the
    # League-Admin-only MANAGE_SETUP permission, not the wider MANAGE_ARENA
    # both operator roles share for the read-only preview.
    if path == "/api/import/commit/teams-players":
        return Permission.MANAGE_SETUP
    # Officials + availability import COMMIT (#94): unlike #93's
    # teams/players commit above, creating/updating an Official already
    # requires only MANAGE_SCHEDULE, not MANAGE_SETUP (see
    # "/api/setup/official" below) — both League Admin and Arena Manager hold
    # MANAGE_SCHEDULE, so both can run this import, matching that existing
    # precedent rather than introducing a League-Admin-only rule for
    # officials specifically.
    if path == "/api/import/commit/officials-availability":
        return Permission.MANAGE_SCHEDULE
    # Rinks + ice slots import COMMIT (#95): mirrors "/api/setup/rink" and
    # "/api/setup/ice-slot" below (both in _ARENA_SETUP) rather than #93's
    # League-Admin-only MANAGE_SETUP — both League Admin and Arena Manager
    # hold MANAGE_ARENA, and rinks/ice slots are arena-side entities just
    # like their single-entity creation routes.
    if path == "/api/import/commit/rinks-ice-slots":
        return Permission.MANAGE_ARENA
    # Existing Program/League/Venue codes for the hierarchy import wizard's
    # pickers (#260 review Q2/Q3/Q6) — same League-Admin-only MANAGE_SETUP
    # gate as the hierarchy import commit itself, since it's read from the
    # same setup surface.
    if path == "/api/import/hierarchy-codes":
        return Permission.MANAGE_SETUP
    # Generating a draft season schedule is a scheduling action (#84).
    if path == "/api/scheduler/draft":
        return Permission.MANAGE_SCHEDULE
    # Committing / publishing / discarding drafts are scheduling actions (#86).
    if path in ("/api/scheduler/commit", "/api/scheduler/drafts/publish",
                "/api/scheduler/drafts/discard"):
        return Permission.MANAGE_SCHEDULE
    # League-wide reschedule approval queue is an operator action (#29) —
    # same MANAGE_SCHEDULE gate as the per-request decide action above.
    if path == "/api/reschedule/pending":
        return Permission.MANAGE_SCHEDULE
    # Draining the notification delivery queue is an operator action (#58).
    if path == "/api/notifications/deliveries/process":
        return Permission.MANAGE_SCHEDULE
    # Dead-letter retry/ignore are operator actions on the queue (#80).
    if re.match(r"^/api/notifications/deliveries/[^/]+/(retry|ignore)$", path):
        return Permission.MANAGE_SCHEDULE
    # Managing contact destinations is an operator action (#60).
    if path == "/api/notifications/contacts":
        return Permission.MANAGE_SCHEDULE
    # Retiring/reactivating one (#232 review 4) is a Player/Official
    # delete-lifecycle action, so it takes the League-Admin-only
    # MANAGE_SETUP permission that gates Player/Official deletion itself,
    # not the wider MANAGE_SCHEDULE an Arena Manager also holds.
    if re.match(r"^/api/notifications/contacts/[^/]+/active$", path):
        return Permission.MANAGE_SETUP
    # Managing push device tokens is an operator action (#65).
    if path == "/api/notifications/device-tokens":
        return Permission.MANAGE_SCHEDULE
    if re.match(r"^/api/notifications/device-tokens/[^/]+/active$", path):
        return Permission.MANAGE_SCHEDULE
    # Retiring/reactivating a notification preference (#232 review 4): same
    # League-Admin-only MANAGE_SETUP rationale as the contact-destination
    # retire above.
    if re.match(r"^/api/notifications/preferences/[^/]+/active$", path):
        return Permission.MANAGE_SETUP
    # Creating/activating login accounts is a distinct, narrower operator
    # action than scheduling — only a league admin holds it (#67).
    if path == "/api/accounts":
        return Permission.MANAGE_USERS
    if re.match(r"^/api/accounts/[^/]+/active$", path):
        return Permission.MANAGE_USERS
    # Viewing/revoking an account's login sessions is a user-management action
    # held only by a league admin (#78).
    if re.match(r"^/api/accounts/[^/]+/sessions$", path):
        return Permission.MANAGE_USERS
    if re.match(r"^/api/accounts/[^/]+/sessions/[^/]+/revoke$", path):
        return Permission.MANAGE_USERS
    # Guardian↔junior link creation/verification (#35) — the same
    # league-admin-only user-management permission as account creation,
    # since binding a guardian's authority to a junior is comparably
    # sensitive identity administration.
    if path == "/api/guardians/links":
        return Permission.MANAGE_USERS
    if re.match(r"^/api/guardians/links/[^/]+/verify$", path):
        return Permission.MANAGE_USERS
    # Officials: an official accepts/declines their own assignment (#54);
    # unassigning is an operator/scheduling action (#30).
    m = re.match(r"^/api/officials/assignments/[^/]+/(accept|decline|unassign)$", path)
    if m:
        return (Permission.MANAGE_SCHEDULE if m.group(1) == "unassign"
                else Permission.RESPOND_ASSIGNMENT)
    if path.startswith("/api/officials/assignments/"):
        return Permission.MANAGE_SCHEDULE

    # Guided onboarding status (#174 PR C): aggregates setup + account/player
    # onboarding state into one League-Admin-only read. MANAGE_SETUP is held by
    # no role but League Admin, so this gates it to League Admins exactly (an
    # Arena Manager, who holds MANAGE_ARENA/MANAGE_SCHEDULE, is 403), matching
    # the "only a League Admin sees full onboarding data" rule (#174).
    if path == "/api/onboarding/status":
        return Permission.MANAGE_SETUP

    # Canonical v2 surface (#233 Slice C2): the v2 readiness read is League-Admin
    # only like its v1 sibling, and the v2 setup routes mirror v1's authz.
    if path == "/api/v2/onboarding/status":
        return Permission.MANAGE_SETUP
    if path.startswith("/api/v2/setup/"):
        return _v2_setup_permission(path[len("/api/v2/setup/"):])

    # Cross-domain league→owner move (#173) — tying a league to an owner spans
    # both domains, so it requires MANAGE_SETUP (League-Admin-only), even
    # though a venue's own arena-side moves don't. Must precede the arena-side
    # venue rule below.
    if re.match(r"^/api/setup/league/[^/]+/assign-organization$", path):
        return Permission.MANAGE_SETUP

    # Reassignment routes (#166 PR D): /api/setup/<entity>/<id>/assign-<parent>.
    # Facility-side moves (venue's owner, rink's venue) are arena actions;
    # league-structure moves (division/team/player parents) require setup.
    m = re.match(r"^/api/setup/(venue|rink)/[^/]+/assign-\w+$", path)
    if m:
        return Permission.MANAGE_ARENA
    m = re.match(r"^/api/setup/(division|team|player)/[^/]+/assign-\w+$", path)
    if m:
        return Permission.MANAGE_SETUP

    if path.startswith("/api/setup/"):
        entity = path[len("/api/setup/"):]
        if entity in ("game", "official"):
            return Permission.MANAGE_SCHEDULE
        if entity in _ARENA_SETUP:
            return Permission.MANAGE_ARENA
        if entity in _LEAGUE_SETUP:
            return Permission.MANAGE_SETUP
        return Permission.MANAGE_SETUP  # unknown setup entity: require setup

    m = re.match(r"^/api/games/[^/]+/(.+)$", path)
    if m:
        action = m.group(1)
        if action in _SCHEDULE_ACTIONS or action.startswith("officials/"):
            return Permission.MANAGE_SCHEDULE
        if action in _ROSTER_ACTIONS:
            return Permission.MANAGE_ROSTER
        if action in _AVAILABILITY_ACTIONS:
            return Permission.RESPOND_AVAILABILITY
        # Reschedule workflow (#29): a coach requests (MANAGE_ROSTER —
        # coach-scoping to their own team is enforced separately, see
        # scope.py); the league/arena decides (MANAGE_SCHEDULE, same as
        # move/publish). "respond" is NOT handled here — it's guarded
        # earlier in do_POST, before this coarse single-permission check
        # ever runs, because the opponent team's coach (MANAGE_ROSTER) OR
        # an operator (MANAGE_SCHEDULE — e.g. an Arena Manager, who holds no
        # MANAGE_ROSTER) may respond, and this function can only express one
        # required permission per path.
        if action == "reschedule/request":
            return Permission.MANAGE_ROSTER
        resched = re.match(r"^reschedule/[^/]+/decide$", action)
        if resched:
            return Permission.MANAGE_SCHEDULE
        # substitutes/{pid}/(offer|accept|decline|add-to-roster)
        sub = re.match(r"^substitutes/[^/]+/(offer|accept|decline|add-to-roster)$", action)
        if sub:
            op = sub.group(1)
            # Coach controls the pool (offer / add); a player accepts/declines.
            return (Permission.MANAGE_ROSTER if op in ("offer", "add-to-roster")
                    else Permission.RESPOND_AVAILABILITY)
        return Permission.MANAGE_ROSTER  # unknown game action: require roster

    return None


def authorize(role, path: str) -> bool:
    """True if ``role`` may POST to ``path``."""
    perm = required_permission(path)
    if perm is None:
        return True
    return can(role, perm)
