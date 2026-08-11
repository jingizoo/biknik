"""Active-tuple scoping for named schedule scenarios (#206/#378, blocker #381).

The blocker. The four scenario entry points authorized only the
``MANAGE_SCHEDULE`` capability, never the scenario's own Program/Season/League
target. ``POST /api/scheduler/scenarios`` accepted caller-supplied foreign
scope ids, ``GET /api/scheduler/scenarios`` returned every stored scenario, and
get/commit-by-id fetched or committed any scenario at all. League Admin and
Arena Manager are both ``context_scope._GLOBAL_ROLES`` -- authorized for every
Program in the installation -- so an operator whose active tuple was Program B
could create a Program A scenario, read A's name, creator, constraints, whole
proposal and generation snapshot, and commit A's frozen Games into A. A
cross-context information disclosure and an IDOR write behind one missing
check, and the pre-existing HTTP tests covered only operator vs non-operator,
so a green CI never touched the boundary.

This is the ground #367/#369/#372 already established, and the fix is the SAME
mechanism rather than a parallel one:

* the tuple is resolved SERVER-SIDE via
  ``ContextService.resolve_with_league(user_id, role, scope)``. Scope ids in a
  request body select WHICH rows to consider and are never entitlement to them;
* a scenario's ``(program_id, season_id, league_id)`` is ONE WHOLE EDGE, judged
  by ``ApiService._setup_target_edge_allows`` verbatim -- #369's "edges, not
  unions" predicate. Three independent axis unions would authorize
  combinations that do not exist;
* every scenario is Season- AND League-bound by construction, so a Program-only
  context fails CLOSED against all of them, exactly as ``get_standings`` does;
* there is NO creator clause. ``setup_target_accessible`` admits one only for a
  genuinely UNLINKED record, and #372 ruled creator authority surviving
  hierarchy linking a blocker in its own right;
* the LIST is filtered on stored rows BEFORE any DTO is built, so a foreign
  scenario's payload is never assembled at all;
* a foreign id and a nonexistent id leave through ONE raise site, so they are
  response-identical in status AND bytes;
* commit RE-AUTHORIZES under the scenario's row lock, inside the same
  transaction as the Game writes, so switching context after generation cannot
  retain commit authority.

Coverage, on Memory/SQLite/PostgreSQL at the service boundary and over real
authenticated HTTP:

1. a B-selected League Admin and Arena Manager cannot create, list, fetch or
   commit A's scenario -- including the same-Program/different-Season and
   same-Season/different-League near misses;
2. the list contains only the active exact tuple;
3. a foreign id and a nonexistent id are response-identical;
4. changing context between create and commit refuses with zero Game, slot,
   counter or audit change;
5. the SAME actor, in the scenario's exact tuple, still creates, reads and
   commits successfully -- the anti-vacuity control every negative above is
   measured against.
"""

import copy
import json
import os
import threading
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


ADMIN = "user_scope_admin"
ARENA = "user_scope_arena"
ICE_BASE = datetime(2026, 9, 7, 18, tzinfo=timezone.utc)
GLOBAL_PRINCIPALS = ((ADMIN, Role.LEAGUE_ADMIN), (ARENA, Role.ARENA_MANAGER))


def _backends():
    """Memory/SQLite always; PostgreSQL only with ``TEST_DATABASE_URL`` set --
    the same idiom ``test_league_filtered_standings.py`` uses."""
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
    idiom ``test_schedule_scenarios`` already uses, because PostgreSQL test
    databases retain counters across per-test data clears."""
    if isinstance(store, InMemoryStore):
        return store._counters.get("game")
    row = store._exec("SELECT value FROM counters WHERE prefix = ?",
                      ("game",)).fetchone()
    return row["value"] if row else None


def _mask_id(payload, scenario_id):
    """The caller's OWN echoed input, replaced -- everything else must already
    be equal for the two refusals to be indistinguishable."""
    masked = copy.deepcopy(payload)
    details = (masked.get("error") or {}).get("details")
    if isinstance(details, dict) and details.get("scenario_id") == scenario_id:
        details["scenario_id"] = "<echoed>"
    return masked


def _league_season(api, tag, program, season_id, league_id, club_id, rink_id,
                   slot_day, teams=4, actor_id=None):
    """One schedulable ``(Season, League)`` corner: a Division, ``teams``
    permanent Teams registered into it, and enough ice for a full single
    round-robin.

    Enough ice is the load-bearing part. Every negative below is measured
    against a corner that really CAN produce a non-empty proposal, so a refusal
    can never be confused with "there was nothing to schedule here anyway".
    """
    division = _ok(api.create_division(
        season_id, f"Division {tag}", league_id=league_id, actor_id=actor_id),
        "division")
    team_ids = []
    for index in range(teams):
        team = _ok(api.create_team(
            club_id, None, f"{tag} Team {index}", actor_id=actor_id,
            program_id=program, league_id=league_id), "team")
        _ok(api.register_team_for_season(
            season_id, team["id"], division["id"], actor_id=actor_id,
            league_id=league_id), "registration")
        team_ids.append(team["id"])
    # C(teams, 2) fixtures need at least that many distinct slots.
    for index in range((teams * (teams - 1)) // 2 + 2):
        start = ICE_BASE + timedelta(days=slot_day + index)
        _ok(api.create_ice_slot(
            rink_id, start.isoformat(),
            (start + timedelta(hours=2)).isoformat(), actor_id=actor_id),
            "slot")
    return {"division": division["id"], "teams": team_ids}


def build_two_programs(api, actor_id=None):
    """Two Programs, and inside Program A the two near-miss corners the owner
    named: same-Program/different-Season, and same-Season/different-League.

      A / A1 / Aa   -- the ACTIVE tuple in most tests below
      A / A1 / Ab   -- same Program, same Season, DIFFERENT League
      A / A2 / Aa2  -- same Program, DIFFERENT Season
      B / B1 / Ba   -- a wholly foreign Program

    Each corner is independently schedulable, so "refused" and "empty" are
    never the same observation.
    """
    fixture = {}
    day = 0
    for tag in ("A", "B"):
        program = _ok(api.create_program(f"Program {tag}", actor_id=actor_id),
                      "program")
        club = _ok(api.create_club(f"Club {tag}", actor_id=actor_id), "club")
        # `Venue.league_id` is LEGACY vocabulary and stores a PROGRAM id -- not
        # a competition League. Passing a League id here would silently build a
        # Venue linked to nothing this fixture can grant against.
        venue = _ok(api.create_venue(f"Venue {tag}", league_id=program["id"],
                                     actor_id=actor_id), "venue")
        rink = _ok(api.create_rink(venue["id"], f"Rink {tag}",
                                   actor_id=actor_id), "rink")
        fixture[tag] = {"program": program["id"], "club": club["id"],
                        "venue": venue["id"], "rink": rink["id"],
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


class ScenarioActiveTupleTest(unittest.TestCase):
    """The service boundary, across Memory / SQLite / PostgreSQL."""

    maxDiff = None

    # -- helpers -----------------------------------------------------------
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

    def _create(self, api, name, division_id, user_id, role):
        return api.create_schedule_scenario(
            name, division_id=division_id, actor_id=user_id,
            user_id=user_id, role=role, scope={})

    # -- clause 5, the CONTROL, first -------------------------------------
    def test_actor_in_the_exact_tuple_can_create_read_list_and_commit(self):
        """Every negative below is only meaningful because this same actor, in
        the scenario's exact tuple, really can do all four things. A test that
        cannot fail is worse than no test."""
        def body(store, api):
            fixture = build_two_programs(api)
            pa, sa, la, da = corner(fixture, "A", "1", "a")
            self._select(api, ADMIN, Role.LEAGUE_ADMIN, pa, sa, la)

            created = self._create(api, "A1a", da, ADMIN, Role.LEAGUE_ADMIN)
            self.assertNotIn("error", created, created)
            self.assertEqual(created["scope"]["program_id"], pa)
            self.assertEqual(created["scope"]["season_id"], sa)
            self.assertEqual(created["scope"]["league_id"], la)
            self.assertEqual(len(created["proposal"]["draft_games"]), 6)

            fetched = api.get_schedule_scenario(
                created["scenario_id"], ADMIN, Role.LEAGUE_ADMIN, {})
            self.assertEqual(fetched, created)

            listed = api.list_schedule_scenarios(ADMIN, Role.LEAGUE_ADMIN, {})
            self.assertEqual(
                [row["scenario_id"] for row in listed["scenarios"]],
                [created["scenario_id"]])

            committed = api.commit_schedule_scenario(
                created["scenario_id"], actor_id=ADMIN, user_id=ADMIN,
                role=Role.LEAGUE_ADMIN, scope={})
            self.assertNotIn("error", committed, committed)
            self.assertEqual(len(committed["created"]), 6)
            self.assertEqual(len(store.all_games()), 6)
        self._on_every_backend(body)

    # -- clause 1: create --------------------------------------------------
    def test_b_selected_principal_cannot_create_into_program_a(self):
        def body(store, api):
            fixture = build_two_programs(api)
            _pa, _sa, _la, da = corner(fixture, "A", "1", "a")
            pb, sb, lb, _db = corner(fixture, "B", "1", "a")
            for user_id, role in GLOBAL_PRINCIPALS:
                with self.subTest(role=role.value):
                    self._select(api, user_id, role, pb, sb, lb)
                    refused = self._create(api, "Stolen", da, user_id, role)
                    self.assertIn(
                        "error", refused,
                        f"a {role.value} active in Program B CREATED a "
                        "Program A scenario")
                    self.assertEqual(refused["error"]["code"], "not_found",
                                     refused)
                    self.assertEqual(refused["error"]["details"]["reason"],
                                     "division_missing", refused)
            self.assertEqual(store.all_schedule_scenarios(), [])
            self.assertFalse(any(
                row.action == "schedule_scenario_created"
                for row in store.all_setup_audit()))
        self._on_every_backend(body)

    def test_create_refuses_the_two_near_miss_corners(self):
        """Same Program/different Season, and same Season/different League.

        These are the cases a Program-only ceiling would wave through, and the
        ones "edges, not unions" exists for.
        """
        def body(store, api):
            fixture = build_two_programs(api)
            pa, sa, la, _da = corner(fixture, "A", "1", "a")
            self._select(api, ADMIN, Role.LEAGUE_ADMIN, pa, sa, la)
            for label, (season_tag, league_tag) in (
                    ("different League, same Season", ("1", "b")),
                    ("different Season, same Program", ("2", "a"))):
                with self.subTest(near_miss=label):
                    _p, _s, _l, division = corner(
                        fixture, "A", season_tag, league_tag)
                    refused = self._create(
                        api, "Near miss", division, ADMIN, Role.LEAGUE_ADMIN)
                    self.assertIn(
                        "error", refused,
                        f"a scenario was created in the {label} corner while "
                        "another tuple was selected")
                    self.assertEqual(refused["error"]["details"]["reason"],
                                     "division_missing", refused)
            self.assertEqual([row.id for row in store.all_schedule_scenarios()],
                             [])
        self._on_every_backend(body)

    def test_create_by_league_wide_scope_ids_is_bound_to_the_same_tuple(self):
        """The League-wide shape takes season_id + league_id STRAIGHT from the
        body -- precisely the "caller-supplied foreign scope ids" half of the
        blocker -- and its refusal matches an unlinked pair's byte for byte."""
        def body(store, api):
            fixture = build_two_programs(api)
            _pa, sa, la, _da = corner(fixture, "A", "1", "a")
            pb, sb, lb, _db = corner(fixture, "B", "1", "a")
            self._select(api, ADMIN, Role.LEAGUE_ADMIN, pb, sb, lb)

            refused = api.create_schedule_scenario(
                "League-wide theft", season_id=sa, league_id=la,
                actor_id=ADMIN, user_id=ADMIN, role=Role.LEAGUE_ADMIN,
                scope={})
            self.assertIn(
                "error", refused,
                "caller-supplied foreign season_id + league_id were accepted "
                "while another tuple was selected")
            self.assertEqual(refused["error"]["code"], "not_found", refused)
            self.assertEqual(refused["error"]["details"]["reason"],
                             "league_season_missing", refused)
            nonexistent = api.create_schedule_scenario(
                "League-wide guess", season_id="season_nope",
                league_id="league_nope", actor_id=ADMIN, user_id=ADMIN,
                role=Role.LEAGUE_ADMIN, scope={})
            self.assertEqual(refused, nonexistent)

            # Control: the SAME shape inside the active tuple succeeds, so the
            # refusal above is the tuple check and not the shape.
            own = api.create_schedule_scenario(
                "League-wide own", season_id=sb, league_id=lb, actor_id=ADMIN,
                user_id=ADMIN, role=Role.LEAGUE_ADMIN, scope={})
            self.assertNotIn("error", own, own)
            self.assertEqual(len(store.all_schedule_scenarios()), 1)
        self._on_every_backend(body)

    def test_create_refusals_do_not_leak_whether_a_foreign_pair_is_linked(self):
        """The MIXED request shape -- division_id AND season_id + league_id.

        Found in re-review, and a real leak: the Season+League branch of
        ``resolve_scenario_scope`` learns its facts in a LADDER (the pair is a
        real LeagueSeason -> the two share a Program -> this Division hangs off
        that LeagueSeason) and each rung refuses with its own reason. With the
        tuple checked only on the RESOLVED hierarchy, a caller active in Program
        B could add a junk ``division_id`` to a guessed ``(season_id, league_id)``
        and read the ladder off the refusal:

          ``division_missing``     -> the pair IS a real linked LeagueSeason
          ``league_season_missing``-> it is not

        -- an existence oracle over another Program's hierarchy, answered before
        any authorization ran, over an id space (`season_3`, `league_5`, ...)
        that is enumerable. The fix judges the edge as soon as the LINK is
        proven, so both rungs collapse to the one refusal a guess produces.

        The controls are what stop this test from passing on a facade that
        simply answers ``league_season_missing`` to everything: inside the
        caller's OWN tuple the specific in-LeagueSeason Division diagnostic is
        still required, and the same shape with a real own Division still
        creates.
        """
        def body(store, api):
            fixture = build_two_programs(api)
            _pa, sa, la, da = corner(fixture, "A", "1", "a")
            pb, sb, lb, db = corner(fixture, "B", "1", "a")
            self._select(api, ADMIN, Role.LEAGUE_ADMIN, pb, sb, lb)

            def create(name, **kwargs):
                return api.create_schedule_scenario(
                    name, actor_id=ADMIN, user_id=ADMIN,
                    role=Role.LEAGUE_ADMIN, scope={}, **kwargs)

            probed = create("Probe A's link", season_id=sa, league_id=la,
                            division_id="division_nope")
            guessed = create("Probe a guess", season_id="season_nope",
                             league_id="league_nope",
                             division_id="division_nope")
            self.assertIn("error", probed, probed)
            self.assertEqual(
                json.dumps(probed, sort_keys=True),
                json.dumps(guessed, sort_keys=True),
                "a junk division_id alongside a FOREIGN season_id + league_id "
                "answered differently from a guessed pair -- the refusal tells "
                "an unauthorized caller whether that LeagueSeason is real")
            # And the same probe against a foreign division that really exists
            # inside that foreign LeagueSeason must not answer differently
            # either -- otherwise the oracle just moves one rung along.
            self.assertEqual(
                json.dumps(create("Probe A's division", season_id=sa,
                                  league_id=la, division_id=da),
                           sort_keys=True),
                json.dumps(guessed, sort_keys=True))
            self.assertEqual(store.all_schedule_scenarios(), [])

            # Control 1: inside the caller's OWN tuple the mixed shape still
            # reports the precise, useful diagnostic -- the leak was closed by
            # ordering the check, not by flattening every refusal.
            own_bad_division = create(
                "Own tuple, bad division", season_id=sb, league_id=lb,
                division_id="division_nope")
            self.assertEqual(
                own_bad_division["error"]["details"]["reason"],
                "division_missing", own_bad_division)
            self.assertEqual(
                own_bad_division["error"]["message"],
                "Division not found in the selected LeagueSeason.",
                own_bad_division)
            # Control 2: the same shape, wholly inside the tuple, still works.
            own = create("Own tuple, own division", season_id=sb,
                         league_id=lb, division_id=db)
            self.assertNotIn("error", own, own)
            self.assertEqual(len(store.all_schedule_scenarios()), 1)
        self._on_every_backend(body)

    # -- clause 2: list ----------------------------------------------------
    def test_list_contains_only_the_active_exact_tuple(self):
        corners = (("A1a", ("A", "1", "a")), ("A1b", ("A", "1", "b")),
                   ("A2a", ("A", "2", "a")), ("B1a", ("B", "1", "a")))

        def body(store, api):
            fixture = build_two_programs(api)
            everywhere = {}
            for tag, path in corners:
                p, s, lg, division = corner(fixture, *path)
                self._select(api, ADMIN, Role.LEAGUE_ADMIN, p, s, lg)
                everywhere[tag] = _ok(self._create(
                    api, f"Scenario {tag}", division, ADMIN,
                    Role.LEAGUE_ADMIN), tag)["scenario_id"]
            self.assertEqual(len(store.all_schedule_scenarios()), 4)

            for tag, path in corners:
                p, s, lg, _d = corner(fixture, *path)
                for user_id, role in GLOBAL_PRINCIPALS:
                    with self.subTest(active=tag, role=role.value):
                        self._select(api, user_id, role, p, s, lg)
                        listed = api.list_schedule_scenarios(
                            user_id, role, {})
                        self.assertEqual(
                            [row["scenario_id"]
                             for row in listed["scenarios"]],
                            [everywhere[tag]],
                            f"{role.value} active in {tag} saw more than its "
                            "own tuple")
                        # Nothing of any other tuple is even NAMED.
                        self.assertEqual(
                            {row["name"] for row in listed["scenarios"]},
                            {f"Scenario {tag}"})
        self._on_every_backend(body)

    def test_list_never_builds_a_dto_for_a_foreign_scenario(self):
        """The owner's wording is "filter list BEFORE DTO construction", and
        that placement needs its own assertion.

        A filter applied to a list of DTOs returns the identical payload, so no
        contents check can tell the two implementations apart. What differs is
        that the wrong one has already deep-copied every foreign scenario's
        name, creator, constraints, whole proposal and generation snapshot into
        a response object -- the count, shape and cost of that work are a signal
        on their own, and it is one `return` away from being the payload. So
        this records which rows the DTO builder is handed at all.
        """
        def body(store, api):
            fixture = build_two_programs(api)
            pa, sa, la, da = corner(fixture, "A", "1", "a")
            pb, sb, lb, db = corner(fixture, "B", "1", "a")
            self._select(api, ADMIN, Role.LEAGUE_ADMIN, pa, sa, la)
            a_id = _ok(self._create(api, "A's evidence", da, ADMIN,
                                    Role.LEAGUE_ADMIN))["scenario_id"]
            self._select(api, ADMIN, Role.LEAGUE_ADMIN, pb, sb, lb)
            b_id = _ok(self._create(api, "B's evidence", db, ADMIN,
                                    Role.LEAGUE_ADMIN))["scenario_id"]

            built = []
            plain = ApiService._scenario_dto

            def recording(scenario):
                built.append(scenario.id)
                return plain(scenario)

            api._scenario_dto = recording
            try:
                listed = api.list_schedule_scenarios(
                    ADMIN, Role.LEAGUE_ADMIN, {})
            finally:
                del api._scenario_dto

            self.assertEqual([row["scenario_id"] for row in
                              listed["scenarios"]], [b_id])
            self.assertEqual(
                built, [b_id],
                "a DTO was assembled for a scenario outside the active tuple "
                "-- the list must be filtered on stored rows BEFORE any "
                "payload is built")
            self.assertNotIn(a_id, built)
        self._on_every_backend(body)

    def test_program_only_context_lists_and_fetches_nothing(self):
        """A scenario is Season-bound by construction, so a context with no
        Season resolved has nothing to compare against and must fail closed --
        never fall back to "every Season of the Program"."""
        def body(store, api):
            fixture = build_two_programs(api)
            pa, sa, la, da = corner(fixture, "A", "1", "a")
            self._select(api, ADMIN, Role.LEAGUE_ADMIN, pa, sa, la)
            scenario = _ok(self._create(
                api, "A1a", da, ADMIN, Role.LEAGUE_ADMIN))
            self.assertEqual(len(store.all_schedule_scenarios()), 1)

            _ok(api.set_active_context(ADMIN, Role.LEAGUE_ADMIN, {}, pa, None,
                                       None), "program-only")
            self.assertEqual(
                api.list_schedule_scenarios(ADMIN, Role.LEAGUE_ADMIN, {}),
                {"scenarios": []})
            fetched = api.get_schedule_scenario(
                scenario["scenario_id"], ADMIN, Role.LEAGUE_ADMIN, {})
            self.assertEqual(fetched["error"]["details"]["reason"],
                             "schedule_scenario_missing", fetched)
        self._on_every_backend(body)

    def test_a_principal_with_no_authorized_program_fails_closed(self):
        """`program is None` -- rule 2 of `setup_target_accessible`, restated.

        Distinct from the Program-only case above, which HAS a Program and
        fails on the Season axis. Here `resolve_with_league` returns
        `(None, None, None)` because the principal is authorized for nothing at
        all: an UNBOUND Coach (`scope={}` resolves to no team, so
        `authorized_program_ids` is empty). Found unfalsified in re-review --
        flipping the guard to `return True` broke no test, because every other
        fixture here drives a GLOBAL role, which is authorized for every
        Program and so can never produce a null one.

        Reachable only at the service boundary today (the HTTP layer refuses a
        Coach on the MANAGE_SCHEDULE gate first), which is exactly why the
        service-level clause has to be held by a service-level test: nothing
        upstream would notice it rotting.
        """
        def body(store, api):
            fixture = build_two_programs(api)
            pa, sa, la, da = corner(fixture, "A", "1", "a")
            self._select(api, ADMIN, Role.LEAGUE_ADMIN, pa, sa, la)
            scenario = _ok(self._create(api, "A1a", da, ADMIN,
                                        Role.LEAGUE_ADMIN))
            unbound = ("user_unbound_coach", Role.COACH)
            # The fixture's own premise: this principal really does resolve to
            # no Program. If that ever stops being true the assertions below
            # would pass for the wrong reason.
            self.assertEqual(
                api.context.resolve_with_league(*unbound, {})[0], None)

            self.assertEqual(
                api.list_schedule_scenarios(*unbound, {}), {"scenarios": []})
            fetched = api.get_schedule_scenario(
                scenario["scenario_id"], *unbound, {})
            self.assertIn("error", fetched,
                          "a principal authorized for NO Program read a "
                          "scenario")
            self.assertEqual(fetched["error"]["details"]["reason"],
                             "schedule_scenario_missing", fetched)
            committed = api.commit_schedule_scenario(
                scenario["scenario_id"], actor_id=unbound[0],
                user_id=unbound[0], role=unbound[1], scope={})
            self.assertIn("error", committed,
                          "a principal authorized for NO Program COMMITTED a "
                          "scenario")
            # #409 — the refusal now lands one rung EARLIER, and that is the
            # stricter answer rather than a different one. A principal with no
            # authorized Program has, by construction, no EXPLICIT persisted
            # selection either, so `_require_explicit_selection` refuses at the
            # head of the committing transaction and the scenario row is never
            # looked up at all. `schedule_scenario_missing` was the verdict of
            # `_scenario_in_active_tuple`, which sits BEHIND that gate.
            self.assertEqual(committed["error"]["code"],
                             "active_context_required", committed)
            # ...and it is INDEPENDENT of the id, which is the property that
            # made the old wording safe and must survive the reordering: a
            # scenario that never existed answers byte-identically, so the
            # refusal is still no kind of existence oracle.
            ghost = api.commit_schedule_scenario(
                "scenario_never_existed", actor_id=unbound[0],
                user_id=unbound[0], role=unbound[1], scope={})
            self.assertEqual(ghost, committed, (ghost, committed))
            created = self._create(api, "By nobody", da, *unbound)
            self.assertIn("error", created,
                          "a principal authorized for NO Program CREATED a "
                          "scenario")
            # #409, exactly as for the commit above: creating a scenario
            # PERSISTS a row bound to a (Program, Season, League) the caller
            # never chose, so the explicit-selection gate refuses at the head
            # of the transaction and `division_missing` — the verdict of the
            # generator's own scope resolution, which sits behind it — is never
            # reached. Earlier, not different.
            self.assertEqual(created["error"]["code"],
                             "active_context_required", created)
            ghost_created = self._create(
                api, "By nobody", "division_never_existed", *unbound)
            self.assertEqual(ghost_created, created,
                             (ghost_created, created))
            self.assertEqual(store.all_games(), [])
            self.assertEqual(len(store.all_schedule_scenarios()), 1)
        self._on_every_backend(body)

    def test_no_league_selection_is_the_program_plus_season_union(self):
        """"No League" is a first-class SELECTION everywhere else in this
        repository (the approved Program + active-Season union), and it stays
        one here -- while the Season ceiling above it does not move."""
        def body(store, api):
            fixture = build_two_programs(api)
            pa, sa, la, da = corner(fixture, "A", "1", "a")
            _p, _s, lb, db = corner(fixture, "A", "1", "b")
            _p2, s2, l2, d2 = corner(fixture, "A", "2", "a")
            for division, season, league in ((da, sa, la), (db, sa, lb),
                                             (d2, s2, l2)):
                self._select(api, ADMIN, Role.LEAGUE_ADMIN, pa, season, league)
                _ok(self._create(api, f"Scenario {division}", division, ADMIN,
                                 Role.LEAGUE_ADMIN))

            _ok(api.set_active_context(ADMIN, Role.LEAGUE_ADMIN, {}, pa, sa,
                                       None), "no league")
            listed = api.list_schedule_scenarios(ADMIN, Role.LEAGUE_ADMIN, {})
            self.assertEqual({row["scope"]["league_id"]
                              for row in listed["scenarios"]}, {la, lb})
            self.assertEqual({row["scope"]["season_id"]
                              for row in listed["scenarios"]}, {sa},
                             "the Season ceiling must not relax just because "
                             "No League is selected")
        self._on_every_backend(body)

    # -- clause 3: foreign and nonexistent are response-identical ----------
    def test_foreign_and_nonexistent_get_are_response_identical(self):
        def body(store, api):
            fixture = build_two_programs(api)
            pa, sa, la, da = corner(fixture, "A", "1", "a")
            pb, sb, lb, _db = corner(fixture, "B", "1", "a")
            self._select(api, ADMIN, Role.LEAGUE_ADMIN, pa, sa, la)
            foreign_id = _ok(self._create(
                api, "A's private work", da, ADMIN,
                Role.LEAGUE_ADMIN))["scenario_id"]

            for user_id, role in GLOBAL_PRINCIPALS:
                with self.subTest(role=role.value):
                    self._select(api, user_id, role, pb, sb, lb)
                    denied = api.get_schedule_scenario(
                        foreign_id, user_id, role, {})
                    missing = api.get_schedule_scenario(
                        "schedule_scenario_nope", user_id, role, {})
                    # THE assertion: the two payloads differ in NOTHING but the
                    # id the caller itself supplied. Mask that one echo and
                    # they serialize to the same bytes.
                    self.assertEqual(
                        json.dumps(_mask_id(denied, foreign_id),
                                   sort_keys=True),
                        json.dumps(_mask_id(missing, "schedule_scenario_nope"),
                                   sort_keys=True))
                    self.assertNotIn("A's private work", json.dumps(denied))
        self._on_every_backend(body)

    def test_foreign_and_nonexistent_commit_are_response_identical(self):
        def body(store, api):
            fixture = build_two_programs(api)
            pa, sa, la, da = corner(fixture, "A", "1", "a")
            pb, sb, lb, _db = corner(fixture, "B", "1", "a")
            self._select(api, ADMIN, Role.LEAGUE_ADMIN, pa, sa, la)
            foreign_id = _ok(self._create(
                api, "A's batch", da, ADMIN,
                Role.LEAGUE_ADMIN))["scenario_id"]

            for user_id, role in GLOBAL_PRINCIPALS:
                with self.subTest(role=role.value):
                    self._select(api, user_id, role, pb, sb, lb)
                    denied = api.commit_schedule_scenario(
                        foreign_id, actor_id=user_id, user_id=user_id,
                        role=role, scope={})
                    missing = api.commit_schedule_scenario(
                        "schedule_scenario_nope", actor_id=user_id,
                        user_id=user_id, role=role, scope={})
                    self.assertEqual(
                        json.dumps(_mask_id(denied, foreign_id),
                                   sort_keys=True),
                        json.dumps(_mask_id(missing, "schedule_scenario_nope"),
                                   sort_keys=True))
            self.assertEqual(store.all_games(), [])
        self._on_every_backend(body)

    # -- clause 4: switching context between create and commit -------------
    def test_switching_context_after_generation_loses_commit_authority(self):
        """The check that must be re-run AT COMMIT, not carried from create.

        Games, ice-slot status, the id counter and the audit trail are all
        asserted unchanged: a refusal that had already written a row and rolled
        back only part of it would surface here.
        """
        def body(store, api):
            fixture = build_two_programs(api)
            pa, sa, la, da = corner(fixture, "A", "1", "a")
            pb, sb, lb, _db = corner(fixture, "B", "1", "a")
            self._select(api, ADMIN, Role.LEAGUE_ADMIN, pa, sa, la)
            scenario = _ok(self._create(api, "Generated in A", da, ADMIN,
                                        Role.LEAGUE_ADMIN))

            games_before = list(store.all_games())
            slots_before = {slot.id: slot.status
                            for slot in store.all_ice_slots()}
            audit_before = len(store.all_setup_audit())
            counter_before = _game_counter(store)

            # The operator switches Program between review and Commit.
            self._select(api, ADMIN, Role.LEAGUE_ADMIN, pb, sb, lb)
            refused = api.commit_schedule_scenario(
                scenario["scenario_id"], actor_id=ADMIN, user_id=ADMIN,
                role=Role.LEAGUE_ADMIN, scope={})

            self.assertIn(
                "error", refused,
                "a scenario generated in Program A was COMMITTED after the "
                "operator switched to Program B")
            self.assertEqual(refused["error"]["details"]["reason"],
                             "schedule_scenario_missing", refused)
            self.assertEqual(store.all_games(), games_before)
            self.assertEqual({slot.id: slot.status
                              for slot in store.all_ice_slots()},
                             slots_before)
            self.assertEqual(len(store.all_setup_audit()), audit_before)
            self.assertEqual(_game_counter(store), counter_before)
            self.assertFalse(any(
                row.action == "schedule_scenario_committed"
                for row in store.all_setup_audit()))

            # ...and switching BACK restores it: the tuple current AT COMMIT
            # TIME is the only thing that ever decided.
            self._select(api, ADMIN, Role.LEAGUE_ADMIN, pa, sa, la)
            committed = api.commit_schedule_scenario(
                scenario["scenario_id"], actor_id=ADMIN, user_id=ADMIN,
                role=Role.LEAGUE_ADMIN, scope={})
            self.assertNotIn("error", committed, committed)
            self.assertEqual(len(committed["created"]), 6)
        self._on_every_backend(body)

    def test_switching_only_the_league_after_generation_also_refuses(self):
        """The narrowest switch there is -- same Program, same Season, sibling
        League -- because a Program-level re-check would wave it through."""
        def body(store, api):
            fixture = build_two_programs(api)
            pa, sa, la, da = corner(fixture, "A", "1", "a")
            _p, _s, lb, _d = corner(fixture, "A", "1", "b")
            self._select(api, ADMIN, Role.LEAGUE_ADMIN, pa, sa, la)
            scenario = _ok(self._create(api, "League Aa", da, ADMIN,
                                        Role.LEAGUE_ADMIN))

            self._select(api, ADMIN, Role.LEAGUE_ADMIN, pa, sa, lb)
            refused = api.commit_schedule_scenario(
                scenario["scenario_id"], actor_id=ADMIN, user_id=ADMIN,
                role=Role.LEAGUE_ADMIN, scope={})
            self.assertIn(
                "error", refused,
                "a League Aa scenario was COMMITTED with sibling League Ab "
                "selected -- the same Program and Season is not the same tuple")
            self.assertEqual(refused["error"]["details"]["reason"],
                             "schedule_scenario_missing", refused)
            self.assertEqual(store.all_games(), [])
        self._on_every_backend(body)

    def test_identity_less_internal_callers_are_completely_ungated(self):
        """``role is None`` is the seed/internal/legacy path and stays
        untouched, exactly as ``setup_target_accessible`` rule 1 requires."""
        def body(store, api):
            fixture = build_two_programs(api)
            _pa, _sa, _la, da = corner(fixture, "A", "1", "a")
            scenario = api.create_schedule_scenario(
                "Internal", division_id=da, actor_id=None)
            self.assertNotIn("error", scenario, scenario)
            self.assertEqual(
                len(api.list_schedule_scenarios()["scenarios"]), 1)
            self.assertNotIn(
                "error", api.get_schedule_scenario(scenario["scenario_id"]))
            self.assertNotIn(
                "error",
                api.commit_schedule_scenario(scenario["scenario_id"]))
        self._on_every_backend(body)


class ScenarioActiveTupleHttpTest(unittest.TestCase):
    """The same contract over the real authenticated route."""

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
        for method, path, body in (
                ("GET", "/api/scheduler/scenarios", None),
                ("GET", "/api/scheduler/scenarios/whatever", None),
                ("POST", "/api/scheduler/scenarios", {"name": "x"}),
                ("POST", "/api/scheduler/scenarios/whatever/commit", {})):
            with self.subTest(signed_out=f"{method} {path}"):
                status, _ = self._json(anonymous, method, path, body)
                self.assertEqual(status, 401)
        coach = self._login("coach")
        for method, path, body in (
                ("GET", "/api/scheduler/scenarios", None),
                ("GET", "/api/scheduler/scenarios/whatever", None),
                ("POST", "/api/scheduler/scenarios", {"name": "x"}),
                ("POST", "/api/scheduler/scenarios/whatever/commit", {})):
            with self.subTest(coach=f"{method} {path}"):
                status, _ = self._json(coach, method, path, body)
                self.assertEqual(status, 403)

    def test_arena_manager_over_http_is_bound_to_its_own_tuple(self):
        """The Arena Manager half of the reported blocker, and the reason the
        role gate was never going to be enough.

        ``Role.ARENA_MANAGER`` HOLDS ``MANAGE_SCHEDULE`` (``roles.py``), so the
        route's capability check passes for it exactly as it does for a League
        Admin -- every refusal below is the active tuple doing the work, and
        nothing else. The control at the end moves the SAME session into the
        scenario's tuple and gets all of it back.
        """
        pa, sa, la, da = corner(self.fixture, "A", "2", "a")
        pb, sb, lb, _db = corner(self.fixture, "B", "1", "a")
        api = srv.STATE.api
        _ok(api.set_active_context(ADMIN, Role.LEAGUE_ADMIN, {}, pa, sa, la))
        scenario = _ok(api.create_schedule_scenario(
            "A2a for the arena test", division_id=da, actor_id=ADMIN,
            user_id=ADMIN, role=Role.LEAGUE_ADMIN, scope={}))
        scenario_id = scenario["scenario_id"]

        arena = self._login("arena")
        self._select(arena, pb, sb, lb)

        status, listed = self._json(arena, "GET", "/api/scheduler/scenarios")
        self.assertEqual(status, 200, listed)
        self.assertNotIn(scenario_id,
                         [row["scenario_id"] for row in listed["scenarios"]])
        self.assertNotIn("A2a for the arena test", json.dumps(listed))

        status, denied = self._json(
            arena, "GET", f"/api/scheduler/scenarios/{scenario_id}")
        self.assertEqual(status, 404, denied)
        status, refused = self._json(
            arena, "POST", "/api/scheduler/scenarios",
            {"name": "Arena cross-program create", "division_id": da})
        self.assertEqual(status, 404, refused)
        status, commit_denied = self._json(
            arena, "POST", f"/api/scheduler/scenarios/{scenario_id}/commit",
            {})
        self.assertEqual(status, 404, commit_denied)

        # Control: the SAME Arena Manager session, moved into the scenario's
        # exact tuple, reads it -- proving the four refusals above are the
        # tuple and not the role or a broken fixture.
        self._select(arena, pa, sa, la)
        status, allowed = self._json(
            arena, "GET", f"/api/scheduler/scenarios/{scenario_id}")
        self.assertEqual(status, 200, allowed)
        self.assertEqual(allowed["scenario_id"], scenario_id)
        status, listed = self._json(arena, "GET", "/api/scheduler/scenarios")
        self.assertEqual(status, 200, listed)
        self.assertIn(scenario_id,
                      [row["scenario_id"] for row in listed["scenarios"]])
        # Order-independent, so a sibling test adding another A2a scenario
        # cannot make this pass or fail for the wrong reason: EVERY row the
        # list returns names this one exact tuple.
        self.assertEqual(
            {(row["scope"]["program_id"], row["scope"]["season_id"],
              row["scope"]["league_id"]) for row in listed["scenarios"]},
            {(pa, sa, la)})

    def test_http_matrix_over_the_real_route(self):
        pa, sa, la, da = corner(self.fixture, "A", "1", "a")
        pb, sb, lb, db = corner(self.fixture, "B", "1", "a")
        _pa2, sa2, la2, da2 = corner(self.fixture, "A", "2", "a")
        _pab, _sab, lab, dab = corner(self.fixture, "A", "1", "b")

        admin = self._login("admin")
        self._select(admin, pa, sa, la)

        # clause 5 -- the control. Real, non-empty work happens.
        status, mine = self._json(admin, "POST", "/api/scheduler/scenarios",
                                  {"name": "A1a over HTTP", "division_id": da})
        self.assertEqual(status, 200, mine)
        self.assertEqual(len(mine["proposal"]["draft_games"]), 6)
        status, fetched = self._json(
            admin, "GET", f"/api/scheduler/scenarios/{mine['scenario_id']}")
        self.assertEqual(status, 200, fetched)
        self.assertEqual(fetched, mine)

        # A scenario in each near-miss corner and in Program B, each created
        # while that corner is genuinely selected -- so the list assertions
        # below have something they COULD wrongly return.
        elsewhere = {}
        for tag, (program, season, league, division) in (
                ("A1b", (pa, sa, lab, dab)),
                ("A2a", (pa, sa2, la2, da2)),
                ("B1a", (pb, sb, lb, db))):
            self._select(admin, program, season, league)
            status, other = self._json(
                admin, "POST", "/api/scheduler/scenarios",
                {"name": f"{tag} over HTTP", "division_id": division})
            self.assertEqual(status, 200, other)
            elsewhere[tag] = other["scenario_id"]

        # clauses 1 + 2 + 3, with Program B selected.
        self._select(admin, pb, sb, lb)
        status, denied_raw = self._raw(
            admin, "GET", f"/api/scheduler/scenarios/{mine['scenario_id']}")
        status_missing, missing_raw = self._raw(
            admin, "GET", "/api/scheduler/scenarios/schedule_scenario_nope")
        self.assertEqual(status, 404)
        self.assertEqual(status_missing, 404)
        self.assertEqual(
            denied_raw.replace(mine["scenario_id"], "<echoed>"),
            missing_raw.replace("schedule_scenario_nope", "<echoed>"))
        self.assertNotIn("A1a over HTTP", denied_raw)

        status, refused = self._json(
            admin, "POST", "/api/scheduler/scenarios",
            {"name": "Cross-program create", "division_id": da})
        self.assertEqual(status, 404, refused)
        self.assertEqual(refused["error"]["details"]["reason"],
                         "division_missing", refused)

        status, listed = self._json(admin, "GET", "/api/scheduler/scenarios")
        self.assertEqual(status, 200, listed)
        self.assertEqual([row["scenario_id"] for row in listed["scenarios"]],
                         [elsewhere["B1a"]])

        # clause 4 -- commit authority does not survive the switch, and the
        # refusal is indistinguishable from a guessed id.
        status, commit_denied = self._raw(
            admin, "POST",
            f"/api/scheduler/scenarios/{mine['scenario_id']}/commit", {})
        status_missing, commit_missing = self._raw(
            admin, "POST",
            "/api/scheduler/scenarios/schedule_scenario_nope/commit", {})
        self.assertEqual(status, 404, commit_denied)
        self.assertEqual(status_missing, 404)
        self.assertEqual(
            commit_denied.replace(mine["scenario_id"], "<echoed>"),
            commit_missing.replace("schedule_scenario_nope", "<echoed>"))
        self.assertFalse(any(
            row.action == "schedule_scenario_committed"
            for row in srv.STATE.api.store.all_setup_audit()))

        # clause 5 again, on the far side of the refusal: switching back
        # restores exactly the authority the switch removed.
        self._select(admin, pa, sa, la)
        status, committed = self._json(
            admin, "POST",
            f"/api/scheduler/scenarios/{mine['scenario_id']}/commit", {})
        self.assertEqual(status, 200, committed)
        self.assertEqual(len(committed["created"]), 6)
        created_ids = {row["game_id"] for row in committed["created"]}
        self.assertTrue(all(
            game.is_draft and not game.published
            for game in srv.STATE.api.store.all_games()
            if game.id in created_ids))


if __name__ == "__main__":
    unittest.main()
