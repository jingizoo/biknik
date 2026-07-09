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
_LEAGUE_SETUP = {"league", "season", "division", "club", "team", "player"}

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
    # Resetting wipes and reseeds ALL demo data — destructive, so it is an
    # operator action, not an anyone-can-press control (hardening review).
    if path == "/api/reset":
        return Permission.MANAGE_SCHEDULE
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
    # Managing push device tokens is an operator action (#65).
    if path == "/api/notifications/device-tokens":
        return Permission.MANAGE_SCHEDULE
    if re.match(r"^/api/notifications/device-tokens/[^/]+/active$", path):
        return Permission.MANAGE_SCHEDULE
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
