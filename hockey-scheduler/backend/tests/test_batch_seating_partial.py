"""PR #427 Part B — the BATCH seating path: skip the ineligible, seat the
rest, and never do it silently.

THE RULING THIS FILE ENFORCES (owner, PR #427, comment 5379885403):

    "On a LeagueSeason-bound game, copy_previous_roster and auto_build_roster
    must skip each currently ineligible player and continue seating the
    eligible remainder. […] This is not permission for a silent partial
    success. The result must deterministically identify both the players
    seated and the players skipped, with a stable reason for each skip […]
    If no candidate remains eligible, return a successful zero-seat result
    with the complete skipped list and make no roster writes. Unexpected
    persistence/transaction failures remain all-or-nothing and must roll
    back the batch."

…plus the 2026-08-22 CORRECTION, which is what most of this file is really
about: candidate discovery for copy-previous must come EXCLUSIVELY from the
newest prior game's durable ``GameRosterEntry.team_side``, never from the
current spine, "otherwise transferred players disappear before they can be
reported again"; the auto-fill cohort is the UNION of legacy team pointers
and the team's season-membership rows; and classification must happen INSIDE
the seating transaction, after its locks.

WHAT WAS RED AT HEAD 4de9452, measured on Memory, SQLite AND real PostgreSQL
(the three reproductions are re-asserted here as regressions, one per
section):

  (i)   a MOVER left in the prior roster ABORTED the whole batch. Both entry
        points answered ``{"error": {"code": "not_eligible"}}`` and seated
        NOBODY — the still-eligible team-mate included.
  (ii)  a genuinely TRANSFERRED player was SILENTLY DROPPED by copy-previous's
        permanent-pointer filter: ``{"copied": 1, …}``, absent from the
        response entirely, reported nowhere. The ruling's "never a silent
        partial success" was already violated, today, for the first shape the
        owner names by hand.
  (iii) ORDERING DECIDED SET MEMBERSHIP. ``auto_build_roster`` truncates at
        the game's targets over a store-ordered pool; from ONE identical
        12-player fixture with ``target_skaters=3``, Memory seated
        "Player 00/01/02" while SQLite and PostgreSQL both seated
        "Player 00/09/10" — different players, same fixture, same call.

TRI-STORE, PROVEN, NOT ASSUMED. ``_stores`` yields Memory, SQLite and — when
TEST_DATABASE_URL is set — real PostgreSQL; ``_assert_backend`` proves the
backend in hand rather than trusting the env var, and ``_assert_ran`` fails a
loop that silently covered fewer backends than were configured. A SKIP IS NOT
A PASS.

IDENTITY, NEVER COUNTS. Every assertion about what was written compares
IDENTITY snapshots (``_writes``, borrowed from
test_roster_attribution_durability.py). A count is satisfied by a
same-cardinality row SWAP, which is precisely the write a refused path must
not perform.
"""

import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer
from typing import NamedTuple

from helpers import BACKEND, FakeClock  # noqa: F401  (sets up sys.path)
from helpers import end_membership_directly, fresh_sql_store

from test_slot_overfill_regression import _OverfillFixture
from test_substitute_membership_cutover import ADMIN, _at

from hockey_scheduler.domain import AuditAction, MembershipStatus
from hockey_scheduler.services import membership_spine as spine
from hockey_scheduler.services.roster_service import RosterService
from hockey_scheduler.store import InMemoryStore, SqlStore
from hockey_scheduler.web import server as srv
from hockey_scheduler.web.auth import DEMO_PASSWORD, DEMO_USERS

_PG_SKIP = (
    "PostgreSQL not configured (TEST_DATABASE_URL) — the #427 batch-seating "
    "contract was NOT exercised on PostgreSQL. A SKIP IS NOT A PASS: the "
    "roster/membership/registration reads behind every partition decision, "
    "the row locks the revalidation depends on, and the rollback the "
    "fail-injection case asserts are all real SQL there. Set "
    "TEST_DATABASE_URL (run_parallel.py --postgres does).")


_ANNOUNCED = set()


def _announce_pg_skip(banner):
    """Say the PostgreSQL-was-not-exercised warning ONCE per banner per
    process. Loud is the point; twenty identical paragraphs is not loud, it
    is what pushes a real assertion out of a failing shard's report (the
    #382 postmortem run_parallel.py's own header describes)."""
    if banner in _ANNOUNCED:
        return
    _ANNOUNCED.add(banner)
    print(f"\n[{banner}] " + _PG_SKIP)


class Shape(NamedTuple):
    """One candidate in the mixed batch, and what must become of it."""
    name: str            # display name, which also fixes the sort position
    seats: bool
    reason: str          # None when it seats


# ======================================================================
# The COPY-PREVIOUS mixed cohort.
#
# Every one of these players was legitimately SEATED on the prior game (so
# each carries a durable ``team_side`` naming HOME), and each then became
# ineligible in a DIFFERENT way. Names are chosen so ``(name, player_id)``
# ordering is obvious by eye — the expected SEATED/SKIPPED lists below are
# written in that order and asserted as sequences, not as sets.
# ======================================================================
COPY_SHAPES = (
    Shape("Ada Available", True, None),
    Shape("Bex Backup", True, None),
    Shape("Cleo Parked", False, "membership_inactive"),
    Shape("Dara Deactivated", False, "player_inactive"),
    Shape("Ewan Released", False, "membership_released"),
    Shape("Finn Unattributed", False, "prior_seat_unattributed"),
    Shape("Gia Transferred", False, "membership_transferred"),
)

# ======================================================================
# The AUTO-FILL cohort — the UNION the correction specifies. Three of these
# are reachable ONLY through the pointer half and two ONLY through the
# membership half, so a cohort that dropped either half fails here.
# ======================================================================
BUILD_SHAPES = (
    Shape("Ada Available", True, None),          # pointer + membership, HOME
    Shape("Bex Mover", True, None),              # pointer THIRD, membership HOME
    Shape("Cleo Pointeronly", False, "no_eligible_membership"),
    Shape("Dara Deactivated", False, "player_inactive"),
    Shape("Ewan Lastseason", False, "membership_other_league_season"),
    Shape("Gia Transferred", False, "membership_transferred"),
)


class _BatchHarness(_OverfillFixture):
    """Fixture + assertions written ONCE and invoked by every backend."""

    # -- stores ----------------------------------------------------------
    def _stores(self):
        yield "memory", InMemoryStore()
        yield "sqlite", SqlStore(":memory:")
        url = os.environ.get("TEST_DATABASE_URL")
        if url:
            yield "postgres", fresh_sql_store(url)

    def _assert_backend(self, label, store):
        """PROVE the backend, never assume it — ``skipUnless`` on the env var
        would prove only that a URL was SET, never that a statement reached
        PostgreSQL."""
        if label == "postgres":
            self.assertIsInstance(store, SqlStore, label)
            self.assertEqual(store.backend, "postgres", store.backend)
        elif label == "sqlite":
            self.assertIsInstance(store, SqlStore, label)
            self.assertEqual(store.backend, "sqlite", store.backend)
        else:
            self.assertIsInstance(store, InMemoryStore, label)

    def _close(self, label, store):
        if isinstance(store, SqlStore):
            if label == "postgres":
                store.reset_schema()
            store.close()

    def _assert_ran(self, labels, banner="BATCH SEATING CONTRACT"):
        expected = {"memory", "sqlite"}
        if os.environ.get("TEST_DATABASE_URL"):
            expected.add("postgres")
        else:
            _announce_pg_skip(banner)
        self.assertEqual(set(labels), expected, sorted(labels))

    # -- the two-game fixture --------------------------------------------
    def _prior_game(self, api, season, league, teams, hour=2,
                    target_skaters=8, rink_name=None):
        """A SECOND, EARLIER game for the same two teams — the source a copy
        reads from. Built on its OWN rink and ice slot, so two prior games at
        the same start_time (the tie-break case) do not collide on ice."""
        venue_id = api.store.get_rink(
            api.store.get_ice_slot(
                api.store.get_game(api.store.all_games()[0].id).ice_slot_id
            ).rink_id).venue_id
        rink = api.create_rink(venue_id, rink_name or f"R{hour}",
                               actor_id=ADMIN)
        self.assertNotIn("error", rink, rink)
        rink_id = rink["id"]
        slot = api.create_ice_slot(rink_id, _at(hour).isoformat(),
                                   _at(hour + 1).isoformat(), "game",
                                   actor_id=ADMIN)
        self.assertNotIn("error", slot, slot)
        g = api.create_game(season["id"], None, teams["home"]["id"],
                            teams["away"]["id"], slot["id"],
                            target_goalies=0, target_skaters=target_skaters,
                            actor_id=ADMIN, league_id=league["id"])
        self.assertNotIn("error", g, g)
        api.publish_game(g["id"], actor_id=ADMIN)
        return g

    def _pair(self, store, target_skaters=8, target_goalies=0):
        """``(api, fx)`` for a HOME side with a prior game and a target game.

        The TARGET game is ``_OverfillFixture._build``'s game (the latest
        slot, hour 18); the PRIOR game sits earlier the same day."""
        api, season, league, teams, game, ls_id = self._build(
            store, target_skaters=target_skaters,
            target_goalies=target_goalies)
        prior = self._prior_game(api, season, league, teams)
        return api, {"api": api, "season": season, "league": league,
                     "teams": teams, "game": game, "prior": prior,
                     "ls_id": ls_id, "gid": game["id"], "pid": prior["id"],
                     "home": teams["home"]["id"],
                     "away": teams["away"]["id"],
                     "third": teams["third"]["id"]}

    # -- write-identity snapshots ----------------------------------------
    def _writes(self, api, *game_ids):
        """Every write class a batch can touch, as comparable IDENTITY
        values across ALL the games in play — never bare counts.

        Both games are snapshotted because a copy READS the prior game and
        WRITES the target one; a bug that mutated the source would be
        invisible to a target-only snapshot."""
        store = api.store
        out = {}
        for gid in game_ids:
            out[f"roster:{gid}"] = sorted(
                (e.id, e.player_id, e.status.value, e.team_side or "",
                 getattr(e.seated_position, "value", ""))
                for e in store.roster_for_game(gid))
            out[f"availability:{gid}"] = sorted(
                (a.id, a.player_id, a.availability_status.value)
                for a in store.availability_for_game(gid))
            out[f"audit:{gid}"] = sorted(
                (a.id, a.action.value, json.dumps(a.detail, sort_keys=True))
                for a in store.audit_for_game(gid))
            out[f"substitutes:{gid}"] = sorted(
                (s.id, s.player_id, s.status.value)
                for s in store.substitutes_for_game(gid))
        return out

    def _occupying(self, api, gid):
        return [e.player_id for e in api.store.roster_for_game(gid)
                if e.status.occupies_slot]

    def _batch_audit(self, api, gid):
        """THE one batch audit row, and a hard assertion that there is
        exactly one — "one audit row per batch" is part of the bar."""
        rows = [a for a in api.store.audit_for_game(gid)
                if a.action == AuditAction.ROSTER_BATCH_SEATED]
        self.assertEqual(len(rows), 1, [(a.id, a.detail) for a in rows])
        return rows[0]

    # -- shape construction ----------------------------------------------
    def _seat_prior(self, api, fx, players):
        """Seat every candidate on the PRIOR game while they are all still
        eligible, so each row carries a durable ``team_side`` naming HOME."""
        ids = [p["id"] for p in players]
        res = api.select_roster(fx["pid"], ids, actor_id=ADMIN)
        self.assertNotIn("error", res if isinstance(res, dict) else {}, res)
        sides = {e.player_id: e.team_side
                 for e in api.store.roster_for_game(fx["pid"])}
        for pid in ids:
            self.assertEqual(sides.get(pid), fx["home"], (pid, sides))
        return ids

    def _copy_cohort(self, api, fx):
        """Build COPY_SHAPES: seat them all on the prior game, THEN break
        each one in its own way. Returns ``{shape_name: player_id}``."""
        made = {s.name: self._player(api, fx["home"], s.name)
                for s in COPY_SHAPES}
        # "Gia Transferred" must be seated from HOME before she leaves, so
        # every candidate is created on HOME and seated together.
        self._seat_prior(api, fx, [made[s.name] for s in COPY_SHAPES])

        api.set_season_roster_membership_status(
            self._stint_id(api, made["Cleo Parked"]["id"], fx["ls_id"]),
            "inactive", actor_id=ADMIN)
        res = api.set_player_active(made["Dara Deactivated"]["id"], False,
                                    actor_id=ADMIN)
        self.assertNotIn("error", res, res)
        end_membership_directly(
            api.store,
            self._stint_id(api, made["Ewan Released"]["id"], fx["ls_id"]),
            "released")
        # The pre-061 row: NULL the durable attribution on a genuinely
        # seated row, which is exactly the on-disk shape migration 061
        # leaves behind (it performs no backfill).
        entry = api.store.roster_entry_for_player(
            fx["pid"], made["Finn Unattributed"]["id"])
        entry.team_side = None
        entry.seated_position = None
        api.store.save_roster_entry(entry)
        reread = api.store.roster_entry_for_player(
            fx["pid"], made["Finn Unattributed"]["id"])
        # The NULL must survive the round trip on a real database, or this
        # shape would be testing an in-process object.
        self.assertIsNone(reread.team_side)
        self._transfer(api, made["Gia Transferred"]["id"], fx["ls_id"],
                       fx["ls_id"], fx["third"])
        return {name: p["id"] for name, p in made.items()}

    def _build_cohort(self, api, fx):
        """Build BUILD_SHAPES — the auto-fill UNION cohort."""
        made = {}
        made["Ada Available"] = self._player(api, fx["home"], "Ada Available")
        # pointer THIRD, seasonal record HOME: reachable ONLY through the
        # membership half of the union, and SEATABLE.
        mover = self._pointer_only_player(api, fx["third"], "Bex Mover")
        self._membership(api, mover["id"], fx["ls_id"], fx["home"])
        made["Bex Mover"] = mover
        made["Dara Deactivated"] = self._player(api, fx["home"],
                                                "Dara Deactivated")
        res = api.set_player_active(made["Dara Deactivated"]["id"], False,
                                    actor_id=ADMIN)
        self.assertNotIn("error", res, res)
        gia = self._player(api, fx["home"], "Gia Transferred")
        self._transfer(api, gia["id"], fx["ls_id"], fx["ls_id"], fx["third"])
        made["Gia Transferred"] = gia
        # A stint on THIS team in a DIFFERENT competition — the owner's
        # "wrong-LeagueSeason" member of the cohort. Reachable only through
        # the membership half.
        #
        # BUILT LAST, DELIBERATELY: registering HOME into the second Season
        # opens a parity stint there for every player whose PERMANENT
        # pointer already names HOME, which would silently give the
        # membership-LESS shape below a membership and turn its reason from
        # ``no_eligible_membership`` into ``membership_other_league_season``
        # — two different shapes wearing one name, and the narrowing this
        # matrix exists to prove would be untested.
        made["Ewan Lastseason"] = self._other_season_stint(
            api, fx, "Ewan Lastseason")
        # pointer HOME, seasonal record silent, and silent it must STAY.
        made["Cleo Pointeronly"] = self._pointer_only_player(
            api, fx["home"], "Cleo Pointeronly")
        self.assertEqual(
            list(api.store.memberships_for_player(
                made["Cleo Pointeronly"]["id"])), [])
        return {name: p["id"] for name, p in made.items()}

    def _other_season_stint(self, api, fx, name):
        """A player whose ONLY membership naming HOME belongs to a DIFFERENT
        LeagueSeason — a second Season of the same Program, with its own
        League and its own registration for the very same Team.

        Their permanent pointer names a third team, so the pointer half of
        the cohort cannot reach them: they are in the batch's candidate pool
        purely because ``memberships_for_team`` is unfiltered by
        LeagueSeason, which is exactly what the correction asks for."""
        program_id = api.store.get_season(fx["season"]["id"]).program_id
        season2 = api.create_season(program_id, "Spring 2027", actor_id=ADMIN)
        self.assertNotIn("error", season2, season2)
        # The SAME Team and the SAME League, a DIFFERENT Season — which is
        # exactly what makes a different ``LeagueSeason`` row (its uniqueness
        # is ``(league_id, season_id)``). Registering the Team into a League
        # that is not its own is refused by the spine, so "last season" is
        # the only honest wrong-LeagueSeason shape for THIS team.
        reg = api.register_team_for_season(season2["id"], fx["home"],
                                           actor_id=ADMIN,
                                           league_id=fx["league"]["id"])
        self.assertNotIn("error", reg, reg)
        this_ls = api.store.get_game(fx["gid"]).league_season_id
        other_ls = [ls.id for ls in api.store.all_league_seasons()
                    if ls.season_id == season2["id"]]
        self.assertEqual(len(other_ls), 1, other_ls)
        self.assertNotEqual(other_ls[0], this_ls)
        p = self._pointer_only_player(api, fx["third"], name)
        self._membership(api, p["id"], other_ls[0], fx["home"])
        return p

    # -- expectation helpers ---------------------------------------------
    def _expected(self, shapes, ids):
        seated = [ids[s.name] for s in shapes if s.seats]
        skipped = [{"player_id": ids[s.name], "name": s.name,
                    "reason": s.reason} for s in shapes if not s.seats]
        return seated, skipped

    def _assert_result(self, result, seated, skipped, label, deferred=()):
        """The RESPONSE contract: exact sequences, in order — identity and
        reason, never counts."""
        self.assertNotIn("error", result, (label, result))
        self.assertEqual(result["seated"], seated, (label, result))
        self.assertEqual(result["skipped"], skipped, (label, result))
        self.assertEqual([d["player_id"] for d in result["deferred"]],
                         list(deferred), (label, result))
        self.assertEqual(result["candidate_count"],
                         len(seated) + len(skipped) + len(deferred),
                         (label, result))

    def _assert_audit(self, api, gid, seated, skipped, label, deferred=()):
        """The AUDIT contract: the same identity, durably, in the same
        order, in ONE row — including on a zero-seat run."""
        detail = self._batch_audit(api, gid).detail
        self.assertEqual(detail["selected_player_ids"], seated,
                         (label, detail))
        self.assertEqual(
            detail["skipped"],
            [{"player_id": s["player_id"], "reason": s["reason"]}
             for s in skipped], (label, detail))
        self.assertEqual([d["player_id"] for d in detail["deferred"]],
                         list(deferred), (label, detail))
        self.assertEqual(detail["candidate_count"],
                         len(seated) + len(skipped) + len(deferred),
                         (label, detail))
        return detail


# ======================================================================
# 1. THE MIXED BATCH — copy-previous
# ======================================================================
class CopyPreviousSeatsTheEligibleAndReportsEverySkip(_BatchHarness,
                                                     unittest.TestCase):
    """Reproductions (i) and (ii), closed, in one case.

    RED at head 4de9452: this exact cohort answered
    ``{"error": {"code": "not_eligible"}}`` (the Mover/transferred shapes
    reached ``select_roster``) and seated NOBODY; and when the pointer filter
    did swallow a transferred player instead, the response said
    ``{"copied": N}`` with no trace of them at all."""

    def _run(self, label, api, fx):
        ids = self._copy_cohort(api, fx)
        seated, skipped = self._expected(COPY_SHAPES, ids)
        result = api.copy_previous_roster(fx["gid"], team_id=fx["home"],
                                          actor_id=ADMIN)
        self._assert_result(result, seated, skipped, label)
        # ONLY the eligible rows were written, and they really are seated.
        self.assertEqual(sorted(self._occupying(api, fx["gid"])),
                         sorted(seated), label)
        self._assert_audit(api, fx["gid"], seated, skipped, label)
        # The legacy keys keep their exact previous meanings.
        self.assertEqual(result["copied"], len(seated), (label, result))
        self.assertEqual(result["from_game_id"], fx["pid"], (label, result))
        self.assertEqual(result["team_id"], fx["home"], (label, result))
        return result

    def test_mixed_cohort_seats_only_the_eligible_rows(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                api, fx = self._pair(store)
                with self.subTest(backend=label):
                    self._run(label, api, fx)
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran)

    def test_every_skip_reason_is_distinct_and_in_the_ladder(self):
        """Two properties the per-shape assertions above cannot see: no two
        skipped candidates share a reason (a classifier that collapsed two
        shapes back together would still give each of them *a* reason), and
        every reason reported is one the documented precedence ladder
        knows — ``reason_rank`` raises for anything unlisted."""
        reasons = [s.reason for s in COPY_SHAPES if not s.seats]
        self.assertEqual(len(set(reasons)), len(reasons), sorted(reasons))
        for reason in reasons:
            spine.reason_rank(reason)


# ======================================================================
# 2. THE MIXED BATCH — auto-fill, and its UNION cohort
# ======================================================================
class AutoBuildSeatsTheEligibleAndReportsEverySkip(_BatchHarness,
                                                  unittest.TestCase):
    """Reproduction (i) for the other entry point, plus the correction's
    "the cohort is the union you described".

    RED at head 4de9452: the pointer-derived pool contained "Gia
    Transferred" (pointer still HOME) and ``select_roster`` raised, so the
    whole call answered ``not_eligible`` and seated nobody. It ALSO could
    never see "Bex Mover" (pointer THIRD) or "Ewan Lastseason" at all."""

    def _run(self, label, api, fx):
        ids = self._build_cohort(api, fx)
        seated, skipped = self._expected(BUILD_SHAPES, ids)
        result = api.auto_build_roster(fx["gid"], team_id=fx["home"],
                                       actor_id=ADMIN)
        self._assert_result(result, seated, skipped, label)
        self.assertEqual(sorted(self._occupying(api, fx["gid"])),
                         sorted(seated), label)
        self._assert_audit(api, fx["gid"], seated, skipped, label)
        # The facade still returns the roster status it always did.
        for key in ("open_skater_slots", "missing_skaters", "short_roster",
                    "status"):
            self.assertIn(key, result, (label, sorted(result)))
        # …and every seated player was CONFIRMED in the same transaction.
        confirmed = sorted(a.player_id for a in
                           api.store.availability_for_game(fx["gid"]))
        self.assertEqual(confirmed, sorted(seated), label)
        return result

    def test_union_cohort_seats_only_the_eligible_rows(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                api, fx = self._pair(store)
                with self.subTest(backend=label):
                    self._run(label, api, fx)
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran)

    def test_each_half_of_the_union_is_load_bearing(self):
        """The cohort is asserted from BOTH directions, so dropping either
        half of the union fails here rather than passing quietly:

        * "Bex Mover" (pointer THIRD, membership HOME) is reachable ONLY
          through the membership half — and she SEATS, so a pointer-only
          cohort loses a player who should play;
        * "Cleo Pointeronly" (pointer HOME, no membership) is reachable ONLY
          through the pointer half — and she is SKIPPED WITH A REASON, so a
          membership-only cohort makes her vanish instead, which is the
          silent drop this ruling abolishes."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                api, fx = self._pair(store)
                with self.subTest(backend=label):
                    ids = self._build_cohort(api, fx)
                    result = api.auto_build_roster(
                        fx["gid"], team_id=fx["home"], actor_id=ADMIN)
                    self.assertIn(ids["Bex Mover"], result["seated"],
                                  (label, result))
                    reported = {s["player_id"]: s["reason"]
                                for s in result["skipped"]}
                    self.assertEqual(reported.get(ids["Cleo Pointeronly"]),
                                     spine.NO_ELIGIBLE_MEMBERSHIP,
                                     (label, result))
                    self.assertEqual(reported.get(ids["Ewan Lastseason"]),
                                     spine.MEMBERSHIP_OTHER_LEAGUE_SEASON,
                                     (label, result))
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran)


# ======================================================================
# 3. ORDERING IS IDENTICAL ACROSS BACKENDS — not merely sorted
# ======================================================================
class BatchOrderingIsIdenticalOnEveryBackend(_BatchHarness,
                                             unittest.TestCase):
    """Reproduction (iii), closed, and asserted the only way that means
    anything: by comparing the FULL result of one backend against another,
    not by checking that each backend's own answer is internally sorted.

    A sortedness assertion passes happily while two backends seat different
    players — which is exactly what was happening: with ``target_skaters=3``
    over a 12-player bench, Memory seated Player 00/01/02 and SQLite and
    PostgreSQL both seated Player 00/09/10. The fixture is built by the
    identical call sequence on every backend, so the id allocation is
    identical too and the results must be EQUAL, element for element."""

    def _fill(self, api, fx, n=12):
        for i in range(n):
            self._player(api, fx["home"], f"Player {i:02d}", jersey=i + 1)

    def test_auto_build_results_are_equal_across_backends(self):
        results, ran = {}, []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                api, fx = self._pair(store, target_skaters=3)
                self._fill(api, fx)
                r = api.auto_build_roster(fx["gid"], team_id=fx["home"],
                                          actor_id=ADMIN)
                self.assertNotIn("error", r, (label, r))
                names = tuple(api.store.get_player(p).name
                               for p in r["seated"])
                results[label] = (tuple(r["seated"]), names,
                                  tuple(d["player_id"]
                                        for d in r["deferred"]))
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran)
        distinct = set(results.values())
        self.assertEqual(len(distinct), 1,
                         {k: v[1] for k, v in results.items()})
        # …and the ONE answer is the first three by (name, player_id), which
        # is the only ordering a coach reading list_addable_players could
        # have predicted.
        (seated, names, deferred), = distinct
        self.assertEqual(list(names),
                         ["Player 00", "Player 01", "Player 02"], names)
        # The eligible remainder is REPORTED, not dropped: nine deferred
        # candidates, in the same order, with the target-met reason.
        self.assertEqual(len(deferred), 9, deferred)

    def test_copy_previous_results_are_equal_across_backends(self):
        results, ran = {}, []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                api, fx = self._pair(store)
                ids = self._copy_cohort(api, fx)
                r = api.copy_previous_roster(fx["gid"], team_id=fx["home"],
                                             actor_id=ADMIN)
                self.assertNotIn("error", r, (label, r))
                results[label] = (tuple(r["seated"]),
                                  tuple(json.dumps(s, sort_keys=True)
                                        for s in r["skipped"]))
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran)
        self.assertEqual(len(set(results.values())), 1, results)

    def test_the_source_game_tie_break_is_total(self):
        """Two prior games at the SAME start_time, each with a seatable
        roster on this side. The old code broke that tie on ``all_games()``
        order — insertion order on Memory, TEXT id order on SQL — so the two
        backends could copy DIFFERENT lineups. ``(start_time, id)``
        descending makes it one answer everywhere."""
        chosen, ran = {}, []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                api, fx = self._pair(store)
                twin = self._prior_game(api, fx["season"], fx["league"],
                                        fx["teams"], hour=3,
                                        rink_name="Twin Rink")
                self.assertNotEqual(twin["id"], fx["pid"])
                # The TIE is forced at the store. Creating two overlapping
                # games for one team is refused by the scheduler
                # (``team_overlap``), so a genuine start_time collision on
                # this side can only arrive the way every other impossible
                # precondition in this suite does — a restored backup, a
                # direct/bulk writer, a relocation. The SORT must be total
                # regardless of how the rows got there.
                tg = api.store.get_game(twin["id"])
                tg.start_time = api.store.get_game(fx["pid"]).start_time
                api.store.save_game(tg)
                self.assertEqual(
                    api.store.get_game(twin["id"]).start_time,
                    api.store.get_game(fx["pid"]).start_time, label)
                a = self._player(api, fx["home"], "Ada Available")
                b = self._player(api, fx["home"], "Bex Backup")
                api.select_roster(fx["pid"], [a["id"]], actor_id=ADMIN)
                api.select_roster(twin["id"], [b["id"]], actor_id=ADMIN)
                r = api.copy_previous_roster(fx["gid"], team_id=fx["home"],
                                             actor_id=ADMIN)
                self.assertNotIn("error", r, (label, r))
                # THE RULE, asserted directly: ``(start_time, id)``
                # descending, so among tied games the GREATER id wins.
                # Cross-backend agreement alone would NOT have pinned this —
                # for a two-game tie, ``all_games()`` happens to hand back
                # the same order on every backend (insertion order and TEXT
                # id order coincide below ``game_10``), so a start_time-only
                # sort agrees with itself everywhere while still being
                # decided by store order. Asserting WHICH game is chosen is
                # what makes the total order falsifiable.
                self.assertEqual(r["from_game_id"],
                                 max(fx["pid"], twin["id"]), (label, r))
                self.assertEqual(r["seated"], [b["id"]], (label, r))
                chosen[label] = (r["from_game_id"], tuple(r["seated"]))
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran)
        self.assertEqual(len(set(chosen.values())), 1, chosen)


# ======================================================================
# 4. ZERO SEATS IS A SUCCESS, AND WRITES NOTHING
# ======================================================================
class AnAllIneligibleBatchSucceedsAndWritesNothing(_BatchHarness,
                                                   unittest.TestCase):
    """"If no candidate remains eligible, return a successful zero-seat
    result with the complete skipped list and make no roster writes."

    Asserted with an IDENTITY snapshot of every write class across BOTH
    games, so a same-cardinality row swap cannot pass for "no writes"."""

    def _all_ineligible(self, api, fx):
        """The whole HOME side's participation ends: its
        ``SeasonTeamRegistration`` goes inactive, so every candidate's spine
        breaks at the same leg and NOBODY resolves."""
        made = [self._player(api, fx["home"], n)
                for n in ("Ada Available", "Bex Backup", "Cleo Parked")]
        self._seat_prior(api, fx, made)
        (reg,) = api.store.registrations_for_team_in_league_season(
            fx["ls_id"], fx["home"])
        reg.active = False
        api.store.save_season_team_registration(reg)
        return [p["id"] for p in made]

    def test_copy_previous_zero_seat_is_a_success_with_no_roster_writes(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                api, fx = self._pair(store)
                with self.subTest(backend=label):
                    ids = self._all_ineligible(api, fx)
                    before = self._writes(api, fx["gid"], fx["pid"])
                    r = api.copy_previous_roster(
                        fx["gid"], team_id=fx["home"], actor_id=ADMIN)
                    self.assertNotIn("error", r, (label, r))
                    self.assertEqual(r["seated"], [], (label, r))
                    self.assertEqual(r["copied"], 0, (label, r))
                    self.assertEqual(
                        [s["player_id"] for s in r["skipped"]], ids,
                        (label, r))
                    self.assertEqual(
                        {s["reason"] for s in r["skipped"]},
                        {spine.NOT_REGISTERED}, (label, r))
                    after = self._writes(api, fx["gid"], fx["pid"])
                    # NOT ONE roster row, availability row or substitute
                    # row anywhere...
                    for key, value in after.items():
                        if key.startswith("audit:"):
                            continue
                        self.assertEqual(value, before[key], (label, key))
                    # ...and the source game is untouched even in its audit.
                    self.assertEqual(after[f"audit:{fx['pid']}"],
                                     before[f"audit:{fx['pid']}"], label)
                    # ...but the batch's OWN audit row exists, because it is
                    # the only durable record that the operation ran.
                    self._assert_audit(api, fx["gid"], [],
                                       r["skipped"], label)
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran)

    def test_auto_build_zero_seat_is_a_success_with_no_roster_writes(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                api, fx = self._pair(store)
                with self.subTest(backend=label):
                    ids = self._all_ineligible(api, fx)
                    before = self._writes(api, fx["gid"])
                    r = api.auto_build_roster(
                        fx["gid"], team_id=fx["home"], actor_id=ADMIN)
                    self.assertNotIn("error", r, (label, r))
                    self.assertEqual(r["seated"], [], (label, r))
                    self.assertEqual(
                        sorted(s["player_id"] for s in r["skipped"]),
                        sorted(ids), (label, r))
                    after = self._writes(api, fx["gid"])
                    for key, value in after.items():
                        if key.startswith("audit:"):
                            continue
                        self.assertEqual(value, before[key], (label, key))
                    self._assert_audit(api, fx["gid"], [], r["skipped"],
                                       label)
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran)


# ======================================================================
# 5. THE NEWEST PRIOR ROSTER IS AUTHORITATIVE — no fall-through
# ======================================================================
class TheNewestPriorRosterIsAuthoritativeEvenWhenItSeatsNobody(
        _BatchHarness, unittest.TestCase):
    """The acceptance bar's third item, and the one that is easiest to get
    wrong in the "helpful" direction.

    The old loop walked backwards until SOME game yielded an eligible list.
    With partial success that would mean: the newest lineup is entirely aged
    out, so quietly seat the one from three weeks ago instead. That both
    seats a roster the coach never asked for AND hides the fact worth
    telling them. ``ValidationError`` is now reserved for its true meaning —
    no earlier game with ANY occupying roster on this side at all."""

    def _two_priors(self, api, fx):
        """An OLDER prior game whose roster is entirely still eligible, and
        the NEWEST prior game whose roster is entirely ineligible."""
        older = self._prior_game(api, fx["season"], fx["league"],
                                 fx["teams"], hour=1)
        old_timer = self._player(api, fx["home"], "Ada Available")
        res = api.select_roster(older["id"], [old_timer["id"]],
                                actor_id=ADMIN)
        self.assertNotIn("error", res if isinstance(res, dict) else {}, res)
        recent = self._player(api, fx["home"], "Gia Transferred")
        self._seat_prior(api, fx, [recent])
        self._transfer(api, recent["id"], fx["ls_id"], fx["ls_id"],
                       fx["third"])
        return older, old_timer["id"], recent["id"]

    def test_a_fully_skipped_newest_roster_does_not_fall_through(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                api, fx = self._pair(store)
                with self.subTest(backend=label):
                    older, old_id, new_id = self._two_priors(api, fx)
                    r = api.copy_previous_roster(
                        fx["gid"], team_id=fx["home"], actor_id=ADMIN)
                    self.assertNotIn("error", r, (label, r))
                    # The NEWEST game is the source, named as such...
                    self.assertEqual(r["from_game_id"], fx["pid"],
                                     (label, r))
                    # ...it seated nobody and said exactly why...
                    self.assertEqual(r["seated"], [], (label, r))
                    self.assertEqual(
                        r["skipped"],
                        [{"player_id": new_id, "name": "Gia Transferred",
                          "reason": spine.MEMBERSHIP_STATUS_REASONS[
                              MembershipStatus.TRANSFERRED]}],
                        (label, r))
                    # ...and the OLDER game's still-eligible player was NOT
                    # quietly seated instead.
                    self.assertEqual(self._occupying(api, fx["gid"]), [],
                                     label)
                    self.assertNotIn(old_id, r["seated"], (label, r))
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran)

    def test_no_earlier_roster_at_all_still_raises(self):
        """The ValidationError kept its meaning — it was NOT widened into a
        general "nothing happened" answer, and it was not narrowed away."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                api, fx = self._pair(store)
                with self.subTest(backend=label):
                    r = api.copy_previous_roster(
                        fx["gid"], team_id=fx["home"], actor_id=ADMIN)
                    self.assertEqual(r["error"]["code"], "validation_error",
                                     (label, r))
                    self.assertIn("No previous roster",
                                  r["error"]["message"], (label, r))
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran)

    def test_a_roster_on_the_other_side_only_is_not_a_source(self):
        """A prior game in which only the AWAY bench was seated yields no
        candidates for HOME, so the walk continues past it — the
        authoritative-source rule is about the newest game with candidates
        ON THIS SIDE, not the newest game with any roster at all."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                api, fx = self._pair(store)
                with self.subTest(backend=label):
                    away_only = self._player(api, fx["away"], "Zoe Away")
                    res = api.select_roster(fx["pid"], [away_only["id"]],
                                            actor_id=ADMIN)
                    self.assertNotIn(
                        "error", res if isinstance(res, dict) else {}, res)
                    r = api.copy_previous_roster(
                        fx["gid"], team_id=fx["home"], actor_id=ADMIN)
                    self.assertEqual(r["error"]["code"], "validation_error",
                                     (label, r))
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran)


# ======================================================================
# 6. THE PRE-061 ROW — NULL attribution fails closed, and is REPORTED
# ======================================================================
class AnUnattributedPriorRowIsRefusedAndNamedNeverGuessed(
        _BatchHarness, unittest.TestCase):
    """The NULL-attribution decision, pinned in both directions.

    A pre-061 roster row names no side. The two honest options are to DROP
    it (the silent omission this ruling abolishes) or to admit it on EVERY
    side and refuse it with a reason. The second is implemented, exactly
    mirroring the already-shipped slot-arithmetic rule
    (``LegacyRowsWithNoAttributionFailClosed``: such a row is charged on
    every side and in both buckets, consulting nothing). The accepted cost
    is OVER-reporting, asserted below so it can never be mistaken for an
    accident."""

    def _legacy_prior_row(self, api, fx, player):
        entry = api.store.roster_entry_for_player(fx["pid"], player["id"])
        entry.team_side = None
        entry.seated_position = None
        api.store.save_roster_entry(entry)
        self.assertIsNone(
            api.store.roster_entry_for_player(fx["pid"],
                                              player["id"]).team_side)

    def test_a_null_row_is_reported_even_when_the_player_is_eligible(self):
        """The fail-closed half: this player would seat perfectly well
        today, and is STILL refused, because the row cannot prove which
        bench it was on. Current eligibility is not consulted at all."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                api, fx = self._pair(store)
                with self.subTest(backend=label):
                    p = self._player(api, fx["home"], "Finn Unattributed")
                    mate = self._player(api, fx["home"], "Ada Available")
                    self._seat_prior(api, fx, [p, mate])
                    self._legacy_prior_row(api, fx, p)
                    # The control: nothing is wrong with Finn TODAY.
                    self.assertIsNone(api.roster.seating_block_reason(
                        api.store.get_game(fx["gid"]),
                        api.store.get_player(p["id"])), label)
                    r = api.copy_previous_roster(
                        fx["gid"], team_id=fx["home"], actor_id=ADMIN)
                    self.assertEqual(r["seated"], [mate["id"]], (label, r))
                    self.assertEqual(
                        r["skipped"],
                        [{"player_id": p["id"], "name": "Finn Unattributed",
                          "reason": spine.PRIOR_SEAT_UNATTRIBUTED}],
                        (label, r))
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran)

    def test_a_null_row_is_reported_on_the_other_side_too(self):
        """The OVER-reporting, stated as an assertion. A NULL row that was
        really on the AWAY bench is reported as unprovable when copying
        HOME. That is the deliberate price of never guessing — and it is
        still strictly better than the alternative, because the operator
        sees the name and can re-select by hand."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                api, fx = self._pair(store)
                with self.subTest(backend=label):
                    away = self._player(api, fx["away"], "Zoe Away")
                    home = self._player(api, fx["home"], "Ada Available")
                    res = api.select_roster(
                        fx["pid"], [away["id"], home["id"]], actor_id=ADMIN)
                    self.assertNotIn(
                        "error", res if isinstance(res, dict) else {}, res)
                    self._legacy_prior_row(api, fx, away)
                    r = api.copy_previous_roster(
                        fx["gid"], team_id=fx["home"], actor_id=ADMIN)
                    self.assertEqual(r["seated"], [home["id"]], (label, r))
                    self.assertEqual(
                        [(s["player_id"], s["reason"])
                         for s in r["skipped"]],
                        [(away["id"], spine.PRIOR_SEAT_UNATTRIBUTED)],
                        (label, r))
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran)

    def test_the_discovery_reason_outranks_every_eligibility_reason(self):
        """Rank 0 of ``SKIP_REASON_PRECEDENCE``, end to end. This candidate
        matches THREE reasons at once — the prior row is unattributed, the
        membership has transferred, and the Player is deactivated — and the
        DISCOVERY-stage reason is the one reported, because a candidate
        whose provenance cannot be proven was never established as a
        candidate for this side and today's eligibility is not consulted at
        all."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                api, fx = self._pair(store)
                with self.subTest(backend=label):
                    p = self._player(api, fx["home"], "Finn Unattributed")
                    mate = self._player(api, fx["home"], "Ada Available")
                    self._seat_prior(api, fx, [p, mate])
                    self._legacy_prior_row(api, fx, p)
                    self._transfer(api, p["id"], fx["ls_id"], fx["ls_id"],
                                   fx["third"])
                    res = api.set_player_active(p["id"], False,
                                                actor_id=ADMIN)
                    self.assertNotIn("error", res, res)
                    # All three really do apply...
                    self.assertEqual(
                        api.roster.seating_block_reason(
                            api.store.get_game(fx["gid"]),
                            api.store.get_player(p["id"])),
                        "membership_transferred", label)
                    r = api.copy_previous_roster(
                        fx["gid"], team_id=fx["home"], actor_id=ADMIN)
                    # ...and the discovery-stage one is what is reported.
                    self.assertEqual(
                        [(x["player_id"], x["reason"])
                         for x in r["skipped"]],
                        [(p["id"], spine.PRIOR_SEAT_UNATTRIBUTED)],
                        (label, r))
                    self.assertEqual(r["seated"], [mate["id"]], (label, r))
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran)


# ======================================================================
# 7. THE TWO DIRECTIONS OF THE TRANSACTION DISTINCTION
# ======================================================================
class _FailInjection(_BatchHarness):
    """A store whose Nth roster insert raises — the "unexpected persistence
    failure" the ruling keeps all-or-nothing.

    Patched on the STORE INSTANCE rather than on the class, so one backend's
    injection cannot leak into the next one's loop iteration."""

    class Boom(RuntimeError):
        pass

    def _fail_after(self, store, n):
        real = store.add_roster_entry
        state = {"n": 0}

        def wrapped(entry):
            state["n"] += 1
            if state["n"] > n:
                raise self.Boom("injected persistence failure")
            return real(entry)

        store.add_roster_entry = wrapped
        return state

    def _fail_the_batch_audit(self, store):
        """Raise on the BATCH audit write — the LAST write of the unit, and
        the only injection point that can tell one outer transaction apart
        from a chain of nested ones.

        ``select_roster`` and ``set_availability`` are each
        ``@_transactional`` in their own right. If the batch entry point
        were NOT itself transactional, those inner blocks would each be the
        OUTERMOST transaction and would COMMIT as they went, so a failure
        here would leave the seats and the confirmations behind. Injecting
        earlier (inside ``select_roster``'s own inserts) cannot see that
        difference: that failure rolls back either way."""
        real = store.add_audit
        state = {"n": 0, "batch": 0}

        def wrapped(entry):
            state["n"] += 1
            if entry.action == AuditAction.ROSTER_BATCH_SEATED:
                state["batch"] += 1
                raise self.Boom("injected audit failure")
            return real(entry)

        store.add_audit = wrapped
        return state


class AnEligibilitySkipRollsNothingBackButAFailureRollsEverythingBack(
        _FailInjection, unittest.TestCase):
    """BOTH directions, because only the pair is meaningful.

    A batch that catches its own failures indiscriminately would pass the
    first test and fail the second; a batch wrapped in a blanket try/except
    would pass the second and fail the first. The distinction is exactly
    what the correction's "partition must be a decision made from
    non-raising classification" buys."""

    def test_a_skip_commits_the_eligible_rows(self):
        """DIRECTION 1 — an eligibility SKIP is not a failure. The seated
        rows persist, and they persist THROUGH the transaction boundary
        (re-read from the store after the call returns)."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                api, fx = self._pair(store)
                with self.subTest(backend=label):
                    ids = self._copy_cohort(api, fx)
                    seated, skipped = self._expected(COPY_SHAPES, ids)
                    r = api.copy_previous_roster(
                        fx["gid"], team_id=fx["home"], actor_id=ADMIN)
                    self.assertNotIn("error", r, (label, r))
                    self.assertEqual(sorted(self._occupying(api, fx["gid"])),
                                     sorted(seated), label)
                    self.assertEqual(len(skipped), 5, skipped)
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran)

    def test_an_injected_failure_after_an_eligible_write_rolls_all_back(self):
        """DIRECTION 2 — an UNEXPECTED failure after the first successful
        roster insert rolls back the roster rows AND the audit rows AND the
        availability rows. Identity snapshots, so a swap cannot pass."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                api, fx = self._pair(store)
                with self.subTest(backend=label):
                    ids = self._copy_cohort(api, fx)
                    before = self._writes(api, fx["gid"], fx["pid"])
                    state = self._fail_after(store, 1)
                    with self.assertRaises(self.Boom):
                        api.roster.copy_previous_roster(
                            fx["gid"], team_id=fx["home"], actor_id=ADMIN)
                    # The injection really did fire AFTER a successful write
                    # — otherwise this would be testing a no-op.
                    self.assertEqual(state["n"], 2, (label, state))
                    del store.add_roster_entry
                    after = self._writes(api, fx["gid"], fx["pid"])
                    self.assertEqual(after, before, label)
                    self.assertEqual(self._occupying(api, fx["gid"]), [],
                                     label)
                    self.assertEqual(
                        [a for a in api.store.audit_for_game(fx["gid"])
                         if a.action == AuditAction.ROSTER_BATCH_SEATED],
                        [], label)
                    self.assertNotIn(ids["Ada Available"],
                                     self._occupying(api, fx["gid"]), label)
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran)

    def test_a_failure_in_the_batch_audit_unwinds_the_seats_it_recorded(self):
        """The injection that distinguishes ONE OUTER TRANSACTION from a
        chain of independently-committing nested ones. See
        ``_fail_the_batch_audit``: the batch audit row is the last write of
        the unit, so a failure there is only recoverable if the seats
        ``select_roster`` already wrote are in the SAME transaction."""
        ran = []
        for entry_point in ("copy", "build"):
            for label, store in self._stores():
                try:
                    self._assert_backend(label, store)
                    store.clear_all_data()
                    api, fx = self._pair(store)
                    with self.subTest(backend=label, entry=entry_point):
                        if entry_point == "copy":
                            self._copy_cohort(api, fx)
                            call = lambda: api.roster.copy_previous_roster(
                                fx["gid"], team_id=fx["home"],
                                actor_id=ADMIN)
                        else:
                            self._build_cohort(api, fx)
                            call = lambda: api.roster.auto_build_roster(
                                fx["gid"], team_id=fx["home"],
                                actor_id=ADMIN)
                        before = self._writes(api, fx["gid"], fx["pid"])
                        state = self._fail_the_batch_audit(store)
                        with self.assertRaises(self.Boom):
                            call()
                        # The injection fired on the BATCH row, and only
                        # after the per-seating audit rows were written —
                        # i.e. genuinely after successful writes.
                        self.assertEqual(state["batch"], 1, (label, state))
                        self.assertGreater(state["n"], 1, (label, state))
                        del store.add_audit
                        self.assertEqual(
                            self._writes(api, fx["gid"], fx["pid"]), before,
                            (label, entry_point))
                        self.assertEqual(self._occupying(api, fx["gid"]), [],
                                         (label, entry_point))
                        self.assertEqual(
                            list(api.store.availability_for_game(fx["gid"])),
                            [], (label, entry_point))
                    if entry_point == "copy":
                        ran.append(label)
                finally:
                    self._close(label, store)
        self._assert_ran(ran)

    def test_auto_build_failure_rolls_back_seats_and_confirmations(self):
        """The same, for the entry point that also CONFIRMS. Its old shape
        (one select_roster transaction, then N separate set_availability
        transactions) could not roll this back at all: a failure mid-loop
        left players seated but unconfirmed with nothing to unwind."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                api, fx = self._pair(store)
                with self.subTest(backend=label):
                    self._build_cohort(api, fx)
                    before = self._writes(api, fx["gid"])
                    state = self._fail_after(store, 1)
                    with self.assertRaises(self.Boom):
                        api.roster.auto_build_roster(
                            fx["gid"], team_id=fx["home"], actor_id=ADMIN)
                    self.assertEqual(state["n"], 2, (label, state))
                    del store.add_roster_entry
                    after = self._writes(api, fx["gid"])
                    self.assertEqual(after, before, label)
                    self.assertEqual(
                        list(api.store.availability_for_game(fx["gid"])), [],
                        label)
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran)


# ======================================================================
# 8. CLASSIFICATION IS INSIDE THE TRANSACTION
# ======================================================================
class ClassificationReadsLiveStateNotTheDiscoverySnapshot(
        _BatchHarness, unittest.TestCase):
    """THE reason the correction insists classification stays inside the
    transaction: a membership change landing between candidate DISCOVERY and
    the batch must be caught by the in-transaction revalidation.

    THE HOOK, and what it does and does not prove. ``_lock_candidates`` is
    the exact seam the correction names — it runs AFTER discovery and BEFORE
    the partition — so mutating there reproduces "a change that lands
    between the two" deterministically, with no threads and no timing. On
    Memory and SQLite(``:memory:``) there is only ONE connection (the store
    holds a process-wide lock for the whole transaction body), so the
    mutation necessarily rides in the batch's own transaction; what this
    proves there is the half that is actually in question — that the
    partition re-reads LIVE state rather than deciding from the pool it
    discovered. ``ACommittedChangeFromASecondConnectionCannotRaceTheBatch``
    below proves the genuine two-connection case on real PostgreSQL.

    RED against a classify-BEFORE-transaction implementation: the candidate
    would be seated, because the pool was decided before the change."""

    def _hook(self, api, fn):
        roster = api.roster
        real = roster._lock_candidates
        fired = {"n": 0}

        def wrapped(player_ids):
            if not fired["n"]:
                fired["n"] += 1
                fn()
            return real(player_ids)

        roster._lock_candidates = wrapped
        return fired

    def test_a_change_between_discovery_and_the_partition_is_caught(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                api, fx = self._pair(store)
                with self.subTest(backend=label):
                    late = self._player(api, fx["home"], "Late Leaver")
                    mate = self._player(api, fx["home"], "Ada Available")
                    self._seat_prior(api, fx, [mate, late])
                    stint = self._stint_id(api, late["id"], fx["ls_id"])

                    def park():
                        end_membership_directly(api.store, stint,
                                                "transferred")

                    fired = self._hook(api, park)
                    r = api.copy_previous_roster(
                        fx["gid"], team_id=fx["home"], actor_id=ADMIN)
                    del api.roster._lock_candidates
                    self.assertEqual(fired["n"], 1, label)
                    self.assertNotIn("error", r, (label, r))
                    # Discovered as a candidate (so still REPORTABLE)...
                    self.assertEqual(r["candidate_count"], 2, (label, r))
                    # ...and refused by the revalidation, not seated.
                    self.assertEqual(r["seated"], [mate["id"]], (label, r))
                    self.assertEqual(
                        [(s["player_id"], s["reason"])
                         for s in r["skipped"]],
                        [(late["id"], "membership_transferred")], (label, r))
                    self.assertEqual(self._occupying(api, fx["gid"]),
                                     [mate["id"]], label)
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran)


class ACommittedChangeFromASecondConnectionCannotRaceTheBatch(
        _BatchHarness, unittest.TestCase):
    """The genuine two-connection race, on real PostgreSQL only — the only
    backend where a second connection exists at all (Memory serializes on a
    process-wide RLock and ``SqlStore(":memory:")`` is one private database
    per handle, so on both of those a concurrent writer is not merely
    unlikely, it is unconstructible).

    DETERMINISTIC, not sampled. The batch is PARKED inside its transaction
    at ``_lock_candidates`` — after discovery, before the partition — on a
    barrier the test controls. A SECOND SqlStore on the SAME DSN then
    commits ``set_player_active(False)`` in its own transaction, and only
    once that commit has returned is the batch released. There is no sleep
    and no polling window: the ordering is forced by two ``threading.Event``
    handoffs.

    The mutator can commit while the batch holds the Season row lock because
    ``set_player_active`` locks only the PLAYER row — which is precisely why
    the batch takes that lock too, and why it must take it BEFORE
    classifying rather than leaving ``select_roster`` to discover the
    problem and abort the whole run."""

    def setUp(self):
        url = os.environ.get("TEST_DATABASE_URL")
        if not url:
            _announce_pg_skip("BATCH SEATING RACE")
            self.skipTest(_PG_SKIP)
        self.url = url

    def test_a_commit_landing_before_the_locks_is_seen_by_the_partition(self):
        store = fresh_sql_store(self.url)
        other = None
        try:
            self.assertEqual(store.backend, "postgres", store.backend)
            store.clear_all_data()
            api, fx = self._pair(store)
            late = self._player(api, fx["home"], "Late Leaver")
            mate = self._player(api, fx["home"], "Ada Available")
            self._seat_prior(api, fx, [mate, late])

            other = SqlStore(self.url)
            self.assertEqual(other.backend, "postgres", other.backend)
            from hockey_scheduler.api import ApiService
            other_api = ApiService(other)

            parked = threading.Event()
            committed = threading.Event()
            errors = []

            def mutator():
                try:
                    self.assertTrue(parked.wait(20), "batch never parked")
                    res = other_api.set_player_active(late["id"], False,
                                                      actor_id=ADMIN)
                    self.assertNotIn("error", res, res)
                finally:
                    committed.set()

            real = api.roster._lock_candidates
            fired = {"n": 0}

            def wrapped(player_ids):
                if not fired["n"]:
                    fired["n"] += 1
                    parked.set()
                    if not committed.wait(20):
                        errors.append("mutator never committed")
                return real(player_ids)

            api.roster._lock_candidates = wrapped
            t = threading.Thread(target=mutator, daemon=True)
            t.start()
            r = api.copy_previous_roster(fx["gid"], team_id=fx["home"],
                                         actor_id=ADMIN)
            t.join(timeout=30)
            del api.roster._lock_candidates
            self.assertEqual(errors, [], errors)
            self.assertEqual(fired["n"], 1)
            self.assertFalse(t.is_alive())

            # The deactivation was committed by ANOTHER connection while
            # this transaction was open; the in-transaction revalidation
            # saw it.
            self.assertNotIn("error", r, r)
            self.assertEqual(r["candidate_count"], 2, r)
            self.assertEqual(r["seated"], [mate["id"]], r)
            self.assertEqual([(s["player_id"], s["reason"])
                              for s in r["skipped"]],
                             [(late["id"], spine.PLAYER_INACTIVE)], r)
            self.assertEqual(self._occupying(api, fx["gid"]), [mate["id"]])
        finally:
            if other is not None:
                other.close()
            store.reset_schema()
            store.close()


class TheBatchLocksBlockAConcurrentChangeUntilItCommits(_BatchHarness,
                                                       unittest.TestCase):
    """The OTHER half of "acquire the relevant locks, revalidate every
    candidate, partition … then seat": once the batch has classified a
    candidate as seatable, a concurrent deactivation must not slip in
    between the partition and the seating and turn the run into the very
    abort this ruling abolished.

    DETERMINISTIC, via a POSITIVE lock-wait observation rather than a sleep
    — the ``pg_stat_activity`` barrier test_reassignment_fks.py already
    uses. The batch is parked immediately AFTER ``_partition_candidates``
    returns (so its Season lock and every candidate Player lock are held); a
    second connection then calls ``set_player_active`` on a candidate the
    partition just called seatable, and a THIRD, monitoring connection polls
    until THAT EXACT backend pid is ``active`` and waiting on a heavyweight
    ``Lock``. Only then is the batch released. Nothing here depends on a
    timeout elapsing: if the lock were not held the mutator would commit at
    once, the poll would never see a waiter, and the test would FAIL rather
    than pass slowly.

    PostgreSQL only, and honestly so: ``InMemoryStore`` serializes every
    transaction on one process-wide RLock and ``SqlStore(":memory:")`` is a
    private database per handle, so on neither backend can a second writer
    exist to be blocked."""

    def setUp(self):
        url = os.environ.get("TEST_DATABASE_URL")
        if not url:
            _announce_pg_skip("BATCH SEATING LOCKS")
            self.skipTest(_PG_SKIP)
        self.url = url

    def _backend_pid(self, store):
        with store.conn.cursor() as cur:
            cur.execute("SELECT pg_backend_pid() AS pid")
            return cur.fetchone()["pid"]

    def _wait_until_blocked_on_lock(self, backend_pid, timeout=15.0):
        """Poll until ``backend_pid`` is ACTIVELY waiting on a heavyweight
        lock. The poll interval only bounds busy-spin; correctness comes
        from the per-PID lock state, so an unrelated waiter can never
        satisfy it."""
        import time

        import psycopg
        deadline = time.monotonic() + timeout
        with psycopg.connect(self.url, autocommit=True) as mon:
            while time.monotonic() < deadline:
                with mon.cursor() as cur:
                    cur.execute(
                        "SELECT state, wait_event_type FROM pg_stat_activity "
                        "WHERE pid = %s", (backend_pid,))
                    row = cur.fetchone()
                    if (row is not None and row[0] == "active"
                            and row[1] == "Lock"):
                        return True
                time.sleep(0.02)
        return False

    def test_a_deactivation_cannot_land_between_partition_and_seating(self):
        store = fresh_sql_store(self.url)
        other = None
        try:
            self.assertEqual(store.backend, "postgres", store.backend)
            store.clear_all_data()
            api, fx = self._pair(store)
            keeper = self._player(api, fx["home"], "Ada Available")
            self._seat_prior(api, fx, [keeper])

            other = SqlStore(self.url)
            from hockey_scheduler.api import ApiService
            other_api = ApiService(other)
            mutator_pid = self._backend_pid(other)

            partitioned = threading.Event()
            release = threading.Event()
            outcome = {}

            def mutator():
                try:
                    self.assertTrue(partitioned.wait(20),
                                    "batch never partitioned")
                    outcome["mutate"] = other_api.set_player_active(
                        keeper["id"], False, actor_id=ADMIN)
                except BaseException as exc:      # surfaced by the asserts
                    outcome["mutate_error"] = repr(exc)

            real = api.roster._partition_candidates
            fired = {"n": 0}

            def wrapped(*a, **kw):
                result = real(*a, **kw)
                if not fired["n"]:
                    fired["n"] += 1
                    partitioned.set()
                    release.wait(25)
                return result

            api.roster._partition_candidates = wrapped
            t = threading.Thread(target=mutator, daemon=True)
            t.start()
            batch = {}

            def run_batch():
                try:
                    batch["result"] = api.copy_previous_roster(
                        fx["gid"], team_id=fx["home"], actor_id=ADMIN)
                except BaseException as exc:
                    batch["error"] = repr(exc)

            b = threading.Thread(target=run_batch, daemon=True)
            b.start()
            self.assertTrue(partitioned.wait(20), "batch never partitioned")
            # THE POSITIVE BARRIER: that exact backend really is blocked on
            # a row lock this batch holds.
            self.assertTrue(
                self._wait_until_blocked_on_lock(mutator_pid),
                "the deactivating backend never registered as waiting on a "
                "lock — the batch is not holding the candidate Player row")
            release.set()
            b.join(timeout=30)
            t.join(timeout=30)
            del api.roster._partition_candidates
            self.assertNotIn("error", batch, batch)
            self.assertNotIn("mutate_error", outcome, outcome)
            self.assertNotIn("error", outcome["mutate"], outcome)

            # The batch was NOT aborted, and it seated the candidate it had
            # classified as seatable.
            self.assertEqual(batch["result"]["seated"], [keeper["id"]],
                             batch)
            self.assertEqual(batch["result"]["skipped"], [], batch)
            self.assertEqual(self._occupying(api, fx["gid"]), [keeper["id"]])
            # …and the deactivation landed afterwards, as its own committed
            # change — it was serialized, not lost.
            check = SqlStore(self.url)
            try:
                self.assertFalse(check.get_player(keeper["id"]).is_active)
            finally:
                check.close()
        finally:
            if other is not None:
                other.close()
            store.reset_schema()
            store.close()


# ======================================================================
# 9. THE REAL HTTP SURFACE, both routes
# ======================================================================
class _HttpBatchHarness(_BatchHarness):
    """A real listening socket + a real authenticated session, tri-store.

    The facade tests above prove the RULE; they do not prove the transport
    relays the new identity keys as JSON rather than dropping them, nor that
    the routes still exist under the same names. Both routes are posted to
    for real, against Memory, SQLite and PostgreSQL in turn — ``srv.STATE.api``
    is pointed at THIS fixture's ApiService for the duration, so the request
    runs against the same store the assertions read."""

    @classmethod
    def setUpClass(cls):
        cls._saved_api = srv.STATE.api
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)
        cls.httpd.server_close()
        srv.STATE.api = cls._saved_api

    def _req(self, opener, method, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with opener.open(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            # Closed explicitly: an HTTPError holds an open response body,
            # and letting the GC reclaim it emits a ResourceWarning at
            # interpreter shutdown that run_parallel.py deliberately does
            # NOT filter.
            with e:
                return e.code, json.loads(e.read() or b"{}")

    def _serve(self, api, label):
        api.accounts.create_account(
            "admin", DEMO_PASSWORD, DEMO_USERS["admin"], scope={},
            actor_id="test_seed", account_id="user_admin")
        srv.STATE.api = api
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))
        status, body = self._req(opener, "POST", "/api/auth/login",
                                 {"username": "admin", "password": "demo"})
        self.assertEqual(status, 200, (label, body))
        return opener


class BothBatchRoutesReportThePartialOutcomeOverRealHttp(_HttpBatchHarness,
                                                         unittest.TestCase):

    def test_copy_previous_route_returns_seated_and_skipped(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                api, fx = self._pair(store)
                with self.subTest(backend=label):
                    ids = self._copy_cohort(api, fx)
                    seated, skipped = self._expected(COPY_SHAPES, ids)
                    opener = self._serve(api, label)
                    status, body = self._req(
                        opener, "POST",
                        f"/api/games/{fx['gid']}/roster/copy-previous",
                        {"team_id": fx["home"]})
                    self.assertEqual(status, 200, (label, body))
                    self.assertEqual(body["seated"], seated, (label, body))
                    self.assertEqual(body["skipped"], skipped, (label, body))
                    self.assertEqual(body["copied"], len(seated),
                                     (label, body))
                    self.assertEqual(sorted(self._occupying(api, fx["gid"])),
                                     sorted(seated), label)
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "BATCH SEATING HTTP")

    def test_build_roster_route_returns_seated_and_skipped(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                api, fx = self._pair(store)
                with self.subTest(backend=label):
                    ids = self._build_cohort(api, fx)
                    seated, skipped = self._expected(BUILD_SHAPES, ids)
                    opener = self._serve(api, label)
                    status, body = self._req(
                        opener, "POST",
                        f"/api/games/{fx['gid']}/build-roster",
                        {"team_id": fx["home"]})
                    self.assertEqual(status, 200, (label, body))
                    self.assertEqual(body["seated"], seated, (label, body))
                    self.assertEqual(body["skipped"], skipped, (label, body))
                    # The status keys the UI has always read are still there.
                    self.assertIn("short_roster", body, sorted(body))
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "BATCH SEATING HTTP")

    def test_a_zero_seat_copy_is_a_200_over_http_not_an_error(self):
        """The ruling's "return a SUCCESSFUL zero-seat result", proven at
        the transport: an operator-visible partial outcome must not arrive
        as a 4xx that the UI renders as a failure."""
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                api, fx = self._pair(store)
                with self.subTest(backend=label):
                    p = self._player(api, fx["home"], "Gia Transferred")
                    self._seat_prior(api, fx, [p])
                    self._transfer(api, p["id"], fx["ls_id"], fx["ls_id"],
                                   fx["third"])
                    opener = self._serve(api, label)
                    status, body = self._req(
                        opener, "POST",
                        f"/api/games/{fx['gid']}/roster/copy-previous",
                        {"team_id": fx["home"]})
                    self.assertEqual(status, 200, (label, body))
                    self.assertNotIn("error", body, (label, body))
                    self.assertEqual(body["seated"], [], (label, body))
                    self.assertEqual(body["copied"], 0, (label, body))
                    self.assertEqual(
                        [s["reason"] for s in body["skipped"]],
                        ["membership_transferred"], (label, body))
                    self.assertEqual(self._occupying(api, fx["gid"]), [],
                                     label)
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran, "BATCH SEATING HTTP")


# ======================================================================
# 10. SINGLE MUTATIONS STILL FAIL CLOSED
# ======================================================================
class TheRulingDidNotRelaxIndividualMutations(_BatchHarness,
                                              unittest.TestCase):
    """"This ruling does not relax the live-eligibility requirement for
    individual mutations."

    ``select_roster`` is the primitive the batch calls. If it had grown a
    skip mode — or if the batch had reached its goal by loosening it — a
    DIRECT call naming an ineligible player would now succeed partially
    instead of failing closed. It must still raise, and seat nobody."""

    def test_select_roster_still_refuses_the_whole_call(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                store.clear_all_data()
                api, fx = self._pair(store)
                with self.subTest(backend=label):
                    ok = self._player(api, fx["home"], "Ada Available")
                    gone = self._player(api, fx["home"], "Gia Transferred")
                    self._transfer(api, gone["id"], fx["ls_id"], fx["ls_id"],
                                   fx["third"])
                    before = self._writes(api, fx["gid"])
                    res = api.select_roster(
                        fx["gid"], [ok["id"], gone["id"]], actor_id=ADMIN)
                    self.assertEqual(res["error"]["code"], "not_eligible",
                                     (label, res))
                    self.assertEqual(self._writes(api, fx["gid"]), before,
                                     label)
                    self.assertEqual(self._occupying(api, fx["gid"]), [],
                                     label)
                ran.append(label)
            finally:
                self._close(label, store)
        self._assert_ran(ran)

    def test_no_skip_flag_reached_the_seating_primitive(self):
        """A source-level tripwire for the same rule, because a future
        caller could reintroduce the flag without any behavioural test
        noticing until it was already used."""
        import inspect
        sig = inspect.signature(RosterService.select_roster)
        self.assertEqual(list(sig.parameters),
                         ["self", "game_id", "player_ids", "actor_id"],
                         list(sig.parameters))


if __name__ == "__main__":
    unittest.main()
