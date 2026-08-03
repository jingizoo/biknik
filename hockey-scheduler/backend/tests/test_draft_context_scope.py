"""Active-tuple scoping for the season scheduler's draft surface (blocker #386).

The blocker. ``draft_season_schedule`` accepted no principal at all -- no
``user_id``, no ``role``, no ``scope`` -- and ``POST /api/scheduler/draft``
passed none, so the endpoint was authorized by the ``MANAGE_SCHEDULE``
capability and by nothing else. League Admin and Arena Manager both hold it and
both are ``context_scope._GLOBAL_ROLES`` (authorized for every Program in the
installation), so an operator whose active context was Program B could post
Program A's identifiers and receive A's whole draft proposal: every pairing,
both team names per pairing, and the ice slot each game would occupy. #369's
rule, restated once more -- *one asserted capability fact is not the whole
workflow capability*: ``MANAGE_SCHEDULE`` establishes what the caller may do,
never WHERE.

It sat directly beside #381's carefully bound scenario routes, which is what
made it a live bypass rather than a latent one: a caller refused a foreign
scenario could ask THIS endpoint for the same Program's proposal and read the
team names straight out of it.

The fix reuses the machinery already here rather than inventing a second one:

* the tuple is resolved SERVER-SIDE via
  ``ContextService.resolve_with_league(user_id, role, scope)``. Scope ids in a
  request body select WHICH rows to consider and are never entitlement to them;
* the REQUESTED hierarchy is resolved into a real ``(Program, Season, League)``
  and that whole edge is judged by ``_setup_target_edge_allows`` verbatim --
  #369's "edges, not unions" predicate. Three independent axis unions would
  authorize combinations that do not exist;
* the check is a CALLBACK handed to ``resolve_scenario_scope``, invoked at the
  earliest point each request shape knows its whole edge, because the
  Season+League shape learns its facts in a LADDER whose earlier rungs refuse
  with their own distinct reasons -- an existence oracle answered before any
  authorization ran;
* a foreign hierarchy and a nonexistent one leave through ONE refusal, so they
  are response-identical in status AND BYTES;
* ``role is None`` -- internal call sites, ``create_schedule_scenario``'s own
  nested generation, the demo/full seeds, the acceptance harnesses -- is
  ungated and completely untouched, matching ``setup_target_accessible``
  rule 1.

The sibling entry points on the other side of the commit carried the identical
omission and are bound here too: ``commit_draft_schedule`` (the same
caller-supplied target, behind a verb that WRITES), ``list_draft_games``
(every draft Game in the installation, both team names, Division, Rink and
time), and ``publish_draft_games`` / ``discard_draft_games`` (a ``game_ids``
list straight from the request body, or ``all: true`` over the installation).

Coverage, on Memory/SQLite/PostgreSQL at the service boundary and over real
authenticated HTTP:

1. the positive exact-tuple CONTROL, first, so every negative below is
   measured against a corner that really does produce a non-empty proposal;
2. a B-selected League Admin and Arena Manager cannot draft A -- in the
   Division shape and in the League-wide shape;
3. the two near-miss corners the owner named -- same Program/different Season
   and same Season/different League -- both refused;
4. the LADDER: a junk ``division_id`` alongside a FOREIGN ``season_id`` +
   ``league_id`` answers identically to a guessed pair;
5. foreign and nonexistent are byte-identical -- compared on RAW HTTP BYTES
   with the caller's own echoed ids masked, never on parsed JSON;
6. no foreign team, slot, rink, venue, blackout or time identifier appears
   ANYWHERE in a refused response;
7. ``role is None`` is unchanged;
8. commit is bound at the preflight AND re-authorized under the write locks;
9. the draft-review surface (list/publish/discard) is bound to the same tuple.
"""

import copy
import json
import os
import re
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND, fresh_sql_store  # noqa: F401  (sets up sys.path)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import Role
from hockey_scheduler.store import InMemoryStore, SqlStore
from hockey_scheduler.web import server as srv


ADMIN = "user_draft_admin"
ARENA = "user_draft_arena"
ICE_BASE = datetime(2026, 9, 7, 18, tzinfo=timezone.utc)
GLOBAL_PRINCIPALS = ((ADMIN, Role.LEAGUE_ADMIN), (ARENA, Role.ARENA_MANAGER))
# An operator that has NEVER saved a context. Deliberately its own id:
# sharing one with the classes above would let their `set_active_context`
# calls create the very row the absent-row races exist to test without.
NEWCOMER = "user_draft_newcomer"

# A guessed id of each kind, in the same sequential namespace the counters hand
# out. These are what "nonexistent" means throughout this file.
NO_SUCH_DIVISION = "division_no_such_row"
NO_SUCH_SEASON = "season_no_such_row"
NO_SUCH_LEAGUE = "league_no_such_row"


def _backends():
    """Memory/SQLite always; PostgreSQL only with ``TEST_DATABASE_URL`` set --
    the same idiom ``test_schedule_scenario_scope.py`` uses."""
    yield "memory", InMemoryStore()
    yield "sqlite", SqlStore(":memory:")
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        yield "postgres", fresh_sql_store(url)


def _close(store):
    if isinstance(store, SqlStore):
        store.close()


def _ok(result, label=""):
    assert isinstance(result, dict) and "error" not in result, (label, result)
    return result


def _game_counter(store):
    """The stored ``game`` id counter, read WITHOUT allocating one -- the exact
    idiom ``test_schedule_scenario_scope`` already uses, because PostgreSQL
    test databases retain counters across per-test data clears."""
    if isinstance(store, InMemoryStore):
        return store._counters.get("game")
    row = store._exec("SELECT value FROM counters WHERE prefix = ?",
                      ("game",)).fetchone()
    return row["value"] if row else None


def _league_season(api, tag, program, season_id, league_id, club_id, rink_id,
                   slot_day, teams=4, actor_id=None):
    """One schedulable ``(Season, League)`` corner: a Division, ``teams``
    permanent Teams registered into it, and enough ice for a full single
    round-robin.

    Enough ice is the load-bearing part, and it is why this file builds its own
    hierarchy rather than leaning on the demo seed. Every negative below is
    measured against a corner that really CAN produce a non-empty proposal, so
    "refused" can never be confused with "there was nothing to schedule here
    anyway" -- the anti-vacuity rule #381 established for exactly this shape of
    test.
    """
    division = _ok(api.create_division(
        season_id, f"Division {tag}", league_id=league_id, actor_id=actor_id),
        "division")
    team_ids, team_names = [], []
    for index in range(teams):
        name = f"{tag} Team {index}"
        team = _ok(api.create_team(
            club_id, None, name, actor_id=actor_id,
            program_id=program, league_id=league_id), "team")
        _ok(api.register_team_for_season(
            season_id, team["id"], division["id"], actor_id=actor_id,
            league_id=league_id), "registration")
        team_ids.append(team["id"])
        team_names.append(name)
    # C(teams, 2) fixtures need at least that many distinct slots.
    slot_ids, slot_times = [], []
    for index in range((teams * (teams - 1)) // 2 + 2):
        start = ICE_BASE + timedelta(days=slot_day + index)
        slot = _ok(api.create_ice_slot(
            rink_id, start.isoformat(),
            (start + timedelta(hours=2)).isoformat(), actor_id=actor_id),
            "slot")
        slot_ids.append(slot["id"])
        slot_times.append(start.isoformat())
    return {"division": division["id"], "teams": team_ids,
            "team_names": team_names, "slots": slot_ids,
            "slot_times": slot_times}


def build_two_programs(api, actor_id=None):
    """Two Programs, and inside Program A the two near-miss corners the owner
    named: same-Program/different-Season, and same-Season/different-League.

      A / A1 / Aa   -- the target hierarchy in most tests below
      A / A1 / Ab   -- same Program, same Season, DIFFERENT League
      A / A2 / Aa2  -- same Program, DIFFERENT Season
      B / B1 / Ba   -- a wholly foreign Program, and the tuple the attacker
                       is standing in

    Deliberately the same shape as ``test_schedule_scenario_scope``'s fixture:
    the two surfaces share one predicate, so they must be provable against one
    hierarchy rather than two independently-invented ones. Each corner is
    independently schedulable.
    """
    fixture = {}
    day = 0
    for tag in ("A", "B"):
        program = _ok(api.create_program(f"Program {tag}", actor_id=actor_id),
                      "program")
        club = _ok(api.create_club(f"Club {tag}", actor_id=actor_id), "club")
        # `Venue.league_id` is LEGACY vocabulary and stores a PROGRAM id -- not
        # a competition League (`store/integrity_checks.py` joins
        # `seasons s ON s.program_id = v.league_id`). Passing a League id here
        # would silently build a Venue linked to nothing this fixture can grant
        # against, and every corner would come back empty instead of refused.
        venue = _ok(api.create_venue(f"Venue {tag}", league_id=program["id"],
                                     actor_id=actor_id), "venue")
        rink = _ok(api.create_rink(venue["id"], f"Rink {tag}",
                                   actor_id=actor_id), "rink")
        fixture[tag] = {"program": program["id"], "club": club["id"],
                        "venue": venue["id"], "venue_name": f"Venue {tag}",
                        "rink": rink["id"], "rink_name": f"Rink {tag}",
                        "seasons": {}}
        for season_tag in (("1", "2") if tag == "A" else ("1",)):
            season = _ok(api.create_season(
                program["id"], f"Season {tag}{season_tag}",
                actor_id=actor_id), "season")
            _ok(api.grant_season_venue_access(
                season["id"], venue["id"], actor_id=actor_id), "grant")
            leagues = {}
            names = ("a", "b") if (tag, season_tag) == ("A", "1") else ("a",)
            for league_tag in names:
                league = _ok(api.create_league(
                    season["id"], f"League {tag}{season_tag}{league_tag}",
                    actor_id=actor_id), "league")
                corner_rows = _league_season(
                    api, f"{tag}{season_tag}{league_tag}", program["id"],
                    season["id"], league["id"], club["id"], rink["id"], day,
                    actor_id=actor_id)
                day += 12
                leagues[league_tag] = {"league": league["id"], **corner_rows}
            fixture[tag]["seasons"][season_tag] = {
                "season": season["id"], "leagues": leagues}
    return fixture


def corner(fixture, program_tag, season_tag, league_tag):
    """``(program_id, season_id, league_id, division_id)`` for one corner."""
    program = fixture[program_tag]
    season = program["seasons"][season_tag]
    league = season["leagues"][league_tag]
    return (program["program"], season["season"], league["league"],
            league["division"])


def foreign_identifiers(fixture, program_tag, season_tag, league_tag):
    """Every identifier a refused caller must NOT see, for one corner.

    This is the #380-facing clause, generalized. #380 ("bounded schedule
    explanations") will add richer candidate evidence to the draft response --
    per-pairing rejected slots, the rink and venue each belongs to, the
    blackout that excluded it, the times considered. That PR is not merged, so
    its own fields cannot be exercised here. What CAN be pinned, and what will
    still hold when they land, is the general property those fields must obey:
    a refused response names NOTHING of the target hierarchy anywhere in its
    bytes -- not a team, not a slot, not the rink, not the venue, not a
    blackout date, not a candidate time.
    """
    program = fixture[program_tag]
    season = program["seasons"][season_tag]
    league = season["leagues"][league_tag]
    values = {program["program"], season["season"], league["league"],
              league["division"], program["venue"], program["venue_name"],
              program["rink"], program["rink_name"]}
    values.update(league["teams"])
    values.update(league["team_names"])
    values.update(league["slots"])
    values.update(league["slot_times"])
    # The blackout dates the refused requests below actually send, so a
    # refusal that echoed the constraints it was given would be caught too.
    values.update(BLACKOUT_DATES)
    return {value for value in values if value}


# Sent as `constraints` on every refused draft request: a refusal must not echo
# them back either, and #380's evidence will name them per rejected candidate.
BLACKOUT_DATES = ["2026-09-09", "2026-09-19"]
REFUSED_CONSTRAINTS = {"season_blackout_dates": BLACKOUT_DATES}


def leaked_identifiers(secrets, raw):
    """Which of ``secrets`` really appears in ``raw``, sorted.

    Matched on identifier boundaries rather than as a bare substring, because
    the id space is sequential: a plain ``"team_1" in raw`` also fires on
    ``team_10``, which would make this assertion pass for the wrong reason on
    one payload and fail spuriously on another. The lookarounds keep it a
    whole-token search while still catching an id INTERPOLATED INTO A MESSAGE
    (``"Division division_5 not found."``), which an exact-scalar comparison
    over the parsed JSON would miss entirely -- and that interpolation is
    exactly how this class of leak has shipped before.
    """
    return sorted(
        value for value in secrets
        if re.search(r"(?<![A-Za-z0-9_])" + re.escape(value)
                     + r"(?![A-Za-z0-9_])", raw))


class DraftActiveTupleTest(unittest.TestCase):
    """The service boundary, across Memory / SQLite / PostgreSQL."""

    maxDiff = None

    def _on_every_backend(self, body):
        """Run ``body(store, api)`` once per real store.

        A plain loop rather than a generator: a generator that closes stores in
        a ``finally`` wrapped around a ``yield`` only tidies up once it is
        exhausted, so a failing assertion inside the loop would leak the
        connection.
        """
        for label, store in _backends():
            with self.subTest(store=label):
                try:
                    body(store, ApiService(store))
                finally:
                    _close(store)

    def _select(self, api, user_id, role, program_id, season_id, league_id):
        _ok(api.set_active_context(user_id, role, {}, program_id, season_id,
                                   league_id), "set_active_context")

    def _draft(self, api, user_id, role, **target):
        return api.draft_season_schedule(
            user_id=user_id, role=role, scope={}, **target)

    # -- the CONTROL, first ------------------------------------------------
    def test_the_same_actor_in_the_exact_tuple_still_gets_its_proposal(self):
        """Every negative in this file is only meaningful because this same
        actor, standing in the target's exact tuple, really does get a
        non-empty proposal from both request shapes. A test that cannot fail
        is worse than no test.
        """
        def body(store, api):
            fixture = build_two_programs(api)
            pa, sa, la, da = corner(fixture, "A", "1", "a")
            for user_id, role in GLOBAL_PRINCIPALS:
                with self.subTest(role=role.value):
                    self._select(api, user_id, role, pa, sa, la)

                    division_shape = _ok(self._draft(
                        api, user_id, role, division_id=da), "division shape")
                    self.assertEqual(len(division_shape["draft_games"]), 6)
                    self.assertEqual(division_shape["season_id"], sa)

                    league_shape = _ok(self._draft(
                        api, user_id, role, season_id=sa, league_id=la),
                        "league-wide shape")
                    self.assertEqual(len(league_shape["draft_games"]), 6)
                    self.assertEqual(league_shape["league_id"], la)

                    # ...and the constraints the refusals below carry do not
                    # themselves empty the proposal, so a refused response can
                    # never be mistaken for "the blackouts left nothing".
                    constrained = _ok(self._draft(
                        api, user_id, role, division_id=da,
                        constraints=copy.deepcopy(REFUSED_CONSTRAINTS)),
                        "constrained")
                    self.assertTrue(constrained["draft_games"], constrained)
        self._on_every_backend(body)

    # -- clause: a foreign Program is refused ------------------------------
    def test_b_selected_principal_cannot_draft_program_a(self):
        def body(store, api):
            fixture = build_two_programs(api)
            pa, sa, la, da = corner(fixture, "A", "1", "a")
            pb, sb, lb, _db = corner(fixture, "B", "1", "a")
            for user_id, role in GLOBAL_PRINCIPALS:
                with self.subTest(role=role.value):
                    self._select(api, user_id, role, pb, sb, lb)

                    by_division = self._draft(
                        api, user_id, role, division_id=da)
                    self.assertIn(
                        "error", by_division,
                        f"a {role.value} active in Program B received Program "
                        "A's draft proposal by division_id")
                    self.assertEqual(by_division["error"]["code"], "not_found",
                                     by_division)
                    self.assertEqual(
                        by_division["error"]["details"]["reason"],
                        "division_missing", by_division)
                    self.assertNotIn("draft_games", by_division)

                    by_league = self._draft(
                        api, user_id, role, season_id=sa, league_id=la)
                    self.assertIn(
                        "error", by_league,
                        f"a {role.value} active in Program B received Program "
                        "A's draft proposal by caller-supplied season_id + "
                        "league_id")
                    self.assertEqual(
                        by_league["error"]["details"]["reason"],
                        "league_season_missing", by_league)
                    self.assertNotIn("draft_games", by_league)
            # A draft writes nothing, so this is a disclosure test only -- but
            # assert it anyway, because a future "record the request" would
            # otherwise land silently.
            self.assertEqual(store.all_games(), [])
        self._on_every_backend(body)

    def test_the_two_near_miss_corners_are_refused(self):
        """Same Program/different Season, and same Season/different League.

        These are exactly the cases a Program-only ceiling would wave through,
        and the reason the edge is judged whole rather than as three
        independent axis unions.
        """
        def body(store, api):
            fixture = build_two_programs(api)
            pa, sa, la, _da = corner(fixture, "A", "1", "a")
            for user_id, role in GLOBAL_PRINCIPALS:
                for label, (season_tag, league_tag) in (
                        ("different League, same Season", ("1", "b")),
                        ("different Season, same Program", ("2", "a"))):
                    with self.subTest(role=role.value, near_miss=label):
                        self._select(api, user_id, role, pa, sa, la)
                        _p, season, league, division = corner(
                            fixture, "A", season_tag, league_tag)

                        by_division = self._draft(
                            api, user_id, role, division_id=division)
                        self.assertIn(
                            "error", by_division,
                            f"a {role.value} drafted the {label} corner while "
                            "another tuple was selected")
                        self.assertEqual(
                            by_division["error"]["details"]["reason"],
                            "division_missing", by_division)

                        by_league = self._draft(
                            api, user_id, role, season_id=season,
                            league_id=league)
                        self.assertIn(
                            "error", by_league,
                            f"a {role.value} drafted the {label} corner "
                            "league-wide while another tuple was selected")
                        self.assertEqual(
                            by_league["error"]["details"]["reason"],
                            "league_season_missing", by_league)
        self._on_every_backend(body)

    def test_each_near_miss_corner_is_independently_schedulable(self):
        """The anti-vacuity control for the corners above, stated separately.

        If League Ab or Season A2 could not produce a proposal at all, the two
        refusals above would pass with the whole gate reverted. Selecting each
        corner in turn and getting its own six fixtures is what makes them
        genuine refusals.
        """
        def body(store, api):
            fixture = build_two_programs(api)
            for season_tag, league_tag in (("1", "a"), ("1", "b"),
                                           ("2", "a")):
                with self.subTest(corner=f"A{season_tag}{league_tag}"):
                    p, s, lg, d = corner(fixture, "A", season_tag, league_tag)
                    self._select(api, ADMIN, Role.LEAGUE_ADMIN, p, s, lg)
                    proposal = _ok(self._draft(
                        api, ADMIN, Role.LEAGUE_ADMIN, division_id=d))
                    self.assertEqual(len(proposal["draft_games"]), 6)
        self._on_every_backend(body)

    def test_a_program_only_context_fails_closed(self):
        """No Season selected means nothing has been validated to compare a
        Season-bound target against, so every Division of that very Program is
        refused -- ``get_standings``' rule, and #381's.
        """
        def body(store, api):
            fixture = build_two_programs(api)
            pa, sa, la, da = corner(fixture, "A", "1", "a")
            self._select(api, ADMIN, Role.LEAGUE_ADMIN, pa, None, None)
            refused = self._draft(api, ADMIN, Role.LEAGUE_ADMIN,
                                  division_id=da)
            self.assertIn(
                "error", refused,
                "a Program-only context drafted a Season-bound Division of "
                "its own Program -- a missing Season selection must not "
                "silently re-widen to every Season")
            self.assertEqual(refused["error"]["details"]["reason"],
                             "division_missing", refused)
            # ...and selecting the Season restores it, so the refusal above is
            # the ceiling and not an empty corner.
            self._select(api, ADMIN, Role.LEAGUE_ADMIN, pa, sa, la)
            self.assertEqual(
                len(_ok(self._draft(api, ADMIN, Role.LEAGUE_ADMIN,
                                    division_id=da))["draft_games"]), 6)
        self._on_every_backend(body)

    def test_no_league_selected_is_the_program_plus_season_union(self):
        """"No League" stays the first-class selection it is everywhere else:
        it permits every League inside the already-validated Program+Season,
        and nothing outside it. Without this the fix would have broken the
        ordinary operator, which is its own kind of failure."""
        def body(store, api):
            fixture = build_two_programs(api)
            pa, sa, _la, da = corner(fixture, "A", "1", "a")
            _p, _s, _lb, db = corner(fixture, "A", "1", "b")
            _p2, sa2, _la2, da2 = corner(fixture, "A", "2", "a")
            self._select(api, ADMIN, Role.LEAGUE_ADMIN, pa, sa, None)
            for label, division in (("League Aa", da), ("League Ab", db)):
                with self.subTest(union=label):
                    self.assertEqual(len(_ok(self._draft(
                        api, ADMIN, Role.LEAGUE_ADMIN,
                        division_id=division))["draft_games"]), 6)
            # The Season ceiling above it does NOT relax.
            refused = self._draft(api, ADMIN, Role.LEAGUE_ADMIN,
                                  division_id=da2)
            self.assertIn(
                "error", refused,
                "\"No League\" relaxed the SEASON ceiling -- it is a League "
                f"selection only ({sa2} is not the selected Season)")
        self._on_every_backend(body)

    def test_a_principal_authorized_for_no_program_is_refused(self):
        """A null active Program must fail CLOSED, and nothing else in this
        file can reach that branch.

        Every other fixture here drives a GLOBAL role (League Admin / Arena
        Manager are both ``context_scope._GLOBAL_ROLES``), which is authorized
        for every Program in the installation and so can never resolve a null
        one. An unbound scoped identity can, and `program is None -> allow`
        would hand it the whole installation.
        """
        def body(store, api):
            fixture = build_two_programs(api)
            _pa, sa, la, da = corner(fixture, "A", "1", "a")
            nobody = "user_scoped_to_nothing"
            program, _season, _league = api.context.resolve_with_league(
                nobody, Role.COACH, {})
            self.assertIsNone(program, "the fixture no longer isolates the "
                                       "null-Program branch")
            refused = self._draft(api, nobody, Role.COACH, division_id=da)
            self.assertIn(
                "error", refused,
                "a principal authorized for NO Program drafted a schedule")
            league_wide = self._draft(api, nobody, Role.COACH, season_id=sa,
                                      league_id=la)
            self.assertIn(
                "error", league_wide,
                "a principal authorized for NO Program drafted a League")
            self.assertEqual(
                _ok(api.list_draft_games(nobody, Role.COACH, {}))
                ["draft_games"], [],
                "a principal authorized for NO Program listed draft games")
        self._on_every_backend(body)

    # -- clause: the ladder ------------------------------------------------
    def test_a_junk_division_beside_foreign_scope_ids_is_not_an_oracle(self):
        """The trap #381 hit and fixed, checked here on its own surface.

        The League-wide shape learns its facts in a LADDER, and each rung
        refuses with its own reason: ``require_league_belongs_to_season``
        answers ``league_missing`` for a League that does not exist and
        ``league_season_mismatch`` for a real League not bound to the Season,
        and only past both of those does the Division lookup answer
        ``division_missing``. Authorizing on the fully resolved hierarchy
        therefore leaves the earlier rungs answerable to anyone: send a junk
        ``division_id`` alongside a foreign ``(season_id, league_id)`` and the
        reason tells you whether that LeagueSeason is real -- over ids the
        counters hand out in sequence.
        """
        def body(store, api):
            fixture = build_two_programs(api)
            pa, sa, la, _da = corner(fixture, "A", "1", "a")
            pb, sb, lb, _db = corner(fixture, "B", "1", "a")
            for user_id, role in GLOBAL_PRINCIPALS:
                with self.subTest(role=role.value):
                    self._select(api, user_id, role, pb, sb, lb)
                    real_pair = self._draft(
                        api, user_id, role, season_id=sa, league_id=la,
                        division_id=NO_SUCH_DIVISION)
                    guessed_pair = self._draft(
                        api, user_id, role, season_id=NO_SUCH_SEASON,
                        league_id=NO_SUCH_LEAGUE,
                        division_id=NO_SUCH_DIVISION)
                    self.assertEqual(
                        json.dumps(real_pair, sort_keys=True),
                        json.dumps(guessed_pair, sort_keys=True),
                        "a junk division_id alongside a FOREIGN season_id + "
                        "league_id answered differently from a guessed pair "
                        "-- the refusal tells an unauthorized caller whether "
                        "that LeagueSeason is real")
                    # And a REAL League that simply is not bound to the
                    # requested Season answers the same way too, so the second
                    # rung is closed as well as the first.
                    unbound = self._draft(
                        api, user_id, role, season_id=sa, league_id=lb,
                        division_id=NO_SUCH_DIVISION)
                    self.assertEqual(
                        json.dumps(unbound, sort_keys=True),
                        json.dumps(guessed_pair, sort_keys=True),
                        "a real-but-unbound foreign League answered "
                        "differently from a guessed one")
                    self.assertNotEqual(pa, pb)   # the fixture really is two
        self._on_every_backend(body)

    # -- clause: foreign and nonexistent are response-identical ------------
    def test_foreign_and_nonexistent_targets_are_response_identical(self):
        def body(store, api):
            fixture = build_two_programs(api)
            _pa, sa, la, da = corner(fixture, "A", "1", "a")
            pb, sb, lb, _db = corner(fixture, "B", "1", "a")
            for user_id, role in GLOBAL_PRINCIPALS:
                with self.subTest(role=role.value):
                    self._select(api, user_id, role, pb, sb, lb)
                    for label, foreign, missing in (
                            ("division shape",
                             {"division_id": da},
                             {"division_id": NO_SUCH_DIVISION}),
                            ("league-wide shape",
                             {"season_id": sa, "league_id": la},
                             {"season_id": NO_SUCH_SEASON,
                              "league_id": NO_SUCH_LEAGUE})):
                        with self.subTest(shape=label):
                            denied = self._draft(api, user_id, role,
                                                 **foreign)
                            guessed = self._draft(api, user_id, role,
                                                  **missing)
                            # Neither refusal echoes ANY id, so there is
                            # nothing to mask: they must be equal outright.
                            self.assertEqual(
                                json.dumps(denied, sort_keys=True),
                                json.dumps(guessed, sort_keys=True),
                                f"the {label} refusal for a FOREIGN target "
                                "differs from the one for a NONEXISTENT "
                                "target -- the pair of wordings is the oracle")
        self._on_every_backend(body)

    # -- the #380-facing clause --------------------------------------------
    def test_a_refusal_names_nothing_of_the_target_hierarchy(self):
        """Not one identifier of the refused hierarchy appears anywhere in the
        response bytes.

        #380 ("bounded schedule explanations") is not merged, so its own
        candidate-evidence fields cannot be exercised here. This asserts the
        general property they will have to obey, on the response shape that
        exists today, written so it keeps holding when the richer evidence
        lands: whatever a draft response grows, a REFUSED one still names no
        team, slot, rink, venue, blackout or time of the target hierarchy.
        """
        def body(store, api):
            fixture = build_two_programs(api)
            _pa, sa, la, da = corner(fixture, "A", "1", "a")
            pb, sb, lb, _db = corner(fixture, "B", "1", "a")
            secrets = foreign_identifiers(fixture, "A", "1", "a")
            self.assertGreater(len(secrets), 15, secrets)   # a real haystack
            for user_id, role in GLOBAL_PRINCIPALS:
                for label, target in (
                        ("division shape", {"division_id": da}),
                        ("league-wide shape",
                         {"season_id": sa, "league_id": la}),
                        ("league-wide + division",
                         {"season_id": sa, "league_id": la,
                          "division_id": da})):
                    with self.subTest(role=role.value, shape=label):
                        self._select(api, user_id, role, pb, sb, lb)
                        refused = self._draft(
                            api, user_id, role,
                            constraints=copy.deepcopy(REFUSED_CONSTRAINTS),
                            slot_ids=list(
                                fixture["A"]["seasons"]["1"]["leagues"]["a"]
                                ["slots"]),
                            **target)
                        self.assertIn("error", refused, refused)
                        raw = json.dumps(refused, sort_keys=True)
                        leaked = leaked_identifiers(secrets, raw)
                        self.assertEqual(
                            leaked, [],
                            f"the {label} refusal named foreign hierarchy "
                            f"identifiers {leaked} -- a refused response must "
                            "carry no team, slot, rink, venue, blackout or "
                            "time of the target")
        self._on_every_backend(body)

    # -- clause: role is None ----------------------------------------------
    def test_identity_less_internal_callers_are_completely_ungated(self):
        """``role is None`` is the seed/internal/legacy path and stays
        untouched, exactly as ``setup_target_accessible`` rule 1 requires --
        including with a persisted context that would refuse every one of
        these targets if a role were supplied."""
        def body(store, api):
            fixture = build_two_programs(api)
            _pa, sa, la, da = corner(fixture, "A", "1", "a")
            pb, sb, lb, _db = corner(fixture, "B", "1", "a")
            self._select(api, ADMIN, Role.LEAGUE_ADMIN, pb, sb, lb)
            for label, target in (
                    ("division shape", {"division_id": da}),
                    ("league-wide shape", {"season_id": sa, "league_id": la})):
                with self.subTest(shape=label):
                    proposal = api.draft_season_schedule(**target)
                    self.assertNotIn("error", proposal, proposal)
                    self.assertEqual(len(proposal["draft_games"]), 6)
            # A user_id may even be supplied without a role: an identity with
            # no role is still the legacy call, not a half-gated one.
            self.assertNotIn(
                "error",
                api.draft_season_schedule(division_id=da, user_id=ADMIN))
            # And the nested generation `create_schedule_scenario` performs is
            # the same ungated path -- it has already authorized the edge
            # itself, and a second resolve inside its open transaction would
            # be a different contract.
            self._select(api, ADMIN, Role.LEAGUE_ADMIN, *corner(
                fixture, "A", "1", "a")[:3])
            self.assertNotIn("error", api.create_schedule_scenario(
                "Nested", division_id=da, actor_id=ADMIN, user_id=ADMIN,
                role=Role.LEAGUE_ADMIN, scope={}))
        self._on_every_backend(body)

    def test_a_nonexistent_target_is_unchanged_for_an_identity_less_caller(self):
        """The ungated path keeps its own precise wording, which is a
        different wording from the gated refusal. Stated explicitly so the
        two contracts cannot be conflated by a later change."""
        def body(store, api):
            build_two_programs(api)
            legacy = api.draft_season_schedule(division_id=NO_SUCH_DIVISION)
            self.assertEqual(legacy["error"]["message"], "Division not found.")
            self.assertNotIn("details", legacy["error"])
        self._on_every_backend(body)

    # -- clause: the in-tuple diagnostics are unchanged --------------------
    def test_inside_the_tuple_the_generator_keeps_its_own_diagnostics(self):
        """The leak was closed by ORDERING the check, not by flattening every
        refusal. A caller standing in its own tuple still gets the precise
        message it always did for a Division that is not in the requested
        LeagueSeason."""
        def body(store, api):
            fixture = build_two_programs(api)
            pa, sa, la, _da = corner(fixture, "A", "1", "a")
            _p, _s, _lb, db = corner(fixture, "A", "1", "b")
            self._select(api, ADMIN, Role.LEAGUE_ADMIN, pa, sa, None)
            refused = self._draft(api, ADMIN, Role.LEAGUE_ADMIN,
                                  season_id=sa, league_id=la, division_id=db)
            self.assertEqual(refused["error"]["message"],
                             f"Division {db} not found.", refused)
            self.assertEqual(refused["error"]["details"]["division_id"], db)
        self._on_every_backend(body)

    # -- clause: commit is bound at the preflight --------------------------
    def test_a_foreign_commit_is_refused_before_the_fingerprint_gate(self):
        """``commit_draft_schedule`` takes the identical caller-supplied
        target and had the identical omission, behind a verb that WRITES.

        The refusal must land BEFORE ``preview_required`` / ``preview_stale``,
        or the fingerprint gate itself becomes the oracle: "your fingerprint
        is stale" confirms the hierarchy exists, while a guessed id would have
        answered not-found.
        """
        def body(store, api):
            fixture = build_two_programs(api)
            _pa, sa, la, da = corner(fixture, "A", "1", "a")
            pb, sb, lb, _db = corner(fixture, "B", "1", "a")
            # A's own operator generates a real, valid fingerprint first.
            self._select(api, ADMIN, Role.LEAGUE_ADMIN, *corner(
                fixture, "A", "1", "a")[:3])
            real = _ok(self._draft(api, ADMIN, Role.LEAGUE_ADMIN,
                                   division_id=da))
            fingerprint = real["draft_fingerprint"]

            for user_id, role in GLOBAL_PRINCIPALS:
                for label, sent in (("no fingerprint", None),
                                    ("A's real fingerprint", fingerprint),
                                    ("a guessed fingerprint", "deadbeef")):
                    with self.subTest(role=role.value, fingerprint=label):
                        self._select(api, user_id, role, pb, sb, lb)
                        denied = api.commit_draft_schedule(
                            division_id=da, draft_fingerprint=sent,
                            actor_id=user_id, user_id=user_id, role=role,
                            scope={})
                        guessed = api.commit_draft_schedule(
                            division_id=NO_SUCH_DIVISION,
                            draft_fingerprint=sent, actor_id=user_id,
                            user_id=user_id, role=role, scope={})
                        self.assertIn("error", denied, denied)
                        self.assertEqual(
                            json.dumps(denied, sort_keys=True),
                            json.dumps(guessed, sort_keys=True),
                            "a foreign COMMIT target answered differently "
                            f"from a nonexistent one ({label}) -- the "
                            "fingerprint gate ran before the tuple gate")
                        self.assertEqual(denied["error"]["details"]["reason"],
                                         "division_missing", denied)
            self.assertEqual(store.all_games(), [],
                             "a foreign commit created Games")
            self.assertFalse(any(row.action == "draft_schedule_committed"
                                 for row in store.all_setup_audit()))
        self._on_every_backend(body)

    def test_the_same_commit_in_the_exact_tuple_still_lands(self):
        """The control for the commit refusals above."""
        def body(store, api):
            fixture = build_two_programs(api)
            pa, sa, la, da = corner(fixture, "A", "1", "a")
            self._select(api, ADMIN, Role.LEAGUE_ADMIN, pa, sa, la)
            preview = _ok(self._draft(api, ADMIN, Role.LEAGUE_ADMIN,
                                      division_id=da))
            committed = _ok(api.commit_draft_schedule(
                division_id=da,
                draft_fingerprint=preview["draft_fingerprint"],
                actor_id=ADMIN, user_id=ADMIN, role=Role.LEAGUE_ADMIN,
                scope={}), "commit")
            self.assertEqual(len(committed["created"]), 6)
            self.assertEqual(len(store.all_games()), 6)
        self._on_every_backend(body)

    def test_a_refused_commit_leaves_no_trace_at_all(self):
        """Games, ice-slot status, the id counter and the audit trail are all
        unchanged after a refused commit.

        A refusal that had already written a row and rolled back only part of
        it would surface here, and nowhere else in this file.
        """
        def body(store, api):
            fixture = build_two_programs(api)
            pa, sa, la, da = corner(fixture, "A", "1", "a")
            pb, sb, lb, _db = corner(fixture, "B", "1", "a")
            self._select(api, ADMIN, Role.LEAGUE_ADMIN, pa, sa, la)
            preview = _ok(self._draft(api, ADMIN, Role.LEAGUE_ADMIN,
                                      division_id=da))

            games_before = list(store.all_games())
            slots_before = {slot.id: slot.status
                            for slot in store.all_ice_slots()}
            audit_before = len(store.all_setup_audit())
            counter_before = _game_counter(store)

            self._select(api, ADMIN, Role.LEAGUE_ADMIN, pb, sb, lb)
            refused = api.commit_draft_schedule(
                division_id=da,
                draft_fingerprint=preview["draft_fingerprint"],
                actor_id=ADMIN, user_id=ADMIN, role=Role.LEAGUE_ADMIN,
                scope={})

            self.assertIn(
                "error", refused,
                "a draft generated in Program A was COMMITTED after the "
                "operator switched to Program B")
            self.assertEqual(refused["error"]["details"]["reason"],
                             "division_missing", refused)
            self.assertEqual(store.all_games(), games_before)
            self.assertEqual({slot.id: slot.status
                              for slot in store.all_ice_slots()},
                             slots_before)
            self.assertEqual(len(store.all_setup_audit()), audit_before)
            self.assertEqual(_game_counter(store), counter_before)

            # ...and switching BACK restores it, so the refusal is the ceiling
            # and not a broken fixture.
            self._select(api, ADMIN, Role.LEAGUE_ADMIN, pa, sa, la)
            committed = _ok(api.commit_draft_schedule(
                division_id=da,
                draft_fingerprint=preview["draft_fingerprint"],
                actor_id=ADMIN, user_id=ADMIN, role=Role.LEAGUE_ADMIN,
                scope={}), "control")
            self.assertEqual(len(committed["created"]), 6)
        self._on_every_backend(body)

    # -- clause: the draft-review surface ----------------------------------
    def _commit_both_corners(self, api):
        """One committed draft batch in A/1/a and one in B/1/a."""
        fixture = build_two_programs(api)
        committed = {}
        for tag in ("A", "B"):
            p, s, lg, d = corner(fixture, tag, "1", "a")
            self._select(api, ADMIN, Role.LEAGUE_ADMIN, p, s, lg)
            preview = _ok(self._draft(api, ADMIN, Role.LEAGUE_ADMIN,
                                      division_id=d))
            result = _ok(api.commit_draft_schedule(
                division_id=d, draft_fingerprint=preview["draft_fingerprint"],
                actor_id=ADMIN, user_id=ADMIN, role=Role.LEAGUE_ADMIN,
                scope={}), f"commit {tag}")
            committed[tag] = [row["game_id"] for row in result["created"]]
        return fixture, committed

    def test_the_draft_review_list_holds_only_the_active_tuple(self):
        def body(store, api):
            fixture, committed = self._commit_both_corners(api)
            secrets = foreign_identifiers(fixture, "A", "1", "a")
            pb, sb, lb, _db = corner(fixture, "B", "1", "a")
            for user_id, role in GLOBAL_PRINCIPALS:
                with self.subTest(role=role.value):
                    self._select(api, user_id, role, pb, sb, lb)
                    listed = _ok(api.list_draft_games(user_id, role, {}))
                    self.assertEqual(
                        sorted(row["game_id"] for row in
                               listed["draft_games"]),
                        sorted(committed["B"]),
                        f"a {role.value} active in Program B saw Program A's "
                        "draft games on the review screen")
                    self.assertEqual(listed["summary"]["draft_count"], 6)
                    raw = json.dumps(listed, sort_keys=True)
                    leaked = leaked_identifiers(secrets, raw)
                    self.assertEqual(
                        leaked, [],
                        f"the review list named foreign identifiers {leaked}")
        self._on_every_backend(body)

    def test_the_review_list_never_builds_a_row_for_a_foreign_draft(self):
        """"Filtered on the stored rows, before any payload is built" needs
        its own assertion, because a filter applied afterwards returns the
        identical bytes. What differs is that the wrong one has already
        resolved both team names, the Division name, the Rink name and the
        roster status of a foreign Game into a response object -- one
        ``return`` away from being the payload.
        """
        def body(store, api):
            fixture, committed = self._commit_both_corners(api)
            pb, sb, lb, _db = corner(fixture, "B", "1", "a")
            self._select(api, ADMIN, Role.LEAGUE_ADMIN, pb, sb, lb)

            built = []
            plain = api._draft_review_row

            def recording(game, *args, **kwargs):
                built.append(game.id)
                return plain(game, *args, **kwargs)

            api._draft_review_row = recording
            try:
                listed = _ok(api.list_draft_games(
                    ADMIN, Role.LEAGUE_ADMIN, {}))
            finally:
                del api._draft_review_row

            self.assertEqual(sorted(built), sorted(committed["B"]),
                             "a review row was assembled for a draft Game "
                             "outside the active tuple -- the list must be "
                             "filtered on stored rows BEFORE any payload is "
                             "built")
            self.assertEqual(len(listed["draft_games"]), 6)
            for game_id in committed["A"]:
                self.assertNotIn(game_id, built)
        self._on_every_backend(body)

    def test_publish_and_discard_cannot_reach_a_foreign_draft(self):
        """``game_ids`` arrives in the request body and ``all`` meant the whole
        installation. A foreign id must match nothing -- arriving at exactly
        the count an id that was never minted already produced, so no separate
        refusal exists for an oracle to read."""
        def body(store, api):
            fixture, committed = self._commit_both_corners(api)
            pb, sb, lb, _db = corner(fixture, "B", "1", "a")
            for verb, call in (
                    ("publish", api.publish_draft_games),
                    ("discard", api.discard_draft_games)):
                for user_id, role in GLOBAL_PRINCIPALS:
                    with self.subTest(verb=verb, role=role.value):
                        self._select(api, user_id, role, pb, sb, lb)
                        by_id = _ok(call(
                            game_ids=list(committed["A"]), actor_id=user_id,
                            user_id=user_id, role=role, scope={}), verb)
                        guessed = _ok(call(
                            game_ids=["game_no_such_row"], actor_id=user_id,
                            user_id=user_id, role=role, scope={}), verb)
                        self.assertEqual(
                            json.dumps(by_id, sort_keys=True),
                            json.dumps(guessed, sort_keys=True),
                            f"a foreign {verb} target answered differently "
                            "from a nonexistent one")
                        self.assertEqual(
                            {row.id for row in store.all_games()
                             if row.is_draft and not row.published},
                            set(committed["A"]) | set(committed["B"]),
                            f"{verb} reached Program A's draft games")
            # `all: true` means all of MINE.
            self._select(api, ADMIN, Role.LEAGUE_ADMIN, pb, sb, lb)
            _ok(api.discard_draft_games(
                all_drafts=True, actor_id=ADMIN, user_id=ADMIN,
                role=Role.LEAGUE_ADMIN, scope={}), "discard all")
            self.assertEqual(
                sorted(row.id for row in store.all_games()),
                sorted(committed["A"]),
                "\"all\" discarded Program A's drafts too")
        self._on_every_backend(body)

    def test_the_review_surface_still_works_inside_its_own_tuple(self):
        """The control for the three review negatives above."""
        def body(store, api):
            fixture, committed = self._commit_both_corners(api)
            pa, sa, la, _da = corner(fixture, "A", "1", "a")
            self._select(api, ADMIN, Role.LEAGUE_ADMIN, pa, sa, la)
            listed = _ok(api.list_draft_games(ADMIN, Role.LEAGUE_ADMIN, {}))
            self.assertEqual(
                sorted(row["game_id"] for row in listed["draft_games"]),
                sorted(committed["A"]))
            self.assertEqual(listed["summary"]["published_count"], 0)
            published = _ok(api.publish_draft_games(
                game_ids=list(committed["A"]), actor_id=ADMIN, user_id=ADMIN,
                role=Role.LEAGUE_ADMIN, scope={}))
            self.assertEqual(published["published"], 6)
            after = _ok(api.list_draft_games(ADMIN, Role.LEAGUE_ADMIN, {}))
            self.assertEqual(len(after["draft_games"]), 0)
            # The published census is scoped too: an installation-wide count
            # reports on the OTHER Program's activity every time they publish.
            self.assertEqual(after["summary"]["published_count"], 6)
            _ok(api.publish_draft_games(
                game_ids=list(committed["B"]), actor_id=ADMIN,
                user_id=ADMIN, role=None), "publish B ungated")
            self.assertEqual(
                _ok(api.list_draft_games(ADMIN, Role.LEAGUE_ADMIN, {}))
                ["summary"]["published_count"], 6,
                "the published count included another Program's games")
            self.assertEqual(
                _ok(api.list_draft_games())["summary"]["published_count"], 12)
        self._on_every_backend(body)

    # -- the FULL Game-parent graph, one constraint at a time --------------
    def _mixed_parent_cases(self, fixture, store):
        """One case per INDEPENDENT parent constraint a draft Game carries.

        Each entry is ``(label, selected_tuple, mutate)``: the tuple the
        caller stands in, and a callable that corrupts EXACTLY ONE parent of a
        Game already committed inside that tuple. Every other parent is left
        naming the active tuple, so a predicate that authorized from a SUBSET
        of the parents would pass the case -- which is precisely the defect
        this matrix exists to catch (`season_id` + `league_id` alone said yes
        while `division_id`, `league_season_id` or a Team pointed elsewhere).
        """
        pa, sa, la, _da = corner(fixture, "A", "1", "a")
        ab = fixture["A"]["seasons"]["1"]["leagues"]["b"]
        a2a = fixture["A"]["seasons"]["2"]["leagues"]["a"]
        b1a = fixture["B"]["seasons"]["1"]["leagues"]["a"]
        pb, sb, lb, db = corner(fixture, "B", "1", "a")
        in_tuple = (pa, sa, la)
        # "No League" selected: the League comparison is legitimately skipped,
        # so ONLY the Season end of a binding can refuse. Without this the
        # Season end is untestable through `league_season_id` -- a sibling
        # League's binding differs on BOTH ends at once, and the blocker names
        # exactly that trap.
        no_league = (pa, sa, None)

        def ls_of(league_id, season_id):
            return store.league_season_for(league_id, season_id).id

        def set_attr(name, value):
            def mutate(game):
                setattr(game, name, value)
            return mutate

        return [
            ("foreign division_id (Program B)", in_tuple,
             set_attr("division_id", db)),
            ("sibling-League division_id (same Program, same Season)",
             in_tuple, set_attr("division_id", ab["division"])),
            ("other-Season division_id (same Program)", in_tuple,
             set_attr("division_id", a2a["division"])),
            ("foreign league_season_id (Program B)", in_tuple,
             set_attr("league_season_id", ls_of(lb, sb))),
            ("league_season_id whose LEAGUE end is a sibling League",
             in_tuple, set_attr("league_season_id",
                                ls_of(ab["league"], sa))),
            ("league_season_id whose SEASON end is another Season, "
             "judged with NO League selected", no_league,
             set_attr("league_season_id",
                      ls_of(a2a["league"],
                            fixture["A"]["seasons"]["2"]["season"]))),
            ("foreign season_id (Program B)", in_tuple,
             set_attr("season_id", sb)),
            ("foreign league_id (Program B)", in_tuple,
             set_attr("league_id", lb)),
            ("foreign HOME team (Program B)", in_tuple,
             set_attr("home_team_id", b1a["teams"][0])),
            ("foreign AWAY team (Program B)", in_tuple,
             set_attr("away_team_id", b1a["teams"][1])),
            ("sibling-League HOME team (same Program)", in_tuple,
             set_attr("home_team_id", ab["teams"][0])),
            ("dangling season_id", in_tuple,
             set_attr("season_id", NO_SUCH_SEASON)),
            ("dangling league_id", in_tuple,
             set_attr("league_id", NO_SUCH_LEAGUE)),
            ("dangling division_id", in_tuple,
             set_attr("division_id", NO_SUCH_DIVISION)),
            ("dangling league_season_id", in_tuple,
             set_attr("league_season_id", "leagueseason_no_such_row")),
            ("dangling home_team_id", in_tuple,
             set_attr("home_team_id", "team_no_such_row")),
        ]

    def test_every_game_parent_constrains_the_review_surface(self):
        """A Game's WHOLE parent graph decides, never a subset of it.

        `_game_parent_constraints` (#372) resolves `season_id`, `league_id`,
        BOTH ends of `league_season_id`, and `division_id` through its own
        binding, INDEPENDENTLY, and fails closed when any non-null one is
        unresolvable or when two of them disagree. This surface adds the two
        participating Teams on the same terms, because the review row
        serializes both team NAMES.

        Each case below corrupts exactly one parent of a Game the caller could
        otherwise see, so a predicate reading only `season_id` + `league_id`
        -- the reduced edge the owner rejected -- passes every one of them.
        """
        def body(store, api):
            # A FRESH hierarchy per case. Sharing one across the matrix
            # coupled the cases through committed Games and allocated ice, so
            # a later case could fail for a reason that had nothing to do with
            # the parent it isolates -- and the blocker asks for fixtures that
            # isolate each constraint independently, which a shared one is not.
            case_count = len(self._mixed_parent_cases(
                build_two_programs(api), store))
            for index in range(case_count):
                fixture = build_two_programs(api)
                label, selected, mutate = self._mixed_parent_cases(
                    fixture, store)[index]
                with self.subTest(parent=label):
                    _p, _s, _l, da = corner(fixture, "A", "1", "a")
                    self._select(api, ADMIN, Role.LEAGUE_ADMIN, *selected)
                    preview = _ok(self._draft(api, ADMIN, Role.LEAGUE_ADMIN,
                                              division_id=da), label)
                    result = _ok(api.commit_draft_schedule(
                        division_id=da,
                        draft_fingerprint=preview["draft_fingerprint"],
                        actor_id=ADMIN, user_id=ADMIN,
                        role=Role.LEAGUE_ADMIN, scope={}), label)
                    game_ids = [row["game_id"] for row in result["created"]]
                    self.assertTrue(game_ids, label)

                    # The CONTROL: before the corruption this caller sees them.
                    listed = _ok(api.list_draft_games(
                        ADMIN, Role.LEAGUE_ADMIN, {}))
                    self.assertIn(
                        game_ids[0],
                        [row["game_id"] for row in listed["draft_games"]],
                        f"{label}: the fixture never saw the Game at all, so "
                        "its exclusion below would prove nothing")

                    target = store.get_game(game_ids[0])
                    mutate(target)
                    store.save_game(target)

                    listed = _ok(api.list_draft_games(
                        ADMIN, Role.LEAGUE_ADMIN, {}))
                    self.assertNotIn(
                        game_ids[0],
                        [row["game_id"] for row in listed["draft_games"]],
                        f"{label}: a draft Game with a parent outside the "
                        "active tuple was served to the review screen -- the "
                        "WHOLE parent graph decides, not season_id + "
                        "league_id")
                    # The other Games of the same batch are untouched, so this
                    # is a per-row decision and not a collapsed list.
                    self.assertIn(
                        game_ids[1],
                        [row["game_id"] for row in listed["draft_games"]],
                        f"{label}: the whole batch vanished")

                    # ...and neither write verb can reach it, with the count
                    # byte-identical to a target that never existed.
                    for verb, call in (("publish", api.publish_draft_games),
                                       ("discard", api.discard_draft_games)):
                        denied = _ok(call(
                            game_ids=[game_ids[0]], actor_id=ADMIN,
                            user_id=ADMIN, role=Role.LEAGUE_ADMIN, scope={}),
                            label)
                        guessed = _ok(call(
                            game_ids=["game_no_such_row"], actor_id=ADMIN,
                            user_id=ADMIN, role=Role.LEAGUE_ADMIN, scope={}),
                            label)
                        self.assertEqual(
                            json.dumps(denied, sort_keys=True),
                            json.dumps(guessed, sort_keys=True),
                            f"{label}: {verb} answered differently for a "
                            "mixed-parent Game than for a nonexistent one")
                    self.assertIsNotNone(
                        store.get_game(game_ids[0]),
                        f"{label}: the mixed-parent Game was DISCARDED")
        self._on_every_backend(body)

    def test_a_mixed_parent_game_names_nothing_on_the_review_screen(self):
        """The exclusion happens before any review row is built, and no
        identifier of the excluded Game reaches the payload."""
        def body(store, api):
            fixture = build_two_programs(api)
            pa, sa, la, da = corner(fixture, "A", "1", "a")
            b1a = fixture["B"]["seasons"]["1"]["leagues"]["a"]
            self._select(api, ADMIN, Role.LEAGUE_ADMIN, pa, sa, la)
            preview = _ok(self._draft(api, ADMIN, Role.LEAGUE_ADMIN,
                                      division_id=da))
            result = _ok(api.commit_draft_schedule(
                division_id=da,
                draft_fingerprint=preview["draft_fingerprint"],
                actor_id=ADMIN, user_id=ADMIN, role=Role.LEAGUE_ADMIN,
                scope={}))
            victim = store.get_game(result["created"][0]["game_id"])
            victim.home_team_id = b1a["teams"][0]
            store.save_game(victim)

            built = []
            plain = api._draft_review_row

            def recording(game, *args, **kwargs):
                built.append(game.id)
                return plain(game, *args, **kwargs)

            api._draft_review_row = recording
            try:
                listed = _ok(api.list_draft_games(
                    ADMIN, Role.LEAGUE_ADMIN, {}))
            finally:
                del api._draft_review_row

            self.assertNotIn(
                victim.id, built,
                "a review row was assembled for a mixed-parent Game -- the "
                "exclusion must happen BEFORE any payload is built")
            raw = json.dumps(listed, sort_keys=True)
            leaked = leaked_identifiers(
                foreign_identifiers(fixture, "B", "1", "a") | {victim.id},
                raw)
            self.assertEqual(
                leaked, [],
                f"the review list named the excluded Game's identifiers "
                f"{leaked}")
        self._on_every_backend(body)

    def test_the_review_surface_is_ungated_for_identity_less_callers(self):
        """``role is None`` sees the whole installation exactly as before."""
        def body(store, api):
            _fixture, committed = self._commit_both_corners(api)
            listed = _ok(api.list_draft_games())
            self.assertEqual(
                sorted(row["game_id"] for row in listed["draft_games"]),
                sorted(committed["A"] + committed["B"]))
            self.assertEqual(
                _ok(api.discard_draft_games(all_drafts=True))["discarded"], 12)
        self._on_every_backend(body)


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "needs PostgreSQL: the race is a DATABASE lock")
class DraftActiveTupleRaceTest(unittest.TestCase):
    """The check/use gap, proven by BLOCKING a real second connection.

    A lock can only be observed through contention, so a same-process test
    cannot tell "authorized inside the write transaction under the
    ActiveContext lock" from "authorized just before it" — both refuse the
    sequential case identically. This class opens a SECOND PostgreSQL
    connection, has it perform the context switch that would invalidate the
    authorization, and asserts it is still BLOCKED at the moment the write
    verb is about to mutate.

    Blocked, then completing once the verb's transaction ends, is the whole
    proof: it shows the wait was a lock and not a broken thread, and it shows
    the two operations were serialized rather than interleaved. Modelled on
    `test_schedule_scenarios.test_the_snapshot_venue_rows_are_locked_against_a_racing_writer`,
    which established this shape here.
    """

    maxDiff = None

    def setUp(self):
        self.store = fresh_sql_store(os.environ["TEST_DATABASE_URL"])
        self.api = ApiService(self.store)
        self.fixture = build_two_programs(self.api)
        self.pa, self.sa, self.la, self.da = corner(
            self.fixture, "A", "1", "a")
        self.pb, self.sb, self.lb, _db = corner(self.fixture, "B", "1", "a")
        self._select(self.pa, self.sa, self.la)

    def tearDown(self):
        self.store.close()

    def _select(self, program_id, season_id, league_id):
        _ok(self.api.set_active_context(
            ADMIN, Role.LEAGUE_ADMIN, {}, program_id, season_id, league_id),
            "set_active_context")

    def _commit_a_batch(self):
        preview = _ok(self.api.draft_season_schedule(
            division_id=self.da, user_id=ADMIN, role=Role.LEAGUE_ADMIN,
            scope={}))
        result = _ok(self.api.commit_draft_schedule(
            division_id=self.da,
            draft_fingerprint=preview["draft_fingerprint"], actor_id=ADMIN,
            user_id=ADMIN, role=Role.LEAGUE_ADMIN, scope={}), "commit")
        return [row["game_id"] for row in result["created"]]

    def _racing_context_switch(self):
        """A second CONNECTION switching this same operator to Program B.

        Returns ``(start, race)``: ``start()`` launches it and asserts it is
        still blocked two seconds later; ``race`` carries the thread and any
        error for the caller to join and re-raise.
        """
        race = {"worker": None, "error": None, "done": False}

        def switch_on_second_connection():
            other = SqlStore(os.environ["TEST_DATABASE_URL"])
            try:
                other_api = ApiService(other)
                other_api.set_active_context(
                    ADMIN, Role.LEAGUE_ADMIN, {}, self.pb, self.sb, self.lb)
                race["done"] = True
            except BaseException as error:   # surfaced on the test thread
                race["error"] = error
            finally:
                other.close()

        def start(where):
            worker = threading.Thread(target=switch_on_second_connection,
                                      daemon=True)
            race["worker"] = worker
            worker.start()
            worker.join(timeout=2)
            self.assertTrue(
                worker.is_alive(),
                f"the operator's context switch COMPLETED at {where} while "
                "the write verb held its transaction -- the ActiveContext row "
                "is not locked, so the verb can mutate under a tuple that no "
                "longer authorizes it")
        return start, race

    def _assert_the_writer_was_only_delayed(self, race):
        race["worker"].join(timeout=10)
        self.assertFalse(race["worker"].is_alive(),
                         "the blocked context switch never completed")
        if race["error"] is not None:
            raise race["error"]
        self.assertTrue(race["done"])
        saved = self.store.get_active_context(ADMIN)
        self.assertEqual(saved.program_id, self.pb,
                         "the racing switch did not actually land")

    def test_publish_holds_the_context_lock_across_its_writes(self):
        game_ids = self._commit_a_batch()
        start, race = self._racing_context_switch()
        original = self.api.setup.publish_game
        seen = {"calls": 0}

        def publish_with_a_racing_switch(game_id, published, actor_id=None):
            seen["calls"] += 1
            if seen["calls"] == 1:
                # Strictly AFTER the ActiveContext + Game locks and the
                # authorization, and strictly BEFORE this batch is finished.
                start("the first publish_game")
            return original(game_id, published, actor_id)

        self.api.setup.publish_game = publish_with_a_racing_switch
        try:
            published = _ok(self.api.publish_draft_games(
                game_ids=game_ids, actor_id=ADMIN, user_id=ADMIN,
                role=Role.LEAGUE_ADMIN, scope={}))
        finally:
            self.api.setup.publish_game = original

        self.assertEqual(published["published"], 6)
        self._assert_the_writer_was_only_delayed(race)

    def test_discard_holds_the_context_lock_across_its_writes(self):
        game_ids = self._commit_a_batch()
        start, race = self._racing_context_switch()
        original = self.store.delete_game
        seen = {"calls": 0}

        def delete_with_a_racing_switch(game_id):
            seen["calls"] += 1
            if seen["calls"] == 1:
                start("the first delete_game")
            return original(game_id)

        self.store.delete_game = delete_with_a_racing_switch
        try:
            discarded = _ok(self.api.discard_draft_games(
                game_ids=game_ids, actor_id=ADMIN, user_id=ADMIN,
                role=Role.LEAGUE_ADMIN, scope={}))
        finally:
            self.store.delete_game = original

        self.assertEqual(discarded["discarded"], 6)
        self._assert_the_writer_was_only_delayed(race)

    def test_discard_all_holds_the_context_lock_across_its_writes(self):
        """``all_drafts`` takes the same path and must take the same lock —
        it is the form that means "everything", so an unlocked one is the
        worst case rather than a lesser one."""
        self._commit_a_batch()
        start, race = self._racing_context_switch()
        original = self.store.delete_game
        seen = {"calls": 0}

        def delete_with_a_racing_switch(game_id):
            seen["calls"] += 1
            if seen["calls"] == 1:
                start("the first delete_game of an all_drafts discard")
            return original(game_id)

        self.store.delete_game = delete_with_a_racing_switch
        try:
            discarded = _ok(self.api.discard_draft_games(
                all_drafts=True, actor_id=ADMIN, user_id=ADMIN,
                role=Role.LEAGUE_ADMIN, scope={}))
        finally:
            self.store.delete_game = original

        self.assertEqual(discarded["discarded"], 6)
        self._assert_the_writer_was_only_delayed(race)

    def test_commit_holds_the_context_lock_across_its_game_writes(self):
        """The commit verb, at its FIRST Game INSERT — after the
        re-authorization and the whole lock plan, before any row lands."""
        preview = _ok(self.api.draft_season_schedule(
            division_id=self.da, user_id=ADMIN, role=Role.LEAGUE_ADMIN,
            scope={}))
        start, race = self._racing_context_switch()
        original = self.store.add_game
        seen = {"calls": 0}

        def add_with_a_racing_switch(game):
            seen["calls"] += 1
            if seen["calls"] == 1:
                start("the first Game INSERT")
            return original(game)

        self.store.add_game = add_with_a_racing_switch
        try:
            committed = _ok(self.api.commit_draft_schedule(
                division_id=self.da,
                draft_fingerprint=preview["draft_fingerprint"],
                actor_id=ADMIN, user_id=ADMIN, role=Role.LEAGUE_ADMIN,
                scope={}), "commit")
        finally:
            self.store.add_game = original

        self.assertEqual(len(committed["created"]), 6)
        self._assert_the_writer_was_only_delayed(race)

    def test_the_switch_really_can_complete_when_nothing_holds_the_lock(self):
        """The anti-vacuity control for all four races above.

        Every one of them asserts a thread is STILL ALIVE after two seconds.
        That assertion passes just as well if the second connection can never
        complete at all — a bad URL, an exhausted pool, a thread that threw
        before reaching the write. So: with no write verb running, the exact
        same racing switch must finish quickly and land.
        """
        start_time = time.monotonic()
        race = {"error": None, "done": False}

        def switch_on_second_connection():
            other = SqlStore(os.environ["TEST_DATABASE_URL"])
            try:
                ApiService(other).set_active_context(
                    ADMIN, Role.LEAGUE_ADMIN, {}, self.pb, self.sb, self.lb)
                race["done"] = True
            except BaseException as error:
                race["error"] = error
            finally:
                other.close()

        worker = threading.Thread(target=switch_on_second_connection,
                                  daemon=True)
        worker.start()
        worker.join(timeout=10)
        if race["error"] is not None:
            raise race["error"]
        self.assertTrue(race["done"], "the racing switch cannot complete even "
                                      "with nothing holding the lock")
        self.assertLess(time.monotonic() - start_time, 2.0,
                        "the racing switch is slow on its own, so 'still "
                        "alive after 2s' proves nothing about locking")
        self.assertEqual(self.store.get_active_context(ADMIN).program_id,
                         self.pb)


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "needs PostgreSQL: a deadlock is a DATABASE outcome")
class DraftBatchVersusSingleGameLockOrderTest(unittest.TestCase):
    """The batch verbs and the single-Game verbs, forced to meet.

    `_locked_draft_targets` used to lock Game rows and leave
    `_guard_active_seasons` to its callers AFTERWARDS, which is the reverse of
    what every single-Game path does: `SetupService.publish_game` and
    `delete_game` lock the Season through `_guard_game_season` and only then
    touch the Game. Batch Game->Season against single Season->Game is an ABBA
    cycle, and PostgreSQL resolves those by killing one side — a 500 on a
    perfectly legitimate pair of requests.

    One canonical order (**Season before Game**) removes the cycle rather than
    making it rarer, and these tests force the exact interleaving that would
    expose it. The barrier fires at the batch's SECOND Game lock, so under the
    old order the batch provably HOLDS a Game while it still needs the Season,
    which is the half of the cycle that has to exist for a deadlock at all.
    """

    maxDiff = None

    def setUp(self):
        self.store = fresh_sql_store(os.environ["TEST_DATABASE_URL"])
        self.api = ApiService(self.store)
        self.fixture = build_two_programs(self.api)
        self.pa, self.sa, self.la, self.da = corner(
            self.fixture, "A", "1", "a")
        _ok(self.api.set_active_context(
            ADMIN, Role.LEAGUE_ADMIN, {}, self.pa, self.sa, self.la))
        preview = _ok(self.api.draft_season_schedule(
            division_id=self.da, user_id=ADMIN, role=Role.LEAGUE_ADMIN,
            scope={}))
        self.committed = [row["game_id"] for row in _ok(
            self.api.commit_draft_schedule(
                division_id=self.da,
                draft_fingerprint=preview["draft_fingerprint"],
                actor_id=ADMIN, user_id=ADMIN, role=Role.LEAGUE_ADMIN,
                scope={}), "seed commit")["created"]]
        self.assertEqual(len(self.committed), 6)

    def tearDown(self):
        self.store.close()

    def _single_game_worker(self, single_op):
        """A SECOND connection running a single-Game mutation on one of the
        batch's own targets — the other half of the would-be cycle."""
        race = {"worker": None, "error": None, "done": False}

        def run():
            other = SqlStore(os.environ["TEST_DATABASE_URL"])
            try:
                single_op(ApiService(other))
                race["done"] = True
            except BaseException as error:   # surfaced on the test thread
                race["error"] = error
            finally:
                other.close()

        race["start"] = lambda: race.__setitem__(
            "worker", self._start(run, race))
        return race

    def _start(self, run, race):
        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(timeout=2)
        self.assertTrue(
            worker.is_alive(),
            "the single-Game path COMPLETED while the batch held its "
            "transaction -- it never contended for the batch's Season, so "
            "this barrier is not exercising the lock order at all")
        return worker

    def _run_batch_with_the_single_path_at_the_second_game_lock(
            self, batch_op, single_op):
        """Fire ``single_op`` on another connection once the batch has locked
        its FIRST Game, then let the batch finish. Returns the batch result."""
        race = self._single_game_worker(single_op)
        original = self.store.get_game_for_update
        seen = {"calls": 0}

        def locking_with_a_racing_single_path(game_id):
            seen["calls"] += 1
            if seen["calls"] == 2:
                # One Game is already locked by this transaction. Under the
                # OLD order no Season is held yet, so the single path can take
                # the Season and then block on that Game — and the batch's own
                # later Season guard closes the cycle.
                race["start"]()
            return original(game_id)

        self.store.get_game_for_update = locking_with_a_racing_single_path
        try:
            result = batch_op()
        finally:
            self.store.get_game_for_update = original

        self.assertGreaterEqual(
            seen["calls"], 2,
            "the batch locked fewer than two Games, so the barrier never "
            "fired and this test proves nothing")
        race["worker"].join(timeout=20)
        self.assertFalse(race["worker"].is_alive(),
                         "the single-Game path never completed")
        return result, race

    @staticmethod
    def _assert_no_deadlock(error):
        if error is None:
            return
        text = f"{type(error).__name__}: {error}"
        assert "deadlock" not in text.lower(), (
            f"the batch and single-Game paths DEADLOCKED: {text}")

    def test_batch_publish_versus_single_game_publish(self):
        target = self.committed[0]

        def batch():
            return self.api.publish_draft_games(
                game_ids=list(self.committed), actor_id=ADMIN,
                user_id=ADMIN, role=Role.LEAGUE_ADMIN, scope={})

        def single(other_api):
            other_api.setup.publish_game(target, True, "racer")

        result, race = self._run_batch_with_the_single_path_at_the_second_game_lock(
            batch, single)
        self._assert_no_deadlock(race["error"])
        self.assertNotIn("error", result, result)
        self.assertEqual(result["published"], 6, result)
        if race["error"] is not None:
            raise race["error"]
        # A valid serial result: the batch published all six, and the single
        # publish of one of them is a no-op re-publish rather than a failure.
        self.assertEqual(
            {g.id for g in self.store.all_games() if g.published},
            set(self.committed))
        self.assertEqual(
            [g.is_draft for g in self.store.all_games()], [False] * 6)

    def test_batch_discard_versus_single_game_delete(self):
        target = self.committed[0]

        def batch():
            return self.api.discard_draft_games(
                game_ids=list(self.committed), actor_id=ADMIN,
                user_id=ADMIN, role=Role.LEAGUE_ADMIN, scope={})

        def single(other_api):
            other_api.setup.delete_game(target, "racer")

        result, race = self._run_batch_with_the_single_path_at_the_second_game_lock(
            batch, single)
        self._assert_no_deadlock(race["error"])
        self.assertNotIn("error", result, result)
        self.assertEqual(result["discarded"], 6, result)
        # The batch ordered first and deleted everything, so the single delete
        # finds nothing — a valid serial outcome, and NOT a deadlock.
        self.assertEqual(self.store.all_games(), [])
        if race["error"] is not None:
            self.assertIn("not found", str(race["error"]).lower(),
                          f"unexpected single-delete failure: {race['error']}")

    def test_batch_publish_versus_single_delete_of_a_target(self):
        """The opposite pairing: batch PUBLISH against single DELETE. The two
        verbs touch the same rows through different code paths, so both
        directions of the pair are exercised rather than one."""
        target = self.committed[0]

        def batch():
            return self.api.publish_draft_games(
                game_ids=list(self.committed), actor_id=ADMIN,
                user_id=ADMIN, role=Role.LEAGUE_ADMIN, scope={})

        def single(other_api):
            other_api.setup.delete_game(target, "racer")

        result, race = self._run_batch_with_the_single_path_at_the_second_game_lock(
            batch, single)
        self._assert_no_deadlock(race["error"])
        self.assertNotIn("error", result, result)
        # Serial either way: the batch published all six first, so the delete
        # then refuses (a published Game is not a deletable draft). What must
        # never happen is a deadlock or a half-published batch.
        self.assertEqual(result["published"], 6, result)
        self.assertEqual(
            {g.id for g in self.store.all_games() if g.published},
            set(self.committed))

    def test_a_target_changing_season_between_plan_and_lock_is_retried(self):
        """The bounded re-read/retry the reordering needs.

        Locking Seasons before Games means the Season set is chosen from an
        UNLOCKED read, so a Game can move to a Season this transaction never
        locked in the window between. The batch must notice under the lock and
        restart against a fresh plan rather than write onto ice it does not
        hold — and the abandoned attempt must leave nothing behind.
        """
        moved = self.committed[-1]
        other_season = self.fixture["A"]["seasons"]["2"]["season"]
        audit_before = len(self.store.all_setup_audit())
        original = self.store.get_game_for_update
        seen = {"calls": 0, "moved": False}

        def move_one_target_mid_plan(game_id):
            seen["calls"] += 1
            if seen["calls"] == 1 and not seen["moved"]:
                seen["moved"] = True
                # A second connection moves a not-yet-locked target into a
                # Season this transaction never planned for, and COMMITS.
                other = SqlStore(os.environ["TEST_DATABASE_URL"])
                try:
                    row = other.get_game(moved)
                    row.season_id = other_season
                    other.save_game(row)
                finally:
                    other.close()
            return original(game_id)

        self.store.get_game_for_update = move_one_target_mid_plan
        try:
            result = self.api.discard_draft_games(
                game_ids=list(self.committed), actor_id=ADMIN,
                user_id=ADMIN, role=Role.LEAGUE_ADMIN, scope={})
        finally:
            self.store.get_game_for_update = original

        self.assertTrue(seen["moved"], "the conflict was never injected")
        self.assertNotIn("error", result, result)
        # The retry re-planned: the moved Game is no longer in the tuple, so
        # the five that remain are discarded and it is left alone.
        self.assertEqual(result["discarded"], 5, result)
        survivors = {g.id for g in self.store.all_games()}
        self.assertEqual(survivors, {moved},
                         "the retry did not act on exactly the re-planned set")
        # The abandoned first attempt rolled back completely: its audit rows
        # are not there on top of the successful attempt's five.
        self.assertEqual(
            len(self.store.all_setup_audit()) - audit_before, 5,
            "the rolled-back attempt left audit rows behind")


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "needs PostgreSQL: the race is a DATABASE lock")
class DraftFirstSelectionRaceTest(unittest.TestCase):
    """The caller's very FIRST context selection, with NO ActiveContext row.

    `DraftActiveTupleRaceTest` above cannot reach this branch, and that is
    exactly the hole the owner's review found: every one of its tests selects
    a context in ``setUp``, so the row always exists and the write verb's
    ``SELECT ... FOR UPDATE`` always has something to lock. **An absent-row
    read is not a lock.** A brand new operator resolves its tuple from
    ``_fallback``, has nothing to lock, and — with only a read->write
    anti-dependency standing between them, which SERIALIZABLE is not obliged
    to abort — could have its first saved selection B and a batch of Program A
    Game writes both commit.

    So every test here asserts, FIRST, that no row exists. If a future change
    to the fixture quietly creates one, these tests fail loudly instead of
    passing while testing nothing.

    The valid serial outcomes are exactly two, and both are asserted:

      * the verb ordered FIRST — it acted on the fallback tuple A, and the B
        selection landed afterwards; or
      * the selection ordered FIRST — the verb saw tuple B and wrote nothing,
        because none of the A drafts is in B.

    What must never happen is the third outcome: B persisted AND A's Games
    mutated. A cross-tuple write is checked directly, not inferred.
    """

    maxDiff = None

    def setUp(self):
        self.store = fresh_sql_store(os.environ["TEST_DATABASE_URL"])
        self.api = ApiService(self.store)
        self.fixture = build_two_programs(self.api)
        # `_fallback` picks the first authorized Program by id STRING and,
        # within it, the latest active Season. Which corner that lands on
        # depends on the store's id counters, which PostgreSQL test databases
        # carry across runs -- so the corner is DERIVED from the resolution
        # rather than hard-coded, and then asserted to be a real, schedulable
        # one. Hard-coding it made this class pass or fail on counter state.
        program, season, league = self.api.context.resolve_with_league(
            NEWCOMER, Role.LEAGUE_ADMIN, {})
        self.assertIsNotNone(program, "the newcomer resolved no Program")
        self.assertIsNotNone(season, "the newcomer resolved no Season")
        self.assertIsNone(league, "a newcomer cannot have a saved League")
        self.pa, self.sa, self.la, self.da = self._corner_at(
            program.id, season.id)
        # The switch target must be a genuinely DIFFERENT Program, or the
        # races would prove nothing about crossing the tuple.
        self.pb, self.sb, self.lb, _db = self._other_program_corner(program.id)
        self.assertNotEqual(self.pa, self.pb)
        # Committed as an identity-less internal caller, so the batch exists
        # WITHOUT the newcomer ever having saved a context.
        preview = _ok(self.api.draft_season_schedule(division_id=self.da))
        self.committed = [row["game_id"] for row in _ok(
            self.api.commit_draft_schedule(
                division_id=self.da,
                draft_fingerprint=preview["draft_fingerprint"]),
            "seed commit")["created"]]
        self.assertEqual(len(self.committed), 6)

    def tearDown(self):
        self.store.close()

    def _corner_at(self, program_id, season_id):
        """The fixture corner the newcomer's FALLBACK really resolved to.

        Asserted to exist: if the fallback ever lands somewhere this fixture
        did not build a schedulable corner for, every race below would refuse
        for scope reasons instead of exercising the lock, and that must fail
        loudly here rather than pass quietly there.
        """
        for tag, programs in self.fixture.items():
            if programs["program"] != program_id:
                continue
            for season in programs["seasons"].values():
                if season["season"] != season_id:
                    continue
                league = sorted(season["leagues"])[0]
                return corner(self.fixture, tag,
                              next(k for k, v in programs["seasons"].items()
                                   if v["season"] == season_id), league)
        self.fail(f"the newcomer's fallback ({program_id}, {season_id}) is "
                  "not a corner this fixture built")

    def _other_program_corner(self, program_id):
        for tag, programs in self.fixture.items():
            if programs["program"] == program_id:
                continue
            season_tag = sorted(programs["seasons"])[0]
            league = sorted(programs["seasons"][season_tag]["leagues"])[0]
            return corner(self.fixture, tag, season_tag, league)
        self.fail("the fixture has only one Program")

    def _assert_no_saved_context(self):
        """The anti-vacuity guard: this whole class is about the ABSENT row."""
        self.assertIsNone(
            self.store.get_active_context(NEWCOMER),
            "this test is about the caller's FIRST selection, but an "
            "ActiveContext row already exists -- it would exercise the "
            "already-covered row-lock path and prove nothing about the "
            "absent-row one")

    def _race_first_selection(self, verb_under_test, at):
        """Run ``verb_under_test`` while a second CONNECTION makes the very
        first A->B selection, released at the ``at`` seam.

        Returns the verb's result. Asserts both sides completed and that the
        outcome is one of the two valid serial ones.
        """
        self._assert_no_saved_context()
        race = {"worker": None, "error": None, "done": False}

        def first_selection_on_second_connection():
            other = SqlStore(os.environ["TEST_DATABASE_URL"])
            try:
                ApiService(other).set_active_context(
                    NEWCOMER, Role.LEAGUE_ADMIN, {}, self.pb, self.sb,
                    self.lb)
                race["done"] = True
            except BaseException as error:       # surfaced on the test thread
                race["error"] = error
            finally:
                other.close()

        def start():
            worker = threading.Thread(
                target=first_selection_on_second_connection, daemon=True)
            race["worker"] = worker
            worker.start()
            worker.join(timeout=2)
            self.assertTrue(
                worker.is_alive(),
                f"the caller's FIRST context selection COMPLETED at {at} "
                "while the write verb held its transaction -- with no row to "
                "lock there is nothing serializing them, so the verb can "
                "write under tuple A while tuple B is persisted")

        result = verb_under_test(start)

        race["worker"].join(timeout=15)
        self.assertFalse(race["worker"].is_alive(),
                         "the blocked first selection never completed")
        if race["error"] is not None:
            raise race["error"]
        self.assertTrue(race["done"])
        self.assertEqual(self.store.get_active_context(NEWCOMER).program_id,
                         self.pb, "the racing first selection did not land")
        return result

    def _assert_no_cross_tuple_write(self, before, target_ids):
        """Whatever the ordering, nothing of Program B's may have changed and
        the batch must be all-or-nothing over its own targets."""
        before_games, _before_slots, before_audit = before
        self.assertEqual(
            {g.id for g in self.store.all_games() if g.season_id == self.sb},
            set(),
            "the batch wrote Games into the newly-selected Program B")
        now_games = {g.id: (g.is_draft, g.published)
                     for g in self.store.all_games()}
        # A discard removes rows; a publish flips is_draft. Either way each
        # TARGET either moved or did not, and they must agree with each other.
        moved = {gid for gid in target_ids
                 if now_games.get(gid) != before_games.get(gid)}
        self.assertIn(
            len(moved), (0, len(target_ids)),
            f"only {len(moved)} of {len(target_ids)} targets moved -- a "
            "partially-applied batch")
        # Nothing OUTSIDE the target set may move at all.
        for gid, state in before_games.items():
            if gid in target_ids:
                continue
            self.assertEqual(now_games.get(gid), state,
                             f"game {gid} changed but was never a target")
        if not moved:
            self.assertEqual(len(self.store.all_setup_audit()), before_audit,
                             "a batch that wrote nothing still audited")

    def _snapshot(self):
        return ({g.id: (g.is_draft, g.published)
                 for g in self.store.all_games()},
                {s.id: s.status for s in self.store.all_ice_slots()},
                len(self.store.all_setup_audit()))

    def test_first_selection_races_publish(self):
        before = self._snapshot()
        original = self.api.setup.publish_game
        seen = {"calls": 0}

        def run(start):
            def publish_with_the_first_selection(game_id, published,
                                                 actor_id=None):
                seen["calls"] += 1
                if seen["calls"] == 1:
                    start()
                return original(game_id, published, actor_id)

            self.api.setup.publish_game = publish_with_the_first_selection
            try:
                return _ok(self.api.publish_draft_games(
                    game_ids=list(self.committed), actor_id=NEWCOMER,
                    user_id=NEWCOMER, role=Role.LEAGUE_ADMIN, scope={}))
            finally:
                self.api.setup.publish_game = original

        result = self._race_first_selection(run, "the first publish_game")
        self.assertIn(result["published"], (0, 6), result)
        self._assert_no_cross_tuple_write(before, set(self.committed))

    def test_first_selection_races_discard(self):
        before = self._snapshot()
        original = self.store.delete_game
        seen = {"calls": 0}

        def run(start):
            def delete_with_the_first_selection(game_id):
                seen["calls"] += 1
                if seen["calls"] == 1:
                    start()
                return original(game_id)

            self.store.delete_game = delete_with_the_first_selection
            try:
                return _ok(self.api.discard_draft_games(
                    game_ids=list(self.committed), actor_id=NEWCOMER,
                    user_id=NEWCOMER, role=Role.LEAGUE_ADMIN, scope={}))
            finally:
                self.store.delete_game = original

        result = self._race_first_selection(run, "the first delete_game")
        self.assertIn(result["discarded"], (0, 6), result)
        self._assert_no_cross_tuple_write(before, set(self.committed))

    def test_first_selection_races_discard_all(self):
        before = self._snapshot()
        original = self.store.delete_game
        seen = {"calls": 0}

        def run(start):
            def delete_with_the_first_selection(game_id):
                seen["calls"] += 1
                if seen["calls"] == 1:
                    start()
                return original(game_id)

            self.store.delete_game = delete_with_the_first_selection
            try:
                return _ok(self.api.discard_draft_games(
                    all_drafts=True, actor_id=NEWCOMER, user_id=NEWCOMER,
                    role=Role.LEAGUE_ADMIN, scope={}))
            finally:
                self.store.delete_game = original

        result = self._race_first_selection(
            run, "the first delete_game of an all_drafts discard")
        self.assertIn(result["discarded"], (0, 6), result)
        self._assert_no_cross_tuple_write(before, set(self.committed))

    def test_first_selection_races_commit(self):
        self._assert_no_saved_context()
        before = self._snapshot()
        # A second Division in the SAME corner, so there is something left to
        # commit after setUp's batch.
        _ok(self.api.discard_draft_games(all_drafts=True), "clear")
        before = self._snapshot()
        preview = _ok(self.api.draft_season_schedule(
            division_id=self.da, user_id=NEWCOMER, role=Role.LEAGUE_ADMIN,
            scope={}))
        original = self.store.add_game
        seen = {"calls": 0}

        def run(start):
            def add_with_the_first_selection(game):
                seen["calls"] += 1
                if seen["calls"] == 1:
                    start()
                return original(game)

            self.store.add_game = add_with_the_first_selection
            try:
                return self.api.commit_draft_schedule(
                    division_id=self.da,
                    draft_fingerprint=preview["draft_fingerprint"],
                    actor_id=NEWCOMER, user_id=NEWCOMER,
                    role=Role.LEAGUE_ADMIN, scope={})
            finally:
                self.store.add_game = original

        result = self._race_first_selection(run, "the first Game INSERT")
        # The commit CREATES rows, so the all-or-nothing check above (which
        # tracks pre-existing targets) does not apply; the two valid serial
        # outcomes are asserted directly instead.
        in_a = {g.id for g in self.store.all_games() if g.season_id == self.sa}
        in_b = {g.id for g in self.store.all_games() if g.season_id == self.sb}
        self.assertEqual(in_b, set(),
                         "the commit wrote Games into the newly-selected "
                         "Program B")
        if "error" in result:
            # The selection ordered FIRST: refused, and nothing written.
            self.assertEqual(result["error"]["details"]["reason"],
                             "division_missing", result)
            self.assertEqual(in_a, set(),
                             "a refused commit still created Games")
            self.assertEqual(len(self.store.all_setup_audit()), before[2],
                             "a refused commit still audited")
        else:
            # The commit ordered FIRST, on the fallback tuple A it really did
            # hold at the time.
            self.assertEqual(len(result["created"]), 6, result)
            self.assertEqual(len(in_a), 6)

    def test_the_mutex_is_held_before_the_snapshot_is_established(self):
        """The mutex must be taken before ANY read that fixes the snapshot.

        The four races above all start the racing selection at the batch's
        first Game MUTATION, by which point the batch already owns the mutex —
        so they cannot see this window at all. It opens strictly earlier: the
        batch reads its candidate Games, which under SERIALIZABLE establishes
        the transaction's snapshot, and only THEN takes the mutex. A first
        selection landing in between acquires the (still free) advisory lock,
        inserts tuple B and commits — and when the batch finally takes the
        mutex its snapshot predates that INSERT, so it still resolves fallback
        tuple A and writes A after B is persisted. The original absent-row
        race, shifted one step earlier.

        The barrier therefore fires at the batch's FIRST candidate read, which
        is the earliest point inside the transaction that has read anything.
        The racing selection must be BLOCKED there: if it can complete, the
        batch does not yet hold the mutex, and its snapshot is already fixed.
        """
        self._assert_no_saved_context()
        before = self._snapshot()
        race = {"worker": None, "error": None, "done": False}

        def first_selection_on_second_connection():
            other = SqlStore(os.environ["TEST_DATABASE_URL"])
            try:
                ApiService(other).set_active_context(
                    NEWCOMER, Role.LEAGUE_ADMIN, {}, self.pb, self.sb,
                    self.lb)
                race["done"] = True
            except BaseException as error:
                race["error"] = error
            finally:
                other.close()

        original = self.store.all_games
        seen = {"calls": 0}

        def read_with_a_racing_first_selection():
            seen["calls"] += 1
            if seen["calls"] == 1:
                worker = threading.Thread(
                    target=first_selection_on_second_connection, daemon=True)
                race["worker"] = worker
                worker.start()
                worker.join(timeout=2)
                self.assertTrue(
                    worker.is_alive(),
                    "the caller's FIRST context selection COMPLETED at the "
                    "batch's first candidate read -- the batch had already "
                    "established its SERIALIZABLE snapshot without holding "
                    "the per-user mutex, so it will resolve the stale "
                    "fallback tuple A and write A after B is persisted")
            return original()

        self.store.all_games = read_with_a_racing_first_selection
        try:
            result = self.api.discard_draft_games(
                game_ids=list(self.committed), actor_id=NEWCOMER,
                user_id=NEWCOMER, role=Role.LEAGUE_ADMIN, scope={})
        finally:
            self.store.all_games = original

        self.assertGreaterEqual(seen["calls"], 1,
                                "the batch never read its candidates, so the "
                                "barrier never fired")
        if race["worker"] is not None:
            race["worker"].join(timeout=15)
            self.assertFalse(race["worker"].is_alive(),
                             "the blocked first selection never completed")
        if race["error"] is not None:
            raise race["error"]
        self.assertTrue(race["done"])
        self.assertEqual(self.store.get_active_context(NEWCOMER).program_id,
                         self.pb, "the racing first selection did not land")
        self.assertNotIn("error", result, result)
        self.assertIn(result["discarded"], (0, 6), result)
        self._assert_no_cross_tuple_write(before, set(self.committed))

    def test_the_first_selection_completes_freely_when_nothing_holds_it(self):
        """The anti-vacuity control for the four races above.

        Each of them asserts a thread is STILL ALIVE after two seconds, which
        would also pass for a thread that can never complete at all. With
        nothing holding the mutex the identical first selection must finish
        quickly and land.
        """
        self._assert_no_saved_context()
        started = time.monotonic()
        race = {"error": None, "done": False}

        def first_selection():
            other = SqlStore(os.environ["TEST_DATABASE_URL"])
            try:
                ApiService(other).set_active_context(
                    NEWCOMER, Role.LEAGUE_ADMIN, {}, self.pb, self.sb,
                    self.lb)
                race["done"] = True
            except BaseException as error:
                race["error"] = error
            finally:
                other.close()

        worker = threading.Thread(target=first_selection, daemon=True)
        worker.start()
        worker.join(timeout=10)
        if race["error"] is not None:
            raise race["error"]
        self.assertTrue(race["done"],
                        "the first selection cannot complete even with "
                        "nothing holding the mutex")
        self.assertLess(time.monotonic() - started, 2.0,
                        "the first selection is slow on its own, so 'still "
                        "alive after 2s' proves nothing about the mutex")
        self.assertEqual(self.store.get_active_context(NEWCOMER).program_id,
                         self.pb)


class DraftActiveTupleHttpTest(unittest.TestCase):
    """The same contract over the real authenticated routes.

    Every refusal comparison here is made on RAW RESPONSE BYTES, not on parsed
    JSON: two payloads can parse equal and still differ in key order, in a
    field one of them omits entirely, or in the status line. Indistinguishable
    has to mean indistinguishable to the client that is probing.
    """

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        srv.STATE.reset()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.fixture = build_two_programs(srv.STATE.api)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)
        cls.httpd.server_close()

    def _client(self):
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))

    def _raw(self, client, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with client.open(request) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as error:
            with error:
                return error.code, error.read().decode()

    def _json(self, client, method, path, body=None):
        status, raw = self._raw(client, method, path, body)
        return status, json.loads(raw or "{}")

    def _login(self, username):
        client = self._client()
        status, body = self._json(client, "POST", "/api/auth/login",
                                  {"username": username, "password": "demo"})
        self.assertEqual(status, 200, body)
        return client

    def _select(self, client, program_id, season_id, league_id):
        status, body = self._json(client, "POST", "/api/context", {
            "program_id": program_id, "season_id": season_id,
            "league_id": league_id})
        self.assertEqual(status, 200, body)

    def test_signed_out_and_non_operator_are_refused_before_any_tuple(self):
        anonymous = self._client()
        routes = (("POST", "/api/scheduler/draft", {"division_id": "x"}),
                  ("POST", "/api/scheduler/commit", {"division_id": "x"}),
                  ("GET", "/api/scheduler/drafts", None),
                  ("POST", "/api/scheduler/drafts/publish", {"all": True}),
                  ("POST", "/api/scheduler/drafts/discard", {"all": True}))
        for method, path, body in routes:
            with self.subTest(signed_out=f"{method} {path}"):
                status, _ = self._json(anonymous, method, path, body)
                self.assertEqual(status, 401)
        coach = self._login("coach")
        for method, path, body in routes:
            with self.subTest(coach=f"{method} {path}"):
                status, _ = self._json(coach, method, path, body)
                self.assertEqual(status, 403)

    def test_both_global_roles_are_bound_over_http(self):
        """The reported blocker, over the real route, for both roles that hold
        ``MANAGE_SCHEDULE`` -- and the control on the far side, so switching
        back restores exactly the authority switching away removed."""
        _pa, sa, la, da = corner(self.fixture, "A", "1", "a")
        pb, sb, lb, _db = corner(self.fixture, "B", "1", "a")
        for username in ("admin", "arena"):
            with self.subTest(role=username):
                client = self._login(username)
                self._select(client, pb, sb, lb)
                for label, body in (
                        ("division shape", {"division_id": da}),
                        ("league-wide shape",
                         {"season_id": sa, "league_id": la})):
                    with self.subTest(shape=label):
                        status, payload = self._json(
                            client, "POST", "/api/scheduler/draft", body)
                        self.assertEqual(
                            status, 404,
                            f"a {username} active in Program B received "
                            f"Program A's proposal ({label}): {payload}")
                        self.assertNotIn("draft_games", payload)
                # The far side of the refusal: switch back and it works.
                self._select(client, *corner(self.fixture, "A", "1", "a")[:3])
                status, payload = self._json(
                    client, "POST", "/api/scheduler/draft",
                    {"division_id": da})
                self.assertEqual(status, 200, payload)
                self.assertEqual(len(payload["draft_games"]), 6)

    def test_foreign_and_nonexistent_are_byte_identical_over_http(self):
        """Compared on raw bytes AND status, per shape, for both roles."""
        _pa, sa, la, da = corner(self.fixture, "A", "1", "a")
        pb, sb, lb, _db = corner(self.fixture, "B", "1", "a")
        for username in ("admin", "arena"):
            client = self._login(username)
            self._select(client, pb, sb, lb)
            for label, foreign, missing in (
                    ("division shape",
                     {"division_id": da},
                     {"division_id": NO_SUCH_DIVISION}),
                    ("league-wide shape",
                     {"season_id": sa, "league_id": la},
                     {"season_id": NO_SUCH_SEASON,
                      "league_id": NO_SUCH_LEAGUE}),
                    ("league-wide + junk division",
                     {"season_id": sa, "league_id": la,
                      "division_id": NO_SUCH_DIVISION},
                     {"season_id": NO_SUCH_SEASON,
                      "league_id": NO_SUCH_LEAGUE,
                      "division_id": NO_SUCH_DIVISION})):
                with self.subTest(role=username, shape=label):
                    denied = self._raw(client, "POST",
                                       "/api/scheduler/draft", foreign)
                    guessed = self._raw(client, "POST",
                                        "/api/scheduler/draft", missing)
                    self.assertEqual(
                        denied, guessed,
                        f"the {label} response for a FOREIGN target is not "
                        "byte-identical to the one for a NONEXISTENT target")
                    self.assertEqual(denied[0], 404, denied)

    def test_a_refused_http_body_names_nothing_of_the_target(self):
        _pa, sa, la, da = corner(self.fixture, "A", "1", "a")
        pb, sb, lb, _db = corner(self.fixture, "B", "1", "a")
        secrets = foreign_identifiers(self.fixture, "A", "1", "a")
        for username in ("admin", "arena"):
            client = self._login(username)
            self._select(client, pb, sb, lb)
            for label, body in (
                    ("draft", {"division_id": da,
                               "constraints": copy.deepcopy(
                                   REFUSED_CONSTRAINTS)}),
                    ("draft league-wide", {"season_id": sa, "league_id": la,
                                           "constraints": copy.deepcopy(
                                               REFUSED_CONSTRAINTS)})):
                with self.subTest(role=username, request=label):
                    _status, raw = self._raw(
                        client, "POST", "/api/scheduler/draft", body)
                    leaked = leaked_identifiers(secrets, raw)
                    self.assertEqual(
                        leaked, [],
                        f"the refused {label} response named foreign "
                        f"identifiers {leaked}")

    def test_commit_over_http_is_bound_and_byte_identical(self):
        """The write verb, over the route, including that a refused commit
        creates no Game at all."""
        _pa, _sa, _la, da = corner(self.fixture, "A", "1", "a")
        pb, sb, lb, _db = corner(self.fixture, "B", "1", "a")
        store = srv.STATE.api.store
        before = {row.id for row in store.all_games()}
        for username in ("admin", "arena"):
            client = self._login(username)
            self._select(client, pb, sb, lb)
            for label, fingerprint in (("no fingerprint", None),
                                       ("a guessed fingerprint", "deadbeef")):
                with self.subTest(role=username, fingerprint=label):
                    body = {"division_id": da}
                    if fingerprint is not None:
                        body["draft_fingerprint"] = fingerprint
                    denied = self._raw(client, "POST",
                                       "/api/scheduler/commit", body)
                    guess = dict(body, division_id=NO_SUCH_DIVISION)
                    guessed = self._raw(client, "POST",
                                        "/api/scheduler/commit", guess)
                    self.assertEqual(
                        denied, guessed,
                        "a foreign COMMIT target is not byte-identical to a "
                        f"nonexistent one ({label})")
                    self.assertEqual(denied[0], 404, denied)
        self.assertEqual({row.id for row in store.all_games()}, before,
                         "a refused commit created a Game")

    def test_the_review_routes_are_bound_over_http(self):
        """``GET /api/scheduler/drafts`` answered with every draft Game in the
        installation, and the two write verbs took ``game_ids`` on trust."""
        pa, sa, la, da = corner(self.fixture, "A", "1", "a")
        pb, sb, lb, db = corner(self.fixture, "B", "1", "a")
        admin = self._login("admin")

        committed = {}
        for tag, (program, season, league, division) in (
                ("A", (pa, sa, la, da)), ("B", (pb, sb, lb, db))):
            self._select(admin, program, season, league)
            status, preview = self._json(admin, "POST", "/api/scheduler/draft",
                                         {"division_id": division})
            self.assertEqual(status, 200, preview)
            status, result = self._json(admin, "POST", "/api/scheduler/commit", {
                "division_id": division,
                "draft_fingerprint": preview["draft_fingerprint"]})
            self.assertEqual(status, 200, result)
            committed[tag] = [row["game_id"] for row in result["created"]]

        secrets = foreign_identifiers(self.fixture, "A", "1", "a")
        for username in ("admin", "arena"):
            with self.subTest(role=username):
                client = self._login(username)
                self._select(client, pb, sb, lb)
                status, raw = self._raw(client, "GET", "/api/scheduler/drafts")
                self.assertEqual(status, 200, raw)
                listed = json.loads(raw)
                self.assertEqual(
                    sorted(row["game_id"] for row in listed["draft_games"]),
                    sorted(committed["B"]),
                    f"a {username} active in Program B listed Program A's "
                    "draft games")
                leaked = leaked_identifiers(secrets, raw)
                self.assertEqual(
                    leaked, [],
                    f"the review list named foreign identifiers {leaked}")

                # ...and neither write verb can reach A's rows by id.
                status, payload = self._json(
                    client, "POST", "/api/scheduler/drafts/publish",
                    {"game_ids": list(committed["A"])})
                self.assertEqual((status, payload), (200, {"published": 0}))
                status, payload = self._json(
                    client, "POST", "/api/scheduler/drafts/discard",
                    {"game_ids": list(committed["A"])})
                self.assertEqual((status, payload), (200, {"discarded": 0}))

        store = srv.STATE.api.store
        self.assertEqual(
            {row.id for row in store.all_games()
             if row.is_draft and not row.published} & set(committed["A"]),
            set(committed["A"]),
            "Program A's committed drafts were published or discarded by a "
            "Program-B-selected operator")

        # The control, on the far side: A's own operator still owns them.
        self._select(admin, pa, sa, la)
        status, payload = self._json(
            admin, "POST", "/api/scheduler/drafts/discard",
            {"game_ids": list(committed["A"])})
        self.assertEqual((status, payload), (200, {"discarded": 6}))


if __name__ == "__main__":
    unittest.main()
