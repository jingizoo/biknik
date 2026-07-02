"""HTTP-boundary authorization for the demo server (#24).

Maps a mutating request path to the :class:`Permission` it requires, then
checks the acting :class:`Role` against the domain policy. Pure functions so the
mapping is unit-testable without spinning up the HTTP server. GET requests are
read-only and always allowed (everyone has VIEW).
"""

import re

from ..domain import Permission, can

# League-structure setup entities vs arena entities (both under /api/setup/).
_ARENA_SETUP = {"venue", "rink", "ice-slot"}
_LEAGUE_SETUP = {"league", "season", "division", "club", "team"}

# Game sub-actions grouped by the permission they require.
_SCHEDULE_ACTIONS = {"move", "publish", "result", "result/approve"}
_ROSTER_ACTIONS = {
    "build-roster", "roster/select", "roster/remove", "roster/copy-previous",
    "roster/lock", "roster/unlock", "cancel",
}
_AVAILABILITY_ACTIONS = {
    "availability", "substitutes/enroll", "substitutes/withdraw",
}


def required_permission(path: str):
    """Return the Permission a POST ``path`` requires, or None if unguarded.

    None means "no special permission" (e.g. the demo reset control) — still
    allowed for any role.
    """
    if path in ("/api/reset",):
        return None
    if path == "/api/demo/add-ice-slot":
        return Permission.MANAGE_ARENA
    # Draining the notification delivery queue is an operator action (#58).
    if path == "/api/notifications/deliveries/process":
        return Permission.MANAGE_SCHEDULE
    # Managing contact destinations is an operator action (#60).
    if path == "/api/notifications/contacts":
        return Permission.MANAGE_SCHEDULE
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
