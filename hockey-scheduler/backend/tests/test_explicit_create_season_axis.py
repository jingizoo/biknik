"""The SEASON half of the #409 create comparison, with the Program half SATISFIED.

WHY THIS FILE EXISTS — a measured hole, not a hypothetical one.

``ApiService._create_context_error`` ends in two comparisons, in this order::

    for index, parent_axes, programs, seasons in axis_parents:
        if "program" in parent_axes and programs and program.id not in programs:
            return None, index
        if ("season" in parent_axes and seasons
                and season is not None and season.id not in seasons):
            return None, index

The owner's ruling requires BOTH to carry weight: "removing the Season
comparison makes a Season-owned cross-context case fail." Before this file, it
did not. Deleting the second ``if`` outright left the entire suite GREEN on
Memory, SQLite and PostgreSQL — the clause was dead weight that any refactor
could have swept away without a single red test.

THE ROOT CAUSE, measured rather than guessed. The second clause was
instrumented to record, over the whole 175-module suite on Memory + SQLite,
every time it was REACHED and every time it DECIDED::

    SEASON_CLAUSE_REACHED_MATCHED kind=registration   597
    SEASON_CLAUSE_REACHED_MATCHED kind=division       398
    SEASON_CLAUSE_REACHED_MATCHED kind=league         296
    SEASON_CLAUSE_REACHED_MATCHED kind=game           128
    SEASON_CLAUSE_DECIDED         (any kind)            0
    PROGRAM_CLAUSE_DECIDED        (any kind)           10

1419 reaches, ZERO decisions. Two independent causes, and BOTH had to be fixed
by one fixture:

  * every Season-owned create in the suite that got as far as the Season
    comparison was standing in the RIGHT Season, so the clause had nothing to
    reject — it evaluated ``season.id in seasons`` and fell through;
  * every case that *was* cross-context — Program B selected, a Program-A
    parent named — was decided by the PROGRAM clause one line above and never
    reached the Season clause at all. Those are the 10 ``PROGRAM_CLAUSE_DECIDED``
    hits, and they include the Season-owned kinds (``league``, ``division``,
    ``registration``): being cross-*Program* is not the same experiment as
    being cross-*Season*, and only the first was ever run.

The missing state is therefore very specific, and no fixture in the suite could
produce it: the two-Program world in ``test_explicit_create_context.py`` gives
each Program exactly ONE Season, so "the saved Program is right and the saved
Season is wrong" is not constructible there. It needs ONE Program with TWO
Seasons.

WHAT THIS FILE PINS. One Program, two Seasons S1 and S2. The operator
explicitly selects ``(P, S2)`` and then aims every SEASON-OWNED create at S1's
records. The saved Program is IDENTICAL to the parents' Program — asserted, not
assumed — so the Program clause provably passes and only the Season clause can
refuse. Each such create must answer the generic
``"<Label> <id> not found."`` 404 and write zero entity rows, zero relationship
rows and zero audit rows.

Non-vacuity is carried by three controls in the SAME saved state:

  * ``test_the_program_axis_creates_still_succeed_in_that_very_state`` — with
    ``(P, S2)`` saved, the PROGRAM-axis creates (season / venue / rink /
    ice_slot / official / team / player) still return 200. This is what proves
    the refusals above come from the SEASON comparison: if the Program
    comparison were doing the work, these would fail too;
  * ``test_the_same_creates_succeed_in_the_matching_season`` — the identical
    requests against S1 with ``(P, S1)`` saved return 200, so the routes are
    not simply broken;
  * ``test_the_refusal_is_byte_identical_to_a_nonexistent_season`` — the
    refusal for a REAL, same-Program Season the caller has not selected is
    indistinguishable from one for an id that never existed, so the new
    refusal is not a fresh existence oracle over sibling Seasons.

FALSIFIER. Delete the Season clause quoted above and
``SeasonAxisMemoryTest`` / ``SeasonAxisSqliteTest`` / ``SeasonAxisPostgresTest``
all fail on ``league``, ``division``, ``registration`` and ``game``, which under
the mutation return 200 and durably write into a Season the operator never
chose (measured: ``league_3 season_id=season_1``, ``division_2``, ``streg_1``).

SCOPE NOTE — ``season_venue_access``. It is Season-owned in
``_CREATE_CONSUMED_AXES``, but its route (``seasons/<id>/venue-access``) goes
through ``_guarded_mutation``, not ``_guarded_create``: its Season argument is
an EXISTING record, so #369's ceiling refuses the unselected sibling Season
before #409 is consulted. It is asserted here as a refusal for completeness and
is explicitly NOT part of the falsifier set — deleting the create-side Season
clause does not change its answer, and claiming otherwise would be a false
teeth claim.

THREE STORES. ``InMemoryStore``, SQLite and PostgreSQL. The comparison reads an
``ActiveContext`` row under a lock and compares it against link triples walked
out of several tables — a dict lookup on one store, real queries with real NULL
semantics and real row locks on the others. A SKIP IS NOT A PASS: the
PostgreSQL classes announce loudly when ``TEST_DATABASE_URL`` is unset.
"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.domain import Role
from hockey_scheduler.web import server as srv

TZ = "America/Toronto"

_HTTPD = None
_THREAD = None
_PORT = None
_TMP_FILES = []
_SAVED_DATABASE_URL = None

# Every store table a refusal must leave untouched, plus the audit set. Named
# explicitly rather than derived, so a new table has to be added here on
# purpose instead of silently escaping the residue assertion.
_TABLES = (
    ("programs", "all_programs"), ("seasons", "all_seasons"),
    ("leagues", "all_leagues"), ("league_seasons", "all_league_seasons"),
    ("divisions", "all_divisions"), ("teams", "all_teams"),
    ("players", "all_players"), ("games", "all_games"),
    ("clubs", "all_clubs"), ("officials", "all_officials"),
    ("venues", "all_venues"), ("rinks", "all_rinks"),
    ("ice_slots", "all_ice_slots"),
    ("organizations", "all_organizations"),
    ("registrations", "all_season_team_registrations"),
)


def setUpModule():
    global _HTTPD, _THREAD, _PORT, _SAVED_DATABASE_URL
    _SAVED_DATABASE_URL = os.environ.get("DATABASE_URL")
    _HTTPD = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    _PORT = _HTTPD.server_address[1]
    _THREAD = threading.Thread(target=_HTTPD.serve_forever, daemon=True)
    _THREAD.start()


def tearDownModule():
    if _HTTPD is not None:
        _HTTPD.shutdown()
        _THREAD.join(timeout=5)
        _HTTPD.server_close()
    if _SAVED_DATABASE_URL is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = _SAVED_DATABASE_URL
    try:
        srv.STATE.reset(seed=False)
    except Exception:
        pass
    for path in _TMP_FILES:
        if os.path.exists(path):
            os.remove(path)


def _sqlite_url():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    _TMP_FILES.append(path)
    return path


def _postgres_url():
    return os.environ.get("TEST_DATABASE_URL")


_PG_SKIP = ("PostgreSQL not configured (TEST_DATABASE_URL) or psycopg missing "
            "— the #409 SEASON comparison was NOT exercised on PostgreSQL. A "
            "SKIP HERE IS NOT A PASS: the sibling-Season comparison runs "
            "against link triples read under real row locks with real NULL "
            "semantics, and none of that is proven by the in-memory store.")


class _SeasonAxisHarness:
    """ONE Program, TWO Seasons, and a fresh operator per test.

    The whole point of the fixture is the shape no other fixture in the suite
    can make: a saved Program that is CORRECT for every parent named below and
    a saved Season that is a DIFFERENT Season OF THAT SAME PROGRAM. That is the
    only state in which the Season comparison can be the deciding one.
    """

    DATABASE_URL = None       # None -> InMemoryStore

    # -- harness -----------------------------------------------------------
    def setUp(self):
        if self.DATABASE_URL is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = self.DATABASE_URL
        srv.STATE.reset(seed=False)
        self.store = srv.STATE.api.store
        self._seq = 0
        self.w = self._world()

    def _next(self, prefix):
        self._seq += 1
        return f"{prefix}-{self._seq}-{uuid.uuid4().hex[:6]}"

    def _client(self):
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))

    def _req(self, opener, method, path, body=None):
        """(status, raw_text, parsed). The RAW body is kept because the
        byte-identity of two refusals is part of the contract."""
        url = f"http://127.0.0.1:{_PORT}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with opener.open(req) as r:
                raw = r.read()
                return r.status, raw.decode(), json.loads(raw or b"{}")
        except urllib.error.HTTPError as e:
            raw = e.read()
            e.close()
            return e.code, raw.decode(), json.loads(raw or b"{}")

    def _operator(self):
        """A brand-new League Admin with NO persisted ActiveContext, signed in
        over real HTTP with a real session cookie. The absence of a saved row
        is asserted, so every "the operator chose X" claim below is a statement
        about what this account did in this test and not about execution order.
        """
        username = f"sx_{uuid.uuid4().hex[:10]}"
        account = srv.STATE.api.accounts.create_account(
            username, "demo", Role.LEAGUE_ADMIN)
        c = self._client()
        status, raw, _ = self._req(c, "POST", "/api/auth/login",
                                   {"username": username, "password": "demo"})
        self.assertEqual(status, 200, raw)
        self.assertIsNone(
            self.store.get_active_context(account.id),
            "the fixture operator already has a saved selection — every "
            "'no selection' assertion below would be vacuous")
        return c, account.id

    def _select(self, c, program_id, season_id):
        status, raw, resp = self._req(
            c, "POST", "/api/context",
            {"program_id": program_id, "season_id": season_id})
        self.assertEqual(status, 200, raw)
        self.assertEqual((resp.get("program") or {}).get("id"), program_id, raw)
        self.assertEqual((resp.get("season") or {}).get("id"), season_id, raw)
        return resp

    def _stand_in(self, season_id):
        """A signed-in operator whose SAVED row names the one Program and the
        given Season of it, with both halves read back off the store."""
        c, user_id = self._operator()
        self._select(c, self.w["program"], season_id)
        saved = self.store.get_active_context(user_id)
        self.assertIsNotNone(saved, "the explicit selection did not persist")
        self.assertEqual(saved.program_id, self.w["program"],
                         "the saved Program is not the fixture Program")
        self.assertEqual(saved.season_id, season_id,
                         "the saved Season is not the one just chosen")
        return c, user_id

    # -- fixture -----------------------------------------------------------
    def _world(self):
        """ONE Program; TWO Seasons of it, S1 and S2; and S1 fully populated.

        Built through the setup service with ``actor_id`` only — no identity,
        so none of the #409 gates run while the fixture is being laid down.
        Nobody has selected any of it.

        The two Seasons are the fixture's entire reason for existing. Because
        they share a Program, a saved ``(P, S2)`` satisfies the Program
        comparison against every S1 record below, and the Season comparison is
        the ONLY thing left that can refuse.
        """
        svc = srv.STATE.api.setup
        program = svc.create_program("Alpha Program", timezone_name=TZ)
        s1 = svc.create_season(program.id, "Season One")
        s2 = svc.create_season(program.id, "Season Two")
        league = svc.create_league(s1.id, "S1 League")
        division = svc.create_division(s1.id, "S1 Div", league_id=league.id)
        club = svc.create_club("Club")
        home = svc.create_team(club_id=club.id, name="Home",
                               program_id=program.id, league_id=league.id)
        away = svc.create_team(club_id=club.id, name="Away",
                               program_id=program.id, league_id=league.id)
        # A THIRD team, deliberately unregistered, so the registration probe
        # has a fresh target that the game probe's own registrations have not
        # already consumed.
        spare = svc.create_team(club_id=club.id, name="Spare",
                                program_id=program.id, league_id=league.id)
        # The game probe must fail (or succeed) for CONTEXT reasons, never
        # because its teams are not registered into the Division it names —
        # that is a 400 ``division_mismatch`` and it would hide whatever the
        # gate actually decided.
        svc.register_team_for_season(s1.id, home.id,
                                     division_id=division.id,
                                     league_id=league.id)
        svc.register_team_for_season(s1.id, away.id,
                                     division_id=division.id,
                                     league_id=league.id)
        org = svc.create_organization("Org")
        venue = svc.create_venue("Venue", organization_id=org.id,
                                 league_id=program.id)
        rink = svc.create_rink(venue.id, "Rink")
        # Same reasoning as the registrations: without the grant, a game create
        # answers 400 ``venue_access_missing`` and the positive control would
        # be measuring Slice E's ice rule instead of #409's Season comparison.
        svc.grant_season_venue_access(s1.id, venue.id)
        # A SECOND Venue that no Season has been granted, so the venue-access
        # probe below is refused by the GATE rather than by "already granted".
        venue2 = svc.create_venue("Spare Venue", organization_id=org.id,
                                  league_id=program.id)
        return {"program": program.id, "s1": s1.id, "s2": s2.id,
                "league": league.id, "division": division.id,
                "club": club.id, "home": home.id, "away": away.id,
                "spare": spare.id, "org": org.id, "venue": venue.id,
                "venue2": venue2.id, "rink": rink.id}

    def _ice_slot(self):
        """A fresh bookable slot on the fixture Rink. Made per call because two
        slots on one rink may not overlap and the game builder runs many
        times."""
        svc = srv.STATE.api.setup
        self._seq += 1
        start = (datetime(2031, 3, 1, tzinfo=timezone.utc)
                 + timedelta(days=self._seq))
        return svc.create_ice_slot(self.w["rink"], start,
                                   start + timedelta(minutes=90)).id

    # -- the SEASON-OWNED create matrix ------------------------------------
    #
    # ``(label, build)`` where ``build(season_id)`` returns
    # ``(method, path, body, echoed_label, echoed_id)``. Every entry names the
    # given Season — directly or through S1's League/Division — and NOTHING
    # else that could be cross-Program: the Program is the same one in every
    # case, which is exactly what makes the Season the only live question.
    def _season_owned_cases(self):
        w = self.w

        def league(season_id):
            return ("POST", "/api/v2/setup/league",
                    {"season_id": season_id, "name": self._next("L")},
                    "Season", season_id)

        def division(season_id):
            return ("POST", "/api/v2/setup/division",
                    {"league_id": w["league"], "season_id": season_id,
                     "name": self._next("D")},
                    "Season", season_id)

        def registration(season_id):
            return ("POST",
                    f"/api/v2/setup/seasons/{season_id}/team-registrations",
                    {"team_id": w["spare"], "league_id": w["league"]},
                    "Season", season_id)

        def game(season_id):
            return ("POST", "/api/v2/setup/game",
                    {"season_id": season_id, "league_id": w["league"],
                     "division_id": w["division"],
                     "home_team_id": w["home"], "away_team_id": w["away"],
                     "ice_slot_id": self._ice_slot()},
                    "Season", season_id)

        return [("league", league), ("division", division),
                ("registration", registration), ("game", game)]

    # ``(label, build)`` for the PROGRAM-axis creates, which consume no Season
    # axis at all. They are the control that isolates WHICH comparison refused.
    def _program_axis_cases(self):
        w = self.w

        def season(_ignored):
            return ("POST", "/api/v2/setup/season",
                    {"program_id": w["program"], "name": self._next("S")})

        def venue(_ignored):
            return ("POST", "/api/setup/venue",
                    {"name": self._next("V"), "league_id": w["program"]})

        def rink(_ignored):
            return ("POST", "/api/v2/setup/rink",
                    {"venue_id": w["venue"], "name": self._next("R")})

        def ice_slot(_ignored):
            self._seq += 1
            day = 1 + (self._seq % 27)
            return ("POST", "/api/v2/setup/ice-slot",
                    {"rink_id": w["rink"],
                     "start_time": f"2032-01-{day:02d}T18:00:00+00:00",
                     "end_time": f"2032-01-{day:02d}T19:30:00+00:00"})

        def official(_ignored):
            return ("POST", "/api/v2/setup/official",
                    {"name": self._next("O"), "home_club_id": w["club"]})

        def team(_ignored):
            return ("POST", "/api/setup/team",
                    {"club_id": w["club"], "name": self._next("T"),
                     "league_id": w["program"],
                     "division_id": w["division"]})

        def player(_ignored):
            return ("POST", "/api/v2/setup/player",
                    {"team_id": w["home"], "name": self._next("P"),
                     "position": "forward"})

        return [("season", season), ("venue(v1)", venue), ("rink", rink),
                ("ice_slot", ice_slot), ("official", official),
                ("team(v1)", team), ("player", player)]

    # -- observation -------------------------------------------------------
    def _snapshot(self):
        snap = {name: {r.id for r in getattr(self.store, getter)()}
                for name, getter in _TABLES}
        snap["audit"] = {(a.action, a.entity_id)
                         for a in self.store.all_setup_audit()}
        return snap

    def _assert_no_residue(self, before, why):
        after = self._snapshot()
        for name in before:
            self.assertEqual(
                before[name], after[name],
                f"{why}: a REFUSED create left residue in `{name}` — "
                f"added {sorted(after[name] - before[name])}, "
                f"removed {sorted(before[name] - after[name])}")

    @staticmethod
    def _blind(raw, *ids):
        for index, value in enumerate(ids):
            if value:
                raw = raw.replace(value, f"<ID{index}>")
        return raw


class _SeasonAxisMixin(_SeasonAxisHarness):

    # -- THE REGRESSION ----------------------------------------------------
    def test_a_sibling_season_refuses_every_season_owned_create(self):
        """THE ONE THE FALSIFIER KILLS.

        Saved ``(P, S2)``; every create names S1 records OF THE SAME PROGRAM P.
        The Program comparison therefore passes — asserted below off the store,
        so this cannot silently degrade into another cross-Program case — and
        the Season comparison is the only thing that can refuse.

        Delete the Season clause in ``_create_context_error`` and all four
        subtests return 200 and durably write into a Season the operator never
        chose.
        """
        for label, build in self._season_owned_cases():
            with self.subTest(create=label):
                c, user_id = self._stand_in(self.w["s2"])
                # NON-VACUITY, stated as an assertion rather than as prose: the
                # saved Program IS the parents' Program, and the saved Season
                # is NOT the Season being named, and both Seasons are real.
                saved = self.store.get_active_context(user_id)
                self.assertEqual(saved.program_id, self.w["program"])
                self.assertEqual(saved.season_id, self.w["s2"])
                self.assertNotEqual(self.w["s1"], self.w["s2"])
                for season_id in (self.w["s1"], self.w["s2"]):
                    row = self.store.get_season(season_id)
                    self.assertIsNotNone(
                        row, f"Season {season_id} vanished from the fixture")
                    self.assertEqual(
                        row.program_id, self.w["program"],
                        "the two fixture Seasons are not siblings — this case "
                        "has decayed into a cross-PROGRAM test and no longer "
                        "exercises the Season comparison at all")

                method, path, body, label_word, echoed = build(self.w["s1"])
                before = self._snapshot()
                status, raw, resp = self._req(c, method, path, body)
                self.assertEqual(
                    status, 404,
                    f"`{label}` was created in Season {self.w['s1']} while "
                    f"the operator had explicitly selected the SIBLING Season "
                    f"{self.w['s2']} of the same Program: {raw}")
                self.assertEqual(resp["error"]["code"], "not_found", raw)
                self.assertEqual(
                    resp["error"]["message"], f"{label_word} {echoed} not found.",
                    f"`{label}`: the refusal is not the generic parent "
                    f"not-found: {raw}")
                self._assert_no_residue(before, f"{label} / sibling Season")

    def test_the_program_axis_creates_still_succeed_in_that_very_state(self):
        """THE ISOLATING CONTROL, and the reason the test above is about the
        SEASON comparison specifically.

        Same saved ``(P, S2)``, same operator shape: the creates that consume
        only the Program axis must all still return 200. If the Program
        comparison were what refused above, these would fail too — and a
        Program whose operator is standing in its newest Season would be unable
        to grow a Venue, a Team or its next Season, which is the failure mode
        the two-axis rule exists to avoid.
        """
        for label, build in self._program_axis_cases():
            with self.subTest(create=label):
                c, _user_id = self._stand_in(self.w["s2"])
                method, path, body = build(None)
                status, raw, resp = self._req(c, method, path, body)
                self.assertEqual(
                    status, 200,
                    f"the PROGRAM-axis create `{label}` was refused while the "
                    f"operator stood in a real Season of its own Program — "
                    f"the Program comparison, not the Season one, is doing "
                    f"the refusing: {raw}")
                self.assertNotIn("error", resp, raw)

    def test_the_same_creates_succeed_in_the_matching_season(self):
        """THE POSITIVE CONTROL. Byte-for-byte the same requests, with S1
        selected instead of S2, must all succeed — otherwise the refusals above
        would be satisfied by a route that is simply broken."""
        for label, build in self._season_owned_cases():
            with self.subTest(create=label):
                c, _user_id = self._stand_in(self.w["s1"])
                method, path, body, _word, _echo = build(self.w["s1"])
                status, raw, resp = self._req(c, method, path, body)
                self.assertEqual(
                    status, 200,
                    f"`{label}` was refused under its OWN exact axes: {raw}")
                self.assertNotIn("error", resp, raw)

    def test_the_refusal_is_byte_identical_to_a_nonexistent_season(self):
        """The new refusal must not become an existence oracle over SIBLING
        Seasons. A real, same-Program Season the caller has not selected and an
        id that never existed must answer identically once the caller's own
        echoed id is masked.

        Only ``league`` is compared byte-for-byte: it is the one Season-owned
        create whose request names exactly ONE axis-bearing parent, so
        swapping that id for a ghost changes the ANSWER rather than which
        question was asked.
        """
        c, _user_id = self._stand_in(self.w["s2"])
        real = self.w["s1"]
        ghost = "season_never_existed"

        before = self._snapshot()
        status, raw, _ = self._req(
            c, "POST", "/api/v2/setup/league",
            {"season_id": real, "name": self._next("L")})
        self._assert_no_residue(before, "league / sibling Season")

        before = self._snapshot()
        gstatus, graw, _ = self._req(
            c, "POST", "/api/v2/setup/league",
            {"season_id": ghost, "name": self._next("L")})
        self._assert_no_residue(before, "league / nonexistent Season")

        self.assertEqual(
            (gstatus, self._blind(graw, ghost)),
            (status, self._blind(raw, real)),
            "an unselected SIBLING Season and a nonexistent one answer "
            f"differently, which is an existence oracle ({raw} vs {graw})")
        self.assertNotIn(
            self.w["s2"], raw,
            "the refusal names the operator's own saved Season back at them "
            "on a request that never mentioned it")

    def test_the_season_scoped_mutation_family_refuses_the_sibling_too(self):
        """COMPLETENESS, explicitly NOT part of the falsifier set.

        ``season_venue_access`` is Season-owned in ``_CREATE_CONSUMED_AXES``,
        but its route runs through ``_guarded_mutation`` because both of its
        arguments are EXISTING records — so #369's ceiling refuses the
        unselected sibling Season before #409's create comparison is reached,
        and deleting that comparison does not change this answer. It is pinned
        here so the file states the whole shape of the surface rather than only
        the part it can falsify.
        """
        c, _user_id = self._stand_in(self.w["s2"])
        before = self._snapshot()
        status, raw, resp = self._req(
            c, "POST", f"/api/v2/setup/seasons/{self.w['s1']}/venue-access",
            {"venue_id": self.w["venue2"]})
        self.assertEqual(
            status, 404,
            f"a venue-access grant landed in the sibling Season "
            f"{self.w['s1']} while {self.w['s2']} was selected: {raw}")
        self.assertEqual(resp["error"]["code"], "not_found", raw)
        self._assert_no_residue(before, "season_venue_access / sibling Season")


# ---------------------------------------------------------------------------
# Concrete backends. A SKIP IS NOT A PASS.
# ---------------------------------------------------------------------------
class SeasonAxisMemoryTest(_SeasonAxisMixin, unittest.TestCase):
    DATABASE_URL = None


class SeasonAxisSqliteTest(_SeasonAxisMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.DATABASE_URL = _sqlite_url()


class SeasonAxisPostgresTest(_SeasonAxisMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.DATABASE_URL = _postgres_url()
        if not cls.DATABASE_URL:
            raise unittest.SkipTest(_PG_SKIP)


if __name__ == "__main__":
    unittest.main()
