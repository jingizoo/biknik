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

It ALSO holds the *pre-spine* skip vocabulary the #427 batch-seating ruling
needs (``MEMBERSHIP_STATUS_REASONS``, ``MEMBERSHIP_OTHER_TEAM``,
``MEMBERSHIP_OTHER_LEAGUE_SEASON``, ``PLAYER_INACTIVE``, and the narrowed
``NO_ELIGIBLE_MEMBERSHIP``) — see the block above those constants for why
every reason an operator can be shown lives in this one module.
"""

from typing import NamedTuple, Optional, Tuple

from ..domain.enums import MembershipStatus
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

# ======================================================================
# WHY A PLAYER DID NOT RESOLVE — the *pre-spine* reasons (PR #427)
# ======================================================================
# The owner's #427 product ruling requires the two BATCH seating entry
# points to "deterministically identify … the players skipped, with a
# stable reason for each skip". Before this commit ``NO_ELIGIBLE_
# MEMBERSHIP`` was the single answer for FOUR of the five candidate shapes
# the ruling names by hand — transferred, membership inactive,
# membership-less, and wrong-LeagueSeason all collapsed into one
# undifferentiated string, because ``_resolve_context_with_reason``'s
# candidate filter ``continue``d before recording anything about a row it
# discarded. An operator told "4 players skipped: no_eligible_membership"
# learns nothing they can act on, and the ruling's "never a silent partial
# success" is only nominally satisfied.
#
# So the filter became a CLASSIFIER (roster_service.
# ``_resolve_context_with_reason``) and these are the names it records.
# They live HERE, beside the spine's own reasons, because the ruling's
# skip vocabulary must have exactly one home — a second table somewhere
# else is how a code ends up meaning two things.
#
# ONE REASON PER MEMBERSHIP STATUS, not one bucket with the status in a
# detail field. ``transferred`` and ``inactive`` are different facts about
# a player, they need different operator-facing words ("transferred to
# another team" vs "membership is inactive"), and a UI that has to reach
# into a detail payload to tell two skips apart is a UI that will
# eventually stop bothering. Deriving the reason from the enum ALSO means
# a new ``MembershipStatus`` value cannot silently inherit some other
# status's wording: :func:`status_ineligible_reason` raises for anything
# unmapped, and ``MembershipReasonsCoverEveryStatus`` in
# tests/test_membership_skip_reasons.py fails the moment the enum grows.
MEMBERSHIP_STATUS_REASONS = {
    MembershipStatus.APPLICANT: "membership_applicant",
    MembershipStatus.INACTIVE: "membership_inactive",
    MembershipStatus.INJURED: "membership_injured",
    MembershipStatus.RELEASED: "membership_released",
    MembershipStatus.TRANSFERRED: "membership_transferred",
}

# A membership exists at this EXACT LeagueSeason, and grants current
# participation — but on a team that is not playing in this game. The
# "borrowed from another club" shape; cross-team borrowing is off (#287
# open question 4 is unruled), so it is a skip, not a seat.
MEMBERSHIP_OTHER_TEAM = "membership_other_team"

# The player holds membership rows, but NONE of them names this game's
# LeagueSeason — the owner's "wrong-LeagueSeason" shape. Distinct from
# ``MEMBERSHIP_OTHER_TEAM`` because the remedy is different: this player is
# registered in a different competition, not merely on a different bench.
MEMBERSHIP_OTHER_LEAGUE_SEASON = "membership_other_league_season"

# The ``Player`` row itself is deactivated (#270's ``Player.is_active``).
# NOT a spine leg and deliberately NOT decided by
# ``_resolve_context_with_reason``: deactivation is a property of the
# person, checked by the write-time gates (``select_roster``,
# ``_require_active_player``) AFTER the context resolves, and folding it
# into the resolver would newly close reads that are open today. It is
# produced by ``RosterService.seating_block_reason``, which layers the two
# gates in the SAME order ``select_roster`` applies them.
PLAYER_INACTIVE = "player_inactive"

# NARROWED (PR #427) to its true meaning: this player has NO membership
# rows AT ALL — nothing to resolve, nothing to explain. Every shape that
# used to land here now has its own name above.
NO_ELIGIBLE_MEMBERSHIP = "no_eligible_membership"


def status_ineligible_reason(status) -> str:
    """The stable reason naming WHY a membership at the right key does not
    grant participation, from its :class:`MembershipStatus`.

    Raises :class:`KeyError` for a status that is not mapped — an ELIGIBLE
    status (``active``/``affiliate``) never needs a reason, and a NEW status
    nobody has classified must fail loudly here rather than be reported to an
    operator under some other status's wording."""
    return MEMBERSHIP_STATUS_REASONS[status]


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
