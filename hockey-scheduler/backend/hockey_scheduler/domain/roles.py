"""Roles and a coarse role-based permission model (#24).

This is the authorization *policy* only — a pure, dependency-free mapping from a
:class:`Role` to the set of :class:`Permission` it holds. The web layer
resolves the acting role (from a real account's session as of #67) and
enforces these rules at the HTTP boundary. Keeping the policy here (domain)
makes it fully unit-testable and the single source of truth for both the
server guard and the UI.
"""

from enum import Enum


class Role(str, Enum):
    LEAGUE_ADMIN = "league_admin"     # full control
    ARENA_MANAGER = "arena_manager"   # venues/rinks/ice + scheduling
    COACH = "coach"                   # team rosters + substitute decisions
    PLAYER = "player"                 # own availability / substitute self-serve
    GUARDIAN = "guardian"             # responds on behalf of linked junior players (#26)
    OFFICIAL = "official"             # accept/decline own game assignments
    VIEWER = "viewer"                 # read-only


class Permission(str, Enum):
    MANAGE_SETUP = "manage_setup"          # league / season / division / club / team
    MANAGE_ARENA = "manage_arena"          # venue / rink / ice slot
    MANAGE_SCHEDULE = "manage_schedule"    # create / move / publish games; assign officials
    MANAGE_ROSTER = "manage_roster"        # select / remove / lock / offer subs
    RESPOND_AVAILABILITY = "respond_availability"  # player availability / sub self-serve
    RESPOND_ASSIGNMENT = "respond_assignment"      # official accepts/declines own assignment
    MANAGE_USERS = "manage_users"           # create / activate / deactivate login accounts (#67)
    VIEW = "view"                          # read everything in the demo


# Which permissions each role holds. VIEW is granted to everyone.
ROLE_PERMISSIONS = {
    Role.LEAGUE_ADMIN: {
        Permission.MANAGE_SETUP, Permission.MANAGE_ARENA,
        Permission.MANAGE_SCHEDULE, Permission.MANAGE_ROSTER,
        Permission.RESPOND_AVAILABILITY, Permission.RESPOND_ASSIGNMENT,
        Permission.MANAGE_USERS, Permission.VIEW,
    },
    Role.ARENA_MANAGER: {
        Permission.MANAGE_ARENA, Permission.MANAGE_SCHEDULE, Permission.VIEW,
    },
    Role.COACH: {
        Permission.MANAGE_ROSTER, Permission.RESPOND_AVAILABILITY, Permission.VIEW,
    },
    Role.PLAYER: {
        Permission.RESPOND_AVAILABILITY, Permission.VIEW,
    },
    # A guardian responds only for their linked juniors (#26). The link check
    # is enforced per-action on the /api/me/guardian/* routes; this permission
    # is the coarse gate that the guardian holds the same respond capability.
    Role.GUARDIAN: {
        Permission.RESPOND_AVAILABILITY, Permission.VIEW,
    },
    Role.OFFICIAL: {
        Permission.RESPOND_ASSIGNMENT, Permission.VIEW,
    },
    Role.VIEWER: {
        Permission.VIEW,
    },
}

# Human-friendly labels for the demo role switcher.
ROLE_LABELS = {
    Role.LEAGUE_ADMIN: "League Admin",
    Role.ARENA_MANAGER: "Arena Manager",
    Role.COACH: "Coach",
    Role.PLAYER: "Player",
    Role.GUARDIAN: "Guardian",
    Role.OFFICIAL: "Official",
    Role.VIEWER: "Viewer",
}


def role_from_str(value, default=Role.VIEWER):
    """Parse a role string; unknown/blank values fall back to ``default``."""
    if not value:
        return default
    try:
        return Role(value)
    except ValueError:
        return default


def can(role, permission) -> bool:
    """Return True if ``role`` holds ``permission``."""
    return permission in ROLE_PERMISSIONS.get(role, set())


def permissions_for(role) -> list:
    """Sorted list of permission values a role holds (for the UI / API)."""
    return sorted(p.value for p in ROLE_PERMISSIONS.get(role, set()))
