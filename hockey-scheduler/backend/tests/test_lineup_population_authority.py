"""PR #427 blocker (owner comments 5390696775 / 5394947899) — the lineup
population comes from the four AUTHORITIES, never from ``Player.team_id``.

THE DEFECT THIS FILE PINS, in the owner's own words::

    "There is still a separate current-head blocker in
    `ApiService._lineup_rows`: both authenticated
    GET /api/games/{id}/lineups and GET /api/games/{id}/board enumerate
    `store.players_for_team(team_id)`, so they continue to use mutable
    `Player.team_id` instead of the game's season authority... Both endpoints
    returned 200 to an authorized HOME Coach, omitted the current member, and
    included the departed player."

REPRODUCED RED at head 337374a on Memory, SQLite and real PostgreSQL, over a
real authenticated Coach session on a real socket, in TWO contradictions on
ONE fixture — and the second is sharper than the reported one:

* ACROSS ENDPOINTS. In the same session, on the same fixture, the
  already-cut-over ``/availability-summary`` named
  ``[Current Member, Enrolled Sub, Legacy Seat, Legacy Sub]`` while
  ``/lineups`` and ``/board`` named
  ``[Departed Player, Away Member, Pointer Ghost]``. The two sets were not
  merely inconsistent, they were DISJOINT: every player one endpoint named
  was absent from the other.

* WITHIN ONE RESPONSE. ``home.status`` reported ``open_skater_slots=1``
  against ``target_skaters=3`` — two durably seated bodies — and
  ``substitutes_enrolled=2``, while ``home.players`` in that SAME JSON
  document listed ZERO ``selected`` rows, ZERO ``substitute`` rows, and three
  strangers marked ``available``. ``status`` was computed from
  ``_side_data``'s durable/live authorities; ``players`` from the permanent
  pointer. One response, two irreconcilable answers about who is on this
  team.

The private fields disclosed for each wrongly-included player were
``id, name, position, slot_type, jersey_number, group, roster_status,
backed_out, availability, sub_status`` — and the position/jersey values were
the PERMANENT ones (``forward``/#18) rather than this game's seasonal ones.

THE FOUR POPULATIONS, and the authority each one answers to
(``RosterService.lineup_population``, which this file exercises directly and
through the facade):

  (a) selected rows       -> ``GameRosterEntry.attribution``  (DURABLE)
  (b) active substitutes  -> ``SubstituteEnrollment.team_id``  (DURABLE)
  (c) unseated candidates -> the live game-season membership  (LIVE)
  (d) unbound exhibition  -> the permanent pointer, on an EXPLICIT branch

THE TRAP THIS FILE ALSO PINS. ``_side_data.matched_entries`` is NOT a
substitute for (a). It deliberately charges a pre-061 NULL-attribution row to
EVERY side, in BOTH buckets, because for SLOT ACCOUNTING over-refusing can
only close slots and never reopen them. Verified tri-store below:
``_side_data(HOME).matched_entries == _side_data(AWAY).matched_entries`` for
such a row. Feeding that list into a READ would place ONE player in BOTH
``home.players`` and ``away.players`` of a single response — a new cross-side
leak manufactured out of the fix for one.
``TheLegacyNullRowsAreOnNeitherSide`` is the regression that would catch it.

MOVER-SHAPED OR BLIND. Every fixture here whose property is about DISCOVERY
uses the MOVER shape — the permanent ``Player.team_id`` pointer and the
seasonal membership deliberately name DIFFERENT teams — and ``_mover``
asserts it. A fixture whose pointer agrees with its membership is satisfied
by pointer discovery and membership discovery alike, which is precisely the
blindness the owner identified in an earlier round's tests and precisely why
the demo fixtures (``SetupService._mirror_memberships_for_new_player`` opens
a parity membership for every facade-created player) could not see this
blocker at all. The permanent POSITION and JERSEY disagree with the seasonal
ones too, for the same reason.

EXECUTABLE FALSIFIERS, not claims. ``_falsified`` reintroduces one specific
defect into the live code and REQUIRES the assertion body to fail:

  * ``players_for_team`` discovery (the owner's named falsification),
  * ``Player.position`` restored on a bound game,
  * ``Player.jersey_number`` restored on a bound game,
  * ``_side_data.matched_entries`` used as the seated population.

A falsifier that does not break its test is itself a failure, reported by
name — so a future edit that stops testing the property cannot pass quietly.

TRI-STORE, PROVEN. ``_stores`` yields Memory, SQLite and — when
TEST_DATABASE_URL is set — real PostgreSQL; ``_assert_backend`` PROVES each
backend rather than trusting the env var, and ``_assert_matrix_ran`` fails a
loop that silently covered fewer backends than were configured. A SKIP IS NOT
A PASS.
"""

import contextlib
import unittest

from helpers import BACKEND, end_membership_directly  # noqa: F401
from test_game_league_season_authority import _Authority
from test_substitute_membership_cutover import ADMIN

from hockey_scheduler.api.service import ApiService
from hockey_scheduler.domain import Player, Position, SubstituteStatus
from hockey_scheduler.services.roster_service import RosterService

# The permanent values every MOVER carries, and the seasonal ones that
# deliberately contradict them. Kept as module constants so a test can say
# "this is the PERMANENT number" without restating it.
PERM_POSITION = Position.FORWARD
SEASON_POSITION = Position.DEFENSE


class _LineupAuthority(_Authority):
    """``_Authority``'s tri-store bound-game fixture plus the lineup-specific
    constructors and reads."""

    _seq = 0

    # -- constructors ----------------------------------------------------
    def _mover(self, fx, name, pointer, membership, status="active",
               season_position=SEASON_POSITION):
        """A player whose PERMANENT pointer names ``pointer`` and whose
        seasonal membership on THIS game's exact LeagueSeason names
        ``membership`` — asserted to DISAGREE — and whose PERMANENT
        position/jersey deliberately disagree with the seasonal ones.

        Both asserts are the guard, not decoration. If a future edit makes
        either pair agree, this fails loudly here rather than quietly passing
        a test that has stopped being able to tell the two authorities
        apart."""
        assert pointer != membership, (
            f"{name} is not a MOVER: pointer and membership both name "
            f"{pointer}, so no assertion below could falsify pointer-based "
            "discovery.")
        assert season_position != PERM_POSITION, (
            f"{name}'s seasonal position equals the permanent one, so no "
            "assertion below could falsify a permanent-field fallback.")
        type(self)._seq += 1
        n = type(self)._seq
        perm_jersey, season_jersey = 10 + n, 50 + n
        api = fx["api"]
        p = Player(id=api.store.next_id("player"), team_id=pointer, name=name,
                   position=PERM_POSITION, jersey_number=perm_jersey)
        api.store.add_player(p)
        m = api.create_season_roster_membership(
            p.id, fx["ls_id"], membership, status=status,
            position=season_position, jersey_number=season_jersey,
            actor_id=ADMIN)
        assert "error" not in m, m
        return {"id": p.id, "name": name, "membership_id": m["id"],
                "perm_jersey": perm_jersey, "season_jersey": season_jersey}

    def _pointer_only(self, fx, name, pointer):
        """Pointer set, seasonal record SILENT — the bulk-import shape. On a
        BOUND game this player has no membership authority at all, so they
        are on nobody's lineup; on an UNBOUND game the pointer is all there
        is."""
        type(self)._seq += 1
        n = type(self)._seq
        api = fx["api"]
        p = Player(id=api.store.next_id("player"), team_id=pointer, name=name,
                   position=PERM_POSITION, jersey_number=10 + n)
        api.store.add_player(p)
        return {"id": p.id, "name": name, "perm_jersey": 10 + n}

    # -- the reads under test --------------------------------------------
    def _rows(self, fx, team_id, game=None):
        """The facade rows for one side — what the HTTP body carries."""
        game = game or fx["api"].store.get_game(fx["gid"])
        return fx["api"]._lineup_rows(game, team_id)

    def _ids(self, fx, team_id, game=None):
        return [r["id"] for r in self._rows(fx, team_id, game)]

    def _by_group(self, fx, team_id, game=None):
        out = {}
        for r in self._rows(fx, team_id, game):
            out.setdefault(r["group"], []).append(r["id"])
        return out

    def _row_for(self, fx, team_id, player_id, game=None):
        for r in self._rows(fx, team_id, game):
            if r["id"] == player_id:
                return r
        return None

    def _pointer_pool(self, fx, team_id):
        return sorted(p.id for p in fx["api"].store.players_for_team(team_id))

    # -- the executable falsifiers ---------------------------------------
    @contextlib.contextmanager
    def _falsified(self, kind):
        """Reintroduce ONE named defect into the LIVE code for the duration
        of the block. Patched on the CLASS and restored in ``finally``.

        Each entry names the exact regression the assertions under it are
        supposed to catch; :meth:`_require_falsifier_breaks` runs the
        assertion body under it and fails if it still passes."""
        if kind == "players_for_team":
            # The owner's named falsification: discovery back on the
            # permanent pointer, which is what the shipped code did.
            def population(self, game, team_id):
                entries = {e.player_id: e
                           for e in self.store.roster_for_game(game.id)}
                subs = {s.player_id: s
                        for s in self.store.substitutes_for_game(game.id)}
                return self._unbound_lineup_population(
                    game, team_id, entries, subs)
            target, attr, patch = RosterService, "lineup_population", population
        elif kind == "permanent_position":
            real = ApiService._lineup_rows

            def rows(self, game, team_id):
                out = real(self, game, team_id)
                by_id = {r.player.id: r.player
                         for r in self.roster.lineup_population(game, team_id)}
                for row in out:
                    player = by_id[row["id"]]
                    row["position"] = player.position.value
                    row["slot_type"] = player.slot_type.value
                return out
            target, attr, patch = ApiService, "_lineup_rows", rows
        elif kind == "permanent_jersey":
            def season_jersey(ctx):
                return None if ctx is None else ctx.player.jersey_number
            target, attr, patch = (RosterService, "_season_jersey",
                                   staticmethod(season_jersey))
        elif kind == "side_data_entries":
            # THE TRAP: seated population taken from `_side_data`, which
            # charges a NULL-attribution row to BOTH sides.
            real_pop = RosterService.lineup_population

            def population(self, game, team_id):
                rows = list(real_pop(self, game, team_id))
                have = {r.player.id for r in rows}
                _s, matched, _subs = self._side_data(game.id, team_id)
                from hockey_scheduler.services.roster_service import LineupRow
                for entry, player in matched:
                    if player.id in have:
                        continue
                    rows.append(LineupRow(
                        player=player, source="roster",
                        position=player.position,
                        jersey_number=player.jersey_number, entry=entry,
                        enrollment=None, context=None, eligible=True))
                return rows
            target, attr, patch = RosterService, "lineup_population", population
        else:  # pragma: no cover - a typo in a falsifier name must be loud
            raise AssertionError(f"unknown falsifier {kind!r}")
        original = target.__dict__[attr]
        setattr(target, attr, patch)
        try:
            yield
        finally:
            setattr(target, attr, original)

    def _require_falsifier_breaks(self, kind, body, label):
        """Run ``body`` under falsifier ``kind`` and REQUIRE it to fail.

        A falsifier that leaves the test green is a test that has stopped
        proving its property, and it is reported by name rather than
        silently tolerated."""
        with self._falsified(kind):
            try:
                body()
            except AssertionError:
                return
        self.fail(
            f"FALSIFIER '{kind}' did not break {label}: the assertions "
            "still passed with the defect reintroduced, so they do not "
            "actually pin this property.")

    # -- fixture ----------------------------------------------------------
    def _fixture(self, store):
        """One bound game, and every shape the ruling enumerates.

        Returns the ``_Authority`` fixture dict with ``people`` added."""
        # Jersey numbers are per-fixture, not per-process: they must stay
        # inside the 1..98 the domain accepts AND unique per (LeagueSeason,
        # team), and each backend rebuilds the whole world from scratch.
        type(self)._seq = 0
        fx = self._build(store, target_skaters=8)
        api = fx["api"]
        HOME, AWAY, THIRD = fx["home"], fx["away"], fx["third"]
        p = {}

        # (a) a DURABLY SEATED current member. Pointer THIRD, membership HOME.
        p["seated"] = self._mover(fx, "Seated Member", THIRD, HOME)
        # (b) an ENROLLED substitute, durably owned by HOME.
        p["enrolled"] = self._mover(fx, "Enrolled Sub", THIRD, HOME)
        # (b) an OFFERED substitute, durably owned by HOME.
        p["offered"] = self._mover(fx, "Offered Sub", THIRD, HOME)
        # (c) a live unseated candidate.
        p["candidate"] = self._mover(fx, "Live Candidate", THIRD, HOME)
        # A DEPARTED player: pointer still names HOME, membership moved to
        # THIRD. On neither of this game's sides.
        p["departed"] = self._mover(fx, "Departed Player", HOME, THIRD)
        # The OPPONENT's member: pointer HOME, membership AWAY.
        p["awayside"] = self._mover(fx, "Away Member", HOME, AWAY)
        # A STALE POINTER with no seasonal record at all.
        p["ghost"] = self._pointer_only(fx, "Pointer Ghost", HOME)

        assert "error" not in api.select_roster(
            fx["gid"], [p["seated"]["id"]], actor_id=ADMIN)
        assert "error" not in api.enroll_substitute(
            fx["gid"], p["enrolled"]["id"], actor_id=ADMIN)
        assert "error" not in api.enroll_substitute(
            fx["gid"], p["offered"]["id"], actor_id=ADMIN)
        assert "error" not in api.offer_substitute(
            fx["gid"], p["offered"]["id"], actor_id=ADMIN)

        # LEGACY, pre-061: a seated row with NO durable attribution, whose
        # occupant IS still a live HOME member.
        p["legacy_seat"] = self._mover(fx, "Legacy Seat", THIRD, HOME)
        assert "error" not in api.select_roster(
            fx["gid"], [p["legacy_seat"]["id"]], actor_id=ADMIN)
        self._strip_attribution(api, fx["gid"], p["legacy_seat"]["id"])
        # LEGACY, pre-060: an ENROLLED row with NO durable owner, whose
        # occupant IS still a live HOME member.
        p["legacy_sub"] = self._mover(fx, "Legacy Sub", THIRD, HOME)
        assert "error" not in api.enroll_substitute(
            fx["gid"], p["legacy_sub"]["id"], actor_id=ADMIN)
        self._strip_sub_owner(api, fx["gid"], p["legacy_sub"]["id"])

        # The same two legacy shapes whose occupant is NOT a live member of
        # either side any more — nothing anywhere can name a side for these,
        # so they must be on NEITHER response.
        p["orphan_seat"] = self._mover(fx, "Orphan Seat", THIRD, HOME)
        assert "error" not in api.select_roster(
            fx["gid"], [p["orphan_seat"]["id"]], actor_id=ADMIN)
        self._strip_attribution(api, fx["gid"], p["orphan_seat"]["id"])
        end_membership_directly(api.store, p["orphan_seat"]["membership_id"])
        p["orphan_sub"] = self._mover(fx, "Orphan Sub", THIRD, HOME)
        assert "error" not in api.enroll_substitute(
            fx["gid"], p["orphan_sub"]["id"], actor_id=ADMIN)
        self._strip_sub_owner(api, fx["gid"], p["orphan_sub"]["id"])
        end_membership_directly(api.store, p["orphan_sub"]["membership_id"])

        fx["people"] = p
        return fx

    @staticmethod
    def _strip_attribution(api, game_id, player_id):
        """Make a seated row look pre-061: both durable columns NULL. Written
        at the STORE, because no service path can produce one any more."""
        with api.store.transaction():
            e = api.store.roster_entry_for_player(game_id, player_id)
            e.team_side = None
            e.seated_position = None
            api.store.save_roster_entry(e)
        assert api.store.roster_entry_for_player(
            game_id, player_id).attribution is None

    @staticmethod
    def _strip_sub_owner(api, game_id, player_id):
        """Make an enrollment look pre-060: ``team_id`` NULL."""
        with api.store.transaction():
            s = api.store.substitute_for_player(game_id, player_id)
            s.team_id = None
            api.store.save_substitute(s)
        assert api.store.substitute_for_player(
            game_id, player_id).team_id is None


# ---------------------------------------------------------------------------
# 1. THE OWNER'S OWN SENTENCE: the current member is present, the departed
#    player is absent, on every backend.
# ---------------------------------------------------------------------------
class MembershipNotThePointerDecidesTheLineup(_LineupAuthority,
                                              unittest.TestCase):
    """"omitted the current member, and included the departed player" —
    both halves, in both mirrored directions, over the facade rows the HTTP
    body carries.

    FIXTURE SHAPE: MOVER, mirrored. The pointer pool and the membership pool
    of HOME are asserted DISJOINT, which is what makes every identity
    assertion below a real choice between the two authorities rather than a
    coincidence."""

    def test_the_lineup_names_the_members_not_the_pointer_pool(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                p = fx["people"]

                def body():
                    home = self._ids(fx, fx["home"])
                    # THE PREMISE. If these overlapped, the assertions below
                    # could be satisfied by pointer discovery.
                    pointer = set(self._pointer_pool(fx, fx["home"]))
                    self.assertEqual(
                        pointer,
                        {p["departed"]["id"], p["awayside"]["id"],
                         p["ghost"]["id"]},
                        "fixture: HOME's pointer pool is not the departed set")
                    self.assertEqual(
                        pointer & set(home), set(),
                        "fixture: pointer pool and lineup overlap, so this "
                        "cannot falsify pointer discovery")
                    # THE CURRENT MEMBERS ARE PRESENT — by exact identity.
                    self.assertEqual(
                        set(home),
                        {p["seated"]["id"], p["enrolled"]["id"],
                         p["offered"]["id"], p["candidate"]["id"],
                         p["legacy_seat"]["id"], p["legacy_sub"]["id"]},
                        f"[{label}] HOME lineup identities")
                    # THE DEPARTED PLAYER IS ABSENT — named explicitly, so a
                    # failure says which player leaked.
                    self.assertNotIn(p["departed"]["id"], home,
                                     f"[{label}] departed player is listed")
                    self.assertNotIn(p["awayside"]["id"], home,
                                     f"[{label}] opponent member is listed")
                    self.assertNotIn(p["ghost"]["id"], home,
                                     f"[{label}] stale-pointer player listed")
                    # THIRD is in nobody's lineup: a team not in this game.
                    with self.assertRaises(Exception):
                        fx["api"].roster.lineup_population(
                            fx["api"].store.get_game(fx["gid"]), fx["third"])
                    # THE MIRROR: AWAY names its own member and not the
                    # pointer-HOME body that moved there.
                    away = self._ids(fx, fx["away"])
                    self.assertEqual(set(away), {p["awayside"]["id"]},
                                     f"[{label}] AWAY lineup identities")
                    self.assertEqual(self._pointer_pool(fx, fx["away"]), [],
                                     "fixture: AWAY's pointer pool is empty, "
                                     "so AWAY's member is found only by "
                                     "membership")

                with self.subTest(backend=label):
                    body()
                    self._require_falsifier_breaks(
                        "players_for_team", body,
                        f"[{label}] the membership-not-pointer identities")
                ran.append((label, "identities"))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, ["identities"])


# ---------------------------------------------------------------------------
# 2. THE FOUR POPULATIONS, each keyed on its own authority.
# ---------------------------------------------------------------------------
class EachPopulationAnswersToItsOwnAuthority(_LineupAuthority,
                                             unittest.TestCase):
    """Durable seating, durable enrollment ownership and live membership are
    three different questions, and each row is grouped by the one that put it
    there — with ACCEPTED deliberately NOT a fourth population."""

    def test_the_groups_match_the_authorities_on_every_backend(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                p, api = fx["people"], fx["api"]
                with self.subTest(backend=label):
                    groups = self._by_group(fx, fx["home"])
                    # (a) DURABLE ATTRIBUTION -> "selected"
                    self.assertEqual(groups.get("selected"),
                                     [p["seated"]["id"]], groups)
                    # (b) DURABLE ENROLLMENT OWNERSHIP -> "substitute",
                    #     ENROLLED and OFFERED alike.
                    self.assertEqual(
                        sorted(groups.get("substitute", [])),
                        sorted([p["enrolled"]["id"], p["offered"]["id"]]),
                        groups)
                    self.assertEqual(
                        {self._row_for(fx, fx["home"],
                                       p["enrolled"]["id"])["sub_status"],
                         self._row_for(fx, fx["home"],
                                       p["offered"]["id"])["sub_status"]},
                        {"enrolled", "offered"})
                    # (c) LIVE MEMBERSHIP -> "available"
                    self.assertEqual(
                        sorted(groups.get("available", [])),
                        sorted([p["candidate"]["id"], p["legacy_seat"]["id"],
                                p["legacy_sub"]["id"]]), groups)

                    # ACCEPTED IS NOT A FOURTH POPULATION. Accepting writes a
                    # GameRosterEntry with a durable side, so the body moves
                    # from (b) to (a) — one row, never two.
                    accepted = api.accept_substitute(
                        fx["gid"], p["offered"]["id"], actor_id=ADMIN)
                    self.assertNotIn("error", accepted, accepted)
                    self.assertEqual(
                        api.store.substitute_for_player(
                            fx["gid"], p["offered"]["id"]).status,
                        SubstituteStatus.ACCEPTED)
                    groups = self._by_group(fx, fx["home"])
                    self.assertIn(p["offered"]["id"],
                                  groups.get("selected", []), groups)
                    self.assertNotIn(p["offered"]["id"],
                                     groups.get("substitute", []), groups)
                    self.assertEqual(
                        [r["id"] for r in self._rows(fx, fx["home"])].count(
                            p["offered"]["id"]), 1,
                        "an accepted substitute appeared twice")
                ran.append((label, "populations"))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, ["populations"])


# ---------------------------------------------------------------------------
# 3. THE LEGACY NULL ROWS: on NEITHER side, never on both.
# ---------------------------------------------------------------------------
class TheLegacyNullRowsAreOnNeitherSide(_LineupAuthority, unittest.TestCase):
    """Owner ruling (comment 5394947899): "A legacy active row with
    ``team_id IS NULL`` belongs to neither side response: do not feed it
    through ``_side_data.matched_entries``, do not place it on both sides, and
    do not attach an attribution marker to one guessed side. If the player
    independently has a current valid membership, they may still appear
    through the live unseated-candidate path."

    All four clauses are separate assertions here, and the ``side_data_entries``
    falsifier reintroduces exactly the mistake the first clause forbids."""

    def test_a_null_owner_row_is_on_neither_side_on_every_backend(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                p, api = fx["people"], fx["api"]
                game = api.store.get_game(fx["gid"])

                def body():
                    home = self._rows(fx, fx["home"])
                    away = self._rows(fx, fx["away"])
                    home_ids = {r["id"] for r in home}
                    away_ids = {r["id"] for r in away}

                    # NEVER DUPLICATED ACROSS SIDES.
                    self.assertEqual(home_ids & away_ids, set(),
                                     f"[{label}] a player is on BOTH sides")

                    # The two whose occupant has NO live membership are on
                    # neither side at all — nothing can name a side for them.
                    for key in ("orphan_seat", "orphan_sub"):
                        self.assertNotIn(p[key]["id"], home_ids,
                                         f"[{label}] {key} on HOME")
                        self.assertNotIn(p[key]["id"], away_ids,
                                         f"[{label}] {key} on AWAY")

                    # The two whose occupant IS independently live appear —
                    # but ONLY as live candidates, with NO marker asserting
                    # the legacy row belongs to this side.
                    seat = self._row_for(fx, fx["home"],
                                         p["legacy_seat"]["id"])
                    self.assertEqual(seat["group"], "available", seat)
                    self.assertIsNone(
                        seat["roster_status"],
                        "a NULL-attribution roster row was asserted onto a "
                        "guessed side through `roster_status`")
                    self.assertFalse(seat["backed_out"], seat)
                    sub = self._row_for(fx, fx["home"], p["legacy_sub"]["id"])
                    self.assertEqual(sub["group"], "available", sub)
                    self.assertIsNone(
                        sub["sub_status"],
                        "a NULL-owner enrollment was asserted onto a guessed "
                        "side through `sub_status`")

                    # THE PREMISE for the falsifier below: `_side_data` really
                    # does charge these rows to BOTH sides, on purpose.
                    _s, home_matched, _x = api.roster._side_data(
                        fx["gid"], fx["home"])
                    _s, away_matched, _x = api.roster._side_data(
                        fx["gid"], fx["away"])
                    both = ({e.player_id for e, _pl in home_matched}
                            & {e.player_id for e, _pl in away_matched})
                    self.assertEqual(
                        both,
                        {p["legacy_seat"]["id"], p["orphan_seat"]["id"]},
                        "fixture: `_side_data` no longer charges a "
                        "NULL-attribution row to both sides, so the trap this "
                        "test guards has changed shape")

                with self.subTest(backend=label):
                    body()
                    self._require_falsifier_breaks(
                        "side_data_entries", body,
                        f"[{label}] the neither-side rule for legacy rows")
                    self.assertIsNotNone(game)
                ran.append((label, "legacy_null"))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, ["legacy_null"])


# ---------------------------------------------------------------------------
# 4. SEASONAL FIELDS, NEVER THE PERMANENT POINTER.
# ---------------------------------------------------------------------------
class TheFieldsAreSeasonalOnABoundGame(_LineupAuthority, unittest.TestCase):
    """Owner ruling 2: seated rows take ``GameRosterEntry.seated_position``,
    active substitute rows ``SubstituteEnrollment.position``, unseated
    candidates the live ``GameMembershipContext.position``; jersey is the
    exact bound membership value, or ``null`` — never
    ``Player.jersey_number``.

    Every player here is a MOVER in the FIELD sense too: permanent
    FORWARD/#1x against seasonal DEFENSE/#5x. Both falsifiers restore one
    permanent field and are required to break these assertions."""

    def test_positions_and_jerseys_are_seasonal_on_every_backend(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                p = fx["people"]

                def body():
                    for key in ("seated", "enrolled", "offered", "candidate"):
                        row = self._row_for(fx, fx["home"], p[key]["id"])
                        self.assertIsNotNone(row, f"[{label}] {key} missing")
                        self.assertEqual(
                            row["position"], SEASON_POSITION.value,
                            f"[{label}] {key} reported the PERMANENT position")
                        self.assertNotEqual(
                            row["position"], PERM_POSITION.value,
                            f"[{label}] {key} reported the PERMANENT position")
                        self.assertEqual(
                            row["jersey_number"], p[key]["season_jersey"],
                            f"[{label}] {key} reported the PERMANENT jersey")
                        self.assertNotEqual(
                            row["jersey_number"], p[key]["perm_jersey"],
                            f"[{label}] {key} reported the PERMANENT jersey")

                with self.subTest(backend=label):
                    body()
                    self._require_falsifier_breaks(
                        "permanent_position", body,
                        f"[{label}] the seasonal-position rule")
                    self._require_falsifier_breaks(
                        "permanent_jersey", body,
                        f"[{label}] the seasonal-jersey rule")
                ran.append((label, "seasonal_fields"))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, ["seasonal_fields"])

    def test_a_seat_whose_membership_ended_keeps_its_seat_and_reports_null(self):
        """"if no authoritative seasonal value exists, return ``null`` rather
        than falling back to the permanent pointer."

        A durably seated row survives its occupant's departure — that is
        exactly what ``_side_data``'s durable attribution is for — but there
        is no longer a seasonal record to read a jersey from. ``null``, not
        ``Player.jersey_number``. The POSITION does survive, because it was
        recorded durably on the row at seating time."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                p, api = fx["people"], fx["api"]
                with self.subTest(backend=label):
                    end_membership_directly(api.store,
                                            p["seated"]["membership_id"])
                    row = self._row_for(fx, fx["home"], p["seated"]["id"])
                    self.assertIsNotNone(
                        row, "the durable seat vanished when its occupant's "
                             "membership ended")
                    self.assertEqual(row["group"], "selected", row)
                    self.assertEqual(row["position"], SEASON_POSITION.value,
                                     "the DURABLE seated position was lost")
                    self.assertIsNone(
                        row["jersey_number"],
                        "a departed occupant's PERMANENT jersey was reported "
                        "as this game's seasonal number")
                    self.assertFalse(
                        row["eligible"],
                        "a seat whose occupant has no live membership is "
                        "still advertised as actionable")
                ran.append((label, "ended_seat"))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, ["ended_seat"])


# ---------------------------------------------------------------------------
# 5. PARTICIPATION ENDING AFTER ENROLLMENT.
# ---------------------------------------------------------------------------
class ParticipationEndingDoesNotFlipTheDurableSide(_LineupAuthority,
                                                   unittest.TestCase):
    """Owner ruling: "Keep ``_side_data``'s live substitute resolution:
    ``substitutes_enrolled`` is the count of currently eligible enrolled
    candidates, not every durable enrollment row. A durably owned enrollment
    may therefore remain visible for cleanup after participation ends while
    that count drops. Make that row non-actionable except for permitted
    cleanup and expose/label its ineligible state."

    Four separate claims, four separate assertions."""

    def test_the_row_stays_owned_visible_and_non_actionable(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                p, api = fx["people"], fx["api"]
                with self.subTest(backend=label):
                    before = api.get_roster_status(fx["gid"])
                    # ONE, NOT TWO -- and the difference is the point (#427
                    # final blocker, round 2). This baseline used to read 2:
                    # HOME's own `enrolled` row PLUS `legacy_sub`, a pre-060
                    # enrollment with `team_id IS NULL` that `_side_data`
                    # charged to HOME because its occupant is a live HOME
                    # member. That is attribution by live membership, the
                    # authority `durable_game_sides` exists to refuse, and it
                    # made this count disagree with `/substitutes` (which
                    # withholds the same row from BOTH sides) about how many
                    # enrollments HOME has. The number is asserted here
                    # against the DURABLE authority rather than restated as a
                    # literal, and the NULL row's exclusion is asserted BY
                    # NAME below -- so this precondition now pins the rule
                    # instead of merely recording a total.
                    self.assertEqual(
                        before["substitutes_enrolled"], 1,
                        "`substitutes_enrolled` must count only enrollments "
                        "this game DURABLY attributes to this side: "
                        f"{before}")
                    legacy = api.store.substitute_for_player(
                        fx["gid"], p["legacy_sub"]["id"])
                    self.assertIsNone(
                        legacy.team_id,
                        "fixture: legacy_sub is not the NULL-owner shape, so "
                        "the count above could not distinguish the two "
                        "authorities")
                    self.assertTrue(
                        legacy.status.is_active_enrollment, legacy)
                    self.assertEqual(
                        RosterService(api.store).team_for_game(
                            api.store.get_game(fx["gid"]),
                            api.store.get_player(p["legacy_sub"]["id"])),
                        fx["home"],
                        "fixture: legacy_sub is not a live HOME member, so a "
                        "live-membership rule would not have counted it here "
                        "and its absence proves nothing")
                    row = self._row_for(fx, fx["home"], p["enrolled"]["id"])
                    self.assertTrue(row["eligible"], row)

                    end_membership_directly(api.store,
                                            p["enrolled"]["membership_id"])

                    # 1. THE DURABLE SIDE DOES NOT FLIP.
                    self.assertEqual(
                        api.store.substitute_for_player(
                            fx["gid"], p["enrolled"]["id"]).team_id,
                        fx["home"],
                        "the enrollment's durable owner moved when the "
                        "player's membership ended")
                    # 2. THE LIVE ELIGIBLE COUNT DROPS.
                    after = api.get_roster_status(fx["gid"])
                    self.assertEqual(
                        after["substitutes_enrolled"],
                        before["substitutes_enrolled"] - 1,
                        "`substitutes_enrolled` still counts a candidate who "
                        "can no longer play")
                    self.assertEqual(after["substitutes_enrolled"], 0, after)
                    # 3. THE ROW REMAINS VISIBLE FOR CLEANUP, on ITS side.
                    row = self._row_for(fx, fx["home"], p["enrolled"]["id"])
                    self.assertIsNotNone(
                        row, "the durably owned enrollment vanished, so the "
                             "owning Coach can no longer clean it up")
                    self.assertEqual(row["group"], "substitute", row)
                    self.assertNotIn(
                        p["enrolled"]["id"], self._ids(fx, fx["away"]),
                        "the row moved to the opponent's side")
                    # 4. IT IS LABELLED INELIGIBLE, so the UI can disable the
                    #    add/seat control the service would refuse.
                    self.assertFalse(
                        row["eligible"],
                        "an unseatable enrollment is still advertised as "
                        "actionable")
                    # And the service really would refuse the seat, which is
                    # what makes offering the control wrong rather than
                    # merely untidy.
                    refused = api.add_substitute_to_roster(
                        fx["gid"], p["enrolled"]["id"], actor_id=ADMIN)
                    self.assertIn("error", refused, refused)
                    # 5. CLEANUP REMAINS POSSIBLE for the owning Coach —
                    #    authorized against the row's DURABLE side.
                    done = api.withdraw_substitute(
                        fx["gid"], p["enrolled"]["id"], actor_id=ADMIN,
                        authorized_team_id=fx["home"])
                    self.assertNotIn("error", done, done)
                ran.append((label, "participation_ended"))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, ["participation_ended"])


# ---------------------------------------------------------------------------
# 6. THE UNBOUND EXHIBITION — the mirror image.
# ---------------------------------------------------------------------------
class UnboundExhibitionKeepsThePermanentPointer(_LineupAuthority,
                                                unittest.TestCase):
    """"keep any permanent-pointer behavior confined to the explicit unbound
    exhibition path."

    The SAME two players, in the SAME store, answered OPPOSITELY by a bound
    game and an unbound one — which is the strongest available statement that
    the bound path is not merely "always membership", and that the unbound
    path is a real branch rather than a fallback that happens to be unreached.
    """

    def test_the_two_games_answer_oppositely_on_the_same_players(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                p, api = fx["people"], fx["api"]
                ex = self._exhibition(fx)
                ex_game = api.store.get_game(ex["id"])
                with self.subTest(backend=label):
                    self.assertIsNone(ex_game.league_season_id, ex)
                    bound = set(self._ids(fx, fx["home"]))
                    unbound = set(self._ids(fx, fx["home"], game=ex_game))
                    # The exhibition's pool IS the permanent pointer pool.
                    self.assertEqual(
                        unbound, set(self._pointer_pool(fx, fx["home"])),
                        f"[{label}] the unbound game did not use the pointer")
                    # And it is the exact complement of the bound answer.
                    self.assertEqual(bound & unbound, set(),
                                     f"[{label}] the two games agree, so this "
                                     "cannot show the branch is real")
                    self.assertIn(p["departed"]["id"], unbound)
                    self.assertNotIn(p["seated"]["id"], unbound)
                    # PERMANENT FIELDS survive on this path, and only here.
                    row = self._row_for(fx, fx["home"], p["departed"]["id"],
                                        game=ex_game)
                    self.assertEqual(row["position"], PERM_POSITION.value, row)
                    self.assertEqual(row["jersey_number"],
                                     p["departed"]["perm_jersey"], row)
                ran.append((label, "exhibition"))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, ["exhibition"])


# ---------------------------------------------------------------------------
# 7. ORDERING AND DEDUP.
# ---------------------------------------------------------------------------
class OrderingIsImposedNotInherited(_LineupAuthority, unittest.TestCase):
    """"De-duplicate the combined result and preserve deterministic
    ordering."

    The shipped code sorted NOTHING and inherited store order. Measured
    tri-store, that is not cosmetic: Memory yields ``player_1, player_2, …,
    player_10, player_11`` (dict insertion) while SQLite and PostgreSQL both
    yield ``player_1, player_10, player_11, player_2, …`` (lexicographic TEXT
    id). This asserts the ORDER ITSELF is identical on all three backends,
    which is the only assertion a per-backend run can make that a
    store-ordered implementation cannot satisfy."""

    _ORDERS = {}

    def test_the_order_is_identical_on_every_backend(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                fx = self._fixture(store)
                with self.subTest(backend=label):
                    rows = self._rows(fx, fx["home"])
                    names = [r["name"] for r in rows]
                    ids = [r["id"] for r in rows]
                    # Sorted by (name, player_id) — the service's own
                    # convention, shared with list_addable_players.
                    self.assertEqual(names, sorted(names),
                                     f"[{label}] rows are not name-ordered")
                    self.assertEqual(len(ids), len(set(ids)),
                                     f"[{label}] a player appears twice")
                    self._ORDERS[label] = names
                ran.append((label, "ordering"))
            finally:
                self._close(label, store)
        self._assert_matrix_ran(ran, ["ordering"])
        # THE CROSS-BACKEND CLAIM: one order, not three.
        self.assertEqual(len(set(map(tuple, self._ORDERS.values()))), 1,
                         f"backends disagree about order: {self._ORDERS}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
