"""Full end-to-end demo dataset.

Builds the Alpine Ice Hockey League scenario using the **real** setup service
(not orphan game data): league → season → divisions, clubs → teams, venue →
rinks → ice slots, and a manual game on an allocated slot. Then seeds the home
team's players and confirms a full roster, leaving one player available to act
as a substitute so the live roster/substitute flow can be demonstrated.

Beyond that core scenario (#97, the "pilot data pack"), the store is filled
out to pilot scale — ~12 teams, ~180 players, ~24 officials, 3 rinks across 2
venues, two weeks of ice inventory, and ~20 published games — so sales demos
and screenshots look like a real multi-week league, not a 4-team toy. This is
purely ADDITIVE: every id/entity the core scenario above returns in ``ids``
(and that ~20 test files key off via ``STATE.ids``) is created exactly as
before; the pilot-scale data is extra teams/players/officials/ice/games
layered on top, never a replacement.

No real PII — all names are obviously fictional.
"""

from datetime import datetime, timedelta, timezone
from typing import Tuple

from .domain import (
    AvailabilityStatus,
    IceSlotType,
    OfficialAvailabilityStatus,
    OfficialRole,
    Position,
)
from .domain.errors import DomainError
from .services import RosterService, SetupService
from .store import InMemoryStore

# The demo's ice inventory is RELATIVE to the instant the demo is seeded at,
# never pinned to the calendar (#387).
#
# It used to be three fixed dates — 2026-09-05 (the core scenario's "Saturday"),
# 2026-09-12 (the unrostered "next game") and 2026-09-06 + 14 days (the
# pilot-scale inventory). Those are a countdown, not data: once real time passes
# them, a freshly-seeded demo is born past-dated. Every surface that reasons
# about "now" then degrades with it — `find_next_game_for_player` returns no
# next game, `accept_substitute_offer` refuses ("no longer upcoming"),
# `delete_ice_slot` refuses ("past slots are history"), and a sales demo shows
# a league whose whole season already happened. Nothing fails loudly; it just
# quietly stops demonstrating anything.
#
# The same absolute dates were also a *clock-dependent* trap for anything that
# books ice relative to now. `test_actor_attribution` booked at `now + 31..34
# days` while inheriting the current hour, so it collided with the evening slots
# below only between 15:00 and 21:00 UTC: identical trees passed as a PR at
# 12:43Z and failed on main at 15:08Z on 2026-08-02. #384 fixed that fixture by
# anchoring it to the rink's own occupancy; this fixes the data it tripped over.
#
# Day zero is derived, not chosen: UTC midnight on the first Saturday at least
# a week after the seed instant. Saturday keeps the demo's weekend look; the
# week of lead time keeps the whole two-week inventory below future-dated even
# for a demo server left running for days. The derivation is a pure function of
# the seed instant, so the same instant always rebuilds the same inventory
# (CLAUDE.md: the domain never calls ``datetime.now()`` itself — the instant is
# passed in).
_DEMO_WEEKDAY = 5          # Saturday, with Monday == 0 (``date.weekday()``)
_DEMO_LEAD_DAYS = 7


def demo_day_zero(seed_instant: datetime) -> datetime:
    """The demo's day zero for ``seed_instant``: UTC midnight on the first
    Saturday at least ``_DEMO_LEAD_DAYS`` days after it.

    ``seed_instant`` must be a timezone-aware datetime — the repo's convention
    for every time that crosses a boundary. A naive value is rejected rather
    than assumed to be UTC, because guessing would silently shift the whole
    inventory by the caller's offset.
    """
    if not isinstance(seed_instant, datetime):
        raise ValueError("seed_instant must be a datetime.")
    if seed_instant.tzinfo is None or seed_instant.utcoffset() is None:
        raise ValueError("seed_instant must be a timezone-aware datetime.")
    day = seed_instant.astimezone(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0)
    day += timedelta(days=_DEMO_LEAD_DAYS)
    return day + timedelta(days=(_DEMO_WEEKDAY - day.weekday()) % 7)

# Every seeded record is attributed to the demo's REAL League-Admin account,
# by its account id — not to a synthetic label (#367 owner ruling).
#
# This used to be the string ``"league_admin"``, which is a role NAME, not an
# account id: no account anywhere ever has it, so no session's ``user_id``
# could ever equal it. Under the creator-owned pending-link contract
# (``ApiService._creator_created_ids``) that made every seeded record with no
# validated Program link owned by NOBODY and therefore visible to NOBODY — the
# 17 seeded Officials who have neither a home Club with a Team nor a game
# assignment vanished from the Setup surface entirely, present in the store
# and unreachable in the UI. The ruling's remedy is to attribute the data to a
# real account, never to special-case a synthetic actor label in the read (a
# label bypass would re-open the installation-wide disclosure the ruling
# reversed, since any caller could then be handed rows nobody authenticated
# for).
#
# The value MUST stay equal to the ``account_id`` the demo bootstrap mints for
# the "admin" persona (``_seed_demo_accounts`` / ``_seed_admin_account`` in
# ``web/server.py``, both ``account_id="user_admin"``). Seeding order is
# store-then-accounts, so this id names an account that exists a moment later
# in the same atomic rebuild; ``tests/test_pending_link_ownership.py`` asserts
# the two really do match by resolving this actor against the seeded accounts,
# so a rename on either side fails there rather than silently orphaning the
# demo data again.
DEMO_ADMIN_ACCOUNT_ID = "user_admin"

_LIONS_SKATERS = [
    "Aarav M.", "Kabir S.", "Rohan P.", "Dev K.", "Neil R.", "Sam T.",
    "Leo V.", "Max W.", "Ivan O.", "Theo L.", "Finn B.", "Zane H.",
    "Owen C.", "Cole D.", "Jude E.", "Reid F.",
]
_FALCONS_SKATERS = [
    "Lukas B.", "Nico H.", "Emil R.", "Jonas K.", "Tim S.", "Ravi P.",
    "Omar D.", "Felix W.", "Noah V.", "Liam O.", "Adam T.", "Eli C.",
    "Yusuf A.", "Karl N.", "Ben F.", "Milo G.",
]


def build_full_demo_store(store=None, seed_instant=None
                          ) -> Tuple[InMemoryStore, str, dict]:
    """Return (store, game_id, ids) for the full E2E demo scenario.

    A store may be injected (e.g. a SqlStore) to seed any backend; defaults to
    a fresh in-memory store.

    ``seed_instant`` is the timezone-aware instant the whole ice inventory is
    laid out relative to (see :func:`demo_day_zero`). Omitted, it comes from
    the SetupService clock this seed already runs on — the seed path's single
    source of "now", the same one that stamps the audit rows written below —
    rather than a second, independently-drifting call to ``datetime.now()``.
    A test that needs exact dates passes the instant in and derives them with
    :func:`demo_day_zero`, which keeps the assertion exact instead of
    approximate.
    """
    store = store if store is not None else InMemoryStore()
    setup = SetupService(store)
    roster = RosterService(store)
    admin = DEMO_ADMIN_ACCOUNT_ID
    day = demo_day_zero(setup.clock() if seed_instant is None else seed_instant)

    # Facility owner that operates the league and owns its arenas (#166/#173) —
    # a rink company, distinct from a hockey Club. The league links up to it,
    # and its venues link to the league (deriving the same owner).
    org = setup.create_organization("Summit Ice Facilities", short_name="Summit",
                                    actor_id=admin)
    league = setup.create_program("Alpine Ice Hockey League", country="AT",
                                  operator_organization_id=org.id, actor_id=admin)
    season = setup.create_season(league.id, "2026–27 Winter Season",
                                 actor_id=admin)
    # A competitive league/grouping holds divisions inside the season (#166/#233).
    # The junior divisions sit under it; Senior A is left league-less on purpose
    # so the Setup hierarchy shows the "No level" bucket.
    junior_tier = setup.create_league(season.id, "Junior Tier", sort_order=1,
                                      actor_id=admin)
    d_u16 = setup.create_division(season.id, "U16 Elite", age_group="U16",
                                  league_id=junior_tier.id, actor_id=admin)
    d_u18 = setup.create_division(season.id, "U18 Development", age_group="U18",
                                  league_id=junior_tier.id, actor_id=admin)
    d_sen = setup.create_division(season.id, "Senior A", actor_id=admin)

    # A second League purely to demonstrate the client's own naming convention
    # (issue #245): Gold/Silver/Diamond are DIVISIONS within a League, never
    # Leagues themselves. No teams are registered here — a structural example
    # only, so it doesn't affect the pilot-scale team/player/game counts below.
    adult_league = setup.create_league(season.id, "Adult League", sort_order=2,
                                       actor_id=admin)
    setup.create_division(season.id, "Gold", league_id=adult_league.id,
                          actor_id=admin)
    setup.create_division(season.id, "Silver", league_id=adult_league.id,
                          actor_id=admin)
    setup.create_division(season.id, "Diamond", league_id=adult_league.id,
                          actor_id=admin)

    club_lions = setup.create_club("Lions HC", country="AT", actor_id=admin)
    club_falcons = setup.create_club("Falcons HC", country="AT", actor_id=admin)
    setup.create_club("Bears HC", country="AT", actor_id=admin)

    # A team plays in a season+division only via a SeasonTeamRegistration
    # (#180): scheduling, moves, publishing and standings all read the
    # registration, not Team.division_id. Register idempotently so re-running
    # or double-registering is a no-op.
    def _register(team_id, division_id):
        if not any(r.team_id == team_id
                   for r in setup.store.registrations_for_season(season.id)):
            setup.register_team_for_season(season.id, team_id, division_id,
                                           actor_id=admin)

    u16_lions = setup.create_team(club_lions.id, d_u16.id, "U16 Lions",
                                  actor_id=admin)
    u16_falcons = setup.create_team(club_falcons.id, d_u16.id, "U16 Falcons",
                                    actor_id=admin)
    setup.create_team(club_lions.id, d_u18.id, "U18 Lions", actor_id=admin)
    setup.create_team(club_lions.id, d_sen.id, "Senior Lions", actor_id=admin)
    # The core-scenario U16 teams play games below (before the round-robin's
    # bulk registration), so register them now.
    _register(u16_lions.id, d_u16.id)
    _register(u16_falcons.id, d_u16.id)

    # Both demo venues belong to the league (and, through it, the owner above).
    venue = setup.create_venue("Nord Arena", address="Alpine Way 1",
                               league_id=league.id, actor_id=admin)
    # #233 Slice E: game/ice eligibility is granted per Season via
    # SeasonVenueAccess, independent of the legacy Venue->Program bridge above.
    setup.grant_season_venue_access(season.id, venue.id, actor_id=admin)
    main_rink = setup.create_rink(venue.id, "Main Rink", actor_id=admin)
    setup.create_rink(venue.id, "Training Rink", actor_id=admin)

    setup.create_ice_slot(main_rink.id, day.replace(hour=16),
                          day.replace(hour=17, minute=30), actor_id=admin)
    slot_game = setup.create_ice_slot(main_rink.id, day.replace(hour=18, minute=30),
                                      day.replace(hour=20), actor_id=admin)
    setup.create_ice_slot(main_rink.id, day.replace(hour=20, minute=30),
                          day.replace(hour=22), actor_id=admin)

    game = setup.create_game(season.id, d_u16.id, u16_lions.id, u16_falcons.id,
                             slot_game.id, actor_id=admin)
    setup.publish_game(game.id, actor_id=admin)  # seeded game is public

    # Seed BOTH U16 teams with a full squad (distinct names) so any U16 game
    # (either team as home) has a rosterable roster.
    def seed_squad(team_id, goalie_name, names):
        goalie = setup.add_player(team_id, goalie_name, Position.GOALIE,
                                  jersey_number=30, actor_id=admin)
        out = []
        for i, name in enumerate(names, start=1):
            pos = Position.DEFENSE if i % 3 == 0 else Position.FORWARD
            out.append(setup.add_player(team_id, name, pos,
                                        jersey_number=i, actor_id=admin))
        return goalie, out

    goalie, skaters = seed_squad(u16_lions.id, "Gabe (G)", _LIONS_SKATERS)
    seed_squad(u16_falcons.id, "Marek (G)", _FALCONS_SKATERS)

    selected = [goalie.id] + [s.id for s in skaters[:15]]
    roster.select_roster(game.id, selected, actor_id="coach_lions")
    for pid in selected:
        roster.set_availability(game.id, pid, AvailabilityStatus.AVAILABLE)

    # A future draft game for the same team, left UNROSTERED, so the operator
    # can demonstrate real roster selection (manual picks + copy previous
    # roster) against an empty roster instead of the already-full seeded game.
    next_day = day + timedelta(days=7)
    slot_next = setup.create_ice_slot(main_rink.id,
                                      next_day.replace(hour=18, minute=30),
                                      next_day.replace(hour=20), actor_id=admin)
    game_next = setup.create_game(season.id, d_u16.id, u16_lions.id,
                                  u16_falcons.id, slot_next.id, actor_id=admin)

    # Officials pool + one assignment on the seeded game so the game sheet has
    # something to show (#30). Names are obviously fictional.
    ref = setup.create_official("Riley Whistle", actor_id=admin)
    setup.create_official("Lee Blueline", actor_id=admin)
    setup.create_official("Sky Tally", actor_id=admin)
    ref_assignment = setup.assign_official(game.id, ref.id, OfficialRole.REFEREE,
                                           actor_id=admin)

    # A finalized result on the seeded game so the demo standings are non-empty
    # (#31). Lions 4, Falcons 2.
    setup.record_result(game.id, 4, 2, actor_id=admin)
    setup.approve_result(game.id, actor_id=admin)

    # Pilot-scale data pack (#97) — purely additive, see module docstring.
    _seed_pilot_scale(setup, roster, admin, season, d_u16, d_u18, d_sen,
                      club_falcons, main_rink, league, day)

    ids = {
        "next_game_id": game_next.id,
        "referee_id": ref.id,
        "ref_assignment_id": ref_assignment.id,
        "league_id": league.id,
        "season_id": season.id,
        "division_id": d_u16.id,
        "game_id": game.id,
        "home_team_id": u16_lions.id,
        "away_team_id": u16_falcons.id,
        "main_rink_id": main_rink.id,
        "venue_id": venue.id,
        "substitute_player_id": skaters[15].id,   # unselected → can sub
        "selected_player_id": skaters[0].id,
    }
    return store, game.id, ids


# ---------------------------------------------------------------------------
# Pilot-scale data pack (#97)
#
# Names below are GENERATED, not hand-typed, so this stays maintainable at
# ~180-player scale — no `random`/timestamp source anywhere, so a fresh reset
# always produces the exact same dataset (deterministic, per CLAUDE.md).
# ---------------------------------------------------------------------------

_PLAYER_FIRST_NAMES = [
    "Aarav", "Kabir", "Rohan", "Dev", "Neil", "Sam", "Leo", "Max", "Ivan", "Theo",
    "Finn", "Zane", "Owen", "Cole", "Jude", "Reid", "Lukas", "Nico", "Emil", "Jonas",
    "Tim", "Ravi", "Omar", "Felix", "Noah", "Liam", "Adam", "Eli", "Yusuf", "Karl",
    "Ben", "Milo", "Arlo", "Hugo", "Jesse", "Kai", "Levi", "Miles", "Nate", "Otto",
    "Pax", "Quinn", "Ryo", "Silas", "Tobias", "Uri", "Viggo", "Wyatt", "Xavier", "Yannick",
]
_LAST_INITIALS = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")

# The first 3 entries match the officials already seeded above (Riley
# Whistle, Lee Blueline, Sky Tally) so _seed_pilot_scale can just skip them
# instead of keeping a second, easily-drifting name list in sync.
_OFFICIAL_FIRST_NAMES = [
    "Riley", "Lee", "Sky", "Casey", "Jordan", "Drew", "Morgan", "Taylor", "Alex",
    "Sam", "Jamie", "Robin", "Charlie", "Frankie", "Bailey", "Reese", "Skyler",
    "Rowan", "Blake", "Ellis", "Dakota", "Sage", "Remy", "Shiloh",
]
_OFFICIAL_SURNAMES = [
    "Whistle", "Blueline", "Tally", "Icecheck", "Faceoff", "Redline", "Crease",
    "Offside", "Powerplay", "Penalty", "Zamboni", "Boarding", "Deke", "Slapshot",
    "Hattrick", "Crossbar", "Icetime", "Backcheck", "Wraparound", "Onetimer",
    "Breakaway", "Neutral", "Overtime", "Shootout",
]


def _squad_names(count: int, offset: int):
    """`count` deterministic, obviously-fictional "First I." names, drawing
    from `offset` onward in the shared name space so different squads don't
    repeat the same combo (50 first names × 26 initials = 1,300 combos, far
    more than the ~184 players this pack ever needs)."""
    names = []
    for i in range(count):
        idx = offset + i
        first = _PLAYER_FIRST_NAMES[idx % len(_PLAYER_FIRST_NAMES)]
        initial = _LAST_INITIALS[(idx // len(_PLAYER_FIRST_NAMES)) % len(_LAST_INITIALS)]
        names.append(f"{first} {initial}.")
    return names


def _round_robin_pairs(team_ids, rounds):
    """(home, away) pairs for `rounds` passes over `team_ids`, alternating
    home/away each pass. A short division (e.g. Senior A's 2 teams) simply
    plays the same rival every round — same as a real small division would."""
    pairs = []
    n = len(team_ids)
    for r in range(rounds):
        for i in range(0, n - 1, 2):
            a, b = team_ids[i], team_ids[i + 1]
            pairs.append((a, b) if r % 2 == 0 else (b, a))
    return pairs


def _seed_pilot_scale(setup, roster, admin, season, d_u16, d_u18, d_sen,
                      club_falcons, main_rink, league, day):
    """Fill the store out to pilot scale on top of the core scenario above:
    12 teams, ~184 players, 24 officials, 3 rinks across 2 venues, two weeks
    of ice inventory, and ~18 more published games (~19-20 total) with a
    couple of officiating conflicts and player non-responses mixed in.

    ``day`` is the core scenario's own day zero (:func:`demo_day_zero`), so
    this pack's inventory stays welded to it instead of carrying a second,
    independently-drifting anchor of its own.
    """
    store = setup.store
    club_bears = next(c for c in store.all_clubs() if c.name == "Bears HC")
    club_wolves = setup.create_club("Wolves HC", country="AT", actor_id=admin)
    club_comets = setup.create_club("Comets HC", country="AT", actor_id=admin)
    club_panthers = setup.create_club("Panthers HC", country="AT", actor_id=admin)
    club_sharks = setup.create_club("Sharks HC", country="AT", actor_id=admin)

    u16_wolves = setup.create_team(club_wolves.id, d_u16.id, "U16 Wolves", actor_id=admin)
    u16_comets = setup.create_team(club_comets.id, d_u16.id, "U16 Comets", actor_id=admin)
    u16_panthers = setup.create_team(club_panthers.id, d_u16.id, "U16 Panthers", actor_id=admin)
    u16_sharks = setup.create_team(club_sharks.id, d_u16.id, "U16 Sharks", actor_id=admin)

    u18_lions = next(t for t in store.all_teams() if t.name == "U18 Lions")
    u18_falcons = setup.create_team(club_falcons.id, d_u18.id, "U18 Falcons", actor_id=admin)
    u18_wolves = setup.create_team(club_wolves.id, d_u18.id, "U18 Wolves", actor_id=admin)
    u18_bears = setup.create_team(club_bears.id, d_u18.id, "U18 Bears", actor_id=admin)

    senior_lions = next(t for t in store.all_teams() if t.name == "Senior Lions")
    senior_falcons = setup.create_team(club_falcons.id, d_sen.id, "Senior Falcons",
                                       actor_id=admin)

    # Squads: a goalie + 14 skaters for every NEW or previously-empty team
    # (15 each). U16 Lions/Falcons already have full 17-player squads from
    # the core scenario above and are left untouched.
    name_offset = 0
    for team in (u18_lions, senior_lions, u16_wolves, u16_comets, u16_panthers,
                u16_sharks, u18_falcons, u18_wolves, u18_bears, senior_falcons):
        skater_names = _squad_names(14, name_offset)
        name_offset += 20  # gap so consecutive squads never share a combo
        setup.add_player(team.id, f"{skater_names[0].split()[0]} (G)",
                         Position.GOALIE, jersey_number=30, actor_id=admin)
        for i, name in enumerate(skater_names, start=1):
            pos = Position.DEFENSE if i % 3 == 0 else Position.FORWARD
            setup.add_player(team.id, name, pos, jersey_number=i, actor_id=admin)

    # Officials: 24 total. The first 3 (Riley Whistle/Lee Blueline/Sky
    # Tally) already exist from the core scenario above; add the other 21.
    officials = list(store.all_officials())
    for first, last in list(zip(_OFFICIAL_FIRST_NAMES, _OFFICIAL_SURNAMES))[3:]:
        officials.append(setup.create_official(f"{first} {last}", actor_id=admin))

    # A second venue/rink so the arena calendar spans more than one arena —
    # 3 rinks total across 2 venues.
    venue2 = setup.create_venue("Lakeside Ice Center", address="2 Harbor Rd",
                                league_id=league.id, actor_id=admin)
    setup.grant_season_venue_access(season.id, venue2.id, actor_id=admin)
    lakeside_rink = setup.create_rink(venue2.id, "Lakeside Rink", actor_id=admin)
    training_rink = next(r for r in store.all_rinks() if r.name == "Training Rink")
    rinks = [main_rink, training_rink, lakeside_rink]

    # Two weeks of ice inventory across all 3 rinks, starting the day after
    # day zero: one evening GAME slot per rink per day, plus a practice slot
    # every 4th day for variety.
    # Defensive: main_rink already has core-scenario bookings on a couple of
    # these days (e.g. the #93-style "next game" slot on day zero + 7, which
    # is offset 6 here), so a slot that would overlap an existing one on the
    # same rink is simply skipped rather than allowed to crash server startup
    # on a collision.
    start = day + timedelta(days=1)
    game_slots = []
    for offset in range(14):
        d = start + timedelta(days=offset)
        for rink in rinks:
            try:
                slot = setup.create_ice_slot(
                    rink.id, d.replace(hour=18), d.replace(hour=19, minute=30),
                    actor_id=admin)
                game_slots.append(slot)
            except DomainError:
                pass
            if offset % 4 == 0:
                try:
                    setup.create_ice_slot(
                        rink.id, d.replace(hour=16), d.replace(hour=17, minute=15),
                        slot_type=IceSlotType.PRACTICE, actor_id=admin)
                except DomainError:
                    pass

    # ~18 more published games across all three divisions, drawn from the
    # ice inventory above. Wrapped defensively for the same reason as the
    # ice slots: a seed script should never crash the whole server over a
    # scheduling edge case, so any conflict just skips that pairing.
    #
    # Deliberately EXCLUDES u16_lions/u16_falcons: they're the core
    # scenario's own teams, and a bunch of existing tests rely on their
    # game history being EXACTLY the two core-scenario games (e.g. "copy
    # previous roster" finds the most recent PRIOR game for a team — adding
    # more games for these two specific teams silently changes which game
    # that resolves to elsewhere in the suite). Every new game below is
    # between teams this pack itself created (or a previously-empty core
    # team like U18 Lions/Senior Lions that had zero games to begin with,
    # so giving them some is a pure addition, not a behavior change).
    matchups = (
        _round_robin_pairs([u16_wolves.id, u16_comets.id,
                           u16_panthers.id, u16_sharks.id], 3)
        + _round_robin_pairs([u18_lions.id, u18_falcons.id, u18_wolves.id,
                             u18_bears.id], 3)
        + _round_robin_pairs([senior_lions.id, senior_falcons.id], 6)
    )
    division_of = {t.id: d_u16.id for t in
                  (u16_wolves, u16_comets, u16_panthers, u16_sharks)}
    division_of.update({t.id: d_u18.id for t in
                       (u18_lions, u18_falcons, u18_wolves, u18_bears)})
    division_of.update({t.id: d_sen.id for t in (senior_lions, senior_falcons)})
    # Register every round-robin team into its season+division before any game
    # is scheduled for it (#180 shared guard). Idempotent so a team already
    # registered by the core scenario is skipped.
    for _tid, _did in division_of.items():
        if not any(r.team_id == _tid
                   for r in setup.store.registrations_for_season(season.id)):
            setup.register_team_for_season(season.id, _tid, _did, actor_id=admin)

    # Every rink's slot on a given day shares the same evening time-of-day,
    # so two games on the same day are effectively simultaneous regardless
    # of rink — a team can appear in at most one of them. Round-robin pairs
    # from adjacent rounds don't align to day boundaries (e.g. a 4-team
    # division plays 2 pairs/round against 3 same-day rink slots), so
    # walking `game_slots` in raw generation order can hand the same team a
    # second "simultaneous" game later the same day. Track which teams are
    # already booked per day and skip forward to a day where neither team
    # has a game yet, instead of relying solely on create_game's own
    # conflict check (which would just as correctly reject it, but as a
    # skipped pairing rather than a scheduled one — this way the ice
    # inventory's day-spread is actually used instead of losing a third of
    # the intended games to same-day collisions).
    # Seeded from the ALREADY-existing games too (the core scenario's two
    # U16 Lions/Falcons games above), not just the ones this function
    # creates, so a new pairing can never be handed a day one of its teams
    # is already booked on, however that booking got there.
    booked_teams_by_day = {}
    for existing in store.all_games():
        if existing.cancelled:
            continue
        day_key = existing.start_time.date()
        booked_teams_by_day.setdefault(day_key, set()).update(
            {existing.home_team_id, existing.away_team_id} - {None})
    used_slot_ids = set()
    new_games = []
    for home_id, away_id in matchups:
        chosen = None
        for slot in game_slots:
            if slot.id in used_slot_ids:
                continue
            day_key = slot.start_time.date()
            day_teams = booked_teams_by_day.setdefault(day_key, set())
            if home_id in day_teams or away_id in day_teams:
                continue
            chosen = slot
            break
        if chosen is None:
            break  # ice inventory exhausted for this pairing
        used_slot_ids.add(chosen.id)
        booked_teams_by_day[chosen.start_time.date()].update((home_id, away_id))
        try:
            g = setup.create_game(season.id, division_of[home_id], home_id, away_id,
                                  chosen.id, actor_id=admin)
            setup.publish_game(g.id, actor_id=admin)
            new_games.append(g)
        except DomainError:
            continue

    # Officiating: rotate the NEW officials (skip the first 3 — Riley
    # Whistle/Lee Blueline/Sky Tally are the core scenario's own officials,
    # and existing tests rely on their assignment/availability history being
    # exactly what the core scenario above established) across the first
    # several new games, and give two of them a declared-unavailable window
    # that overlaps a game they're ALREADY assigned to — a realistic "arena
    # manager needs to resolve this" conflict, seeded in the same order a
    # real one would arise (assign first, the official's plans change
    # afterward — assigning into an already-declared-unavailable window is
    # blocked outright, so this is the only order that produces the
    # conflict instead of an error).
    new_officials = officials[3:]
    for i, g in enumerate(new_games[:6]):
        official = new_officials[i % len(new_officials)]
        try:
            setup.assign_official(g.id, official.id, OfficialRole.REFEREE,
                                  actor_id=admin)
        except DomainError:
            continue
        if i < 2:
            setup.set_official_availability(
                official.id, g.start_time, g.end_time,
                OfficialAvailabilityStatus.UNAVAILABLE,
                note="Schedule conflict — needs cover", actor_id=admin)

    # Roster + some player no-responses: select a roster for the first few
    # new games but leave a couple of players in PENDING (never responded)
    # instead of confirming everyone, so the roster screen shows that real
    # state too, not just a fully-confirmed squad.
    for g in new_games[:3]:
        home_players = [p for p in store.all_players() if p.team_id == g.home_team_id]
        away_players = [p for p in store.all_players() if p.team_id == g.away_team_id]
        selected = [p.id for p in (home_players[:15] + away_players[:15])]
        if not selected:
            continue
        roster.select_roster(g.id, selected, actor_id="coach_demo")
        for j, pid in enumerate(selected):
            if j % 6 == 0:
                continue  # left as PENDING — a player who hasn't responded
            roster.set_availability(g.id, pid, AvailabilityStatus.AVAILABLE)
