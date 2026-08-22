"""The membership spine — ONE predicate, shared by the write-time guards and
the read-time resolver (#205 review blocker 2, owner comment 5368386042).

A ``SeasonRosterMembership`` is only meaningful while the participation that
justified it is still real. ``SetupService`` has always proven that at WRITE
time (mint, and parked-row revival) via ``_assert_membership_program_spine``/
``_assert_membership_spine_valid``. Nothing proved it at READ time, so a
restored backup, a direct/bulk writer, or a parent-mutation race could leave a
membership row whose Team's participation in that competition has ENDED —
and the substitute workflow's resolver, which checked only "the LeagueSeason
row exists" plus the membership's own LS/team/status, still handed that row
out as current authority.

This module holds the spine as a PURE, non-raising predicate so both layers
answer it identically:

* :func:`missing_or_unequal` — the MISSING-or-DISAGREEING rule (moved here
  verbatim from ``setup_service``, which now imports it).
* :func:`side_spine_break` — the Team-League-Season/Program/registration legs
  at one ``(LeagueSeason, team)`` pair, returning the STABLE reason string
  naming the FIRST broken edge, or ``None`` with the resolved rows.

The reason strings are the ones ``setup_service``'s raising guards already
use, so a read-time refusal and a write-time refusal name the same edge.
"""

from typing import NamedTuple, Optional, Tuple

from .league_scope import exact_registration_or_conflict


def missing_or_unequal(a, b) -> bool:
    """A scope-spine key is BROKEN when EITHER side is MISSING or the two
    DISAGREE (#205 review round 3 blocker 3) — the Python twin of
    ``integrity_checks._MISSING_OR_UNEQUAL``, the SQL predicate migration
    059's preflight applies to this very invariant.

    The membership spine guards used to be spelled ``if team.league_id and
    ls.league_id != team.league_id``. The leading conjunct is a FALSY-SKIP:
    a Team with NO permanent League skipped the coherence check entirely
    rather than failing it — the exact service-layer analogue of the NULL
    evasion blocker 1 fixed in the preflight, where ``a != b`` evaluated
    UNKNOWN (not TRUE) against a NULL and the row was filtered out. The two
    layers then disagreed: 059 REFUSED to backfill a league-less Team while
    the live service happily minted and revived memberships on one.

    ``not a`` rather than ``a is None`` deliberately: the guards this
    replaces were truthiness gates, so an empty-string id was skipped too.
    Treating both shapes as MISSING is strictly stronger than what shipped
    and keeps one rule for "this key is not there".

    Both-missing is a violation, not agreement — the same conclusion
    ``_MISSING_OR_UNEQUAL``'s own docstring reaches about why
    ``IS DISTINCT FROM`` is the wrong operator for a scope spine."""
    return not a or not b or a != b


class SideSpine(NamedTuple):
    """The rows a validated ``(LeagueSeason, team)`` pair resolves to."""
    team: object
    league: object
    season: object
    registration: object


# The stable reason strings, in the order the legs are checked. Identical to
# the ones ``SetupService``'s raising guards already produce, so a read-time
# refusal and a write-time refusal name the SAME edge.
TEAM_MISSING = "membership_team_missing"
LEAGUE_SEASON_MISSING = "membership_league_season_missing"
SEASON_MISSING = "membership_season_missing"
LEAGUE_MISMATCH = "membership_league_mismatch"
PROGRAM_MISMATCH = "membership_program_mismatch"
NOT_REGISTERED = "team_not_registered"
REGISTRATION_CONFLICT = "team_registration_conflict"
# Two legs the READ-time resolver owns (they are properties of the membership
# row and the player, not of one ``(LeagueSeason, team)`` pair), named here so
# every spine reason lives in one place.
PLAYER_MISSING = "membership_player_missing"
DENORMALIZED_SEASON_MISMATCH = "membership_denormalized_season_mismatch"
NO_ELIGIBLE_MEMBERSHIP = "no_eligible_membership"


def side_spine_break(store, ls, team_id) -> Tuple[Optional[str],
                                                  Optional[SideSpine]]:
    """``(reason, None)`` naming the FIRST broken edge of the spine at
    ``(ls, team_id)``, or ``(None, SideSpine)`` when every leg holds.

    The legs, in order — each one an edge the owner's correction names:

    1. the participating **Team** exists (``membership_team_missing``);
    2. the **Season** the LeagueSeason names exists
       (``membership_season_missing``) — needed both as a spine row of its
       own and as the authority the membership's DENORMALIZED ``season_id``
       is compared against by the caller;
    3. **Team-League**: ``team.league_id == ls.league_id`` under
       :func:`missing_or_unequal`, so a league-LESS Team is a violation and
       not an exemption (``membership_league_mismatch``);
    4. the **Program** leg: Team, League and Season all present and naming
       ONE Program (``membership_program_mismatch``) — the exact predicate
       ``SetupService._assert_membership_program_spine`` applies at write
       time;
    5. a **current, unambiguous, ACTIVE SeasonTeamRegistration** at this
       exact ``(team, league_season)`` key. Resolved through
       ``exact_registration_or_conflict`` rather than the bare store lookup
       so a duplicated key fails CLOSED (``team_registration_conflict``)
       instead of silently picking whichever row sorts first on
       ``InMemoryStore``, which enforces no uniqueness — the same discipline
       ``context_scope``/``league_scope``'s read-only gates already apply.
       A missing or inactive row is ``team_not_registered``.
    """
    team = store.get_team(team_id) if team_id else None
    if team is None:
        return TEAM_MISSING, None
    if ls is None:
        return LEAGUE_SEASON_MISSING, None
    season = store.get_season(ls.season_id) if ls.season_id else None
    if season is None:
        return SEASON_MISSING, None
    if missing_or_unequal(team.league_id, ls.league_id):
        return LEAGUE_MISMATCH, None
    league = store.get_league(ls.league_id) if ls.league_id else None
    league_program = league.program_id if league is not None else None
    if (missing_or_unequal(team.program_id, league_program)
            or missing_or_unequal(league_program, season.program_id)):
        return PROGRAM_MISMATCH, None
    registration, conflicts = exact_registration_or_conflict(
        store, ls.id, team_id)
    if conflicts:
        return REGISTRATION_CONFLICT, None
    if registration is None or not registration.active:
        return NOT_REGISTERED, None
    return None, SideSpine(team=team, league=league, season=season,
                           registration=registration)
